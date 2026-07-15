# Optimize Synthesis Sub-Protocol

SCOPE OF THIS STEP: Documentation only — write the experimentation findings into the task work log. No code edits. The `policy_compliance_audit` post-func runs heuristic metric-gaming detection and emits advisory flags for the next step.

## 1. Findings Section in the Task File

Append a `## Findings` section (or extend the existing one) with:

- **Best metric** and the iteration that produced it. Cite the row from `results.tsv`.
- **Total iterations** run; how the loop terminated (the `check_termination` reason from the summary row).
- **Time spent** (compute from first vs last `timestamp` in `results.tsv`).
- **Top 3 hypotheses by improvement** (if direction is `min`, sort ascending; if `max`, descending). Reuse `_build_leaderboard` semantics conceptually — the actual leaderboard is generated again at completion for the MR/PR description.
- **Dead ends** (hypotheses that crashed or worsened) — surface lessons for future runs.
- **Surprises** — anything counter-intuitive that the user should know.

## 2. Policy Compliance Audit (Non-Blocking)

`_func_policy_compliance_audit` scans `git log --name-only <baseline_commit>..HEAD` and emits non-blocking flags for three patterns the v1 design explicitly cares about:

1. **Frozen-path edits** — any commit that touched a path in `optimize-state.json:frozen_paths`. This means hook enforcement was bypassed somehow, or `frozen_paths` was widened mid-run. Always investigate.
2. **Hardcoded best_metric constants in added diff lines** — a commit that adds the literal best-so-far number to the source. Often a sign of metric-gaming (the agent learned the parser and is satisfying the regex by hardcoding the answer).
3. **`results.tsv` edits inside hypothesis commits** — the engine is the only legitimate TSV writer. Any commit that touches `results.tsv` is suspicious.

The audit is **best-effort heuristic, not a security control** (per brainstorm scope F2/F10). It will produce false positives. It will miss anything not in the three patterns above.

## 3. What to Do With Findings

The audit returns a `metric_gaming_flags` list in the post_funcs result. For each flag:

- **Brief description** (audit does that for you — "frozen-path edit in commit abc123: src/frozen.py").
- **Verdict**: legitimate (e.g. `frozen_paths` was widened intentionally) / suspicious (the diff really does game the metric) / ambiguous.
- **Action**: if suspicious → revert the commit and re-run from before that iteration via `protocol_goto(step_name="experimentation")` and `protocol_advance(args={"restart": True})`. If legitimate, document it in `## Findings` so the code-review step's reviewer doesn't re-flag it.

## 4. Mode Note

This step is `mode: documentation` — only `team-management/tasks/<task>.md` and `CLAUDE.md` files are editable. If you discover a bug that needs a code edit, `protocol_goto(step_name="experimentation")` first; do not write TODOs or skip fixes.

## 5. Advance Summary

```
mcp__plugin_team-management_tm__protocol_advance(
    summary="Synthesis: best=31.2 at iter 17 (caching pass). Loop ended max_iterations after 50. policy_audit: 0 flags.",
    args={},
)
```
