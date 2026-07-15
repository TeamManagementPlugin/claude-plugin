---
description: Fork a system protocol into custom/ so you can customize it
argument-hint: "[protocol-name]"
---

# Create a Custom Protocol

Fork one of the built-in (system) protocols into `team-management/protocol-configs/custom/` so you can edit its steps, modes, funcs, and step text freely. The engine resolves `custom/` over `system/`, so your fork takes effect immediately — no further wiring needed.

## Workflow

1. **List the available protocols.** Call `mcp__plugin_team-management_tm__protocol_list()`. For each protocol, present to the user:
   - its **name** and one-line **description**, and
   - its **steps** in order — each step's `name`, `mode`, and `description` (from the `steps` field),

   so the user understands how the protocol works and can choose which one to take as a base / rewrite.
2. **If the user passed a protocol name as the argument**, confirm it appears in the list, then skip to step 4 with that choice.
3. **Otherwise ask which protocol to fork** with `AskUserQuestion` — one option per protocol, the option `description` summarizing what that protocol does. (The widget appends an "Other" option automatically — do not add one.)
4. **Fork it.** Call `mcp__plugin_team-management_tm__protocol_customize(protocol_name="<choice>")`.
   - If the result lists files under `skipped`, a previous fork already exists; those files were preserved. Tell the user, and only re-run with `force=True` on their explicit confirmation (it overwrites their edits).
5. **Report** the `created` (and any `skipped`) file paths plus the `next_steps` text. Tell the user to edit the copied files under `team-management/protocol-configs/custom/`, and that the engine now resolves their custom versions over system automatically.

## Rules

- Do NOT read or edit files under `team-management/protocol-configs/system/` — it is a protected path; the MCP tool reads it for you inside the server process.
- Do NOT try to copy protocol files with Bash/Edit/Write — `sessions-enforce` blocks reads of `system/`. Only `protocol_customize` (MCP server, hook-exempt) can do the copy.
- After a future team-management reinstall, run `/team-management:custom-protocol-update-after-reinstall` to reconcile upstream changes against your fork.
