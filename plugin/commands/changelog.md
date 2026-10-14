---
description: Show the plugin's release notes (CHANGELOG.md) — latest version by default, or the versions the user asks for.
argument-hint: "[version|all]"
---

# Plugin Changelog

Present the team-management plugin's release notes from the shipped changelog.

## Steps

1. Read `${CLAUDE_PLUGIN_ROOT}/CHANGELOG.md`. If the file does not exist (very old install), say so and stop.
2. Determine the currently installed version from `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` (`version`).
3. Select what to show based on **$ARGUMENTS**:
   - empty → the newest `## [X.Y.Z]` section only.
   - a version (e.g. `0.4.4`) → that version's section; if absent, list the versions that do exist.
   - `all` → every section, newest first (summarize older ones briefly if the list is long).
4. Present the selected notes **concisely in the user's language**, keeping the bullet structure. Mention the installed version once (e.g. "installed: 0.4.5").

## Rules

- Read-only: never modify the changelog or any state files.
- Do not paraphrase entries into vagueness — keep concrete feature/config names (`safety_guards.*`, `/team-management:config`, agent names) verbatim.
- The changelog is plugin-owned and replaced on every update — direct the user to the project's git history for changes older than the changelog.
