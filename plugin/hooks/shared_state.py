#!/usr/bin/env python3
"""Shared state management for team-management hooks."""
import os
import sys
import json
import time
import hashlib
import re
import tempfile
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

# Three-root path model (plugin conversion): PROJECT_DIR / PLUGIN_ROOT / PLUGIN_DATA.
def get_project_root():
    """Find the project root.

    Plugin model: Claude Code sets ``CLAUDE_PROJECT_DIR`` in the hook environment
    (verified by the PoC spike, F2) and it points at the project root, not the
    plugin install — so read it first. Fall back to walking up from cwd looking
    for a ``.claude`` directory (dev / non-plugin invocation, where the env var
    is absent). This function NO LONGER writes the env var: in the plugin runtime
    it is already present, and the old module-level write masked a
    plugin-injected value.
    """
    env_dir = os.environ.get('CLAUDE_PROJECT_DIR')
    if env_dir:
        p = Path(env_dir)
        if p.exists():
            return p
    current = Path.cwd()
    while current.parent != current:
        if (current / ".claude").exists():
            return current
        current = current.parent
    # Fallback to current directory if no .claude found
    return Path.cwd()


def get_plugin_root():
    """Read-only plugin install dir (hooks / mcp / agents / commands / system
    protocol-configs / knowledge / templates).

    Reads ``CLAUDE_PLUGIN_ROOT`` (set by Claude Code for plugin processes). Dev /
    source-checkout fallback: this file lives at ``<plugin>/hooks/shared_state.py``
    so ``parent.parent`` is the plugin source dir. PLUGIN_ROOT is replaced on
    plugin update (spike F3) — never persist state under it; use get_plugin_data().
    """
    env_root = os.environ.get('CLAUDE_PLUGIN_ROOT')
    if env_root:
        p = Path(env_root)
        if p.exists():
            return p
    return Path(__file__).resolve().parent.parent


def get_plugin_data():
    """Persistent per-plugin data dir (venv, caches); survives plugin updates.

    Reads ``CLAUDE_PLUGIN_DATA`` only — there is deliberately NO ``__file__``
    fallback: PLUGIN_ROOT is ephemeral (spike F3), so falling back to it for
    persistent state would silently lose data on update. Returns ``None`` when
    the env var is absent (non-plugin / dev run).
    """
    env_data = os.environ.get('CLAUDE_PLUGIN_DATA')
    return Path(env_data) if env_data else None


def is_plugin_mode():
    """True when running as a Claude Code plugin (host injects CLAUDE_PLUGIN_ROOT).

    Legacy / dev hook runs (invoked from .claude/ via settings.json) leave it
    unset. Plugin-only behaviors — the boot-detector advisory + PreToolUse
    companion and guidance-inject — gate on this so the dual-purpose hook files
    stay no-ops in a legacy/dev checkout and never self-block it.
    """
    return bool(os.environ.get('CLAUDE_PLUGIN_ROOT'))


def ensure_task_template_deployed(project_root, plugin_root):
    """Deploy the task-file TEMPLATE.md into a project (best-effort, create-if-absent).

    The retired pip installer (#7) used to copy ``plugin/templates/TEMPLATE.md``
    into a project as ``team-management/tasks/TEMPLATE.md``; nothing replaced that
    step, yet many protocol steps tell the agent to read that path for the standard
    task-file format. Two callers deploy it so the file is present both across
    restarts (the SessionStart self-heal in ``session-start.py``) and immediately
    after a fresh ``/team-management:config`` (the ``config_update`` MCP tool, which
    is what first creates ``team-management/`` on a new install):

      - create-if-absent: never clobbers an existing (possibly user-edited) copy;
      - creates ``team-management/tasks/`` when missing;
      - returns ``True`` only when it actually wrote the file, else ``False``;
      - best-effort: any error is reported once to stderr and swallowed — the
        SessionStart hook and the MCP server must never break on this.

    ``plugin_root`` is the plugin install dir (``get_plugin_root()``); the source is
    ``<plugin_root>/templates/TEMPLATE.md``. A ``None`` plugin_root or an absent
    source template degrades to a no-op (returns ``False``) — e.g. a misresolved
    ``CLAUDE_PLUGIN_ROOT``, consistent with the other ``_ensure_*`` self-heals.
    """
    try:
        if plugin_root is None:
            return False
        src = Path(plugin_root) / 'templates' / 'TEMPLATE.md'
        dest = Path(project_root) / 'team-management' / 'tasks' / 'TEMPLATE.md'
        if dest.exists() or not src.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Byte copy (NOT read_text/write_text): a faithful template copy must be
        # byte-identical to the source — text mode would translate newlines on
        # Windows (\n -> \r\n on write), breaking the byte-identical contract.
        # Atomic (tmp + os.replace, like _write_json_durable): a process kill or
        # full disk mid-write would otherwise leave a TRUNCATED dest, which the
        # create-if-absent guard above would then never re-heal.
        data = src.read_bytes()
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=f"{dest.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        return True
    except Exception as exc:
        sys.stderr.write(
            f"[team-management] could not deploy task TEMPLATE.md: {exc}\n"
        )
        return False


def _refresh_file_if_changed(src, dest):
    """Atomic byte copy of ``src`` -> ``dest``, but ONLY when content differs.

    Refresh-on-change variant of the ``ensure_task_template_deployed`` copy (tmp +
    fsync + os.replace, byte mode so Windows never translates newlines). Returns
    True when it wrote (dest missing or stale), False when already up to date or
    ``src`` is absent. Caller wraps in a best-effort try/except.
    """
    src = Path(src)
    dest = Path(dest)
    if not src.exists():
        return False
    data = src.read_bytes()
    if dest.exists():
        try:
            if dest.read_bytes() == data:
                return False
        except OSError:
            pass  # unreadable dest -> overwrite
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=f"{dest.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, dest)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return True


# --- agy read-only PreToolUse deny-gate deployment ------------------------------
# The agy-cli wrapper runs `agy --dangerously-skip-permissions` for headless review
# (headless print mode otherwise soft-denies every command). skip-permissions is
# CONTAINED by a project-local `.agents/hooks.json` PreToolUse hook that hard-denies
# any tool call outside a read-only allowlist. team-management deploys that gate into
# a project whenever agy is an enabled provider (SessionStart + config_update).
_AGY_GATE_HOOK_NAME = "team-management-readonly-gate"
_AGY_GATE_SCRIPT = "agy-readonly-gate.py"


def _agy_enabled(config):
    """True iff agy would actually be dispatched as an AI provider.

    Mirrors the dual gate the AI-provider resolver uses (``ai_providers.py``
    ``_resolve_ai_providers``): agy must be listed in
    ``ai_providers.enabled_providers`` AND ``agy.enabled`` must be true. Kept in
    lockstep with that check so the gate is deployed exactly when agy can run.
    isinstance-guarded so a malformed config never raises.
    """
    if not isinstance(config, dict):
        return False
    ai = config.get("ai_providers")
    enabled_providers = ai.get("enabled_providers", []) if isinstance(ai, dict) else []
    agy = config.get("agy")
    agy_enabled = bool(agy.get("enabled", False)) if isinstance(agy, dict) else False
    return "agy" in (enabled_providers or []) and agy_enabled


def _agy_gate_hooks_entry():
    """The team-management named-hook entry for a project's .agents/hooks.json.

    The command is RELATIVE (``python3 agy-readonly-gate.py``): agy runs a hook
    command via ``sh -c`` with cwd set to the directory containing hooks.json
    (``.agents/``), so a relative reference is portable — it survives the project
    moving and needs no absolute path baked into a committable file.
    """
    return {
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": f"python3 {_AGY_GATE_SCRIPT}", "timeout": 10},
                ],
            }
        ]
    }


def agy_gate_is_deployed(project_root):
    """True iff a project's .agents/hooks.json already carries our named gate hook.

    Consumed by the agy-cli wrapper's preflight so it never runs
    ``--dangerously-skip-permissions`` uncontained. Requires ALL of: the gate
    script present, hooks.json parseable as a JSON object, and our named entry
    DEEP-EQUAL to the canonical entry (so a tampered command target, a hook
    pointed at another script, or a structurally-wrong entry fails the check
    rather than passing a mere key-presence test). Best-effort — any error → False.
    """
    try:
        agents = Path(project_root) / '.agents'
        hooks_json = agents / 'hooks.json'
        if not (agents / _AGY_GATE_SCRIPT).exists() or not hooks_json.exists():
            return False
        data = json.loads(hooks_json.read_text(encoding='utf-8'))
        return (isinstance(data, dict)
                and data.get(_AGY_GATE_HOOK_NAME) == _agy_gate_hooks_entry())
    except Exception:
        return False


def ensure_agy_readonly_gate_deployed(project_root, plugin_root):
    """Deploy the agy read-only PreToolUse deny-gate into a project (best-effort).

    Called whenever agy is an enabled provider (SessionStart plugin block + the
    config_update MCP tool). Deploys two artefacts under ``<project>/.agents/``:

      - ``agy-readonly-gate.py`` — refresh-on-change byte copy of the plugin
        source (plugin-owned, replaced on update, like the guidance files);
      - ``hooks.json`` — MERGE-AWARE: the named hook
        ``team-management-readonly-gate`` is added/updated while every OTHER
        top-level key (the user's own agy hooks) is preserved verbatim. A
        pre-existing hooks.json that is not a JSON object is left UNTOUCHED (a
        breadcrumb is written) rather than clobbered — a safe degradation
        (the wrapper preflight then reports the gate is not deployed).

    Best-effort: any error → one stderr breadcrumb + ``False``. A ``None``
    plugin_root or an absent source degrades to a no-op. Returns ``True`` when it
    wrote/updated hooks.json, else ``False``.
    """
    try:
        if plugin_root is None:
            return False
        src = Path(plugin_root) / 'templates' / _AGY_GATE_SCRIPT
        if not src.exists():
            return False
        agents_dir = Path(project_root) / '.agents'
        agents_dir.mkdir(parents=True, exist_ok=True)
        # Keep the plugin-generated gate out of the host project's VCS (mirrors
        # `.claude/`); runs on every deploy incl. no-op/already-current calls.
        ensure_agents_dir_gitignored(project_root)
        # 1. Deploy the gate script (refresh-on-change; plugin-owned, replaced on update).
        _refresh_file_if_changed(src, agents_dir / _AGY_GATE_SCRIPT)
        # 2. Merge our named hook into hooks.json without clobbering user hooks.
        hooks_path = agents_dir / 'hooks.json'
        data = {}
        if hooks_path.exists():
            try:
                data = json.loads(hooks_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                data = None
            if not isinstance(data, dict):
                sys.stderr.write(
                    "[team-management] .agents/hooks.json is not a JSON object; "
                    "leaving it untouched (agy read-only gate not wired)\n"
                )
                return False
        desired = _agy_gate_hooks_entry()
        if data.get(_AGY_GATE_HOOK_NAME) == desired:
            return False  # already current — skip the write (no needless git churn)
        data[_AGY_GATE_HOOK_NAME] = desired
        _write_json_durable(hooks_path, data)
        return True
    except Exception as exc:
        sys.stderr.write(
            f"[team-management] could not deploy agy read-only gate: {exc}\n"
        )
        return False


# Plugin-owned behavioral-guidance files deployed into a project so they can be
# wired into the project's CLAUDE.md via native @-includes (durable across /compact)
# instead of injected as one-shot SessionStart additionalContext (which fades out of
# a long/compacted session). The knowledge files are deployed so the on-demand
# (backticked, NOT @-imported) references in CLAUDE.tm.md resolve when the model
# reads them on demand (h-durable-guidance-via-claude-md).
_GUIDANCE_KNOWLEDGE_FILES = ("tdd-discipline.md", "debugging.md", "receiving-feedback.md")


def deploy_guidance_files(project_root, plugin_root, wiki_enabled):
    """Refresh-on-change deploy of the plugin-owned guidance files into a project.

    Mirrors ``ensure_task_template_deployed`` (atomic, byte-copy, best-effort) but
    REFRESHES on content change rather than create-if-absent, because CLAUDE.tm.md
    and the knowledge files are plugin-owned and replaced on every plugin update (a
    refresh produces a reviewable git diff; it never commits). Deploys:

      - ``<plugin>/templates/CLAUDE.tm.md``   -> ``<proj>/CLAUDE.tm.md``
      - ``<plugin>/knowledge/<f>.md``         -> ``<proj>/team-management/knowledge/<f>.md``
      - ``<plugin>/templates/CLAUDE.wiki.md`` -> ``<proj>/CLAUDE.wiki.md`` (only when wiki_enabled)

    Returns the list of dest paths actually (re)written. A ``None`` plugin_root or an
    absent source degrades to a no-op (consistent with the other _ensure_* helpers).
    """
    refreshed = []
    try:
        if plugin_root is None:
            return refreshed
        plugin_root = Path(plugin_root)
        project_root = Path(project_root)
        targets = [
            (plugin_root / 'templates' / 'CLAUDE.tm.md', project_root / 'CLAUDE.tm.md'),
        ]
        for name in _GUIDANCE_KNOWLEDGE_FILES:
            targets.append((plugin_root / 'knowledge' / name,
                            project_root / 'team-management' / 'knowledge' / name))
        if wiki_enabled:
            targets.append((plugin_root / 'templates' / 'CLAUDE.wiki.md',
                            project_root / 'CLAUDE.wiki.md'))
        for src, dest in targets:
            if _refresh_file_if_changed(src, dest):
                refreshed.append(str(dest))
    except Exception as exc:
        sys.stderr.write(f"[team-management] could not deploy guidance files: {exc}\n")
    return refreshed


_CLAUDE_MD_BEGIN = "<!-- team-management:begin (managed by /team-management:init; do not edit inside) -->"
_CLAUDE_MD_END = "<!-- team-management:end -->"


def _build_managed_block(wiki_enabled):
    """Construct the managed @-include block text (no trailing newline)."""
    lines = [_CLAUDE_MD_BEGIN, "@CLAUDE.tm.md", "@CLAUDE.tm.custom.md"]
    if wiki_enabled:
        lines.append("@CLAUDE.wiki.md")
    lines.append(_CLAUDE_MD_END)
    return "\n".join(lines)


def _strip_managed_blocks(content):
    """Remove every well-formed BEGIN..END managed block from ``content``.

    Loop-based (no regex dependency). A BEGIN with no following END is left intact
    (pathological hand-edit); orphan marker lines are dropped separately by the
    caller. Collapsing all blocks lets us re-append exactly one canonical block,
    which is what keeps the operation idempotent across repeated hook runs.
    """
    out = content
    while True:
        b = out.find(_CLAUDE_MD_BEGIN)
        if b == -1:
            break
        e = out.find(_CLAUDE_MD_END, b)
        if e == -1:
            break  # orphan BEGIN, no matching END -> stop, leave it
        out = out[:b] + out[e + len(_CLAUDE_MD_END):]
    return out


def ensure_claude_md_managed_block(project_root, wiki_enabled):
    """Idempotently wire the guidance @-includes into the project's CLAUDE.md.

    Writes a marked region (``_CLAUDE_MD_BEGIN`` .. ``_CLAUDE_MD_END``) holding
    ``@CLAUDE.tm.md`` + ``@CLAUDE.tm.custom.md`` (+ ``@CLAUDE.wiki.md`` when
    wiki_enabled). On re-run all managed blocks are collapsed and the canonical
    block is re-appended once at EOF (handles duplicate/orphan markers); user content
    outside the markers is preserved in order. Toggling wiki adds/removes only the
    @CLAUDE.wiki.md line. Creates CLAUDE.md with a minimal header when absent. Idempotent (no
    write when already correct). Atomic, best-effort. Returns True when it wrote.

    NOTE: @-imports in CLAUDE.md resolve relative to the file that contains them, so
    these project-root-relative includes require the deployed files to be siblings of
    CLAUDE.md. Callers MUST deploy_guidance_files() BEFORE wiring so no @-target is
    ever dangling (Claude Code degrades a missing import gracefully, but order avoids
    even a transient gap).
    """
    try:
        project_root = Path(project_root)
        path = project_root / 'CLAUDE.md'
        block = _build_managed_block(wiki_enabled)
        if path.exists():
            content = path.read_text(encoding='utf-8')
        else:
            content = f"# {project_root.name}\n"
        body = _strip_managed_blocks(content)
        # Drop orphan marker lines left by hand-editing (a BEGIN with no END, etc.).
        if _CLAUDE_MD_BEGIN in body or _CLAUDE_MD_END in body:
            body = "\n".join(
                ln for ln in body.split("\n")
                if ln.strip() not in (_CLAUDE_MD_BEGIN, _CLAUDE_MD_END)
            )
        body = body.rstrip("\n")
        # Canonical form: one managed block appended at EOF (collapses 0/1/N prior
        # blocks uniformly; stable/idempotent on re-run).
        new_content = f"{body}\n\n{block}\n" if body else f"{block}\n"
        if path.exists() and new_content == content:
            return False
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        return True
    except Exception as exc:
        sys.stderr.write(f"[team-management] could not wire CLAUDE.md guidance block: {exc}\n")
        return False


_CUSTOM_RULES_STUB = (
    "# CLAUDE.tm.custom.md\n\n"
    "<!-- Project-specific rules and custom protocol notes go here.\n"
    "     team-management never overwrites this file on update; it is wired into\n"
    "     CLAUDE.md via @CLAUDE.tm.custom.md. -->\n"
)


def ensure_custom_rules_stub(project_root):
    """Create the project-owned custom-rules stub (create-if-absent, atomic).

    The managed CLAUDE.md block references ``@CLAUDE.tm.custom.md``; this ensures the
    @-target exists for both the SessionStart self-heal and /team-management:init.
    Never overwrites an existing file (it holds the user's rules). Best-effort.
    Returns True only when it wrote.
    """
    try:
        dest = Path(project_root) / 'CLAUDE.tm.custom.md'
        if dest.exists():
            return False
        data = _CUSTOM_RULES_STUB.encode('utf-8')
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=f"{dest.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        return True
    except Exception as exc:
        sys.stderr.write(f"[team-management] could not create CLAUDE.tm.custom.md stub: {exc}\n")
        return False


def ensure_guidance_deployed_and_wired(project_root, plugin_root, wiki_enabled):
    """Deploy the plugin-owned guidance files, then wire the CLAUDE.md @-block.

    Single entry point for the whole delivery so the deploy-BEFORE-wire ordering
    cannot drift between the SessionStart self-heal and the config_update MCP path
    (both call this). Order: ensure the custom-rules stub exists, refresh-on-change
    the plugin-owned files, then wire/refresh the managed @-block. Each step is
    independently best-effort. Returns the deploy_guidance_files result list.
    """
    ensure_custom_rules_stub(project_root)
    refreshed = deploy_guidance_files(project_root, plugin_root, wiki_enabled)
    ensure_claude_md_managed_block(project_root, wiki_enabled)
    return refreshed


# Provider API token resolution (m-per-project-provider-tokens): the token lives in
# a PER-PROJECT, user-authored file `.claude/state/provider-tokens.json`, keyed by
# provider name. Resolution is file -> config. The OS-keychain `userConfig` model and
# its env tier (CLAUDE_PLUGIN_OPTION_*) were REMOVED: the keychain is
# global-per-plugin-per-user, so two projects could not use different tokens.
# `_PROVIDER_TOKEN_ENV` is kept ONLY to (a) enumerate the known providers for the seed
# template and (b) read the LEGACY env-name key from bridge files written by older
# versions. The AI cannot read the token file -- it is a PROTECTED, git-ignored, 0600
# path (sessions-enforce.PROTECTED_PATHS + _targets_token_bridge).
_PROVIDER_TOKEN_ENV = {
    "gitlab": "CLAUDE_PLUGIN_OPTION_GITLAB_API_TOKEN",
    "jira": "CLAUDE_PLUGIN_OPTION_JIRA_API_TOKEN",
    "github": "CLAUDE_PLUGIN_OPTION_GITHUB_API_TOKEN",
    "telegram": "CLAUDE_PLUGIN_OPTION_TELEGRAM_BOT_TOKEN",
}


def resolve_provider_token(provider, config_value=None):
    """Resolve a provider API token: per-project token file -> config value.

    `provider` is one of ``gitlab`` / ``jira`` / ``github`` / ``telegram``.
    Resolution order:
      1. ``.claude/state/provider-tokens.json`` -- the per-project, user-authored
         token store, keyed by provider name (legacy ``CLAUDE_PLUGIN_OPTION_*``
         env-name keys are accepted as a back-compat fallback). A plain file read,
         never a Claude tool, so the protected-path guard on ``.claude/state/`` does
         not block the server's own read.
      2. ``config_value`` -- continuity for existing config.json installs.
    An unknown provider (or an absent / empty file value) resolves to ``config_value``.

    There is NO env/keychain tier -- the OS-keychain userConfig model was retired (it
    was global-per-plugin, so per-project tokens were impossible).
    """
    val = _read_provider_token_file(provider)
    if val:
        return val
    return config_value


def _provider_tokens_path():
    """Path to the per-project provider-token file, resolved fresh from
    get_project_root() at CALL time (NOT an import-time constant) so the hook seeder
    and the MCP-server reader resolve the SAME file even if one imported shared_state
    before CLAUDE_PROJECT_DIR was set (mirrors _config_session_flag_path)."""
    return get_project_root() / ".claude" / "state" / "provider-tokens.json"


def _read_provider_token_file(provider):
    """Read one provider's token from the per-project token file.

    Tries the provider-name key first (the user-authored format), then the legacy
    ``CLAUDE_PLUGIN_OPTION_<KEY>`` env-name key (bridge files written by older
    versions). Returns the value or None if the file is absent / unreadable /
    malformed / non-dict or the value is missing or empty. Best-effort -- never
    raises. A plain file read (NOT a Claude tool), so the sessions-enforce
    protected-path guard on .claude/state/ does not apply; this is how the MCP
    server (which does not inherit userConfig) reads the token.
    """
    try:
        path = _provider_tokens_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in (provider, _PROVIDER_TOKEN_ENV.get(provider)):
                if not key:
                    continue
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
    except (OSError, ValueError):
        pass
    return None


def _chmod_0600(path):
    """Tighten a secret file to owner-only on POSIX; best-effort (swallow OSError,
    no-op on Windows)."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def ensure_provider_tokens_file():
    """Create-if-absent the per-project provider-token TEMPLATE the user fills in.

    The token file is per-project and user-authored (the AI cannot read it). This
    seeder writes a template with every known provider key EMPTY plus a `_comment`
    explaining the file, so the user just fills in the tokens they use. It is strictly
    create-if-absent: an existing file (populated OR user-created-empty OR even
    malformed) is NEVER overwritten or deleted -- the retired bridge writer
    rewrote/deleted the file every session, which would wipe a hand-authored file now
    that no env tokens exist to repopulate it.

    On an already-existing (user-authored) file it re-applies `chmod 0600` without
    touching contents — the documented flow lets users create the file themselves and
    nothing else tightens a hand-created file's permissions (codex review P2).
    `.claude/` is git-ignored on creation here, and unconditionally by session-start
    every plugin session, so the secret is never committable. The file is 0600 (POSIX).

    Called from SessionStart (plugin mode) and the config_update MCP tool. Returns
    True on success (including the no-op when the file already exists), else False.
    Best-effort: any error is written once to stderr and swallowed.
    """
    try:
        path = _provider_tokens_path()
        if path.exists():
            # Never overwrite a user-authored file's CONTENTS, but still enforce the
            # owner-only (0600) invariant on it -- the documented flow lets users
            # create the file themselves, and nothing else tightens a hand-created
            # file's permissions (codex review P2). `.claude/` git-ignoring is owned
            # by session-start, which runs unconditionally on EVERY plugin session
            # BEFORE config_update, so re-ensuring it here would be redundant.
            _chmod_0600(path)
            return True
        ensure_claude_dir_gitignored(get_project_root())
        template = {
            "_comment": (
                "Provider API tokens for THIS project only. Claude (the AI) cannot "
                "read this file -- it is git-ignored and access-blocked. Fill in only "
                "the tokens you use; leave the rest empty. Keys: gitlab, jira, github "
                "(also Gitea), telegram."
            ),
        }
        for provider in _PROVIDER_TOKEN_ENV:
            template[provider] = ""
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_durable(path, template)
        _chmod_0600(path)
        return True
    except Exception as exc:
        sys.stderr.write(
            f"[team-management] could not seed provider-tokens.json: {exc}\n"
        )
        return False


def ensure_claude_dir_gitignored(project_root):
    """Ensure the whole `.claude/` directory is git-ignored (best-effort).

    `.claude/` holds per-machine, runtime, and system files -- `state/` (incl. the
    provider-token file), `logs/`, `settings.local.json` (the ABSOLUTE statusline
    path pinned by session-start's `_ensure_statusline_pinned`), `plugins/`, ... --
    none of which should ever be tracked. The agent cannot edit `.gitignore` during
    `/team-management:init` (it is NOT on the DAIC admin whitelist in
    sessions-enforce.py), so a hook / the MCP server -- which run OUTSIDE DAIC
    enforcement -- ensure the ignore entry. Idempotent: a no-op when a blanket
    `.claude/`-style rule is already present. Migrates the legacy narrow
    `.claude/settings.local.json` line (from before the dir was ignored wholesale) by
    dropping it when `.claude/` is added. Best-effort, never raises.

    Extracted from session-start.py so `ensure_provider_tokens_file` (also called from
    the config_update MCP tool, which does not otherwise ensure the ignore) can reuse
    the single implementation.
    """
    try:
        entry = '.claude/'
        # Lines that already ignore the WHOLE `.claude/` dir. The legacy narrow
        # `.claude/settings.local.json` line does NOT count as coverage.
        covering = {'.claude/', '/.claude/', '.claude', '/.claude'}
        # Only the `.claude/`-SCOPED narrow lines are migrated. A bare
        # `settings.local.json` is a GENERIC gitignore pattern (the user's own rule)
        # and must NOT be removed.
        legacy = {'.claude/settings.local.json', '/.claude/settings.local.json'}
        gi = project_root / '.gitignore'
        existing = ''
        if gi.exists():
            try:
                existing = gi.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                return  # cannot read -- do not risk clobbering
        # rstrip (git strips trailing ws but keeps leading); skip comments.
        nonblank = [ln.rstrip() for ln in existing.splitlines()
                    if ln.rstrip() and not ln.rstrip().startswith('#')]
        if any(s in covering for s in nonblank):
            return  # whole `.claude/` already ignored
        kept = [ln for ln in existing.splitlines() if ln.rstrip() not in legacy]
        out = '\n'.join(kept)
        sep = '' if (not out or out.endswith('\n')) else '\n'
        gi.write_text(f'{out}{sep}{entry}\n', encoding='utf-8')
    except Exception:
        return  # best-effort; never break the caller


def ensure_agents_dir_gitignored(project_root):
    """Ensure the whole `.agents/` directory is git-ignored (best-effort).

    `.agents/` holds the agy read-only deny-gate the plugin deploys
    (`agy-readonly-gate.py` + a merge-aware `hooks.json`) whenever agy is an
    enabled provider. It is plugin-generated runtime config, redeployed
    (refresh-on-change) on every session-start, so it should never be tracked --
    same rationale as `.claude/` (see `ensure_claude_dir_gitignored`). Unlike
    `.claude/`, `.agents/hooks.json` is merge-aware and MAY carry a user's own agy
    hooks; ignoring the whole dir hides those too (an accepted trade-off, chosen
    to match the `.claude/` model). The agent cannot edit `.gitignore` during a
    protocol (not on the DAIC admin whitelist), so this runs from a hook / the MCP
    server, which are OUTSIDE DAIC enforcement. Idempotent: a no-op when a blanket
    `.agents/`-style rule is already present. No legacy migration (`.agents/` has
    no legacy narrow variants). Best-effort, never raises.

    Known limitation (same as `ensure_claude_dir_gitignored`): a `.gitignore`
    entry does NOT untrack files already committed -- a project that committed
    `.agents/` before this fix must `git rm --cached` it manually.
    """
    try:
        entry = '.agents/'
        # Lines that already ignore the WHOLE `.agents/` dir (all four spellings).
        covering = {'.agents/', '/.agents/', '.agents', '/.agents'}
        gi = Path(project_root) / '.gitignore'
        existing = ''
        if gi.exists():
            try:
                existing = gi.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                return  # cannot read -- do not risk clobbering
        # rstrip (git strips trailing ws but keeps leading); skip comments.
        nonblank = [ln.rstrip() for ln in existing.splitlines()
                    if ln.rstrip() and not ln.rstrip().startswith('#')]
        if any(s in covering for s in nonblank):
            return  # whole `.agents/` already ignored
        sep = '' if (not existing or existing.endswith('\n')) else '\n'
        gi.write_text(f'{existing}{sep}{entry}\n', encoding='utf-8')
    except Exception:
        return  # best-effort; never break the caller


# Config-session intent-gate (m-config-mcp-flow): the `/team-management:config`
# command is the ONLY thing allowed to mutate config.json, and only the
# deterministic `config_intent_gate.py` hook (outside the LLM) writes this flag.
# `config_update` (MCP) refuses to write unless the flag is live. Hard gate =
# TTL + existence; session_id is recorded and enforced best-effort (the MCP
# server cannot always obtain its own session id — see the task SC). The flag
# lives under .claude/state/ which is PROTECTED (SEC-006), so the LLM cannot
# self-author it. Path is computed at CALL time (not an import-time constant) so
# the hook process and the MCP server process resolve the SAME file even if one
# imported shared_state before CLAUDE_PROJECT_DIR was set.
CONFIG_SESSION_TTL_SECONDS = 900  # 15 minutes


def _config_session_flag_path():
    """Path to the config-session flag, resolved fresh from get_project_root()."""
    return get_project_root() / ".claude" / "state" / "config-session.flag"


def write_config_session_flag(session_id=None, ttl_seconds=CONFIG_SESSION_TTL_SECONDS,
                              preserve_session_id=False):
    """Write/refresh the config-session flag with an absolute expiry.

    `session_id` is recorded for best-effort session binding. `preserve_session_id`
    (used by `config_update`'s TTL refresh) keeps the existing flag's session_id
    instead of overwriting it with None — a naive rewrite would null the
    hook-written id and make every later write session-agnostic.
    """
    path = _config_session_flag_path()
    sid = session_id
    if preserve_session_id:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("session_id") is not None:
                sid = existing.get("session_id")
        except (OSError, ValueError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_durable(path, {"expires_at": time.time() + ttl_seconds, "session_id": sid})


def check_config_session_flag(session_id=None):
    """Return (ok: bool, reason: str) for the config-session intent-gate.

    Hard checks: the flag exists and has not expired. Session binding is
    enforced ONLY when both the flag and the caller carry a session_id and they
    differ — the MCP server often cannot supply one, so it stays best-effort.
    """
    path = _config_session_flag_path()
    if not path.exists():
        return False, "no active config session — run /team-management:config first"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "config-session flag is unreadable — run /team-management:config again"
    if not isinstance(data, dict) or time.time() >= data.get("expires_at", 0):
        return False, "config session expired — run /team-management:config again"
    flag_sid = data.get("session_id")
    if session_id and flag_sid and session_id != flag_sid:
        return False, "config session belongs to a different session"
    return True, "ok"


PROJECT_ROOT = get_project_root()
# NB: there is intentionally no module-level PLUGIN_ROOT constant — it would be
# import-time-fixed and ignore CLAUDE_PLUGIN_ROOT set/changed later (and in tests).
# Always call get_plugin_root() at the point of use so the env is respected.

# All state files in .claude/state/
STATE_DIR = PROJECT_ROOT / ".claude" / "state"
TASK_STATES_DIR = STATE_DIR / "tasks"  # Per-task state directories
DAIC_STATE_FILE = STATE_DIR / "daic-mode.json"
TASK_STATE_FILE = STATE_DIR / "current_task.json"
TASK_STATE_LOCK_FILE = STATE_DIR / "current_task.lock"
WORKFLOW_BYPASS_FILE = STATE_DIR / "workflow-bypass.json"
OPTIMIZE_STATE_FILE = STATE_DIR / "optimize-state.json"
SUBAGENT_DEPTH_FILE = STATE_DIR / "subagent-depth.json"

# Bounded tail-read window for session transcript JSONL. The statusline runs on
# every prompt and the transcript grows to tens of MB in long sessions; reading
# only the last TRANSCRIPT_TAIL_BYTES keeps transcript reads flat w.r.t. file
# size (see read_last_jsonl_entry / get_context_length_from_transcript). 1 MB is
# far larger than the distance from EOF to the newest main-chain usage entry in
# practice, so the bounded read stays correct while never scanning the prefix.
TRANSCRIPT_TAIL_BYTES = 1_048_576

# task-transcript-link.py stages the parent transcript for a dispatched subagent
# on every Task/Agent PreToolUse. Cap the staged tail so the blocking hook's
# tiktoken encode + RSS stay flat w.r.t. session length; the chunk consumer
# already tolerates partial context (m-fix-unbounded-transcript-reads).
TRANSCRIPT_STAGE_CAP_BYTES = 1_048_576  # 1 MB

# Mode description strings
DISCUSSION_MODE_MSG = "You are now in Discussion Mode and should focus on discussing and investigating with the user (no edit-based tools)"
IMPLEMENTATION_MODE_MSG = "You are now in Implementation Mode and may use tools to execute the agreed upon actions - when you are done return immediately to Discussion Mode"
DOCUMENTATION_MODE_MSG = "You are now in Documentation Mode and may edit documentation files only (CLAUDE.md, task files, docs/) - source code edits are blocked"

def ensure_state_dir():
    """Ensure the state directory exists."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

def get_provider_logger(log_name: str):
    """Return a logger fn appending timestamped lines to <project_root>/.claude/logs/<log_name>.

    Keyed off the project root (PROJECT_ROOT), NOT Path.cwd(), so provider logs
    land in the project's .claude/logs regardless of the process working directory.
    Best-effort: write failures are swallowed so logging never breaks a workflow.
    """
    def log(message: str):
        try:
            logs_dir = PROJECT_ROOT / '.claude' / 'logs'
            logs_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().isoformat(timespec='seconds')
            with open(logs_dir / log_name, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass
    return log

def _write_json_durable(path, data, **kwargs):
    """Atomically write JSON via tempfile + rename with fsync.

    Readers always see either the old or the new content — never a
    partial/truncated write. os.replace is atomic on POSIX and Windows.
    """
    kwargs.setdefault('indent', 2)
    path = Path(path)
    # Unique temp name (mkstemp) — a deterministic f"{path}.tmp" would be
    # shared by concurrent writers of files NOT serialised by _state_lock
    # (daic-mode.json, optimize-state.json, workflow-bypass.json): os.replace
    # keeps the final file consistent but silently drops the loser's update.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, **kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def write_optimize_state(state: dict) -> None:
    """Atomically write .claude/state/optimize-state.json.

    T1-owned (h-optimize-frozen-paths-enforcement): the only writer T1
    introduces in shared_state.py. T2 must not edit this function.
    """
    ensure_state_dir()
    _write_json_durable(OPTIMIZE_STATE_FILE, state)


def _increment_counter_unlocked(path) -> int:
    """Read-increment-write a single-int counter WITHOUT the lock. A missing,
    unreadable, or corrupt file counts as 0; write failures are swallowed. Used
    both inside the lock and as the degraded fallback when the lock cannot be
    acquired (m-fix-posttooluse-lock-failure-resilience)."""
    try:
        value = int(path.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        value = 0
    value += 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding='utf-8')
    except OSError:
        pass
    return value


def _reset_counter_unlocked(path) -> None:
    """Write '0' to a single-int counter WITHOUT the lock; write failures
    swallowed. See _increment_counter_unlocked."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('0', encoding='utf-8')
    except OSError:
        pass


def increment_counter_file(path) -> int:
    """Locked increment of a single-int counter file. Returns the new value.

    R2-6: concurrent PostToolUse hooks doing bare read_text/write_text
    lose-update each other; the lock serialises the read-modify-write.
    A missing, unreadable, or corrupt file counts as 0 before the increment.
    Write failures are swallowed (throttle counters must never break a hook).

    Lock resilience (m-fix-posttooluse-lock-failure-resilience): a throttle
    counter must NEVER break a hook. If `_state_lock()` cannot be ACQUIRED
    (OSError: no flock support on a network/synced FS, permissions, or msvcrt
    contention) we degrade to a single unlocked best-effort increment. An OSError
    raised on lock RELEASE/close AFTER the body already ran (the `f.close()` in
    `_state_lock` is outside its inner try) is swallowed — the increment already
    persisted, so re-running it would double-count. The `entered` sentinel
    distinguishes acquisition failure from post-body release failure.
    """
    path = Path(path)
    entered = False
    try:
        with _state_lock():
            value = _increment_counter_unlocked(path)
            entered = True  # set LAST — `entered` True ⟹ `value` is bound, so the
                            # except branch can safely return it (no UnboundLocalError)
        return value
    except OSError:
        if entered:
            return value  # body completed under the lock; only release/close failed
        return _increment_counter_unlocked(path)  # acquisition failed — degrade


def reset_counter_file(path) -> None:
    """Locked reset of a single-int counter file to 0. See increment_counter_file
    for the lock-resilience contract — on acquisition failure it degrades to an
    unlocked reset; an OSError on release/close after the reset ran is swallowed
    (m-fix-posttooluse-lock-failure-resilience)."""
    path = Path(path)
    entered = False
    try:
        with _state_lock():
            _reset_counter_unlocked(path)
            entered = True  # set LAST (see increment_counter_file)
    except OSError:
        if not entered:
            _reset_counter_unlocked(path)  # acquisition failed — degrade


if sys.platform == 'win32':
    import msvcrt

    @contextmanager
    def _state_lock():
        """Exclusive cross-process lock for current_task.json read-modify-write.

        Serialises concurrent writers so updates don't clobber each other
        (e.g. set_task_state vs set_protocol_state racing from separate
        hook / MCP processes).
        """
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        f = open(TASK_STATE_LOCK_FILE, 'a+')
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            f.close()
else:
    import fcntl

    @contextmanager
    def _state_lock():
        """Exclusive cross-process lock for current_task.json read-modify-write.

        Serialises concurrent writers so updates don't clobber each other
        (e.g. set_task_state vs set_protocol_state racing from separate
        hook / MCP processes).
        """
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        f = open(TASK_STATE_LOCK_FILE, 'a+')
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            f.close()

# ---------------------------------------------------------------------------
# Subagent-context depth counter
# ---------------------------------------------------------------------------
# Tracks how many subagent (Task) contexts are currently active. Replaces the
# old single-boolean flag (.claude/state/in_subagent_context.flag), which broke
# under parallel subagents — the first to finish cleared the flag while siblings
# were still running — and went stale across crashes. Hooks read
# in_subagent_context() to suppress DAIC enforcement and reminder/auto-sync
# injection for subagent tool calls.
#
# increment on Task PreToolUse (task-transcript-link.py), decrement on Task
# PostToolUse (post-tool-use.py), hard reset on UserPromptSubmit (user-messages.py)
# and SessionStart (session-start.py). read_subagent_depth() is intentionally
# lock-free so the mutators can call it inside the (non-reentrant) _state_lock()
# critical section without re-acquiring the lock and deadlocking.

def read_subagent_depth() -> int:
    """Current subagent nesting depth (0 == main agent context).

    Lock-free and tolerant: a missing, corrupt, or non-integer counter file all
    read as 0, and negative values are clamped up to 0.
    """
    try:
        with open(SUBAGENT_DEPTH_FILE, 'r', encoding='utf-8') as f:
            return max(0, int(json.load(f).get("depth", 0)))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError, OSError):
        return 0

def in_subagent_context() -> bool:
    """True when at least one subagent (Task) context is currently active."""
    return read_subagent_depth() > 0

def _mutate_subagent_depth(new_depth_fn) -> int:
    """Compute + persist the subagent depth under _state_lock, degrading to a
    best-effort UNLOCKED write on lock-ACQUISITION failure so a context-detection
    counter never breaks a hook (m-fix-posttooluse-lock-failure-resilience).
    `new_depth_fn(current) -> new`.

    Lock resilience mirrors increment_counter_file. `entered` is set as the LAST
    statement in the lock body, so `entered is True` guarantees `n` is bound and
    the write was attempted. Three OSError paths:
    - acquisition failure (`entered` stays False) → single UNLOCKED best-effort
      write (the FS is writable but flock-less, so the unlocked write succeeds);
    - the locked `_write_json_durable` itself fails → swallowed in place
      (best-effort, consistent with the counter helper's internal swallow) — NOT
      retried unlocked, because `_state_lock` already `mkdir`s STATE_DIR so a
      locked-write OSError is a genuine I/O error (disk full / fsync) the unlocked
      retry to the same dir cannot fix; depth stays at its prior on-disk value;
    - lock RELEASE/close failure after the write → return `n` (write already
      attempted); no unlocked retry, which could stale-overwrite a concurrent
      writer.

    Degraded-semantics residual: on a genuine write failure the depth is not
    updated for that one invocation — subagent-context suppression can be briefly
    lost. Bounded by the turn/session depth resets (UserPromptSubmit /
    SessionStart). Protected-state INTEGRITY writers (set_task_state /
    set_protocol_state / set_daic_mode / edit_state) deliberately do NOT degrade —
    they keep raising (fail-closed).
    """
    entered = False
    try:
        with _state_lock():
            n = new_depth_fn(read_subagent_depth())
            try:
                _write_json_durable(SUBAGENT_DEPTH_FILE, {"depth": n})
            except OSError:
                pass  # locked write failed — best-effort swallow (depth stays stale)
            entered = True  # set LAST — `entered` True ⟹ `n` bound + write attempted
        return n
    except OSError:
        if entered:
            return n  # only lock release/close failed; the write already ran
    # Lock ACQUISITION failed — best-effort UNLOCKED write.
    n = new_depth_fn(read_subagent_depth())
    try:
        ensure_state_dir()
        _write_json_durable(SUBAGENT_DEPTH_FILE, {"depth": n})
    except OSError:
        pass
    return n

def increment_subagent_depth() -> int:
    """Increment the depth on Task PreToolUse. Returns the new depth. Degrades on
    lock-acquisition failure — see _mutate_subagent_depth."""
    return _mutate_subagent_depth(lambda cur: cur + 1)

def decrement_subagent_depth() -> int:
    """Decrement the depth on Task PostToolUse, clamped at 0. Returns new depth.
    Degrades on lock-acquisition failure — see _mutate_subagent_depth."""
    return _mutate_subagent_depth(lambda cur: max(0, cur - 1))

def reset_subagent_depth() -> None:
    """Force the depth to 0 at a turn/session boundary (UserPromptSubmit,
    SessionStart). The main agent is unambiguously in control at those points,
    so 0 is always correct — this self-heals any leak left by a hard-interrupted
    or denied subagent whose decrement never fired. Degrades on lock-acquisition
    failure — see _mutate_subagent_depth.
    """
    _mutate_subagent_depth(lambda cur: 0)

def subagent_dir_name(tool_input) -> str:
    """Filesystem-safe `subagent_type` path segment for transcript staging dirs.

    Sanitises to ``[A-Za-z0-9_-]``; a missing / None / empty / fully-stripped
    value falls back to ``"shared"``. Both task-transcript-link.py and
    post-tool-use.py call this so they (a) agree on the directory and (b) can
    never let a crafted ``subagent_type`` (e.g. ``"../.."``) escape
    ``.claude/state/<type>/`` — important now that post-tool-use.py runs
    ``shutil.rmtree`` on the keyed staging dir. Also avoids the ``Path / None``
    TypeError when the key is present but null.
    """
    raw = ""
    if isinstance(tool_input, dict):
        raw = tool_input.get("subagent_type") or ""
    cleaned = "".join(c for c in str(raw) if (c.isascii() and c.isalnum()) or c in ("_", "-"))
    return cleaned or "shared"

def subagent_transcript_key(tool_input) -> str:
    """Deterministic per-Task-invocation key for isolating subagent transcript
    staging directories.

    The `Task` tool_input is byte-identical in the PreToolUse and PostToolUse
    payloads for the same call, so task-transcript-link.py (which stages chunks)
    and post-tool-use.py (which archives them) derive the same key and agree on
    the directory — without it, parallel subagents of the SAME type would share
    one staging dir and clobber each other's chunks. Falls back to repr() for a
    non-JSON-serializable tool_input so it never raises.
    """
    try:
        blob = json.dumps(tool_input or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(tool_input)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

def check_daic_mode_raw() -> str:
    """Read raw DAIC mode string from state file. Returns 'discussion', 'implementation', or 'documentation'.

    Degrades to 'discussion' (the restrictive default — blocks edit tools) on ANY
    read failure: missing / corrupt-bytes (UnicodeDecodeError ⊂ ValueError) / non-dict
    state, or an ensure_state_dir mkdir OSError. ensure_state_dir runs INSIDE the try
    so its mkdir cannot escape. A raise here would exit 1 from the PreToolUse
    enforcement hook, silently disabling DAIC (h-fix-daic-enforcement-fail-open).
    """
    try:
        ensure_state_dir()
        with open(DAIC_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("mode", "discussion")
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        pass
    return "discussion"

def set_daic_mode(value: str|bool):
    """Set DAIC mode to a specific value."""
    ensure_state_dir()
    if value == True or value == "discussion":
        mode = "discussion"
        name = "Discussion Mode"
    elif value == False or value == "implementation":
        mode = "implementation"
        name = "Implementation Mode"
    elif value == "documentation":
        mode = "documentation"
        name = "Documentation Mode"
    else:
        raise ValueError(f"Invalid mode value: {value}")

    # R2-4: serialise concurrent daic-mode writers (session-start restore vs
    # MCP protocol engine switching mode) — same lock as current_task.json.
    with _state_lock():
        _write_json_durable(DAIC_STATE_FILE, {"mode": mode})
    return name

def ensure_discussion_mode_best_effort() -> None:
    """Best-effort force of global DAIC *discussion* mode for the two fail-SAFE
    hook call sites — the SessionStart no-protocol fallback and the emergency
    STOP (m-fix-posttooluse-lock-failure-resilience).

    NEVER raises (must not crash the hook). Unlike set_daic_mode — an integrity
    writer that keeps raising on a _state_lock acquisition failure — this degrades
    to an UNLOCKED write of discussion mode when the lock cannot be acquired,
    rather than leaving a stale `"implementation"` mode active (which would make a
    no-protocol session, or an emergency STOP, silently run with edits allowed on
    a flock-less FS). The unlocked degrade is deliberately NARROW and SAFE: it
    only ever writes the RESTRICTIVE `"discussion"` mode, so it can only tighten
    enforcement, never open a hole — it is NOT a general set_daic_mode degrade
    (which could write `"implementation"`). Non-transactional by nature (the lock
    is unavailable), but discussion is the safe direction, so a race can only land
    on discussion or a concurrent writer's value, never a spurious loosening.
    """
    try:
        set_daic_mode("discussion")
        return
    except Exception:
        pass
    # Lock unavailable (or set_daic_mode otherwise failed) — write the restrictive
    # mode UNLOCKED, best-effort. Only ever "discussion", so this cannot fail open.
    try:
        ensure_state_dir()
        _write_json_durable(DAIC_STATE_FILE, {"mode": "discussion"})
    except Exception:
        pass

# ============================================================================
# WORKFLOW BYPASS STATE MANAGEMENT
# ============================================================================

# Bypass mode description strings
BYPASS_ENABLED_MSG = "Workflow bypass is ENABLED. All DAIC enforcement is bypassed. Hooks run but skip enforcement."
BYPASS_DISABLED_MSG = "Workflow bypass is DISABLED. Normal DAIC enforcement is active."

def check_workflow_bypass() -> bool:
    """Check if workflow bypass mode is enabled. Returns True if bypassed.

    Degrades to False (not bypassed — the restrictive default that keeps enforcement
    ON) on ANY read failure: missing / corrupt-bytes / non-dict state, or an
    ensure_state_dir mkdir OSError (moved INSIDE the try). A raise here would exit 1
    from the PreToolUse enforcement hook (h-fix-daic-enforcement-fail-open).
    """
    try:
        ensure_state_dir()
        with open(WORKFLOW_BYPASS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("enabled", False)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        pass
    # Default to not bypassed if the file is absent / unreadable / malformed
    return False

def set_workflow_bypass(enabled: bool, reason: str = None):
    """Set workflow bypass state."""
    ensure_state_dir()
    data = {
        "enabled": enabled,
        "reason": reason,
        "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    }
    _write_json_durable(WORKFLOW_BYPASS_FILE, data)
    return BYPASS_ENABLED_MSG if enabled else BYPASS_DISABLED_MSG

def toggle_workflow_bypass() -> str:
    """Toggle workflow bypass and return new state message."""
    current = check_workflow_bypass()
    return set_workflow_bypass(not current)

def get_workflow_bypass_state() -> dict:
    """Get full workflow bypass state including reason and timestamp."""
    ensure_state_dir()
    try:
        with open(WORKFLOW_BYPASS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"enabled": False, "reason": None, "updated": None}

# Task and branch state management
def get_task_state() -> dict:
    """Get current task state including branch and affected services.

    Degrades to a null-task dict (the restrictive default — branch enforcement's
    fail-safe then blocks edits) on ANY read failure: missing / corrupt-bytes state,
    or valid-but-non-dict JSON (null / list / scalar). A raise here would exit 1 from
    the PreToolUse enforcement hook (h-fix-daic-enforcement-fail-open).
    """
    try:
        with open(TASK_STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        # isinstance covers the historical `state is None` case plus any other
        # valid-but-non-dict JSON that would crash a later `.get()` caller.
        if isinstance(state, dict):
            return state
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        pass
    return {"task": None, "branch": None, "services": [], "updated": None}

def set_task_state(task: str, branch: str, services: list):
    """Set current task state, preserving extra fields (e.g. protocol).

    Atomic across processes: lock → read → modify → atomic-rename write.
    """
    ensure_state_dir()
    with _state_lock():
        state = get_task_state()
        state["task"] = task
        state["branch"] = branch
        state["services"] = services
        state["updated"] = datetime.now().strftime("%Y-%m-%d")
        _write_json_durable(TASK_STATE_FILE, state)
    return state

# ============================================================================
# ENUMS FOR STATUSLINE SUPPORT
# ============================================================================

from enum import Enum

class Model(str, Enum):
    """Model types for context limit detection."""
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"
    UNKNOWN = "unknown"

class Mode(str, Enum):
    """DAIC mode states."""
    NO = "discussion"      # Discussion mode (NO editing)
    GO = "implementation"  # Implementation mode (GO ahead)
    DOC = "documentation"  # Documentation mode (docs-only edits)

class IconStyle(str, Enum):
    """Icon display styles for statusline."""
    NERD_FONTS = "nerd_fonts"
    EMOJI = "emoji"
    ASCII = "ascii"

# ============================================================================
# GIT UTILITIES
# ============================================================================

def find_git_repo(dir_path: Path) -> Path:
    """Walk up directory tree to find .git directory.

    Args:
        dir_path: Directory to start search from (NOT a file path)

    Returns:
        Path to git repository root, or None if not found
    """
    if not isinstance(dir_path, Path):
        dir_path = Path(dir_path)

    current = dir_path
    while True:
        if (current / '.git').exists():
            return current
        if current == PROJECT_ROOT or current.parent == current:
            break
        current = current.parent
    return None

# ============================================================================
# STATE MANAGEMENT (for statusline compatibility)
# ============================================================================

def load_state():
    """Load DAIC mode state for statusline.

    Returns a simple object with mode attribute for backward compatibility
    with original cc-sessions statusline.
    """
    class StateObj:
        def __init__(self, mode_str):
            if mode_str == "implementation":
                self.mode = Mode.GO
            elif mode_str == "documentation":
                self.mode = Mode.DOC
            else:
                self.mode = Mode.NO
            self.model = Model.UNKNOWN  # Default model
            self.current_task = type('obj', (object,), {
                'name': get_task_state().get('task')
            })()

    try:
        with open(DAIC_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            mode = data.get("mode", "discussion")
            return StateObj(mode)
    except (FileNotFoundError, json.JSONDecodeError):
        return StateObj("discussion")

@contextmanager
def edit_state():
    """Context manager for editing state (simplified version).

    Yields a state object that can be modified. Changes are saved on exit.
    """
    state = load_state()

    try:
        yield state
    finally:
        # Save mode if it was changed
        if state.mode == Mode.GO:
            mode_str = "implementation"
        elif state.mode == Mode.DOC:
            mode_str = "documentation"
        else:
            mode_str = "discussion"
        ensure_state_dir()
        # R2-4: same lock as set_daic_mode — daic-mode.json writers serialise.
        with _state_lock():
            _write_json_durable(DAIC_STATE_FILE, {"mode": mode_str})

# ============================================================================
# TASK FRONTMATTER PARSING
# ============================================================================

def parse_task_frontmatter(task_name: str) -> dict:
    """Parse task file frontmatter to get branch requirement and other metadata.

    Args:
        task_name: Task name (e.g., 'm-fix-statusline-tokens-and-mcp')

    Returns:
        dict with frontmatter fields (branch, status, etc.), or empty dict if not found
    """
    if not task_name:
        return {}

    # Try both file and directory task formats
    tasks_dir = PROJECT_ROOT / "team-management" / "tasks"
    task_file = tasks_dir / f"{task_name}.md"

    # If not found as file, try as directory with README.md
    if not task_file.exists():
        task_file = tasks_dir / task_name / "README.md"

    if not task_file.exists():
        return {}

    try:
        with open(task_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # Parse frontmatter (between --- markers)
        if not content.startswith('---'):
            return {}

        # Find end of frontmatter
        end_marker = content.find('---', 3)
        if end_marker == -1:
            return {}

        frontmatter = content[3:end_marker].strip()

        # Parse frontmatter fields
        result = {}
        for line in frontmatter.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                result[key.strip()] = value.strip()

        return result
    except (FileNotFoundError, IOError, ValueError):
        return {}

def infer_task_from_branch(branch_name: str) -> str:
    """Infer task name from git branch name.

    Branch patterns:
      fix/workflow-state-clearing → *-fix-workflow-state-clearing
      feature/implement-auto-detect → *-implement-auto-detect

    Args:
        branch_name: Git branch name (e.g., "fix/workflow-state-clearing")

    Returns:
        Task name if found, None otherwise
    """
    if not branch_name:
        return None

    # Optimize tasks use a distinct prefix scheme: branch optimize/<name>
    # maps directly to task o-<name> with no priority prefix (the o- IS the
    # prefix). T1-owned (h-optimize-frozen-paths-enforcement); T2 must not edit.
    if branch_name.startswith("optimize/"):
        name_part = branch_name[len("optimize/"):]
        if not name_part:
            return None
        task_name = f"o-{name_part}"
        tasks_dir = PROJECT_ROOT / "team-management" / "tasks"
        if (tasks_dir / f"{task_name}.md").exists():
            return task_name
        if (tasks_dir / task_name / "README.md").exists():
            return task_name
        return None

    # Brainstorm tasks use a distinct prefix scheme: branch brainstorm/<name>
    # maps to task b-brainstorm-<name>. The b- IS the priority prefix; the
    # "brainstorm-" infix is part of the task name body (mirrors the optimize/
    # pattern above, with an additional fixed infix).
    if branch_name.startswith("brainstorm/"):
        name_part = branch_name[len("brainstorm/"):]
        if not name_part:
            return None
        task_name = f"b-brainstorm-{name_part}"
        tasks_dir = PROJECT_ROOT / "team-management" / "tasks"
        if (tasks_dir / f"{task_name}.md").exists():
            return task_name
        if (tasks_dir / task_name / "README.md").exists():
            return task_name
        return None

    # Branch prefix to task action mapping
    branch_to_action = {
        "fix/": "fix-",
        "feature/": ["implement-", "refactor-", "migrate-", "test-", "docs-"],
        "bugfix/": "fix-",
        "hotfix/": "fix-",
    }

    # Extract branch prefix and name
    for branch_prefix, actions in branch_to_action.items():
        if branch_name.startswith(branch_prefix):
            name_part = branch_name[len(branch_prefix):]

            # Normalize: convert string to list for uniform handling
            if isinstance(actions, str):
                actions = [actions]

            # Look for matching task files
            task_dirs = [
                PROJECT_ROOT / "team-management" / "tasks",
            ]

            # For feature/ branches, check if name_part already starts with an action
            # If so, search directly without adding action prefix again
            for action in actions:
                if name_part.startswith(action):
                    # Name already has action, search for {priority}{name_part}
                    for priority in ["h-", "m-", "l-", "r-", "o-", "b-"]:
                        task_name = f"{priority}{name_part}"
                        for tasks_dir in task_dirs:
                            task_file = tasks_dir / f"{task_name}.md"

                            if task_file.exists():
                                return task_name

                            # Also check directory format
                            task_dir = tasks_dir / task_name
                            if (task_dir / "README.md").exists():
                                return task_name
                    break  # Found matching action, don't try others

            # If no action matched, try adding action prefix (for short branch names)
            for action in actions:
                for priority in ["h-", "m-", "l-", "r-", "o-", "b-"]:
                    task_name = f"{priority}{action}{name_part}"
                    for tasks_dir in task_dirs:
                        task_file = tasks_dir / f"{task_name}.md"

                        if task_file.exists():
                            return task_name

                        # Also check directory format
                        task_dir = tasks_dir / task_name
                        if (task_dir / "README.md").exists():
                            return task_name

            # Fallback: try direct match without action prefix
            # e.g., feature/annotation-list-panel → m-annotation-list-panel
            for priority in ["h-", "m-", "l-", "r-", "o-", "b-"]:
                task_name = f"{priority}{name_part}"
                for tasks_dir in task_dirs:
                    task_file = tasks_dir / f"{task_name}.md"

                    if task_file.exists():
                        return task_name

                    # Also check directory format
                    task_dir = tasks_dir / task_name
                    if (task_dir / "README.md").exists():
                        return task_name

            break

    return None

# ============================================================================
# CONFIG MANAGEMENT (for statusline compatibility)
# ============================================================================

def load_config():
    """Load config with safe defaults for statusline.

    Returns a simple config object with features.icon_style for statusline.
    Falls back to ASCII if config doesn't exist or lacks icon_style.
    """
    config_file = PROJECT_ROOT / "team-management" / "config.json"

    # Create default config object
    class ConfigObj:
        def __init__(self, icon_style=IconStyle.ASCII, project_name=None):
            self.features = type('obj', (object,), {
                'icon_style': icon_style
            })()
            # Optional statusline project label; None means "fall back to the
            # project folder name" (resolved by the caller). Only a non-empty
            # string survives; whitespace-only / non-str is treated as unset.
            self.project_name = project_name

    # Try to load actual config
    if config_file.exists():
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # A valid-but-non-dict JSON root (list / string / number) has no
            # .get() we rely on below; fall back to defaults instead of raising
            # past the narrow except clause and breaking statusline rendering.
            if not isinstance(data, dict):
                return ConfigObj()

            # Check for icon_style in various possible locations
            icon_style = IconStyle.ASCII  # default - safe for all terminals

            # Try features.icon_style (preferred)
            if 'features' in data:
                if 'icon_style' in data['features']:
                    try:
                        icon_style = IconStyle(data['features']['icon_style'])
                    except (ValueError, KeyError):
                        pass
                # Legacy: features.use_nerd_fonts
                elif 'use_nerd_fonts' in data['features']:
                    icon_style = IconStyle.NERD_FONTS if data['features']['use_nerd_fonts'] else IconStyle.ASCII

            # Optional project_name (statusline label). Keep only a non-empty
            # string; anything else falls back to the folder name downstream.
            project_name = None
            raw_project_name = data.get('project_name')
            if isinstance(raw_project_name, str) and raw_project_name.strip():
                project_name = raw_project_name.strip()

            return ConfigObj(icon_style, project_name)
        except (json.JSONDecodeError, IOError):
            pass

    # Return defaults if config doesn't exist or failed to load
    return ConfigObj()


# ============================================================================
# CONTEXT LIMIT DETECTION UTILITIES
# ============================================================================

def get_model_context_limit(model_display_name: str, model_id: str = "") -> int:
    """
    Detect context limit based on config override, model display name, or model ID.

    Priority:
    1. auto_compact.context_limit from config.json (explicit override)
    2. Model name/ID heuristics ([1m], (1M context), etc.)
    3. Default: 200000

    Args:
        model_display_name: Model display name from Claude API
        model_id: Model ID string (e.g., "claude-opus-4-6[1m]")

    Returns:
        Context limit in tokens
    """
    # 1. Check config override first
    try:
        config_file = PROJECT_ROOT / "team-management" / "config.json"
        if config_file.exists():
            import json as _json
            with open(config_file, 'r', encoding='utf-8') as f:
                _cfg = _json.load(f)
            configured_limit = _cfg.get("auto_compact", {}).get("context_limit")
            if configured_limit and isinstance(configured_limit, (int, float)) and configured_limit > 0:
                return int(configured_limit)
    except Exception:
        pass

    # 2. Heuristic detection from model name/ID
    import re
    combined = f"{model_display_name} {model_id}"
    if re.search(r'\[1[mM]\]|\(1[mM][)\s]|1[mM]\s+context', combined, re.IGNORECASE):
        return 1000000

    # 3. Default for standard models
    return 200000


def _read_file_tail(path, max_bytes=TRANSCRIPT_TAIL_BYTES, return_capped=False):
    """Return the last complete lines of a text file as a list of str, reading at
    most ``max_bytes`` from the END (seek-based; flat runtime w.r.t. file size).

    When the file is larger than ``max_bytes`` the window starts mid-line, so the
    first (partial) line of the window is dropped — callers never see a truncated
    line. Bytes are decoded ``errors='backslashreplace'`` so a multi-byte char
    straddling the seek boundary can't raise. Returns ``[]`` on any error/empty.

    The read is bounded to the size snapshotted on the OPEN descriptor
    (``f.read(size - read_from)``, NOT ``f.read()``), so a concurrent append can
    never make the read exceed the window. When ``return_capped`` is True, returns
    ``(lines, capped)`` where ``capped`` is True iff the file exceeded ``max_bytes``
    (the window started mid-file, so head content — including any work-start
    marker — was dropped). ``capped`` is derived from the SAME descriptor
    observation as the read, so it corresponds exactly to the bytes returned (no
    TOCTOU against a separate ``os.path.getsize``).
    """
    capped = False
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_from = max(0, size - max_bytes)
            capped = read_from > 0  # file exceeded the window -> head was dropped
            # Only drop the window's first line when the seek actually landed
            # MID-line. If the byte just before read_from is a newline, the window
            # starts on a clean line boundary and its first line is complete —
            # dropping it would discard a full in-window entry (and could falsely
            # yield 0). Peek that boundary byte to decide.
            partial_first = False
            if read_from > 0:
                f.seek(read_from - 1)
                partial_first = f.read(1) != b'\n'  # leaves the cursor at read_from
            else:
                f.seek(read_from)
            data = f.read(size - read_from)  # bounded to the snapshotted window
        text = data.decode('utf-8', errors='backslashreplace')
        lines = text.split('\n')
        if partial_first and lines:
            lines = lines[1:]  # drop the partial first line of the window
        return (lines, capped) if return_capped else lines
    except (OSError, ValueError):
        return ([], False) if return_capped else []


def _scan_tail_then_full(path, max_bytes, scan_fn):
    """Reverse-scan the file's tail (last ``max_bytes``) via ``scan_fn(lines) ->
    result | None``. If the bounded tail yields nothing AND the file is larger
    than the window — i.e. a single oversized final line filled the whole window,
    so ``_read_file_tail`` dropped it as the partial first line — fall back to ONE
    full-file scan. Flat runtime in the common case; correct (bounded by file
    size) in the rare oversized-final-record case, still strictly no worse than
    the old unconditional full read. Returns ``scan_fn``'s result or ``None``.
    Shared by read_last_jsonl_entry and get_context_length_from_transcript so both
    handle the oversized-final-record case identically (codex re-review)."""
    result = scan_fn(_read_file_tail(path, max_bytes))
    if result is not None:
        return result
    try:
        if os.path.getsize(path) > max_bytes:
            with open(path, 'r', encoding='utf-8', errors='backslashreplace') as f:
                return scan_fn(f.read().split('\n'))
    except (OSError, ValueError):
        return None
    return None


def _last_dict_in_lines(lines):
    """Return the last non-empty line that parses to a JSON dict, or ``None``."""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def read_last_jsonl_entry(path, max_bytes=TRANSCRIPT_TAIL_BYTES):
    """Return the last non-empty JSONL entry (a dict), or ``None``. Bounded
    tail-read (does not scale with file size) with a full-file fallback for the
    rare case where the final record is itself larger than the tail window (a big
    tool result / paste at EOF) — without it the oversized final line is dropped
    and the statusline's find_current_transcript loses the last timestamp /
    sessionId and mis-detects transcript staleness (codex re-review). Used by
    find_current_transcript instead of readlines()-ing the whole session transcript.
    """
    return _scan_tail_then_full(path, max_bytes, _last_dict_in_lines)


def _newest_usage_in_lines(lines):
    """Reverse-scan JSONL lines for the newest main-chain (non-sidechain) entry
    carrying ``message.usage``; return its total input tokens, or ``None`` if
    none. Transcript JSONL is append-ordered, so the LAST eligible entry in file
    order is the newest by time — no timestamp comparison needed."""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        # Skip sidechain entries (subagent calls) — main-chain only.
        if data.get('isSidechain', False):
            continue
        message = data.get('message')
        usage = message.get('usage') if isinstance(message, dict) else None
        if usage:
            return (
                usage.get('input_tokens', 0) +
                usage.get('cache_read_input_tokens', 0) +
                usage.get('cache_creation_input_tokens', 0)
            )
    return None


def get_context_length_from_transcript(transcript_path: str, max_bytes=TRANSCRIPT_TAIL_BYTES) -> int:
    """
    Get current context length from the most recent main-chain message in transcript.

    Reverse-scans the transcript's tail (last ``max_bytes`` bytes) for the newest
    main-chain (non-sidechain) ``message.usage`` entry. Bounded tail-read: runtime
    is flat w.r.t. transcript size in the common case (the statusline reads this on
    every prompt, and it also feeds the auto-compact monitors in user-messages.py /
    post-tool-use.py — so it must stay correct, not merely fast).

    Fallback for correctness: if the bounded tail holds no eligible entry AND the
    file is larger than the window, do ONE full-file scan. That case only arises
    when a single transcript line at/near EOF is itself larger than ``max_bytes``
    (a big tool result or paste pushes the newest assistant usage outside the
    window) — rare, so the common path stays flat, but the auto-compact monitor
    never blind-spots to 0 the way a tail-only read would (codex + code-review).

    Args:
        transcript_path: Path to the transcript JSONL file
        max_bytes: Tail window size in bytes (default TRANSCRIPT_TAIL_BYTES)

    Returns:
        Total input tokens used (input + cache_read + cache_creation), or 0 if unavailable
    """
    try:
        if not os.path.exists(transcript_path):
            return 0
        result = _scan_tail_then_full(transcript_path, max_bytes, _newest_usage_in_lines)
        if result is not None:
            return result
    except Exception:
        pass
    return 0


def _newest_model_in_lines(lines):
    """Reverse-scan JSONL lines for the newest entry carrying ``message.model``;
    return the model string, or ``None``. Transcript JSONL is append-ordered, so
    the LAST model-bearing entry in file order is the newest. Mirrors
    ``_newest_usage_in_lines`` — skips blank / unparseable / non-dict lines and a
    non-dict ``message``."""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        message = data.get('message')
        model = message.get('model') if isinstance(message, dict) else None
        if model:
            return model
    return None


def get_model_from_transcript(transcript_path: str, max_bytes=TRANSCRIPT_TAIL_BYTES) -> str:
    """Extract the model display name from the transcript's newest model-bearing
    entry, or ``"unknown"``.

    Bounded tail-read: reverse-scans only the last ``max_bytes`` for the newest
    ``message.model`` (flat w.r.t. transcript size — this runs on every
    UserPromptSubmit and in the auto-compact monitor, so it must not scale with
    session length). Falls back to ONE full-file scan only when the bounded tail
    holds no model AND the file exceeds the window (a >``max_bytes`` final record,
    or a long model-less tail) — same primitive and rare fallback as
    ``read_last_jsonl_entry`` / ``get_context_length_from_transcript``, and never
    an unconditional ``readlines()``.
    """
    try:
        if not os.path.exists(transcript_path):
            return "unknown"
        result = _scan_tail_then_full(transcript_path, max_bytes, _newest_model_in_lines)
        return result if result else "unknown"
    except Exception:
        return "unknown"


# ============================================================================
# TASK-SCOPED STATE MANAGEMENT (Multi-Session Support)
# ============================================================================

def get_task_state_manager():
    """Get TaskStateManager instance for current project.

    Returns:
        TaskStateManager configured with project state directory
    """
    from task_state_manager import TaskStateManager
    return TaskStateManager(STATE_DIR)


def cleanup_task_state_on_completion(task_name: str) -> bool:
    """Clean up task state directory when task completes.

    Called by task-completion protocol.

    Args:
        task_name: Name of the task

    Returns:
        True if cleanup was performed
    """
    manager = get_task_state_manager()
    return manager.cleanup_task_state(task_name)


# ============================================================================
# PROTOCOL STATE MANAGEMENT — READ FUNCTIONS
# ============================================================================
# These functions are safe to call from hooks (read-only).

PROTOCOL_LOGS_DIR = STATE_DIR / "protocol-logs"

def get_protocol_state() -> dict:
    """Get protocol state from current_task.json.
    Returns protocol dict or None if no protocol is active.

    SAFE: Read-only, callable from hooks and MCP.
    """
    task_state = get_task_state()
    return task_state.get("protocol")

def load_protocol_config(protocol_name: str) -> dict:
    """Load protocol JSON configuration.
    Search order: custom → system → development.
    Custom protocols override system protocols of the same name.
    Returns protocol config dict or None.

    SAFE: Read-only, callable from hooks and MCP.
    """
    search_paths = [
        # custom (user-owned, in the project) overrides everything
        PROJECT_ROOT / 'team-management' / 'protocol-configs' / 'custom' / f'{protocol_name}.json',
        # system from the plugin install (read-only; in dev PLUGIN_ROOT == <repo>/plugin).
        # Call get_plugin_root() at runtime (not the import-time PLUGIN_ROOT constant)
        # so it respects CLAUDE_PLUGIN_ROOT set/changed after import.
        get_plugin_root() / 'protocol-configs' / f'{protocol_name}.json',
        # legacy deployed-system tier (pre-plugin installer layout) — backward-compat
        PROJECT_ROOT / 'team-management' / 'protocol-configs' / 'system' / f'{protocol_name}.json',
    ]
    for path in search_paths:
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except json.JSONDecodeError as e:
                # Surface a malformed config (esp. a broken custom fork) instead of
                # silently falling through to the next tier and masking the typo.
                sys.stderr.write(
                    f"[load_protocol_config] malformed protocol config {path}: {e}; "
                    f"falling through to next tier\n"
                )
                continue
            except IOError:
                continue
            if not isinstance(config, dict):
                sys.stderr.write(
                    f"[load_protocol_config] protocol config {path} is not a JSON object; "
                    f"falling through to next tier\n"
                )
                continue
            return config
    return None

def resolve_protocol_start_text(start_text: str, protocol_name: str) -> str:
    """Resolve @-references in protocol step start text.
    "@sub-protocols/task-creation.md" -> contents of that file.

    SAFE: Read-only, callable from hooks and MCP.
    """
    if not start_text or not start_text.startswith("@"):
        return start_text
    ref_path = start_text[1:]
    search_bases = [
        # custom (project, user-owned) first
        PROJECT_ROOT / 'team-management' / 'protocol-configs' / 'custom',
        # plugin install (read-only): protocol-configs + the plugin root itself
        # (so @knowledge/*.md, @sub-protocols/*.md resolve). Dev: PLUGIN_ROOT == <repo>/plugin.
        # get_plugin_root() at runtime (not the import-time constant) — respects env.
        get_plugin_root() / 'protocol-configs',
        get_plugin_root(),
        # legacy deployed-system + project bases (backward-compat)
        PROJECT_ROOT / 'team-management' / 'protocol-configs' / 'system',
        PROJECT_ROOT / 'team-management',
        PROJECT_ROOT / '.claude',
    ]
    for base in search_bases:
        ref_file = base / ref_path
        if ref_file.exists():
            try:
                return ref_file.read_text(encoding='utf-8')
            except IOError:
                pass
    return start_text  # Return as-is if not resolved

def get_protocol_log(task_name: str) -> dict:
    """Read protocol log for a task. Returns dict or None.
    Falls back to _pending.json if task-specific log not found.

    SAFE: Read-only, callable from hooks and MCP.
    """
    log_file = PROTOCOL_LOGS_DIR / f'{task_name}.json'
    if not log_file.exists() and task_name:
        log_file = PROTOCOL_LOGS_DIR / '_pending.json'
    if log_file.exists():
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return None


# ============================================================================
# PROTOCOL STATE MANAGEMENT — WRITE FUNCTIONS (MCP-ONLY)
# ============================================================================
# WARNING: These functions write to protected state files.
# They must ONLY be called from MCP tools (protocol.py, task_state.py),
# NEVER from hooks (sessions-enforce.py, user-messages.py, etc.).

def set_protocol_state(protocol_name: str, current_step: int,
                       step_name: str, started_at: str,
                       extra: dict = None):
    """Set protocol state in current_task.json.

    Atomic across processes: lock → read → modify → atomic-rename write.
    MCP-ONLY: Do not call from hooks.

    The four canonical fields (name, current_step, step_name, started_at)
    are always overwritten. `extra`, if provided, is merged on top — used
    by the optimize protocols to persist `loop_iteration` and
    `experimentation_started_at` alongside the canonical fields. Caller
    owns lifecycle: pass through previous values to preserve them across
    advances, omit them to clear.
    """
    ensure_state_dir()
    with _state_lock():
        task_state = get_task_state()
        protocol_block = {
            "name": protocol_name,
            "current_step": current_step,
            "step_name": step_name,
            "started_at": started_at
        }
        if extra:
            protocol_block.update(extra)
        task_state["protocol"] = protocol_block
        task_state["updated"] = datetime.now().strftime("%Y-%m-%d")
        _write_json_durable(TASK_STATE_FILE, task_state)
        _throttle = STATE_DIR / 'protocol-end-condition-counter.txt'
        try:
            if _throttle.exists():
                _throttle.unlink()
        except Exception:
            pass

def clear_protocol_state():
    """Clear protocol state from current_task.json.

    Atomic across processes: lock → read → modify → atomic-rename write.
    MCP-ONLY: Do not call from hooks.
    """
    ensure_state_dir()
    with _state_lock():
        task_state = get_task_state()
        task_state.pop("protocol", None)
        task_state["updated"] = datetime.now().strftime("%Y-%m-%d")
        _write_json_durable(TASK_STATE_FILE, task_state)
        _throttle = STATE_DIR / 'protocol-end-condition-counter.txt'
        try:
            if _throttle.exists():
                _throttle.unlink()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# User-message intent matchers (consumed by user-messages.py)
# ---------------------------------------------------------------------------
# These live here — not inline in user-messages.py — so they are unit-testable:
# user-messages.py runs `json.load(sys.stdin)` at import, so it cannot be
# imported directly by a test. Both are pure (str -> bool / str|None).

# Whole-word, case-sensitive. Matches a bare `STOP`/`SILENCE` token but NOT
# substrings like `BACKSTOP` / `unSTOPpable` (no word boundary) or lowercase
# `stop`. Replaces the former `any(word in prompt ...)` substring match that
# fired on any report text quoting the word.
_EMERGENCY_STOP_RE = re.compile(r'\b(?:STOP|SILENCE)\b')


def is_emergency_stop(prompt):
    """True if `prompt` contains a whole-word `STOP` or `SILENCE` (case-sensitive).

    The emergency stop must stay trivially triggerable in a real emergency, so
    this is deliberately permissive within the whole-word constraint. Accepted
    residual: a report that whole-word-quotes an uppercase STOP still fires.
    """
    return bool(_EMERGENCY_STOP_RE.search(prompt or ""))


# Ordered (protocol_key, compiled_pattern); first match wins. Patterns are
# word-boundary + present-tense-imperative anchored, so descriptive / past-tense
# report text ("I created a task earlier", "the compaction runbook") no longer
# fires — the substring matchers these replaced matched any occurrence. All
# quantifiers are bounded/linear (no catastrophic-backtracking risk).
_PROTOCOL_INTENT_PATTERNS = [
    ("task-creation", re.compile(
        r'\b(?:create|make|add|open|start)\s+(?:a\s+)?(?:new\s+)?task\b'
        r'|\bnew\s+task\s+for\b',  # noun-first directive: "new task for the auth bug"
        re.IGNORECASE)),
    ("task-startup", re.compile(
        r'\b(?:switch\s+to|work\s+on|resume|change\s+to)\s+task\b',
        re.IGNORECASE)),
    ("task-completion", re.compile(
        r'\b(?:complete|finish|close|wrap\s+up)\b[^.\n]{0,30}\btask\b'
        r'|\btask\s+is\s+done\b'
        r'|\bmark\b[^.\n]{0,20}\b(?:complete|done)\b',  # "mark as complete"
        re.IGNORECASE)),
    ("context-compaction", re.compile(
        r'/compact\b|\bcontext\s+compaction\b|\bcompact\s+(?:the\s+)?context\b'
        r'|\brestart\s+(?:the\s+)?session\b',
        re.IGNORECASE)),
]


def detect_protocol_intent(prompt):
    """Return the protocol key a user message is directing toward, else None.

    One of ``task-creation`` / ``task-startup`` / ``task-completion`` /
    ``context-compaction``. Replaces the loose substring matchers in
    user-messages.py that misfired on ordinary report text. First match wins,
    in creation -> startup -> completion -> compaction order.
    """
    text = prompt or ""
    for key, pattern in _PROTOCOL_INTENT_PATTERNS:
        if pattern.search(text):
            return key
    return None