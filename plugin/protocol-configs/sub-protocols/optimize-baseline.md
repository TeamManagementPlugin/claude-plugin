# Optimize Baseline Sub-Protocol

SCOPE OF THIS STEP: Run the metric ONCE on the current HEAD to establish the baseline. Implementation mode — no hypothesis edits in this step; the working tree must be clean.

## 1. What This Step Does

The `_func_capture_metric_baseline` post_func runs `metric_command` exactly once and persists three values:

- `optimize-state.json:baseline_metric` — the measured metric value at HEAD.
- `optimize-state.json:baseline_wall_clock_s` — how long that single run took (this drives the cost projection).
- `optimize-state.json:baseline_commit` AND `task-frontmatter:optimize.baseline_commit` — the commit SHA the baseline was measured against. Used by the synthesis-step `policy_compliance_audit` to scope the audit to `baseline..HEAD`.

After the baseline is captured, `_func_check_cost_estimate` runs and computes the projected total wall-clock cost.

## 2. Why Cost Estimate Lives Here, Not in Setup

The brainstorm wording put the cost estimate "at end of setup". That wording was imprecise — the projection requires `baseline_wall_clock_s × runs_per_iteration × max_iterations`, and the wall-clock figure does not exist until the baseline run completes. Running cost estimate any earlier would be hand-waved (user-supplied estimate, not measured), which the brainstorm explicitly rejected ("not a user-supplied estimate"). Hence the wiring: `setup` collects the user's settings; `baseline` measures the real timing and reports the projection.

## 3. Bounded vs Unbounded

`_func_check_cost_estimate` branches on the termination configuration:

- **Bounded** (at least one of `max_iterations` or `max_duration` is set): returns a projection like `"projected_wall_clock = 2.5s × 3 runs × 50 iterations = 375s (~6m 15s)"`. No user input required to advance.
- **Unbounded** (`max_iterations` AND `max_duration` both `null`): rejects advance unless `args["unbounded_acknowledged"] == "i-accept-unbounded-cost"`. The literal string is the typed risk-checklist confirmation.

## 4. Advance — No Args (Bounded) / Typed Ack (Unbounded)

```python
# Bounded case
mcp__plugin_team-management_tm__protocol_advance(summary="Baseline=42.0, wall=2.5s; projection 375s for 50 iters @ 3 runs", args={})

# Unbounded case
mcp__plugin_team-management_tm__protocol_advance(
    summary="Baseline=42.0, wall=2.5s; running unbounded with explicit acknowledgement",
    args={"unbounded_acknowledged": "i-accept-unbounded-cost"},
)
```

## 5. On Capture Failure

If `_func_capture_metric_baseline` fails, the post_func chain stops. Common causes:

- **Working tree dirty**: the baseline run reflects whatever is on disk, including unsaved hypothesis changes. Clean the working tree (`git stash` or `git restore`) and `protocol_advance` again.
- **`metric_command` broke since metric-script step**: re-run it manually, identify the breakage, `protocol_goto(step_name="metric-script")` to refine.
- **Subprocess timeout (>600s)**: the metric is too slow for routine runs. Decide whether to optimize the metric script itself (`protocol_goto(step_name="metric-script")`) or accept a longer per-iteration cost.

## 6. Advance Summary

Cite the actual numbers:
> `"Baseline metric=42.0; baseline_wall_clock_s=2.5; cost projection 375s for max_iterations=50 × runs_per_iteration=3."`
