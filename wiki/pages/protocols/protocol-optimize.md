---
title: optimize Protocol
tags: [protocols, daic, git, ai-providers]
created: 2026-05-31
updated: 2026-05-31
sources: [plugin/protocol-configs/optimize.json, plugin/protocol-configs/sub-protocols/optimize-setup.md, plugin/protocol-configs/sub-protocols/optimize-metric-script.md, plugin/protocol-configs/sub-protocols/optimize-baseline.md, plugin/protocol-configs/sub-protocols/optimize-experimentation-batch.md, plugin/protocol-configs/sub-protocols/optimize-synthesis.md]
---

# optimize Protocol

`optimize` is the metric-driven optimization protocol in **interactive batched mode**: it turns "make this number better" into a reproducible hypothesis loop where the engine measures every commit and logs a TSV leaderboard, pausing for user approval at batch boundaries. The config is `optimize.json` (7 steps, `optimize.json`): setup → metric-script → baseline → experimentation (looping) → synthesis → code-review → completion.

This page walks the step flow and the batched-mode specifics. **The deep engine mechanics — the looping-step primitive, the engine-owned measurement contract (`log_experiment_result`/`run_metric`), `update_best_commit`, `check_termination`, frozen-path enforcement, the resume credential scan, and the squash+leaderboard completion — are shared with `optimize-unattended` and documented once in [Optimize Protocols](pages/protocols/optimize-protocols.md).** Its autonomous twin is [optimize-unattended](pages/protocols/protocol-optimize-unattended.md). For the protocol family overview see [Workflow Protocols](pages/protocols/workflow-protocols.md).

The two optimize protocols are a **D3 hybrid**: every sub-protocol is byte-identical **except experimentation**. `optimize` uses `optimize-experimentation-batch.md` and carries the `batch_checkpoint` post_func; the unattended twin drops it. A CI drift-guard (`test/test_optimize_json_drift_guard.py`) asserts the shared blocks stay identical and that experimentation diverges. Task convention: `o-<name>` on `optimize/<name>` (the `o-`→`optimize/` branch prefix; the `o-` prefix is required for frozen-path enforcement).

## Step 1 — setup (`mode: discussion`)

Interactive metric elicitation; the only step that writes `optimize-state.json` from setup.

- **`pre_funcs`**: `auto_detect_task`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `validate_optimize_setup`, `git_setup_branch`, `create_task_file`, `set_task_state`, `create_issue_if_enabled`, `update_task_status_in_progress`, `write_optimize_setup` (`optimize.json`).
- **`advance_args`**: `task`, `branch`, `task_content`, `metric_command`, `metric_parser`, `metric_direction`, `metric_monotonic`.

The sub-protocol (`optimize-setup.md`) opens with a **deliberately narrow v1 contract** (Section 0): single scalar metric only, monotonic direction only — no multi-objective/Pareto, no significance testing, no plateau termination. Then it elicits, in plain language: the metric (name + `min`/`max` direction + explicit monotonicity confirmation), termination conditions (`max_iterations` default 50, `max_duration` default "8h", `regression_halt_n` default 5, optional `target_metric`), noise control (`runs_per_iteration`, `aggregator`, `stability_threshold_pct`), **`batch_size`** (default 3), safety (`frozen_paths`, `env_pass`), and the `metric_command` + `metric_parser`.

The post_func ordering is a deliberate trap-avoidance: **`validate_optimize_setup` is an early gate that does NOT write**, so a bad arg aborts before `git_setup_branch`/`create_task_file` create durable side effects; **`write_optimize_setup` is the last post_func** and the sole writer of `optimize-state.json`. Both share strict arg validation (`bool` monotonic, `list` frozen_paths, numeric coercion, `max_duration` normalised to seconds). See [Optimize Protocols](pages/protocols/optimize-protocols.md#setup-arg-validation-_validate_optimize_setup_args) for the validation detail.

## Step 2 — metric-script (`mode: implementation`)

Author the measurement script; validate via a double-run stability gate.

- **`pre_funcs`**: `verify_branch_and_task`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `validate_metric_script`.

The metric-script contract (`optimize-metric-script.md`): print **exactly one float** to stdout matching `metric_parser`'s first capture group; **exit 0** (non-zero → a `crash` row, NaN value); be **deterministic** (two runs within `stability_threshold_pct`); read inputs from disk, not args; write nothing the agent depends on. The LLM authors the script, sanity-checks it manually, then advances — `validate_metric_script` runs it **twice** and gates on stability (`"Metric script stable: <v1> ≈ <v2> (Δ <pct>% ≤ <threshold>%)"`). On failure, the error block names the three fix classes: non-determinism (fix the script, stay here), legitimate variance (`protocol_goto(setup)` to raise the threshold), or wrong parser (`protocol_goto(setup)` to fix the regex).

## Step 3 — baseline (`mode: implementation`)

Capture the baseline metric on HEAD; project total cost from real timing.

- **`pre_funcs`**: `verify_branch_and_task`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `capture_metric_baseline`, `check_cost_estimate`.

`capture_metric_baseline` runs the metric **once** on HEAD and persists `baseline_metric`, `baseline_wall_clock_s`, and `baseline_commit` (to both `optimize-state.json` and task frontmatter). Then `check_cost_estimate` projects `baseline_wall_clock_s × runs_per_iteration × max_iterations`. The cost estimate lives here, not in setup, because the wall-clock figure **doesn't exist until a real run completes** (`optimize-baseline.md`). **Unbounded mode** (both `max_iterations` and `max_duration` null) rejects advance unless `args["unbounded_acknowledged"] == "i-accept-unbounded-cost"` — the typed risk acknowledgment is gated here, at the baseline, for the same reason. The working tree must be clean (the baseline reflects whatever is on disk).

## Step 4 — experimentation (`mode: implementation`, `looping_step: true`) — the batched loop

The hypothesis loop, with discussion checkpoints between batches.

- **`pre_funcs`**: `verify_branch_and_task`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `log_experiment_result`, `update_best_commit`, `check_termination`, **`batch_checkpoint`** (`optimize.json`).

The per-iteration contract (`optimize-experimentation-batch.md`): **one hypothesis = one focused commit** on the optimize branch = run the metric = `protocol_advance(args={"hypothesis": "<label>"})`. The LLM passes **only** `hypothesis` — the engine measures `metric_value`/`run_count`/`wall_clock_s` on HEAD itself (LLM-reported metrics are never trusted) and writes the TSV row. It refuses a dirty working tree (the leaderboard is only meaningful when each row's metric binds to a single commit), respects `frozen_paths` (hook-blocked), and forbids direct `results.tsv` edits.

The post_funcs run in order each iteration: `log_experiment_result` (re-measures, appends the TSV row) → `update_best_commit` (direction-aware; records `optimize.best_commit`/`best_metric` in frontmatter) → `check_termination` (four priority-ordered conditions) → `batch_checkpoint`.

### The batch checkpoint — what makes `optimize` interactive

`batch_checkpoint` (`_func_batch_checkpoint`) is **present only in this protocol**. It is **modulo-gated**: it fires only at `(loop_iteration + 1) % batch_size == 0` — mid-batch it's a no-op. At a boundary it switches DAIC to **discussion** and returns `success=False` with a summary block (last `batch_size` rows + best-so-far); wired with `post_funcs_stop_on_failure: true`, the `success=False` aborts the advance, gating the loop on user approval. Three valid moves at the gate (`optimize-experimentation-batch.md`):

- **Approve & continue**: `args={"approve_next_batch": True, "hypothesis": "<next>"}`.
- **Exit the loop**: `args={"exit_loop": True, "approve_next_batch": True}` (both required — `batch_checkpoint` runs before the engine sees `exit_loop`).
- **Restart from scratch**: `args={"restart": True}` — archives `results.tsv` to `results.tsv.run-<N>`, clears the best, resets `loop_iteration`/`experimentation_started_at`.

A **terminate-aware short-circuit** makes `batch_checkpoint` a no-op when `check_termination` already wrote the summary row this iteration, so a terminate-at-boundary doesn't force an awkward `{exit_loop, approve_next_batch}` workaround. The loop also auto-resumes after a compaction/restart (with a credential scan over `results.tsv` + `resume-stdout-tail.txt`); see [Optimize Protocols](pages/protocols/optimize-protocols.md#resume-credential-scan).

## Step 5 — synthesis (`mode: documentation`)

Findings document + non-blocking metric-gaming audit.

- **`post_funcs`** (`post_funcs_stop_on_failure: false`): `policy_compliance_audit`.

Documentation mode — write `## Findings` (best metric + producing iteration, total iterations + termination reason, time spent, top-3 hypotheses by improvement, dead ends, surprises). `policy_compliance_audit` scans `git log baseline..HEAD` for three gaming patterns — frozen-path edits, hardcoded `best_metric` constants in added diff lines, `results.tsv` edits inside hypothesis commits — and emits advisory `metric_gaming_flags`. It is **best-effort heuristic, never blocks advance** (`optimize-synthesis.md`). For each flag the LLM gives a verdict (legitimate/suspicious/ambiguous); suspicious flags → revert + `protocol_goto(experimentation)` + `restart`.

## Step 6 — code-review (`mode: implementation`) — the full two-stage gate

Spec-compliance + code-quality review on the **cumulative `baseline..best_commit` diff**.

- **`pre_funcs`**: `resolve_ai_providers`, `require_spec_review_passed`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `require_spec_review_passed`, `verify_tests_pass`, `check_completion_evidence`, `validate_code_review_in_worklog`, `validate_no_critical_issues_in_worklog`.

Identical wiring to [task](pages/protocols/protocol-task.md)'s code-review step: the `SPEC_REVIEW: PASSED` sentinel gate, the optional `verify_tests_pass`, and the `check_completion_evidence` evidence gate all apply. Uses the shared `code-review.md` sub-protocol.

## Step 7 — completion (`mode: discussion`) — squash + leaderboard

User confirms; the engine squashes from `optimize.best_commit` and ships a leaderboard MR/PR.

- **`pre_funcs`**: `present_completion_options`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `require_discard_confirmation`, `completion_dispatch`.

`completion_dispatch` detects the active protocol is `optimize` and routes to `_completion_optimize`: it reads `optimize.best_commit` + `optimize.baseline_commit` (errors if absent or **equal** — no improvement to ship), builds a direction-aware markdown leaderboard (top 10 TSV rows), and **squashes** `baseline..best` into one commit carrying the leaderboard as its message before handing off to the provider chain or the disabled-provider 4-option menu. `discard` skips the squash. Full squash sequence, branch-safety precondition, and leaderboard mechanics in [Optimize Protocols](pages/protocols/optimize-protocols.md#completion-squash--leaderboard) and [Completion and Git Flow](pages/procedures/completion-and-git-flow.md).

## Design Decisions (batched mode)

- **Batch checkpoints keep a human in the loop.** The whole point of the batched variant is steerability: every `batch_size` hypotheses the engine flips to discussion mode and waits, so the user can redirect, abort, or restart before more budget is spent. `batch_size=1` is "approve every hypothesis"; `10+` is "mostly autonomous with periodic check-ins".
- **The checkpoint is a post_func that fails closed.** Implementing the pause as `batch_checkpoint` returning `success=False` under `post_funcs_stop_on_failure: true` reuses the engine's existing gate machinery — no special "paused loop" state is needed.
- **Modulo-gating made `batch_size` real.** An earlier version fired the checkpoint every iteration regardless of `batch_size`; the `(loop_iteration + 1) % batch_size == 0` gate is what makes the configured batch size actually control cadence.

## Gotchas

- **The engine measures; the LLM only labels.** Passing `metric_value` in advance args does nothing — the engine always re-measures on HEAD. Pass only `hypothesis`.
- **Exiting at a checkpoint needs two args.** `batch_checkpoint` runs before the engine sees `exit_loop`, so leaving the loop at a boundary requires `{exit_loop: True, approve_next_batch: True}`.
- **best == baseline aborts completion.** If no iteration beat the baseline, `_completion_optimize` refuses with "no improvement found, nothing to squash".
- **Frozen paths only bite during experimentation.** The hook step-gates the freeze to the `experimentation` step — a frozen path is freely editable during `metric-script`/`baseline` (the metric script may live in a frozen dir). Surprising if you expect an absolute freeze. See [DAIC Enforcement](pages/topics/daic-enforcement.md).
- **`max_duration` must be normalised at setup.** Storing `"8h"` raw silently disables the wall-clock cap (`check_termination` does `float(...)` and swallows the error). Setup normalises to seconds — which is why the cap actually fires.
- **Shared sub-protocols are byte-identical by contract.** Editing any optimize sub-protocol except experimentation must be mirrored across both JSONs or `test_optimize_json_drift_guard.py` fails. Author in `plugin/protocol-configs/sub-protocols/` (the package source).
