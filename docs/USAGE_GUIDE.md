# team-management Usage Guide

This guide complements the in-tool reference (`mcp__plugin_team-management_tm__protocol_list()`) with worked examples. For protocol shape, step lists, and DAIC mode per step, the canonical source is the JSON-driven engine — call `protocol_list()` from your Claude session to see what is currently installed.

## Workflow Protocols

All implementation work goes through a workflow protocol. The engine manages DAIC mode, task files, git branches, and completion automatically. To start one:

```
mcp__plugin_team-management_tm__protocol_start(protocol_name="<name>")
```

Available protocols: `task`, `brainstorm`, `research`, `refactoring`, `optimize`, `optimize-unattended`. See `plugin/templates/CLAUDE.tm.md` for a one-line description of each, or `protocol_list()` for the full step structure.

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

## AI Provider Phase Coverage

External AI providers (OpenAI Codex via the `codex` CLI, Google Antigravity via the `agy` CLI) run as **parallel Task agents** alongside Claude during selected protocol steps, giving you an independent second (and third) read. They never block the workflow — a provider failure degrades to a `<provider> review unavailable: …` note and the step proceeds. Their output is **advisory**: verify any finding against a real file:line before acting on it.

Enable providers globally with `ai_providers.enabled_providers` (e.g. `["codex", "agy"]`), then turn participation on per phase with the `ai_providers.include_in_*` flags below. `ai_providers.timeout` (default `300`) is currently **inert** — the wrappers enforce a fixed deadline (codex 300s, agy 330s watchdog) and do not read this key; it is kept for a future plumbing task.

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

The prompt sent to each provider for the 5 template-driven phases (every phase except `code_review`, which keeps inline prompts) is loaded from a markdown file, so you can override it per project without touching code.

**Filename convention:** `<provider>-<phase>.md`, where `<phase>` is the **hyphenated** phase name (note: the config keys use underscores, but the template basenames use hyphens). The five template-driven phases are `brainstorm`, `investigation`, `implementation`, `research-exploration`, and `refactoring-planning` — e.g. `codex-investigation.md`, `agy-refactoring-planning.md`.

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

**JSON when a program reads it; markdown when an agent reads it.** The provider's reply is read by *you* (a human or the orchestrating agent), so **default to markdown** — headings and prose, not a rigid schema. Only ask the provider for JSON when its output is consumed *programmatically* by a downstream step; in that case state the exact shape in the template and wrap it in a fenced ```` ```json ```` block. Imposing JSON on a prompt whose answer a person reads just makes the answer harder to read.
