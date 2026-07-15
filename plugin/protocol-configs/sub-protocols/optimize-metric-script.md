# Optimize Metric-Script Sub-Protocol

SCOPE OF THIS STEP: Author or refine the measurement script and validate it via a double-run stability check. Implementation mode — only the metric script and its support files may be edited here. No hypothesis edits, no source-code changes outside the metric subsystem.

## 1. The Metric Script Contract

The script must:

1. **Print exactly one float** to stdout that the configured `metric_parser` regex captures via its first group.
2. **Exit 0 on success.** Any non-zero exit is treated as a `crash` row (NaN metric value, no improvement).
3. **Be deterministic.** Two consecutive runs on the same code must yield values within `stability_threshold_pct` (default 5%). The validator enforces this in this step's post_func.
4. **Read its own inputs from disk, not args.** The protocol runs the script via `metric_command` configured in `optimize-state.json` — no per-iteration argument passing.
5. **Write nothing the agent depends on.** Side effects (e.g. a log file the agent might inspect) defeat the purpose of an isolated metric run.

## 2. Author the Script

Use Edit/Write to create or update the script file referenced by `metric_command`. Common shapes:

- **Line-count metric** (toy): `print(float(len(open("fixture.txt").read().splitlines())))`.
- **Latency metric**: time the workload, print median in ms.
- **Token-count metric**: load the prompt/response, print `len(tokenizer.encode(text))` as float.
- **Quality metric**: run an evaluator on a fixed test set, print F1 or accuracy as float.

Avoid embedding secrets or hardcoded paths to user-private data. The script will run under a filtered subprocess env (allowlisted vars only).

## 3. Sanity-Check Manually

Before calling `protocol_advance`, run the script once yourself via Bash and confirm:
- Exit 0.
- Stdout contains a parseable float matching `metric_parser`.
- The number is plausible (e.g. fixture line count == fixture line count; latency in expected range).

If the manual run fails, fix the script before advancing — the validator will reject otherwise.

## 4. Advance — Validator Runs Automatically

Call `protocol_advance(summary="...", args={})`. No special args needed.

The `_func_validate_metric_script` post_func then:
1. Runs `metric_command` once. Must exit 0; parser must yield a float.
2. Runs it a second time. Both values must be within `stability_threshold_pct` of each other.
3. Returns success on stable validation, or a structured error block on failure.

Progress text emitted by the validator (CR3-7): `"Metric script stable: <v1> ≈ <v2> (Δ <pct>% ≤ <threshold>%)"`.

## 5. On Validator Failure

The error block names: first value, second value, observed `delta_pct`, threshold. Read it carefully — three classes of fix:

- **Non-determinism in the script** (uses `time.time()`, network calls, random seeds without fixing them) → fix the script. Stay in this step; re-run `protocol_advance`.
- **Legitimate small variance** (e.g. CPU jitter on real workloads) → raise `stability_threshold_pct` via `protocol_goto(step_name="setup", reason="raise stability threshold to N%")`.
- **Wrong parser regex** (script prints `latency=42.5` but parser is `^([0-9.]+)$` which doesn't match) → fix the regex via `protocol_goto(step_name="setup", reason="fix metric_parser")`.

## 6. Going Back

If the metric definition itself was wrong (wrong direction, wrong scalar), `protocol_goto(step_name="setup", reason="...")`. The validator does NOT re-write `metric_command` / `metric_parser` — those came from setup.

## 7. Advance Summary

Once the validator passes, advance with a summary citing the actual numbers:
> `"Validator pass: run1=42.0, run2=42.0, Δ=0.00% ≤ 5.0%."`
