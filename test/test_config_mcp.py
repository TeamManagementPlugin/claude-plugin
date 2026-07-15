#!/usr/bin/env python3
"""Config MCP tool tests (m-config-mcp-flow, commit 2).

Covers config_get (ungated, token-masked) and config_update (gated, schema +
SEC-007 + tracked-check + gitignore-ensure + atomic merge preserving tokens).

Run with: python3 -m pytest test/test_config_mcp.py -v
"""

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_MCP_DIR = _REPO / "plugin" / "mcp"
_HOOKS_DIR = _REPO / "plugin" / "hooks"
for _p in (str(_MCP_DIR), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools import config as config_tool  # noqa: E402
from core import project as core_project  # noqa: E402
from core import config as config_module  # noqa: E402
import shared_state  # noqa: E402


class MockMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def dec(func):
            self.tools[func.__name__] = func
            return func
        return dec


@pytest.fixture
def proj(tmp_path, monkeypatch):
    """A tmp project (NOT a git repo) with CLAUDE_PROJECT_DIR set and config tools
    registered. The non-git tree makes tracked-check pass (untracked) and
    gitignore-ensure a no-op, isolating the validation/merge logic."""
    (tmp_path / "team-management").mkdir()
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(core_project, "_project_root", None, raising=False)
    mcp = MockMCP()
    config_tool.register_tools(mcp)
    return tmp_path, mcp


def _config_path(root):
    return root / "team-management" / "config.json"


def _write_config(root, data):
    _config_path(root).write_text(json.dumps(data), encoding="utf-8")


def _read_config(root):
    return json.loads(_config_path(root).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Intent-gate
# --------------------------------------------------------------------------

def test_update_refused_without_flag(proj):
    root, mcp = proj
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is False
    assert "gated" in out["error"]
    assert not _config_path(root).exists()


def test_update_accepted_with_live_flag(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert _read_config(root)["developer_name"] == "Max"


def test_update_refused_when_flag_expired(proj):
    root, mcp = proj
    shared_state.write_config_session_flag(ttl_seconds=-5)  # already expired
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is False
    assert "expired" in out["error"]


# --------------------------------------------------------------------------
# Core-config cache invalidation (m-fix-mcp-config-cache-poisoning)
# --------------------------------------------------------------------------

def test_config_update_invalidates_core_config_cache(proj, monkeypatch):
    """A successful config_update must invalidate the long-lived MCP server's
    core.config cache, so the same session observes the write without a Claude
    Code restart. Asserting reload_config() was CALLED (not merely that a later
    load returns the value) is deliberate: the mtime-based load_config() would
    self-heal after the write anyway, so a bare read would pass even if
    config_update never invalidated. The spy proves config_update owns the fix."""
    root, mcp = proj
    # Clean slate, then PRE-POISON the cache the way the fresh-install flow does:
    # a load before config.json exists caches {} for the process lifetime.
    monkeypatch.setattr(config_module, "_config", None, raising=False)
    monkeypatch.setattr(config_module, "_config_mtime", None, raising=False)
    monkeypatch.setattr(config_module, "_provider", None, raising=False)
    assert config_module.load_config() == {}
    assert config_module._config == {}  # poisoned

    # Spy on reload_config (call-through) to prove config_update invalidates.
    calls = []
    real_reload = config_module.reload_config

    def _spy():
        calls.append(1)
        real_reload()

    monkeypatch.setattr(config_module, "reload_config", _spy)

    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert calls, "config_update must call core.config.reload_config() after a successful write"
    # End-to-end: the same-process cache now returns the written value (no restart).
    assert config_module.load_config()["developer_name"] == "Max"


# --------------------------------------------------------------------------
# Guidance deploy + wire (h-durable-guidance-via-claude-md)
# --------------------------------------------------------------------------

def test_config_update_deploys_and_wires_guidance(proj):
    """A successful config_update deploys the plugin-owned guidance into the project
    AND wires the managed @-block into CLAUDE.md (same-session effect, no restart).
    get_plugin_root() falls back to the dev <repo>/plugin here, so the real source
    files are copied."""
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"wiki.enabled": True})
    assert out["success"] is True, out
    assert (root / "CLAUDE.tm.md").exists()
    assert (root / "team-management" / "knowledge" / "debugging.md").exists()
    assert (root / "CLAUDE.tm.custom.md").exists()
    md = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "team-management:begin" in md
    assert "@CLAUDE.tm.md" in md
    assert "@CLAUDE.wiki.md" in md
    assert (root / "CLAUDE.wiki.md").exists()


def test_config_update_no_wiki_omits_wiki_wiring(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"wiki.enabled": False})
    assert out["success"] is True, out
    md = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@CLAUDE.tm.md" in md
    assert "@CLAUDE.wiki.md" not in md
    assert not (root / "CLAUDE.wiki.md").exists()


def test_config_update_tolerates_non_dict_wiki(proj):
    """A pre-existing non-dict `wiki` value (not touched by this update) must not
    crash config_update after the write succeeded (code-review Warning 1)."""
    root, mcp = proj
    _write_config(root, {"wiki": "yes"})  # malformed pre-existing config
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "Max"})  # does NOT touch wiki.*
    assert out["success"] is True, out
    md = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@CLAUDE.tm.md" in md
    assert "@CLAUDE.wiki.md" not in md


# --------------------------------------------------------------------------
# Sensitive-key reject
# --------------------------------------------------------------------------

def test_sensitive_key_rejected(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"gitlab.api_token": "glpat-xxx"})
    assert out["success"] is False
    assert any("provider-tokens.json" in e for e in out["errors"])
    assert not _config_path(root).exists()


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------

def test_unknown_key_rejected(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"totally.unknown": 1})
    assert out["success"] is False
    assert any("unknown config key" in e for e in out["errors"])


def test_bad_type_rejected(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"auto_compact.threshold": "high"})
    assert out["success"] is False
    assert any("expected int" in e for e in out["errors"])


def test_bool_not_accepted_as_int(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"auto_compact.threshold": True})
    assert out["success"] is False
    assert any("expected int, got bool" in e for e in out["errors"])


def test_bad_enum_rejected(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"issue_tracking.provider": "bitbucket"})
    assert out["success"] is False
    assert any("must be one of" in e for e in out["errors"])


def test_valid_enum_accepted(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"issue_tracking.provider": "gitlab"})
    assert out["success"] is True, out
    assert _read_config(root)["issue_tracking"]["provider"] == "gitlab"


# --------------------------------------------------------------------------
# SEC-007 URL validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://gitlab.com",          # not https
    "https://localhost",          # localhost
    "https://gitlab.internal.local",  # .local
    "https://10.0.0.1",           # RFC-1918
    "https://127.0.0.1",          # loopback
    "https://192.168.1.5",        # RFC-1918
    "https://169.254.1.1",        # link-local
    "ftp://gitlab.com",           # wrong scheme
    "not-a-url",
    "https://2130706433",         # decimal-encoded 127.0.0.1 (SSRF bypass)
    "https://0x7f000001",         # hex-encoded 127.0.0.1
    "https://0o17700000001",      # octal-encoded
])
def test_validate_safe_url_rejects(url):
    ok, reason = config_tool._validate_safe_url(url)
    assert ok is False, f"{url} should be rejected"
    assert reason


@pytest.mark.parametrize("url", [
    "https://gitlab.com",
    "https://gitlab.example.com/api/v4",
    "https://git.company.io",
])
def test_validate_safe_url_accepts(url):
    ok, reason = config_tool._validate_safe_url(url)
    assert ok is True, f"{url} should be accepted ({reason})"


def test_update_rejects_unsafe_url(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"gitlab.base_url": "http://10.0.0.1"})
    assert out["success"] is False
    assert any("gitlab.base_url" in e for e in out["errors"])


def test_update_accepts_safe_url(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"gitlab.base_url": "https://gitlab.com"})
    assert out["success"] is True, out
    assert _read_config(root)["gitlab"]["base_url"] == "https://gitlab.com"


# --------------------------------------------------------------------------
# Merge preserves pre-existing tokens; atomic write valid JSON
# --------------------------------------------------------------------------

def test_merge_preserves_existing_token(proj):
    root, mcp = proj
    _write_config(root, {"gitlab": {"api_token": "PRE-EXISTING", "enabled": True},
                         "developer_name": "Old"})
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "New"})
    assert out["success"] is True, out
    cfg = _read_config(root)
    assert cfg["developer_name"] == "New"
    assert cfg["gitlab"]["api_token"] == "PRE-EXISTING"  # untouched
    assert cfg["gitlab"]["enabled"] is True


def test_write_is_valid_json_and_merges_nested(proj):
    root, mcp = proj
    _write_config(root, {"gitlab": {"enabled": True}})
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"gitlab.base_url": "https://gitlab.com",
                                      "wiki.enabled": True})
    assert out["success"] is True, out
    cfg = _read_config(root)  # parses → valid JSON
    assert cfg["gitlab"]["enabled"] is True
    assert cfg["gitlab"]["base_url"] == "https://gitlab.com"
    assert cfg["wiki"]["enabled"] is True


# --------------------------------------------------------------------------
# config_get masking (ungated)
# --------------------------------------------------------------------------

def test_config_get_masks_tokens(proj):
    root, mcp = proj
    _write_config(root, {"gitlab": {"api_token": "glpat-secret", "base_url": "https://gitlab.com"},
                         "jira": {"api_token": ""}, "developer_name": "Max"})
    out = mcp.tools["config_get"]()  # no flag needed — ungated
    assert out["success"] is True
    cfg = out["config"]
    assert cfg["gitlab"]["api_token"] == "***set***"
    assert cfg["jira"]["api_token"] == "***unset***"
    assert cfg["gitlab"]["base_url"] == "https://gitlab.com"  # non-sensitive untouched
    assert cfg["developer_name"] == "Max"


def test_config_get_missing_file(proj):
    root, mcp = proj
    out = mcp.tools["config_get"]()
    assert out["success"] is True
    assert out["exists"] is False
    assert out["config"] == {}


def test_update_refuses_non_dict_intermediate(proj):
    """A corrupted config with a non-object at an intermediate path is surfaced
    as an error, not silently clobbered (data-loss guard)."""
    root, mcp = proj
    _write_config(root, {"gitlab": "oops-a-string"})
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"gitlab.base_url": "https://gitlab.com"})
    assert out["success"] is False
    assert "not an object" in out["error"]
    # The corrupt value is left intact — not overwritten.
    assert _read_config(root)["gitlab"] == "oops-a-string"


# --------------------------------------------------------------------------
# git tracked-check + gitignore-ensure (real tiny git repo)
# --------------------------------------------------------------------------

def test_tracked_config_is_refused(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(core_project, "_project_root", None, raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / "team-management").mkdir()
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    cfg = tmp_path / "team-management" / "config.json"
    cfg.write_text('{"developer_name": "x"}', encoding="utf-8")
    subprocess.run(["git", "add", "team-management/config.json"], cwd=tmp_path, check=True)
    mcp = MockMCP()
    config_tool.register_tools(mcp)
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is False
    assert "git-tracked" in out["error"]


def test_gitignore_ensured_on_write(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(core_project, "_project_root", None, raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / "team-management").mkdir()
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    mcp = MockMCP()
    config_tool.register_tools(mcp)
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert out["gitignore"]["status"] == "added"
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "team-management/config.json" in gitignore


# --------------------------------------------------------------------------
# gitignore directory-pattern recognition + status feedback
# (m-harden-config-gitignore-guard)
# --------------------------------------------------------------------------

def _git_project(tmp_path, monkeypatch):
    """A tmp project that IS a git repo, with config tools registered and the
    config-session gate opened. Mirrors the inline setup of the tests above."""
    import subprocess
    monkeypatch.setattr(core_project, "_project_root", None, raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / "team-management").mkdir()
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    # Pre-seed the provider-token file so config_update's step-8d seeder no-ops here
    # (it early-returns when the file exists) and does NOT append `.claude/` to the
    # .gitignore these team-management-gitignore tests assert on. The seed+gitignore
    # behavior is covered by test_config_update_seeds_provider_tokens_file.
    (tmp_path / ".claude" / "state" / "provider-tokens.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    mcp = MockMCP()
    config_tool.register_tools(mcp)
    shared_state.write_config_session_flag()
    return mcp


@pytest.mark.parametrize("line", [
    "team-management/config.json",
    "/team-management/config.json",
    "team-management/",
    "/team-management/",
    "team-management",
    "/team-management",
])
def test_gitignore_covers_recognizes(line):
    assert config_tool._gitignore_covers({line}, "team-management/config.json") == line


def test_gitignore_covers_rejects_unrelated():
    assert config_tool._gitignore_covers(
        {"node_modules/", "*.log", "team-management-notes.txt"},
        "team-management/config.json",
    ) is None


def test_gitignore_covered_by_directory_pattern(tmp_path, monkeypatch):
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\nteam-management/\n", encoding="utf-8")
    before = gi.read_text(encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert out["gitignore"]["status"] == "already_covered"
    assert out["gitignore"]["covered_by"] == "team-management/"
    assert gi.read_text(encoding="utf-8") == before  # no redundant line appended


def test_gitignore_covered_by_rooted_directory(tmp_path, monkeypatch):
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("/team-management/\n", encoding="utf-8")
    before = gi.read_text(encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert out["gitignore"]["status"] == "already_covered"
    assert out["gitignore"]["covered_by"] == "/team-management/"
    assert gi.read_text(encoding="utf-8") == before


def test_gitignore_covered_by_exact_entry(tmp_path, monkeypatch):
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("team-management/config.json\n", encoding="utf-8")
    before = gi.read_text(encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert out["gitignore"]["status"] == "already_covered"
    assert out["gitignore"]["covered_by"] == "team-management/config.json"
    assert gi.read_text(encoding="utf-8") == before


def test_gitignore_added_status_reported(tmp_path, monkeypatch):
    mcp = _git_project(tmp_path, monkeypatch)
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert out["gitignore"]["status"] == "added"
    assert out["gitignore"]["covered_by"] is None
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "team-management/config.json" in gi


def test_gitignore_unavailable_when_not_git_repo(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert out["gitignore"]["status"] == "unavailable"


def test_gitignore_append_preserves_trailing_newline(tmp_path, monkeypatch):
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n", encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["gitignore"]["status"] == "added"
    assert gi.read_text(encoding="utf-8") == "node_modules/\nteam-management/config.json\n"


def test_gitignore_append_inserts_missing_newline(tmp_path, monkeypatch):
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/", encoding="utf-8")  # no trailing newline
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["gitignore"]["status"] == "added"
    assert gi.read_text(encoding="utf-8") == "node_modules/\nteam-management/config.json\n"


def test_gitignore_comment_line_not_treated_as_covered(tmp_path, monkeypatch):
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("# team-management/\n", encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["gitignore"]["status"] == "added"  # a commented pattern does not cover
    assert gi.read_text(encoding="utf-8") == "# team-management/\nteam-management/config.json\n"


def test_gitignore_trailing_whitespace_counts(tmp_path, monkeypatch):
    # git strips TRAILING spaces from a pattern, so "team-management/  " ignores the
    # dir -> already covered (rstrip mirrors git here).
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("team-management/  \n", encoding="utf-8")
    before = gi.read_text(encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["gitignore"]["status"] == "already_covered"
    assert out["gitignore"]["covered_by"] == "team-management/"
    assert gi.read_text(encoding="utf-8") == before  # no redundant line


def test_gitignore_leading_whitespace_not_covered(tmp_path, monkeypatch):
    # git treats LEADING whitespace as significant, so "  team-management/" does NOT
    # ignore the dir -> the guard must NOT report covered; it appends to protect.
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("  team-management/\n", encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["gitignore"]["status"] == "added"
    assert gi.read_text(encoding="utf-8").endswith("team-management/config.json\n")


def test_gitignore_covered_by_first_line_in_order(tmp_path, monkeypatch):
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("team-management/\nteam-management/config.json\n", encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["gitignore"]["status"] == "already_covered"
    assert out["gitignore"]["covered_by"] == "team-management/"  # first covering line wins


@pytest.mark.parametrize("line,expected", [
    ("team-management/", "team-management/"),
    ("team-management/sub/", "team-management/sub/"),
    ("/team-management/sub/config.json", "/team-management/sub/config.json"),
])
def test_gitignore_covers_deeper_nested_rel(line, expected):
    assert config_tool._gitignore_covers([line], "team-management/sub/config.json") == expected


def test_gitignore_unavailable_on_non_utf8(tmp_path, monkeypatch):
    # A non-UTF-8 .gitignore makes read_text raise UnicodeDecodeError (a ValueError,
    # NOT an OSError). The guard must degrade to "unavailable" and NOT block the
    # config write (regression for the "never raises" contract).
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_bytes(b"\xff\xfe team-management stuff\n")  # invalid UTF-8
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out  # write must NOT be blocked
    assert out["gitignore"]["status"] == "unavailable"


def test_gitignore_negation_forces_reignore(tmp_path, monkeypatch):
    # A negation re-including the file inverts a positive directory match — the guard
    # appends the positive entry to re-protect (last-match-wins re-ignores it).
    mcp = _git_project(tmp_path, monkeypatch)
    gi = tmp_path / ".gitignore"
    gi.write_text("team-management/\n!team-management/config.json\n", encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert out["gitignore"]["status"] == "added"
    assert gi.read_text(encoding="utf-8").endswith("team-management/config.json\n")


def test_gitignore_covers_negation_semantics():
    # exact-file negation -> not covered (so caller re-protects)
    assert config_tool._gitignore_covers(
        ["team-management/", "!team-management/config.json"],
        "team-management/config.json") is None
    # ancestor-dir negation -> not covered
    assert config_tool._gitignore_covers(
        ["team-management/", "!team-management/"],
        "team-management/config.json") is None
    # an UNRELATED negation must NOT suppress a genuine positive match
    assert config_tool._gitignore_covers(
        ["team-management/", "!node_modules/"],
        "team-management/config.json") == "team-management/"


# --------------------------------------------------------------------------
# config_update seeds the per-project provider-token file (m-per-project-provider-tokens)
# --------------------------------------------------------------------------

def test_config_update_seeds_provider_tokens_file(proj):
    """config_update creates .claude/state/provider-tokens.json (the per-project,
    user-authored token store) create-if-absent, and ensures `.claude/` is
    gitignored so the secret is never committable (codex R3 finding B — config_update
    otherwise only gitignored team-management/config.json)."""
    root, mcp = proj
    shared_state.write_config_session_flag()
    tokfile = root / ".claude" / "state" / "provider-tokens.json"
    assert not tokfile.exists()
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    # seeded with the provider-key template (all four keys + _comment).
    assert tokfile.exists()
    data = json.loads(tokfile.read_text(encoding="utf-8"))
    assert {k for k in data if k != "_comment"} == set(shared_state._PROVIDER_TOKEN_ENV)
    # `.claude/` ensured gitignored (secret must not be committable).
    gi = root / ".gitignore"
    assert gi.exists()
    assert ".claude/" in {ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()}


def test_config_update_does_not_clobber_existing_tokens_file(proj):
    """A pre-existing (filled) provider-token file is NEVER overwritten by
    config_update's seeder."""
    root, mcp = proj
    shared_state.write_config_session_flag()
    tokfile = root / ".claude" / "state" / "provider-tokens.json"
    tokfile.write_text(json.dumps({"github": "ghp_real"}), encoding="utf-8")
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True, out
    assert json.loads(tokfile.read_text(encoding="utf-8")) == {"github": "ghp_real"}


# --------------------------------------------------------------------------
# auto_compact.context_limit — positive-int override (m-fix-plugin-mode-install-bugs)
# get_model_context_limit() reads it as a >0 override; the config flow must be
# able to set it, with strict positive-int validation.
# --------------------------------------------------------------------------

def test_context_limit_accepted(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"auto_compact.context_limit": 1000000})
    assert out["success"] is True, out
    assert _read_config(root)["auto_compact"]["context_limit"] == 1000000


def test_context_limit_rejects_zero(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"auto_compact.context_limit": 0})
    assert out["success"] is False
    assert any("positive" in e for e in out["errors"])


def test_context_limit_rejects_negative(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"auto_compact.context_limit": -1})
    assert out["success"] is False
    assert any("positive" in e for e in out["errors"])


def test_context_limit_rejects_bool(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"auto_compact.context_limit": True})
    assert out["success"] is False
    assert any("positive" in e for e in out["errors"])


def test_context_limit_rejects_float(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"auto_compact.context_limit": 1.5})
    assert out["success"] is False
    assert any("positive" in e for e in out["errors"])


def test_context_limit_rejects_string(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"auto_compact.context_limit": "1000000"})
    assert out["success"] is False
    assert any("positive" in e for e in out["errors"])


# --------------------------------------------------------------------------
# Task-template deploy (h-fix-task-template-not-deployed)
# config_update is what first creates team-management/ on a fresh install, so it
# also deploys the task-file TEMPLATE.md the protocols reference — without it the
# user would have to restart before /team-management:config produces a template.
# --------------------------------------------------------------------------

_SOURCE_TEMPLATE = _REPO / "plugin" / "templates" / "TEMPLATE.md"


def test_config_update_deploys_task_template(proj, monkeypatch):
    root, mcp = proj
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_REPO / "plugin"))
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True
    dest = root / "team-management" / "tasks" / "TEMPLATE.md"
    assert dest.exists()
    # byte-identical to the plugin source
    assert dest.read_bytes() == _SOURCE_TEMPLATE.read_bytes()


def test_config_update_does_not_clobber_task_template(proj, monkeypatch):
    root, mcp = proj
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_REPO / "plugin"))
    dest = root / "team-management" / "tasks" / "TEMPLATE.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("# my custom template\n", encoding="utf-8")
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True
    assert dest.read_text(encoding="utf-8") == "# my custom template\n"


def test_config_update_succeeds_when_template_deploy_fails(proj, monkeypatch):
    root, mcp = proj
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_REPO / "plugin"))
    # tasks/ is a regular file -> the deploy fails internally; the config write
    # must still succeed (deploy is best-effort and self-swallowing).
    (root / "team-management" / "tasks").write_text("not a dir\n", encoding="utf-8")
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"developer_name": "Max"})
    assert out["success"] is True
    assert _read_config(root)["developer_name"] == "Max"
    assert (root / "team-management" / "tasks").is_file()


# --------------------------------------------------------------------------
# Schema exposure (m-fix-config-schema-exposure)
# config_get now returns a `schema` catalog so the /team-management:config flow
# reads each key's type/enum instead of guessing.
# --------------------------------------------------------------------------

def test_config_get_returns_schema(proj):
    root, mcp = proj
    _write_config(root, {"developer_name": "Max"})
    out = mcp.tools["config_get"]()
    assert out["success"] is True
    assert isinstance(out["schema"], list) and out["schema"]
    by_key = {e["key"]: e for e in out["schema"]}
    # the reported-missing key is now present and typed as a boolean
    assert by_key["code_review.enforce_warnings"]["type"] == "boolean"
    assert by_key["code_review.enforce_warnings"]["description"]


def test_config_get_schema_present_when_missing_file(proj):
    root, mcp = proj
    out = mcp.tools["config_get"]()
    assert out["success"] is True
    assert out["exists"] is False
    # schema is available even before config.json exists (configure-from-scratch)
    assert isinstance(out["schema"], list) and out["schema"]


def test_config_get_schema_present_when_unreadable(proj):
    root, mcp = proj
    _config_path(root).write_text("{not json", encoding="utf-8")
    out = mcp.tools["config_get"]()
    assert out["success"] is False
    assert "unreadable" in out["error"]
    assert isinstance(out["schema"], list) and out["schema"]


def test_every_schema_key_has_description():
    """Drift guard: _SCHEMA_DESCRIPTIONS must stay parallel to _CONFIG_SCHEMA, and
    _describe_schema must emit a non-empty description for every allowlisted key."""
    missing = [k for k in config_tool._CONFIG_SCHEMA
               if k not in config_tool._SCHEMA_DESCRIPTIONS]
    assert missing == [], f"schema keys with no description: {missing}"
    # reverse direction: no stray/misspelled description-only key (silently ignored
    # by _describe_schema's .get(key, "") otherwise)
    extra = [k for k in config_tool._SCHEMA_DESCRIPTIONS
             if k not in config_tool._CONFIG_SCHEMA]
    assert extra == [], f"descriptions with no matching schema key: {extra}"
    for entry in config_tool._describe_schema():
        assert entry["description"], f"empty description for {entry['key']}"
        assert entry["type"], f"empty type for {entry['key']}"


def test_describe_schema_renders_types():
    by_key = {e["key"]: e for e in config_tool._describe_schema()}
    # enum -> type + allowed list
    prov = by_key["issue_tracking.provider"]
    assert prov["type"] == "string (enum)"
    assert prov["allowed"] == ["gitlab", "jira", "github", "disabled"]
    # https URL / positive integer / string-or-null / bool / int / list / dict
    assert by_key["gitlab.base_url"]["type"] == "https URL"
    assert by_key["auto_compact.context_limit"]["type"] == "positive integer"
    assert by_key["test_command"]["type"] == "string or null"
    assert by_key["api_mode"]["type"] == "boolean"
    assert by_key["auto_compact.threshold"]["type"] == "integer"
    assert by_key["blocked_tools"]["type"] == "array"
    assert by_key["github.workflow_labels"]["type"] == "object"
    assert by_key["features.icon_style"]["allowed"] == ["nerd_fonts", "emoji", "ascii"]


# --------------------------------------------------------------------------
# Newly-allowlisted keys are settable (m-fix-config-schema-exposure)
# --------------------------------------------------------------------------

def test_enforce_warnings_settable(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"code_review.enforce_warnings": True})
    assert out["success"] is True, out
    assert _read_config(root)["code_review"]["enforce_warnings"] is True


def test_enforce_warnings_rejects_non_bool(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"code_review.enforce_warnings": "yes"})
    assert out["success"] is False
    assert any("code_review.enforce_warnings" in e for e in out["errors"])


def test_new_scalar_and_collection_keys_settable(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({
        "features.icon_style": "emoji",
        "branch_enforcement.branch_prefixes": {"spike-": "spike/"},
        "notifications.enabled": True,
        "notifications.mode": "off",
        "notifications.prefix": "myproj",
        "notifications.channels.telegram.enabled": True,
        "notifications.channels.telegram.chat_id": "123456",
        "github.default_labels": ["claude-code"],
        "jira.api_version": "3",
        "jira.default_issue_type": "Task",
        "jira.supported_issue_types": ["Bug", "Task"],
    })
    assert out["success"] is True, out
    cfg = _read_config(root)
    assert cfg["features"]["icon_style"] == "emoji"
    assert cfg["branch_enforcement"]["branch_prefixes"] == {"spike-": "spike/"}
    assert cfg["notifications"]["mode"] == "off"
    assert cfg["notifications"]["channels"]["telegram"]["chat_id"] == "123456"
    assert cfg["github"]["default_labels"] == ["claude-code"]
    assert cfg["jira"]["supported_issue_types"] == ["Bug", "Task"]


def test_project_name_settable(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"project_name": "my-cool-app"})
    assert out["success"] is True, out
    assert _read_config(root)["project_name"] == "my-cool-app"


def test_project_name_in_schema(proj):
    root, mcp = proj
    out = mcp.tools["config_get"]()
    by_key = {e["key"]: e for e in out["schema"]}
    assert by_key["project_name"]["type"] == "string"
    assert by_key["project_name"]["description"]


def test_icon_style_rejects_bad_enum(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"features.icon_style": "comic-sans"})
    assert out["success"] is False
    assert any("must be one of" in e for e in out["errors"])


def test_notifications_mode_rejects_bad_enum(proj):
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"notifications.mode": "loud"})
    assert out["success"] is False
    assert any("must be one of" in e for e in out["errors"])


def test_jira_api_version_rejects_unsupported(proj):
    """jira.api_version is an enum matching the JiraAPI runtime allowlist ('2'/'3',
    jira_utils.py:84) — an unsupported value must be rejected at write time rather
    than persisted into a config the runtime later refuses to load."""
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"]({"jira.api_version": "4"})
    assert out["success"] is False
    assert any("must be one of" in e for e in out["errors"])


def test_telegram_bot_token_still_rejected_but_chat_id_ok(proj):
    """The Telegram bot token is a secret (matched by _SENSITIVE_KEY_RE) and must
    stay rejected even though it lives under notifications.*; the non-secret chat_id
    is settable."""
    root, mcp = proj
    shared_state.write_config_session_flag()
    out = mcp.tools["config_update"](
        {"notifications.channels.telegram.bot_token": "12345:secret"})
    assert out["success"] is False
    assert any("provider-tokens.json" in e for e in out["errors"])
    assert not _config_path(root).exists()
    # chat_id (non-secret) writes fine on its own
    out2 = mcp.tools["config_update"](
        {"notifications.channels.telegram.chat_id": "999"})
    assert out2["success"] is True, out2
    assert _read_config(root)["notifications"]["channels"]["telegram"]["chat_id"] == "999"
