#!/usr/bin/env python3
"""Token-resolution + seeder + SEC-003 tests.

Provider tokens live in a PER-PROJECT, user-authored file
`.claude/state/provider-tokens.json` (m-per-project-provider-tokens). The
OS-keychain `userConfig` model and its env tier (`CLAUDE_PLUGIN_OPTION_*`) were
removed because the keychain is global-per-plugin-per-user and cannot hold
different tokens for two projects.

Covers:
- `resolve_provider_token`: file -> config fallback (NO env tier). File key is
  the provider name, with the legacy `CLAUDE_PLUGIN_OPTION_*` env-name key
  accepted as a back-compat fallback.
- `ensure_provider_tokens_file`: create-if-absent seeding of a template, 0600,
  never clobbering / deleting an existing file, ensures `.claude/` is gitignored.
- the `CLAUDE_PLUGIN_OPTION_` credential-filter pattern (SEC-003a) and the
  `env -i` scrub in the codex/agy wrapper agents (SEC-003b) — both retained as
  defense-in-depth for any lingering leak channel.

Run with: python3 -m pytest test/test_token_resolution.py -v
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
AGENTS_DIR = REPO_ROOT / "plugin" / "agents"
sys.path.insert(0, str(HOOKS_DIR))

import shared_state  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_project(monkeypatch, tmp_path):
    """Pin CLAUDE_PROJECT_DIR to a tmp dir for every test in this module.

    resolve_provider_token reads .claude/state/provider-tokens.json under
    get_project_root(); without this a test expecting the config fallback could
    accidentally read the developer's REAL token file. Also clears the four
    legacy CLAUDE_PLUGIN_OPTION_* vars so a stray value in the dev env cannot
    influence a test (the env tier is gone, but the credential-filter tests set
    these strings, so keep the baseline clean)."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    for env_name in shared_state._PROVIDER_TOKEN_ENV.values():
        monkeypatch.delenv(env_name, raising=False)


def _tokens_path():
    return shared_state._provider_tokens_path()


def _write_tokens(mapping):
    _tokens_path().write_text(json.dumps(mapping), encoding="utf-8")


# --------------------------------------------------------------------------
# resolve_provider_token: file (provider-name key) -> config fallback
# --------------------------------------------------------------------------

def test_file_provider_key_wins_over_config():
    _write_tokens({"gitlab": "glpat_file"})
    assert shared_state.resolve_provider_token("gitlab", "cfg") == "glpat_file"


def test_config_fallback_when_no_file():
    assert shared_state.resolve_provider_token("github", "cfg") == "cfg"
    assert shared_state.resolve_provider_token("github") is None


def test_config_fallback_when_key_empty():
    """An empty provider key must not shadow a real config token."""
    _write_tokens({"github": ""})
    assert shared_state.resolve_provider_token("github", "cfg") == "cfg"


def test_both_absent_returns_none():
    assert shared_state.resolve_provider_token("gitlab") is None
    assert shared_state.resolve_provider_token("gitlab", None) is None


def test_legacy_envname_key_fallback():
    """A bridge file written by an older version keyed tokens by the
    CLAUDE_PLUGIN_OPTION_* env-name. Those are still read as a back-compat
    fallback when the provider-name key is absent."""
    _write_tokens({"CLAUDE_PLUGIN_OPTION_GITHUB_API_TOKEN": "ghp_legacy"})
    assert shared_state.resolve_provider_token("github", "cfg") == "ghp_legacy"


def test_provider_key_wins_over_legacy_key():
    _write_tokens({"github": "ghp_new",
                   "CLAUDE_PLUGIN_OPTION_GITHUB_API_TOKEN": "ghp_old"})
    assert shared_state.resolve_provider_token("github", "cfg") == "ghp_new"


def test_env_tier_removed_env_no_longer_wins(monkeypatch):
    """REGRESSION: the env/keychain tier is GONE. A CLAUDE_PLUGIN_OPTION_* env
    var must NOT resolve as a token — only the file and config are consulted."""
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_GITLAB_API_TOKEN", "env-tok")
    assert shared_state.resolve_provider_token("gitlab", "cfg") == "cfg"
    assert shared_state.resolve_provider_token("gitlab") is None


def test_unknown_provider_returns_config():
    assert shared_state.resolve_provider_token("bitbucket", "cfg") == "cfg"
    assert shared_state.resolve_provider_token("bitbucket") is None


def test_unknown_provider_ignores_file():
    """An unknown provider has no key mapping, so the file is never a source
    even if it contains keys."""
    _write_tokens({"github": "ghp_file"})
    assert shared_state.resolve_provider_token("bitbucket", "cfg") == "cfg"


def test_each_provider_reads_its_own_key():
    """Per-provider isolation across the four known providers (incl. telegram)."""
    cases = {"gitlab": "glpat", "jira": "jira", "github": "ghp", "telegram": "tg"}
    for provider, val in cases.items():
        _write_tokens({provider: f"{val}-file"})
        assert shared_state.resolve_provider_token(provider, "cfg") == f"{val}-file"
        other = next(p for p in cases if p != provider)
        assert shared_state.resolve_provider_token(other, "cfg") == "cfg"


def test_telegram_resolves_via_file():
    _write_tokens({"telegram": "tg_file"})
    assert shared_state.resolve_provider_token("telegram", "cfg") == "tg_file"


def test_reader_tolerates_malformed_file():
    _tokens_path().write_text("{ not json", encoding="utf-8")
    assert shared_state.resolve_provider_token("github", "cfg") == "cfg"


def test_reader_tolerates_non_dict_file():
    _tokens_path().write_text("[]", encoding="utf-8")
    assert shared_state.resolve_provider_token("github", "cfg") == "cfg"


def test_resolver_uses_call_time_project_root(tmp_path):
    """shared_state was imported at module load with no CLAUDE_PROJECT_DIR, so its
    module-level PROJECT_ROOT is the repo root, NOT this tmp project. Resolution
    must still find the tmp file -> the path is resolved at CALL time, not from
    the frozen import-time constant."""
    assert shared_state.PROJECT_ROOT != tmp_path
    _write_tokens({"jira": "jira_file"})
    assert shared_state.resolve_provider_token("jira", "cfg") == "jira_file"
    assert _tokens_path().parent.parent.parent == tmp_path


# --------------------------------------------------------------------------
# ensure_provider_tokens_file: create-if-absent seeder (never clobber/delete)
# --------------------------------------------------------------------------

def test_seeder_creates_template_when_absent():
    assert not _tokens_path().exists()
    assert shared_state.ensure_provider_tokens_file() is True
    data = json.loads(_tokens_path().read_text(encoding="utf-8"))
    assert "_comment" in data
    # every known provider seeded with an empty value for the user to fill.
    for provider in shared_state._PROVIDER_TOKEN_ENV:
        assert data.get(provider) == ""


def test_seeder_template_keys_match_providers():
    shared_state.ensure_provider_tokens_file()
    data = json.loads(_tokens_path().read_text(encoding="utf-8"))
    token_keys = {k for k in data if k != "_comment"}
    assert token_keys == set(shared_state._PROVIDER_TOKEN_ENV)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode")
def test_seeder_sets_0600():
    shared_state.ensure_provider_tokens_file()
    mode = stat.S_IMODE(os.stat(_tokens_path()).st_mode)
    assert mode == 0o600, oct(mode)


def test_seeder_never_clobbers_existing():
    """A hand-authored (filled) file must survive verbatim — the seeder is
    create-if-absent, so a populated token file is NEVER overwritten."""
    _write_tokens({"gitlab": "my_real_token", "github": "ghp_real"})
    assert shared_state.ensure_provider_tokens_file() is True
    data = json.loads(_tokens_path().read_text(encoding="utf-8"))
    assert data == {"gitlab": "my_real_token", "github": "ghp_real"}


def test_seeder_never_deletes_when_empty():
    """A file with all-empty values is still a user-owned file — the seeder must
    NOT delete it (the old bridge writer deleted on empty, which would wipe a
    user's freshly-created file every session)."""
    _write_tokens({"gitlab": "", "jira": "", "github": "", "telegram": ""})
    assert shared_state.ensure_provider_tokens_file() is True
    assert _tokens_path().exists()
    data = json.loads(_tokens_path().read_text(encoding="utf-8"))
    assert data == {"gitlab": "", "jira": "", "github": "", "telegram": ""}


def test_seeder_never_clobbers_malformed_existing():
    """A pre-existing but malformed file is still NOT overwritten (create-if-absent
    short-circuits on `path.exists()` before any read) — so a user mid-edit does not
    lose their content to a silent re-seed."""
    _tokens_path().write_text("{ half-written not json", encoding="utf-8")
    assert shared_state.ensure_provider_tokens_file() is True
    assert _tokens_path().read_text(encoding="utf-8") == "{ half-written not json"


def test_seeder_ensures_claude_gitignored(tmp_path):
    """Seeding a secret must guarantee `.claude/` is gitignored first (covers the
    config_update seed path, which does not otherwise ensure it)."""
    gi = tmp_path / ".gitignore"
    assert not gi.exists()
    shared_state.ensure_provider_tokens_file()
    assert gi.exists()
    nonblank = {ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()}
    assert ".claude/" in nonblank


def test_seeder_gitignore_noop_when_already_ignored(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text(".claude/\n", encoding="utf-8")
    shared_state.ensure_provider_tokens_file()
    # idempotent: no duplicate `.claude/` line.
    lines = [ln for ln in gi.read_text(encoding="utf-8").splitlines() if ln.strip() == ".claude/"]
    assert len(lines) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode")
def test_seeder_tightens_perms_on_existing_user_created_file():
    """A user-created token file with loose 0644 perms is tightened to owner-only
    (0600) by the seeder WITHOUT its contents being rewritten (codex review P2) —
    nothing else tightens a hand-created secret file's permissions. `.claude/`
    git-ignoring is owned by session-start (unconditional every session), so the
    seeder deliberately does NOT re-touch the .gitignore on the existing-file path."""
    tok = _tokens_path()
    tok.write_text(json.dumps({"gitlab": "user_tok"}), encoding="utf-8")
    os.chmod(tok, 0o644)
    assert shared_state.ensure_provider_tokens_file() is True
    # contents untouched
    assert json.loads(tok.read_text(encoding="utf-8")) == {"gitlab": "user_tok"}
    # permissions tightened to owner-only
    assert stat.S_IMODE(os.stat(tok).st_mode) == 0o600


# --------------------------------------------------------------------------
# SEC-003a: CLAUDE_PLUGIN_OPTION_ credential filter (retained defense-in-depth)
# --------------------------------------------------------------------------

def _filter(text):
    """Call the mixin's _filter_credentials unbound — it ignores `self`."""
    import ai_providers
    return ai_providers.AIProvidersMixin._filter_credentials(None, text)


def test_credential_filter_redacts_plugin_option_assignment():
    text = "export CLAUDE_PLUGIN_OPTION_GITHUB_API_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    out = _filter(text)
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in out
    assert "[REDACTED:" in out


def test_credential_filter_redacts_plugin_option_non_token_suffix():
    """A CLAUDE_PLUGIN_OPTION_ var whose name does NOT end in token/key/secret
    is still redacted by the dedicated plugin-option pattern."""
    text = "CLAUDE_PLUGIN_OPTION_SOMETHING: superSecretValue123"
    out = _filter(text)
    assert "superSecretValue123" not in out
    assert "[REDACTED:" in out


def test_credential_filter_passes_ordinary_prose():
    text = "The configurator exports CLAUDE_PLUGIN_OPTION variables to subprocesses."
    out = _filter(text)
    # No assignment separator → not an assignment → passes through unredacted.
    assert out.strip() == text


# --------------------------------------------------------------------------
# SEC-003b: env -i scrub in the wrapper agents (drift grep)
# --------------------------------------------------------------------------

def test_codex_wrapper_scrubs_env():
    md = (AGENTS_DIR / "codex-cli.md").read_text(encoding="utf-8")
    scrubbed = 'env -i PATH="$PATH" HOME="$HOME" codex'
    # §2 skeleton (both branches) + §3 schema block (both branches) → 4 scrubbed
    # invocations. (The prose intentionally quotes the WRONG `300s codex` form as
    # an anti-pattern, so a bare-grep negative would false-positive on it.)
    assert md.count(scrubbed) >= 2
    # Every fenced timeout-branch line that runs codex carries the scrub.
    assert 'env -i PATH="$PATH" HOME="$HOME" codex' in md


def test_agy_wrapper_scrubs_env():
    md = (AGENTS_DIR / "agy-cli.md").read_text(encoding="utf-8")
    scrubbed = 'env -i PATH="$PATH" HOME="$HOME" agy'
    # Exactly two agy invocations (timeout branch + shell-native fallback),
    # both scrubbed.
    assert md.count(scrubbed) == 2
    # No bare "330s agy" (timeout branch without the scrub).
    assert "330s agy " not in md
