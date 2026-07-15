#!/usr/bin/env python3
"""session-start guidance DELIVERY tests (h-durable-guidance-via-claude-md).

Behavioral guidance is no longer injected as one-shot SessionStart
``additionalContext`` (it faded out of long/compacted sessions). Instead, in
plugin mode the hook self-heals a DURABLE delivery: it deploys the plugin-owned
files into the project (refresh-on-change) and wires native ``@``-includes into the
project's ``CLAUDE.md`` (a project-root CLAUDE.md + its @-imports are re-read after
``/compact``). These tests assert:

  - additionalContext OMITS the old guidance/wiki/custom headers (no injection);
  - CLAUDE.tm.md + the knowledge files are deployed byte-equal to the plugin source;
  - CLAUDE.md gets an idempotent managed @-block (deploy-before-wire: no dangling @);
  - wiki opt-in toggles only the @CLAUDE.wiki.md line + CLAUDE.wiki.md deployment;
  - refresh-on-change overwrites a stale copy; user content + duplicate blocks handled;
  - the opted-in gate (team-management/ exists) and non-plugin mode are respected;
  - the deployed CLAUDE.tm.md has NO native @team-management/knowledge import.

Run with: python3 -m pytest test/test_session_start_guidance.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"
HOOK = PLUGIN_DIR / "hooks" / "session-start.py"

TM_SRC = PLUGIN_DIR / "templates" / "CLAUDE.tm.md"
WIKI_SRC = PLUGIN_DIR / "templates" / "CLAUDE.wiki.md"
KNOWLEDGE_SRC_DIR = PLUGIN_DIR / "knowledge"
KNOWLEDGE_FILES = ("tdd-discipline.md", "debugging.md", "receiving-feedback.md")

# Must match shared_state._CLAUDE_MD_BEGIN / _CLAUDE_MD_END exactly. Hardcoded so a
# silent drift in the marker strings trips these tests.
BEGIN = "<!-- team-management:begin (managed by /team-management:init; do not edit inside) -->"
END = "<!-- team-management:end -->"


def _project(tmp_path, *, legacy=False, wiki=False, opted_in=True):
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    if opted_in:
        tm = tmp_path / "team-management"
        tm.mkdir(parents=True, exist_ok=True)
        (tm / "config.json").write_text(
            json.dumps({"wiki": {"enabled": wiki}}), encoding="utf-8")
    if legacy:
        hooks = tmp_path / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "sessions-enforce.py").write_text("# legacy\n")
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [
                {"type": "command", "command": "python3 .claude/hooks/sessions-enforce.py"}]}]}}))
    return tmp_path


def _run(project, *, plugin_mode):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    if plugin_mode:
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_DIR)
    else:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    return subprocess.run([sys.executable, str(HOOK)], input=payload,
                          capture_output=True, text=True, cwd=str(project),
                          env=env, timeout=20)


def _additional_context(result):
    assert result.returncode == 0, f"session-start failed: {result.stderr!r}"
    return json.loads(result.stdout.strip())["hookSpecificOutput"]["additionalContext"]


def _claude_md(project):
    return (project / "CLAUDE.md").read_text(encoding="utf-8")


# --- No injection (the whole point) -----------------------------------------

def test_guidance_not_injected_into_context(tmp_path):
    ctx = _additional_context(_run(_project(tmp_path), plugin_mode=True))
    assert "## team-management Behaviors" not in ctx
    assert "## Project Custom Rules" not in ctx
    assert "## Wiki Behaviors" not in ctx


def test_wiki_guidance_not_injected_even_when_enabled(tmp_path):
    ctx = _additional_context(_run(_project(tmp_path, wiki=True), plugin_mode=True))
    assert "## Wiki Behaviors" not in ctx


# --- Deploy (refresh-on-change byte copy) -----------------------------------

def test_tm_md_deployed_byte_equal(tmp_path):
    project = _project(tmp_path)
    _run(project, plugin_mode=True)
    deployed = project / "CLAUDE.tm.md"
    assert deployed.exists()
    assert deployed.read_bytes() == TM_SRC.read_bytes()


def test_knowledge_files_deployed(tmp_path):
    project = _project(tmp_path)
    _run(project, plugin_mode=True)
    for name in KNOWLEDGE_FILES:
        dest = project / "team-management" / "knowledge" / name
        assert dest.exists(), f"{name} not deployed"
        assert dest.read_bytes() == (KNOWLEDGE_SRC_DIR / name).read_bytes()


def test_custom_rules_stub_created(tmp_path):
    project = _project(tmp_path)
    _run(project, plugin_mode=True)
    assert (project / "CLAUDE.tm.custom.md").exists()


def test_stale_tm_md_is_refreshed(tmp_path):
    project = _project(tmp_path)
    stale = project / "CLAUDE.tm.md"
    stale.write_text("STALE PLACEHOLDER — older plugin version\n", encoding="utf-8")
    _run(project, plugin_mode=True)
    assert stale.read_bytes() == TM_SRC.read_bytes()


# --- Wire (managed @-block in CLAUDE.md) ------------------------------------

def test_claude_md_managed_block_wired(tmp_path):
    project = _project(tmp_path)
    _run(project, plugin_mode=True)
    md = _claude_md(project)
    assert BEGIN in md and END in md
    assert "@CLAUDE.tm.md" in md
    assert "@CLAUDE.tm.custom.md" in md


def test_no_dangling_at_targets(tmp_path):
    """Every @-include line in the managed block must reference an existing file."""
    project = _project(tmp_path, wiki=True)
    _run(project, plugin_mode=True)
    md = _claude_md(project)
    start = md.index(BEGIN)
    end = md.index(END)
    block = md[start:end]
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("@"):
            target = project / line[1:]
            assert target.exists(), f"dangling @-target: {line}"


def test_managed_block_idempotent(tmp_path):
    project = _project(tmp_path)
    _run(project, plugin_mode=True)
    first = _claude_md(project)
    _run(project, plugin_mode=True)
    second = _claude_md(project)
    assert first == second
    assert second.count(BEGIN) == 1
    assert second.count(END) == 1


def test_managed_block_preserves_user_content(tmp_path):
    project = _project(tmp_path)
    (project / "CLAUDE.md").write_text(
        "# My Project\n\nUSER_SENTINEL_LINE preserve me\n", encoding="utf-8")
    _run(project, plugin_mode=True)
    md = _claude_md(project)
    assert "USER_SENTINEL_LINE preserve me" in md
    assert BEGIN in md and "@CLAUDE.tm.md" in md


def test_managed_block_collapses_duplicates(tmp_path):
    project = _project(tmp_path)
    dup_block = f"{BEGIN}\n@CLAUDE.tm.md\n@CLAUDE.tm.custom.md\n{END}"
    (project / "CLAUDE.md").write_text(
        f"# P\n\n{dup_block}\n\nmiddle text\n\n{dup_block}\n", encoding="utf-8")
    _run(project, plugin_mode=True)
    md = _claude_md(project)
    assert md.count(BEGIN) == 1
    assert md.count(END) == 1
    assert "middle text" in md


# --- Wiki opt-in toggles only the wiki line + file --------------------------

def test_wiki_disabled_no_wiki_wiring(tmp_path):
    project = _project(tmp_path, wiki=False)
    _run(project, plugin_mode=True)
    md = _claude_md(project)
    assert "@CLAUDE.wiki.md" not in md
    assert not (project / "CLAUDE.wiki.md").exists()


def test_wiki_enabled_wires_and_deploys(tmp_path):
    project = _project(tmp_path, wiki=True)
    _run(project, plugin_mode=True)
    md = _claude_md(project)
    assert "@CLAUDE.wiki.md" in md
    deployed = project / "CLAUDE.wiki.md"
    assert deployed.exists()
    assert deployed.read_bytes() == WIKI_SRC.read_bytes()


def test_non_dict_wiki_config_does_not_crash(tmp_path):
    """A malformed config.json (non-dict `wiki`) must leave wiki off, not crash the
    hook (code-review Note 1)."""
    project = _project(tmp_path)
    (project / "team-management" / "config.json").write_text(
        json.dumps({"wiki": "yes"}), encoding="utf-8")
    r = _run(project, plugin_mode=True)
    assert r.returncode == 0, r.stderr
    md = _claude_md(project)
    assert "@CLAUDE.tm.md" in md
    assert "@CLAUDE.wiki.md" not in md
    assert not (project / "CLAUDE.wiki.md").exists()


# --- Gates: opted-in + plugin-mode ------------------------------------------

def test_no_deploy_when_not_opted_in(tmp_path):
    project = _project(tmp_path, opted_in=False)
    _run(project, plugin_mode=True)
    assert not (project / "CLAUDE.tm.md").exists()
    assert not (project / "CLAUDE.md").exists()


def test_no_deploy_without_plugin_mode(tmp_path):
    project = _project(tmp_path)
    ctx = _additional_context(_run(project, plugin_mode=False))
    assert "## team-management Behaviors" not in ctx
    assert not (project / "CLAUDE.tm.md").exists()


# --- Boot-detector advisory (unchanged) + removed MCP-cache block -----------

def test_advisory_when_legacy_present(tmp_path):
    ctx = _additional_context(_run(_project(tmp_path, legacy=True), plugin_mode=True))
    assert "[Boot Detector" in ctx


def test_no_advisory_when_clean(tmp_path):
    ctx = _additional_context(_run(_project(tmp_path), plugin_mode=True))
    assert "[Boot Detector" not in ctx


def test_mcp_status_file_not_written(tmp_path):
    project = _project(tmp_path)
    _run(project, plugin_mode=True)
    assert not (project / ".claude" / "state" / "mcp-status.json").exists()


# --- Deployed CLAUDE.tm.md must not natively @-import the knowledge tree -----

def test_deployed_tm_md_has_no_native_knowledge_import(tmp_path):
    """On-demand knowledge: the @team-management/knowledge refs must be backticked
    (literal) so Claude Code's recursive @-import (max 4 hops) does NOT pull the
    whole knowledge tree into every session. A bare ' @team-management/knowledge'
    (space-then-@, i.e. not inside inline code) would be imported."""
    project = _project(tmp_path)
    _run(project, plugin_mode=True)
    text = (project / "CLAUDE.tm.md").read_text(encoding="utf-8")
    assert "@team-management/knowledge" in text or "team-management/knowledge" in text, \
        "knowledge references vanished entirely — expected literal (backticked) refs"
    assert " @team-management/knowledge" not in text, \
        "found an un-backticked @team-management/knowledge import in deployed CLAUDE.tm.md"
