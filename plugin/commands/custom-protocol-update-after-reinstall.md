---
description: Reconcile custom protocols with system after a team-management update
argument-hint: ""
---

# Update Custom Protocols After Reinstall

After a team-management reinstall/update, the built-in (system) protocols may have changed. This reconciles your forked custom protocols (created via `/team-management:custom-protocol-create`) against the new system versions.

## Workflow

1. **Detect drift.** Call `mcp__plugin_team-management_tm__protocol_check_drift()`.
2. **Report the result:**
   - `drifted` — each entry's system source changed since you forked it. The new system version has been staged next to your custom copy as `new-<basename>` (e.g. `custom/new-task.json`, `custom/sub-protocols/new-task-investigation.md`). Show the user each `custom` path and its `staged` counterpart.
   - `unknown` — custom protocols with no provenance record (hand-made forks, or forked before provenance tracking). The tool cannot diff these; list them so the user knows they are untracked.
   - If `drifted` is empty, tell the user everything is up to date and stop here.
3. **Guide the merge, one file at a time.** For each drifted file, read both the user's `custom/<file>` and the staged `new-<file>`, and help the user fold the upstream changes they want to keep into their custom version. Leave the staged `new-` file untouched until they confirm.
4. **Finalize.** Once the user confirms all merges are done, call `mcp__plugin_team-management_tm__protocol_check_drift(acknowledge=True)`. This deletes the `new-` staged files and refreshes the provenance hashes so the next run starts clean. Report `removed_staging` and `acknowledged`.

## Rules

- Do NOT read or edit files under `team-management/protocol-configs/system/` — it is protected; the MCP tool reads it for you.
- The staged `new-<file>` copies live in `custom/` but are NOT active protocols — `protocol_list` ignores the `new-` prefix, and the engine never resolves them. They are scratch copies for merging only.
- Only call `acknowledge=True` after the user has finished merging — it discards the staged versions and resets the drift baseline.
