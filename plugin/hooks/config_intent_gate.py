#!/usr/bin/env python3
"""Config-session intent-gate hook (m-config-mcp-flow).

Writes ``.claude/state/config-session.flag`` when the user PHYSICALLY invokes
``/team-management:config``, opening the gate that ``config_update`` (MCP) checks.
This is deterministic code running OUTSIDE the LLM — the LLM cannot author the
flag itself because ``.claude/state`` is a PROTECTED path (SEC-006). That is the
whole point of the gate: the LLM must not be able to change config during normal
work; only the user typing the command opens the window.

Wired to BOTH events in ``hooks.json``:
  - ``UserPromptExpansion`` (PRIMARY) — fires on the structured command expansion
    (``command_source == "plugin"`` + ``command_name``), per spike F7. Precise:
    it does NOT fire on a mere prose mention of the command text.
  - ``UserPromptSubmit`` (BACKUP) — fires when the raw prompt's first token is the
    command, in case a future Claude Code build stops emitting the expansion event.

Plugin-only: no-ops unless ``CLAUDE_PLUGIN_ROOT`` is set (``is_plugin_mode``), so a
legacy/dev checkout never trips it. Always exits 0 — it must never block a prompt.
"""
import json
import sys

from shared_state import is_plugin_mode, write_config_session_flag

# command_name forms the expansion event may carry (namespaced + bare fallback).
# /team-management:init is included so init can drive config_update (e.g. set
# wiki.enabled) through the same validated, intent-gated path the user explicitly
# opened by typing the command (h-durable-guidance-via-claude-md).
_COMMAND_NAMES = {"team-management:config", "config", "team-management:init", "init"}
_RAW_COMMANDS = {"/team-management:config", "/team-management:init"}


def _should_fire(data):
    """True when the payload represents a real /team-management:config invocation."""
    # PRIMARY — UserPromptExpansion structural match (a real plugin command).
    if data.get("command_source") == "plugin" and data.get("command_name") in _COMMAND_NAMES:
        return True
    # BACKUP — UserPromptSubmit raw text: the first whitespace-delimited token is
    # exactly the command (so "/team-management:config" and "… <args>" match, but
    # "tell me about /team-management:config" does not).
    prompt = data.get("prompt", "")
    if isinstance(prompt, str):
        first = prompt.strip().split()[:1]
        if first and first[0] in _RAW_COMMANDS:
            return True
    return False


def main():
    if not is_plugin_mode():
        return
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if _should_fire(data):
        try:
            write_config_session_flag(session_id=data.get("session_id"))
        except OSError:
            pass


if __name__ == "__main__":
    main()
    sys.exit(0)
