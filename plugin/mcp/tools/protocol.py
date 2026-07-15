"""
Protocol engine MCP tools.

Manages structured workflow protocols with step-by-step advancement,
DAIC mode enforcement per step, pre/post funcs execution, and audit logging.
"""

from typing import Dict, Any

from core.project import _import_from_hooks


def register_tools(mcp):
    """Register protocol tools with the FastMCP server."""

    def _check_enabled():
        """Check if protocol engine is enabled. Returns error dict or None."""
        try:
            import json
            shared_state = _import_from_hooks("shared_state")
            config_file = shared_state.PROJECT_ROOT / "team-management" / "config.json"
            if config_file.exists():
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                if not config.get("protocol_engine", {}).get("enabled", True):
                    return {"success": False, "error": "Protocol engine is disabled."}
                return None  # Found config, engine is enabled
        except Exception:
            pass  # Default to enabled
        return None

    def _get_engine():
        """Get ProtocolEngine instance."""
        protocol_engine = _import_from_hooks("protocol_engine")
        shared_state = _import_from_hooks("shared_state")
        return protocol_engine.ProtocolEngine(shared_state.PROJECT_ROOT)

    @mcp.tool()
    def protocol_list() -> Dict[str, Any]:
        """List all available workflow protocols with their step counts and descriptions."""
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.list_protocols()
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_start(protocol_name: str, task: str = None,
                       resume_force_safe: bool = False) -> Dict[str, Any]:
        """Start a workflow protocol. Sets DAIC mode and executes first step's pre_funcs.

        Args:
            protocol_name: Name of the protocol to start (e.g., "task", "research").
            task: Optional existing task name. When provided for the "task" protocol,
                  the investigation step will review and enrich the existing task
                  instead of creating from scratch. Pass empty task_content in
                  protocol_advance to keep your on-disk task file — the engine
                  re-validates it (frontmatter / status / prefix / branch /
                  ## Success Criteria) instead of overwriting.
            resume_force_safe: When the protocol is already active with
                  loop_iteration > 0 (optimize protocols), start_protocol
                  auto-resumes after a credential-pattern scan. Set this
                  flag to bypass the scan when it returns false positives —
                  the bypass is recorded in the protocol audit log.
        """
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.start_protocol(protocol_name, task=task,
                                          resume_force_safe=resume_force_safe)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_current() -> Dict[str, Any]:
        """Get current protocol state with step details, start/end text, and full steps overview."""
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.get_current()
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_advance(summary: str, args: dict = None) -> Dict[str, Any]:
        """Advance to next protocol step. Executes post_funcs, logs, advances, executes pre_funcs."""
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.advance_step(summary, args=args)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_goto(step_name: str, reason: str) -> Dict[str, Any]:
        """Go back to a previous protocol step. Only backward navigation allowed."""
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.goto_step(step_name, reason)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_log(task_name: str = None) -> Dict[str, Any]:
        """Get the protocol audit log for a task (defaults to current task)."""
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.get_log(task_name)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_abort(reason: str) -> Dict[str, Any]:
        """Abort the current protocol. Logs reason and sets DAIC to discussion mode."""
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.abort_protocol(reason)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_save_note(note: str) -> Dict[str, Any]:
        """Save a note to the protocol log. Notes survive session restarts."""
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.save_note(note)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_available_funcs() -> Dict[str, Any]:
        """List all available functions for pre_funcs/post_funcs in protocol configs.

        Returns metadata for each function including description, typical usage
        (pre_func or post_func), required/optional args, whether it reads task state,
        and side effects. Useful when creating or editing custom protocol JSON configs.
        """
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.get_available_funcs()
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_customize(protocol_name: str, force: bool = False) -> Dict[str, Any]:
        """Bootstrap-copy a system protocol into custom/ for full local editing.

        Copies the protocol's JSON + every referenced @sub-protocol + the provider
        templates for its AI phases from protocol-configs/system/ into
        protocol-configs/custom/, and records a provenance sidecar
        (custom/.forked-from.json) so protocol_check_drift can later detect upstream
        changes. The engine already resolves custom/ over system/, so the copied
        files take effect immediately. Existing custom files are preserved unless
        force=True. Runs in the MCP server process (hook-exempt to read the
        protected system/ tree). Surfaced by the /team-management:custom-protocol-create command.

        Args:
            protocol_name: Name of the system protocol to fork (e.g. "task").
            force: Overwrite existing custom files instead of skipping them.
        """
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.customize_protocol(protocol_name, force=force)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def protocol_check_drift(acknowledge: bool = False) -> Dict[str, Any]:
        """Detect/reconcile drift between forked custom protocols and the system.

        Reads the provenance sidecar written by protocol_customize. For each
        recorded file whose system source changed since the fork (e.g. after a
        team-management reinstall), stages the new system version next to the
        custom copy as 'new-<basename>' and returns a merge report. Call again
        with acknowledge=True after merging to remove the staged files and refresh
        the recorded hashes. Custom protocols without a provenance entry are
        reported as unknown-provenance. Runs in the MCP server process (reads the
        protected system/ tree). Surfaced by the
        /team-management:custom-protocol-update-after-reinstall command.

        Args:
            acknowledge: Finalize after merging — remove 'new-' staging and
                refresh the sidecar hashes to the current system.
        """
        disabled = _check_enabled()
        if disabled:
            return disabled
        try:
            engine = _get_engine()
            return engine.check_drift(acknowledge=acknowledge)
        except Exception as e:
            return {"success": False, "error": str(e)}
