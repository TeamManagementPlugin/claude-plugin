#!/usr/bin/env python3
"""Provider project-root + token resolution under the plugin path model
(m-plugin-verification, commit 1).

Two plugin-runtime correctness guards for `plugin/hooks/issue_provider_base.py`:

1. **`_find_project_root` must be env-first.** The base provider `__init__` calls
   `_find_project_root()` → `_load_config()`. The original implementation walked up
   from `Path(__file__)` then cwd looking for marker dirs and NEVER consulted
   `CLAUDE_PROJECT_DIR`. In a dev checkout `__file__` lives at
   `plugin/hooks/issue_provider_base.py`, so the walk finds the repo root (has
   `.git` + `team-management`) and everything works — which is exactly why the bug
   was invisible. In a REAL plugin install `__file__` is under the marketplace cache
   OUTSIDE the project, so the walk resolves to the wrong root and every provider
   (GitLab / GitHub / Jira) loads an empty config. The fix mirrors
   `shared_state.get_project_root()` / `mcp/core/project.get_project_root()`: read
   `CLAUDE_PROJECT_DIR` first (when it exists), then fall back to the legacy walk.

   These tests set `CLAUDE_PROJECT_DIR` to a tmp project DIFFERENT from the repo
   root; the legacy walk would still return the repo root, so a divergent env var
   deterministically exposes the bug without relocating `__file__`.

2. **`resolve_provider_token` continuity.** Tokens live in the per-project
   `.claude/state/provider-tokens.json` file (the OS-keychain userConfig model was
   retired — it was global-per-plugin). The resolver must prefer that file when a
   value is set and fall back to the config value otherwise, so existing config.json
   users need no reconfiguration.

`_find_project_root` does not use `self`, so it is exercised as an unbound call with
a dummy `self`; `_load_config` reads only `self.project_root`, so it is exercised
with a `SimpleNamespace`.

Run with: python3 -m pytest test/test_provider_root_resolution.py -v
"""

import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "plugin" / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from issue_provider_base import IssueTrackingProvider  # noqa: E402
from shared_state import resolve_provider_token  # noqa: E402

# A config sentinel that the real repo config.json does not contain, so a test that
# reads the WRONG (repo) root fails the assertion instead of coincidentally passing.
_SENTINEL = {"_verify_sentinel": "m-plugin-verification"}


def _make_project(tmp_path, config=None):
    proj = tmp_path / "proj"
    (proj / "team-management").mkdir(parents=True)
    if config is not None:
        (proj / "team-management" / "config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
    return proj


# --------------------------------------------------------------------------
# _find_project_root — env-first
# --------------------------------------------------------------------------

def test_find_project_root_honors_claude_project_dir(tmp_path, monkeypatch):
    """env-first: a set+existing CLAUDE_PROJECT_DIR wins over the __file__ walk."""
    proj = _make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    got = IssueTrackingProvider._find_project_root(None)
    assert got == proj, f"expected env project root {proj}, got {got}"


def test_find_project_root_ignores_nonexistent_env(tmp_path, monkeypatch):
    """A CLAUDE_PROJECT_DIR pointing at a missing path is ignored (not trusted
    blindly), mirroring shared_state.get_project_root — falls back to the walk,
    which in dev finds the repo root."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "does-not-exist"))
    got = IssueTrackingProvider._find_project_root(None)
    assert got == REPO, f"expected fallback to repo root {REPO}, got {got}"


def test_find_project_root_falls_back_without_env(tmp_path, monkeypatch):
    """No CLAUDE_PROJECT_DIR → legacy __file__/cwd marker walk (dev → repo root)."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    got = IssueTrackingProvider._find_project_root(None)
    assert got == REPO, f"expected repo root {REPO}, got {got}"


def test_load_config_reads_env_project(tmp_path, monkeypatch):
    """End-to-end of the bug: with the fix, a provider reads the env project's
    config.json, not the repo's. The sentinel key proves the right file was read."""
    proj = _make_project(tmp_path, config=_SENTINEL)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    root = IssueTrackingProvider._find_project_root(None)
    ns = types.SimpleNamespace(project_root=root)
    cfg = IssueTrackingProvider._load_config(ns)
    assert cfg.get("_verify_sentinel") == "m-plugin-verification", cfg


# --------------------------------------------------------------------------
# resolve_provider_token — file-first, config fallback
# --------------------------------------------------------------------------

def test_token_config_fallback_when_no_env(monkeypatch, tmp_path):
    """Legacy continuity: no env var → the config.json token is used as-is."""
    # Isolate the project root so the state-file tier cannot read a developer's real
    # .claude/state/provider-tokens.json (h-fix-mcp-token-ondisk-bridge).
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_GITLAB_API_TOKEN", raising=False)
    assert resolve_provider_token("gitlab", "cfg-token") == "cfg-token"


def test_token_file_wins_over_config(monkeypatch, tmp_path):
    """Plugin path: a token in the per-project provider-tokens.json wins over the
    config fallback (the env/keychain tier was retired)."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (state / "provider-tokens.json").write_text(
        json.dumps({"gitlab": "file-token"}), encoding="utf-8")
    assert resolve_provider_token("gitlab", "cfg-token") == "file-token"


def test_token_unknown_provider_returns_config(monkeypatch):
    """An unknown provider resolves straight to the config value."""
    assert resolve_provider_token("nope", "cfg-token") == "cfg-token"
