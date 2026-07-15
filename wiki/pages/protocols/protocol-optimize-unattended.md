---
title: optimize-unattended Protocol
tags: [protocols, daic, git, ai-providers]
created: 2026-05-31
updated: 2026-05-31
sources: [plugin/protocol-configs/optimize-unattended.json, plugin/protocol-configs/sub-protocols/optimize-experimentation-auto.md, plugin/protocol-configs/sub-protocols/optimize-setup.md]
---

# optimize-unattended Protocol

`optimize-unattended` is the **autonomous twin** of [optimize](pages/protocols/protocol-optimize.md): the same metric-driven hypothesis loop, but with **no batch checkpoints** — the experimentation step runs unattended to a termination condition (or a manual interrupt), making it suitable for overnight and weekend runs. The config is `optimize-unattended.json` (7 steps, `optimize-unattended.json`): setup → metric-script → baseline → experimentation (looping) → synthesis → code-review → completion.

**Six of the seven steps are byte-identical to `optimize`.** Only the experimentation step differs: it references `optimize-experimentation-auto.md` instead of `optimize-experimentation-batch.md`, and its `post_funcs` chain **drops `batch_checkpoint`** — `[log_experiment_result, update_best_commit, check_termination]` instead of the batched `[…, batch_checkpoint]` (`optimize-unattended.json`). This page documents the differences; for the full step-by-step flow read [optimize](pages/protocols/protocol-optimize.md), and for the shared engine mechanics (measurement contract, `update_best_commit`, `check_termination`, frozen paths, resume scan, squash+leaderboard) read [Optimize Protocols](pages/protocols/optimize-protocols.md). For the protocol family overview see [Workflow Protocols](pages/protocols/workflow-protocols.md).

The two protocols are a **D3 hybrid**, and a CI drift-guard (`test/test_optimize_json_drift_guard.py`) asserts the shared sub-protocols stay byte-identical while experimentation diverges as expected. Task convention is the same: `o-<name>` on `optimize/<name>`.

## What's identical to `optimize`

Steps 1-3 and 5-7 are shared verbatim with the batched protocol:

- **setup** (`mode: discussion`) — same interactive metric elicitation, same v1 contract, same arg validation, same `write_optimize_setup` as the sole `optimize-state.json` writer. `batch_size` is still elicited but is **inert** here (there's no checkpoint to gate).
- **metric-script** (`mode: implementation`) — same metric-script contract + double-run stability gate.
- **baseline** (`mode: implementation`) — same single-run baseline capture + cost projection + unbounded-mode typed acknowledgment.
- **synthesis** (`mode: documentation`) — same findings document + non-blocking `policy_compliance_audit`.
- **code-review** (`mode: implementation`) — same full two-stage gate (`SPEC_REVIEW: PASSED` sentinel + `verify_tests_pass` + `check_completion_evidence`).
- **completion** (`mode: discussion`) — same `_completion_optimize` squash-from-`best_commit` + leaderboard MR/PR. The dispatcher routes `optimize` **and** `optimize-unattended` identically (`get_protocol_state().name in ("optimize", "optimize-unattended")`).

## What's different — the experimentation step (`mode: implementation`, `looping_step: true`)

The autonomous loop. **No DAIC switches mid-run, no per-iteration user gate, no discussion checkpoint** — "NEVER STOP semantics until a termination condition fires or the operator interrupts" (`optimize-experimentation-auto.md`).

- **`pre_funcs`**: `verify_branch_and_task`.
- **`post_funcs`** (`post_funcs_stop_on_failure: true`): `log_experiment_result`, `update_best_commit`, `check_termination`. **No `batch_checkpoint`.**

The per-iteration contract is otherwise identical to the batched variant: one hypothesis = one focused commit = `protocol_advance(args={"hypothesis": "<label>"})`, engine-owned measurement on HEAD (LLM metric values untrusted), dirty-tree refusal, `frozen_paths` respected, no direct `results.tsv` edits. The difference is purely the absence of the pause: after the post_funcs run, the loop re-runs the step's pre_funcs immediately, with no user approval in between. The only exits are `check_termination` returning `terminate=True` or a manual interrupt.

> **Autonomous by design (CR2-11).** The sub-protocol calls out that this step enters fully autonomous implementation mode *by design* — not as a generic property of `mode: implementation` steps. Other protocols' implementation steps remain bounded by their natural completion conditions; this one is explicitly licensed to keep going.

### Termination conditions (priority order)

The loop's only automatic exit is `check_termination`, which evaluates four conditions in priority order and stops on the first match (`optimize-experimentation-auto.md`):

1. **`max_iterations`** — iteration count reaches the cap. The most predictable bound.
2. **`max_duration`** — wall-clock since `experimentation_started_at` (preserved across iterations) reaches the limit. Numeric seconds or `<N>h`/`<N>m`/`<N>s`.
3. **`regression_halt_n`** — the last N consecutive `ok` rows are all worse-than-best (direction-aware). "Give up after N misses in a row".
4. **`target_metric`** — `best_metric` reaches the target (direction-aware). Early exit when good enough.

On terminate, the engine appends a `status=summary`, `iteration=-1` row to `results.tsv`, skips re-running the loop's pre_funcs, and **auto-advances to `synthesis`** with no user input. For unbounded runs (both `max_iterations` and `max_duration` null) the only automatic stoppers are `regression_halt_n` / `target_metric` — which is why unbounded mode requires the typed `unbounded_acknowledged` at the baseline step.

## Manual Interrupt — the operator's two paths

Because there's no checkpoint, stopping an unattended run early is an operator action, and **the choice of how matters** (`optimize-experimentation-auto.md`):

- **Ctrl-C in the terminal — *stop early but keep the partial run.*** Interrupts the currently-running command but does **not** clear the protocol; engine state (`current_task.json:protocol`, `optimize-state.json`, `results.tsv`) is preserved and the protocol stays active. From here the operator can resume the loop (`protocol_advance`), skip to synthesis and ship best-so-far (`protocol_goto(step_name="synthesis", ...)`), or exit the loop (`protocol_advance(args={"exit_loop": True, "hypothesis": "manual stop"})` → auto-advances to synthesis).
- **`protocol_abort(reason=...)` — *throw the run away.*** Calls `clear_protocol_state()`; the protocol is gone. `results.tsv`, `optimize-state.json`, and the `optimize.best_*` frontmatter lines remain on disk for forensic salvage, but there is **no active protocol to resume or `protocol_goto` from** — shipping would require a fresh `optimize-unattended` run (which auto-resumes only if the previous protocol was **not** aborted) or hand-rolling the squash.

**To ship a partial run, use Ctrl-C, not `protocol_abort`.** Likewise, `protocol_goto` requires an active protocol — back up via the Ctrl-C path, not abort.

### Recommended overnight pattern

The sub-protocol prescribes (`optimize-experimentation-auto.md`): launch inside `tmux`/`screen` (disconnects don't kill it); set **both** `max_iterations` **and** `max_duration` (either ceiling catches a runaway); set `regression_halt_n` (e.g. 5) so a stuck local optimum doesn't burn the whole budget; check `results.tsv` and the frontmatter `optimize.best_commit:` in the morning — the engine has already auto-advanced to `synthesis` if a condition fired.

## Resume after compaction / host restart

If the session was compacted or the host restarted, `protocol_start("optimize-unattended")` triggers `_resume_protocol`, which auto-resumes the loop iteration **and** runs a credential scan over the last 10 `results.tsv` rows + the last 100 KB/1000 lines of `resume-stdout-tail.txt` (AWS/JWT/OAuth/GitHub/GitLab/Slack regexes). A hit writes `resume-blocked.txt` and refuses to resume unless `resume_force_safe=True` (forwarded by the MCP `protocol_start` wrapper; bypass logged). Best-effort, not a security control. See [Optimize Protocols](pages/protocols/optimize-protocols.md).

## Design Decisions

- **NEVER STOP is opt-in, not a side effect.** Autonomy is a property of *this* protocol's experimentation step, deliberately separated from the batched twin so that "implementation mode" never silently means "run unbounded". The CR2-11 wording in both the JSON description and the sub-protocol preamble makes the licence explicit.
- **Dropping one post_func is the whole behavioural delta.** Rather than a separate engine code path, unattended mode is the batched config minus `batch_checkpoint`. The completion dispatcher already recognized both names, so shipping the twin required no engine change — just a new JSON + one new sub-protocol.
- **Ctrl-C vs abort encodes intent.** Keeping the protocol active on Ctrl-C (so a partial run is salvageable) while making `protocol_abort` a clean throw-away gives the overnight operator two clearly-distinct outcomes instead of one ambiguous "stop".
- **Both ceilings recommended for unattended runs.** With no human in the loop, `max_iterations` + `max_duration` + `regression_halt_n` together are the defense against a runaway burning the whole budget.

## Gotchas

- **`batch_size` is inert here.** It's still accepted at setup (the sub-protocols are shared), but with no `batch_checkpoint` post_func nothing reads it during experimentation. Setting it has no effect on an unattended run.
- **`protocol_abort` strands a salvageable run.** It clears the active protocol, so even though `results.tsv` and `optimize.best_commit` survive on disk, there's nothing left to resume or `goto` from. Use Ctrl-C when you want to keep the partial result.
- **Unbounded runs have no automatic clock stopper.** With both `max_iterations` and `max_duration` null, only `regression_halt_n` / `target_metric` or a manual interrupt can stop the loop — which is why the baseline step demands the typed `i-accept-unbounded-cost` acknowledgment.
- **Termination auto-advances to synthesis with no confirmation.** When a condition fires, the engine moves on by itself — the operator finds the run already at `synthesis` rather than paused awaiting input. This is the intended unattended behaviour, not a missed gate.
- **Everything else is `optimize`.** For setup arg validation, the metric-script contract, the engine-owned measurement invariant, frozen-path step-gating, and the squash+leaderboard completion, see [optimize](pages/protocols/protocol-optimize.md) and [Optimize Protocols](pages/protocols/optimize-protocols.md) — they are not duplicated here.
