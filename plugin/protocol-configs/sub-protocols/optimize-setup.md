# Optimize Setup Sub-Protocol

SCOPE OF THIS STEP: Interactive metric elicitation, scope agreement, and persistence of all optimize settings to `optimize-state.json`. Discussion mode — no code edits in this step. The metric script itself is authored in the next step.

## 0. Out-of-Scope Notice (READ FIRST)

The optimize protocols ship with a **deliberately narrow v1 contract**. Do NOT attempt to optimize any of the following — they are tracked as future work, not v1 features:

- **Multi-objective optimization** (Pareto fronts, weighted sums, lexicographic ordering) — single scalar metric only.
- **Statistical significance testing on metric deltas** — `regression_halt_n` consecutive worsenings is the only built-in regression detector.
- **Plateau-based termination** — covered indirectly by `max_iterations`.
- **Non-monotonic metrics** — every metric must improve monotonically in one direction (`min` or `max`).

If the user describes a goal that needs any of the above, surface it as a constraint and offer to either narrow the scope or open a follow-up task. Do not silently push the protocol past its v1 contract.

## 1. Discuss the Metric

Establish with the user, in plain language:

1. **What single number measures success?** Examples: latency p95 in ms, prompt tokens per query, F1 score, lines of code, build time in seconds, GPU memory peak in MB.
2. **Direction**: `min` (lower is better, e.g. latency, cost) or `max` (higher is better, e.g. accuracy, F1)?
3. **Monotonicity confirmation (CR3-12)**: explicitly ask the user — "is improvement always in this direction, no edge cases where the opposite is better?". Capture their answer (`metric_monotonic: true|false`). If `false`, halt and recommend either narrowing the metric (e.g. constraining the regime where direction holds) or aborting in favour of a research protocol.

## 2. Termination Conditions

Establish termination with explicit defaults — leaving any of these unset is a deliberate choice the user must make:

- `max_iterations` — default `50`. Set lower (e.g. 10) for quick spikes; set `null` for unbounded mode.
- `max_duration` — default `"8h"`. Plain seconds (number) or shorthand `<N>h`/`<N>m`/`<N>s`. Set `null` for no wall-clock cap. *(Setup normalises to seconds before persisting; check_termination reads the numeric value directly.)*
- `regression_halt_n` — default `5` consecutive worsenings vs. best. Set `null` to disable.
- `target_metric` — optional early exit when best meets target.

**Unbounded mode** (`max_iterations: null` AND `max_duration: null`) requires explicit typed risk acknowledgement at the `baseline` step (`unbounded_acknowledged="i-accept-unbounded-cost"`). Do NOT set both to `null` without flagging this to the user.

## 3. Noise Control

For deterministic metrics (line count, file size, parser-checked output), defaults are fine:
- `runs_per_iteration: 1`, `aggregator: "median"`, `stability_threshold_pct: 5.0`.

For metrics with run-to-run variance (latency, ML training, network calls):
- `runs_per_iteration: 3` or higher, `aggregator: "median"` (most robust to outliers).
- Raise `stability_threshold_pct` if the validator's double-run check rejects scripts the user considers stable.

## 4. Batch Size

`batch_size` (default `3`) controls how many hypotheses run autonomously between discussion checkpoints. The `_func_batch_checkpoint` post-func switches DAIC mode to discussion at every Nth iteration boundary.

- `1` = approve each hypothesis individually (very interactive).
- `3-5` = recommended for iterative tuning where the user wants to redirect every few hypotheses.
- `10+` = mostly autonomous with periodic check-ins.

## 5. Safety: frozen_paths and env_pass

- `frozen_paths` — list of paths the agent **must not modify** during experimentation. Hook-enforced via `is_frozen_path`. Use this to protect: training data, the metric script itself, evaluation rubrics, anything where editing it would game the metric. Default `[]`.
- `env_pass` — extra environment variable names allowed through to the metric subprocess (extends the built-in allowlist `PATH`/`HOME`/`USER`/`LANG`/`VIRTUAL_ENV`/`CARGO_HOME`/`CARGO_MANIFEST_DIR`/`NODE_PATH`/`PYTHONPATH`/`PYTHONDONTWRITEBYTECODE`/`GOPATH`/`RUSTUP_HOME`). Anything matching `*KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*`/`*CREDENTIAL*` is stripped regardless. Default `[]`.

## 6. Metric Command + Parser

Even though the script itself is written in the next step, the user names them now:

- `metric_command` — exact shell invocation. Must start with one of the allowlisted metric-command prefixes (`python`/`python3`/`node`/`deno`/`bash`/`sh`/`make`/`npm`/`yarn`/`pnpm`/`cargo`/`go`/`pytest`/`jest`/`rspec`); validated when the metric runs. Source of truth: `_METRIC_CMD_ALLOWED_PREFIXES` in `optimize_completion.py`. Example: `python3 metric.py`.
- `metric_parser` — regex with one capture group extracting a float from stdout. Example: `^([0-9.]+)$` (line containing only the number); `latency_p95=([0-9.]+)` (key=value form).

## 7. Compose the Task File

Use `team-management/tasks/TEMPLATE.md`. Frontmatter must include `task: o-<name>` (the `o-` prefix is required for `optimize/` branch mapping per T1 enforcement) and `branch: optimize/<name>`. The `## Success Criteria` section should name the metric, the direction, and the target (or "best achievable in N iterations" if no target is set).

## 8. Worked `protocol_advance` Example

```python
mcp__plugin_team-management_tm__protocol_advance(
    summary="Setup approved: minimize latency_p95 over 20 iterations, batch_size=3, ...",
    args={
        "task": "o-latency-p95",
        "branch": "optimize/latency-p95",
        "task_content": "<full markdown task file>",
        "metric_command": "python3 measure.py",
        "metric_parser": "^([0-9.]+)$",
        "metric_direction": "min",
        "metric_monotonic": True,
        "max_iterations": 20,
        "max_duration": "4h",
        "regression_halt_n": 5,
        "target_metric": 100.0,
        "runs_per_iteration": 3,
        "aggregator": "median",
        "stability_threshold_pct": 5.0,
        "batch_size": 3,
        "frozen_paths": ["src/measure.py", "data/eval/"],
        "env_pass": ["MY_ENV_VAR"],
    },
)
```

## 9. Alignment Before Advance

Before calling `protocol_advance`, you MUST:

1. Present the user with a concise summary: metric (name + direction + target), termination conditions (iterations/duration/regression/target), batch size, frozen paths, and the proposed task file structure.
2. Wait for EXPLICIT user agreement. Silence ≠ agreement.
3. Compose the task file content per the template.
4. Call `protocol_advance` with all required + relevant optional args.

The protocol engine automatically: creates the task file, sets task state, creates + checks out the `optimize/<name>` branch, creates the provider issue (if enabled), flips status to in-progress, and writes `optimize-state.json`.

DIRTY WORKING TREE: If there are uncommitted changes when you call `protocol_advance` (e.g. you wrote the task file first), `git_setup_branch` pauses with `needs_confirmation=true` and lists the dirty files. Ask the user whether to carry them onto the new branch (re-run `protocol_advance` with `carry_changes: true` in args) or to commit/stash them first.

## 10. Going Back

If during a later step an assumption breaks (script can't be written cleanly, metric is non-deterministic, scope creeps), use `protocol_goto(step_name="setup", reason="...")` to return here and re-elicit.
