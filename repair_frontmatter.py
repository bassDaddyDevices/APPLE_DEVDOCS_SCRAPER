#!/usr/bin/env python3
"""Audit and conservatively repair Markdown YAML front matter.

By default this is a dry run. Use --apply to write verified changes. It only
joins clearly split top-level keys / key separators and removes a UTF-8 BOM
before the opening delimiter. Use --indent-list-items only as an explicit
compatibility normalization; indentless YAML sequences are valid YAML.
Requires PyYAML (python3 -m pip install pyyaml).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python3 -m pip install pyyaml")

KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
BROKEN_KEY_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*-$")
KEY_WITH_COLON = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):[ \t]*(.*)$")


def frontmatter_span(text: str):
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return lines, i
    return lines, None


def repair_block(block: list[str], indent_lists: bool) -> tuple[list[str], list[str]]:
    """Repair unambiguous wrapped keys; return updated lines and explanations."""
    out: list[str] = []
    changes: list[str] = []
    i = 0
    while i < len(block):
        current = block[i]
        stripped = current.rstrip("\r\n")
        newline = current[len(stripped):]
        if i + 1 < len(block):
            nxt = block[i + 1].rstrip("\r\n")
            next_nl = block[i + 1][len(nxt):]
            # A top-level key split immediately before its colon: "title\n: X".
            if not stripped.startswith((" ", "\t")) and KEY.fullmatch(stripped) and nxt.startswith(":"):
                out.append(stripped + nxt + (next_nl or newline))
                changes.append("joined key and its separated colon")
                i += 2
                continue
            # A hyphenated top-level key split at the line break: "document-\nType: X".
            match = KEY_WITH_COLON.fullmatch(nxt)
            if not stripped.startswith((" ", "\t")) and BROKEN_KEY_PART.fullmatch(stripped) and match:
                out.append(stripped + match.group(1) + ": " + match.group(2) + (next_nl or newline))
                changes.append("joined a line-wrapped hyphenated key")
                i += 2
                continue
        # Optional parser-compatibility formatting; standard YAML allows this
        # indentless sequence style, so never enable unless explicitly asked.
        if indent_lists and stripped.startswith("- ") and not current.startswith((" ", "\t")):
            if out and out[-1].rstrip("\r\n").endswith(":"):
                out.append("  " + stripped + newline)
                changes.append("indented a sequence item under its key")
                i += 1
                continue
        out.append(current)
        i += 1
    return out, changes


def validate_yaml(block: list[str]) -> str | None:
    try:
        yaml.safe_load("".join(block))
        return None
    except yaml.YAMLError as exc:
        return str(exc).splitlines()[0]


def atomic_write(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp_name, path.stat().st_mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def process(path: Path, apply: bool, indent_lists: bool):
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return "SKIP", f"not UTF-8 ({exc})"
    span = frontmatter_span(text)
    if span is None:
        return "SKIP", "no opening --- front matter delimiter"
    lines, close = span
    if close is None:
        return "ERROR", "no closing front matter delimiter"
    block, reasons = repair_block(lines[1:close], indent_lists)
    old_error = validate_yaml(lines[1:close])
    new_error = validate_yaml(block)
    changed = bom or block != lines[1:close]
    if new_error:
        detail = f"YAML invalid after safe repair: {new_error}"
        if old_error:
            detail = f"YAML invalid: {old_error}; {detail}"
        return "ERROR", detail
    if not changed:
        if old_error:
            return "FIXABLE", f"YAML became valid after: {', '.join(reasons)}"
        return "OK", "valid front matter"
    updated = "".join(lines[:1] + block + lines[close:])
    if apply:
        atomic_write(path, updated.encode("utf-8"))
        return "FIXED", ", ".join(((["removed UTF-8 BOM"] if bom else []) + reasons))
    return "WOULD_FIX", ", ".join(((["remove UTF-8 BOM"] if bom else []) + reasons))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parent,
                        help="root directory to scan (default: directory containing this script)")
    parser.add_argument("--apply", action="store_true", help="write verified repairs (default is dry run)")
    parser.add_argument("--indent-list-items", action="store_true",
                        help="also indent valid top-level YAML list items by two spaces")
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error(f"not a directory: {args.root}")
    counts = {k: 0 for k in ("OK", "FIXED", "WOULD_FIX", "FIXABLE", "ERROR", "SKIP")}
    for path in sorted(args.root.rglob("*.md")):
        status, detail = process(path, args.apply, args.indent_list_items)
        counts[status] += 1
        if status not in ("OK", "SKIP"):
            print(f"{status}: {path}: {detail}")
    print("Summary: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    if counts["ERROR"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
