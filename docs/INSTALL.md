# Installing team-management (Claude Code plugin)

team-management is a **native Claude Code plugin**. You install it through Claude Code's
plugin/marketplace system — there is no pip/npm package and no separate installer to run
(the old `team-management-install` flow was retired in favour of plugin mechanics).

> team-management turns Claude Code into a structured pair-programming harness: it
> enforces a **Discussion → Alignment → Implementation → Check (DAIC)** loop through
> non-bypassable hooks, manages tasks and git branches, and drives work through
> JSON-defined workflow protocols exposed as MCP tools.

---

## 1. Prerequisites

- **Claude Code** — the CLI/IDE client that loads the plugin (hooks, MCP server, commands).
- **Python 3.10+** on your `PATH` — the plugin builds its own isolated venv on first run
  (cold-start bootstrap). On macOS/Linux it launches from `python3`. **On Windows there is no
  `python3` by default** (only `python.exe` and the `py` launcher), so the manifests' `python3`
  command won't resolve until you run `/team-management:init` once — it provisions a real
  `python3.exe` for you (this needs the `py` launcher, bundled with the python.org installer).
  See the **Windows** note under §2 below.
- **git** — strongly recommended; branch enforcement and the completion flows need it.

No system Python packages are installed: the plugin's runtime dependencies (`mcp`,
`tiktoken`, `requests`) are installed into a plugin-private venv that Claude Code stores
under its managed plugin-data directory, isolated from your environment.

---

## 2. Install the plugin

### From a marketplace

```
# In Claude Code, register the marketplace, then install the plugin:
/plugin marketplace add TeamManagementPlugin/claude-plugin
/plugin install team-management@team-management
```

`TeamManagementPlugin/claude-plugin` is the team-management repository (which carries
`.claude-plugin/marketplace.json` at its root, pointing at the `./plugin` directory); a full URL
or a local checkout path also works.
`team-management@team-management` is `<plugin-name>@<marketplace-name>` — both happen to be
`team-management`. You can also run bare `/plugin` to browse and install from the TUI.

On first use the plugin's MCP server **cold-starts**: it builds its venv under Claude
Code's managed plugin-data directory before exposing tools. The first turn may finish
before the build completes; the tools appear once the server connects (usually the next
turn). This is normal and only happens once per version.

### Enable it for your whole team

Run the init command from inside the project you want managed:

```
/team-management:init
```

> **Windows — required.** `/team-management:init` also provisions the `python3` runtime the plugin needs: it uses the `py` launcher to find your Python, then copies `python.exe` to `python3.exe`, so the manifests' `python3` command resolves. Run it once, then **fully quit and reopen Claude Code**. If you skip it, the hooks and MCP server fail to load with `python3: command not found` (or `'python3' is not recognized as an internal or external command`). The `py` launcher ships with the python.org installer.

This merges `enabledPlugins` and `extraKnownMarketplaces` into the **project**
`.claude/settings.json` (merge, not replace; no secrets). Commit that file and your
teammates auto-enable the plugin when they open the project. The statusline is **not**
written here — the SessionStart hook pins it per-machine into the gitignored
`.claude/settings.local.json`, because Claude Code does not expand `${CLAUDE_PLUGIN_ROOT}`
in a `settings.json` statusLine command.

---

## 3. Configure

Configuration is done **inside Claude Code**, not by an installer:

- **Non-secret settings** — run `/team-management:config`. A guided flow writes
  `team-management/config.json` via the `config_update` MCP tool (developer name, DAIC
  options, issue-tracking provider, AI providers, auto-compact, wiki, etc.).
- **Secret tokens** (GitLab / Jira / GitHub / Telegram) — put them in the **per-project**
  file **`.claude/state/provider-tokens.json`**, keyed by provider name (`gitlab` / `jira` /
  `github` / `telegram`). This file is git-ignored, owner-only (`0600`), and unreadable by
  Claude (a protected path), so tokens never enter `config.json` or your transcript. It is
  auto-created with blank keys plus an explanatory `_comment` the first time you run
  `/team-management:config` or start a session — open it in your editor and fill in only the
  tokens you use. Because each project has its own file, different projects can use different
  tokens.

Provider tokens resolve **`provider-tokens.json` → `config.json` fallback**, so an existing
plaintext token in `team-management/config.json` from a previous (pip-era) install keeps
working unchanged.

---

## 4. Develop the plugin from a checkout

Two ways to run the plugin from a local source checkout:

```bash
# Quickest: point Claude Code at the plugin directory directly.
claude --plugin-dir /path/to/team-management/plugin

# Or register the checkout as a local marketplace (relative/local source):
#   /plugin marketplace add /path/to/team-management
#   /plugin install team-management@team-management
```

`--plugin-dir` loads the plugin straight from `plugin/` — faithful to a marketplace
install for MCP startup, env substitution, and command/agent namespacing (it differs only
in symlink handling). It is the fastest dev loop: edit files under `plugin/`, restart the
session, and the changes are live.

The test suite runs from the repository root with `python3 -m pytest test/`. It needs
`pytest` plus the plugin's runtime dependencies (`tiktoken`, `requests`, `mcp`, pinned in
`plugin/requirements.lock`) available on your interpreter.

---

## 5. How it works in Claude Code

- **DAIC enforcement.** Hooks gate the editing tools by mode:
  - *Discussion* (default) — source edits are **blocked**; reads and read-only shell are
    allowed, forcing you to align on an approach first.
  - *Implementation* — full edit access.
  - *Documentation* — only docs (`CLAUDE.md`, task files, `docs/`) are editable.
  You don't flip modes by hand — the **protocol engine** sets the right mode per step.

- **Protocol-first workflow.** Real work goes through a protocol: discuss the request →
  Claude calls `protocol_start(protocol_name="…")` → the engine walks the steps, switching
  DAIC mode, creating the task file and git branch, and enforcing completion gates.

- **Tasks & branches.** Each task is a markdown file under `team-management/tasks/` with a
  priority prefix (`h-`/`m-`/`l-`/`r-`/`o-`/`b-`). The engine maps task type to a branch
  prefix (e.g. `fix-` → `fix/`, `o-` → `optimize/`) and blocks code edits on the wrong branch.

- **Context preservation.** State persists across sessions; auto-compact checkpoints near
  the token threshold so long sessions survive context compaction.

- **Specialized subagents.** Heavy work (code review, context gathering, exploration,
  architecture, risk analysis, logging, …) is delegated to subagents in their own contexts.

### What lives where

```
# Read-only plugin install (managed by Claude Code; replaced on every update):
plugin/        hooks, MCP server, agents, commands, protocol-configs, knowledge, templates

# Your project (created/owned by you; the plugin reads and writes here):
team-management/
  config.json           # your configuration (/team-management:config)
  tasks/                # task files (+ tasks/done/ archive, TEMPLATE.md)
  protocol-configs/custom/   # your protocol overrides (custom wins over system)
  knowledge/  wiki/ …
.claude/
  state/        # runtime state (daic-mode, current_task, …) — managed, do not edit
  settings.json # written by /team-management:init (enabledPlugins, extraKnownMarketplaces)
  settings.local.json # per-machine, gitignored; statusline pinned here by the SessionStart hook
```

---

## 6. Workflow protocols

Discover them at runtime with `protocol_list()`. The six built-ins:

| Protocol               | Steps                                                                      | Use it for                                                              |
| ---------------------- | ------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| **task**               | investigation → implementation → code-review → documentation → completion | Standard feature/bugfix lifecycle with task, branch, and review gates. |
| **brainstorm**         | topic → discussion → analysis → results → planning                        | Structured ideation with 6 parallel specialist agents; spawns tasks.   |
| **research**           | scoping → exploration → synthesis → conclusion                            | Spikes, PoCs, architecture analysis, technology evaluations.           |
| **refactoring**        | test-baseline → planning → refactoring → test-verify → code-review → completion | Safe refactoring gated by a captured test baseline.               |
| **optimize**           | setup → metric-script → baseline → experimentation* → synthesis → code-review → completion | Iterative metric-driven tuning, interactive — checkpoints between batches. |
| **optimize-unattended**| same steps as `optimize`                                                  | Autonomous twin of `optimize` — runs to a termination condition with no checkpoints (overnight sweeps). |

\* `experimentation` is a looping step. On completion both optimize protocols squash from
the best commit and ship a leaderboard in the MR/PR description.

When AI providers are configured, Codex/agy join specific steps (investigation,
implementation, code-review, brainstorm analysis, research exploration, refactoring
planning) as parallel advisory reviewers.

**Custom protocols:** drop a JSON config in `team-management/protocol-configs/custom/` to
override a system protocol of the same name, or use `/team-management:custom-protocol-create`
to fork one for editing.

---

## 7. Uninstall

### Plugin uninstall

Run `/plugin` and uninstall **team-management** (or remove it from `enabledPlugins` /
`extraKnownMarketplaces` in `.claude/settings.json`). The plugin's venv and caches live
under Claude Code's managed plugin-data directory and are cleaned up with the plugin.

### Legacy uninstall (only if you previously used the pip installer)

If this project still has artifacts from the old pip/npm installer, remove them — the
plugin's boot-detector blocks edits while a legacy install coexists and points here:

- `.claude/hooks/` — the deployed hook scripts
- `.venv-team-management/` — the old isolated interpreter
- the team-management **hook entries** in `.claude/settings.json`
- the `team-management` **server entry** in `.mcp.json`
- the `@CLAUDE.tm.md` / `@CLAUDE.wiki.md` **include lines** in your `CLAUDE.md` (if present)
- then `pip uninstall team-management` / `pipx uninstall team-management`

> **⚠️ Never delete `team-management/`.** That directory holds your task history,
> `config.json`, and custom protocols — the plugin reuses it. Removing it permanently loses
> your tasks and customizations. The uninstall steps above intentionally leave it in place.
