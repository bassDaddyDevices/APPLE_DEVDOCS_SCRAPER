#!/usr/bin/env python3
"""
apple_docs_to_kb.py — scrape Apple developer docs straight into the knowledge
base, in one pass.

Per page:

    developer.apple.com/documentation/...   polite incremental mirror
      -> metadata normalised to YAML front matter
      -> Osaurus `type:` field added
      -> filed as KNOWLEDGE/CODING/APPLE/<FRAMEWORK>/<path>.md

The crawl is appledocsscraper.py, imported unchanged: same robots.txt
handling, rate limiting, conditional requests and SQLite manifest, so re-runs
are cheap and resumable. The mirror is kept as the staging area; the knowledge
base only ever receives finished files.

Apple's .md endpoint ships metadata as a JSON HTML comment:

    <!--
    { "documentType" : "symbol", "framework" : "Metal", ... }
    -->

The knowledge base stores the same fields as YAML front matter, with the
symbol sub-object flattened to kind/module/preciseIdentifier and `type:` last.
That is what this script emits; its output is byte-identical to the 17k pages
already filed. Classification reuses add_osaurus_types.apple_type, and the
front-matter repairs are the ones from repair_frontmatter.py, applied in
memory on the way through rather than as a later clean-up pass.

Run with no arguments and it prompts, as the scraper does.

    ./apple_docs_to_kb.py
    ./apple_docs_to_kb.py --seed /documentation/coreai
    ./apple_docs_to_kb.py --seed /documentation/metal --refresh
    ./apple_docs_to_kb.py --publish-only                  # re-file the mirror
    ./apple_docs_to_kb.py --convert-dir CORE_AI --apply   # fix files already filed

Standard library only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import appledocsscraper as scraper
from add_osaurus_types import apple_type
from add_osaurus_types import frontmatter as split_front_matter
from add_osaurus_types import scalar_value

VERSION = "1.0"
KB_ROOT = Path("/Volumes/DEVELOPMENT/KNOWLEDGE/CODING/APPLE")
MIRROR_ROOT = Path("~/AppleDocs")

# Key order used by every page already in the knowledge base. Anything Apple
# adds later is appended, sorted, after these; `type:` always goes last.
ORDER = ("title", "framework", "documentType", "role", "kind", "module",
         "identifier", "preciseIdentifier", "availability")
# Internal to Apple's feed, not worth storing.
DROP = ("metadataVersion",)


# --------------------------------------------------------------------------
# YAML front matter (emit)
# --------------------------------------------------------------------------
#
# A full YAML library is overkill for a flat map of strings and string lists,
# and PyYAML is not installed here. These two functions reproduce PyYAML's
# block style and minimal-quoting rules for exactly that shape, which is what
# generated the existing corpus.

# Scalars matching one of YAML 1.1's implicit resolvers must be quoted or they
# would read back as a bool, number, date or null instead of a string.
RESOLVED = re.compile(r"""^(?:
    ~|null|Null|NULL|
   |yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF
   |[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+
   |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+
   |[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
   |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)
   |[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]
   |[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9].*
   |=|<<
)$""", re.X)

# Characters that open a different YAML construct when they lead a scalar.
LEADING = set(",[]{}#&*!|>'\"%@`")


def emit_scalar(value: str) -> str:
    """Render a string as a plain scalar where that is unambiguous, else single-quoted."""
    if (value == "" or value.strip() != value
            or RESOLVED.match(value)
            or value[0] in LEADING
            # ':', '?' and '-' only start a construct when whitespace follows.
            or (value[0] in "-?:" and (len(value) == 1 or value[1].isspace()))
            or any(ch in value for ch in "\n\r\t\x00")
            # A colon or comment marker mid-string would split the line.
            or ": " in value or value.endswith(":") or " #" in value):
        return "'" + value.replace("'", "''") + "'"
    return value


def emit_block(meta: dict) -> str:
    """Render the metadata map as the body of a front-matter block."""
    out = []
    for key, value in meta.items():
        if isinstance(value, list):
            out.append(f"{key}:\n")
            out.extend(f"- {emit_scalar(item)}\n" for item in value)
        else:
            out.append(f"{key}: {emit_scalar(value)}\n")
    return "".join(out)


def parse_block(lines: list[str]) -> dict:
    """Read back a block this script emitted, for verification."""
    meta: dict = {}
    key = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line.startswith("- ") and key is not None:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(read_scalar(line[2:]))
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name
        meta[key] = [] if value.strip() == "" else read_scalar(value.strip())
    return meta


# Every line this script writes is a key, a list header or a list item.
# Anything else in a block means the file was damaged before we saw it.
WELL_FORMED = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_.-]*:(?: .*)?|- .+)$")


def lint_block(lines: list[str]) -> str | None:
    for raw in lines:
        if not WELL_FORMED.match(raw.rstrip("\r\n")):
            return f"malformed front-matter line: {raw.strip()[:80]!r}"
    return None


def read_scalar(text: str):
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1]
    return text


# --------------------------------------------------------------------------
# Metadata extraction
# --------------------------------------------------------------------------

RE_JSON_HEADER = re.compile(r"\A﻿?<!--[ \t]*\n(\{.*?\n\})[ \t]*\n-->", re.S)


def coerce(value):
    """Keep strings and string lists; stringify anything else sanely."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return [coerce(item) for item in value]
    return json.dumps(value, sort_keys=True)


def metadata_from_json_header(text: str):
    """Apple's JSON comment header -> (ordered metadata, remaining body)."""
    match = RE_JSON_HEADER.match(text)
    if not match:
        return None
    raw = json.loads(match.group(1))
    if not isinstance(raw, dict):
        raise ValueError("header JSON is not an object")

    flat = {k: v for k, v in raw.items() if k not in DROP and k != "symbol"}
    symbol = raw.get("symbol") or {}
    if isinstance(symbol, dict):
        if symbol.get("kind"):
            flat["kind"] = symbol["kind"]
        modules = symbol.get("modules") or []
        if modules:
            # Apple sometimes lists the display name and the module name
            # ("Core AI", "CoreAI"); the corpus keeps the first.
            flat["module"] = modules[0]
        if symbol.get("preciseIdentifier"):
            flat["preciseIdentifier"] = symbol["preciseIdentifier"]

    meta = {key: coerce(flat.pop(key)) for key in ORDER if key in flat}
    for key in sorted(flat):
        meta[key] = coerce(flat[key])
    return meta, text[match.end():]


# --------------------------------------------------------------------------
# Front-matter repair (the rules from repair_frontmatter.py)
# --------------------------------------------------------------------------

KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
BROKEN_KEY_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*-$")
KEY_WITH_COLON = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):[ \t]*(.*)$")


def repair_block(block: list[str]) -> tuple[list[str], list[str]]:
    """Rejoin top-level keys that an earlier pass wrapped across two lines."""
    out: list[str] = []
    notes: list[str] = []
    i = 0
    while i < len(block):
        current = block[i]
        stripped = current.rstrip("\r\n")
        newline = current[len(stripped):]
        if i + 1 < len(block):
            nxt = block[i + 1].rstrip("\r\n")
            next_nl = block[i + 1][len(nxt):]
            # "title\n: X"
            if not stripped.startswith((" ", "\t")) and KEY.fullmatch(stripped) and nxt.startswith(":"):
                out.append(stripped + nxt + (next_nl or newline))
                notes.append("joined key and its separated colon")
                i += 2
                continue
            # "document-\nType: X"
            match = KEY_WITH_COLON.fullmatch(nxt)
            if not stripped.startswith((" ", "\t")) and BROKEN_KEY_PART.fullmatch(stripped) and match:
                out.append(stripped + match.group(1) + ": " + match.group(2) + (next_nl or newline))
                notes.append("joined a line-wrapped hyphenated key")
                i += 2
                continue
        out.append(current)
        i += 1
    return out, notes


# --------------------------------------------------------------------------
# The per-page transform
# --------------------------------------------------------------------------

class PageError(Exception):
    pass


def convert(text: str) -> tuple[str, str, dict]:
    """
    Normalise one page.

    Returns (finished text, a short note on what was done, metadata).
    Raises PageError if the page has no usable metadata header.
    """
    notes = []
    parsed = metadata_from_json_header(text)
    if parsed is not None:
        meta, body = parsed
        text = "---\n" + emit_block(meta) + "---" + body
        notes.append("converted JSON header")
    elif text.startswith("﻿"):
        text = text.lstrip("﻿")
        notes.append("removed UTF-8 BOM")

    span, error = split_front_matter(text)
    if error:
        raise PageError(error)
    lines, start, close = span

    block, repairs = repair_block(lines[start + 1:close])
    if repairs:
        lines = lines[:start + 1] + block + lines[close:]
        close = start + 1 + len(block)
        notes.extend(repairs)

    # `type:` goes in immediately before the closing delimiter, as
    # add_osaurus_types.py places it, and is classified the same way.
    if scalar_value(lines, start, close, "type") is None:
        chosen = apple_type(lines, start, close)
        newline = "\r\n" if any(line.endswith("\r\n") for line in lines[:close + 1]) else "\n"
        lines = lines[:close] + [f"type: {chosen}{newline}"] + lines[close:]
        close += 1
        notes.append(f"type: {chosen}")

    text = "".join(lines)

    # Verify what we are about to file: the delimiters still parse, every key
    # reads back as written, and `type` is present and non-empty.
    span, error = split_front_matter(text)
    if error:
        raise PageError(f"post-transform check failed: {error}")
    check_lines, check_start, check_close = span
    block = check_lines[check_start + 1:check_close]
    malformed = lint_block(block)
    if malformed:
        raise PageError(malformed)
    meta = parse_block(block)
    if parsed is not None:
        written = {k: v for k, v in meta.items() if k != "type"}
        if written != parsed[0]:
            differing = sorted(set(written) ^ set(parsed[0])) or [
                k for k in parsed[0] if written.get(k) != parsed[0][k]]
            raise PageError(f"front matter did not round-trip: {', '.join(differing)}")
    if not meta.get("type"):
        raise PageError("no type could be assigned")
    return text, "; ".join(notes) or "already normalised", meta


# --------------------------------------------------------------------------
# Where a page is filed
# --------------------------------------------------------------------------

WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def framework_folder(framework: str) -> str:
    """Metal -> METAL, AudioToolbox -> AUDIO_TOOLBOX, MIDIDriverKit -> MIDI_DRIVER_KIT."""
    parts = WORD.findall(framework)
    return "_".join(part.upper() for part in parts) if parts else framework.upper()


def existing_folder(kb_root: Path, name: str) -> str:
    """Reuse a folder that is already there rather than making a near-duplicate."""
    key = name.replace("_", "").replace("-", "").upper()
    if kb_root.is_dir():
        for child in sorted(kb_root.iterdir()):
            if child.is_dir() and child.name.replace("_", "").replace("-", "").upper() == key:
                return child.name
    return name


def read_seed(value: str) -> str | None:
    """scraper.seed_from_input, but also accepts a host pasted without a scheme."""
    text = value.strip().strip('"').strip("'")
    if re.match(r"^(?:www\.)?developer\.apple\.com/", text, re.I):
        text = "https://" + text
    return scraper.seed_from_input(text)


def doc_path_of(mirror: Path, md: Path) -> str:
    """documentation/metal/mtldevice.md -> /documentation/metal/mtldevice"""
    return "/" + md.relative_to(mirror).with_suffix("").as_posix()


def kb_destination(kb_root: Path, mirror: Path, md: Path, meta: dict, forced: str | None) -> Path:
    rel = md.relative_to(mirror).parts            # ("documentation", "metal", ...)
    framework = meta.get("framework")
    if not isinstance(framework, str) or not framework:
        framework = Path(rel[1]).stem if len(rel) > 1 else "MISC"
    folder = forced or existing_folder(kb_root, framework_folder(framework))
    # Everything below /documentation/<framework>/ keeps its shape; the
    # framework's own landing page keeps its name (METAL/metal.md).
    tail = rel[2:] if len(rel) > 2 else rel[1:]
    return kb_root.joinpath(folder, *tail)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temp_name, path.stat().st_mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------
# Publishing the mirror into the knowledge base
# --------------------------------------------------------------------------

def publish(mirror: Path, kb_root: Path, prefixes: tuple[str, ...], args) -> dict:
    docs = mirror / "documentation"
    if not docs.is_dir():
        print(f"!! nothing mirrored yet under {docs}", file=sys.stderr)
        return {"filed": 0, "unchanged": 0, "skipped": 0, "failed": 0}

    counts = {"filed": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    for md in sorted(docs.rglob("*.md")):
        if not md.is_file():
            continue
        doc_path = doc_path_of(mirror, md)
        if prefixes and not any(scraper.path_has_prefix(doc_path, p) for p in prefixes):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            counts["failed"] += 1
            print(f"!! {md}: {error}", file=sys.stderr)
            continue
        try:
            finished, note, meta = convert(text)
        except (PageError, ValueError, json.JSONDecodeError) as error:
            counts["skipped"] += 1
            if args.verbose:
                print(f"   skipped {doc_path}: {error}")
            continue
        dest = kb_destination(kb_root, mirror, md, meta, args.folder)
        if dest.exists() and dest.read_text(encoding="utf-8", errors="replace") == finished:
            counts["unchanged"] += 1
            continue
        if args.dry_run:
            counts["filed"] += 1
            if args.verbose:
                print(f"   would file {dest} ({note})")
            continue
        atomic_write(dest, finished)
        counts["filed"] += 1
        if args.verbose:
            print(f"   filed {dest} ({note})")
    return counts


def convert_in_place(target: Path, args) -> dict:
    """Normalise pages already sitting in the knowledge base."""
    counts = {"filed": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    for md in sorted(target.rglob("*.md")):
        if not md.is_file():
            continue
        try:
            text = md.read_text(encoding="utf-8")
            finished, note, _ = convert(text)
        except (OSError, UnicodeDecodeError) as error:
            counts["failed"] += 1
            print(f"!! {md}: {error}", file=sys.stderr)
            continue
        except (PageError, ValueError, json.JSONDecodeError) as error:
            counts["skipped"] += 1
            print(f"   skipped {md}: {error}")
            continue
        if finished == text:
            counts["unchanged"] += 1
            continue
        counts["filed"] += 1
        if args.apply:
            atomic_write(md, finished)
            print(f"   fixed {md} ({note})")
        else:
            print(f"   would fix {md} ({note})")
    return counts


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

def prompt_for_run(args) -> None:
    """Ask for the seed and destination, in the scraper's style."""
    print("Apple developer docs -> knowledge base.")
    print("  e.g. https://developer.apple.com/documentation/metal")
    while not args.seed:
        seed = read_seed(input("URL or /documentation/... path: "))
        if seed:
            args.seed = [seed]
        else:
            print("  ^ needs to be a developer.apple.com /documentation/... URL or path")

    kb = input(f"Knowledge base [{args.kb}]: ").strip()
    if kb:
        args.kb = Path(kb).expanduser()

    slug = args.seed[0].split("/")[2] if args.seed[0].count("/") > 1 else args.seed[0]
    suggested = existing_folder(args.kb, framework_folder(slug))
    folder = input(f"Folder [{suggested}]: ").strip()
    args.folder = folder or None
    shown = folder or f"{suggested} (or whatever each page reports)"

    print(f"\nMirroring {scraper.HOST}{args.seed[0]}")
    print(f"  staging        -> {args.mirror}")
    print(f"  knowledge base -> {args.kb / shown}\n")


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Scrape Apple developer docs into the knowledge base, front matter and Osaurus types included.")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--seed", action="append", default=[],
                   help="doc path or URL to start from, e.g. /documentation/metal (repeatable)")
    p.add_argument("--prefix", action="append", default=[],
                   help="restrict the crawl to these path prefixes (defaults to the seeds)")
    p.add_argument("--kb", type=Path, default=KB_ROOT, help=f"knowledge base root (default {KB_ROOT})")
    p.add_argument("--mirror", type=Path, default=MIRROR_ROOT,
                   help=f"staging mirror and manifest (default {MIRROR_ROOT})")
    p.add_argument("--folder", help="file every page of this run under this knowledge base folder")
    p.add_argument("--publish-only", action="store_true", help="skip the crawl; re-file what is already mirrored")
    p.add_argument("--convert-dir", type=Path,
                   help="normalise pages already in the knowledge base (a path, or a folder name under --kb)")
    p.add_argument("--apply", action="store_true", help="with --convert-dir, write the changes")
    p.add_argument("--sitemap", action="append", default=[], help="optional sitemap URL (repeatable)")
    p.add_argument("--user-agent", default=scraper.USER_AGENT, help="HTTP User-Agent")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1.0)")
    p.add_argument("--jitter", type=float, default=0.4, help="extra random delay up to N seconds")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--max-depth", type=int, default=6)
    p.add_argument("--max-pages", type=int, default=0, help="stop after N pages (0 = unlimited)")
    p.add_argument("--max-retries", type=int, default=4, help="maximum 429/503 attempts per page")
    p.add_argument("--refresh", action="store_true", help="re-check already-fetched pages with conditional requests")
    p.add_argument("--dry-run", action="store_true", help="fetch nothing, write nothing")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--status", action="store_true", help="print the mirror's manifest summary and exit")
    p.add_argument("--ignore-robots", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()

    args.kb = args.kb.expanduser()
    args.mirror = args.mirror.expanduser()
    if args.max_retries < 1:
        p.error("--max-retries must be at least 1")

    if args.status:
        scraper.status(argparse.Namespace(out=str(args.mirror)))
        return 0

    if args.convert_dir is not None:
        target = args.convert_dir.expanduser()
        if not target.is_dir():
            candidate = args.kb / args.convert_dir
            if not candidate.is_dir():
                p.error(f"not a directory: {target}")
            target = candidate
        print(f"{'Fixing' if args.apply else 'Checking'} {target}")
        counts = convert_in_place(target, args)
        verb = "fixed" if args.apply else "would fix"
        print(f"\n   {verb} {counts['filed']}, unchanged {counts['unchanged']}, "
              f"skipped {counts['skipped']}, failed {counts['failed']}")
        if not args.apply and counts["filed"]:
            print("   re-run with --apply to write these.")
        return 2 if counts["failed"] else 0

    seeds = [s for s in (read_seed(s) for s in args.seed) if s]
    if not seeds and not args.publish_only:
        if sys.stdin.isatty():
            try:
                prompt_for_run(args)
            except (EOFError, KeyboardInterrupt):
                print("\naborted")
                return 1
            seeds = [s for s in (read_seed(s) for s in args.seed) if s]
        else:
            p.error("need at least one --seed (or --publish-only, --convert-dir, --status)")
    args.seed = seeds

    if not args.verbose:
        scraper.PROGRESS = scraper.Progress(live=sys.stdout.isatty())

    if not args.publish_only:
        scraper.crawl(argparse.Namespace(
            out=str(args.mirror), seed=seeds, prefix=args.prefix, sitemap=args.sitemap,
            user_agent=args.user_agent, delay=args.delay, jitter=args.jitter,
            timeout=args.timeout, max_depth=args.max_depth, max_pages=args.max_pages,
            max_retries=args.max_retries, refresh=args.refresh, dry_run=args.dry_run,
            verbose=args.verbose, ignore_robots=args.ignore_robots,
        ))
        if args.dry_run:
            print("   dry run: nothing mirrored, so nothing to file")
            return 0

    scraper.PROGRESS = None
    prefixes = tuple(x.lower() for x in (args.prefix or seeds))
    print(f"   filing into {args.kb}")
    counts = publish(args.mirror, args.kb, prefixes, args)
    print(f"   filed {counts['filed']}, unchanged {counts['unchanged']}, "
          f"skipped {counts['skipped']}, failed {counts['failed']}")
    if counts["skipped"] and not args.verbose:
        print("   (--verbose lists the skipped pages)")
    return 2 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
