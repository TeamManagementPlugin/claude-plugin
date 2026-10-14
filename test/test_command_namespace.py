#!/usr/bin/env python3
"""Namespace drift-guard for the plugin command files (m-namespace-rename).

Plugin slash commands namespace as `/team-management:<filename-without-.md>`, so
the command files must carry BARE names (no `tm-` prefix) and the whole `plugin/`
tree must be free of legacy `/tm-` slash refs and `tm-*.md` filename refs — both
would break for plugin users (the slash command, the docs pointing at it).

This is the automated form of the task gate
`grep -rnE '/tm-|tm-[A-Za-z0-9_-]+\\.md' plugin/`.

Run with: python3 -m pytest test/test_command_namespace.py -v
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"
COMMANDS = PLUGIN / "commands"

EXPECTED_COMMANDS = {
    "config.md", "init.md",
    "changelog.md",
    "clean-check.md",
    "wiki-ingest.md", "wiki-lint.md", "wiki-tune.md",
    "custom-protocol-create.md", "custom-protocol-update-after-reinstall.md",
}

# Matches a legacy slash ref (/tm-foo) OR a legacy command filename ref (tm-foo.md).
# The one exception is the update marker `tm-last-seen-version.json`, whose `tm-`
# prefix is a deliberate collision guard against other plugins sharing a data
# directory. Excluding it by NAME rather than by preceding character keeps every
# other spelling a hit — including path forms like `plugin/commands/tm-wiki-ingest`,
# which a preceding-character lookbehind would have silently dropped.
_LEGACY_REF = re.compile(r"/tm-(?!last-seen-version)|tm-[A-Za-z0-9_-]+\.md")

# Text-ish files worth scanning (the plugin ships markdown / python / config).
_TEXT_SUFFIXES = {
    ".md", ".py", ".json", ".txt", ".in", ".lock", ".cfg", ".toml",
    ".sh", ".js", ".yml", ".yaml", "",
}


def test_no_tm_prefixed_command_files():
    offenders = sorted(p.name for p in COMMANDS.glob("tm-*.md"))
    assert not offenders, f"command files still carry the tm- prefix: {offenders}"


def test_expected_bare_command_files_exist():
    actual = {p.name for p in COMMANDS.glob("*.md")}
    missing = EXPECTED_COMMANDS - actual
    extra = actual - EXPECTED_COMMANDS
    assert not missing, f"missing expected command files: {sorted(missing)}"
    assert not extra, f"unexpected command files (update EXPECTED_COMMANDS?): {sorted(extra)}"


def test_legacy_ref_pattern_matches_commands_not_paths():
    """The guard must fire on legacy slash commands and command filenames, and
    stay quiet on path segments that merely start with `tm-`."""
    for hit in (
        "/tm-wiki-ingest", "`/tm-clean-check`", "see tm-wiki-lint.md",
        # Path spellings of a legacy command file are a plausible drift form and
        # must stay caught — excluding the marker by name (not by preceding
        # character) is what keeps them in.
        "plugin/commands/tm-wiki-ingest", "./tm-wiki-lint",
    ):
        assert _LEGACY_REF.search(hit), hit
    for miss in (
        "CLAUDE_PLUGIN_DATA/tm-last-seen-version.json",
        ".claude/state/tm-last-seen-version.json",
        "the `tm-` prefix guards against collisions",
    ):
        assert not _LEGACY_REF.search(miss), miss


def test_plugin_tree_has_no_legacy_tm_refs():
    """Walk plugin/ and assert no text file contains a /tm- or tm-*.md ref —
    the automated gate. Fails loudly with file:line so the offender is obvious."""
    violations = []
    for path in sorted(PLUGIN.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _LEGACY_REF.search(line):
                rel = path.relative_to(REPO)
                violations.append(f"{rel}:{lineno}: {line.strip()[:100]}")
    assert not violations, "legacy /tm- or tm-*.md refs remain in plugin/:\n" + "\n".join(violations)
