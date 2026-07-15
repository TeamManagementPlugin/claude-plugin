---
description: Enable team-management for this project (plugin enablement + behavioral-guidance wiring + optional wiki)
---

# team-management: Init

Set up team-management for THIS project: enable the plugin on this machine, wire the
behavioral guidance into the project's `CLAUDE.md` via native `@`-includes (durable
across `/compact`, unlike one-shot SessionStart injection), and optionally enable the
LLM Wiki.

Two different sharing models are in play, by design:
- **Plugin enablement is per-machine.** `.claude/` is gitignored wholesale by the
  SessionStart hook (`ensure_claude_dir_gitignored`), so `.claude/settings.json` is
  NOT committed and does NOT auto-enable the plugin for teammates — each developer
  runs `/team-management:init` themselves and puts their own tokens in the per-project
  `.claude/state/provider-tokens.json` file.
- **The behavioral-guidance files ARE committed.** `CLAUDE.tm.md`,
  `team-management/knowledge/*.md`, and the managed `@`-block in `CLAUDE.md` live at
  the project root / under `team-management/` (outside the `.claude/` ignore), so the
  whole team gets the same guidance via the committed `CLAUDE.md` `@`-includes.

## Workflow

### 0. Windows runtime bootstrap (Windows only — provisions `python3`)

The plugin's hooks and MCP server launch from static manifests (`hooks.json` /
`.mcp.json`) that invoke the literal token **`python3`** — correct on macOS/Linux.
**Windows has no `python3.exe`** (only `python.exe` / the `py` launcher), so on Windows
those launches fail (`python3: command not found`) and the plugin is dead until
`python3` resolves to a **real executable**. A `python3.cmd`/`.bat` shim is NOT enough:
Claude Code spawns the MCP `command` directly, and Windows cannot direct-spawn a `.cmd`
without a shell — only a real `.exe` is found by every launch path (direct-spawn, bash,
cmd, PowerShell). Do this FIRST — before the `config_update` step below, which needs
the runtime up.

1. **Detect the platform.** On macOS/Linux skip this whole section. Treat as Windows
   when `uname` reports `MINGW*`/`MSYS*`/`CYGWIN*`, or `%OS%` is `Windows_NT`, or only
   PowerShell/cmd is available.
2. **Is `python3` already a real, runnable `.exe`?** Run `python3 --version` AND
   `where python3` (`where.exe python3`). Skip the rest ONLY if `--version` prints 3.x
   **and** the first `where python3` hit ends in **`.exe`**. If it instead resolves to a
   `.cmd`/`.bat` shim (broken for the MCP server per above, even though `--version`
   works), opens the Microsoft Store, or errors → do NOT skip; continue and provision a
   real `.exe` below.
3. **Require the `py` launcher.** Run `py --version`. If `py` is missing → STOP and tell
   the user to install Python from python.org with the **py launcher** option (or the
   Microsoft Store build, which already provides a real `python3.exe`), then re-run
   `/team-management:init`. Do not substitute another interpreter.
4. **Find the Python install dir** (holds `python.exe`, is on PATH, and — for the
   python.org installer — precedes `%LOCALAPPDATA%\Microsoft\WindowsApps`, so the shim
   is not shadowed by a Store `python3.exe` app-execution-alias):
   `py -3 -c "import os,sys;print(os.path.dirname(sys.executable))"`.
5. **Create a REAL `python3.exe` by copying `python.exe`** in that same directory. A
   copy of `python.exe` IS a working `python3` — it loads the same `pythonXX.dll` from
   its own directory — and is directly spawnable by the MCP launcher. Create it only if
   `python3.exe` is absent there. Use whichever form works
   in the agent's shell (the detection commands above are plain program calls and work
   in any shell; only this copy is shell-specific):
   - PowerShell: `Copy-Item "<dir>\python.exe" "<dir>\python3.exe"`
   - cmd:        `copy /Y "<dir>\python.exe" "<dir>\python3.exe"` (the `/Y` keeps it
     non-interactive — plain `copy` prompts `Overwrite?` and would stall the agent shell)
   - Git-bash:   `cp "<dir>/python.exe" "<dir>/python3.exe"`
6. **Verify** in a fresh process: `python3 --version` must print a 3.x version and
   `where python3` must point at the `python3.exe` you just created in the dir from step
   4 (NOT a WindowsApps alias). **If `where python3` finds nothing**, that dir is **not
   on PATH** (Python was installed without "Add Python to PATH"), so copying the exe
   there cannot help — tell the user to either re-run the python.org installer with "Add
   Python to PATH" enabled, OR add the dir to PATH (`setx PATH "%PATH%;<dir>"`, then open
   a NEW shell), and then re-run `/team-management:init`. Otherwise print the exact manual
   copy command and stop.
7. **Tell the user to RESTART Claude Code**, then continue setup. On a fresh Windows
   install the hooks (including the `config_intent_gate.py` hook that authorizes
   `config_update`) AND the MCP server were already launched broken this session and
   only retry on restart — so the wiki/guidance step below CANNOT complete now. After
   the restart, run `/team-management:config` to finish it.

### 1. Enable the plugin on this machine (`.claude/settings.json`)

1. Read the existing `.claude/settings.json` (create it as `{}` if absent). Preserve
   every key already there — this is a merge, not a replacement.
2. Merge in two keys (only adding, never clobbering unrelated config):
   - **`enabledPlugins`** — ensure `"team-management"` is enabled. Keep any others.
   - **`extraKnownMarketplaces`** — add this repo's marketplace so the plugin
     resolves. Use the marketplace source the user installed from (a git URL for a
     published repo, or the local path for a dev checkout). Ask with
     `AskUserQuestion` if the source is not obvious from the repo remotes.
   - **Do NOT write a `statusLine` here.** Claude Code does not expand
     `${CLAUDE_PLUGIN_ROOT}` inside a `settings.json` `statusLine` command, so a
     working plugin-relative entry is impossible and an absolute path would be
     per-machine. The SessionStart hook auto-pins the resolved absolute path into the
     gitignored `.claude/settings.local.json` (`_ensure_statusline_pinned`).
3. **Clean up a stale statusLine (only if present).** If a previous init left a
   `statusLine` in `.claude/settings.json` whose command contains
   `${CLAUDE_PLUGIN_ROOT}` (and references `statusline.py`), remove that entry — it
   never resolves and the hook's `settings.local.json` overrides it anyway. Leave a
   user's own custom `statusLine` untouched. Do NOT edit `.gitignore` here.
4. Write `.claude/settings.json` back with the merged content (valid JSON, 2-space
   indent). This is a whitelisted init target — approve it when prompted.

### 2. Ask about the LLM Wiki

Use `AskUserQuestion` (one question) to ask whether to enable the LLM Wiki for this
project:
- **Yes** — Claude maintains a persistent, compounding knowledge base under `wiki/`
  (you curate sources in `wiki/raw/` and run `/team-management:wiki-ingest`; Claude
  writes and maintains the pages). The wiki behavioral guidance (`CLAUDE.wiki.md`)
  gets wired into `CLAUDE.md`.
- **No** — skip the wiki. You can enable it later via `/team-management:config`.

### 3. Apply the choice via `config_update` (this also deploys + wires the guidance)

**Fresh Windows install (you just provisioned `python3` in section 0): SKIP this step
this session.** `config_update` is gated by the `config_intent_gate.py` hook and runs in
the MCP server — both were launched broken before the restart, so the call would be
refused or unavailable. Tell the user to RESTART Claude Code and then run
`/team-management:config` to apply the wiki choice and wire the guidance. On macOS/Linux
and any already-running install, proceed normally:

Call the `config_update` MCP tool **once**, ALWAYS with an explicit boolean (an empty
`config_update` is rejected by validation):
- Wiki **yes**: `config_update({"wiki.enabled": true})`
- Wiki **no**:  `config_update({"wiki.enabled": false})`

`config_update` is authorized because typing `/team-management:init` opened the
intent-gate window (the same gate `/team-management:config` opens). As a side effect
of the write it deploys the plugin-owned guidance into the project and wires the
managed `@`-include block into `CLAUDE.md`, so the guidance takes effect THIS session
without waiting for a restart:
- `CLAUDE.tm.md` (behavioral guidance) → project root
- `team-management/knowledge/*.md` (on-demand detail referenced by `CLAUDE.tm.md`) → project
- `CLAUDE.wiki.md` → project root (only when wiki is enabled)
- a `<!-- team-management:begin … --> … <!-- team-management:end -->` block in
  `CLAUDE.md` with `@CLAUDE.tm.md`, `@CLAUDE.tm.custom.md` (and `@CLAUDE.wiki.md` when
  wiki is enabled). The block is bounded by its markers — content outside it is
  preserved.

If wiki was enabled and `wiki/` does not yet exist, seed it idempotently:
`wiki/index.md`, `wiki/log.md`, `wiki/schema.md`, `wiki/raw/README.md` (the `wiki/`
directory is whitelisted, so these writes are allowed). NEVER overwrite an existing
wiki file. Pages live in category subdirectories (`wiki/pages/<category>/<slug>.md`),
created lazily on first ingest — do NOT pre-create `wiki/pages/`. Seed `wiki/schema.md`
from the plugin template, which carries a `## Categories` section listing starter
categories.

### 4. Custom-rules stub

The deploy in step 3 also creates `CLAUDE.tm.custom.md` (create-if-absent) — the
project-owned place for your custom rules / custom-protocol notes, wired into
`CLAUDE.md` via `@CLAUDE.tm.custom.md`. It is NEVER overwritten on plugin update,
whereas the package-owned `CLAUDE.tm.md` is refreshed by the SessionStart hook on
every update. Put your project rules in `CLAUDE.tm.custom.md`, not in `CLAUDE.tm.md`.

### 5. Report

Tell the user what changed and remind them:
> Plugin enablement is set for THIS machine — `.claude/` is gitignored, so it is not
> shared; each developer runs `/team-management:init` and puts their own API tokens
> in `.claude/state/provider-tokens.json` (a per-project, AI-unreadable file). The
> behavioral guidance (`CLAUDE.tm.md`,
> `team-management/knowledge/`, and the `@`-block in `CLAUDE.md`) IS committed, so the
> team shares it; the hook refreshes the package-owned copies automatically on plugin
> updates (a reviewable git diff — never auto-committed). The statusline appears after
> your next Claude Code restart.

On **Windows**, also confirm what section 0 did: if it created `python3.exe`, the user
MUST restart Claude Code once for the hooks + MCP server to start (they were launched
broken this session), and then run `/team-management:config` to finish the wiki/guidance
wiring that had to be deferred.

## Rules

- Merge, never overwrite — preserve unrelated keys in `.claude/settings.json`, and
  preserve user content in `CLAUDE.md` (the managed block is bounded by its markers).
- Never write a token/secret here — tokens live in the per-project
  `.claude/state/provider-tokens.json` file (git-ignored, AI-unreadable).
- Always pass an explicit `wiki.enabled` boolean to `config_update` — empty updates
  are rejected.
- Do not hand-edit `CLAUDE.tm.md` / `team-management/knowledge/*` / the managed
  `@`-block — they are plugin-owned and refreshed by the hook. Custom rules go in
  `CLAUDE.tm.custom.md`.
- Idempotent: re-running adds nothing when the plugin is enabled, the guidance is
  deployed + wired, and the wiki choice is already applied.
- Windows requires the **`py` launcher** (shipped by the python.org installer's "py
  launcher" option). Section 0 provisions a real `python3.exe`; never weaken it to a
  `python3.cmd`/`.bat` (the MCP server is direct-spawned and cannot launch a `.cmd`).
- The manifests (`hooks.json` / `.mcp.json`) intentionally keep the `python3` token —
  it is correct on macOS/Linux. Do NOT change it to `python`/`py` (there is no real
  `python` on modern macOS, so that would break it there); Windows is handled by the
  section-0 `python3.exe` provisioning instead.
