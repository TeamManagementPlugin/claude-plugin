#!/usr/bin/env python3
"""Print the plugin/CHANGELOG.md section body for a version (release notes).

Usage: python extract_changelog_notes.py <version>

Used by the CI release job (`.github/workflows/ci.yml`) to source GitHub
Release notes from the changelog: the section's BODY (without the
`## [X.Y.Z]` heading line — the release title already carries the version)
is printed to stdout. When the file or the section is absent, prints nothing
and exits 0 — the workflow then falls back to `--generate-notes`. Exit 2 only
on usage errors. Standalone stdlib on purpose: CI scripts must not depend on
the plugin tree.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # .github/scripts/ -> repo root
CHANGELOG_MD = ROOT / "plugin" / "CHANGELOG.md"

_HEADING_RE = re.compile(r"^##\s*\[([^\]]+)\]", re.MULTILINE)


def extract_section_body(text: str, version: str) -> str:
    """Return the body of the `## [<version>]` section (heading excluded)."""
    matches = list(_HEADING_RE.finditer(text))
    for i, match in enumerate(matches):
        if match.group(1).strip() != version:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[match.start():end]
        # Drop the heading line itself; keep the body as-is.
        _, _, body = section.partition("\n")
        return body.strip("\n").rstrip() + "\n" if body.strip() else ""
    return ""


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].strip():
        print("usage: extract_changelog_notes.py <version>", file=sys.stderr)
        return 2
    try:
        text = CHANGELOG_MD.read_text(encoding="utf-8")
    except OSError:
        return 0  # no changelog -> empty output, workflow falls back
    body = extract_section_body(text, argv[1].strip())
    if body:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
