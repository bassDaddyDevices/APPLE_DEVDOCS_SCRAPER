#!/usr/bin/env python3
"""
appledocs.py — polite incremental mirror of Apple's developer documentation
via the published Markdown endpoint.

Apple renders its DocC documentation client-side, but publishes a plain-text
Markdown version of every page for "Automated tools and assistive tools":

    https://developer.apple.com/documentation/metal
    -> https://developer.apple.com/documentation/metal.md

This script walks that graph from one or more seed paths and mirrors the
Markdown locally.

Design goals, in priority order:
  1. Be a good citizen. Honour robots.txt, identify honestly, rate limit,
     back off on 429/503, single connection.
  2. Be cheap on re-runs. Store ETag / Last-Modified and send conditional
     requests. A refresh of an unchanged corpus is a few hundred KB of
     headers, not tens of MB of body.
  3. Be resumable. SQLite manifest; Ctrl-C and restart loses nothing.

Standard library only. Python 3.9+.

Usage:
    ./appledocs.py --seed /documentation/metal
    ./appledocs.py --seed /documentation/metal --seed /documentation/metalfx
    ./appledocs.py --seed /documentation/metal --refresh      # re-check known pages
    ./appledocs.py --seed /documentation/metal --max-pages 20 --dry-run
    ./appledocs.py --status
"""

from __future__ import annotations

import argparse
import email.utils
import gzip
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HOST = "https://developer.apple.com"

# Identify honestly, but note a real constraint discovered by testing:
#
#   Apple's edge returns 403 for any User-Agent containing a URL or an email
#   address. The conventional polite-bot form -- "mybot/1.0 (+https://.../bot)"
#   -- is exactly what gets blocked. A plain product token plus a parenthetical
#   description is accepted.
#
# So: describe yourself truthfully, do NOT impersonate a browser, and do not
# put a contact URL or mailto in here or every request will 403.
VERSION = "2.0"
USER_AGENT = "appledocs-mirror/1.0 (personal offline documentation mirror)"

# --------------------------------------------------------------------------
# Link extraction
# --------------------------------------------------------------------------

# Markdown links to doc paths:      [text](/documentation/Metal/MTLDevice)
RE_MD_LINK = re.compile(r"\]\(\s*(/documentation/[^)\s#?]+)")
# Absolute links to the same:       https://developer.apple.com/documentation/...
RE_ABS_LINK = re.compile(
    r"https://developer\.apple\.com(/documentation/[^)\s\"'<>#?.!,]+)"
)
# DocC internal refs:               <doc://com.apple.documentation/documentation/MetalFX>
RE_DOC_SCHEME = re.compile(r"doc://[^/]+(/documentation/[^\s`>\"')]+)")
# Any Apple documentation path appearing in Markdown or DocC text.
RE_DOC_PATH = re.compile(r"(?<![A-Za-z0-9_])(/documentation/[A-Za-z0-9_./~%+:-]+?)(?=[\s`\"'<>#?.,!):;]|$)")

SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf",
    ".zip", ".mp4", ".m3u8", ".json", ".css", ".js",
)


def extract_links(markdown: str) -> set[str]:
    """Pull every /documentation/... path out of a fetched Markdown page."""
    found: set[str] = set()
    for rx in (RE_MD_LINK, RE_ABS_LINK, RE_DOC_SCHEME, RE_DOC_PATH):
        for m in rx.finditer(markdown):
            found.add(m.group(1))
    return found


def normalise(path: str) -> str | None:
    """
    Canonicalise a doc path into the form used as the primary key.

    Apple's paths are case-insensitive in practice but appear with mixed case
    (/documentation/Metal/... in links, /documentation/metal/... in search
    results). Lowercase them so the same page isn't fetched twice.
    """
    path = urllib.parse.unquote(path)
    path = path.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if not path.startswith("/documentation/"):
        return None
    if path.lower().endswith(SKIP_SUFFIXES):
        return None
    if path.endswith(".md"):
        path = path[:-3]
    # Apple's own doc-scheme refs sometimes double up the prefix.
    path = re.sub(r"^(/documentation)+", "/documentation", path)
    return path.lower()


def local_path(out_dir: Path, doc_path: str) -> Path:
    """Mirror /documentation/metal/mtldevice -> <out>/documentation/metal/mtldevice.md"""
    rel = doc_path.lstrip("/")
    # Guard against traversal from a malformed link.
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    return out_dir.joinpath(*parts).with_suffix(".md")


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    path          TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued|ok|gone|error
    http_status   INTEGER,
    etag          TEXT,
    last_modified TEXT,
    sha256        TEXT,
    bytes         INTEGER,
    depth         INTEGER NOT NULL DEFAULT 0,
    fetched_at    TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON pages(status);
"""


class Manifest:
    def __init__(self, db_path: Path, memory: bool = False):
        if memory:
            self.db = sqlite3.connect(":memory:")
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(db_path, timeout=30)
            self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def enqueue(self, path: str, depth: int) -> bool:
        """Insert if unseen. Returns True if newly added."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO pages (path, depth) VALUES (?, ?)",
            (path, depth),
        )
        return cur.rowcount > 0

    def next_batch(self, limit: int = 200, excluded: set[str] | None = None) -> list[tuple[str, int]]:
        excluded = excluded or set()
        if not excluded:
            return self.db.execute(
                "SELECT path, depth FROM pages WHERE status='queued' ORDER BY depth, path LIMIT ?",
                (limit,),
            ).fetchall()
        marks = ",".join("?" for _ in excluded)
        return self.db.execute(
            f"SELECT path, depth FROM pages WHERE status='queued' AND path NOT IN ({marks}) "
            "ORDER BY depth, path LIMIT ?", (*excluded, limit)
        ).fetchall()

    def requeue_all_ok(self) -> int:
        cur = self.db.execute("UPDATE pages SET status='queued' WHERE status IN ('ok', 'error')")
        self.db.commit()
        return cur.rowcount

    def validators(self, path: str) -> tuple[str | None, str | None]:
        row = self.db.execute(
            "SELECT etag, last_modified FROM pages WHERE path=?", (path,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def record(self, path: str, **kw) -> None:
        kw["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cols = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(
            f"UPDATE pages SET {cols} WHERE path=?", (*kw.values(), path)
        )
        self.db.commit()

    def counts(self) -> dict[str, int]:
        return dict(
            self.db.execute("SELECT status, COUNT(*) FROM pages GROUP BY status")
        )

    def total_bytes(self) -> int:
        return self.db.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM pages WHERE status='ok'"
        ).fetchone()[0]


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

@dataclass
class Result:
    code: int
    body: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    retry_after: float | None = None
    error: str | None = None


class Fetcher:
    """One connection, rate limited, conditional, with backoff."""

    def __init__(self, delay: float, jitter: float, timeout: float, user_agent: str = USER_AGENT):
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.user_agent = user_agent
        self._last = 0.0
        self.opener = urllib.request.build_opener()
        self.opener.addheaders = []  # we set headers per request

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        target = self.delay + random.uniform(0, self.jitter)
        if gap < target:
            time.sleep(target - gap)
        self._last = time.monotonic()

    def get(self, url: str, etag: str | None, last_mod: str | None) -> Result:
        self._wait()
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.1",
            "Accept-Encoding": "gzip",
        }
        # Conditional request: this is what makes re-runs nearly free.
        if etag:
            headers["If-None-Match"] = etag
        if last_mod:
            headers["If-Modified-Since"] = last_mod

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return Result(
                    code=resp.status,
                    body=raw.decode("utf-8", errors="replace"),
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                )
        except urllib.error.HTTPError as e:
            ra = e.headers.get("Retry-After") if e.headers else None
            retry_after = parse_retry_after(ra)
            detail = ""
            try:
                detail = e.read(512).decode("utf-8", errors="replace").strip().replace("\n", " ")
            except OSError:
                pass
            error = f"HTTP {e.code}: {e.reason}" + (f" ({detail[:300]})" if detail else "")
            return Result(code=e.code, retry_after=retry_after, error=error)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"network error: {e}") from e


# --------------------------------------------------------------------------
# Crawl
# --------------------------------------------------------------------------

def check_robots(seeds: list[str], user_agent: str = USER_AGENT, verbose: bool = True) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{HOST}/robots.txt")
    # RobotFileParser.read() does not send our UA, so do it manually.
    # Retry: a single transient 403/5xx at the CDN edge should not abort a run.
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                f"{HOST}/robots.txt",
                headers={"User-Agent": user_agent, "Accept": "text/plain, */*"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                rp.parse(resp.read().decode("utf-8", errors="replace").splitlines())
                break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 403 and attempt == 0:
                # Almost always a bad User-Agent, not a real ban.
                print("!! 403 fetching robots.txt.", file=sys.stderr)
                print("   Apple's edge rejects any User-Agent containing a URL "
                      "or email address.", file=sys.stderr)
                print(f"   Current UA: {USER_AGENT}", file=sys.stderr)
                if re.search(r"https?://|@|mailto:", USER_AGENT):
                    print("   ^ this contains a URL or email — remove it.",
                          file=sys.stderr)
                    raise SystemExit(2)
            time.sleep(2 ** attempt)
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    else:
        print(f"!! could not read robots.txt after 4 tries ({last_err}).",
              file=sys.stderr)
        print("   Refusing to crawl without it.", file=sys.stderr)
        raise SystemExit(2)

    blocked = [s for s in seeds if not rp.can_fetch(user_agent, HOST + s + ".md")]
    if blocked:
        print("!! robots.txt disallows these seeds:", file=sys.stderr)
        for b in blocked:
            print(f"     {b}", file=sys.stderr)
        print("   Stopping. Do not work around this.", file=sys.stderr)
        raise SystemExit(3)

    if verbose:
        delay = rp.crawl_delay(user_agent)
        rrate = rp.request_rate(user_agent)
        print(f"   robots.txt: seeds allowed"
              + (f", crawl-delay={delay}s" if delay else "")
              + (f", request-rate={rrate.requests}/{rrate.seconds}s" if rrate else ""))
    return rp


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After as seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def sitemap_paths(url: str, timeout: float = 20.0, limit: int = 5000) -> list[str]:
    """Fetch a bounded XML sitemap and return documentation paths."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read(2_000_000).decode("utf-8", errors="replace")
    found = []
    for match in re.finditer(r"<loc>\s*(?:https://developer\.apple\.com)?([^<]+?)\s*</loc>", text, re.I):
        path = normalise(match.group(1))
        if path and path not in found:
            found.append(path)
            if len(found) >= limit:
                break
    return found


def path_has_prefix(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


# The web console runs this module through scraper_worker.py and parses stdout
# as JSON lines, so the JSON event stream stays the default. A direct CLI run
# installs a Progress instead and gets one self-updating counter line.
PROGRESS: "Progress | None" = None


class Progress:
    """Terminal-friendly stand-in for the per-page JSON event stream."""

    def __init__(self, live: bool = True) -> None:
        self.live = live
        self.saved = 0
        self.unchanged = 0
        self.failed = 0
        self.width = 0

    def handle(self, event: str, data: dict) -> bool:
        """Consume an event; return False to let it fall through to JSON."""
        if event == "page":
            status = data.get("status")
            if status in ("saved", "dry-run"):
                self.saved += 1
            elif status == "unchanged":
                self.unchanged += 1
            elif status == "error":
                self.failed += 1
            self.draw()
            return True
        if event == "complete":
            self.clear()
            return True
        if event == "sitemap":
            self.note(f"   sitemap {data.get('url')}: {data.get('discovered', 0)} paths")
            return True
        if event == "sitemap_error":
            self.note(f"!! sitemap {data.get('url')} failed: {data.get('error')}")
            return True
        return False

    def draw(self) -> None:
        if not self.live:
            return
        line = f"   saved {self.saved:,} page" + ("" if self.saved == 1 else "s")
        if self.unchanged:
            line += f", {self.unchanged:,} unchanged"
        if self.failed:
            line += f", {self.failed:,} failed"
        print("\r" + line.ljust(self.width), end="", flush=True)
        self.width = len(line)

    def clear(self) -> None:
        if self.live and self.width:
            print("\r" + " " * self.width + "\r", end="", flush=True)
            self.width = 0

    def note(self, message: str) -> None:
        """Print a one-off line without mangling the counter."""
        self.clear()
        print(message)
        self.draw()


def note(message: str) -> None:
    """Print an occasional status line, counter-safe."""
    if PROGRESS is not None:
        PROGRESS.note(message)
    else:
        print(message)


def emit_event(event: str, **data) -> None:
    if PROGRESS is not None and PROGRESS.handle(event, data):
        return
    print(json.dumps({"event": event, **data}, sort_keys=True), flush=True)


def crawl(args: argparse.Namespace) -> dict:
    out_dir = Path(args.out).expanduser()
    mf = Manifest(out_dir / ".manifest.sqlite", memory=args.dry_run)

    seeds = [normalise(s) for s in args.seed]
    seeds = [s for s in seeds if s]
    if not seeds:
        print("!! no valid seeds (must start with /documentation/)", file=sys.stderr)
        raise SystemExit(1)

    rp = None
    if not args.ignore_robots:
        rp = check_robots(seeds, args.user_agent)
    else:
        print("!! --ignore-robots set. You are on your own.", file=sys.stderr)

    # Respect a server-declared crawl-delay if it is slower than ours.
    delay = args.delay
    if rp:
        declared = rp.crawl_delay(args.user_agent)
        if declared and declared > delay:
            print(f"   honouring robots.txt crawl-delay of {declared}s")
            delay = float(declared)

    if args.refresh:
        n = mf.requeue_all_ok()
        print(f"   re-queued {n} known pages for conditional re-check")

    for s in seeds:
        if mf.enqueue(s, 0):
            print(f"   seeded {s}")

    fetcher = Fetcher(delay=delay, jitter=args.jitter, timeout=args.timeout, user_agent=args.user_agent)
    prefixes = tuple(p.lower() for p in (args.prefix or seeds))

    fetched = unchanged = failed = discovered = sitemap_discovered = 0
    for sitemap in getattr(args, "sitemap", []) or []:
        try:
            paths = sitemap_paths(sitemap, timeout=args.timeout)
            for path in paths:
                if mf.enqueue(path, 0):
                    sitemap_discovered += 1
            emit_event("sitemap", url=sitemap, discovered=len(paths), queued=sitemap_discovered)
        except (OSError, urllib.error.URLError, ValueError) as error:
            emit_event("sitemap_error", url=sitemap, error=str(error))
    backoff = 0.0
    hit_limit = False
    retry_counts: dict[str, int] = {}
    exhausted: set[str] = set()
    started = time.monotonic()

    try:
        while True:
            batch = mf.next_batch(excluded=exhausted)
            if not batch:
                break

            for path, depth in batch:
                if args.max_pages and fetched + unchanged >= args.max_pages:
                    note(f"   hit --max-pages limit ({args.max_pages})")
                    hit_limit = True
                    raise KeyboardInterrupt

                url = f"{HOST}{path}.md"

                if rp and not rp.can_fetch(args.user_agent, url):
                    if not args.dry_run:
                        mf.record(path, status="gone", error="robots.txt disallow")
                    continue

                if args.dry_run:
                    emit_event("page", path=path, status="dry-run")
                    fetched += 1
                    continue

                if backoff:
                    note(f"   backing off {backoff:.0f}s")
                    time.sleep(backoff)

                etag, last_mod = mf.validators(path)
                try:
                    r = fetcher.get(url, etag, last_mod)
                except RuntimeError as e:
                    failed += 1
                    emit_event("page", path=path, status="error", error=str(e))
                    backoff = min(max(backoff * 2, 5.0), 300.0)
                    continue

                if r.code == 304:
                    unchanged += 1
                    backoff = 0.0
                    mf.record(path, status="ok", http_status=304)
                    local = local_path(out_dir, path)
                    if depth < args.max_depth and local.exists():
                        for link in extract_links(local.read_text(encoding="utf-8", errors="replace")):
                            n = normalise(link)
                            if not n or not any(path_has_prefix(n, prefix) for prefix in prefixes):
                                continue
                            if mf.enqueue(n, depth + 1):
                                discovered += 1
                    emit_event("page", path=path, status="unchanged", discovered=discovered)
                    continue

                if r.code in (429, 503):
                    retry_counts[path] = retry_counts.get(path, 0) + 1
                    if retry_counts[path] >= args.max_retries:
                        exhausted.add(path)
                        mf.record(path, status="queued", http_status=r.code,
                                  error=f"{r.error or 'temporary HTTP error'}; retry cap reached")
                        note(f"!! {r.code} on {path}; retry cap reached, left queued")
                        continue
                    wait = r.retry_after or max(backoff * 2, 10.0)
                    backoff = min(wait, 300.0)
                    note(f"!! {r.code} on {path}; pausing {backoff:.0f}s")
                    # Leave it queued so it is retried.
                    continue

                if r.code == 404:
                    # Plenty of paths have no .md rendering. Normal, not an error.
                    mf.record(path, status="gone", http_status=404)
                    if args.verbose:
                        note(f"   404 {path}")
                    continue

                if r.code != 200 or r.body is None:
                    failed += 1
                    mf.record(path, status="error", http_status=r.code, error=r.error or f"HTTP {r.code}")
                    continue

                backoff = 0.0
                body = r.body
                dest = local_path(out_dir, path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(dest, body)

                mf.record(
                    path,
                    status="ok",
                    http_status=200,
                    etag=r.etag,
                    last_modified=r.last_modified,
                    sha256=hashlib.sha256(body.encode()).hexdigest(),
                    bytes=len(body.encode()),
                    error=None,
                )
                fetched += 1
                emit_event("page", path=path, status="saved", bytes=len(body.encode()))

                if depth < args.max_depth:
                    for link in extract_links(body):
                        n = normalise(link)
                        if not n or not any(path_has_prefix(n, prefix) for prefix in prefixes):
                            continue
                        if mf.enqueue(n, depth + 1):
                            discovered += 1

    except KeyboardInterrupt:
        if not hit_limit:
            note("   interrupted — progress saved, re-run to resume")

    elapsed = time.monotonic() - started
    if PROGRESS is not None:
        PROGRESS.clear()
    print(f"\n   fetched {fetched}, unchanged {unchanged}, failed {failed}, "
          f"newly discovered {discovered}")
    print(f"   {elapsed:.1f}s elapsed, {mf.total_bytes():,} bytes mirrored total")
    print(f"   -> {out_dir}")
    metrics = {"fetched": fetched, "unchanged": unchanged, "failed": failed,
               "discovered": discovered, "sitemap_discovered": sitemap_discovered,
               "bytes": mf.total_bytes(), "elapsed": round(elapsed, 1)}
    emit_event("complete", **metrics)
    return metrics


def status(args: argparse.Namespace) -> None:
    out_dir = Path(args.out).expanduser()
    db = out_dir / ".manifest.sqlite"
    if not db.exists():
        print("no manifest yet")
        return
    mf = Manifest(db)
    for k, v in sorted(mf.counts().items()):
        print(f"   {k:<8} {v:>6}")
    print(f"   {'bytes':<8} {mf.total_bytes():>6,}")


def seed_from_input(value: str) -> str | None:
    """
    Turn whatever the user pasted into a seed path.

    Accepts a full developer.apple.com URL, a bare /documentation/... path, or
    the same without the leading slash.
    """
    value = value.strip().strip('"').strip("'")
    if not value:
        return None
    if "://" in value:
        parsed = urllib.parse.urlparse(value)
        if parsed.netloc and not parsed.netloc.endswith("developer.apple.com"):
            return None
        value = parsed.path
    if not value.startswith("/"):
        value = "/" + value
    return normalise(value)


def prompt_for_seed(args: argparse.Namespace) -> None:
    """Ask for the seed (and output dir) when the script is run with no flags."""
    print("Apple developer docs mirror — paste the page to start from.")
    print("  e.g. https://developer.apple.com/documentation/metal")
    while not args.seed:
        seed = seed_from_input(input("URL or /documentation/... path: "))
        if seed:
            args.seed = [seed]
        else:
            print("  ^ needs to be a developer.apple.com /documentation/... URL or path")
    default_out = f"output-{args.seed[0].rsplit('/', 1)[-1]}"
    args.out = input(f"Output directory [{default_out}]: ").strip() or default_out
    print(f"\nMirroring {HOST}{args.seed[0]} into {args.out}\n")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Polite incremental mirror of Apple developer docs (.md endpoint)"
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--user-agent", default=USER_AGENT, help="HTTP User-Agent")
    p.add_argument("--max-retries", type=int, default=4, help="maximum 429/503 attempts per page")
    p.add_argument("--seed", action="append", default=[],
                   help="doc path to start from, e.g. /documentation/metal "
                        "(repeatable)")
    p.add_argument("--prefix", action="append", default=[],
                   help="restrict crawl to these path prefixes "
                        "(defaults to the seeds)")
    p.add_argument("--out", default="~/AppleDocs", help="output directory")
    p.add_argument("--sitemap", action="append", default=[], help="optional sitemap URL (repeatable)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds between requests (default 1.0)")
    p.add_argument("--jitter", type=float, default=0.4,
                   help="extra random delay up to N seconds (default 0.4)")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--max-depth", type=int, default=6)
    p.add_argument("--max-pages", type=int, default=0,
                   help="stop after N pages (0 = unlimited)")
    p.add_argument("--refresh", action="store_true",
                   help="re-check already-fetched pages with conditional requests")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--status", action="store_true", help="print manifest summary and exit")
    p.add_argument("--ignore-robots", action="store_true",
                   help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.max_retries < 1:
        p.error("--max-retries must be at least 1")

    if args.status:
        status(args)
        return

    # Direct CLI run: swap the JSON event stream for a progress counter.
    # --verbose keeps the old per-page event lines.
    if not args.verbose:
        global PROGRESS
        PROGRESS = Progress(live=sys.stdout.isatty())
    if not args.seed:
        if sys.stdin.isatty():
            try:
                prompt_for_seed(args)
            except (EOFError, KeyboardInterrupt):
                print("\naborted")
                return
        else:
            p.error("need at least one --seed (or use --status)")
    crawl(args)


if __name__ == "__main__":
    main()
