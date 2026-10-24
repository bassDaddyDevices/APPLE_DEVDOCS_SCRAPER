#!/usr/bin/env python3
"""Add an Osaurus-compatible `type` field to PHP and Apple reference Markdown.

Default is a dry run. --apply inserts a missing `type` immediately before the
front-matter closing delimiter. Existing metadata/content are otherwise kept
byte-for-byte. Classification uses Apple role/kind/documentType metadata and
PHP filename/headings/signatures. No YAML reserialization is performed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

APPLE_ROOT = Path("/Volumes/DEVELOPMENT/KNOWLEDGE/CODING/APPLE")
APPLE_ROOTS = [APPLE_ROOT / name for name in (
    "AUDIO_TOOLBOX", "CORE_MIDI", "CORE_ML", "CREATE_ML", "METAL",
    "MIDI_DRIVER_KIT", "SWIFT", "SWIFT_UI",
)]
PHP_ROOT = Path("/Volumes/DEVELOPMENT/KNOWLEDGE/CODING/WEB_DEV/php/php-manual")
DEFAULT_ROOTS = [PHP_ROOT, *APPLE_ROOTS]

APPLE_TYPE = {
    "article": "article",
    "sampleCode": "sample-code",
    "collectionGroup": "reference",
    "Global Variable": "constant",
    "Instance Property": "property",
    "Type Property": "property",
    "Structure": "structure",
    "Case": "case",
    "Enumeration Case": "case",
    "Type Alias": "type-alias",
    "Enumeration": "enumeration",
    "Protocol": "protocol",
    "Class": "class",
    "Instance Method": "method",
    "Type Method": "method",
    "Static Method": "method",
    "Associated Type": "associated-type",
    "Macro": "macro",
    "Function": "function",
    "Framework": "framework",
    "Keyword": "keyword",
    "Namespace": "namespace",
}


def frontmatter(text: str):
    lines = text.splitlines(keepends=True)
    bom = bool(lines and lines[0].startswith("\ufeff"))
    start = 1 if bom else 0
    if start >= len(lines) or lines[start].strip() != "---":
        return None, "missing opening --- delimiter"
    close = next((i for i in range(start + 1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if close is None:
        return None, "missing closing front-matter delimiter"
    return (lines, start, close), None


def scalar_value(lines: list[str], start: int, close: int, key: str) -> str | None:
    pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*:\s*(.*?)\s*(?:#.*)?$")
    for line in lines[start + 1:close]:
        match = pattern.match(line.rstrip("\r\n"))
        if match:
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return None


def apple_type(lines: list[str], start: int, close: int) -> str:
    role = scalar_value(lines, start, close, "role") or scalar_value(lines, start, close, "kind")
    if role in APPLE_TYPE:
        return APPLE_TYPE[role]
    if scalar_value(lines, start, close, "documentType") == "article":
        return "article"
    # The requested Apple trees are framework references; this also safely
    # classifies previously unrecognized symbol roles as general references.
    return "reference"


def php_type(path: Path, text: str) -> str:
    stem = path.stem.lower()
    head = "\n".join(text.splitlines()[:100])
    head_lower = head.lower()
    if stem.startswith("function."):
        return "function"
    if stem.startswith("class."):
        return "class"
    if stem.startswith("enum."):
        return "enumeration"
    if stem.startswith("interface."):
        return "interface"
    if stem.startswith("trait."):
        return "trait"
    # PHP magic/language attributes have a known dedicated page prefix.
    if stem.startswith("attribute.") or stem.startswith("language.attributes") or stem.startswith("reserved.attributes"):
        return "attribute"
    # Heading signatures distinguish PHP class properties, methods, and
    # procedural functions in the occasional mixed API pages.
    if re.search(r"^#\s+[^\n]*::\$[A-Za-z_]", head, re.M):
        return "property"
    if re.search(r"^#\s+[^\n]*::[A-Za-z_]", head, re.M):
        return "method"
    if re.search(r"^#\s+[^\n]*\bfunction\s+[A-Za-z_]", head, re.M):
        return "function"
    if stem.startswith(("book.", "ref.", "language.", "reserved.", "appendices.", "intro.")):
        return "reference"
    if stem.startswith(("install.", "about.", "faq.")):
        return "article"
    return "reference"


def infer_type(path: Path, text: str, lines: list[str], start: int, close: int) -> str:
    if PHP_ROOT == path or PHP_ROOT in path.parents:
        return php_type(path, text)
    return apple_type(lines, start, close)


def add_type(path: Path, apply: bool):
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return "ERROR", None, f"not UTF-8: {exc}"
    parsed, error = frontmatter(text)
    if error:
        return "ERROR", None, error
    lines, start, close = parsed
    existing_type = scalar_value(lines, start, close, "type")
    if existing_type is not None:
        if not existing_type:
            return "ERROR", None, "existing type is empty"
        return "EXISTING", existing_type, ""
    chosen = infer_type(path, text, lines, start, close)
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines[:close + 1]) else "\n"
    insertion = f"type: {chosen}{newline}"
    updated = "".join(lines[:close] + [insertion] + lines[close:])
    # Structural verification, deliberately without validating/reformatting
    # legacy YAML: some upstream PHP titles have unescaped backslashes.
    check, check_error = frontmatter(updated)
    if check_error:
        return "ERROR", None, f"post-insertion delimiter check failed: {check_error}"
    check_lines, check_start, check_close = check
    if scalar_value(check_lines, check_start, check_close, "type") != chosen:
        return "ERROR", None, "post-insertion type check failed"
    if apply:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(updated.encode("utf-8"))
            os.chmod(temp_name, path.stat().st_mode)
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    return ("ADDED" if apply else "WOULD_ADD"), chosen, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", type=Path, help="optional root directories; defaults to the nine requested folders")
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    parser.add_argument("--report", type=Path, help="write a TSV report of every Markdown file")
    args = parser.parse_args()
    roots = args.roots or DEFAULT_ROOTS
    missing = [root for root in roots if not root.is_dir()]
    if missing:
        parser.error("directory not found: " + ", ".join(str(p) for p in missing))
    paths = sorted({p for root in roots for p in root.rglob("*.md") if p.is_file()})
    statuses: Counter[str] = Counter()
    types: Counter[str] = Counter()
    report = ["status\ttype\tpath"]
    errors = 0
    for path in paths:
        status, typ, message = add_type(path, args.apply)
        statuses[status] += 1
        if typ:
            types[typ] += 1
        report.append(f"{status}\t{typ or ''}\t{path}")
        if status == "ERROR":
            errors += 1
            print(f"ERROR: {path}: {message}", file=sys.stderr)
    print(f"Markdown files scanned: {len(paths)}")
    print("Status: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))
    print("Type assignments: " + ", ".join(f"{k}={v}" for k, v in sorted(types.items())))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"Report: {args.report}")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
