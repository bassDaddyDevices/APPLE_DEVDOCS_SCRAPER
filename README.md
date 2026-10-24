# Apple DevDocs Scraper

Mirrors Apple's developer documentation as Markdown and files it into the local
knowledge base at `/Volumes/DEVELOPMENT/KNOWLEDGE/CODING/APPLE`, with YAML front
matter and an Osaurus-compatible `type:` field on every page.

Apple renders DocC client-side, but publishes a plain-text Markdown version of
every page for "automated tools and assistive tools":

    https://developer.apple.com/documentation/metal
    -> https://developer.apple.com/documentation/metal.md

That endpoint is what these scripts read. Nothing here scrapes rendered HTML or
works around a block.

## Requirements

Python 3.9+, standard library only. **Do not add a dependency on PyYAML** —
nothing on this machine has it installed, which is why `apple_docs_to_kb.py`
emits YAML by hand (see [Front matter](#front-matter)) and why
`repair_frontmatter.py` cannot currently run.

## Quick start

```sh
./apple_docs_to_kb.py                                   # prompts for a URL
./apple_docs_to_kb.py --seed /documentation/CoreAI      # or say it up front
./apple_docs_to_kb.py --seed /documentation/Metal --refresh
```

That is the whole pipeline: crawl, normalise, classify, file. Run it again
later with `--refresh` and it re-checks what it already has with conditional
requests — an unchanged corpus costs a few hundred KB of headers, not tens of MB
of bodies.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--max-pages N` | stop after N pages; good for a first look |
| `--dry-run` | fetch nothing, write nothing |
| `--publish-only` | skip the crawl, re-file what is already mirrored |
| `--convert-dir METAL` | normalise pages already in the knowledge base (add `--apply` to write) |
| `--folder NAME` | force this run's pages into one knowledge base folder |
| `--mirror PATH` | staging area and manifest (default `~/AppleDocs`) |
| `--kb PATH` | knowledge base root |
| `--status` | manifest summary for the mirror |
| `--verbose` | per-page lines instead of the progress counter |

## The pipeline

```
developer.apple.com/documentation/...
  |  appledocsscraper.crawl   robots.txt, rate limit, ETag/Last-Modified, SQLite manifest
  v
~/AppleDocs/documentation/<framework>/<path>.md        staging mirror, raw
  |  JSON comment header -> YAML front matter
  |  wrapped-key repairs, BOM removal
  |  type: <osaurus type>
  |  re-parse and verify before writing
  v
KNOWLEDGE/CODING/APPLE/<FRAMEWORK>/<path>.md           knowledge base, finished
```

The mirror is the staging area and is kept deliberately: it holds the manifest
that makes re-runs cheap, and the knowledge base only ever receives files that
passed verification. Writes are atomic, and a page whose content already matches
is left untouched.

## Scripts

| Script | Role |
| --- | --- |
| `apple_docs_to_kb.py` | **The one to run.** Whole pipeline, end to end. Imports the scraper unchanged. |
| `appledocsscraper.py` | The crawler on its own. Mirrors to a directory, no front-matter work. |
| `add_osaurus_types.py` | Adds a missing `type:` to Markdown already on disk (Apple trees and the PHP manual). Dry run by default. |
| `repair_frontmatter.py` | Standalone front-matter audit. Needs PyYAML, so it does not run here; its two repair rules live inside `apple_docs_to_kb.py`. |

`add_osaurus_types.py` also covers
`KNOWLEDGE/CODING/WEB_DEV/php/php-manual`, where it classifies from filenames
and headings rather than Apple metadata.

## How pages are filed

`/documentation/AudioToolbox/AUAudioUnit/allParameterValues`
becomes `AUDIO_TOOLBOX/auaudiounit/allparametervalues.md`:

- the folder is the page's own `framework` metadata, camel-case split and
  upper-snake-cased — `MIDIDriverKit` -> `MIDI_DRIVER_KIT`, `SwiftUI` ->
  `SWIFT_UI`. An existing folder that differs only in underscores is reused
  rather than duplicated.
- everything below `/documentation/<framework>/` keeps its shape, lowercased.
- a framework's own landing page keeps its name: `METAL/metal.md`.

Eleven frameworks, roughly 18,400 pages, are filed this way today.

## Front matter

Apple ships metadata as a JSON HTML comment:

```
<!--
{ "documentType" : "symbol", "framework" : "Metal", "symbol" : { "kind" : "Protocol", ... } }
-->
```

The knowledge base stores the same fields as YAML, `symbol` flattened to
`kind` / `module` / `preciseIdentifier`, `metadataVersion` dropped, keys in a
fixed order, `type:` last:

```yaml
---
title: MTLDevice
framework: Metal
documentType: symbol
role: Protocol
kind: Protocol
module: Metal
identifier: /documentation/Metal/MTLDevice
preciseIdentifier: c:objc(pl)MTLDevice
availability:
- 'iOS: 8.0.0 -'
type: protocol
---
```

The corpus was originally generated with PyYAML, so `emit_scalar` / `emit_block`
reproduce its block style and minimal quoting — plain scalars where that is
unambiguous, single quotes where YAML 1.1 would otherwise read a string as a
bool, number, date or null. It was checked against the 17,222 pages already
filed: 17,221 are byte-identical. Keep it that way if you touch it.

Existing YAML front matter is never reserialised. Only two repairs are applied,
both from `repair_frontmatter.py`: rejoining a top-level key split across a line
break (`document-\nType: symbol`, `title\n: X`) and removing a UTF-8 BOM. Every
page is re-parsed and round-trip compared before it is written; anything that
fails is reported and skipped, never half-written.

## Osaurus types

`type:` is classified from `role` / `kind` / `documentType` by
`add_osaurus_types.apple_type`. Apple pages resolve to `article`, `sample-code`,
`reference`, `framework`, `namespace`, `class`, `structure`, `protocol`,
`enumeration`, `case`, `method`, `function`, `property`, `constant`,
`type-alias`, `associated-type`, `macro` or `keyword`; unrecognised roles fall
back to `reference`. The PHP manual adds `interface`, `trait` and `attribute`.

## Known quirks

- **The User-Agent must not contain a URL or an email address.** Apple's edge
  returns 403 for the conventional polite-bot form
  `mybot/1.0 (+https://example.com/bot)`. A plain product token plus a
  parenthetical description is accepted. Describe yourself honestly; do not
  impersonate a browser.
- **Dotted path segments lose their suffix.** `local_path` applies
  `.with_suffix(".md")`, so `/documentation/Metal/MTLPackedFloat3-swift.typealias`
  is filed as `mtlpackedfloat3-swift.md` while its children live in
  `mtlpackedfloat3-swift.typealias/`. That is the established convention and
  both the mirror and the knowledge base follow it.
- **One page in the knowledge base is damaged.**
  `MIDI_DRIVER_KIT/iousermididevice.md` has `availability:'DriverKit: 24.0'` on
  one line, from the old line-wrapping bug. The malformed-line check flags it
  instead of passing it through; re-scraping that framework fixes it, because
  existing YAML is deliberately not rewritten.
- **`CORE_AI`, `CORE_AUDIO_TYPES` and `FOUNDATION_MODELS` still hold raw JSON
  headers** — they were mirrored before the conversion step existed.
  `./apple_docs_to_kb.py --convert-dir CORE_AI --apply` converts them in place.

## Politeness

robots.txt is fetched with the real User-Agent and honoured, including a
`Crawl-delay` slower than the configured one; disallowed seeds stop the run.
One connection, one request at a time, 1s delay plus jitter by default, with
exponential backoff on 429 and 503 and a per-page retry cap. Ctrl-C loses
nothing: the manifest is committed as it goes, so a restart resumes.

Be a good citizen. Raise `--delay` rather than lowering it.
