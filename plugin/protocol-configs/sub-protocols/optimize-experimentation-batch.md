# Optimize Experimentation Sub-Protocol (Batched / Interactive)

SCOPE OF THIS STEP: The hypothesis loop. One iteration = generate one hypothesis = make one focused commit on the feature branch = run the metric = call `protocol_advance`. The step is `looping_step: true` — `protocol_advance` re-runs this step's pre_funcs until either `args["exit_loop"]=true`, the user passes `args["restart"]=true`, or `_func_check_termination` returns `terminate=True`.

## 1. The Iteration Contract

Each iteration must:

1. **One hypothesis = one focused change.** Do not bundle unrelated optimizations into a single commit. The leaderboard and `update_best_commit` only function correctly when each commit isolates one experiment.
2. **Respect `frozen_paths`.** The hook will block writes — do not waste tokens trying to edit frozen files. If a hypothesis genuinely requires editing a frozen path, that is a scope mismatch — `protocol_goto(step_name="setup", reason="frozen_paths needs adjustment")`.
3. **Do not edit `results.tsv` directly.** The engine is the only writer; direct edits are blocked by hook enforcement and would corrupt the leaderboard ordering.
4. **Commit the change before calling `protocol_advance`.** The engine enforces a clean working tree via `git status --porcelain` and refuses the iteration if dirty — the leaderboard is only meaningful when each TSV row's `metric_value` is bound to the same `commit_sha`. Suggested commit message: `iter<N>: <hypothesis>`. The error message from a dirty-tree refusal includes this suggestion verbatim.

## 2. Per-Iteration Workflow

1. **Read pre_funcs_results from the previous response.** On a resumed run, look for the hint `"Resuming from iteration N. Pass restart=true to start fresh."` (CR3-8). The engine restored your state automatically.
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

## 3. Post-Funcs Chain (What Happens on Each Advance)

The engine runs these in order:

1. `log_experiment_result` — refuses if the working tree is dirty (with a suggested `iter<N>: <hypothesis>` commit message); otherwise invokes `_func_run_metric` to measure on HEAD, then appends the row to `results.tsv` with the iteration's metric, commit SHA, wall-clock, status. If `_func_run_metric` fails (subprocess crashed / parser miss / timeout), no row is written and the chain halts with `stage=run_metric` plus the captured `raw_outputs` for debugging.
2. `update_best_commit` — direction-aware (`min` or `max` from `optimize-state.json`). First iteration always sets `optimize.best_commit` and `optimize.best_metric` in task frontmatter. Subsequent iterations overwrite only on improvement.
3. `check_termination` — evaluates four conditions in priority order: `max_iterations` → `max_duration` → `regression_halt_n` → `target_metric`. First match wins. On terminate, appends a summary row (`iteration=-1, status=summary, hypothesis=<reason>`) and exits the loop.
4. `batch_checkpoint` (modulo-gated) — only fires at `(loop_iteration + 1) % batch_size == 0`. Mid-batch is a no-op. At the boundary, switches DAIC to discussion mode and blocks advance with a summary block (last `batch_size` rows + best-so-far).

## 4. Discussion Checkpoint

At a batch boundary, the response will contain a `post_funcs_results` entry with `func: "batch_checkpoint", success: false, awaiting_approval: true`. The error string contains the summary. The engine has switched DAIC to discussion mode. Three valid moves:

- **Approve and continue**: `protocol_advance(args={"approve_next_batch": True, "hypothesis": "next-hypothesis-label"})`. The next iteration starts immediately.
- **Exit the loop**: `protocol_advance(args={"exit_loop": True, "approve_next_batch": True})` — both args required because `batch_checkpoint` runs before the engine sees `exit_loop`.
- **Restart from scratch**: `protocol_advance(args={"restart": True})`. Archives `results.tsv` to `results.tsv.run-<N+1>`, clears `optimize.best_commit` and `optimize.best_metric` from frontmatter, resets `loop_iteration` to 0 and `experimentation_started_at` to now. The discoverability hint references this on every resumed run.

## 5. Resume Hint (CR3-8)

If the previous session was compacted or restarted, calling `protocol_start("optimize")` in the new session triggers `_resume_protocol`. If the credential scan over `results.tsv` and `resume-stdout-tail.txt` reports a hit, resume is blocked — read `resume-blocked.txt` for the redacted match. If the match is a false positive, retry with `protocol_start(name="optimize", resume_force_safe=True)`. The bypass is logged in the audit trail.

## 6. Termination Reasons in Step Output (CR2-5)

When `check_termination` exits the loop, the post_funcs_results contains `{terminate: True, reason: "<one of max_iterations | max_duration | regression_halt | target_reached>", ...}`. The TSV gets a final row with `iteration=-1, status=summary, hypothesis=<reason>`. The advance summary should cite the reason verbatim:

> `"Loop terminated: reason=max_iterations after 50 iterations. Best: iter 17, metric 31.2."`

## 7. Going Back

- Mid-batch realization that the metric definition was wrong → `protocol_goto(step_name="setup", reason="redefine metric")`. This invalidates the `SPEC_REVIEW: PASSED` sentinel for any future code-review step (good — the review must re-run on the new definition).
- Metric script broke partway through → `protocol_goto(step_name="metric-script", reason="...")`. Re-validate, then return here.

## 8. Advance Summary Discipline

Each iteration's advance summary should evidence what changed and what the metric did. Cite the actual TSV row that just landed. "Should improve" / "looks better" without numbers fails the `check_completion_evidence` gate that lands later in the code-review step.
