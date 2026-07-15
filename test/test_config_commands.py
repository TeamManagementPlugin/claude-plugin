#!/usr/bin/env python3
"""Structural tests for the config / init slash-command files (m-config-mcp-flow,
commit 4). These markdown files are LLM instructions — no runtime exec — so the
guard asserts they carry the required contract surface (tool names, section order,
token-redirect, idempotency) and won't silently rot.

Run with: python3 -m pytest test/test_config_commands.py -v
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS = REPO_ROOT / "plugin" / "commands"


def _read(name):
    return (COMMANDS / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# config.md
# --------------------------------------------------------------------------

def test_config_md_exists_with_frontmatter():
    text = _read("config.md")
    assert text.startswith("---")
    assert "description:" in text.split("---", 2)[1]


def test_config_md_references_both_tools():
    text = _read("config.md")
    assert "config_get" in text
    assert "config_update" in text


def test_config_md_covers_all_sections_in_order():
    text = _read("config.md").lower()
    # Match the bolded section markers (unique) rather than bare words like "ai".
    order = ["**identity**", "**daic", "**auto-compact**", "**issue-tracking**",
             "**ai providers**", "**wiki**"]
    positions = [text.find(s) for s in order]
    assert all(p != -1 for p in positions), f"missing section(s): {list(zip(order, positions))}"
    assert positions == sorted(positions), "sections are not in the required order"


def test_config_md_has_token_redirect():
    text = _read("config.md")
    # Tokens are redirected to the per-project file the AI cannot read (the OS-keychain
    # userConfig model was retired — m-per-project-provider-tokens).
    assert "provider-tokens.json" in text
    assert "cannot read" in text.lower()
    # Must instruct NOT to set tokens here.
    assert "never" in text.lower() or "not" in text.lower()


def test_config_md_uses_askuserquestion_and_batching():
    text = _read("config.md")
    assert "AskUserQuestion" in text
    assert "batch" in text.lower()  # per-section batching contract


def test_config_md_has_teammate_advisory():
    text = _read("config.md").lower()
    assert "teammate" in text or "each developer" in text


# --------------------------------------------------------------------------
# init.md
# --------------------------------------------------------------------------

def test_init_md_exists_with_frontmatter():
    text = _read("init.md")
    assert text.startswith("---")
    assert "description:" in text.split("---", 2)[1]


def test_init_md_writes_the_three_keys():
    text = _read("init.md")
    assert "enabledPlugins" in text
    assert "extraKnownMarketplaces" in text
    assert "statusLine" in text


def test_init_md_targets_project_settings():
    text = _read("init.md")
    assert ".claude/settings.json" in text


def test_init_md_does_not_write_statusline_into_settings_json():
    """init.md must NOT write a statusLine into the committed settings.json
    (m-fix-statusline-plugin-delivery). Claude Code does not expand
    ${CLAUDE_PLUGIN_ROOT} inside a settings.json statusLine command (only inside a
    plugin's hooks.json), so the old `python3 ${CLAUDE_PLUGIN_ROOT}/templates/
    statusline.py` directive never resolved. The SessionStart hook now pins the
    resolved absolute path into the gitignored settings.local.json instead. Guard
    against the broken directive regressing, and require the corrected path to be
    documented."""
    text = _read("init.md")
    # The old broken command directive must be gone.
    assert "python3 ${CLAUDE_PLUGIN_ROOT}/templates/statusline.py" not in text
    # The corrected delivery path must be documented.
    assert "settings.local.json" in text
    assert "_ensure_statusline_pinned" in text


def test_init_md_is_idempotent_and_merges():
    text = _read("init.md").lower()
    assert "idempotent" in text
    assert "merge" in text  # must not clobber unrelated keys


def test_init_md_does_not_write_secrets():
    text = _read("init.md").lower()
    assert "token" in text  # mentions tokens only to say they go elsewhere
    assert "provider-tokens.json" in _read("init.md")
