"""Drift guard: no runtime pointer to the retired legacy `protocols/` files.

h-retire-legacy-protocol-pointers deleted `plugin/protocols/{task-creation,
task-startup,context-compaction,task-completion}.md` (undeployed, partly
harmful) and rewrote every hook / agent / sub-protocol reference to the live
protocol engine. This guard fails if a future change reintroduces a runtime
pointer to `team-management/protocols/` or `plugin/protocols/`, or re-creates
the deleted directory.

Scope note: only the RUNTIME surface is checked (hooks, agent prompts,
protocol-configs) — NOT the `*/CLAUDE.md` docs or `wiki/`, which may legitimately
mention the retired files as historical context. The substrings checked are the
exact legacy path forms, chosen so they do NOT false-match the live
`team-management/protocol-configs/` or `@sub-protocols/` paths.
"""
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_PLUGIN = _REPO / "plugin"

# The exact legacy path forms. Neither is a substring of the live
# `protocol-configs/` or `sub-protocols/` paths.
_LEGACY_FORMS = ("team-management/protocols/", "plugin/protocols/")

_HOOK_FILES = [
    _PLUGIN / "hooks" / "user-messages.py",
    _PLUGIN / "hooks" / "post-tool-use.py",
    _PLUGIN / "hooks" / "sessions-enforce.py",
]


def _runtime_markdown_and_py():
    """Runtime prompt / config surface: agent prompts + protocol-configs.

    CLAUDE.md files are excluded — they are documentation, not runtime pointers,
    and may reference the retired files historically.
    """
    files = []
    for base in (_PLUGIN / "agents", _PLUGIN / "protocol-configs"):
        for path in base.rglob("*"):
            if path.suffix in (".md", ".json") and path.name != "CLAUDE.md":
                files.append(path)
    files.extend(_HOOK_FILES)
    return files


def test_legacy_protocols_dir_is_gone():
    assert not (_PLUGIN / "protocols").exists(), (
        "plugin/protocols/ was retired in h-retire-legacy-protocol-pointers; "
        "it must not be recreated."
    )


@pytest.mark.parametrize("hook", _HOOK_FILES, ids=lambda p: p.name)
def test_hook_has_no_legacy_pointer(hook):
    # Non-vacuous: the file must exist and be non-trivial.
    assert hook.is_file(), f"{hook} missing — test would pass vacuously"
    text = hook.read_text(encoding="utf-8")
    assert len(text) > 200
    for form in _LEGACY_FORMS:
        assert form not in text, f"{hook.name} still references legacy '{form}'"


def test_runtime_surface_has_no_legacy_pointer():
    scanned = _runtime_markdown_and_py()
    # Sanity: we actually scanned a meaningful number of files.
    assert len(scanned) > 10, "runtime-surface walk found too few files — glob broke?"
    offenders = []
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        for form in _LEGACY_FORMS:
            if form in text:
                offenders.append(f"{path.relative_to(_REPO)} -> {form}")
    assert not offenders, "legacy protocol pointers remain on the runtime surface:\n" + "\n".join(offenders)
