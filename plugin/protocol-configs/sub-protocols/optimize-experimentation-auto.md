# Optimize Experimentation Sub-Protocol (Autonomous / NEVER STOP)

> **Autonomous by design (CR2-11).** This protocol enters fully autonomous implementation mode for the experimentation step. This is by design, not a generic property of `mode:implementation` steps. Other protocols' implementation steps remain bounded by their natural completion conditions; this one runs to `_func_check_termination`-driven exit or manual interrupt.

SCOPE OF THIS STEP: The autonomous hypothesis loop. One iteration = generate one hypothesis = make one focused commit on the feature branch = run the metric = call `protocol_advance`. The step is `looping_step: true` — `protocol_advance` re-runs this step's pre_funcs until either `_func_check_termination` returns `terminate=True` or `args["exit_loop"]=true` is passed (engine primitive). Unlike the batched variant, there is no user-facing checkpoint between iterations — the loop runs until a termination condition fires.

## 1. The Iteration Contract

Each iteration must:

1. **One hypothesis = one focused change.** Do not bundle unrelated optimizations into a single commit. The leaderboard and `update_best_commit` only function correctly when each commit isolates one experiment.
2. **Respect `frozen_paths`.** The hook will block writes — do not waste tokens trying to edit frozen files. If a hypothesis genuinely requires editing a frozen path, that is a scope mismatch — interrupt the run and `protocol_goto(step_name="setup", reason="frozen_paths needs adjustment")`.
3. **Do not edit `results.tsv` directly.** The engine is the only writer; direct edits are blocked by hook enforcement and would corrupt the leaderboard ordering.
4. **Commit the change before calling `protocol_advance`.** The engine enforces a clean working tree via `git status --porcelain` and refuses the iteration if dirty — the leaderboard is only meaningful when each TSV row's `metric_value` is bound to the same `commit_sha`. Suggested commit message: `iter<N>: <hypothesis>`. The error message from a dirty-tree refusal includes this suggestion verbatim.

## 2. Per-Iteration Workflow

1. **Read pre_funcs_results from the previous response.** On a resumed run, look for the hint `"Resuming from iteration N."` (CR3-8). The engine restored your state automatically.
2. **Decide the hypothesis.** A hypothesis is a one-sentence prediction: "if I do X, the metric will improve because Y."
3. **Make the edit(s)** via Edit/Write/MultiEdit. Stay focused — one logical change.
4. **Commit on the optimize branch.** Use a one-line commit subject matching the hypothesis.
5. **(Optional) Run the metric manually once** to sanity-check that the change does not crash the script. The engine will measure it again on advance — your manual run is just a smoke test.
6. **Call `protocol_advance`.** Pass only `hypothesis` (a free-text label that lands in the TSV `hypothesis` column). The engine measures `metric_value` / `run_count` / `wall_clock_s` / `aggregator` on `HEAD` via `_func_run_metric` and writes the TSV row — LLM-passed metric values are NOT trusted (the engine is the single source of truth for the leaderboard). `commit_sha` auto-falls-back to `git rev-parse --short HEAD`.

```python
mcp__plugin_team-management_tm__protocol_advance(
    summary="Hypothesis 4: cache token embeddings → metric 38.2 (was 42.0 baseline)",
    args={"hypothesis": "cache token embeddings"},
)
```

The loop does not pause for user approval between iterations — keep iterating until the engine signals termination.

## 3. Post-Funcs Chain (What Happens on Each Advance)

The engine runs these in order:

1. `log_experiment_result` — refuses if the working tree is dirty (with a suggested `iter<N>: <hypothesis>` commit message); otherwise invokes `_func_run_metric` to measure on HEAD, then appends the row to `results.tsv` with the iteration's metric, commit SHA, wall-clock, status. If `_func_run_metric` fails (subprocess crashed / parser miss / timeout), no row is written and the chain halts with `stage=run_metric` plus the captured `raw_outputs` for debugging.
2. `update_best_commit` — direction-aware (`min` or `max` from `optimize-state.json`). First iteration always sets `optimize.best_commit` and `optimize.best_metric` in task frontmatter. Subsequent iterations overwrite only on improvement.
3. `check_termination` — evaluates four conditions in priority order: `max_iterations` → `max_duration` → `regression_halt_n` → `target_metric`. First match wins. On terminate, appends a summary row (`iteration=-1, status=summary, hypothesis=<reason>`) and exits the loop.

Note: there is no `batch_checkpoint` post_func in this protocol — the loop never pauses for user approval mid-run. The only ways to exit are `_func_check_termination` returning `terminate=True`, or manual interrupt (see Section 5).

## 4. Autonomous Loop Semantics

The loop runs entirely in `mode: implementation` with no DAIC switches mid-run. There is no batch checkpoint, no per-iteration user approval, no discussion gate — this is the F6 design from the brainstorm: **NEVER STOP semantics until termination condition or manual interrupt**.

### Termination conditions (priority order)

1. **`max_iterations`** — total iterations completed reaches the configured cap. Configured at setup as an integer. Most predictable bound.
2. **`max_duration`** — wall-clock since `experimentation_started_at` (preserved across loop iterations in protocol state) reaches the configured limit. Configured as numeric seconds or `<N>h` / `<N>m` / `<N>s` shorthand.
3. **`regression_halt_n`** — the last N consecutive `ok` rows are all worse-than-best (direction-aware). Configured as an integer; useful for "give up after N misses in a row" patterns.
4. **`target_metric`** — `best_metric` reaches a configured value (direction-aware: `<= target` for `min`, `>= target` for `max`). Configured as a float; the loop exits early when good enough.

First match wins — the engine does not evaluate later conditions once an earlier one fires.

### What happens on terminate

When `_func_check_termination` matches a condition, it returns `{"terminate": True, "reason": "<one of max_iterations | max_duration | regression_halt | target_reached>", "detail": "..."}`. The engine then:

1. Appends a summary row to `results.tsv` with `iteration=-1`, `status=summary`, `hypothesis=<reason>`.
2. Skips re-running this step's pre_funcs (loop exits).
3. Auto-advances out of the loop — the next call to `protocol_advance` would land in the `synthesis` step. (No user input required.)

For unbounded runs (both `max_iterations` and `max_duration` null) the operator must rely on `regression_halt_n` / `target_metric` or manual interrupt. The setup step's cost-projection gate already requires explicit `unbounded_acknowledged` for that mode — this section assumes bounded runs as the default.

## 5. Manual Interrupt (Overnight Operators)

Unattended runs are designed for overnight or weekend execution. To stop a run early, pick the path that matches the intent:

- **Ctrl-C in the terminal** — *stop early but keep the partial run.* Interrupts the currently running command (typically the metric subprocess) but does **not** clear the protocol — the engine state in `current_task.json:protocol`, `optimize-state.json`, and `results.tsv` is preserved on disk and the protocol stays active. From here, the operator can:
  - Resume the loop: re-attach the session and call `protocol_advance` to continue iterating.
  - Skip ahead to `synthesis` and ship `optimize.best_commit` so far: `protocol_goto(step_name="synthesis", reason="manual stop, ship best so far")` then proceed normally through `code-review` and `completion`.
  - Exit the loop without going back: `protocol_advance(args={"exit_loop": True, "hypothesis": "manual stop"})` — the engine auto-advances to `synthesis`.
- **`mcp__plugin_team-management_tm__protocol_abort(reason="<why>")`** — *throw the run away.* Calls `clear_protocol_state()` and the protocol is gone. `results.tsv`, `optimize-state.json`, and the `optimize.best_commit:` / `optimize.best_metric:` lines in task frontmatter are still on disk, but there is **no active protocol to resume or `protocol_goto` from** — the operator would have to start a fresh `optimize-unattended` run (which auto-resumes the loop iteration via `_resume_protocol` only if the previous protocol was not aborted) or hand-roll the squash + MR/PR steps. Use this only when the run was a dead end and you do not want to ship anything.

If you want to ship a partial run, **use Ctrl-C, not `protocol_abort`**.

Recommended pattern for overnight runs:

1. Launch the run inside `tmux` or `screen` so disconnects don't kill the session.
2. Configure conservative `max_iterations` AND `max_duration` (so either ceiling will catch a runaway loop).
3. Set `regression_halt_n` to a value like `5` so a stuck local optimum doesn't burn the whole budget.
4. Check `team-management/tasks/<task>/results.tsv` and the task frontmatter's `optimize.best_commit:` in the morning — the engine has already auto-advanced to `synthesis` if a termination condition fired.

## 6. Resume Hint (CR3-8)

If the previous session was compacted or the host was restarted, calling `protocol_start("optimize-unattended")` in the new session triggers `_resume_protocol`. If the credential scan over `results.tsv` (last 10 rows) and `resume-stdout-tail.txt` (last 100 KB / 1000 lines) reports a hit (AWS / JWT / OAuth / GitHub / GitLab / Slack token regexes), resume is blocked — read `team-management/tasks/<task>/resume-blocked.txt` for the redacted match. If the match is a false positive, retry with `protocol_start(name="optimize-unattended", resume_force_safe=True)`. The bypass is logged in the audit trail.

## 7. Termination Reasons in Step Output (CR2-5)

When `check_termination` exits the loop, the post_funcs_results contains `{terminate: True, reason: "<one of max_iterations | max_duration | regression_halt | target_reached>", ...}`. The TSV gets a final row with `iteration=-1, status=summary, hypothesis=<reason>`. The advance summary should cite the reason verbatim:

> `"Loop terminated: reason=max_iterations after 50 iterations. Best: iter 17, metric 31.2."`

## 8. Going Back

`protocol_goto` requires an active protocol — use the **Ctrl-C path** (Section 5), not `protocol_abort`, when the intent is to back up and re-plan.

- Mid-run realization that the metric definition was wrong → Ctrl-C (protocol stays active), then `protocol_goto(step_name="setup", reason="redefine metric")`. This invalidates the `SPEC_REVIEW: PASSED` sentinel for any future code-review step (good — the review must re-run on the new definition).
- Metric script broke partway through → Ctrl-C, then `protocol_goto(step_name="metric-script", reason="...")`. Re-validate, then return here.

## 9. Advance Summary Discipline

Each iteration's advance summary should evidence what changed and what the metric did. Cite the actual TSV row that just landed. "Should improve" / "looks better" without numbers fails the `check_completion_evidence` gate that lands later in the code-review step.
