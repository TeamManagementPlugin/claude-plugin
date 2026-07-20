"""
Config MCP tools (m-config-mcp-flow).

`config_get`  — read-only snapshot of team-management/config.json with sensitive
                values masked. NOT gated.
`config_update` — deterministic, schema-validated writer for NON-secret config.
                Gated by the config-session intent-gate (a deterministic hook
                writes the flag only when the user physically types
                /team-management:config). Tokens are NEVER written here — they
                live in the per-project .claude/state/provider-tokens.json file
                (which the AI cannot read).

Self-contained: this module does NOT import from plugin/installer/ (deleted in
task #7). The small URL/IP validation it needs is implemented inline (SEC-007).
"""

import ipaddress
import json
import os
import re
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from core.project import get_project_root, setup_provider_imports
from core import config as core_config

# Keys that must NEVER be written through config_update — tokens go to the
# per-project .claude/state/provider-tokens.json file (which the AI cannot read),
# not config.json. Matched against the full dotted key.
_SENSITIVE_KEY_RE = re.compile(r"(token|secret|api[_-]?key|password|credential)", re.IGNORECASE)

# Allowed non-secret config keys (flat dotted form) → expected type.
# Sentinels: "url" → SEC-007 URL validation; "pos_int" → positive int (>0);
# ("enum", [...]) → membership.
_URL = "url"
_POS_INT = "pos_int"
_CONFIG_SCHEMA: Dict[str, Any] = {
    # identity
    "developer_name": str,
    "project_name": str,
    "api_mode": bool,
    # DAIC / workflow
    "blocked_tools": list,
    "test_command": (str, type(None)),
    "protocol_engine.enabled": bool,
    "branch_enforcement.enabled": bool,
    "branch_enforcement.branch_prefixes": dict,
    "task_detection.enabled": bool,
    "features.icon_style": ("enum", ["nerd_fonts", "emoji", "ascii"]),
    "code_review.enforce_warnings": bool,
    # auto-compact
    "auto_compact.enabled": bool,
    "auto_compact.threshold": int,
    "auto_compact.context_limit": _POS_INT,
    # issue tracking
    "issue_tracking.provider": ("enum", ["gitlab", "jira", "github", "disabled"]),
    "issue_tracking.auto_sync": bool,
    "gitlab.enabled": bool,
    "gitlab.project_path": str,
    "gitlab.base_url": _URL,
    "gitlab.auto_sync": bool,
    "gitlab.default_labels": list,
    "gitlab.issue_tracking_enabled": bool,
    "jira.enabled": bool,
    "jira.base_url": _URL,
    "jira.project_key": str,
    # api_version is a closed set the JiraAPI runtime enforces (jira_utils.py:84 —
    # rejects anything outside 2/3), so constrain the write surface to match it.
    "jira.api_version": ("enum", ["2", "3"]),
    "jira.default_issue_type": str,
    "jira.supported_issue_types": list,
    "jira.auto_sync": bool,
    "jira.issue_tracking_enabled": bool,
    "github.enabled": bool,
    "github.base_url": _URL,
    "github.repository": str,
    "github.auto_sync": bool,
    "github.default_labels": list,
    "github.workflow_labels": dict,
    "github.issue_tracking_enabled": bool,
    # AI providers
    "ai_providers.enabled_providers": list,
    "ai_providers.include_in_code_review": bool,
    "ai_providers.include_in_brainstorm": bool,
    "ai_providers.include_in_investigation": bool,
    "ai_providers.include_in_implementation": bool,
    "ai_providers.include_in_research_exploration": bool,
    "ai_providers.include_in_refactoring_planning": bool,
    "ai_providers.timeout": int,
    "codex.enabled": bool,
    "agy.enabled": bool,
    # notifications — secret tokens excluded (telegram bot_token is rejected by
    # _SENSITIVE_KEY_RE and lives in .claude/state/provider-tokens.json, never here)
    "notifications.enabled": bool,
    "notifications.mode": ("enum", ["per_step", "off"]),
    "notifications.prefix": str,
    "notifications.channels.telegram.enabled": bool,
    "notifications.channels.telegram.chat_id": str,
    "notifications.channels.telegram.ca_bundle": str,
    # wiki
    "wiki.enabled": bool,
}

# Human-readable one-line descriptions for every _CONFIG_SCHEMA key, surfaced to the
# /team-management:config flow via config_get's `schema` field so the LLM never has to
# guess a key's meaning. Kept parallel to _CONFIG_SCHEMA — a drift-guard test asserts
# every schema key has an entry here.
_SCHEMA_DESCRIPTIONS: Dict[str, str] = {
    "developer_name": "Name team-management uses to address you.",
    "project_name": "Project label shown in the statusline; empty/unset falls back to the project folder name.",
    "api_mode": "Reserved API-mode toggle for headless/non-interactive operation.",
    "blocked_tools": "Tool names blocked while in DAIC discussion mode (e.g. Edit, Write, MultiEdit, NotebookEdit).",
    "test_command": "Optional test-runner command for the code-review gate; null skips it. Must start with an allowlisted prefix (pytest, npm test, cargo test, ...).",
    "protocol_engine.enabled": "Enable the JSON-driven protocol engine.",
    "branch_enforcement.enabled": "Enable git-branch enforcement for tasks.",
    "branch_enforcement.branch_prefixes": "Task-prefix -> branch-prefix map (e.g. {\"fix-\": \"fix/\", \"o-\": \"optimize/\"}); overrides the built-in defaults.",
    "task_detection.enabled": "Enable task-based workflows and task-state detection.",
    "features.icon_style": "Statusline icon set: nerd_fonts (patched-font glyphs), emoji, or ascii (safest for any terminal).",
    "code_review.enforce_warnings": "When true, code-review warnings must be acknowledged before task completion.",
    "auto_compact.enabled": "Enable automatic context compaction near the token threshold.",
    "auto_compact.threshold": "Token-usage percentage that triggers auto-compaction (e.g. 85).",
    "auto_compact.context_limit": "Explicit model context-window budget in tokens (e.g. 1000000 for 1M); overrides auto-detection.",
    "issue_tracking.provider": "Active issue-tracking provider, or 'disabled'.",
    "issue_tracking.auto_sync": "Global toggle for automatic task<->issue synchronization.",
    "gitlab.enabled": "Enable GitLab integration.",
    "gitlab.project_path": "GitLab project path (namespace/project).",
    "gitlab.base_url": "GitLab instance URL (https).",
    "gitlab.auto_sync": "Auto-sync tasks to GitLab issues.",
    "gitlab.default_labels": "Default labels applied to created GitLab issues/MRs.",
    "gitlab.issue_tracking_enabled": "When false, protocols skip GitLab issue create/close (MR creation unaffected).",
    "jira.enabled": "Enable Jira integration.",
    "jira.base_url": "Jira instance URL (https).",
    "jira.project_key": "Jira project key (e.g. PROJ).",
    "jira.api_version": "Jira REST API version to target. Supported: \"2\" or \"3\".",
    "jira.default_issue_type": "Default Jira issue type for created issues (e.g. Task).",
    "jira.supported_issue_types": "Jira issue types this project recognizes.",
    "jira.auto_sync": "Auto-sync tasks to Jira issues.",
    "jira.issue_tracking_enabled": "When false, protocols skip Jira issue create/close.",
    "github.enabled": "Enable GitHub / Gitea integration.",
    "github.base_url": "GitHub or Gitea API URL (https; api.github.com for GitHub, /api/v1 for Gitea).",
    "github.repository": "GitHub / Gitea repository (owner/repo).",
    "github.auto_sync": "Auto-sync tasks to GitHub / Gitea issues.",
    "github.default_labels": "Default labels applied to created GitHub / Gitea issues/PRs.",
    "github.workflow_labels": "State->label map for workflow states (names for GitHub, auto-converted to IDs for Gitea).",
    "github.issue_tracking_enabled": "When false, protocols skip GitHub / Gitea issue create/close (PR creation unaffected).",
    "ai_providers.enabled_providers": "Active AI providers, e.g. [\"codex\", \"agy\"].",
    "ai_providers.include_in_code_review": "Run AI providers during the code-review phase.",
    "ai_providers.include_in_brainstorm": "Run AI providers during the brainstorm analysis phase.",
    "ai_providers.include_in_investigation": "Run AI providers during the task investigation phase.",
    "ai_providers.include_in_implementation": "Run AI providers during the implementation planning phase.",
    "ai_providers.include_in_research_exploration": "Run AI providers during the research exploration phase.",
    "ai_providers.include_in_refactoring_planning": "Run AI providers during the refactoring planning phase.",
    "ai_providers.timeout": "Currently inert (default 300): NOT read by the codex/agy wrappers, which enforce a fixed deadline (codex 300s, agy 330s watchdog). Kept for a future plumbing task.",
    "codex.enabled": "Enable the Codex (OpenAI) provider.",
    "agy.enabled": "Enable the Antigravity (agy) provider; uses the CLI-default model.",
    "notifications.enabled": "Enable external notifications (e.g. Telegram) on protocol events.",
    "notifications.mode": "'per_step' pings on every protocol step; 'off' silences per-step pings (the completion ping is still sent).",
    "notifications.prefix": "Short label prefixed to notification messages (e.g. the project name).",
    "notifications.channels.telegram.enabled": "Enable the Telegram notification channel.",
    "notifications.channels.telegram.chat_id": "Telegram chat id to deliver notifications to (the bot token is a secret set in .claude/state/provider-tokens.json under key telegram, not here).",
    "notifications.channels.telegram.ca_bundle": "Optional path to a CA bundle (PEM) for verifying api.telegram.org — only needed if the Python running the MCP server can't verify Telegram's cert (empty CA store, or a TLS-inspecting proxy whose root you point at here). Env SSL_CERT_FILE / REQUESTS_CA_BUNDLE also honored.",
    "wiki.enabled": "Enable the LLM Wiki feature.",
}


def _describe_schema() -> List[Dict[str, Any]]:
    """Render _CONFIG_SCHEMA as a discoverable, LLM-facing list — the authoritative
    catalog of settable non-secret keys. Each entry is
    ``{"key", "type", "allowed"?, "description"}``. config_get returns this as its
    ``schema`` field so the /team-management:config flow reads a key's type/enum
    straight from the tool instead of guessing (secrets never appear here — they are
    rejected by _SENSITIVE_KEY_RE and live in .claude/state/provider-tokens.json)."""
    out: List[Dict[str, Any]] = []
    for key, spec in _CONFIG_SCHEMA.items():
        entry: Dict[str, Any] = {"key": key}
        if spec == _URL:
            entry["type"] = "https URL"
        elif spec == _POS_INT:
            entry["type"] = "positive integer"
        elif isinstance(spec, tuple) and spec and spec[0] == "enum":
            entry["type"] = "string (enum)"
            entry["allowed"] = list(spec[1])
        elif spec == (str, type(None)):
            entry["type"] = "string or null"
        elif spec is bool:
            entry["type"] = "boolean"
        elif spec is int:
            entry["type"] = "integer"
        elif spec is str:
            entry["type"] = "string"
        elif spec is list:
            entry["type"] = "array"
        elif spec is dict:
            entry["type"] = "object"
        else:  # pragma: no cover - defensive; every spec above is handled
            entry["type"] = str(spec)
        entry["description"] = _SCHEMA_DESCRIPTIONS.get(key, "")
        out.append(entry)
    return out


_GIT_TIMEOUT = 10


# --------------------------------------------------------------------------
# SEC-007 — URL safety
# --------------------------------------------------------------------------

def _validate_safe_url(url: Any) -> Tuple[bool, Optional[str]]:
    """https-only; reject localhost / *.local / literal private/loopback/
    link-local/reserved IPs AND numeric-encoded IPs (decimal/hex/octal).

    Accepted residual gaps (anti-SSRF for the literal URL only — no network
    resolution here): a hostname that DNS-resolves to a private address, and
    dotted-octal/mixed-radix forms like ``0177.0.0.1`` that neither
    ``ipaddress`` nor this guard normalise."""
    if not isinstance(url, str) or not url.strip():
        return False, "URL must be a non-empty string"
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        return False, f"URL must use https:// (got '{parsed.scheme or 'no scheme'}')"
    host = parsed.hostname
    if not host:
        return False, "URL has no host"
    low = host.lower()
    if low == "localhost" or low.endswith(".local") or low.endswith(".localhost"):
        return False, f"URL host '{host}' is local — not allowed"
    # Numeric-encoded IPs (decimal 2130706433, hex 0x7f000001, octal 0o..) do NOT
    # parse via ipaddress.ip_address(host) but resolvers accept them as e.g.
    # 127.0.0.1 — an SSRF validator-bypass class. A legitimate https host is
    # never bare-numeric or 0x/0o/0b-prefixed, so reject those outright.
    if "." not in low and ":" not in low and (
        low.isdigit() or low.startswith(("0x", "0o", "0b"))
    ):
        return False, f"URL host '{host}' looks like a numeric IP encoding — not allowed"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            return False, f"URL host '{host}' is a private/loopback/link-local address"
    except ValueError:
        pass  # hostname, not a literal IP
    return True, None


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------

def _validate_updates(updates: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate a flat dotted-key update dict against the schema. Returns
    (ok, errors). Sensitive keys are rejected BEFORE the schema check so the
    message names the real reason (token → provider-tokens.json)."""
    errors: List[str] = []
    if not isinstance(updates, dict) or not updates:
        return False, ["updates must be a non-empty object of dotted keys"]
    for key, value in updates.items():
        if _SENSITIVE_KEY_RE.search(key):
            errors.append(f"'{key}': secrets are not written to config.json — "
                          f"set tokens in .claude/state/provider-tokens.json "
                          f"(per-project; the AI cannot read it)")
            continue
        spec = _CONFIG_SCHEMA.get(key)
        if spec is None:
            errors.append(f"'{key}': unknown config key (not allowed by config_update)")
            continue
        if spec == _URL:
            ok, reason = _validate_safe_url(value)
            if not ok:
                errors.append(f"'{key}': {reason}")
        elif spec == _POS_INT:
            # bool is a subclass of int — reject it explicitly, like the int path.
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"'{key}': expected a positive integer")
            elif value <= 0:
                errors.append(f"'{key}': must be a positive integer (got {value})")
        elif isinstance(spec, tuple) and spec and spec[0] == "enum":
            if value not in spec[1]:
                errors.append(f"'{key}': must be one of {spec[1]} (got {value!r})")
        else:
            # bool must be checked before int (bool is a subclass of int).
            if spec is int and isinstance(value, bool):
                errors.append(f"'{key}': expected int, got bool")
            elif not isinstance(value, spec):
                expected = spec if isinstance(spec, tuple) else spec.__name__
                errors.append(f"'{key}': expected {expected}, got {type(value).__name__}")
    return (not errors), errors


def _set_dotted(config: Dict[str, Any], dotted: str, value: Any) -> None:
    """Set a nested key from a dotted path, creating intermediate dicts.

    Raises ValueError if an existing intermediate node is a non-dict (a
    corrupted config like ``{"gitlab": "oops"}``) rather than silently clobbering
    it — config_update surfaces that as an error instead of dropping data."""
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        nxt = node.get(part)
        if nxt is None:
            nxt = {}
            node[part] = nxt
        elif not isinstance(nxt, dict):
            raise ValueError(f"existing '{part}' in config.json is not an object")
        node = nxt
    node[parts[-1]] = value


def _mask_sensitive(obj: Any) -> Any:
    """Recursively mask sensitive values for config_get — never echo a token."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k) and not isinstance(v, (dict, list)):
                out[k] = "***set***" if v else "***unset***"
            else:
                out[k] = _mask_sensitive(v)
        return out
    if isinstance(obj, list):
        return [_mask_sensitive(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# git / gitignore helpers
# --------------------------------------------------------------------------

def _git(args: List[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=_GIT_TIMEOUT)


def _git_toplevel(cwd) -> Optional[str]:
    try:
        r = _git(["rev-parse", "--show-toplevel"], cwd)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _is_tracked(config_path, cwd) -> bool:
    try:
        r = _git(["ls-files", "--error-unmatch", str(config_path)], cwd)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False  # not a repo / git missing → cannot be tracked


def _gitignore_covers(lines: Iterable[str], rel: str) -> Optional[str]:
    """Return the existing .gitignore line that already ignores ``rel``, else None.

    Recognizes the exact file entry and any ancestor-directory entry, each with an
    optional leading ``/`` and (for directories) an optional trailing ``/``. This
    deliberately does NOT implement full gitignore glob semantics (``*``, ``**``,
    character classes) — only the plain literal forms that realistically pre-cover
    the config file (e.g. a project that ignores ``team-management/`` wholesale).
    Exotic glob patterns fall through, and a redundant exact line is appended — the
    same behaviour as before this recognition existed.

    Negation safety: a later ``!``-re-include of this exact file or a covering
    ancestor (e.g. ``!team-management/config.json``) would leave the file trackable
    despite a positive directory match, inverting the guard's intent. So if any
    ``!`` line targets a candidate, this returns None — the caller then appends the
    positive entry, which (last-match-wins) re-ignores the file. Safe because the
    config file is guaranteed untracked here by the step-5 tracked-check. Deeper
    glob/dir-re-include negation forms remain the documented accepted gap."""
    lines = list(lines)  # materialize: iterated twice (negation pass, then positive pass)
    candidates = {rel, f"/{rel}"}
    parts = rel.split("/")
    for i in range(1, len(parts)):  # ancestor directories: 'team-management', then deeper
        prefix = "/".join(parts[:i])
        candidates |= {prefix, f"{prefix}/", f"/{prefix}", f"/{prefix}/"}
    if any(ln.startswith("!") and ln[1:] in candidates for ln in lines):
        return None  # a negation re-includes the file/ancestor — append to re-protect
    for ln in lines:
        if ln in candidates:
            return ln
    return None


def _ensure_gitignored(toplevel: Optional[str], config_path) -> Dict[str, Any]:
    """Ensure ``config_path`` is ignored by the host project's .gitignore
    (idempotent, best-effort). Returns a status dict::

        {"status": "added" | "already_covered" | "unavailable",
         "path": <repo-relative posix path, or None>,
         "covered_by": <existing .gitignore line that already covers it, or None>}

    Never raises — a .gitignore read/write failure (no git repo, an OSError, or a
    non-UTF-8 .gitignore raising a UnicodeError) degrades to status
    ``"unavailable"`` and must not block the config write."""
    if not toplevel:
        return {"status": "unavailable", "path": None, "covered_by": None}
    from pathlib import Path
    top = Path(toplevel)
    try:
        rel = Path(config_path).resolve().relative_to(top.resolve()).as_posix()
    except (ValueError, OSError):
        rel = "team-management/config.json"
    gitignore = top / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        # Ordered list (not a set) so _gitignore_covers returns the FIRST covering
        # line in .gitignore order — deterministic when several lines could match.
        # rstrip (NOT strip): git treats LEADING whitespace as part of the pattern
        # ("  team-management/" does NOT ignore the dir) but strips TRAILING spaces
        # ("team-management/  " does ignore it). Accepted gap: a backslash-escaped
        # trailing space (rare) is over-trimmed here.
        lines = [ln.rstrip() for ln in existing.splitlines()
                 if ln.rstrip() and not ln.rstrip().startswith("#")]
        covered_by = _gitignore_covers(lines, rel)
        if covered_by is not None:
            return {"status": "already_covered", "path": rel, "covered_by": covered_by}
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        with open(gitignore, "a", encoding="utf-8") as f:
            f.write(f"{sep}{rel}\n")
        return {"status": "added", "path": rel, "covered_by": None}
    except (OSError, UnicodeError):
        # UnicodeError (decode on read / encode on write) is a ValueError, not an
        # OSError — catch it so a non-UTF-8 .gitignore never blocks the config write.
        return {"status": "unavailable", "path": rel, "covered_by": None}


def register_tools(mcp):
    """Register config tools with the FastMCP server."""

    @mcp.tool()
    def config_get() -> Dict[str, Any]:
        """
        Return the current team-management/config.json with sensitive values
        masked (tokens shown as ***set***/***unset***, never echoed). Read-only,
        ungated — safe to call any time to show current settings.

        Also returns a `schema` field: the authoritative catalog of every setting
        config_update can write, each as {key, type, allowed?, description}. Read it
        to learn a key's exact type/enum before calling config_update — it is the
        ground truth (secrets are NOT listed there; set tokens in .claude/state/provider-tokens.json).
        """
        project_root = get_project_root()
        config_path = project_root / "team-management" / "config.json"
        schema = _describe_schema()
        if not config_path.exists():
            return {"success": True, "exists": False, "config": {},
                    "schema": schema, "config_path": str(config_path)}
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"success": False, "error": f"config.json unreadable: {e}",
                    "schema": schema, "config_path": str(config_path)}
        return {"success": True, "exists": True, "config": _mask_sensitive(config),
                "schema": schema, "config_path": str(config_path)}

    @mcp.tool()
    def config_update(updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update NON-secret team-management config.json keys. Gated: only runs
        inside an active /team-management:config session (a deterministic hook
        writes the session flag — the LLM cannot open the gate itself).

        `updates` is a flat object of dotted keys, e.g.
        {"developer_name": "Max", "issue_tracking.provider": "gitlab",
         "gitlab.base_url": "https://gitlab.com"}.

        Tokens/secrets are rejected — set them in .claude/state/provider-tokens.json
        (per-project; the AI cannot read it).
        Pre-existing token values already in config.json are preserved untouched.
        """
        project_root = get_project_root()
        config_path = project_root / "team-management" / "config.json"

        # 1. Intent-gate — must be inside a live config session.
        setup_provider_imports()
        import shared_state
        ok, reason = shared_state.check_config_session_flag()
        if not ok:
            return {"success": False, "error": f"config_update is gated: {reason}",
                    "hint": "Run /team-management:config (a deterministic hook opens the gate)."}

        # 2-4. Sensitive-key reject + schema + SEC-007 URL validation.
        valid, errors = _validate_updates(updates)
        if not valid:
            return {"success": False, "error": "validation failed", "errors": errors}

        # 5. git tracked-check — refuse to manage a committed config.json (it
        #    would carry tokens into history).
        toplevel = _git_toplevel(project_root)
        if config_path.exists() and _is_tracked(config_path, project_root):
            return {"success": False,
                    "error": "team-management/config.json is git-tracked — it must be "
                             "gitignored before config_update will write to it (it may hold "
                             "tokens). Run: git rm --cached team-management/config.json"}

        # 6. gitignore-ensure (so the write stays out of version control).
        gitignore_status = _ensure_gitignored(toplevel, config_path)

        # 7. read-modify-merge — load existing, set only the provided (non-secret)
        #    keys; pre-existing token values are left untouched.
        config: Dict[str, Any] = {}
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return {"success": False, "error": f"existing config.json unreadable: {e}"}
            if not isinstance(config, dict):
                return {"success": False, "error": "existing config.json is not a JSON object"}
        try:
            for key, value in updates.items():
                _set_dotted(config, key, value)
        except ValueError as e:
            return {"success": False, "error": f"cannot apply update: {e}"}

        # 8. atomic write + 0600 (holds non-secret config but lives next to tokens).
        config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shared_state._write_json_durable(config_path, config)
            if os.name != "nt":
                os.chmod(config_path, 0o600)
        except OSError as e:
            return {"success": False, "error": f"failed to write config.json: {e}"}

        # 8a. Invalidate the long-lived MCP server's config cache so THIS session
        #     picks up the write without a Claude Code restart
        #     (m-fix-mcp-config-cache-poisoning). Uses the canonical core.config
        #     module identity (NOT plugin.mcp.core.config — a separate module whose
        #     cache would desync, per test/test_mcp_core.py). load_config() would
        #     self-heal on the mtime change anyway, but detect_provider caches
        #     _provider separately; reload_config() clears both + the provider
        #     singletons so a provider switch in this same call takes effect too.
        core_config.reload_config()

        # 8b. Deploy the task-file TEMPLATE.md the protocols reference, now that
        #     team-management/ exists (the retired installer used to; #7 left
        #     nothing to). This is what makes /team-management:config produce a
        #     usable task template WITHOUT waiting for the next session start.
        #     Best-effort and self-swallowing — never affects the config result.
        shared_state.ensure_task_template_deployed(project_root, shared_state.get_plugin_root())

        # 8c. Deploy + wire the behavioral guidance (CLAUDE.tm.md / knowledge /
        #     optional CLAUDE.wiki.md) and ensure the managed @-include block in
        #     CLAUDE.md, so /team-management:config (and the /team-management:init
        #     flow that calls config_update) take effect in this session without
        #     waiting for the next restart. The SessionStart hook self-heals the same
        #     delivery. Best-effort; never affects the config result
        #     (h-durable-guidance-via-claude-md).
        # isinstance guard: a pre-existing non-dict `wiki` value (null/str/int) that
        # the current update does not touch must NOT crash config_update AFTER the
        # config write already succeeded (code-review Warning 1). `config` itself is
        # guaranteed a dict (the non-dict-root case is rejected upstream).
        _wiki_cfg = config.get("wiki")
        _wiki_on = bool(_wiki_cfg.get("enabled", False)) if isinstance(_wiki_cfg, dict) else False
        shared_state.ensure_guidance_deployed_and_wired(
            project_root, shared_state.get_plugin_root(), _wiki_on)

        # 8c-bis. Deploy the agy read-only PreToolUse deny-gate into the project when
        #     agy is an enabled provider, so `.agents/hooks.json` is present the moment
        #     the user turns agy on via /team-management:config (matching what the
        #     SessionStart hook does on restart). This is what CONTAINS the agy-cli
        #     wrapper's --dangerously-skip-permissions review. Merge-aware (never
        #     clobbers a user's own .agents hooks); best-effort; never affects the result.
        if shared_state._agy_enabled(config):
            shared_state.ensure_agy_readonly_gate_deployed(
                project_root, shared_state.get_plugin_root())

        # 8d. Seed the per-project provider-token file (create-if-absent) so the user
        #     has a template to fill in — tokens live in
        #     .claude/state/provider-tokens.json (per-project, git-ignored, 0600,
        #     AI-unreadable), NOT the OS keychain. NEVER clobbers an existing file.
        #     Best-effort; never affects the config result.
        shared_state.ensure_provider_tokens_file()

        # 9. refresh the session TTL (preserve the hook-written session_id).
        try:
            shared_state.write_config_session_flag(preserve_session_id=True)
        except OSError:
            pass

        return {"success": True, "updated_keys": sorted(updates.keys()),
                "config_path": str(config_path), "gitignore": gitignore_status}
