#!/usr/bin/env python3
"""SessionStart statusline-pinning tests (m-fix-statusline-plugin-delivery).

Claude Code does NOT expand ${CLAUDE_PLUGIN_ROOT} in a settings.json statusLine
command (only inside a plugin's hooks.json), so /team-management:init cannot write
a portable plugin-relative statusLine. Instead the SessionStart hook — which DOES
receive CLAUDE_PLUGIN_ROOT — resolves the absolute statusline.py path and pins it
into the project's gitignored, per-machine .claude/settings.local.json
(_ensure_statusline_pinned).

These run session-start.py as a SUBPROCESS (matching test_session_start_guidance.py)
under controlled env vars and assert on the settings.local.json it writes:
  - creates it (abs path) when absent
  - idempotent: leaves a correct file (and its other keys) intact
  - self-heals a stale / broken ${CLAUDE_PLUGIN_ROOT} command to the current path
  - preserves unrelated keys
  - never clobbers a user's own (non-team-management) statusLine
  - never clobbers an unparseable settings.local.json
  - never raises (a write failure degrades to a no-op, hook still exits 0)
  - does nothing outside plugin mode

Run with: python3 -m pytest test/test_session_start_statusline.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"
HOOK = PLUGIN_DIR / "hooks" / "session-start.py"

# The command the hook should pin: the hook's own interpreter (sys.executable,
# guaranteed present + >=3.10), absolute + double-quoted, resolved. The hook is run
# below via [sys.executable, HOOK], so the interpreter it sees == this sys.executable.
EXPECTED_STATUSLINE = (PLUGIN_DIR / "templates" / "statusline.py").resolve()
EXPECTED_COMMAND = f'"{sys.executable}" "{EXPECTED_STATUSLINE}"'


def _project(tmp_path):
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    return tmp_path


def _settings_local(project):
    return project / ".claude" / "settings.local.json"


def _run(project, *, plugin_mode=True):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    if plugin_mode:
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_DIR)
    else:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    return subprocess.run([sys.executable, str(HOOK)], input=payload,
                          capture_output=True, text=True, cwd=str(project),
                          env=env, timeout=20)


def _read_local(project):
    return json.loads(_settings_local(project).read_text(encoding="utf-8"))


def test_creates_statusline_when_absent(tmp_path):
    project = _project(tmp_path)
    r = _run(project)
    assert r.returncode == 0, r.stderr
    data = _read_local(project)
    assert data["statusLine"]["type"] == "command"
    assert data["statusLine"]["command"] == EXPECTED_COMMAND
    assert data["statusLine"]["padding"] == 0


def test_idempotent_preserves_correct_file(tmp_path):
    project = _project(tmp_path)
    _settings_local(project).write_text(json.dumps({
        "statusLine": {"type": "command", "command": EXPECTED_COMMAND, "padding": 0},
        "_sentinel": "keep-me",
    }), encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    data = _read_local(project)
    assert data["statusLine"]["command"] == EXPECTED_COMMAND
    assert data["_sentinel"] == "keep-me"


def test_self_heals_broken_plugin_root_command(tmp_path):
    """The exact broken value init used to write must be replaced with the
    resolved absolute path (this is the real-world self-heal)."""
    project = _project(tmp_path)
    _settings_local(project).write_text(json.dumps({
        "statusLine": {"type": "command",
                       "command": "python3 ${CLAUDE_PLUGIN_ROOT}/templates/statusline.py",
                       "padding": 0},
    }), encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert _read_local(project)["statusLine"]["command"] == EXPECTED_COMMAND


def test_self_heals_stale_absolute_path(tmp_path):
    project = _project(tmp_path)
    _settings_local(project).write_text(json.dumps({
        "statusLine": {"type": "command",
                       "command": 'python3 "/old/0.0.1/templates/statusline.py"',
                       "padding": 0},
    }), encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert _read_local(project)["statusLine"]["command"] == EXPECTED_COMMAND


def test_self_heals_windows_backslash_command(tmp_path):
    """On Windows `Path.resolve()` stringifies with BACKSLASHES, so a previously
    pinned command embeds `...\\templates\\statusline.py`. `_is_ours` must recognize it
    (separator-normalized) — otherwise after a plugin-version bump the stale Windows
    command is misread as a user's own and never re-pinned, silently breaking the
    statusline (codex P2 / code-review Warning #1). The hook runs on POSIX here, but
    the stale command string carries Windows backslashes, exercising the match path."""
    project = _project(tmp_path)
    stale = 'python3 "C:\\Users\\x\\plugin\\templates\\statusline.py"'
    _settings_local(project).write_text(json.dumps({
        "statusLine": {"type": "command", "command": stale, "padding": 0},
    }), encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert _read_local(project)["statusLine"]["command"] == EXPECTED_COMMAND


def test_preserves_other_keys(tmp_path):
    project = _project(tmp_path)
    _settings_local(project).write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
    }), encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    data = _read_local(project)
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert data["statusLine"]["command"] == EXPECTED_COMMAND


def test_does_not_clobber_user_custom_statusline(tmp_path):
    """A statusLine that does not reference statusline.py / CLAUDE_PLUGIN_ROOT is
    the user's own — leave it untouched."""
    project = _project(tmp_path)
    custom = {"type": "command", "command": "/usr/local/bin/my-prompt.sh", "padding": 1}
    _settings_local(project).write_text(json.dumps({"statusLine": custom}),
                                        encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert _read_local(project)["statusLine"] == custom


def test_no_clobber_unparseable_settings_local(tmp_path):
    project = _project(tmp_path)
    _settings_local(project).write_text("{ this is : not json", encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    # Left exactly as-is — we must not destroy a file we cannot parse.
    assert _settings_local(project).read_text(encoding="utf-8") == "{ this is : not json"


def test_never_raises_on_write_failure(tmp_path):
    """If settings.local.json cannot be written (here: it is a directory), the
    hook must still exit 0 — statusline pinning is best-effort, never fatal."""
    project = _project(tmp_path)
    _settings_local(project).mkdir()  # a directory cannot be overwritten as a file
    r = _run(project)
    assert r.returncode == 0, r.stderr


def test_not_written_outside_plugin_mode(tmp_path):
    project = _project(tmp_path)
    r = _run(project, plugin_mode=False)
    assert r.returncode == 0, r.stderr
    assert not _settings_local(project).exists()


def _settings_committed(project):
    return project / ".claude" / "settings.json"


def test_does_not_clobber_user_named_statusline_script(tmp_path):
    """A user's OWN script merely named statusline.py (e.g. python3
    .claude/statusline.py) must NOT be treated as ours — "ours" is anchored on the
    templates/statusline.py plugin-path tail, not the bare basename (codex P3)."""
    project = _project(tmp_path)
    custom = {"type": "command", "command": "python3 .claude/statusline.py", "padding": 0}
    _settings_local(project).write_text(json.dumps({"statusLine": custom}), encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert _read_local(project)["statusLine"] == custom


def test_does_not_override_custom_committed_statusline(tmp_path):
    """A custom statusLine in the COMMITTED settings.json (no settings.local.json
    yet) must be respected — we must not silently override it via a local entry
    (codex P2)."""
    project = _project(tmp_path)
    _settings_committed(project).write_text(json.dumps({
        "statusLine": {"type": "command", "command": "/usr/local/bin/team-prompt.sh", "padding": 0},
    }), encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    # No local override created — the committed custom statusLine stands.
    assert not _settings_local(project).exists()


def test_self_heals_broken_committed_statusline_via_local(tmp_path):
    """A broken ${CLAUDE_PLUGIN_ROOT} statusLine in committed settings.json (and no
    settings.local.json yet) self-heals: the hook pins the resolved abs path into
    the local override."""
    project = _project(tmp_path)
    _settings_committed(project).write_text(json.dumps({
        "statusLine": {"type": "command",
                       "command": "python3 ${CLAUDE_PLUGIN_ROOT}/templates/statusline.py",
                       "padding": 0},
    }), encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert _read_local(project)["statusLine"]["command"] == EXPECTED_COMMAND


def test_does_not_clobber_non_dict_statusline(tmp_path):
    """A non-dict statusLine value (e.g. a bare string) is left untouched — we only
    self-heal a statusLine we recognise as ours (code-review Note #1)."""
    project = _project(tmp_path)
    _settings_local(project).write_text(json.dumps({"statusLine": "weird-string"}),
                                        encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert _read_local(project)["statusLine"] == "weird-string"


# NOTE: the `.gitignore`-ensuring tests moved to test_session_start_claude_gitignore.py
# when the hook was broadened from ignoring `.claude/settings.local.json` to the whole
# `.claude/` dir (h-fix-mcp-token-and-claude-gitignore).
