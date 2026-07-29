#!/usr/bin/env python3
"""
Protocol Engine for structured workflow management.

Manages protocol lifecycle: start, advance, goto, abort.
Executes pre/post step functions (built-in and custom),
maintains audit logs, and enforces DAIC mode per protocol step.

Custom functions can be added by placing Python files in
team-management/protocol-configs/custom/funcs/ and referencing
them as custom(func_name) in protocol JSON configs.

This module is imported by MCP tools (protocol.py, task_state.py)
and reads/writes to .claude/state/ files.
"""

import importlib.util
import json
import os
import re
import shlex
import subprocess
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine_constants import (
    GIT_TIMEOUT_FAST,
    GIT_TIMEOUT_MEDIUM,
    GIT_TIMEOUT_SLOW,
    TEST_TIMEOUT,
)

from shared_state import (
    get_task_state,
    set_task_state,
    set_daic_mode,
    get_protocol_state,
    set_protocol_state,
    clear_protocol_state,
    load_protocol_config,
    resolve_protocol_start_text,
    get_protocol_log,
    infer_task_from_branch,
    cleanup_task_state_on_completion,
    parse_task_frontmatter,
    _write_json_durable,
    ensure_state_dir,
)

from optimize_completion import OptimizeCompletionMixin

# _PHASE_REGISTRY & friends are re-exported here for back-compat importers
# (e.g. `from protocol_engine import _PHASE_REGISTRY`); the noqa keeps ruff from
# stripping the re-exports this module does not itself reference.
from ai_providers import (
    AIProvidersMixin,
    _PHASE_REGISTRY,
    _PHASES_TEMPLATE_DRIVEN,  # noqa: F401
    _CREDENTIAL_FILTER_PATTERNS,  # noqa: F401
    _PROVIDER_INLINE_DEFAULT_TEMPLATE,  # noqa: F401
    _DefaultEmptyDict,  # noqa: F401
)


class ProtocolEngine(OptimizeCompletionMixin, AIProvidersMixin):
    """Manages structured workflow protocols with step tracking and automation."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.state_dir = project_root / ".claude" / "state"
        self.logs_dir = self.state_dir / "protocol-logs"
        self._explicit_task = None  # Set by start_protocol(task=...) for auto_detect_task

    # ========================================================================
    # READ METHODS (safe for hooks + MCP)
    # ========================================================================

    def get_current_step(self) -> Optional[Dict]:
        """Get current protocol step details."""
        protocol_info = get_protocol_state()
        if not protocol_info:
            return None

        config = load_protocol_config(protocol_info["name"])
        if not config:
            return None

        steps = config.get("steps", [])
        idx = protocol_info.get("current_step", 0)
        if idx < len(steps):
            step = steps[idx]
            return {
                "index": idx,
                "name": step["name"],
                "mode": step.get("mode", "discussion"),
                "total_steps": len(steps),
                "description": step.get("description", ""),
            }
        return None

    def get_end_condition(self) -> Optional[str]:
        """Get end condition text for the current step."""
        protocol_info = get_protocol_state()
        if not protocol_info:
            return None

        config = load_protocol_config(protocol_info["name"])
        if not config:
            return None

        steps = config.get("steps", [])
        idx = protocol_info.get("current_step", 0)
        if idx < len(steps):
            return steps[idx].get("end", "")
        return None

    def list_protocols(self) -> Dict[str, Any]:
        """List all available protocols from custom + system + dev directories.

        Custom protocols shadow system/dev of the same name. Files reserved for
        the customization tooling are skipped: a leading ``new-`` marks drift
        staging (``protocol_check_drift``) and a leading ``.`` marks a sidecar
        (e.g. ``.forked-from.json``) — neither is a protocol config, and on this
        platform ``Path.glob("*.json")`` matches both. Malformed JSON is surfaced
        (stderr + a returned ``warnings`` list) instead of being silently dropped,
        which would otherwise mask a broken custom fork.
        """
        import sys
        from shared_state import get_plugin_root
        search_dirs = [
            (self.project_root / "team-management" / "protocol-configs" / "custom", "custom"),
            # system bundled in the plugin install (dev: PLUGIN_ROOT == <repo>/plugin)
            (get_plugin_root() / "protocol-configs", "system"),
            # legacy deployed-system tier (pre-plugin installer layout) — backward-compat
            (self.project_root / "team-management" / "protocol-configs" / "system", "deployed"),
        ]

        seen = set()
        protocols = []
        warnings = []

        for search_dir, source in search_dirs:
            if not search_dir.exists():
                continue
            for config_file in sorted(search_dir.glob("*.json")):
                # Reserved customization artefacts, not protocol configs.
                if config_file.name.startswith(("new-", ".")):
                    continue
                try:
                    with open(config_file, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    if not isinstance(config, dict):
                        # Valid JSON but not an object (e.g. top-level list) — a
                        # protocol config must be a dict; treat as malformed.
                        raise ValueError("top-level JSON is not an object")
                    name = config.get("name", config_file.stem)
                    if name in seen:
                        continue
                    seen.add(name)
                    steps = config.get("steps", [])
                    protocols.append({
                        "name": name,
                        "description": config.get("description", ""),
                        "steps_count": len(steps),
                        "step_names": [s["name"] for s in steps],
                        "steps": [
                            {
                                "name": s["name"],
                                "description": s.get("description", ""),
                                "mode": s.get("mode", "discussion"),
                            }
                            for s in steps
                        ],
                        "source": source,
                    })
                except (json.JSONDecodeError, IOError, KeyError, ValueError, TypeError) as e:
                    # TypeError covers a dict config with a non-list ``steps``
                    # (e.g. "steps": null) so one malformed custom fork warns +
                    # skips instead of blanking the whole listing.
                    msg = f"{config_file}: {type(e).__name__}: {e}"
                    warnings.append(msg)
                    sys.stderr.write(f"[protocol_list] skipped malformed config {msg}\n")
                    continue

        if not protocols:
            result = {"success": True, "protocols": [], "message": "No protocol configs found."}
        else:
            result = {"success": True, "protocols": protocols}
        if warnings:
            result["warnings"] = warnings
        return result

    @staticmethod
    def _read_provenance_sidecar(sidecar):
        """Load ``custom/.forked-from.json``.

        Returns ``(data, error)``. ``error`` is a message string when the file
        exists but is corrupt / not a JSON object — callers MUST abort rather
        than overwrite it, which would wipe provenance for every other forked
        protocol. Absent file -> ``({}, None)``.
        """
        if not sidecar.exists():
            return {}, None
        try:
            loaded = json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return None, (f"Provenance sidecar {sidecar.name} is unreadable ({e}); "
                          f"refusing to overwrite it — fix or remove it and retry.")
        if not isinstance(loaded, dict):
            return None, (f"Provenance sidecar {sidecar.name} is not a JSON object; "
                          f"refusing to overwrite it — fix or remove it and retry.")
        return loaded, None

    def _encode_system_source(self, src: Path) -> str:
        """Represent a system-source path portably in the provenance sidecar.

        - Under the project root -> project-relative (legacy/dev layout; byte-identical
          to the pre-plugin behaviour, so the sidecar stays committable).
        - Under PLUGIN_ROOT but OUTSIDE the project (a real plugin install) -> a
          ``${PLUGIN_ROOT}/...`` marker resolved per-machine by check_drift.
        - Otherwise -> absolute string (best effort).

        Without this, ``src.relative_to(self.project_root)`` raised ValueError and
        crashed customize_protocol whenever the system source lived outside the
        project (i.e. every real plugin install — get_plugin_root() is external).
        """
        from shared_state import get_plugin_root
        try:
            return str(src.relative_to(self.project_root))
        except ValueError:
            pass
        try:
            return "${PLUGIN_ROOT}/" + str(src.relative_to(get_plugin_root()))
        except ValueError:
            return str(src)

    def _decode_system_source(self, rel_system: str) -> Path:
        """Inverse of _encode_system_source: resolve a recorded system path."""
        from shared_state import get_plugin_root
        marker = "${PLUGIN_ROOT}/"
        if rel_system.startswith(marker):
            return get_plugin_root() / rel_system[len(marker):]
        p = Path(rel_system)
        return p if p.is_absolute() else self.project_root / rel_system

    def customize_protocol(self, protocol_name: str, force: bool = False) -> Dict[str, Any]:
        """Bootstrap-copy a system protocol into custom/ for full local editing.

        Copies the protocol JSON + every referenced @sub-protocol (from each
        step's top-level ``start``) + the provider templates for the protocol's
        AI phases (only those with an on-disk template — ``code_review`` uses
        inline prompts and is skipped) from ``protocol-configs/system/`` into
        ``protocol-configs/custom/``. Records a provenance sidecar
        ``custom/.forked-from.json`` (sha256 of each system source at fork time)
        so :meth:`check_drift` can later detect upstream changes. Existing custom
        files are preserved unless ``force=True`` — and a skipped file keeps its
        previously-recorded provenance hash (or ``None`` when this is a
        pre-existing fork with no prior provenance), so a no-force re-run after
        an upstream change cannot silently mask real drift. The sidecar is
        written atomically; a corrupt existing sidecar aborts rather than being
        overwritten.

        MUST run in the MCP server process: reading the protected ``system/`` tree
        is blocked for agent-side tools by sessions-enforce.
        """
        import hashlib
        from datetime import datetime, timezone

        # Reject path-traversal / non-bare names before touching the filesystem.
        if (not protocol_name or protocol_name.startswith(".")
                or protocol_name != Path(protocol_name).name):
            return {"success": False,
                    "error": (f"Invalid protocol name '{protocol_name}': must be a bare "
                              f"name with no path separators.")}

        from shared_state import get_plugin_root
        cfg = self.project_root / "team-management" / "protocol-configs"
        custom_dir = cfg / "custom"
        src_base = cfg / "system"
        src_json = src_base / f"{protocol_name}.json"
        if not src_json.exists():
            # plugin-bundled system configs (dev: PLUGIN_ROOT == <repo>/plugin)
            plugin_base = get_plugin_root() / "protocol-configs"
            if (plugin_base / f"{protocol_name}.json").exists():
                src_base, src_json = plugin_base, plugin_base / f"{protocol_name}.json"
            else:
                return {"success": False,
                        "error": f"Unknown protocol '{protocol_name}': no system or plugin config found."}

        try:
            config = json.loads(src_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return {"success": False, "error": f"Cannot read protocol '{protocol_name}': {e}"}
        if not isinstance(config, dict):
            return {"success": False,
                    "error": f"Protocol '{protocol_name}' config is not a JSON object."}
        steps = config.get("steps", [])

        # Source->dest copy list: json + referenced sub-protocols + provider templates.
        pairs = [(src_json, custom_dir / f"{protocol_name}.json")]
        for step in steps:
            start = step.get("start", "")
            if isinstance(start, str) and start.startswith("@sub-protocols/"):
                ref = start[1:]  # 'sub-protocols/<name>.md'
                pairs.append((src_base / ref, custom_dir / ref))

        # Provider templates only for phases this protocol actually wires via
        # pre_funcs, and only those with a file on disk (code_review is inline).
        func_to_subpath = {e["func_name"]: e["template_subpath"] for e in _PHASE_REGISTRY.values()}
        subpaths = []
        for step in steps:
            for pf in step.get("pre_funcs", []):
                sp = func_to_subpath.get(pf)
                if sp and sp not in subpaths:
                    subpaths.append(sp)
        for sp in subpaths:
            for provider in ("codex", "agy"):
                fn = f"{provider}-{sp}.md"
                src = src_base / "providers" / fn
                if src.exists():
                    pairs.append((src, custom_dir / "providers" / fn))

        # Preserve prior provenance hashes for files we skip (no-clobber).
        sidecar = custom_dir / ".forked-from.json"
        sidecar_data, err = self._read_provenance_sidecar(sidecar)
        if err:
            return {"success": False, "error": err}
        # Provenance is per-FILE, not per-protocol: a file shared by several
        # protocols (e.g. sub-protocols/code-review.md, task-completion.md) keeps
        # the fork-time hash recorded by whichever protocol copied it first, so
        # forking a second protocol that shares it inherits real provenance
        # instead of a None (unknown) baseline. Scan ALL protocol entries.
        prior = {}
        for entry in sidecar_data.values():
            if not isinstance(entry, dict):
                continue
            for r in entry.get("files", []):
                if (isinstance(r, dict) and r.get("custom")
                        and r.get("sha256") is not None):
                    prior.setdefault(r["custom"], r["sha256"])

        created, skipped, file_records, seen_dest = [], [], [], set()
        for src, dest in pairs:
            if dest in seen_dest or not src.exists():
                continue
            seen_dest.add(dest)
            rel_custom = str(dest.relative_to(self.project_root))
            rel_system = self._encode_system_source(src)
            if dest.exists() and not force:
                skipped.append(rel_custom)
                # Keep the known fork-time hash (possibly inherited from another
                # protocol that shares this file); record None only when no
                # protocol has ever tracked it (a genuine hand-made fork).
                file_records.append({"custom": rel_custom, "system": rel_system,
                                     "sha256": prior.get(rel_custom)})
                continue
            data = src.read_bytes()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            created.append(rel_custom)
            file_records.append({"custom": rel_custom, "system": rel_system,
                                 "sha256": hashlib.sha256(data).hexdigest()})

        sidecar_data[protocol_name] = {
            "forked_at": datetime.now(timezone.utc).isoformat(),
            "files": file_records,
        }
        custom_dir.mkdir(parents=True, exist_ok=True)
        _write_json_durable(sidecar, sidecar_data)

        return {
            "success": True,
            "protocol": protocol_name,
            "created": created,
            "skipped": skipped,
            "next_steps": (
                "Edit the copied files under team-management/protocol-configs/custom/ to "
                f"customize '{protocol_name}'. The engine resolves custom/ over system/ "
                "automatically. After a team-management reinstall, run protocol_check_drift() "
                "(slash command /team-management:custom-protocol-update-after-reinstall) to reconcile "
                "upstream changes."
            ),
        }

    def check_drift(self, acknowledge: bool = False) -> Dict[str, Any]:
        """Detect / reconcile drift between forked custom protocols and system.

        Reads the provenance sidecar ``custom/.forked-from.json`` (written by
        :meth:`customize_protocol`). For each recorded file it re-hashes the
        current system source; when the hash differs from the fork-time hash the
        upstream changed. In detect mode (``acknowledge=False``) each changed
        system file is staged next to its custom copy as ``new-<basename>`` and
        reported so the user can merge. Per-file ``status`` values: ``changed``
        (staged), ``system-removed`` (upstream gone), ``custom-removed`` (the
        user deleted their fork — nothing to merge into, so nothing is staged),
        ``unknown-baseline`` (a pre-existing fork with no recorded fork point).

        With ``acknowledge=True`` (call after merging) each staged ``new-`` file
        is removed and the baseline is reset to the *staged* snapshot the user
        merged against — not the live system — closing the detect->acknowledge
        TOCTOU window. Records whose upstream source was removed are dropped so
        they stop being reported forever. Custom protocols with no sidecar entry
        are reported as ``unknown``-provenance. A corrupt sidecar aborts (it is
        never silently reset). MUST run in the MCP server process (reads the
        protected ``system/`` tree).
        """
        import hashlib

        cfg = self.project_root / "team-management" / "protocol-configs"
        custom_dir = cfg / "custom"
        sidecar = custom_dir / ".forked-from.json"

        sidecar_data, err = self._read_provenance_sidecar(sidecar)
        if err:
            return {"success": False, "error": err}

        def _sha(p):
            return hashlib.sha256(p.read_bytes()).hexdigest()

        if acknowledge:
            removed, acknowledged = [], []
            staged_hashes = {}  # staged path -> snapshot hash, computed once per file
            for proto, entry in sidecar_data.items():
                if not isinstance(entry, dict):
                    continue
                surviving, touched = [], False
                for rec in entry.get("files", []):
                    if not isinstance(rec, dict):
                        surviving.append(rec)
                        continue
                    rel_custom, rel_system = rec.get("custom"), rec.get("system")
                    if not rel_custom or not rel_system:
                        surviving.append(rec)
                        continue
                    custom_path = self.project_root / rel_custom
                    system_path = self._decode_system_source(rel_system)
                    staged = custom_path.parent / ("new-" + custom_path.name)
                    staged_key = str(staged)
                    # Hash + unlink each staged file ONCE; every protocol that
                    # shares it reuses that same snapshot, so a 2nd upstream
                    # change between detect and acknowledge is never adopted.
                    if staged_key in staged_hashes:
                        staged_hash = staged_hashes[staged_key]
                    elif staged.exists():
                        staged_hash = _sha(staged)
                        staged_hashes[staged_key] = staged_hash
                        staged.unlink()
                        removed.append(str(staged.relative_to(self.project_root)))
                    else:
                        staged_hash = None
                    if not system_path.exists():
                        # Upstream removed the source -> drop the record so it
                        # stops being reported as drifted on every future run.
                        touched = True
                        continue
                    if staged_hash is not None:
                        # Baseline = the snapshot the user merged against.
                        rec["sha256"] = staged_hash
                        touched = True
                    elif rec.get("sha256") is not None:
                        # Refresh a known baseline to current system (no-op if
                        # unchanged); leave an unknown (None) baseline untouched
                        # so an unreviewed manual fork is not silently adopted.
                        rec["sha256"] = _sha(system_path)
                    surviving.append(rec)
                entry["files"] = surviving
                if touched:
                    acknowledged.append(proto)
            _write_json_durable(sidecar, sidecar_data)
            return {
                "success": True,
                "acknowledged": acknowledged,
                "removed_staging": removed,
                "message": (f"Refreshed provenance for {len(acknowledged)} protocol(s); "
                            f"removed {len(removed)} staged file(s)."),
            }

        drifted = []
        for proto, entry in sidecar_data.items():
            if not isinstance(entry, dict):
                continue
            for rec in entry.get("files", []):
                if not isinstance(rec, dict):
                    continue
                rel_custom, rel_system = rec.get("custom"), rec.get("system")
                if not rel_custom or not rel_system:
                    continue
                baseline = rec.get("sha256")
                custom_path = self.project_root / rel_custom
                system_path = self._decode_system_source(rel_system)
                if baseline is None:
                    # Pre-existing fork with unknown fork point — surface, don't stage.
                    drifted.append({"protocol": proto, "custom": rel_custom,
                                    "staged": None, "status": "unknown-baseline"})
                    continue
                if not system_path.exists():
                    drifted.append({"protocol": proto, "custom": rel_custom,
                                    "staged": None, "status": "system-removed"})
                    continue
                if _sha(system_path) != baseline:
                    if not custom_path.exists():
                        # User deleted their custom copy — nothing to merge into;
                        # don't recreate the dir or stage an orphan new- file.
                        drifted.append({"protocol": proto, "custom": rel_custom,
                                        "staged": None, "status": "custom-removed"})
                        continue
                    staged = custom_path.parent / ("new-" + custom_path.name)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    staged.write_bytes(system_path.read_bytes())
                    drifted.append({"protocol": proto, "custom": rel_custom,
                                    "staged": str(staged.relative_to(self.project_root)),
                                    "status": "changed"})

        unknown = []
        if custom_dir.exists():
            for jf in sorted(custom_dir.glob("*.json")):
                if jf.name.startswith(("new-", ".")):
                    continue
                if jf.stem not in sidecar_data:
                    unknown.append(jf.stem)

        if drifted:
            msg = (f"{len(drifted)} file(s) drifted from system. Review each staged "
                   f"'new-<file>' next to your custom copy, merge the changes you want, "
                   f"then call protocol_check_drift(acknowledge=True).")
        else:
            msg = "No drift: all forked custom protocols match the current system."
        return {"success": True, "drifted": drifted, "unknown": unknown, "message": msg}

    def get_available_funcs(self) -> Dict[str, Any]:
        """List all available functions for pre_funcs/post_funcs in protocol configs.

        Returns structured metadata for each function including description,
        typical usage, required args, and side effects. Includes cross-validation
        against the actual handler registry.
        """
        # Canonical metadata for all registered functions
        func_metadata = [
            {
                "name": "auto_detect_task",
                "description": "Auto-detect task from current git branch.",
                "typical_usage": "pre_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": ["Sets task state if branch matches a task file"],
            },
            {
                "name": "verify_branch_and_task",
                "description": "Verify git branch matches task state and task file exists. Non-blocking (warnings only).",
                "typical_usage": "pre_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": [],
            },
            {
                "name": "create_task_file",
                "description": "Create/validate the task markdown file. Pass full markdown inline for small files, OR (preferred for substantial files) write the file to team-management/tasks/ first and pass task_content=\"\" — the empty-content path re-validates the on-disk file (frontmatter/status/prefix/branch/## Success Criteria/no unresolved NEEDS-CLARIFICATION markers) instead of trusting existence.",
                "typical_usage": "post_func",
                "required_args": ["task", "task_content"],
                "optional_args": ["branch"],
                "reads_task_state": False,
                "side_effects": ["Creates or updates task file in team-management/tasks/"],
            },
            {
                "name": "set_task_state",
                "description": "Set current_task.json state and rename pending protocol log.",
                "typical_usage": "post_func",
                "required_args": ["task"],
                "optional_args": ["branch"],
                "reads_task_state": False,
                "side_effects": [
                    "Writes to .claude/state/current_task.json",
                    "Renames _pending.json log to {task}.json",
                ],
            },
            {
                "name": "git_setup_branch",
                "description": "Create and checkout git branch for the task. Returns needs_confirmation=true if uncommitted changes exist; re-run with carry_changes=true to carry them into the new branch.",
                "typical_usage": "post_func",
                "required_args": ["branch"],
                "optional_args": ["carry_changes"],
                "reads_task_state": False,
                "side_effects": [
                    "Creates git branch from default branch",
                    "Checks out the new or existing branch",
                    "Pulls latest from remote (non-fatal if offline)",
                ],
            },
            {
                "name": "create_issue_if_enabled",
                "description": "Create provider issue if issue tracking is enabled in config.",
                "typical_usage": "post_func",
                "required_args": ["task"],
                "reads_task_state": False,
                "side_effects": ["Creates issue on configured provider (GitLab/GitHub/Jira)"],
            },
            {
                "name": "update_task_status_in_progress",
                "description": "Update task markdown file status frontmatter to in-progress.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": [
                    "Updates status field in task file frontmatter",
                    "Adds started date if not present",
                ],
            },
            {
                "name": "require_spec_review_passed",
                "description": "Require SPEC_REVIEW: PASSED note in the task's protocol log. Use as pre_func for entry-time reminder and as post_func for structural block on advance-out of code-review.",
                "typical_usage": "pre_func | post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": [],
            },
            {
                "name": "check_completion_evidence",
                "description": "Required-acknowledgment: block advance if summary lacks verification evidence (fenced block, N/N passed, exit 0, or escape hatch 'no-verification-applicable: <reason>'). Reads args['advance_summary'] auto-injected by advance_step.",
                "typical_usage": "post_func",
                "required_args": ["advance_summary (auto-injected by advance_step)"],
                "reads_task_state": False,
                "side_effects": [],
            },
            {
                "name": "verify_tests_pass",
                "description": "Optional test gate for code-review step. Reads test_command from config.json; if null/absent, skips. Enforces metacharacter block + prefix allowlist on raw string BEFORE shlex.split, runs with shell=False. Non-zero exit blocks advance. MUST be wired in a step's post_funcs (not pre_funcs) with post_funcs_stop_on_failure: true to actually gate — advance_step does not check pre_funcs success.",
                "typical_usage": "post_func (pre_funcs_placement_is_cosmetic)",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": ["Executes the configured test command in project_root"],
            },
            {
                "name": "present_completion_options",
                "description": "Pre-func for completion step: when issue_tracking.provider == 'disabled', returns a 4-option menu (merge_local / push_pr / keep / discard). Otherwise returns skipped so the provider-driven flow runs unchanged.",
                "typical_usage": "pre_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": [],
            },
            {
                "name": "require_discard_confirmation",
                "description": "Post-func gate for completion step discard option. Two-step typed confirmation (dry-run + args['discard_confirmation']='discard' + args['discard_confirmed_dry_run']=True) before the dispatcher force-deletes. FRICTION, NOT SECURITY — LLM can trivially produce the string.",
                "typical_usage": "post_func",
                "required_args": [],
                "optional_args": ["completion_option", "discard_confirmation", "discard_confirmed_dry_run"],
                "reads_task_state": True,
                "side_effects": [],
            },
            {
                "name": "completion_dispatch",
                "description": "Post-func for completion step. Replaces the old straight-line completion chain. Provider != 'disabled' → runs the provider-driven chain (archive → commit → merge → push → MR → issue status → cleanup → checkout). Provider == 'disabled' → dispatches args['completion_option'] to merge_local / push_pr / keep / discard flows.",
                "typical_usage": "post_func",
                "required_args": [],
                "optional_args": ["completion_option"],
                "reads_task_state": True,
                "side_effects": [
                    "Commits, pushes, merges, or deletes branches depending on dispatch path",
                    "Archives task file (all paths except discard)",
                    "Clears task state (all paths)",
                ],
            },
            {
                "name": "validate_code_review_in_worklog",
                "description": "Validate that code review results are appended to the task work log.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": [],
            },
            {
                "name": "validate_no_critical_issues_in_worklog",
                "description": "Parse the latest '# Code Review:' block in the task work log and fail if it reports any critical issues.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": [],
            },
            {
                "name": "git_commit",
                "description": "Stage and commit changes with task-derived message.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": [
                    "Stages changed files (excluding sensitive configs)",
                    "Creates git commit with task-derived message",
                ],
            },
            {
                "name": "git_merge_main",
                "description": "Fetch and merge default branch (main/master) into current branch. Returns conflict details on failure.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": [
                    "Fetches latest from remote default branch",
                    "Merges origin/main (or master) into current branch",
                ],
            },
            {
                "name": "git_push",
                "description": "Push current branch to remote with upstream tracking.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": ["Pushes branch to remote with -u flag"],
            },
            {
                "name": "create_merge_request",
                "description": "Create MR/PR linked to provider issue.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": ["Creates merge/pull request on configured provider"],
            },
            {
                "name": "update_issue_status",
                "description": "Update provider issue status to completed/closed.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": ["Updates issue state on configured provider"],
            },
            {
                "name": "archive_task",
                "description": "Archive task file (any protocol type) to tasks/done/.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": ["Moves task file or directory to tasks/done/"],
            },
            {
                "name": "cleanup_task_scoped_state",
                "description": "Remove task-scoped state directory (.claude/state/tasks/{task}/).",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": ["Deletes task-scoped state directory"],
            },
            {
                "name": "clear_task_state",
                "description": "Reset current_task.json to null state.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": ["Clears .claude/state/current_task.json"],
            },
            {
                "name": "checkout_default_branch",
                "description": "Switch back to main/master branch after task completion.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": ["Checks out main or master branch"],
            },
            {
                "name": "capture_test_baseline",
                "description": "Save test baseline snapshot (command and summary) for regression verification.",
                "typical_usage": "post_func",
                "required_args": ["test_command", "baseline_summary"],
                "reads_task_state": False,
                "side_effects": ["Writes to .claude/state/test-baseline.json"],
            },
            {
                "name": "load_test_baseline",
                "description": "Load test baseline snapshot for regression comparison. Fails if no baseline exists.",
                "typical_usage": "pre_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": [],
            },
            {
                "name": "cleanup_test_baseline",
                "description": "Remove test baseline file after refactoring completion. Idempotent.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": ["Removes .claude/state/test-baseline.json"],
            },
            # Optimize-protocol funcs (T2: h-optimize-protocol-engine)
            {
                "name": "log_experiment_result",
                "description": "Engine-owned experimentation row writer. Always invokes _func_run_metric() to measure metric_value/run_count/wall_clock_s/aggregator on HEAD — LLM-passed metric values are NOT trusted (the engine is the single source of truth for the leaderboard). Pre-checks dirty working tree (git status --porcelain) and refuses with a suggested 'iter<N>: <hypothesis>' commit message if non-empty. On run_metric failure, returns success=False with stage='run_metric' and propagates raw_outputs — no TSV row written. Backup rotation: copies TSV to .results.tsv.bak every 100 data rows. commit_sha falls back to 'git rev-parse --short HEAD' when omitted/empty/'-'; writes '-' only when git is also unavailable.",
                "typical_usage": "post_func",
                "required_args": [],
                "optional_args": ["hypothesis", "commit_sha", "iteration_override"],
                "reads_task_state": True,
                "side_effects": ["Invokes _func_run_metric (subprocess)", "Appends to results.tsv on success", "May rotate backup to .results.tsv.bak"],
            },
            {
                "name": "run_metric",
                "description": "Run the configured metric command N times (runs_per_iteration) under shell=False with a filtered subprocess env (allowlist + credential-pattern stripping); parse each stdout via metric_parser regex; aggregate via 'median'/'mean'/'min'/'max'.",
                "typical_usage": "pre_func | post_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": ["Executes metric_command in project_root with filtered env"],
            },
            {
                "name": "validate_metric_script",
                "description": "Pre-flight metric-script validation: run twice, assert exit 0 + parser yields float + values within stability_threshold_pct (default 5%). Used in the metric-script step before experimentation begins.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": ["Executes metric_command twice"],
            },
            {
                "name": "capture_metric_baseline",
                "description": "Run the metric once on HEAD; persist baseline_metric, baseline_wall_clock_s, baseline_commit into optimize-state.json; write optimize.baseline_commit into task frontmatter (used by check_cost_estimate and policy_compliance_audit).",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": ["Updates .claude/state/optimize-state.json", "Writes optimize.baseline_commit to task frontmatter"],
            },
            {
                "name": "check_cost_estimate",
                "description": "Post-func at end of `setup`. Computes wall-clock cost projection (baseline_wall_clock_s × runs_per_iteration × max_iterations). Unbounded mode (both null) requires args['unbounded_acknowledged']='i-accept-unbounded-cost'.",
                "typical_usage": "post_func",
                "required_args": [],
                "optional_args": ["unbounded_acknowledged"],
                "reads_task_state": False,
                "side_effects": [],
            },
            {
                "name": "check_termination",
                "description": "Post-func after each experimentation iteration. Checks four conditions in order (max_iterations / max_duration / regression_halt_n / target_metric); returns the first match as structured reason. Appends summary row to results.tsv on terminate.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": ["Appends summary row to results.tsv on terminate"],
            },
            {
                "name": "update_best_commit",
                "description": "Post-func after each iteration. Falls back to last results.tsv ok-row metric_value and `git rev-parse --short HEAD` commit_sha when args omitted (the protocol-wired case). Explicit args override. If new metric improves on the recorded best (per metric_direction), rewrites optimize.best_commit and optimize.best_metric in task frontmatter. First iteration always sets the best.",
                "typical_usage": "post_func",
                "required_args": [],
                "optional_args": ["metric_value", "commit_sha"],
                "reads_task_state": True,
                "side_effects": ["Writes optimize.best_commit and optimize.best_metric to task frontmatter on improvement"],
            },
            {
                "name": "policy_compliance_audit",
                "description": "Synthesis-step best-effort heuristic audit: scans git log/diff between optimize.baseline_commit and HEAD for (a) frozen-path edits, (b) hardcoded best_metric constants in added lines, (c) results.tsv edits inside hypothesis commits. NEVER blocks advance — findings injected into code-review prompt as metric_gaming_flags.",
                "typical_usage": "post_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": [],
            },
            {
                "name": "batch_checkpoint",
                "description": "B-only post-func used by `optimize` (interactive). Modulo-gated: only fires at batch boundaries (`(loop_iteration + 1) % batch_size == 0`); mid-batch returns success=True (no-op). At the boundary: switches DAIC mode to discussion, returns last-batch + best-so-far summary, and blocks advance with success=False until args['approve_next_batch']=True. Wire with post_funcs_stop_on_failure: true to actually gate.",
                "typical_usage": "post_func",
                "required_args": [],
                "optional_args": ["approve_next_batch"],
                "reads_task_state": True,
                "side_effects": ["Switches DAIC mode to discussion at batch boundary"],
            },
            {
                "name": "validate_optimize_setup",
                "description": "Early-gate post_func of the optimize protocols' `setup` step. Validates user-provided settings (same surface as write_optimize_setup) WITHOUT writing — so a validation failure aborts before git_setup_branch / create_task_file / create_issue_if_enabled create durable side effects. Pair with write_optimize_setup as the last post_func.",
                "typical_usage": "post_func",
                "required_args": ["metric_command", "metric_parser", "metric_direction", "metric_monotonic"],
                "optional_args": ["frozen_paths", "env_pass", "runs_per_iteration", "aggregator", "stability_threshold_pct", "max_iterations", "max_duration", "target_metric", "regression_halt_n", "batch_size"],
                "reads_task_state": False,
                "side_effects": [],
            },
            {
                "name": "write_optimize_setup",
                "description": "LAST post_func of the optimize protocols' `setup` step. Re-validates settings defensively then persists to .claude/state/optimize-state.json via shared_state.write_optimize_state. Required: metric_command, metric_parser, metric_direction, metric_monotonic. Optional fields fall back to T1 schema defaults (frozen_paths=[], env_pass=[], runs_per_iteration=1, aggregator='median', stability_threshold_pct=5.0, max_iterations=50, max_duration='8h' → 28800s, target_metric=None, regression_halt_n=5, batch_size=3). max_duration accepts numeric seconds or '<N>h'/'<N>m'/'<N>s' shorthand and is normalised before persisting. metric_monotonic must be a strict bool; metric_monotonic=false is rejected (v1 contract). frozen_paths and env_pass must be lists. Closes the T2 gap where `setup` had no func to write user settings (the file is in PROTECTED_PATHS).",
                "typical_usage": "post_func",
                "required_args": ["metric_command", "metric_parser", "metric_direction", "metric_monotonic"],
                "optional_args": ["frozen_paths", "env_pass", "runs_per_iteration", "aggregator", "stability_threshold_pct", "max_iterations", "max_duration", "target_metric", "regression_halt_n", "batch_size"],
                "reads_task_state": False,
                "side_effects": ["Writes .claude/state/optimize-state.json"],
            },
            {
                "name": "wiki_update_reminder",
                "description": "Check if LLM Wiki is enabled and inject a reminder to update wiki pages during the documentation step. Skips silently when wiki is not configured or wiki/ directory is absent.",
                "typical_usage": "pre_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": [],
            },
        ]

        # AI provider resolver metadata — registry-driven for future phase additions.
        # Each entry surfaces phase_key, config_flag, and protocol_json_steps so
        # contributors can see where the func is wired and what flag it reads.
        for phase_key, entry in _PHASE_REGISTRY.items():
            wiring = ", ".join(f"{pf}:{step}" for pf, step in entry["protocol_json_steps"])
            func_metadata.append({
                "name": entry["func_name"],
                "description": (
                    f"Read config and return AI provider Task-agent launch instructions "
                    f"for {entry['description']} phase (phase_key={phase_key}, "
                    f"config_flag=ai_providers.{entry['config_flag']}). "
                    f"Wired in: {wiring or 'unwired'}."
                ),
                "typical_usage": "pre_func",
                "required_args": [],
                "reads_task_state": True,
                "side_effects": [],
            })

        # Cross-validate metadata against actual handler registry
        handler_keys = set(self._build_handlers().keys())
        metadata_keys = {f["name"] for f in func_metadata}

        discrepancies = {}
        missing_metadata = handler_keys - metadata_keys
        missing_handlers = metadata_keys - handler_keys
        if missing_metadata:
            discrepancies["handlers_without_metadata"] = sorted(missing_metadata)
        if missing_handlers:
            discrepancies["metadata_without_handlers"] = sorted(missing_handlers)

        # Discover custom funcs
        custom_funcs = self._discover_custom_funcs()
        custom_metadata = []
        for name, callable_obj in sorted(custom_funcs.items()):
            doc = getattr(callable_obj, "__doc__", None) or ""
            first_line = doc.strip().split("\n")[0] if doc.strip() else "Custom function (no docstring)"
            custom_metadata.append({
                "name": f"custom({name})",
                "description": first_line,
                "typical_usage": "pre_func or post_func",
                "required_args": [],
                "reads_task_state": False,
                "side_effects": [],
            })

        result = {
            "success": True,
            "total": len(func_metadata) + len(custom_metadata),
            "funcs": func_metadata,
        }
        if custom_metadata:
            result["custom_funcs"] = custom_metadata
        if discrepancies:
            result["discrepancies"] = discrepancies

        return result

    def get_protocol_config(self, name: str) -> Optional[Dict]:
        """Load protocol config by name. Delegates to shared_state."""
        return load_protocol_config(name)

    def get_current(self) -> Dict[str, Any]:
        """Get full current protocol state with steps overview."""
        protocol_info = get_protocol_state()
        if not protocol_info:
            return {
                "success": True,
                "active": False,
                "message": "No protocol active. Use protocol_list() to see available protocols.",
            }

        protocol_name = protocol_info["name"]
        config = load_protocol_config(protocol_name)
        if not config:
            return {
                "success": False,
                "error": f"Protocol config '{protocol_name}' not found.",
            }

        steps = config.get("steps", [])
        idx = protocol_info.get("current_step", 0)

        # Build steps overview
        steps_overview = []
        for i, step in enumerate(steps):
            if i < idx:
                status = "completed"
            elif i == idx:
                status = "current"
            else:
                status = "pending"
            steps_overview.append({
                "index": i,
                "name": step["name"],
                "description": step.get("description", ""),
                "mode": step.get("mode", "discussion"),
                "status": status,
            })

        # Resolve start text for current step
        start_text = ""
        end_text = ""
        if idx < len(steps):
            step = steps[idx]
            start_text = self._resolve_start_text(step.get("start", ""), protocol_name)
            end_text = step.get("end", "")

        task_state = get_task_state()

        return {
            "success": True,
            "active": True,
            "protocol": protocol_name,
            "step": {
                "index": idx,
                "name": protocol_info.get("step_name", ""),
                "mode": steps[idx].get("mode", "discussion") if idx < len(steps) else "discussion",
                "total_steps": len(steps),
            },
            "start": start_text,
            "end": end_text,
            "started_at": protocol_info.get("started_at", ""),
            "task": task_state.get("task"),
            "steps_overview": steps_overview,
        }

    def get_log(self, task_name: str = None) -> Dict[str, Any]:
        """Get protocol log for a task."""
        if not task_name:
            task_state = get_task_state()
            task_name = task_state.get("task")
        if not task_name:
            return {"success": False, "error": "No task specified and no active task."}

        log_data = get_protocol_log(task_name)
        if not log_data:
            return {"success": False, "error": f"No protocol log found for task '{task_name}'."}

        return {"success": True, "task": task_name, "log": log_data}

    # ========================================================================
    # WRITE METHODS (MCP-only)
    # ========================================================================

    def start_protocol(self, name: str, task: str = None,
                       resume_force_safe: bool = False) -> Dict[str, Any]:
        """Start a new protocol.

        Auto-resume: if a protocol with the same name is already active AND
        its loop_iteration > 0, resume the session at the current step
        instead of erroring. This is the path taken on a session restart
        after compaction. A credential-pattern scan runs first; on match,
        the resume aborts and writes resume-blocked.txt unless
        `resume_force_safe=True` overrides (the override is recorded in the
        protocol audit log).
        """
        # Check if protocol already active
        existing = get_protocol_state()
        if existing:
            same_name = existing.get("name") == name
            loop_iter = existing.get("loop_iteration", 0)
            is_resume = same_name and loop_iter > 0

            if not is_resume:
                step_idx = existing.get("current_step", 0)
                step_name = existing.get("step_name", "unknown")
                return {
                    "success": False,
                    "error": (
                        f"Protocol '{existing['name']}' already active at step "
                        f"{step_idx + 1} ('{step_name}'). Use protocol_abort() first."
                    ),
                }

            # Resume path
            return self._resume_protocol(existing, name, resume_force_safe)

        # Load protocol config
        config = load_protocol_config(name)
        if not config:
            available = self.list_protocols()
            names = [p["name"] for p in available.get("protocols", [])]
            return {
                "success": False,
                "error": f"Protocol '{name}' not found. Available: {names}",
            }

        steps = config.get("steps", [])
        if not steps:
            return {"success": False, "error": f"Protocol '{name}' has no steps."}

        # Handle explicit task parameter — validate existing task file
        existing_task_info = None
        if task:
            tasks_dir = self.project_root / "team-management" / "tasks"
            task_file = tasks_dir / f"{task}.md"
            if not task_file.exists():
                task_file = tasks_dir / task / "README.md"
            if not task_file.exists():
                return {
                    "success": False,
                    "error": f"Task file not found for '{task}'. Looked for {task}.md and {task}/README.md in team-management/tasks/.",
                }
            # Read content and parse frontmatter
            task_content = task_file.read_text(encoding="utf-8")
            frontmatter = parse_task_frontmatter(task)
            existing_task_info = {
                "task": task,
                "path": str(task_file.relative_to(self.project_root)),
                "content": task_content,
                "frontmatter": frontmatter,
            }
            # Store for auto_detect_task to use
            self._explicit_task = task

        first_step = steps[0]
        now = datetime.now(timezone.utc).isoformat()

        # Set DAIC mode from first step
        self._set_daic_for_step(first_step)

        # Set protocol state in current_task.json
        set_protocol_state(name, 0, first_step["name"], now)

        # Create protocol log as _pending.json (task name unknown at start)
        log_data = {
            "protocol": name,
            "started_at": now,
            "completed_at": None,
            "steps": [],
            "gotos": [],
            "notes": [],
        }
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        pending_log = self.logs_dir / "_pending.json"
        _write_json_durable(pending_log, log_data, ensure_ascii=False)

        # Execute pre_funcs of first step
        pre_funcs = first_step.get("pre_funcs", [])
        try:
            pre_funcs_results = self.execute_funcs(pre_funcs)
        finally:
            # Clear explicit task after pre_funcs (auto_detect_task may have used it)
            self._explicit_task = None

        # Resolve start text
        start_text = self._resolve_start_text(first_step.get("start", ""), name)

        # If existing task provided, prepend preamble to start text
        if existing_task_info:
            content_preview = existing_task_info['content']
            max_content_len = 10000
            if len(content_preview) > max_content_len:
                content_preview = content_preview[:max_content_len] + (
                    f"\n\n... (truncated, {len(existing_task_info['content'])} chars total. "
                    f"Read `{existing_task_info['path']}` for full content.)"
                )
            preamble = (
                f"## Existing Task: {existing_task_info['task']}\n\n"
                f"**This task already exists** at `{existing_task_info['path']}`.\n\n"
                f"Review the existing content below. Your goal is to **review and enrich** "
                f"the task — not create it from scratch. Ask clarifying questions, suggest "
                f"improvements, and update the content if needed. If no changes are needed, "
                f"pass `task_content=\"\"` when advancing to skip overwriting the file.\n\n"
                f"### Current Task Content\n\n"
                f"```markdown\n{content_preview}\n```\n\n"
                f"---\n\n"
            )
            start_text = preamble + start_text

        response = {
            "success": True,
            "protocol": name,
            "step": {
                "index": 0,
                "name": first_step["name"],
                "mode": first_step.get("mode", "discussion"),
                "total_steps": len(steps),
            },
            "start": start_text,
            "end": first_step.get("end", ""),
            "pre_funcs_results": pre_funcs_results,
        }

        if existing_task_info:
            response["existing_task"] = {
                "task": existing_task_info["task"],
                "path": existing_task_info["path"],
                "frontmatter": existing_task_info["frontmatter"],
            }

        return response

    def advance_step(self, summary: str, args: Dict = None) -> Dict[str, Any]:
        """Advance to the next protocol step."""
        if not summary or not summary.strip():
            return {
                "success": False,
                "error": "Summary is required. Describe what was accomplished in this step.",
            }

        protocol_info = get_protocol_state()
        if not protocol_info:
            return {"success": False, "error": "No protocol active. Nothing to advance."}

        protocol_name = protocol_info["name"]
        config = load_protocol_config(protocol_name)
        if not config:
            return {"success": False, "error": f"Protocol config '{protocol_name}' not found."}

        steps = config.get("steps", [])
        current_index = protocol_info.get("current_step", 0)

        if current_index >= len(steps):
            return {"success": False, "error": "Protocol already completed."}

        current_step = steps[current_index]
        args = args or {}

        # 1. Validate advance_args
        validation_error = self._validate_advance_args(current_step, args)
        if validation_error:
            return {"success": False, "error": validation_error}

        # 1b. Restart branch — only valid on a looping_step. Skips the
        # current iteration's post_funcs (we are abandoning the iteration's
        # data anyway), archives results.tsv, clears optimize.best_commit,
        # resets loop_iteration to 0 and experimentation_started_at to now,
        # and re-runs pre_funcs of the same step.
        if args.get("restart") is True:
            if not current_step.get("looping_step"):
                return {
                    "success": False,
                    "error": (
                        f"args['restart'] is only valid on a step with looping_step=true. "
                        f"Current step '{current_step['name']}' is not a looping step."
                    ),
                }
            return self._handle_loop_restart(
                protocol_info, protocol_name, current_step, current_index, summary, args, steps,
            )

        # 2. Capture task name BEFORE post_funcs (which may clear_task_state)
        task_state_before = get_task_state()
        task_name_before = task_state_before.get("task")

        # 3. Execute post_funcs of CURRENT step (with args + injected advance_summary)
        # post_funcs that need the advance summary (e.g. _func_check_completion_evidence)
        # read it from args["advance_summary"]. Existing funcs ignore the extra key.
        post_funcs = current_step.get("post_funcs", [])
        stop_on_failure = current_step.get("post_funcs_stop_on_failure", False)
        args_for_post = dict(args or {})
        args_for_post["advance_summary"] = summary
        post_funcs_results = self.execute_funcs(post_funcs, args=args_for_post, stop_on_failure=stop_on_failure)

        # 4. Check if chain was stopped
        if post_funcs_results and isinstance(post_funcs_results[-1], dict) and post_funcs_results[-1].get("chain_stopped"):
            return {
                "success": False,
                "error": "Post-func chain stopped due to failure. Protocol NOT advanced.",
                "post_funcs_results": post_funcs_results,
                "step": {"index": current_index, "name": current_step["name"]},
            }

        # 5. Re-read task state AFTER post_funcs so logs/notifications reflect
        #    task-state changes they may have made. Prefer the fresh task name
        #    (set_task_state created a new task); fall back to the pre-funcs
        #    snapshot if state was cleared (e.g. task-completion).
        task_state_after = get_task_state()
        task_name_for_log = task_state_after.get("task") or task_name_before

        # 6. Log current step completion
        self._log_step(
            current_step["name"], summary, post_funcs_results,
            started_at=protocol_info.get("started_at"),
            task_name=task_name_for_log,
        )

        # 7. Update protocol_step in task file frontmatter
        self._update_task_protocol_step(current_step["name"])

        # 7b. Looping branch — re-run pre_funcs of the SAME step instead of
        # advancing. Triggered when current_step has looping_step=true AND
        # neither args["exit_loop"] nor a post_func returning terminate=true
        # asked to break out. Each iteration is a normal advance call in the
        # audit log; only the index is held fixed.
        if current_step.get("looping_step") and not args.get("exit_loop"):
            terminate_signal = any(
                isinstance(r, dict) and r.get("terminate") is True
                for r in post_funcs_results
            )
            if not terminate_signal:
                return self._handle_loop_iteration(
                    protocol_info, protocol_name, current_step, current_index,
                    summary, post_funcs_results, task_name_for_log, steps,
                )

        # 8. Check if this was the last step
        next_index = current_index + 1
        is_last = next_index >= len(steps)

        if is_last:
            # Send notification for protocol completion (non-blocking)
            if not current_step.get("skip_notification", False):
                try:
                    from notification_utils import send_protocol_notification
                    send_protocol_notification(protocol_name, current_index, current_step["name"], len(steps), is_complete=True, summary=summary, task_name=task_name_for_log or "")
                except Exception:
                    pass

            # Complete the protocol (pass task name since state may be cleared)
            self._complete_protocol(task_name=task_name_for_log)
            return {
                "success": True,
                "previous_step": {
                    "index": current_index,
                    "name": current_step["name"],
                    "summary": summary,
                },
                "protocol_complete": True,
                "message": f"Protocol '{protocol_name}' completed. All {len(steps)} steps finished.",
                "post_funcs_results": post_funcs_results,
                "log_file": str(self._get_log_path(task_name=task_name_for_log)),
            }

        # 9. Advance to next step
        next_step = steps[next_index]
        now = datetime.now(timezone.utc).isoformat()
        set_protocol_state(protocol_name, next_index, next_step["name"], protocol_info.get("started_at", now))

        # 10. Set DAIC mode from next step
        self._set_daic_for_step(next_step)

        # 11. Execute pre_funcs of NEXT step
        pre_funcs = next_step.get("pre_funcs", [])
        pre_funcs_results = self.execute_funcs(pre_funcs)

        # 12. Resolve start text
        start_text = self._resolve_start_text(next_step.get("start", ""), protocol_name)

        # 13. Send notification for completed step (non-blocking)
        if not current_step.get("skip_notification", False):
            try:
                from notification_utils import send_protocol_notification
                send_protocol_notification(protocol_name, current_index, current_step["name"], len(steps), summary=summary, task_name=task_name_for_log or "")
            except Exception:
                pass

        return {
            "success": True,
            "previous_step": {
                "index": current_index,
                "name": current_step["name"],
                "summary": summary,
            },
            "post_funcs_results": post_funcs_results,
            "pre_funcs_results": pre_funcs_results,
            "step": {
                "index": next_index,
                "name": next_step["name"],
                "mode": next_step.get("mode", "discussion"),
                "total_steps": len(steps),
            },
            "start": start_text,
            "end": next_step.get("end", ""),
            "protocol_complete": False,
        }

    def _handle_loop_iteration(self, protocol_info, protocol_name, current_step,
                               current_index, summary, post_funcs_results,
                               task_name_for_log, steps) -> Dict[str, Any]:
        """Re-run pre_funcs of the same step. Increments loop_iteration and
        preserves experimentation_started_at across iterations."""
        prev_iter = protocol_info.get("loop_iteration", 0)
        new_iter = prev_iter + 1
        started_at = protocol_info.get("experimentation_started_at") or \
                     datetime.now(timezone.utc).isoformat()

        set_protocol_state(
            protocol_name, current_index, current_step["name"],
            protocol_info.get("started_at", ""),
            extra={
                "loop_iteration": new_iter,
                "experimentation_started_at": started_at,
            },
        )

        # Mode is still the same step's mode — re-set in case any post_func
        # toggled it (e.g. batch_checkpoint switching to discussion).
        self._set_daic_for_step(current_step)

        # Re-execute pre_funcs of SAME step
        pre_funcs = current_step.get("pre_funcs", [])
        pre_funcs_results = self.execute_funcs(pre_funcs)

        start_text = self._resolve_start_text(current_step.get("start", ""), protocol_name)

        return {
            "success": True,
            "looped": True,
            "loop_iteration": new_iter,
            "experimentation_started_at": started_at,
            "previous_step": {
                "index": current_index,
                "name": current_step["name"],
                "summary": summary,
            },
            "post_funcs_results": post_funcs_results,
            "pre_funcs_results": pre_funcs_results,
            "step": {
                "index": current_index,
                "name": current_step["name"],
                "mode": current_step.get("mode", "discussion"),
                "total_steps": len(steps),
            },
            "start": start_text,
            "end": current_step.get("end", ""),
            "protocol_complete": False,
        }

    def _handle_loop_restart(self, protocol_info, protocol_name, current_step,
                             current_index, summary, args, steps) -> Dict[str, Any]:
        """Reset a looping_step to iteration 0: archive results.tsv to
        results.tsv.run-N, clear optimize.best_commit, reset
        experimentation_started_at, re-run pre_funcs.
        """
        task_state = get_task_state()
        task_name = task_state.get("task")

        archive_result = self._archive_results_tsv(task_name)
        cleared_best_commit = self._clear_optimize_field(task_name, "best_commit")
        # Also clear best_metric — leaving it would cause _func_update_best_commit
        # to compare new iterations against the pre-restart best, blocking new
        # bests from being recorded (Codex round-1 warning).
        self._clear_optimize_field(task_name, "best_metric")

        now = datetime.now(timezone.utc).isoformat()
        set_protocol_state(
            protocol_name, current_index, current_step["name"],
            protocol_info.get("started_at", ""),
            extra={
                "loop_iteration": 0,
                "experimentation_started_at": now,
            },
        )

        # Log restart in audit trail (synthetic post_funcs result so the
        # log step accepts it).
        restart_record = {
            "func": "loop_restart",
            "success": True,
            "archive": archive_result,
            "cleared_best_commit": cleared_best_commit,
        }
        self._log_step(
            current_step["name"],
            f"[RESTART] {summary}",
            [restart_record],
            started_at=protocol_info.get("started_at"),
            task_name=task_name,
        )

        self._set_daic_for_step(current_step)

        pre_funcs = current_step.get("pre_funcs", [])
        pre_funcs_results = self.execute_funcs(pre_funcs)

        start_text = self._resolve_start_text(current_step.get("start", ""), protocol_name)

        return {
            "success": True,
            "restarted": True,
            "loop_iteration": 0,
            "experimentation_started_at": now,
            "archive": archive_result,
            "cleared_best_commit": cleared_best_commit,
            "pre_funcs_results": pre_funcs_results,
            "step": {
                "index": current_index,
                "name": current_step["name"],
                "mode": current_step.get("mode", "discussion"),
                "total_steps": len(steps),
            },
            "start": start_text,
            "end": current_step.get("end", ""),
            "protocol_complete": False,
        }

    def _archive_results_tsv(self, task_name: str) -> Dict:
        """Archive team-management/tasks/<task>/results.tsv to
        results.tsv.run-N where N is highest existing + 1. Idempotent —
        returns {archived: false} when no TSV exists.
        """
        if not task_name:
            return {"archived": False, "reason": "no task name"}
        task_dir = self.project_root / "team-management" / "tasks" / task_name
        tsv_path = task_dir / "results.tsv"
        if not tsv_path.exists():
            return {"archived": False, "reason": "no results.tsv to archive"}
        n = 1
        while (task_dir / f"results.tsv.run-{n}").exists():
            n += 1
        archive_path = task_dir / f"results.tsv.run-{n}"
        try:
            os.replace(str(tsv_path), str(archive_path))
            return {
                "archived": True,
                "path": str(archive_path.relative_to(self.project_root)),
                "run_n": n,
            }
        except (IOError, OSError) as e:
            return {"archived": False, "error": str(e)}

    # Credential regex set for resume safety scan. Best-effort heuristic —
    # not a security control. Bypass via resume_force_safe=True is allowed.
    # Patterns are prefix-anchored to keep false-positive rates low — a bare
    # base64-blob heuristic would match ISO timestamps, hashes, and long
    # hypothesis text, training users to reflexively pass resume_force_safe
    # (Codex round-1 review).
    _CREDENTIAL_REGEX = {
        "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
        "jwt": re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
        "oauth_bearer": re.compile(
            r"(?i)(bearer|oauth|access_token)[:=\s]+[A-Za-z0-9._~+/-]{20,}"
        ),
        # Common GitHub / GitLab / Slack token prefixes — anchored to avoid FPs.
        "github_token": re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,})\b"),
        "gitlab_token": re.compile(r"\b(glpat-[A-Za-z0-9_-]{20,})\b"),
        "slack_token": re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})\b"),
    }

    def _resume_protocol(self, existing: Dict, name: str,
                         resume_force_safe: bool) -> Dict[str, Any]:
        """Auto-resume an active protocol (post-compaction or cross-session
        re-entry). Runs credential-pattern scan over results.tsv and
        resume-stdout-tail.txt; aborts with resume-blocked.txt on match
        unless resume_force_safe=True.
        """
        config = load_protocol_config(name)
        if not config:
            return {
                "success": False,
                "error": f"Protocol config '{name}' not found.",
            }

        steps = config.get("steps", [])
        current_index = existing.get("current_step", 0)
        if current_index >= len(steps):
            return {
                "success": False,
                "error": f"Protocol '{name}' has invalid current_step {current_index} (>= {len(steps)} steps).",
            }

        current_step = steps[current_index]
        task_state = get_task_state()
        task_name = task_state.get("task")

        # Credential scan unless explicitly bypassed
        scan_result = self._resume_credential_scan(task_name) if not resume_force_safe else {"clean": True, "bypassed": True}
        if not scan_result.get("clean") and not resume_force_safe:
            return {
                "success": False,
                "error": (
                    f"Resume blocked: credential pattern detected in task artifacts. "
                    f"Wrote team-management/tasks/{task_name}/resume-blocked.txt with redacted matches. "
                    f"Review the file. To bypass, call protocol_start with resume_force_safe=true."
                ),
                "resume_blocked": True,
                "matches": scan_result.get("matches", []),
            }

        # Re-set DAIC mode for current step (post-compaction may have lost it)
        self._set_daic_for_step(current_step)

        # Resolve start text and prepend resume hint
        loop_iter = existing.get("loop_iteration", 0)
        start_text = self._resolve_start_text(current_step.get("start", ""), name)
        bypass_marker = " (resume_force_safe=true bypass active — credential scan SKIPPED)" if resume_force_safe else ""
        resume_hint = (
            f"## Resuming protocol '{name}' at step '{current_step['name']}' "
            f"(loop_iteration={loop_iter}){bypass_marker}\n\n"
            f"Pass `restart=true` to protocol_advance to start fresh "
            f"(archives results.tsv and resets to iteration 0).\n\n---\n\n"
        )

        # Audit-log the resume (and bypass, if any)
        if task_name:
            note_text = f"RESUME: protocol={name} step={current_step['name']} iter={loop_iter}"
            if resume_force_safe:
                note_text += " (resume_force_safe=true; credential scan BYPASSED)"
            note_entry = {
                "at": datetime.now(timezone.utc).isoformat(),
                "step": current_step["name"],
                "note": note_text,
            }
            try:
                def add_note(log):
                    if "notes" not in log:
                        log["notes"] = []
                    log["notes"].append(note_entry)
                self._update_log(add_note)
            except Exception:
                pass

        return {
            "success": True,
            "protocol": name,
            "resumed": True,
            "loop_iteration": loop_iter,
            "experimentation_started_at": existing.get("experimentation_started_at"),
            "step": {
                "index": current_index,
                "name": current_step["name"],
                "mode": current_step.get("mode", "discussion"),
                "total_steps": len(steps),
            },
            "start": resume_hint + start_text,
            "end": current_step.get("end", ""),
            "scan_result": scan_result,
        }

    def _resume_credential_scan(self, task_name: str) -> Dict:
        """Scan task artifacts (results.tsv last 10 rows, resume-stdout-tail.txt
        last 100 KB / 1000 lines) for credential patterns. Writes
        resume-blocked.txt with redacted matches on hit.

        Returns: {"clean": bool, "matches": [{"file", "line_no", "kind", "redacted"}, ...]}
        """
        if not task_name:
            return {"clean": True, "matches": []}

        matches = []
        task_dir = self.project_root / "team-management" / "tasks" / task_name

        # Scan results.tsv (last 10 rows)
        tsv_path = task_dir / "results.tsv"
        if tsv_path.exists():
            try:
                tsv_text = tsv_path.read_text(encoding="utf-8", errors="replace")
                tsv_lines = tsv_text.splitlines()
                last_lines = tsv_lines[-10:]
                first_ln = max(1, len(tsv_lines) - len(last_lines) + 1)
                for offset, line in enumerate(last_lines):
                    ln = first_ln + offset
                    for kind, regex in self._CREDENTIAL_REGEX.items():
                        m = regex.search(line)
                        if m:
                            redacted = m.group(0)[:6] + "***"
                            matches.append({
                                "file": "results.tsv",
                                "line_no": ln,
                                "kind": kind,
                                "redacted": redacted,
                            })
            except (IOError, OSError):
                pass

        # Scan resume-stdout-tail.txt (last 100 KB / 1000 lines, whichever smaller)
        stdout_path = task_dir / "resume-stdout-tail.txt"
        if stdout_path.exists():
            try:
                data = stdout_path.read_bytes()
                tail = data[-100 * 1024:]
                text = tail.decode("utf-8", errors="replace")
                lines = text.splitlines()[-1000:]
                first_ln = max(1, len(text.splitlines()) - len(lines) + 1)
                for offset, line in enumerate(lines):
                    ln = first_ln + offset
                    for kind, regex in self._CREDENTIAL_REGEX.items():
                        m = regex.search(line)
                        if m:
                            redacted = m.group(0)[:6] + "***"
                            matches.append({
                                "file": "resume-stdout-tail.txt",
                                "line_no": ln,
                                "kind": kind,
                                "redacted": redacted,
                            })
            except (IOError, OSError):
                pass

        # On match, write resume-blocked.txt
        if matches:
            blocked_path = task_dir / "resume-blocked.txt"
            try:
                blocked_path.parent.mkdir(parents=True, exist_ok=True)
                lines_out = [
                    "# Resume blocked: credential pattern detected",
                    "",
                    f"Detected at: {datetime.now(timezone.utc).isoformat()}",
                    "",
                    "Best-effort heuristic — not a security control. Review the matches",
                    "below; if they are false positives or you accept the risk, call",
                    "protocol_start(resume_force_safe=true) to bypass.",
                    "",
                ]
                for m in matches:
                    lines_out.append(
                        f"- {m['kind']} in {m['file']} line {m['line_no']}: {m['redacted']}"
                    )
                blocked_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
            except (IOError, OSError):
                pass

        return {"clean": not matches, "matches": matches}

    def _clear_optimize_field(self, task_name: str, field: str) -> bool:
        """Remove a flat-key `optimize.<field>:` line from task frontmatter.
        Idempotent. Returns True if the line was removed, False otherwise.
        """
        if not task_name:
            return False
        tasks_dir = self.project_root / "team-management" / "tasks"
        task_file = tasks_dir / f"{task_name}.md"
        if not task_file.exists():
            task_file = tasks_dir / task_name / "README.md"
        if not task_file.exists():
            return False
        try:
            content = task_file.read_text(encoding="utf-8")
            if not content.startswith("---"):
                return False
            end_marker = content.find("---", 3)
            if end_marker == -1:
                return False
            frontmatter = content[3:end_marker]
            body = content[end_marker + 3:]
            target_prefix = f"optimize.{field}:"
            lines = frontmatter.split("\n")
            new_lines = [l for l in lines if not l.strip().startswith(target_prefix)]
            if len(new_lines) == len(lines):
                return False  # No matching line found
            new_content = "---" + "\n".join(new_lines) + "---" + body
            task_file.write_text(new_content, encoding="utf-8")
            return True
        except (IOError, OSError):
            return False

    def goto_step(self, step_name: str, reason: str) -> Dict[str, Any]:
        """Go back to a previous step."""
        if not reason or not reason.strip():
            return {"success": False, "error": "Reason is required. Explain why you're going back to this step."}

        protocol_info = get_protocol_state()
        if not protocol_info:
            return {"success": False, "error": "No protocol active. Nothing to navigate."}

        protocol_name = protocol_info["name"]
        config = load_protocol_config(protocol_name)
        if not config:
            return {"success": False, "error": f"Protocol config '{protocol_name}' not found."}

        steps = config.get("steps", [])
        current_index = protocol_info.get("current_step", 0)

        # Find target step by name
        target_index = None
        for i, step in enumerate(steps):
            if step["name"] == step_name:
                target_index = i
                break

        if target_index is None:
            available_names = [s["name"] for s in steps]
            return {
                "success": False,
                "error": f"Step '{step_name}' not found in protocol '{protocol_name}'. Available: {available_names}",
            }

        # Only allow going backward
        if target_index >= current_index:
            return {
                "success": False,
                "error": (
                    f"Cannot goto step '{step_name}' (index {target_index}) from step "
                    f"'{steps[current_index]['name']}' (index {current_index}). Use protocol_advance to move forward."
                ),
            }

        target_step = steps[target_index]

        # Log the goto
        self._log_goto(steps[current_index]["name"], step_name, reason)

        # Set protocol state to target step
        set_protocol_state(
            protocol_name,
            target_index,
            target_step["name"],
            protocol_info.get("started_at", ""),
        )

        # Set DAIC mode from target step
        self._set_daic_for_step(target_step)

        # Do NOT execute pre_funcs (goto is a manual override)

        # Resolve start text
        start_text = self._resolve_start_text(target_step.get("start", ""), protocol_name)

        return {
            "success": True,
            "from_step": {
                "index": current_index,
                "name": steps[current_index]["name"],
            },
            "to_step": {
                "index": target_index,
                "name": step_name,
                "mode": target_step.get("mode", "discussion"),
                "total_steps": len(steps),
            },
            "reason": reason,
            "start": start_text,
            "end": target_step.get("end", ""),
            "message": f"Returned to step '{step_name}'. Fix the issue and advance through remaining steps.",
        }

    def abort_protocol(self, reason: str) -> Dict[str, Any]:
        """Abort the current protocol."""
        if not reason or not reason.strip():
            return {"success": False, "error": "Reason is required."}

        protocol_info = get_protocol_state()
        if not protocol_info:
            return {"success": False, "error": "No protocol active. Nothing to abort."}

        protocol_name = protocol_info["name"]
        step_index = protocol_info.get("current_step", 0)
        step_name = protocol_info.get("step_name", "unknown")
        now = datetime.now(timezone.utc).isoformat()

        # Log abort
        self._update_log(lambda log: log.update({
            "completed_at": now,
            "abort_reason": reason,
            "aborted_at_step": step_name,
        }))

        # Send abort notification (non-blocking)
        try:
            from notification_utils import send_notification
            import html as _html
            task_state = get_task_state()
            _task_name = task_state.get("task", "")
            task_label = f"[{_html.escape(_task_name)}] " if _task_name else ""
            send_notification(
                f"{task_label}aborted at step {step_index + 1} ({_html.escape(step_name)})\n{_html.escape(reason)}",
                category="protocol",
            )
        except Exception:
            pass

        # Clear protocol state
        clear_protocol_state()

        # Reset active-task indicators so the aborted task is no longer considered
        # active. The git working-tree branch and team-management/tasks/<task>.md
        # are preserved on disk as work-in-progress; current_task.json:branch is
        # cleared as part of the identity reset (the system uses the git branch as
        # the source of truth on resume via infer_task_from_branch).
        # optimize-state.json is intentionally NOT removed: the contract in
        # optimize-experimentation-auto.md:75 keeps it on disk for forensic
        # salvage of aborted optimize runs, and the _load_frozen_paths step-gate
        # already ignores stale state from non-active protocols.
        _task_state_for_cleanup = get_task_state()
        _task_name_for_cleanup = _task_state_for_cleanup.get("task")
        cleaned: List[str] = []

        if _task_name_for_cleanup:
            try:
                if cleanup_task_state_on_completion(_task_name_for_cleanup):
                    cleaned.append("task_scoped_state")
            except OSError:
                pass  # abort must not fail on cleanup

        set_task_state(None, None, [])
        cleaned.append("current_task_identity")

        # Set DAIC to discussion (safe default)
        set_daic_mode("discussion")

        return {
            "success": True,
            "aborted_protocol": protocol_name,
            "aborted_at_step": {"index": step_index, "name": step_name},
            "reason": reason,
            "cleaned": cleaned,
            "message": f"Protocol '{protocol_name}' aborted. DAIC mode set to discussion.",
        }

    def save_note(self, note: str) -> Dict[str, Any]:
        """Save a note to the protocol log."""
        if not note or not note.strip():
            return {"success": False, "error": "Note text is required."}

        protocol_info = get_protocol_state()
        if not protocol_info:
            return {"success": False, "error": "No protocol active. Start a protocol first."}

        step_name = protocol_info.get("step_name", "unknown")
        now = datetime.now(timezone.utc).isoformat()

        note_entry = {
            "at": now,
            "step": step_name,
            "note": note.strip(),
        }

        # Append note to log
        def add_note(log):
            if "notes" not in log:
                log["notes"] = []
            log["notes"].append(note_entry)

        self._update_log(add_note)

        return {
            "success": True,
            "message": "Note saved.",
            "note": note_entry,
        }

    # ========================================================================
    # FUNC EXECUTION
    # ========================================================================

    def execute_funcs(self, funcs: List[str], args: Dict = None, stop_on_failure: bool = False) -> List[Dict]:
        """Execute step functions sequentially."""
        if not funcs:
            return []

        results = []
        for func_name in funcs:
            try:
                handler = self._get_func_handler(func_name)
                if handler:
                    result = handler(args=args)
                    results.append(result)
                    if stop_on_failure and not result.get("success", True):
                        results.append({
                            "chain_stopped": True,
                            "reason": f"Function '{func_name}' failed with stop_on_failure enabled.",
                            "remaining": funcs[funcs.index(func_name) + 1:],
                        })
                        return results
                else:
                    error_result = {
                        "func": func_name,
                        "success": False,
                        "error": f"Unknown function: {func_name}",
                    }
                    results.append(error_result)
                    if stop_on_failure:
                        results.append({"chain_stopped": True, "reason": f"Unknown function: {func_name}"})
                        return results
            except Exception as e:
                error_result = {
                    "func": func_name,
                    "success": False,
                    "error": str(e),
                }
                results.append(error_result)
                if stop_on_failure:
                    results.append({"chain_stopped": True, "reason": str(e)})
                    return results
        return results

    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================

    def _resolve_start_text(self, text: str, protocol_name: str) -> str:
        """Resolve @-references in start text."""
        return resolve_protocol_start_text(text, protocol_name)

    def _set_daic_for_step(self, step: Dict) -> None:
        """Set DAIC mode based on step definition."""
        mode = step.get("mode", "discussion")
        set_daic_mode(mode)

    def _log_step(self, step_name: str, summary: str,
                  post_funcs_results: List[Dict],
                  started_at: str = None,
                  pre_funcs_results: List[Dict] = None,
                  task_name: str = None) -> None:
        """Log step completion to protocol log.

        Args:
            task_name: Explicit task name for log file lookup. Required when
                       post_funcs (e.g. clear_task_state) may have cleared
                       current_task.json before this method is called.
        """
        now = datetime.now(timezone.utc).isoformat()

        def add_step(log):
            if "steps" not in log:
                log["steps"] = []

            entry = {
                "name": step_name,
                "completed_at": now,
                "summary": summary,
                "post_funcs_executed": post_funcs_results if post_funcs_results else [],
            }
            if started_at:
                entry["started_at"] = started_at
            if pre_funcs_results:
                entry["pre_funcs_executed"] = pre_funcs_results

            log["steps"].append(entry)

        self._update_log(add_step, task_name=task_name)

    def _log_goto(self, from_step: str, to_step: str, reason: str) -> None:
        """Log a goto navigation to protocol log."""
        now = datetime.now(timezone.utc).isoformat()

        def add_goto(log):
            if "gotos" not in log:
                log["gotos"] = []
            log["gotos"].append({
                "from_step": from_step,
                "to_step": to_step,
                "reason": reason,
                "at": now,
            })

        self._update_log(add_goto)

    def _validate_advance_args(self, step: Dict, args: Dict) -> Optional[str]:
        """Validate that required advance_args are present. Returns error string or None."""
        required = step.get("advance_args", [])
        if not required:
            return None

        if not args:
            return (
                f"Step '{step['name']}' requires args: {required}. "
                f"Received: {{}}. Pass args={{...}} to protocol_advance."
            )

        missing = []
        for k in required:
            if k not in args or not args[k]:
                # Allow empty task_content when the task file already exists
                if k == "task_content" and args.get("task"):
                    tasks_dir = self.project_root / "team-management" / "tasks"
                    tf = tasks_dir / f"{args['task']}.md"
                    if not tf.exists():
                        tf = tasks_dir / args["task"] / "README.md"
                    if tf.exists():
                        continue
                missing.append(k)
        if missing:
            return (
                f"Step '{step['name']}' requires args: {required}. "
                f"Missing: {missing}. Pass args={{...}} to protocol_advance."
            )
        return None

    def _update_task_protocol_step(self, completed_step_name: str) -> None:
        """Update protocol_step in task file frontmatter for session recovery."""
        task_state = get_task_state()
        task_name = task_state.get("task")
        if not task_name:
            return

        # Find task file
        tasks_dir = self.project_root / "team-management" / "tasks"
        task_file = tasks_dir / f"{task_name}.md"
        if not task_file.exists():
            task_file = tasks_dir / task_name / "README.md"
        if not task_file.exists():
            return

        try:
            content = task_file.read_text(encoding="utf-8")
            if not content.startswith("---"):
                return

            end_marker = content.find("---", 3)
            if end_marker == -1:
                return

            frontmatter = content[3:end_marker]
            body = content[end_marker + 3:]

            # Update or add protocol_step field
            lines = frontmatter.strip().split("\n")
            updated = False
            new_lines = []
            for line in lines:
                if line.strip().startswith("protocol_step:"):
                    new_lines.append(f"protocol_step: {completed_step_name}")
                    updated = True
                else:
                    new_lines.append(line)

            if not updated:
                new_lines.append(f"protocol_step: {completed_step_name}")

            new_content = "---\n" + "\n".join(new_lines) + "\n---" + body
            task_file.write_text(new_content, encoding="utf-8")
        except (IOError, OSError):
            pass

    def _complete_protocol(self, task_name: str = None) -> None:
        """Complete the protocol: update log, clear state, set discussion mode.

        Args:
            task_name: Explicit task name for log file lookup. Required when
                       post_funcs have already cleared current_task.json.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Update log with completion time (use explicit task_name since
        # clear_task_state may have already nulled current_task.json)
        self._update_log(lambda log: log.update({"completed_at": now}), task_name=task_name)

        # Clear protocol state from current_task.json
        clear_protocol_state()

        # Set DAIC to discussion
        set_daic_mode("discussion")

    def _get_log_path(self, task_name: str = None) -> Path:
        """Get current protocol log file path.

        Args:
            task_name: Explicit task name. Falls back to current_task.json.
        """
        if not task_name:
            task_state = get_task_state()
            task_name = task_state.get("task")
        if task_name:
            return self.logs_dir / f"{task_name}.json"
        return self.logs_dir / "_pending.json"

    def _update_log(self, updater, task_name: str = None) -> None:
        """Read log, apply updater function, write back.

        Args:
            task_name: Explicit task name for log file lookup. When provided,
                       bypasses get_task_state() which may return null if
                       clear_task_state has already run.
        """
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # Use explicit task_name if provided, otherwise read from state
        if not task_name:
            task_state = get_task_state()
            task_name = task_state.get("task")

        log_file = self.logs_dir / f"{task_name}.json" if task_name else self.logs_dir / "_pending.json"

        if not log_file.exists():
            # Fallback to _pending
            log_file = self.logs_dir / "_pending.json"

        log_data = {}
        if log_file.exists():
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    log_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                log_data = {"steps": [], "gotos": [], "notes": []}

        updater(log_data)

        _write_json_durable(log_file, log_data, ensure_ascii=False)

    def _build_handlers(self) -> Dict[str, Any]:
        """Build and return the full handler registry mapping function names to methods.

        AI provider resolver handlers are registered by iterating _PHASE_REGISTRY so
        future phase additions need only edit the registry (plus the per-phase _func
        method). The registry's func_name field maps to `_func_<func_name>` on self.
        """
        handlers = {
            # Pre-funcs
            "auto_detect_task": self._func_auto_detect_task,
            # Post-funcs (investigation)
            "create_task_file": self._func_create_task_file,
            "set_task_state": self._func_set_task_state,
            "git_setup_branch": self._func_git_setup_branch,
            "create_issue_if_enabled": self._func_create_issue_if_enabled,
            "update_task_status_in_progress": self._func_update_task_status,
            # Pre/Post-funcs (code-review — spec compliance + evidence gates)
            "require_spec_review_passed": self._func_require_spec_review_passed,
            "check_completion_evidence": self._func_check_completion_evidence,
            # Pre-func (code-review — optional test gate)
            "verify_tests_pass": self._func_verify_tests_pass,
            # Post-funcs (code-review)
            "validate_code_review_in_worklog": self._func_validate_code_review_in_worklog,
            "validate_no_critical_issues_in_worklog": self._func_validate_no_critical_issues_in_worklog,
            # Pre-funcs (implementation)
            "verify_branch_and_task": self._func_verify_branch_and_task,
            # Pre-func (completion — disabled-provider menu)
            "present_completion_options": self._func_present_completion_options,
            # Post-funcs (task/brainstorm/research archival)
            # Post-funcs (completion)
            "git_commit": self._func_git_commit,
            "git_merge_main": self._func_git_merge_main,
            "git_push": self._func_git_push,
            "create_merge_request": self._func_create_merge_request,
            "update_issue_status": self._func_update_issue_status,
            "archive_task": self._func_archive_task,
            "cleanup_task_scoped_state": self._func_cleanup_task_scoped_state,
            "clear_task_state": self._func_clear_task_state,
            "checkout_default_branch": self._func_checkout_default_branch,
            # Post-funcs (completion — friction gate + dispatcher)
            "require_discard_confirmation": self._func_require_discard_confirmation,
            "completion_dispatch": self._func_completion_dispatch,
            # Refactoring protocol funcs
            "capture_test_baseline": self._func_capture_test_baseline,
            "load_test_baseline": self._func_load_test_baseline,
            "cleanup_test_baseline": self._func_cleanup_test_baseline,
            # Optimize-protocol funcs (T2: h-optimize-protocol-engine)
            "log_experiment_result": self._func_log_experiment_result,
            "run_metric": self._func_run_metric,
            "validate_metric_script": self._func_validate_metric_script,
            "capture_metric_baseline": self._func_capture_metric_baseline,
            "check_cost_estimate": self._func_check_cost_estimate,
            "check_termination": self._func_check_termination,
            "update_best_commit": self._func_update_best_commit,
            "policy_compliance_audit": self._func_policy_compliance_audit,
            "batch_checkpoint": self._func_batch_checkpoint,
            # Optimize-protocol funcs (T4: m-optimize-protocol-batched)
            "validate_optimize_setup": self._func_validate_optimize_setup,
            "write_optimize_setup": self._func_write_optimize_setup,
            # Wiki integration
            "wiki_update_reminder": self._func_wiki_update_reminder,
        }
        # AI provider resolver handlers — registry-driven for future phase additions.
        for entry in _PHASE_REGISTRY.values():
            method_name = f"_func_{entry['func_name']}"
            method = getattr(self, method_name, None)
            if method is None:
                # Should not happen — registry → method is a checked invariant.
                # Skip rather than crash _build_handlers (which is called by
                # get_available_funcs and would block protocol introspection).
                continue
            handlers[entry["func_name"]] = method
        return handlers

    def _discover_custom_funcs(self) -> Dict[str, Any]:
        """Discover custom functions from protocol-configs/custom/funcs/*.py.

        Scans Python files in the custom funcs directory and collects all
        public (non-underscore) callables. Returns empty dict if directory
        doesn't exist. Import errors are caught per-file.
        """
        custom_funcs_dir = self.project_root / "team-management" / "protocol-configs" / "custom" / "funcs"
        if not custom_funcs_dir.exists():
            return {}

        funcs = {}
        for py_file in sorted(custom_funcs_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            module_name = f"custom_funcs_{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                for attr_name in dir(module):
                    if attr_name.startswith("_"):
                        continue
                    obj = getattr(module, attr_name)
                    if callable(obj):
                        funcs[attr_name] = obj
            except Exception:
                # Skip files with import/syntax errors — don't break all custom funcs
                continue
        return funcs

    def _get_func_handler(self, name: str):
        """Map function name to handler method.

        Supports built-in functions by name and custom functions via
        custom(func_name) syntax.
        """
        if not hasattr(self, "_handlers_cache"):
            self._handlers_cache = self._build_handlers()

        # Try built-in handler first
        handler = self._handlers_cache.get(name)
        if handler:
            return handler

        # Try custom(func_name) syntax
        match = re.match(r'^custom\((\w+)\)$', name)
        if not match:
            return None

        func_name = match.group(1)

        # Lazy-load custom funcs cache
        if not hasattr(self, "_custom_funcs_cache"):
            self._custom_funcs_cache = self._discover_custom_funcs()

        custom_callable = self._custom_funcs_cache.get(func_name)
        if not custom_callable:
            return None

        # Wrap custom callable in a handler with error handling
        def custom_handler(args=None):
            try:
                result = custom_callable(args)
                if not isinstance(result, dict):
                    return {
                        "func": f"custom({func_name})",
                        "success": False,
                        "error": f"Custom function '{func_name}' must return a dict, got {type(result).__name__}.",
                    }
                if "func" not in result:
                    result["func"] = f"custom({func_name})"
                return result
            except Exception as e:
                return {
                    "func": f"custom({func_name})",
                    "success": False,
                    "error": f"Custom function '{func_name}' raised: {e}",
                }

        return custom_handler

    # ========================================================================
    # FUNC HANDLERS
    # ========================================================================

    def _func_auto_detect_task(self, args: Dict = None) -> Dict:
        """Auto-detect task from current git branch or explicit task parameter."""
        # Check for explicit task from protocol_start(task=...)
        explicit_task = getattr(self, '_explicit_task', None)
        if explicit_task:
            frontmatter = parse_task_frontmatter(explicit_task)
            branch = frontmatter.get("branch", "")
            if branch:
                set_task_state(explicit_task, branch, [])
            else:
                # No branch in frontmatter — still set task state without branch
                set_task_state(explicit_task, "", [])
            return {
                "func": "auto_detect_task",
                "success": True,
                "task": explicit_task,
                "branch": branch or None,
                "message": f"Using explicit task '{explicit_task}' from protocol_start parameter.",
            }

        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_FAST, check=False,
                cwd=str(self.project_root),
            )
            branch = result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            branch = None

        if not branch or branch in ("main", "master"):
            return {
                "func": "auto_detect_task",
                "success": True,
                "task": None,
                "message": f"No task found matching current branch '{branch or 'unknown'}'. Task will be created when investigation completes.",
            }

        inferred = infer_task_from_branch(branch)
        if inferred:
            # Set task state
            set_task_state(inferred, branch, [])
            return {
                "func": "auto_detect_task",
                "success": True,
                "task": inferred,
                "branch": branch,
                "message": "Auto-detected task from branch",
            }

        return {
            "func": "auto_detect_task",
            "success": True,
            "task": None,
            "branch": branch,
            "message": f"No task found matching branch '{branch}'. Task will be created when investigation completes.",
        }

    def _validate_task_file_structure(self, task_name: str, task_content: str, branch: str = "") -> List[str]:
        """Hard structural/identity checks shared by the inline and skip-overwrite
        paths of _func_create_task_file. Returns an ordered list of error strings
        ([] == valid); callers surface errors[0] to preserve the historical
        first-error order. **Author:** is deliberately NOT checked here — the inline
        path injects it (never blocks) and the skip path warns without mutating the
        file, so author handling lives in _func_create_task_file, not here.
        """
        if not task_content.startswith("---"):
            return ["task_content must start with frontmatter (---)."]
        end_marker = task_content.find("---", 3)
        if end_marker == -1:
            return ["Malformed frontmatter: no closing ---."]
        fm_fields = {}
        for line in task_content[3:end_marker].strip().split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                fm_fields[key.strip()] = value.strip()
        errors: List[str] = []
        if "status" not in fm_fields:
            errors.append("Frontmatter missing required field: status.")
        if branch:
            fm_branch = fm_fields.get("branch", "")
            if fm_branch and fm_branch != branch:
                errors.append(
                    f"Branch mismatch: args.branch='{branch}' but frontmatter branch='{fm_branch}'. They must match."
                )
        if not any(task_name.startswith(p) for p in ("h-", "m-", "l-", "r-", "o-", "b-")):
            errors.append(
                f"Task name '{task_name}' must start with a priority prefix: h- (high), m- (medium), l- (low), r- (research/investigate), o- (optimize), b- (brainstorm)."
            )
        body_text = task_content[end_marker + 3:]
        if "## Success Criteria" not in body_text and "## Criteria" not in body_text:
            errors.append(
                "Task file missing required section: ## Success Criteria. Include measurable success criteria in the task content."
            )
        # Drafting-only clarification markers must be resolved before delivery.
        # \s+ (not a literal space) also keeps regex-source spellings of the
        # pattern itself from self-matching when quoted in a task file.
        if re.search(r"\[NEEDS\s+CLARIFICATION", body_text, re.IGNORECASE):
            errors.append(
                "Task file contains unresolved NEEDS-CLARIFICATION markers. Resolve every marker "
                "with the user (or record the accepted unknown in ## User Notes) before delivering the task file."
            )
        return errors

    def _func_create_task_file(self, args: Dict = None) -> Dict:
        """Create task file from AI-provided content.

        Substantial task files should use *write-file-first*: write the file to
        team-management/tasks/ directly, then advance with task_content="" — the
        empty-content branch re-validates the on-disk file (STRICT PARITY with the
        inline validation below) rather than trusting existence alone, and never
        mutates it (a missing **Author:** is warned, not injected).
        """
        if not args:
            return {"func": "create_task_file", "success": False, "error": "No args provided."}

        task_name = args.get("task", "")
        task_content = args.get("task_content", "")
        branch = args.get("branch", "")

        if not task_name:
            return {"func": "create_task_file", "success": False, "error": "task arg is required."}
        if not task_content:
            # Write-file-first / re-investigate: the file must already exist AND pass
            # validation (STRICT PARITY with the inline path) — no longer a bare
            # existence check. The on-disk file is never mutated here.
            tasks_dir = self.project_root / "team-management" / "tasks"
            task_file = tasks_dir / f"{task_name}.md"
            if not task_file.exists():
                task_file = tasks_dir / task_name / "README.md"
            if not task_file.exists():
                return {"func": "create_task_file", "success": False, "error": "task_content is empty and no existing file."}
            rel = str(task_file.relative_to(self.project_root))
            try:
                existing = task_file.read_text(encoding="utf-8")
            except (IOError, OSError) as e:
                return {"func": "create_task_file", "success": False, "error": f"Could not read existing task file '{rel}': {e}", "path": rel}
            errors = self._validate_task_file_structure(task_name, existing, branch)
            if errors:
                return {
                    "func": "create_task_file",
                    "success": False,
                    "error": f"Existing task file '{rel}' failed validation: {errors[0]}",
                    "path": rel,
                }
            end_marker = existing.find("---", 3)
            body = existing[end_marker + 3:] if end_marker != -1 else existing
            result = {
                "func": "create_task_file",
                "success": True,
                "action": "skipped",
                "path": rel,
                "message": "Task file already exists and passed validation — skipping overwrite.",
            }
            if "**Author:**" not in body:
                result["warnings"] = [
                    "Task file is missing an **Author:** line (not injected — write-file-first files are never overwritten)."
                ]
                result["message"] += " Warning: missing **Author:** line."
            return result

        # Inline content path — validate via the shared helper (STRICT PARITY with
        # the skip branch above), preserving the historical first-error order.
        errors = self._validate_task_file_structure(task_name, task_content, branch)
        if errors:
            return {"func": "create_task_file", "success": False, "error": errors[0]}

        # Recompute the frontmatter boundary for **Author:** injection + write below.
        end_marker = task_content.find("---", 3)
        body_text = task_content[end_marker + 3:]

        # Auto-inject author from config if not present in body
        if "**Author:**" not in body_text:
            config_file = self.project_root / "team-management" / "config.json"
            developer_name = None
            if config_file.exists():
                try:
                    with open(config_file, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    developer_name = cfg.get("developer_name")
                except Exception:
                    pass
            if developer_name:
                # Insert after the first '# Title' line in the body
                frontmatter_and_sep = task_content[: end_marker + 3]
                lines = body_text.split("\n")
                inserted = False
                for i, line in enumerate(lines):
                    if line.startswith("# ") and not line.startswith("## "):
                        lines.insert(i + 1, f"\n**Author:** {developer_name}")
                        inserted = True
                        break
                if inserted:
                    task_content = frontmatter_and_sep + "\n".join(lines)

        tasks_dir = self.project_root / "team-management" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        task_file = tasks_dir / f"{task_name}.md"
        action = "updated" if task_file.exists() else "created"

        try:
            task_file.write_text(task_content, encoding="utf-8")
        except (IOError, OSError) as e:
            return {"func": "create_task_file", "success": False, "error": str(e)}

        return {
            "func": "create_task_file",
            "success": True,
            "action": action,
            "path": str(task_file.relative_to(self.project_root)),
        }

    def _func_set_task_state(self, args: Dict = None) -> Dict:
        """Set task state and rename pending log."""
        if not args:
            return {"func": "set_task_state", "success": False, "error": "No args provided."}

        task_name = args.get("task", "")
        branch = args.get("branch", "")

        if not task_name:
            return {"func": "set_task_state", "success": False, "error": "task arg is required."}

        # Parse task frontmatter for services
        services = []
        frontmatter = parse_task_frontmatter(task_name)
        modules_str = frontmatter.get("modules", "")
        if modules_str:
            # Parse YAML list format: [service1, service2]
            modules_str = modules_str.strip("[]")
            services = [s.strip() for s in modules_str.split(",") if s.strip()]

        state = set_task_state(task_name, branch, services)

        # Rename _pending.json log to {task}.json
        pending_log = self.logs_dir / "_pending.json"
        if pending_log.exists():
            task_log = self.logs_dir / f"{task_name}.json"
            try:
                log_data = json.loads(pending_log.read_text(encoding="utf-8"))
                log_data["task"] = task_name
                task_log.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
                pending_log.unlink()
            except (json.JSONDecodeError, IOError):
                # Fallback: simple rename
                pending_log.rename(task_log)

        return {
            "func": "set_task_state",
            "success": True,
            "state": state,
        }

    def _func_git_setup_branch(self, args: Dict = None) -> Dict:
        """Create and checkout git branch for the task."""
        if not args:
            return {"func": "git_setup_branch", "success": False, "error": "No args provided."}

        branch = args.get("branch", "")
        if not branch:
            return {"func": "git_setup_branch", "success": False, "error": "branch arg is required."}

        # Option-injection guard: never pass a leading-dash / metachar branch
        # name to `git checkout`/`checkout -b`/`branch --list` argv (shared
        # validator, single source of truth in git_operations.py).
        from git_operations import validate_branch_name
        if not validate_branch_name(branch):
            return {
                "func": "git_setup_branch",
                "success": False,
                "error": f"Invalid branch name {branch!r}: only [A-Za-z0-9/._-] allowed, no leading '-'.",
            }

        carry_changes = args.get("carry_changes", False)
        cwd = str(self.project_root)

        try:
            # Check for dirty working tree before any branch operations
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            if result.returncode == 0 and result.stdout.strip():
                dirty_files = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
                if not carry_changes:
                    return {
                        "func": "git_setup_branch",
                        "success": False,
                        "needs_confirmation": True,
                        "dirty_files": dirty_files[:10],
                        "dirty_count": len(dirty_files),
                        "error": (
                            f"Working tree has {len(dirty_files)} uncommitted change(s). "
                            f"These are likely task/brainstorm files created during this protocol step.\n"
                            f"Files: {', '.join(dirty_files[:5])}"
                            + (f" (and {len(dirty_files) - 5} more)" if len(dirty_files) > 5 else "")
                            + f"\n\nAsk the user: should these changes be carried into the new branch '{branch}'? "
                            f"If yes, re-run protocol_advance with carry_changes: true in args."
                        ),
                    }

            # Check current branch
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_FAST, check=False, cwd=cwd,
            )
            current = result.stdout.strip() if result.returncode == 0 else ""

            if current == branch:
                return {
                    "func": "git_setup_branch",
                    "success": True,
                    "branch": branch,
                    "created": False,
                    "modules_branched": [],
                    "message": f"Already on branch '{branch}'.",
                }

            # Check if branch already exists
            result = subprocess.run(
                ["git", "branch", "--list", branch],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_FAST, check=False, cwd=cwd,
            )
            branch_exists = bool(result.stdout.strip())

            if branch_exists:
                # Just checkout existing branch
                result = subprocess.run(
                    ["git", "checkout", branch],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
                )
                if result.returncode != 0:
                    return {"func": "git_setup_branch", "success": False, "error": f"Failed to checkout branch: {result.stderr.strip()}"}
                return {
                    "func": "git_setup_branch",
                    "success": True,
                    "branch": branch,
                    "created": False,
                    "modules_branched": [],
                    "message": f"Checked out existing branch '{branch}'.",
                }

            if carry_changes:
                # Create branch from current position to preserve uncommitted changes
                result = subprocess.run(
                    ["git", "checkout", "-b", branch],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
                )
                if result.returncode != 0:
                    return {"func": "git_setup_branch", "success": False, "error": f"Failed to create branch: {result.stderr.strip()}"}

                return {
                    "func": "git_setup_branch",
                    "success": True,
                    "branch": branch,
                    "created": True,
                    "carried_changes": True,
                    "modules_branched": [],
                    "message": f"Branch '{branch}' created from current position with uncommitted changes preserved.",
                }

            # Detect the default branch via the shared helper (origin/HEAD →
            # main/master/develop/trunk/stable → "main") so a repo with a
            # custom default like develop/trunk branches from the right base.
            # Mirrors _func_checkout_default_branch / _func_git_merge_main.
            default_branch = self._detect_default_branch()

            # Checkout default branch first
            result = subprocess.run(
                ["git", "checkout", default_branch],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            if result.returncode != 0:
                return {"func": "git_setup_branch", "success": False, "error": f"Failed to checkout {default_branch}: {result.stderr.strip()}"}

            # Pull latest (non-fatal if offline)
            subprocess.run(
                ["git", "pull", "--ff-only"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )

            # Create new branch from default branch
            result = subprocess.run(
                ["git", "checkout", "-b", branch],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            if result.returncode != 0:
                return {"func": "git_setup_branch", "success": False, "error": f"Failed to create branch: {result.stderr.strip()}"}

            return {
                "func": "git_setup_branch",
                "success": True,
                "branch": branch,
                "created": True,
                "modules_branched": [],
                "message": "Branch created and checked out.",
            }
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return {"func": "git_setup_branch", "success": False, "error": str(e)}

    def _provider_issue_tracking_enabled(self, config: Dict, provider: str) -> bool:
        """Return False only if `<provider>.issue_tracking_enabled` is explicitly False.
        Default True preserves backward compatibility with configs that omit the key."""
        return config.get(provider, {}).get("issue_tracking_enabled", True) is not False

    def _func_create_issue_if_enabled(self, args: Dict = None) -> Dict:
        """Create provider issue if issue tracking is enabled."""
        try:
            config_file = self.project_root / "team-management" / "config.json"
            if not config_file.exists():
                return {"func": "create_issue_if_enabled", "success": True, "action": "skipped", "message": "No config file found."}

            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            provider = config.get("issue_tracking", {}).get("provider", "disabled")
            if provider == "disabled":
                return {"func": "create_issue_if_enabled", "success": True, "action": "skipped", "message": "Issue tracking is disabled."}

            if not self._provider_issue_tracking_enabled(config, provider):
                return {"func": "create_issue_if_enabled", "success": True, "action": "skipped",
                        "message": f"{provider}.issue_tracking_enabled is false."}

            task_name = args.get("task", "") if args else ""
            if not task_name:
                return {"func": "create_issue_if_enabled", "success": True, "action": "skipped", "message": "No task name provided."}

            # Import provider utilities
            from issue_provider_base import find_task_file
            task_file = find_task_file(self.project_root, task_name)
            if not task_file:
                return {"func": "create_issue_if_enabled", "success": False, "error": f"Task file not found for '{task_name}'."}

            # Route to appropriate provider
            if provider == "gitlab":
                from gitlab_utils import get_gitlab_sync
                sync = get_gitlab_sync()
                if sync:
                    # Idempotency: skip if the task is already linked to a GitLab issue.
                    # Re-running this step (dirty-tree pause, protocol_goto) must not
                    # create a duplicate and overwrite the mapping.
                    existing = sync.get_task_mapping(task_name)
                    if existing and existing.get("gitlab_issue_iid"):
                        return {"func": "create_issue_if_enabled", "success": True, "action": "skipped",
                                "provider": "gitlab", "message": f"Task already linked to issue {existing.get('gitlab_issue_iid')}."}
                    result = sync.create_issue_from_task(task_name)
                    if isinstance(result, dict) and result.get("error"):
                        return {"func": "create_issue_if_enabled", "success": False, "error": str(result)}
                    return {"func": "create_issue_if_enabled", "success": True, "action": "created", "provider": "gitlab", "result": str(result)}
            elif provider == "github":
                from github_utils import get_github_sync
                sync = get_github_sync()
                if sync:
                    existing = sync.get_task_mapping(task_name)
                    if existing and existing.get("issue_id"):
                        return {"func": "create_issue_if_enabled", "success": True, "action": "skipped",
                                "provider": "github", "message": f"Task already linked to issue {existing.get('issue_id')}."}
                    result = sync.create_issue_from_task(task_name)
                    if isinstance(result, dict) and result.get("error"):
                        return {"func": "create_issue_if_enabled", "success": False, "error": str(result)}
                    return {"func": "create_issue_if_enabled", "success": True, "action": "created", "provider": "github", "result": str(result)}
            elif provider == "jira":
                from jira_utils import get_jira_sync
                sync = get_jira_sync()
                if sync:
                    existing = sync.get_task_mapping(task_name)
                    existing_key = existing.get("jira_issue_key") or existing.get("jira_issue_id") if existing else None
                    if existing_key:
                        return {"func": "create_issue_if_enabled", "success": True, "action": "skipped",
                                "provider": "jira", "message": f"Task already linked to issue {existing_key}."}
                    result = sync.create_issue_from_task(task_name)
                    if isinstance(result, dict) and result.get("error"):
                        return {"func": "create_issue_if_enabled", "success": False, "error": str(result)}
                    return {"func": "create_issue_if_enabled", "success": True, "action": "created", "provider": "jira", "result": str(result)}

            return {"func": "create_issue_if_enabled", "success": True, "action": "skipped", "message": f"Provider '{provider}' not available."}

        except Exception as e:
            return {"func": "create_issue_if_enabled", "success": False, "error": str(e)}

    def _func_update_task_status(self, args: Dict = None) -> Dict:
        """Update task .md file status to in-progress."""
        task_state = get_task_state()
        task_name = task_state.get("task")
        if not task_name:
            return {"func": "update_task_status_in_progress", "success": False, "error": "No active task."}

        # Find task file
        tasks_dir = self.project_root / "team-management" / "tasks"
        task_file = tasks_dir / f"{task_name}.md"
        if not task_file.exists():
            task_file = tasks_dir / task_name / "README.md"
        if not task_file.exists():
            return {"func": "update_task_status_in_progress", "success": False, "error": f"Task file not found for '{task_name}'."}

        try:
            content = task_file.read_text(encoding="utf-8")
            if not content.startswith("---"):
                return {"func": "update_task_status_in_progress", "success": False, "error": "Task file has no frontmatter."}

            end_marker = content.find("---", 3)
            if end_marker == -1:
                return {"func": "update_task_status_in_progress", "success": False, "error": "Malformed frontmatter."}

            frontmatter = content[3:end_marker]
            body = content[end_marker + 3:]

            lines = frontmatter.strip().split("\n")
            updated_lines = []
            previous_status = None
            has_started = False
            today = datetime.now().strftime("%Y-%m-%d")

            for line in lines:
                if line.strip().startswith("status:"):
                    previous_status = line.split(":", 1)[1].strip()
                    updated_lines.append("status: in-progress")
                elif line.strip().startswith("started:"):
                    updated_lines.append(line)
                    has_started = True
                else:
                    updated_lines.append(line)

            if not has_started:
                final = []
                for line in updated_lines:
                    final.append(line)
                    if line.strip().startswith("created:"):
                        final.append(f"started: {today}")
                updated_lines = final

            new_content = "---\n" + "\n".join(updated_lines) + "\n---" + body
            task_file.write_text(new_content, encoding="utf-8")

            result = {
                "func": "update_task_status_in_progress",
                "success": True,
                "task": task_name,
                "previous_status": previous_status,
                "new_status": "in-progress",
            }
            if not has_started:
                result["started"] = today
            return result

        except (IOError, OSError) as e:
            return {"func": "update_task_status_in_progress", "success": False, "error": str(e)}

    def _find_task_file(self, task_name: str) -> Optional[Path]:
        """Locate the task markdown file for the given task name."""
        base = self.project_root / "team-management"
        candidate = base / "tasks" / f"{task_name}.md"
        if candidate.exists():
            return candidate
        candidate = base / "tasks" / task_name / "README.md"
        if candidate.exists():
            return candidate
        return None

    def _func_validate_code_review_in_worklog(self, args: Dict = None) -> Dict:
        """Validate that code review results are appended to the task work log."""
        task_state = get_task_state()
        task_name = task_state.get("task")
        if not task_name:
            return {"func": "validate_code_review_in_worklog", "success": False, "error": "No active task."}

        task_file = self._find_task_file(task_name)
        if task_file is None:
            return {"func": "validate_code_review_in_worklog", "success": False, "error": f"Task file not found for '{task_name}'."}

        try:
            content = task_file.read_text(encoding="utf-8")
            if "# Code Review:" not in content:
                return {
                    "func": "validate_code_review_in_worklog",
                    "success": False,
                    "error": (
                        "Task work log missing code review results. "
                        "Append '# Code Review: [Title]' section with Summary, Critical Issues, "
                        "Warnings, and Notes before advancing."
                    ),
                }
            return {
                "func": "validate_code_review_in_worklog",
                "success": True,
                "message": "Code review results found in task work log.",
            }
        except (IOError, OSError) as e:
            return {"func": "validate_code_review_in_worklog", "success": False, "error": str(e)}

    def _func_validate_no_critical_issues_in_worklog(self, args: Dict = None) -> Dict:
        """Validate that the latest code review block in the task work log has zero critical issues.

        Looks at the LAST '# Code Review:' section (so re-reviews after fixes win) and
        parses the Critical Issues count from headings of the form:
            ## Critical Issues (N)        — without emoji (sub-protocol format)
            ## 🔴 Critical Issues (N)     — with emoji (agent template format)

        Fails if N > 0, the section is missing entirely, or the count cannot be parsed.
        """
        task_state = get_task_state()
        task_name = task_state.get("task")
        if not task_name:
            return {"func": "validate_no_critical_issues_in_worklog", "success": False, "error": "No active task."}

        task_file = self._find_task_file(task_name)
        if task_file is None:
            return {"func": "validate_no_critical_issues_in_worklog", "success": False, "error": f"Task file not found for '{task_name}'."}

        try:
            content = task_file.read_text(encoding="utf-8")
        except (IOError, OSError) as e:
            return {"func": "validate_no_critical_issues_in_worklog", "success": False, "error": str(e)}

        # Take the LAST '# Code Review:' section so re-reviews after fixes are the source of truth.
        # Section ends at the next single-# heading (other than '# Code Review:') or end of file.
        review_blocks = list(re.finditer(
            r'^# Code Review:.*?(?=^# [^#]|\Z)',
            content,
            flags=re.MULTILINE | re.DOTALL,
        ))
        if not review_blocks:
            return {
                "func": "validate_no_critical_issues_in_worklog",
                "success": False,
                "error": (
                    "Task work log missing code review results. "
                    "Append '# Code Review: [Title]' section with Summary, Critical Issues, "
                    "Warnings, and Notes before advancing."
                ),
            }

        last_review = review_blocks[-1].group(0)

        # Match '## Critical Issues (N)' or '## 🔴 Critical Issues (N)' (with optional surrounding whitespace).
        match = re.search(
            r'^##\s*(?:🔴\s*)?Critical Issues\s*\((\d+)\)',
            last_review,
            flags=re.MULTILINE,
        )
        if not match:
            return {
                "func": "validate_no_critical_issues_in_worklog",
                "success": False,
                "error": (
                    "Latest '# Code Review:' block is missing a 'Critical Issues (N)' subsection. "
                    "Add '## Critical Issues (0)' (or '## 🔴 Critical Issues (0)') with 'None found.' "
                    "if there are no critical issues."
                ),
            }

        count = int(match.group(1))
        if count > 0:
            return {
                "func": "validate_no_critical_issues_in_worklog",
                "success": False,
                "error": (
                    f"Code review reports {count} critical issue(s). Fix all of them and append a "
                    f"new '# Code Review:' block with Critical Issues (0) before advancing."
                ),
                "critical_count": count,
            }

        return {
            "func": "validate_no_critical_issues_in_worklog",
            "success": True,
            "message": "Latest code review reports zero critical issues.",
        }

    # Positive-evidence patterns for _func_check_completion_evidence. Kept strict
    # to avoid false positives on prose mentions of numbers (Architect's concern):
    # only tight formats (N/N passed, exit 0, keyword tokens) count. Fenced blocks
    # are checked separately below — a fence must contain a verification signal,
    # not be a bare `curl example` code fence.
    _EVIDENCE_PATTERNS = (
        r'\b\d+\s*/\s*\d+\s+(?:passed|tests?)\b',             # N/N passed | N/N tests
        r'\bexit\s+(?:code[:\s]+)?0\b',                       # exit 0 | exit code: 0
        r'[\u2713\u2714]\s*\d+|\u2717\s*\d+|\d+\s*[\u2713\u2714\u2717]',  # check marks + digit
        r'\b(?:all\s+)?tests?\s+pass(?:ed|ing)?\b',           # tests pass(ed|ing)
        # «issues» dropped — matches prose («no issues with this approach», «0 issues reported»).
        # `tested|approved|verified` dropped — negative-outcome prose («user tested and found a regression») would pass.
        # `user confirmed` dropped — was added for completion step (now ungated); on code-review it
        # accepts non-verification prose like «user confirmed the intended behavior» (Codex W1 round 5).
        r'\bno\s+(?:errors?|warnings?|critical|failures?)\b',   # no errors/warnings/critical/failures
        r'\b0\s+(?:errors?|warnings?|critical|failures?)\b',    # 0 critical | 0 warnings — matches canonical «Final pass: 0 critical, 0 warnings»
    )

    # Fenced-block evidence: the fence MUST contain a verification signal.
    # Without this, any ``` curl example ``` would satisfy the gate (Codex W2 round 3).
    _FENCED_BLOCK_RE = re.compile(r'```[\s\S]+?```')
    # Dropped bare `ok|warning|critical|PASS|FAIL` (Codex W1 round 6) — they
    # accepted JSON/log examples («{"ok": true}», «warning: deprecated») as
    # evidence. Dropped bare `✓✔✗` (Codex W1 round 7) — plain checklists
    # («✓ renamed endpoint») accepted without counts. Count-adjacent forms
    # are the minimum anchor, consistent with outer _EVIDENCE_PATTERNS.
    _FENCED_INNER_SIGNAL_RE = re.compile(
        r'\b\d+\s+(?:passed|failed|errors?|warnings?|critical|failures?)\b'
        r'|\b(?:passed|failed|errors?|warnings?|critical|failures?)\s+\d+\b'
        r'|\d+\s*[\u2713\u2714\u2717]|[\u2713\u2714\u2717]\s*\d+'
        r'|\bexit\s+(?:code[:\s]+)?\d+\b'
        r'|\b\d+\s*/\s*\d+\b',
        re.IGNORECASE,
    )

    _ESCAPE_HATCH_RE = re.compile(
        r'^\s*no-verification-applicable\s*:\s*(.+?)\s*$',
        re.IGNORECASE | re.MULTILINE,
    )

    def _func_check_completion_evidence(self, args: Dict = None) -> Dict:
        """Block advance if summary lacks verification evidence.

        Required-acknowledgment pattern: summary must contain either
          (a) a strict positive marker (fenced block, N/N passed, exit 0, check marks,
              unambiguous test-pass keyword, no-errors token), OR
          (b) the escape-hatch marker `no-verification-applicable: <reason>` for steps
              that structurally cannot be verified by a command.

        Reads summary from args["advance_summary"] (injected by advance_step).
        """
        summary = (args or {}).get("advance_summary", "") or ""

        escape_match = self._ESCAPE_HATCH_RE.search(summary)
        if escape_match:
            reason = escape_match.group(1).strip()
            return {
                "func": "check_completion_evidence",
                "success": True,
                "escape_hatch": True,
                "reason": reason,
                "message": f"Escape hatch used: no-verification-applicable: {reason}",
            }

        # Fenced blocks: accept ONLY if the fence content contains a verification signal.
        # Bare ``` curl example ``` with no signal does NOT count (Codex W2 round 3).
        for fence in self._FENCED_BLOCK_RE.finditer(summary):
            if self._FENCED_INNER_SIGNAL_RE.search(fence.group(0)):
                return {
                    "func": "check_completion_evidence",
                    "success": True,
                    "message": "Verification evidence found in fenced code block.",
                }

        for pat in self._EVIDENCE_PATTERNS:
            if re.search(pat, summary, re.IGNORECASE):
                return {
                    "func": "check_completion_evidence",
                    "success": True,
                    "message": "Verification evidence found in advance summary.",
                }

        error = (
            "[BLOCKED] Advance requires verification evidence. Include one of: "
            "command output in a fenced block (must contain passed/failed/error/"
            "exit N/check-mark/N/N inside the fence), test counts (N/N passed), "
            "exit code (exit 0 / exit code: 0), check marks, keyword "
            "'(all) tests passed|passing', or review counts "
            "('0 critical', '0 warnings', '0 errors', '0 failures', or the "
            "'no' variants). For non-verifiable advances (docs, planning, "
            "discussion, user-confirmation-only), include: "
            "`no-verification-applicable: <reason>` on its own line."
        )
        return {
            "func": "check_completion_evidence",
            "success": False,
            "error": error,
            "message": "[BLOCKED] Advance summary missing verification evidence — see error for required formats.",
        }

    # Steps that represent code-changing work. A goto back to one of these
    # invalidates an earlier SPEC_REVIEW: PASSED sentinel (Codex W1 round 3).
    _CODE_CHANGING_STEPS = frozenset({"investigation", "implementation", "test-baseline", "refactoring", "experimentation"})

    def _func_require_spec_review_passed(self, args: Dict = None) -> Dict:
        """Require a SPEC_REVIEW: PASSED note in the task's audit log before advancing.

        Used as both pre_func (entry reminder) and post_func (structural block) on
        the code-review step. Scans the notes list for the exact sentinel string
        AND verifies the sentinel is newer than the most recent backward goto to
        a code-changing step — otherwise the sentinel is stale and the spec audit
        must be re-run against the updated diff.
        """
        task_state = get_task_state()
        task_name = task_state.get("task")
        if not task_name:
            return {
                "func": "require_spec_review_passed",
                "success": False,
                "error": "No active task — cannot locate protocol log for spec-review sentinel.",
                "message": "[BLOCKED] No active task.",
            }

        # Read task-specific log file directly — do NOT consult _pending.json
        # fallback (Codex W2 round 4: a stale SPEC_REVIEW: PASSED note in an
        # unrelated pending log could otherwise unblock this gate).
        log_path = self.logs_dir / f"{task_name}.json"
        if not log_path.exists():
            return {
                "func": "require_spec_review_passed",
                "success": False,
                "error": (
                    "Task-specific protocol log is missing — cannot verify "
                    "SPEC_REVIEW: PASSED sentinel. Re-enter the code-review "
                    "step via protocol_goto or dispatch the agent now and "
                    "save the sentinel."
                ),
                "message": "[BLOCKED] Task protocol log not found — fail closed.",
            }
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                log = json.load(f)
        except (IOError, OSError, json.JSONDecodeError) as e:
            return {
                "func": "require_spec_review_passed",
                "success": False,
                "error": f"Failed to read protocol log: {e}",
                "message": "[BLOCKED] Cannot read protocol log.",
            }
        notes = log.get("notes", []) or []
        gotos = log.get("gotos", []) or []

        sentinel_notes = [n for n in notes if n.get("note", "").strip() == "SPEC_REVIEW: PASSED"]
        if not sentinel_notes:
            return {
                "func": "require_spec_review_passed",
                "success": False,
                "error": (
                    "Spec compliance review not recorded as passing. Dispatch the "
                    "spec-compliance-reviewer agent and call "
                    "protocol_save_note('SPEC_REVIEW: PASSED') upon pass. "
                    "Recovery: re-enter step via protocol_goto or dispatch now."
                ),
                "message": "[BLOCKED] Missing SPEC_REVIEW: PASSED sentinel in protocol log.",
            }

        # ISO 8601 timestamps sort lexicographically the same as chronologically.
        latest_sentinel_at = max((n.get("at", "") for n in sentinel_notes), default="")
        backward_goto_ats = [
            g.get("at", "") for g in gotos
            if g.get("to_step") in self._CODE_CHANGING_STEPS
        ]
        latest_backward_at = max(backward_goto_ats, default="")
        if latest_backward_at and latest_sentinel_at < latest_backward_at:
            return {
                "func": "require_spec_review_passed",
                "success": False,
                "error": (
                    "Spec compliance review is STALE — the diff has changed since "
                    "the last SPEC_REVIEW: PASSED sentinel was recorded "
                    f"(sentinel at {latest_sentinel_at}, last backward goto to a "
                    f"code-changing step at {latest_backward_at}). Re-dispatch the "
                    "spec-compliance-reviewer agent against the current diff and "
                    "save a fresh SPEC_REVIEW: PASSED note."
                ),
                "message": "[BLOCKED] Stale SPEC_REVIEW: PASSED sentinel — re-run spec audit.",
                "latest_sentinel_at": latest_sentinel_at,
                "latest_backward_at": latest_backward_at,
            }

        return {
            "func": "require_spec_review_passed",
            "success": True,
            "message": "Spec compliance review recorded as passed and still fresh.",
            "latest_sentinel_at": latest_sentinel_at,
        }


    def _func_verify_branch_and_task(self, args: Dict = None) -> Dict:
        """Verify git branch matches task state and task file exists. Non-blocking."""
        task_state = get_task_state()
        task_name = task_state.get("task")
        expected_branch = task_state.get("branch")
        warnings = []

        if not task_name:
            warnings.append("No active task in task state.")
        else:
            base = self.project_root / "team-management"
            tf = base / "tasks" / f"{task_name}.md"
            if not tf.exists():
                tf = base / "tasks" / task_name / "README.md"
            if not tf.exists():
                warnings.append(f"Task file not found for '{task_name}'.")

        if expected_branch:
            try:
                result = subprocess.run(
                    ["git", "branch", "--show-current"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_FAST, check=False,
                    cwd=str(self.project_root),
                )
                current_branch = result.stdout.strip() if result.returncode == 0 else "unknown"
                if current_branch != expected_branch:
                    warnings.append(f"Branch mismatch: on '{current_branch}' but task expects '{expected_branch}'.")
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                warnings.append("Could not verify git branch.")
        else:
            warnings.append("No branch set in task state.")

        return {
            "func": "verify_branch_and_task",
            "success": True,  # Non-blocking — warnings only
            "warnings": warnings,
            "message": "All checks passed." if not warnings else f"{len(warnings)} warning(s) found.",
        }

    def _func_git_commit(self, args: Dict = None) -> Dict:
        """Stage and commit changes."""
        task_state = get_task_state()
        branch = task_state.get("branch")
        task_name = task_state.get("task", "unknown")

        if not branch:
            return {"func": "git_commit", "success": False, "error": "No branch in task state."}

        cwd = str(self.project_root)

        try:
            # Get changed files
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            if result.returncode != 0:
                return {"func": "git_commit", "success": False, "error": f"git status failed: {result.stderr.strip()}"}

            changed_files = []
            files_to_stage = []
            for line in result.stdout.splitlines():
                if line and len(line) > 3:
                    x_status = line[0]
                    y_status = line[1]
                    path = line[3:].strip()
                    if " -> " in path:
                        path = path.split(" -> ", 1)[1]
                    changed_files.append(path)
                    if y_status != ' ' or line[:2] == '??':
                        files_to_stage.append(path)

            if not changed_files:
                return {"func": "git_commit", "success": True, "branch": branch, "commit": None, "files_committed": 0, "message": "No changes to commit."}

            # Exclude config files that contain sensitive data (tokens, keys)
            sensitive_configs = ["team-management/config.json"]
            changed_files = [f for f in changed_files if f not in sensitive_configs]
            files_to_stage = [f for f in files_to_stage if f not in sensitive_configs]

            if not changed_files:
                return {"func": "git_commit", "success": True, "branch": branch, "commit": None, "files_committed": 0, "message": "No changes to commit (only config files with sensitive data)."}

            # Safety check for suspicious files
            import re as _re
            suspicious_exact = [".env", ".key", ".pem", ".p12"]
            suspicious_names = ["credentials"]
            # Template/example suffixes are safe — they're meant to be committed
            template_suffixes = (".example", ".sample", ".template", ".default", ".dist", ".tpl")
            suspicious_regex = _re.compile(
                r'(?:^|[/\\._-])(?:secret|secrets)(?:[/\\._-]|$)',
                _re.IGNORECASE,
            )
            suspicious = []
            for f in changed_files:
                fl = f.lower()
                basename = fl.split('/')[-1]
                # Skip template files (.env.example, .env.sample, credentials.template, etc.)
                if basename.endswith(template_suffixes):
                    continue
                if any(fl.endswith(ext) for ext in suspicious_exact) or basename == ".env" or basename.startswith(".env."):
                    suspicious.append(f)
                elif any(name in basename.split('.')[0] for name in suspicious_names):
                    suspicious.append(f)
                elif suspicious_regex.search(fl):
                    suspicious.append(f)
            if suspicious:
                return {
                    "func": "git_commit",
                    "success": False,
                    "error": "Suspicious files detected. Review and add to .gitignore or explicitly approve.",
                    "suspicious_files": suspicious,
                }

            # Stage files
            if files_to_stage:
                result = subprocess.run(
                    ["git", "add"] + files_to_stage,
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
                )
                if result.returncode != 0:
                    return {"func": "git_commit", "success": False, "error": f"git add failed: {result.stderr.strip()}"}

            # Extract commit message from task file title
            commit_msg = f"feat: {task_name}"
            base = self.project_root / "team-management"
            tf = base / "tasks" / f"{task_name}.md"
            if not tf.exists():
                tf = base / "tasks" / task_name / "README.md"
            if tf.exists():
                try:
                    for line in tf.read_text(encoding="utf-8").splitlines():
                        if line.startswith("# "):
                            commit_msg = line.replace("# ", "", 1).strip()
                            break
                except (IOError, OSError):
                    pass

            result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            if result.returncode != 0:
                return {"func": "git_commit", "success": False, "error": f"git commit failed: {result.stderr.strip()}"}

            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_FAST, check=False, cwd=cwd,
            )
            commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else "unknown"

            return {
                "func": "git_commit",
                "success": True,
                "branch": branch,
                "commit": commit_hash,
                "files_committed": len(changed_files),
            }

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return {"func": "git_commit", "success": False, "error": str(e)}

    def _origin_remote_exists(self) -> bool:
        """True iff an 'origin' remote is configured.

        Lets the provider-driven completion chain skip its fetch/push steps in
        a local-only repo (no remote) instead of hard-failing on
        `git fetch origin` / `git push origin` and stranding the chain. Skips
        ONLY on a demonstrably-absent origin — a failed `git remote` query (not
        a repo / git missing) returns True so the caller's fetch/push runs and
        surfaces the real error rather than silently skipping (codex review).
        """
        try:
            result = subprocess.run(
                ["git", "remote"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_FAST, check=False, cwd=str(self.project_root),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return True
        if result.returncode != 0:
            return True
        return "origin" in result.stdout.split()

    def _func_git_merge_main(self, args: Dict = None) -> Dict:
        """Fetch and merge default branch (main/master) into current branch.

        Returns conflict details on failure so the AI can resolve them.
        """
        task_state = get_task_state()
        branch = task_state.get("branch")

        if not branch:
            return {"func": "git_merge_main", "success": False, "error": "No branch in task state."}

        cwd = str(self.project_root)

        # Local-only repo (no 'origin') — nothing to fetch/merge from a remote.
        # Skip gracefully so the completion chain still finalises instead of
        # stranding on a failed `git fetch origin`
        # (m-fix-completion-strands-without-remote).
        if not self._origin_remote_exists():
            return {
                "func": "git_merge_main",
                "success": True,
                "action": "skipped",
                "branch": branch,
                "message": "No 'origin' remote configured — skipping fetch/merge of the default branch (local-only repo).",
            }

        try:
            # Detect the default branch via the shared helper (origin/HEAD →
            # main/master/develop/trunk/stable → "main") so a custom-default
            # repo fetches/merges the correct target instead of a missing main.
            default_branch = self._detect_default_branch()

            # Fetch latest from remote
            result = subprocess.run(
                ["git", "fetch", "origin", default_branch],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            if result.returncode != 0:
                return {"func": "git_merge_main", "success": False, "error": f"git fetch failed: {result.stderr.strip()}"}

            # Merge
            result = subprocess.run(
                ["git", "merge", f"origin/{default_branch}", "--no-edit"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            if result.returncode != 0:
                # Check for merge conflicts
                conflict_result = subprocess.run(
                    ["git", "diff", "--name-only", "--diff-filter=U"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
                )
                conflicting_files = conflict_result.stdout.strip().splitlines() if conflict_result.returncode == 0 else []

                return {
                    "func": "git_merge_main",
                    "success": False,
                    "error": f"Merge conflicts with {default_branch}. Resolve conflicts and call git_commit, then continue.",
                    "default_branch": default_branch,
                    "conflicting_files": conflicting_files,
                    "merge_output": result.stdout.strip(),
                    "merge_stderr": result.stderr.strip(),
                }

            return {
                "func": "git_merge_main",
                "success": True,
                "branch": branch,
                "default_branch": default_branch,
                "message": f"Successfully merged origin/{default_branch} into {branch}.",
            }

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return {"func": "git_merge_main", "success": False, "error": str(e)}

    def _func_git_push(self, args: Dict = None) -> Dict:
        """Push current branch to remote."""
        task_state = get_task_state()
        branch = task_state.get("branch")

        if not branch:
            return {"func": "git_push", "success": False, "error": "No branch in task state."}

        # Option-injection guard before the branch reaches `git push` argv. This
        # path is reachable via provider-driven completion WITHOUT the optimize/
        # local dispatch precondition, so it validates independently.
        from git_operations import validate_branch_name
        if not validate_branch_name(branch):
            return {
                "func": "git_push",
                "success": False,
                "error": f"Invalid branch name {branch!r}: only [A-Za-z0-9/._-] allowed, no leading '-'.",
            }

        cwd = str(self.project_root)

        # Local-only repo (no 'origin') — nothing to push to. Skip gracefully
        # so the completion chain still finalises instead of stranding on a
        # failed `git push origin` (m-fix-completion-strands-without-remote).
        if not self._origin_remote_exists():
            return {
                "func": "git_push",
                "success": True,
                "action": "skipped",
                "branch": branch,
                "message": "No 'origin' remote configured — skipping push (local-only repo).",
            }

        try:
            result = subprocess.run(
                ["git", "push", "-u", "origin", branch],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_SLOW, check=False, cwd=cwd,
            )
            if result.returncode != 0:
                return {"func": "git_push", "success": False, "error": f"git push failed: {result.stderr.strip()}"}

            return {
                "func": "git_push",
                "success": True,
                "branch": branch,
                "message": f"Pushed {branch} to origin.",
            }

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return {"func": "git_push", "success": False, "error": str(e)}

    def _func_create_merge_request(self, args: Dict = None) -> Dict:
        """Create the MR/PR for the task (provider-driven completion).

        NON-FATAL by contract: ``_run_completion_chain`` stops on the first
        ``success: False``, so a failed PR/MR here would strand task cleanup
        (update-issue / cleanup / clear / checkout would never run). This func
        therefore ALWAYS returns ``success: True`` and conveys the real outcome
        via ``action`` (``created`` / ``skipped`` / ``failed``) plus a
        reason/error. In particular it NEVER reports a misleading "not
        implemented" for github/gitlab when the provider IS supported but its
        sync could not be built -- the usual cause is the API token failing to
        resolve in the MCP server process (provider tokens live in the per-project
        ``.claude/state/provider-tokens.json`` file, which the MCP server reads and
        the AI cannot).
        """
        _sync_unavailable = (
            "{prov} sync unavailable: the API token did not resolve in the MCP "
            "server process, or {prov} is not enabled. Provider tokens live in the "
            "per-project .claude/state/provider-tokens.json file (which the AI cannot "
            "read) -- if it is empty, set the {prov} token there (key: {prov}) and retry."
        )
        try:
            config_file = self.project_root / "team-management" / "config.json"
            if not config_file.exists():
                return {"func": "create_merge_request", "success": True, "action": "skipped", "message": "No config file."}

            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            provider = config.get("issue_tracking", {}).get("provider", "disabled")
            if provider == "disabled":
                return {"func": "create_merge_request", "success": True, "action": "skipped", "message": "Issue tracking disabled."}

            task_state = get_task_state()
            task_name = task_state.get("task", "")
            branch = task_state.get("branch", "")

            if not task_name or not branch:
                return {"func": "create_merge_request", "success": True, "action": "skipped", "message": "No active task/branch."}

            if provider == "gitlab":
                from gitlab_utils import get_gitlab_sync
                # get_gitlab_sync now returns None on not-enabled / missing-token
                # (unified factory contract, m-provider-layer-dedup) — same as
                # get_github_sync — so no GitLab-specific try/except is needed.
                sync = get_gitlab_sync()
                if sync is None:
                    return {"func": "create_merge_request", "success": True, "action": "skipped",
                            "provider": "gitlab", "reason": _sync_unavailable.format(prov="GitLab")}
                mr_result = sync.create_merge_request_for_task(task_name, source_branch=branch)
                if mr_result:
                    return {"func": "create_merge_request", "success": True, "action": "created",
                            "provider": "gitlab", "result": str(mr_result)}
                return {"func": "create_merge_request", "success": True, "action": "failed", "provider": "gitlab",
                        "reason": "GitLab MR creation returned no merge request (see provider logs)."}

            if provider == "github":
                from github_utils import get_github_sync
                sync = get_github_sync()
                if sync is None:
                    return {"func": "create_merge_request", "success": True, "action": "skipped",
                            "provider": "github", "reason": _sync_unavailable.format(prov="GitHub")}
                pr_result = sync.create_pull_request_from_task(task_name, branch)
                if pr_result:
                    return {"func": "create_merge_request", "success": True, "action": "created",
                            "provider": "github", "result": str(pr_result)}
                return {"func": "create_merge_request", "success": True, "action": "failed", "provider": "github",
                        "reason": "GitHub PR creation returned no pull request (see provider logs)."}

            # jira (and any other provider) has no MR/PR workflow in the engine.
            return {"func": "create_merge_request", "success": True, "action": "skipped",
                    "message": f"MR/PR creation is not applicable for provider '{provider}'."}

        except Exception as e:
            # Non-fatal: a PR/MR failure must not abort task cleanup. Report it
            # honestly as a failed action rather than as a silent success.
            return {"func": "create_merge_request", "success": True, "action": "failed", "error": str(e)}

    def _func_update_issue_status(self, args: Dict = None) -> Dict:
        """Update provider issue status to completed/closed."""
        try:
            config_file = self.project_root / "team-management" / "config.json"
            if not config_file.exists():
                return {"func": "update_issue_status", "success": True, "action": "skipped", "message": "No config file."}

            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            provider = config.get("issue_tracking", {}).get("provider", "disabled")
            if provider == "disabled":
                return {"func": "update_issue_status", "success": True, "action": "skipped", "message": "Issue tracking disabled."}

            if not self._provider_issue_tracking_enabled(config, provider):
                return {"func": "update_issue_status", "success": True, "action": "skipped",
                        "message": f"{provider}.issue_tracking_enabled is false."}

            task_state = get_task_state()
            task_name = task_state.get("task", "")
            if not task_name:
                return {"func": "update_issue_status", "success": True, "action": "skipped", "message": "No active task."}

            if provider == "gitlab":
                from gitlab_utils import get_gitlab_sync
                sync = get_gitlab_sync()
                if sync:
                    sync.sync_task_status_to_gitlab(task_name, "completed")
                    return {"func": "update_issue_status", "success": True, "provider": "gitlab", "new_status": "closed"}
            elif provider == "github":
                from github_utils import get_github_sync
                sync = get_github_sync()
                if sync:
                    sync.sync_task_status_to_issue(task_name, "completed")
                    return {"func": "update_issue_status", "success": True, "provider": "github", "new_status": "closed"}
            elif provider == "jira":
                from jira_utils import get_jira_sync
                sync = get_jira_sync()
                if sync:
                    sync.sync_task_status_to_issue(task_name, "completed")
                    return {"func": "update_issue_status", "success": True, "provider": "jira", "new_status": "done"}

            return {"func": "update_issue_status", "success": True, "action": "skipped"}
        except Exception as e:
            # Non-fatal: issue status update failure should not block task cleanup
            return {"func": "update_issue_status", "success": True, "warning": str(e)}

    @staticmethod
    def _mark_task_file_completed(task_file: Path) -> bool:
        """Best-effort frontmatter rewrite for an archived task: set
        `status: completed` and add a paired `completed: <today>` date line
        (mirrors _func_update_task_status setting in-progress + started:).

        Returns True when the file was rewritten, False otherwise. Never
        raises — archiving is the load-bearing operation and must not be
        blocked by a malformed/unreadable task file.
        """
        try:
            content = task_file.read_text(encoding="utf-8")
            lines = content.split("\n")
            # Delimiters must be whole lines ("---"), never substrings — a
            # "---" inside a value must not close the frontmatter. Keys must
            # be unindented top-level keys (an indented "status:" inside a
            # block scalar is content, not metadata).
            if not lines or not re.match(r"^---\s*$", lines[0]):
                return False
            close_idx = next(
                (i for i in range(1, len(lines)) if re.match(r"^---\s*$", lines[i])),
                None,
            )
            if close_idx is None:
                return False

            updated_lines = []
            has_status = False
            has_completed = False
            for line in lines[1:close_idx]:
                if re.match(r"^status:", line):
                    updated_lines.append("status: completed")
                    has_status = True
                else:
                    if re.match(r"^completed:", line):
                        has_completed = True
                    updated_lines.append(line)
            if not has_status:
                return False

            if not has_completed:
                today = datetime.now().strftime("%Y-%m-%d")
                for anchor in (r"^started:", r"^created:"):
                    idx = next(
                        (i for i, ln in enumerate(updated_lines) if re.match(anchor, ln)),
                        None,
                    )
                    if idx is not None:
                        updated_lines.insert(idx + 1, f"completed: {today}")
                        break
                else:
                    updated_lines.append(f"completed: {today}")

            task_file.write_text(
                "\n".join([lines[0]] + updated_lines + lines[close_idx:]),
                encoding="utf-8",
            )
            return True
        except (OSError, ValueError):
            return False

    def _func_archive_task(self, args: Dict = None) -> Dict:
        """Move completed task file to tasks/done/, flipping its frontmatter
        status to completed (best-effort) on the way."""
        task_state = get_task_state()
        task_name = task_state.get("task")
        if not task_name:
            return {"func": "archive_task", "success": False, "error": "No active task."}

        # Find task file
        tasks_dir = self.project_root / "team-management" / "tasks"
        task_file = tasks_dir / f"{task_name}.md"
        if task_file.exists():
            status_updated = self._mark_task_file_completed(task_file)
            done_dir = tasks_dir / "done"
            done_dir.mkdir(parents=True, exist_ok=True)
            dest = done_dir / f"{task_name}.md"
            shutil.move(str(task_file), str(dest))
            return {
                "func": "archive_task",
                "success": True,
                "task": task_name,
                "status_updated": status_updated,
                "from": str(task_file.relative_to(self.project_root)),
                "to": str(dest.relative_to(self.project_root)),
            }

        # Check directory task
        task_dir = tasks_dir / task_name
        if task_dir.exists() and task_dir.is_dir():
            status_updated = self._mark_task_file_completed(task_dir / "README.md")
            done_dir = tasks_dir / "done"
            done_dir.mkdir(parents=True, exist_ok=True)
            dest = done_dir / task_name
            shutil.move(str(task_dir), str(dest))
            return {
                "func": "archive_task",
                "success": True,
                "task": task_name,
                "status_updated": status_updated,
                "from": str(task_dir.relative_to(self.project_root)),
                "to": str(dest.relative_to(self.project_root)),
            }

        # Check if already archived (idempotent — previous attempt may have
        # archived before failing later). A retry also repairs a stale
        # status left by a prior attempt that moved the file but died
        # before/without the frontmatter rewrite.
        done_file = tasks_dir / "done" / f"{task_name}.md"
        if done_file.exists():
            status_updated = self._mark_task_file_completed(done_file)
            return {"func": "archive_task", "success": True, "task": task_name, "already_archived": True, "status_updated": status_updated, "message": "Task already in done/."}
        done_dir = tasks_dir / "done" / task_name
        if done_dir.exists() and done_dir.is_dir():
            status_updated = self._mark_task_file_completed(done_dir / "README.md")
            return {"func": "archive_task", "success": True, "task": task_name, "already_archived": True, "status_updated": status_updated, "message": "Task already in done/."}

        return {"func": "archive_task", "success": False, "error": f"Task file not found for '{task_name}'."}

    def _func_cleanup_task_scoped_state(self, args: Dict = None) -> Dict:
        """Remove task-scoped state directory."""
        task_state = get_task_state()
        task_name = task_state.get("task")
        if not task_name:
            return {"func": "cleanup_task_scoped_state", "success": True, "cleaned_up": False, "message": "No active task."}

        cleaned = cleanup_task_state_on_completion(task_name)
        return {
            "func": "cleanup_task_scoped_state",
            "success": True,
            "task": task_name,
            "cleaned_up": cleaned,
        }

    def _func_clear_task_state(self, args: Dict = None) -> Dict:
        """Reset current_task.json to null state."""
        set_task_state(None, None, [])
        return {
            "func": "clear_task_state",
            "success": True,
            "message": "Task state cleared.",
        }

    def _func_checkout_default_branch(self, args: Dict = None) -> Dict:
        """Switch back to the repo's default branch after task completion."""
        try:
            cwd = str(self.project_root)

            # Remember current branch before switching
            prev_branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_FAST, check=False, cwd=cwd,
            )
            prev_branch = prev_branch_result.stdout.strip() if prev_branch_result.returncode == 0 else None

            # Detect default branch via the shared helper so repos with
            # custom defaults (develop/trunk/etc.) work here too — Codex
            # round-7 flagged that this func still hard-coded main/master
            # while the new dispatcher helpers had already moved to
            # origin/HEAD-based detection.
            default_branch = self._detect_default_branch()

            result = subprocess.run(
                ["git", "checkout", default_branch],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_MEDIUM, check=False, cwd=cwd,
            )
            if result.returncode != 0:
                return {"func": "checkout_default_branch", "success": False, "error": f"Failed to checkout {default_branch}: {result.stderr.strip()}"}

            message = f"Switched to {default_branch}."
            if prev_branch and prev_branch != default_branch:
                message += (
                    f" All committed changes remain on branch '{prev_branch}' "
                    f"and will be available in {default_branch} after the merge/pull request is merged."
                )

            return {
                "func": "checkout_default_branch",
                "success": True,
                "branch": default_branch,
                "previous_branch": prev_branch,
                "message": message,
            }
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return {"func": "checkout_default_branch", "success": False, "error": str(e)}

    # Shell-safety allowlists for _func_verify_tests_pass.
    # Order is enforced IN THE FUNCTION BODY — raw-string scans run BEFORE
    # shlex.split because shlex happily turns "pytest;" into a single token
    # which would hide the `;` from any post-split metacharacter check.
    _TEST_CMD_FORBIDDEN_CHARS = (';', '&&', '||', '|', '`', '$(', '>', '<')
    _TEST_CMD_ALLOWED_PREFIXES = (
        "pytest",
        "npm test",
        "cargo test",
        "go test",
        "rspec",
        "rake test",
        "python -m pytest",
        "python -m unittest",
        "python3 -m pytest",
        "python3 -m unittest",
        "jest",
    )
    # Note (Codex round-final): bare "ruby" was intentionally removed —
    # `ruby -e "<arbitrary>"` would pass the metacharacter + prefix checks
    # and execute arbitrary code under shell=False. Ruby test runners live
    # behind dedicated binaries (rspec, rake test) that cannot smuggle
    # inline code; those are what the allowlist accepts.

    def _validate_run_command(self, cmd: str, allowed_prefixes: tuple) -> Dict:
        """Validate a run-command string against forbidden metacharacters and
        a prefix allowlist, then tokenise via shlex.split.

        Shared across all engine funcs that shell out (verify_tests_pass,
        validate_metric_script, run_metric). The same RAW-STRING-FIRST order
        applies: metachars and prefix check happen on the raw string BEFORE
        shlex.split, because shlex.split('pytest;') would silently hide the
        forbidden `;` in a single token.

        Returns:
            {"success": True, "argv": [...]} on validation pass
            {"success": False, "error": "..."} on any failure
        Caller wraps with their `func` name for protocol logging.
        """
        if not isinstance(cmd, str):
            return {
                "success": False,
                "error": f"command must be a string, got {type(cmd).__name__}.",
            }

        # (1) Metacharacter scan on RAW STRING — before shlex.split.
        for bad in self._TEST_CMD_FORBIDDEN_CHARS:
            if bad in cmd:
                return {
                    "success": False,
                    "error": (
                        f"command contains forbidden metacharacter '{bad}'. "
                        f"Command must be a single runner invocation without shell features. "
                        f"Forbidden: {list(self._TEST_CMD_FORBIDDEN_CHARS)}."
                    ),
                }

        # (2) Prefix allowlist on RAW STRING. Word-boundary match — must be
        #     exactly the prefix or the prefix followed by whitespace, so
        #     'pytesting' (prefix pytest) is rejected.
        if not any(
            cmd == p or cmd.startswith(p + " ")
            for p in allowed_prefixes
        ):
            return {
                "success": False,
                "error": (
                    f"command must be exactly one of the allowlisted prefixes, "
                    f"or start with one followed by a space. Allowlist: "
                    f"{list(allowed_prefixes)}. Got: {cmd!r}."
                ),
            }

        # (3) Tokenise.
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            return {
                "success": False,
                "error": f"Failed to tokenise command: {e}",
            }

        if not argv:
            return {
                "success": False,
                "error": "command tokenises to empty argv.",
            }

        return {"success": True, "argv": argv}

    def _func_verify_tests_pass(self, args: Dict = None) -> Dict:
        """Optional pre-func for the code-review step: run the configured test
        suite and block advance on non-zero exit.

        Reads `test_command` from team-management/config.json. Missing / null →
        graceful skip. If present, the raw command string is validated BEFORE
        any tokenization:

          1. Metacharacter scan on the raw string — rejects shell-injection
             attempts like "pytest; rm -rf .". Must run on the raw string
             because shlex.split('pytest;') produces the single token 'pytest;'
             which would sail past a post-split scan.
          2. Prefix allowlist on the raw string — rejects commands not anchored
             to a known test runner.

        Only then is the command tokenised with shlex.split and executed with
        shell=False, which eliminates injection even if the allowlist is later
        relaxed.
        """
        config_file = self.project_root / "team-management" / "config.json"
        if not config_file.exists():
            return {
                "func": "verify_tests_pass",
                "success": True,
                "skipped": "no config file — nothing to verify",
            }

        # An unreadable / non-dict config must not block the code-review step
        # when test_command is optional. A fat-finger edit elsewhere in
        # config.json should not become a completion blocker (Codex round-4
        # warning). Skip the gate gracefully and surface the config error in
        # the result for the operator.
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (IOError, OSError, json.JSONDecodeError) as e:
            return {
                "func": "verify_tests_pass",
                "success": True,
                "skipped": f"config.json unreadable — test gate skipped ({e})",
            }

        if not isinstance(config, dict):
            return {
                "func": "verify_tests_pass",
                "success": True,
                "skipped": f"config.json top-level is not an object ({type(config).__name__}) — test gate skipped",
            }

        test_command = config.get("test_command")
        if test_command is None or (isinstance(test_command, str) and not test_command.strip()):
            return {
                "func": "verify_tests_pass",
                "success": True,
                "skipped": "test_command not configured",
            }

        if not isinstance(test_command, str):
            return {
                "func": "verify_tests_pass",
                "success": False,
                "error": f"test_command must be a string, got {type(test_command).__name__}.",
            }

        # Validate via shared helper (raw-string metachar scan + prefix
        # allowlist + shlex.split). Helper returns either argv or an error.
        validation = self._validate_run_command(test_command, self._TEST_CMD_ALLOWED_PREFIXES)
        if not validation["success"]:
            # Preserve "test_command" naming in the error for backward-
            # compatible regression tests that match against this string.
            err = validation["error"].replace("command ", "test_command ", 1)
            return {
                "func": "verify_tests_pass",
                "success": False,
                "error": err,
            }
        argv = validation["argv"]

        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT,
                check=False,
                cwd=str(self.project_root),
            )
        except subprocess.TimeoutExpired:
            return {
                "func": "verify_tests_pass",
                "success": False,
                "error": f"test_command timed out after {TEST_TIMEOUT}s: {test_command!r}",
            }
        except (FileNotFoundError, OSError) as e:
            return {
                "func": "verify_tests_pass",
                "success": False,
                "error": f"Failed to execute test_command {test_command!r}: {e}",
            }

        stdout_tail = (result.stdout or "")[-1000:]
        stderr_tail = (result.stderr or "")[-1000:]

        if result.returncode != 0:
            return {
                "func": "verify_tests_pass",
                "success": False,
                "error": (
                    f"Tests failed (exit {result.returncode}) for command {test_command!r}:\n"
                    f"--- stdout (tail) ---\n{stdout_tail}\n"
                    f"--- stderr (tail) ---\n{stderr_tail}"
                ),
                "exit_code": result.returncode,
            }

        return {
            "func": "verify_tests_pass",
            "success": True,
            "test_command": test_command,
            "message": f"Tests passed for {test_command!r}. Tail:\n{stdout_tail}",
        }

    def _func_capture_test_baseline(self, args: Dict = None) -> Dict:
        """Save test baseline snapshot for regression verification."""
        if not args:
            return {"func": "capture_test_baseline", "success": False, "error": "No args provided."}

        test_command = args.get("test_command", "")
        baseline_summary = args.get("baseline_summary", "")

        if not test_command:
            return {"func": "capture_test_baseline", "success": False, "error": "test_command arg is required."}
        if not baseline_summary:
            return {"func": "capture_test_baseline", "success": False, "error": "baseline_summary arg is required."}

        # Capture current branch
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=GIT_TIMEOUT_FAST, check=False,
                cwd=str(self.project_root),
            )
            captured_on_branch = result.stdout.strip() if result.returncode == 0 else "unknown"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            captured_on_branch = "unknown"

        baseline_data = {
            "test_command": test_command,
            "baseline_summary": baseline_summary,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "captured_on_branch": captured_on_branch,
        }

        baseline_file = self.state_dir / "test-baseline.json"
        try:
            ensure_state_dir()
            _write_json_durable(baseline_file, baseline_data, ensure_ascii=False)
        except (IOError, OSError) as e:
            return {"func": "capture_test_baseline", "success": False, "error": str(e)}

        return {
            "func": "capture_test_baseline",
            "success": True,
            "baseline": baseline_data,
            "message": "Test baseline captured.",
        }

    def _func_load_test_baseline(self, args: Dict = None) -> Dict:
        """Load test baseline snapshot for regression comparison."""
        baseline_file = self.state_dir / "test-baseline.json"

        if not baseline_file.exists():
            return {
                "func": "load_test_baseline",
                "success": False,
                "error": "No test baseline found at .claude/state/test-baseline.json. Cannot verify regressions without a baseline. Was the test-baseline step completed?",
            }

        try:
            with open(baseline_file, "r", encoding="utf-8") as f:
                baseline_data = json.load(f)
        except (json.JSONDecodeError, IOError, OSError) as e:
            return {"func": "load_test_baseline", "success": False, "error": f"Failed to read baseline: {e}"}

        return {
            "func": "load_test_baseline",
            "success": True,
            "baseline": baseline_data,
            "message": f"Baseline loaded. Test command: {baseline_data.get('test_command', 'unknown')}. Summary: {baseline_data.get('baseline_summary', 'unknown')}.",
        }

    def _func_cleanup_test_baseline(self, args: Dict = None) -> Dict:
        """Remove test baseline file after refactoring completion."""
        baseline_file = self.state_dir / "test-baseline.json"

        if baseline_file.exists():
            try:
                baseline_file.unlink()
            except (IOError, OSError) as e:
                return {"func": "cleanup_test_baseline", "success": False, "error": str(e)}

        return {
            "func": "cleanup_test_baseline",
            "success": True,
            "message": "Test baseline cleaned up.",
        }

    def _func_wiki_update_reminder(self, args: Dict = None) -> Dict:
        """Check if wiki is enabled and inject a reminder to update wiki pages."""
        try:
            config_file = self.project_root / "team-management" / "config.json"
            if not config_file.exists():
                return {"func": "wiki_update_reminder", "success": True, "action": "skipped", "message": "No config.json found."}

            config = json.loads(config_file.read_text(encoding='utf-8'))
            wiki_enabled = config.get("wiki", {}).get("enabled", False)
            if not wiki_enabled:
                return {"func": "wiki_update_reminder", "success": True, "action": "skipped", "message": "Wiki not enabled."}

            wiki_dir = self.project_root / "wiki"
            if not wiki_dir.is_dir():
                return {"func": "wiki_update_reminder", "success": True, "action": "skipped", "message": "wiki/ directory not found."}

            return {
                "func": "wiki_update_reminder",
                "success": True,
                "action": "reminder",
                "message": (
                    "LLM Wiki is enabled. Before completing documentation, capture any durable "
                    "knowledge this task produced into the wiki:\n"
                    "- WHAT to capture: architecture decisions, domain concepts, non-obvious patterns, "
                    "protocol details, or external integrations introduced or changed by this task.\n"
                    "- WHERE: create/update pages at wiki/pages/<category>/<slug>.md. Read wiki/schema.md "
                    "for the domain focus and the category list (## Categories); if no category fits, "
                    "propose a new one, add it to wiki/schema.md, and use it.\n"
                    "- THEN update wiki/index.md (under the category's heading) and append a line to "
                    "wiki/log.md.\n"
                    "- Reference code by symbol/file name, never line numbers. Do NOT duplicate content "
                    "that already lives in CLAUDE.md or CLAUDE.tm.md.\n"
                    "- SKIP only if this was pure refactoring or a bug fix with no new durable knowledge."
                ),
            }
        except Exception:
            return {"func": "wiki_update_reminder", "success": True, "action": "skipped", "message": "Could not check wiki config."}
