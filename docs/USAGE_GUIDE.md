# team-management Usage Guide

This guide complements the in-tool reference (`mcp__plugin_team-management_tm__protocol_list()`) with worked examples. For protocol shape, step lists, and DAIC mode per step, the canonical source is the JSON-driven engine — call `protocol_list()` from your Claude session to see what is currently installed.

## Workflow Protocols

All implementation work goes through a workflow protocol. The engine manages DAIC mode, task files, git branches, and completion automatically. To start one:

```
mcp__plugin_team-management_tm__protocol_start(protocol_name="<name>")
```

Available protocols: `task`, `brainstorm`, `research`, `refactoring`, `optimize`, `optimize-unattended`, `retro`. See `plugin/templates/CLAUDE.tm.md` for a one-line description of each, or `protocol_list()` for the full step structure.

## The Agent Team

team-management ships 17 specialized subagents — think of them as a team with
human-recognizable roles. Each runs in its own context window and returns
structured results. You dispatch some directly; others are brought in
automatically by the workflow protocols. Names below are the exact
`subagent_type` values.

### Investigation & design

- **code-explorer** — your codebase guide: traces a feature's execution from entry point to storage and returns the essential files.
- **code-architect** — your systems architect: turns a feature request into an implementation blueprint — files to touch, data flows, build sequence.
- **scope-strategist** — your delivery planner: defines full scope, natural phases, and execution order with parallelism opportunities.

### Challenge & risk

- **critic** — your devil's advocate: hunts flaws, risks, and unnecessary complexity in proposals, with explicit YAGNI checks.
- **user-perspective** — your user advocate: evaluates proposals from the end-user's standpoint — convenience, friction, capability delta.
- **risk-security-analyst** — your security officer: dependencies, failure modes, attack vectors, and threat models with risk matrices.

### Quality assurance

- **code-review** — your lead reviewer: correctness, security, and consistency review of the implementation diff.
- **spec-compliance-reviewer** — your spec auditor: diffs the work against the task's Success Criteria after the final quality-review round (dispatched automatically by the code-review step).
- **testing-specialist** — your test-quality engineer: negative-path coverage, isolation violations, flaky patterns (dispatched automatically on larger diffs).
- **red-team** — your second-echelon reviewer: receives the merged first-round findings and hunts for what they missed (dispatched automatically on large or Critical-bearing reviews).
- **code-cleanliness** — your maintainability reviewer: unused code, naming, duplication, complexity (dispatched automatically on larger diffs, alongside the first round; also on demand via `/team-management:clean-check`).

### Knowledge & records

- **context-gathering** — your onboarding researcher: builds a comprehensive context manifest into a new task file.
- **context-refinement** — your context keeper: updates the manifest with discoveries from the work session.
- **logging** — your scribe: consolidates work logs into clean chronological order.
- **service-documentation** — your documentation engineer: keeps CLAUDE.md module docs current.

### External reviewers

- **codex-cli** — OpenAI Codex as an independent external reviewer, run as a parallel Task agent during selected protocol steps (advisory, opt-in).
- **agy-cli** — Google Antigravity as an independent external reviewer, same dispatch model (advisory, opt-in).

External reviewers participate only when AI providers are enabled — see [AI Provider Phase Coverage](#ai-provider-phase-coverage).

## Optimization Protocols

The `optimize` and `optimize-unattended` protocols share infrastructure. Both run the cycle **setup → metric-script → baseline → experimentation → synthesis → code-review → completion**. Only the experimentation step differs:

- **`optimize`** — interactive batched mode. The experimentation loop runs `batch_size` hypotheses autonomously, then drops to discussion mode for user approval before the next batch. Use this when you want a human in the loop on every batch boundary.
- **`optimize-unattended`** — autonomous mode. The experimentation loop runs to a `_func_check_termination` exit (`max_iterations`, `max_duration`, `regression_halt_n`, optional `target_metric`) or manual `protocol_abort`. Use this for overnight or unattended runs.

### Worked example: tuning a Python hot loop's runtime

Goal: minimise `wall_clock_s` of a hot loop in `myproject/hot_path.py` over up to 30 hypotheses.

**1. Start the interactive protocol.**

```python
mcp__plugin_team-management_tm__protocol_start(protocol_name="optimize")
```

This auto-detects (or creates) an `o-<name>` task and the matching `optimize/<name>` branch. The first step is `setup` (DAIC: discussion).

**2. Provide the metric definition at the setup → metric-script transition.**

```python
mcp__plugin_team-management_tm__protocol_advance(
    summary="Metric agreed with user: minimise wall-clock seconds of hot_path.run().",
    args={
        "task": "o-hotpath-tuning",
        "branch": "optimize/hotpath-tuning",
        "task_content": "<full task markdown>",
        "metric_command": "python -m myproject.bench --json",
        "metric_parser": "json:wall_clock_s",
        "metric_direction": "min",
        "metric_monotonic": True,
        "max_iterations": 30,
        "regression_halt_n": 5,
        "runs_per_iteration": 3,
        "aggregator": "median",
        "frozen_paths": ["tests/", "myproject/bench.py"],
        "batch_size": 5
    }
)
```

`frozen_paths` is enforced by the `sessions-enforce` hook — edits to those paths during experimentation are blocked. `batch_size: 5` means a discussion checkpoint after every 5 hypotheses.

**3. Author the metric script (DAIC: implementation).**

The `metric-script` step's `validate_metric_script` post-func runs the script twice and rejects values that drift more than `stability_threshold_pct` (default 5%) — fix the script and re-call `protocol_advance` until the validator passes.

**4. Baseline (DAIC: implementation).**

`capture_metric_baseline` runs the metric on `HEAD`, persists `baseline_metric` and `baseline_wall_clock_s`, and `check_cost_estimate` projects total cost: `baseline_wall_clock_s × runs_per_iteration × max_iterations`. For the unbounded case (`max_iterations=null` and `max_duration=null`), the post-func requires `args["unbounded_acknowledged"] == "i-accept-unbounded-cost"`.

**5. Experimentation (looping).**

Each iteration: generate hypothesis → modify code → commit → run metric N times → log TSV row → update `optimize.best_commit` if improved → `check_termination`. After 5 iterations (`batch_size`), `batch_checkpoint` switches DAIC to discussion and blocks `protocol_advance` until you pass `args={"approve_next_batch": True}`. To exit the loop early before any termination condition fires, pass `args={"exit_loop": True}`.

To start fresh after a partial run, pass `args={"restart": True}` — archives `results.tsv` to `results.tsv.run-N`, resets `loop_iteration`, clears `optimize.best_commit`.

**6. Synthesis (DAIC: documentation).**

`policy_compliance_audit` scans `optimize.baseline_commit..HEAD` for frozen-path edits, hardcoded best-metric constants in added lines, and `results.tsv` edits inside hypothesis commits. Findings are emitted as `metric_gaming_flags` for the next step's reviewer — the audit never blocks advance.

**7. Code review and completion.**

The `code-review` step reviews the cumulative diff `baseline_commit..best_commit` (not interleaved iteration noise). The `completion` step squashes from `optimize.best_commit` back to `optimize.baseline_commit`, builds a leaderboard from the top 10 TSV rows in metric-direction order, and uses that leaderboard as the merge-request / pull-request description.

### Recovering a corrupted `results.tsv`

The engine rotates a backup to `team-management/tasks/<task>/.results.tsv.bak` every 100 data rows. If `results.tsv` becomes corrupted (e.g. a crashed metric writer), restore it:

```
python3 plugin/scripts/recover_tsv.py --task o-hotpath-tuning
```

Pass `--dry-run` to preview without writing. The script validates that every row has 9 tab-separated columns matching the engine's TSV schema; rows that fail validation cause exit code 2 with the offending row number.

### Drift guard between the two protocols

`optimize.json` and `optimize-unattended.json` share six step blocks verbatim: `setup`, `metric-script`, `baseline`, `synthesis`, `code-review`, `completion`. Only `experimentation` differs (batch checkpoint vs autonomous loop). `test/test_optimize_json_drift_guard.py` enforces byte-identical equality across the shared blocks via `json.dumps(..., sort_keys=True)`. Intentional changes to a shared block must be applied to both files in the same commit, or the test fails in CI.

### Frontmatter schema (engine-managed)

The engine writes the following keys into the optimize task's frontmatter; do not edit by hand:

- `optimize.best_commit`, `optimize.best_metric` — current leader
- `optimize.baseline_commit`, `optimize.baseline_metric`, `optimize.baseline_wall_clock_s` — baseline reference
- `optimize.runs_per_iteration`, `optimize.aggregator` — noise control
- `optimize.experimentation_started_at` — wall-clock anchor for `max_duration`

## Retro Protocol

The `retro` protocol turns the enforced audit trail (git history + protocol logs) into a human-readable "what did the team just do" report with week-over-week trends. Start it at the end of a work week:

```python
mcp__plugin_team-management_tm__protocol_start(protocol_name="retro")
```

Four steps: **data-collection → metrics → narrative → snapshot**. The protocol is branchless — each run creates a per-run task `r-retro-<date>` (frontmatter `branch: none`, like research tasks) whose date is engine-supplied UTC; the agent never derives the date itself.

### Stale-anchor pre-flight guard

A retro must not fabricate a confident narrative from scarce or stale data. The guard blocks report generation (structurally — the first step cannot be advanced) when any of these hold:

- git is unavailable or the directory is not a repository
- detached HEAD
- the latest commit on the default branch is older than the window
- fewer than 3 non-merge commits on the default branch within the window

The window defaults to 7 days and is set per run via the `retro_window_days` task-frontmatter key (clamped to 1-90). The pre-flight always checks the default window first; when the block reasons are exclusively window-dependent (stale anchor / scarce data) the block is marked `recoverable` — with explicit user approval the run may proceed by writing a wider `retro_window_days` into the task file, and the structural gate re-validates the anchor with that window on advance. Hard reasons (detached HEAD, git failure) are never recoverable in-protocol.

A repository without a remote is fine: the guard and the metrics read the local default branch and never contact a remote.

### v1 metrics (deterministic, engine-computed)

All metrics are computed server-side from the default branch's non-merge window commits — the agent presents them but never recomputes:

- **Sessions** — consecutive commits by the same author with gaps of at most 45 minutes count as one work session.
- **Focus score** — share of file-touches in the top touched top-level directory (root files count as `(root)`).
- **Weighted commits** — bucket weights 0.5 (< 5 LOC) / 1.0 / 1.5 (> 300 LOC). Raw LOC is deliberately downweighted: AI-assisted work inflates it.
- **Hotspots** — files touched by 5+ commits in the window.
- **Protocol stats** — protocols started, tasks completed, goto-loops and aborts inside the window, each event by its own timestamp, aggregated from `.claude/state/protocol-logs/` through a typed projection (task names, timestamps, step transitions, statuses — never raw summary/note text, which is a cross-task prompt-injection channel).

### Snapshot and trends

The narrative step writes `docs/retros/retro-<date>.md` (praise anchored to real commits, exactly one growth opportunity framed as an investment, three habits adoptable in under 5 minutes, trends). On advance the engine credential-filters the report in place, verifies the required sections, and writes a typed JSON snapshot to `docs/retros/retro-<date>.json` (`schema_version: 1`: report date, window, scalar metrics, deltas). The next run reads the previous snapshot through a strict typed schema and renders week-over-week deltas.

The protocol makes **no git commits** — the report, the snapshot, and the archived task are left in the working tree for you to review and commit (suggested message: `docs: team retro <date>`).

Accepted v1 limitations: section-heading verification does not count list items (exactly-one/exactly-three are prompt-level rules) and a heading inside a code fence would satisfy the check; file paths containing control characters arrive in git's C-quoted spelling and are bucketed under that quoted spelling in area/hotspot metrics (no parse corruption — the spelling is inert and still credential-filtered at display); commit subjects containing the `\x01` byte drop that commit from the harvest (deterministic skip — the NUL record boundary itself is unforgeable).

## Safety Guards

A destructive-Bash-command guard in the PreToolUse enforcement hook (`sessions-enforce.py`) blocks the following pattern set behind an exit-2 block plus a two-step typed confirmation:

- `rm` with recursive + force flags (`rm -rf`, `rm -r -f`, `--recursive --force`, incl. `sudo`/wrapper-prefixed forms)
- SQL `DROP TABLE` / `DROP DATABASE` / `TRUNCATE` anywhere in a command segment
- `git push --force` / `-f` / `--force-with-lease`
- `git reset --hard`
- `git checkout .` / `git checkout -- .` / `git restore .`
- `git clean` with a force flag (dry-run `-n` stays allowed)
- `kubectl delete`
- `docker rm -f`, `docker system prune`

Commands are analysed **segment-aware** (`|`, `||`, `&`, `&&`, `;`, newline chains) with word-boundary token matching, so a destructive command hidden behind `echo ok && …` is still caught, while read-side appearances (`grep "rm -rf" notes.md`) are not.

**Confirmation flow.** A blocked command prints a `[Safety Guard]` message with a one-time nonce. The agent must ask you for approval, then re-run the exact command with the confirmation prefix from the message:

```
SAFETY_CONFIRM=3f2a9c81d4e07b56 git reset --hard
```

The nonce is single-use, expires after 10 minutes, and is bound to the exact raw command string (copy the displayed line verbatim) — a different or reformatted command, a stale nonce, or a replay after use blocks again with a fresh nonce.

**Sensitivity levels** (`safety_guards.sensitivity`, settable via `/team-management:config`):

- `off` — guard disabled (equivalent to `safety_guards.enabled: false`)
- `warn-destructive-only` (default) — only the destructive pattern set above is gated
- `warn-all` — additionally, every write-pattern Bash command (`mkdir`, `cp`, `mv`, redirects, …) requires confirmation; the own-branch exception is disabled. Note: the guard binds subagents too, so under `warn-all` the AI-provider wrapper agents (codex-cli / agy-cli) — whose compound Bash uses mktemp/redirects — will be blocked and their phases degrade to `<provider> review unavailable`; expect to pair `warn-all` with disabled providers or accept the degradation

**Built-in allowances:**

- Whole-command allowlist: `rm -rf` of `node_modules` / `dist` / `build` / `__pycache__` / `.cache` / `coverage` passes without confirmation — but only when the **entire** command matches (chains, comments, or extra targets defeat it). Extend with your own exact command strings via `safety_guards.allowlist`.
- Own-branch exception (`warn-destructive-only` only): `git reset --hard` and `git push --force` pass without confirmation when you are on the task's own feature branch with **no remote-tracking ref** — these are reflog-recoverable and affect no shared state. Working-tree destruction (`git checkout .`, `git restore .`, `git clean`) is never exempted, and the exception is withheld when the command retargets another repo (`cd …`, `git -C …`).

**Best-effort friction guard, not a security control.** The confirmation is replayable within the same turn (like the discard gate) and `safety_guards.enabled` is a UX toggle, not protection against a malicious agent. Known bypasses, accepted for v1: shell variables (`T=/; rm -rf $T`), command substitution, `bash -c` / `python -c` embedded strings, `find -exec`, base64-piped payloads, unexpanded globs, wrapper commands outside the known wrapper set (`sudo`/`env`/`timeout`/`xargs`/… are covered; an exotic launcher is not), quoted chain separators / mid-command fd-redirects that sever segment parsing for trailing-positional rules (`git checkout "a;b" .` — the `.` lands in a severed segment), a persistent shell working directory (a `cd /other/repo` in a PREVIOUS Bash call is invisible to the hook, so the own-branch exception vets the wrong repo), attached-short wrapper chdir spellings (`env -C/other` — still blocked, but the own-branch exception's foreign-repo vetting misses them), and unusual `git clean` spellings whose attached/`--`-separated arguments contain `n` (misread as dry runs). Expect occasional false positives (e.g. SQL keywords inside quoted strings that are never executed, or the coreutils `truncate -s 0 log` command tripping the SQL TRUNCATE rule) — the cost is one confirmation round-trip, accepted deliberately.

## AI Provider Phase Coverage

External AI providers (OpenAI Codex via the `codex` CLI, Google Antigravity via the `agy` CLI) run as **parallel Task agents** alongside Claude during selected protocol steps, giving you an independent second (and third) read. They never block the workflow — a provider failure degrades to a `<provider> review unavailable: …` note and the step proceeds. Their output is **advisory**: verify any finding against a real file:line before acting on it.

Enable providers globally with `ai_providers.enabled_providers` (e.g. `["codex", "agy"]`), then turn participation on per phase with the `ai_providers.include_in_*` flags below. `ai_providers.timeout` (default `300`) is currently **inert** — the wrappers enforce a fixed deadline (codex 300s, agy 510s watchdog) and do not read this key; it is kept for a future plumbing task.

### agy: one-time setup per machine

agy needs one extra step that cannot be automated, and skipping it fails quietly rather than loudly.

Headless `agy -p` has nobody to answer a permission prompt, so it **auto-denies** every command that no `permissions.allow` rule grants — it still runs, still answers, and returns a review with nothing in it. That reads exactly like a clean review. `/team-management:config` therefore offers to add read-only rules (`command(git status)`, `command(git diff)`, `command(cat)`, `command(rg)`, …) to your **global** `~/.gemini/antigravity-cli/settings.json`. git is granted per subcommand, never as a bare `command(git)` — the rules are global while the deny-gate is per project, so `git push` / `git reset` / `git commit` keep prompting everywhere. It is a merge: rules you added yourself are never removed. The file is global per user — agy has no per-project settings file and no flag pointing elsewhere — which is why the framework asks instead of writing it silently, and why each machine needs it once.

If the rules are missing while agy is enabled, session start says so and the agy wrapper replies `agy review unavailable: permission rules not configured` rather than passing an empty review off as a clean one.

Two layers bound what agy can do, and they are not the same thing: the allow rules say which **binaries** agy may attempt, and the project-local `.agents/hooks.json` read-only deny-gate (deployed automatically) decides which **invocations** are reads. Inside a gated project the gate is the boundary: `command(git branch)` lets agy try `git branch -D`, and the gate is what refuses it. Inspect both with `agy -p "/permissions"` and `agy -p "/hooks"` — neither starts an agent turn or spends quota.

| Phase | Protocol step(s) | Config key | What the provider does |
|-------|------------------|------------|------------------------|
| `code_review` | `task` / `refactoring` / `optimize` / `optimize-unattended` `code-review` | `include_in_code_review` | Reviews the diff in parallel with the Claude code-review agent |
| `brainstorm` | `brainstorm` `analysis` | `include_in_brainstorm` | Independent analysis alongside the 6 specialist agents |
| `investigation` | `task` `investigation` | `include_in_investigation` | Independent reading of task scope, risks, and hidden coupling |
| `implementation` | `task` `implementation` | `include_in_implementation` | Plan review before any code is written |
| `research_exploration` | `research` `exploration` | `include_in_research_exploration` | Independent exploration of the research question |
| `refactoring_planning` | `refactoring` `planning` | `include_in_refactoring_planning` | Review of the proposed refactoring plan |

The registry that wires these phases lives in `_PHASE_REGISTRY` (`plugin/hooks/ai_providers.py`, imported by `protocol_engine.py`) — the single source of truth for contributors adding a new phase.

> **Legacy keys.** `ai_providers.include_in_architecture`, `ai_providers.include_in_exploration`, and `gemini.default_model` are deprecated. Their values are **never** auto-forwarded to the new keys; session-start emits a one-time deprecation warning when they appear so you can migrate deliberately.

## Custom AI Provider Prompt Templates

The prompt sent to each provider is loaded from a markdown file — all 6 phases, `code_review` included — so you can override any of them per project without touching code.

**Filename convention:** `<provider>-<phase>.md`, where `<phase>` is the **hyphenated** phase name (note: the config keys use underscores, but the template basenames use hyphens). The six phases are `code-review`, `brainstorm`, `investigation`, `implementation`, `research-exploration`, and `refactoring-planning` — e.g. `codex-investigation.md`, `agy-refactoring-planning.md`.

**One rule for a custom `codex-code-review.md`:** state the review scope in words, and do NOT add a scope flag — `--uncommitted`, `--base` or `--commit` — to the invocation. The codex CLI refuses to combine any of them with a prompt (`the argument '--uncommitted' cannot be used with '[PROMPT]'`), so the wrapper would drop your instructions and review to its own default brief without erroring. `codex review` already defaults to the working-tree diff. The engine rejects such a template outright rather than dispatching a prompt it knows will be discarded.

**Search order** (first hit wins; `_load_provider_template` in `plugin/hooks/ai_providers.py`):

1. `team-management/protocol-configs/custom/providers/<provider>-<phase>.md` — **your overrides** (project-local; this is where you put custom templates)
2. `plugin/protocol-configs/providers/<provider>-<phase>.md` — the plugin's shipped defaults (resolved from `CLAUDE_PLUGIN_ROOT`)
3. `team-management/protocol-configs/system/providers/<provider>-<phase>.md` — legacy deployed copy (backward-compat with pre-plugin installs)
4. A built-in inline default, plus a one-line warning on stderr

A missing template is **non-fatal** — the engine falls back to the inline default and warns rather than crashing the protocol step.

**Variables available to templates** (substituted with Python `str.format_map`; an unknown `{name}` resolves to an empty string rather than erroring):

- `{task_name}` — the active task's name
- `{branch}` — the task's git branch
- `{task_file_path}` — path to the task markdown file
- `{phase}` — the human-readable phase name (e.g. `task investigation`)
- `{plan_summary}` — the full task markdown file (frontmatter included), passed through the credential filter before injection
- `{changed_files}` — the working tree's changed paths with their git porcelain status codes, capped at 200 with an explicit dropped-count line past the cap. Populated for `code_review` only; the other five phases render it empty. Empty is also what a clean tree renders (the `optimize` protocols reach code-review with everything committed), so a template that uses it should tell the reviewer to fall back to discovering the scope itself. Note that the credential filter also runs over the finished prompt, and a few of its patterns match ordinary filenames — a path like `src/auth/api_key.py` or `.env.example` is replaced by `[REDACTED:…]`. The replacement is visible, and the provider CLIs discover the diff themselves, so treat the list as grounding rather than a complete manifest. The same applies to `{task_name}` / `{branch}` / `{task_file_path}`: a task named `m-fix-api-key-rotation` makes the whole template line carrying it render as `[REDACTED:…]`, so the provider loses task identity (it still reviews the diff). Exempting those variables from the filter is deliberately not done — a task name can legitimately carry secret-shaped material.

**JSON when a program reads it; markdown when an agent reads it.** The provider's reply is read by *you* (a human or the orchestrating agent), so **default to markdown** — headings and prose, not a rigid schema. Only ask the provider for JSON when its output is consumed *programmatically* by a downstream step; in that case state the exact shape in the template and wrap it in a fenced ```` ```json ```` block. Imposing JSON on a prompt whose answer a person reads just makes the answer harder to read.
