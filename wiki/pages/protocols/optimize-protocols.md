---
title: Optimize Protocols
tags: [protocols, git, daic]
created: 2026-05-31
updated: 2026-06-10
sources: [plugin/protocol-configs/optimize.json, plugin/protocol-configs/optimize-unattended.json, plugin/hooks/optimize_completion.py, plugin/protocol-configs/sub-protocols/optimize-setup.md, plugin/protocol-configs/sub-protocols/optimize-experimentation-batch.md, plugin/protocol-configs/sub-protocols/optimize-experimentation-auto.md, plugin/hooks/sessions-enforce.py]
---

`optimize` and `optimize-unattended` are the two metric-driven optimization protocols. They turn "make this number better" into a reproducible loop: define a single scalar metric + measurement script, capture a baseline, then iterate one-hypothesis-one-commit while the engine measures every commit and logs a TSV leaderboard. The engine — not the LLM — is the single source of truth for every metric value, so the resulting leaderboard cannot be gamed by a hallucinated number. At completion the protocol squashes the feature branch down to `baseline..best_commit` and ships an MR/PR carrying the leaderboard.

The two protocols are a **D3 hybrid**: every sub-protocol is shared verbatim except the experimentation step. `optimize` is interactive batched (checkpoints between batches return DAIC to discussion for user approval); `optimize-unattended` is the autonomous twin (no batch checkpoints, runs to a termination condition — designed for overnight runs). A CI drift-guard (`test/test_optimize_json_drift_guard.py`) asserts the shared blocks stay byte-identical and that experimentation diverges as expected.

**This page is the shared-mechanics reference for both.** For the per-protocol step-by-step walkthroughs (and the batched-vs-autonomous differences framed from each protocol's side), see [optimize Protocol](pages/protocols/protocol-optimize.md) and [optimize-unattended Protocol](pages/protocols/protocol-optimize-unattended.md).

All the machinery lives in `OptimizeCompletionMixin` (`optimize_completion.py`), composed into `ProtocolEngine` via `class ProtocolEngine(OptimizeCompletionMixin, AIProvidersMixin)`. The mixin imports nothing from `protocol_engine` (one-way dependency to avoid an import cycle) and reaches core engine helpers through `self`.

## Step Flow

Both protocols are 7 steps (`optimize.json`, `optimize-unattended.json`):

1. **setup** (`mode: discussion`) — Interactive metric elicitation. `post_funcs`: `[validate_optimize_setup, git_setup_branch, create_task_file, set_task_state, create_issue_if_enabled, update_task_status_in_progress, write_optimize_setup]`. Note the ordering trap: `validate_optimize_setup` is an **early gate that does NOT write** (`_func_validate_optimize_setup`, `optimize_completion.py`) so a bad arg aborts *before* `git_setup_branch`/`create_task_file`/`create_issue_if_enabled` create durable side effects. `write_optimize_setup` is the **last** post_func and the only writer of `optimize-state.json` from setup (`_func_write_optimize_setup`). Both share `_validate_optimize_setup_args`; the write func re-validates defensively. `optimize-state.json` is in `PROTECTED_PATHS`, so direct edits are blocked — this is the only legitimate write path during setup.
2. **metric-script** (`mode: implementation`) — Author the measurement script; `validate_metric_script` runs it twice and gates on stability within `stability_threshold_pct` (default 5%).
3. **baseline** (`mode: implementation`) — `capture_metric_baseline` runs the metric once on HEAD; `check_cost_estimate` projects total wall-clock from real timing.
4. **experimentation** (`mode: implementation`, `looping_step: true`) — The hypothesis loop. See below.
5. **synthesis** (`mode: documentation`, `post_funcs_stop_on_failure: false`) — Findings document; `policy_compliance_audit` is a non-blocking metric-gaming scan.
6. **code-review** (`mode: implementation`) — Spec-compliance + code-quality review on the cumulative `baseline..best_commit` diff. AI providers join via `resolve_ai_providers` pre_func.
7. **completion** (`mode: discussion`) — `completion_dispatch` routes to `_completion_optimize`: squash + leaderboard MR/PR.

### Setup arg validation (`_validate_optimize_setup_args`)

Strict by design — several traps are guarded against:
- `metric_monotonic` must be a real `bool`. Truthy strings like `"false"` are rejected because `bool("false") == True` would silently defeat the v1 monotonic-only gate. `metric_monotonic=false` is also rejected outright (v1 contract; recommends the research protocol instead).
- `frozen_paths` / `env_pass` must be `list`s. A bare string `"src/foo.py"` would coerce via `list(...)` to a char array, silently disabling frozen-path enforcement for the real path.
- `max_iterations` / `regression_halt_n` / `target_metric` are strictly coerced so a stringified `"fifty"` cannot slip past `check_termination`'s silent `except: pass`.
- `max_duration` is normalised to **seconds** at the setup boundary via `_parse_duration_to_seconds` (accepts `null`, numeric, or `"8h"`/`"30m"`/`"90s"`). This is deliberate: `check_termination` does `float(max_duration)` and silently passes on `ValueError`, so a non-numeric default like `"8h"` would silently disable the wall-clock cap if stored raw.
- Typo guard: `_detect_optimize_key_typos` fuzzy-matches unrecognised keys against `_OPTIMIZE_SETUP_KEYS` via difflib — advisory only, never blocks.

## The Experimentation Loop

`looping_step: true` is the engine primitive (`protocol_engine.py`): `protocol_advance` re-runs the **same step's** pre_funcs instead of advancing, until one of: `args["exit_loop"]=true` (caller-driven), a post_func returns `terminate=True` (engine-driven, from `check_termination`), or `args["restart"]=true` (reset). `_handle_loop_iteration` (`protocol_engine.py`) increments `loop_iteration` and **preserves `experimentation_started_at`** across iterations (the wall-clock anchor for `max_duration`).

The per-iteration contract: one hypothesis = one focused commit on the feature branch. The engine refuses a dirty working tree (see Dirty-tree gate below) because the leaderboard is only meaningful when each TSV row's `metric_value` is bound to a single `commit_sha`.

**`optimize` experimentation post_funcs** (`optimize.json`): `[log_experiment_result, update_best_commit, check_termination, batch_checkpoint]`.
**`optimize-unattended` experimentation post_funcs** (`optimize-unattended.json`): same minus `batch_checkpoint` — `[log_experiment_result, update_best_commit, check_termination]`.

### `log_experiment_result` — engine-owned measurement contract

This is the most important invariant in the whole protocol. The func **always invokes `_func_run_metric()`** to measure on HEAD; LLM-passed metric values are NOT trusted. The LLM passes only `hypothesis` (+ optional `commit_sha`, `iteration_override`). Flow:
1. **Control-call short-circuit**: if `args["approve_next_batch"]` or `args["exit_loop"]` is set, skip — no new iteration occurred, logging again would write a spurious duplicate row.
2. **Dirty-tree pre-check**: runs `git -c core.quotePath=false status --porcelain --untracked-files=all`, filters through `_filter_engine_owned_dirty_lines`, refuses with a suggested `iter<N>: <hypothesis>` commit message if user-side changes remain. The `core.quotePath=false` and `--untracked-files=all` flags are load-bearing (Unicode task names; fresh untracked task dirs collapsing to a single `??` line). The gate lives here, NOT in `_func_run_metric`, because `validate_metric_script` legitimately runs the metric while the script is still uncommitted.
3. **SHA captured BEFORE run_metric** so the recorded `commit_sha` matches the measured tree even if the metric script mutates HEAD.
4. Append TSV row, `fsync`, rotate `.results.tsv.bak` every 100 data rows.

`_filter_engine_owned_dirty_lines` allowlist (engine-written, excluded from the dirty gate): `<task>.md`, `<task>/README.md`, `<task>/results.tsv`, `<task>/.results.tsv.bak`, `<task>/results.tsv.run-<N>`, `<task>/resume-*.txt`, and the bare task-dir entry. Renames are filtered only when BOTH paths are engine-owned. User-authored files inside the task dir (e.g. `metric.py`) are NOT filtered — those edits must still block.

TSV schema (`_TSV_HEADER`): `iteration  timestamp  commit_sha  metric_value  run_count  aggregator  wall_clock_s  status  hypothesis`. The terminator row uses `status="summary"`, `iteration=-1` — an internal-only path that bypasses both the dirty-tree gate and the run_metric call.

### `run_metric` — filtered, allowlisted subprocess

Runs `metric_command` N times (`runs_per_iteration`), parses each stdout with `metric_parser` (first capture group → float), aggregates via `aggregator` (median/mean/min/max, `_aggregate_metric`). The command is validated through `_validate_run_command` against `_METRIC_CMD_ALLOWED_PREFIXES` (python/node/bash/make/npm/cargo/go/pytest/jest/…). Env is filtered (`_build_metric_env`): starts from `_METRIC_ENV_ALLOWLIST` extended by user `env_pass`, then strips any var whose UPPER name contains `KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`CREDENTIAL` (`_CREDENTIAL_KEY_PATTERNS`). Runs `subprocess.run(shell=False, timeout=600)`.

**Bounded parser matching** (l-optimize-robustness-cleanup): the user-supplied `metric_parser` regex never runs in the engine process. `_bounded_regex_search` caps stdout at `METRIC_STDOUT_SEARCH_CAP` (256 K chars) and executes `re.search` in a **killable child process** (`subprocess.Popen` of `python -I -c`, pattern+text as stdin JSON — no argv injection/leakage, `communicate(timeout=METRIC_PARSER_TIMEOUT_S=5.0)`, `kill()` on timeout). A daemon-thread `join(timeout)` was tried first and **cannot work**: stdlib `re` holds the GIL for the entire C-level match, so the parent's join never wakes while a catastrophic-backtracking pattern spins. On truncation the trailing partial line is dropped so a number cut mid-digits (42.567 → "42.5") errors loudly instead of parsing a wrong value (accepted gap: a newline-free capped prefix is not dropped). `Popen` rather than `subprocess.run` is deliberate — tests patch `protocol_engine.subprocess.run` with side_effect lists the guarded match must not consume. The stability gate in `validate_metric_script` is symmetric at zero: `delta_pct` uses `max(|v1|,|v2|)` as denominator (both-zero → 0% stable, one-zero → 100% in either order, never `inf`), with a zero-run hint in the failure message.

### `update_best_commit`

Direction-aware (`min`/`max` from `optimize-state.json`). First iteration always sets `optimize.best_commit` + `optimize.best_metric` in task **frontmatter** (via `_set_optimize_field`); later iterations overwrite only on improvement. Falls back to the last `ok` TSV row + `git rev-parse --short HEAD` when args are absent. Also short-circuits on control calls.

### `check_termination` — four conditions, priority order

1. `loop_iteration >= max_iterations`
2. `now - experimentation_started_at >= max_duration`
3. last `regression_halt_n` ok-rows all worse than best (`_is_worse`)
4. `target_metric` reached (direction-aware: `<= target` for min, `>= target` for max)

First match wins. On terminate, `_terminate` appends the `status=summary` row and returns `terminate=True`, driving the loop exit.

## Interactive Batched vs Unattended

The only behavioural difference is `batch_checkpoint` (`_func_batch_checkpoint`), present only in `optimize`:
- **Modulo-gated**: fires only at `(loop_iteration + 1) % batch_size == 0`. Mid-batch is a no-op (this closed a T2 gap where `batch_size` was cosmetic and the checkpoint fired every iteration).
- At a boundary it switches DAIC to **discussion** (`set_daic_mode("discussion")`) and returns `success=False` with a summary block (last batch + best-so-far). Wired with `post_funcs_stop_on_failure: true`, so `success=False` aborts the advance — gating the loop on user approval.
- Three valid moves at the gate (see `optimize-experimentation-batch.md` §4): approve (`args={"approve_next_batch": True, "hypothesis": ...}`), exit (`args={"exit_loop": True, "approve_next_batch": True}` — both required because `batch_checkpoint` runs before the engine sees `exit_loop`), or restart (`args={"restart": True}`).
- **Terminate-aware short-circuit**: if `check_termination` already wrote the summary row this iteration, batch_checkpoint becomes a no-op — otherwise a terminate-at-boundary (e.g. `max_iterations=4`, `batch_size=2`) would force the user to pass an awkward `{exit_loop, approve_next_batch}` workaround.

`optimize-unattended` has no such gate — it runs entirely in `mode: implementation` with no DAIC switches until termination or manual interrupt. Its sub-protocol (`optimize-experimentation-auto.md`) documents the manual-interrupt distinction: **Ctrl-C keeps the partial run** (protocol stays active, resume/`goto`/exit-loop available), while **`protocol_abort` throws it away** (no active protocol to resume — though `results.tsv`, `optimize-state.json`, and the `optimize.best_*` frontmatter lines stay on disk for forensic salvage). To ship a partial run, use Ctrl-C, not abort.

### Restart (`_handle_loop_restart`, `protocol_engine.py`)

`args["restart"]=true` is only valid on a `looping_step`. It archives `results.tsv` to `results.tsv.run-<N>` (next available N), clears `optimize.best_commit`/`optimize.best_metric` from frontmatter (otherwise `update_best_commit` would compare new iterations against the pre-restart best), and resets `loop_iteration=0` and `experimentation_started_at=now`.

### Resume credential scan

`start_protocol` auto-resumes when the same protocol is active with `loop_iteration > 0` (`protocol_engine.py`). `_resume_protocol` runs `_resume_credential_scan` over the last 10 TSV rows + the last 100 KB/1000 lines of `resume-stdout-tail.txt` (AWS/JWT/OAuth/GitHub/GitLab/Slack regexes). A hit writes `resume-blocked.txt` and refuses to resume unless `resume_force_safe=true` (forwarded by the MCP `protocol_start` wrapper; bypass logged in the audit trail). Best-effort, not security.

## Frozen Paths

`frozen_paths` (set at setup) are files the agent must not edit during experimentation — protect training data, the metric script, eval rubrics, anything where editing it would game the metric. Enforcement lives in the pre-tool-use hook, NOT this mixin.

- Written to `optimize-state.json` exclusively via `shared_state.write_optimize_state` (the file is in `PROTECTED_PATHS` so direct edits are blocked).
- `sessions-enforce.py:_load_frozen_paths` reads the list and gates `Edit/Write/MultiEdit/NotebookEdit` plus write-flavoured Bash via `is_frozen_path` / `_bash_targets_frozen`.
- **Two fast-path short-circuits return `[]`** (zero overhead / correct scoping): (1) the state file is absent — non-optimize sessions; (2) the active protocol step is not `experimentation`, OR no protocol is active. Earlier steps (`metric-script`, `baseline`) must author/run the metric script, which may itself live in a frozen directory; later steps (`synthesis`/`code-review`/`completion`) don't edit code. A stale `optimize-state.json` from an aborted run with no active protocol also short-circuits.
- This is a **best-effort workflow guard, not a security control** — the Bash-target extractor is regex-based per-command rules, not a shell parser, and is fully bypassable.

`policy_compliance_audit` (synthesis step) is the after-the-fact counterpart: a heuristic `git log baseline..HEAD` scan flagging (a) frozen-path edits, (b) the literal `best_metric` value appearing in added diff lines (hardcoded-constant gaming), (c) `results.tsv` edits inside hypothesis commits. It emits `metric_gaming_flags` for the code-review prompt and **never blocks advance**. Its frozen-path matching uses `_audit_path_matches_frozen` — a component-boundary matcher (equality or containment under a directory entry) that deliberately **replicates** `sessions-enforce._path_matches_frozen` rather than importing it (the hook file has a hyphenated name and runs `json.load(sys.stdin)` at module level — an import would hang). The earlier suffix `endswith` match gave both false positives (`vendor/src/foo.py` vs frozen `src/foo.py`) and false negatives (frozen `src/` never matched `src/foo.py`).

## Completion: Squash + Leaderboard

`_func_completion_dispatch` checks `get_protocol_state().name in ("optimize", "optimize-unattended")` and routes to `_completion_optimize`:

1. **Branch-safety precondition**: HEAD must equal the task's recorded feature branch (refuses to squash on the wrong branch).
2. Reads `optimize.best_commit` + `optimize.baseline_commit` from frontmatter. Errors if either is absent, `"-"`, or **equal** (no improvement to squash).
3. Builds the leaderboard (`_build_leaderboard`): top 10 ok-rows from `results.tsv`, direction-aware sort, markdown table. Injected into the squash commit message and the MR/PR description.
4. `_squash_to_best`: the correct sequence is `git reset --hard <best_commit>` (tree matches best, **including deletions** — the earlier `git checkout <best> -- .` leaked deleted files) → `git reset --soft <baseline_commit>` (move branch pointer back, keep tree) → `git commit --allow-empty` (leaderboard as message). Captures original HEAD up front and rolls back via `git reset --hard <original>` on failure of **any** of the three steps (step 1 included since l-optimize-robustness-cleanup); if the HEAD capture itself fails there is no rollback point, so it aborts with an explicit error **before any reset runs** instead of proceeding with a silently no-op rollback.
5. Hands off to the provider chain (gitlab/github/jira/legacy/unreadable) or, when `issue_tracking.provider == "disabled"`, the 4-option menu (`merge_local`/`push_pr`/`keep`/`discard`). Option validation and the typed discard gate run **before** the squash so a discard dry-run or missing-option error does not pointlessly rewrite the branch. **`discard` skips the squash entirely** — the branch is being deleted, so squashing first would mutate history and risk a partial-failure stranding.

See [optimize Protocol](pages/protocols/protocol-optimize.md) and [optimize-unattended Protocol](pages/protocols/protocol-optimize-unattended.md) for the per-protocol step walkthroughs. See [Completion and Git Flow](pages/procedures/completion-and-git-flow.md) for the shared dispatcher, the 4-option menu, the typed discard gate, and `_detect_default_branch`. See [DAIC Enforcement](pages/topics/daic-enforcement.md) for the per-step mode application and the frozen-path block. See [State Files](pages/entities/state-files.md) for `optimize-state.json` and the `current_task.json:protocol` loop fields. See [Protocol Engine](pages/subsystems/protocol-engine.md) for `looping_step` machinery and [Workflow Protocols](pages/protocols/workflow-protocols.md) for the non-optimize protocols.

## Gotchas

- **Engine-owned measurement, not LLM-reported.** Passing `metric_value` in `protocol_advance` args does nothing — the engine always re-measures on HEAD. The sub-protocols deliberately tell the LLM to pass only `hypothesis`. Tests use the single-arg `protocol_advance(args={"hypothesis": ...})` pattern.
- **Dirty-tree gate after an improving iteration.** Iteration N's `update_best_commit` rewrites frontmatter; without the `_filter_engine_owned_dirty_lines` allowlist, iteration N+1 would always fail the dirty gate. **Known limitation**: if `metric_command` writes files *outside* `team-management/tasks/<task>/`, those block iter N+1's gate — mitigate by `.gitignore`-ing metric output paths.
- **`max_duration` must be normalised at setup.** Storing `"8h"` raw silently disables the wall-clock cap (`check_termination` does `float(...)` and swallows the `ValueError`). The `_parse_duration_to_seconds` normalisation at the setup boundary is what makes the cap actually fire.
- **Unbounded mode requires a typed ack.** Both `max_iterations` AND `max_duration` null → `check_cost_estimate` rejects advance unless `args["unbounded_acknowledged"] == "i-accept-unbounded-cost"`. The ack is gated at the `baseline` step, not setup, because the cost projection needs real `baseline_wall_clock_s`.
- **Control-call double-logging.** `log_experiment_result`, `update_best_commit`, and `check_termination` all short-circuit on `approve_next_batch`/`exit_loop`. Without this, re-calling advance to release a batch gate would append a duplicate TSV row and re-evaluate termination.
- **Frozen paths only bite during `experimentation`.** The step-gate in `_load_frozen_paths` means a frozen path is freely editable during `metric-script`/`baseline` — intentional, since the metric script may live in a frozen dir, but surprising if you expect the freeze to be absolute.
- **best == baseline aborts completion.** If no iteration improved on the baseline, `optimize.best_commit` equals `optimize.baseline_commit` and `_completion_optimize` refuses with "no improvement found, nothing to squash."
- **Shared sub-protocols are byte-identical by contract.** All optimize sub-protocols except experimentation are shared between the two JSONs; editing one without the other breaks `test_optimize_json_drift_guard.py`. Author in `plugin/protocol-configs/sub-protocols/` (the package source), not the deployed `team-management/protocol-configs/system/` copy.
- **A daemon-thread timeout cannot bound a stdlib `re` match.** `re`'s C-level match holds the GIL until it returns, so `thread.join(timeout)` in the parent never wakes while the match runs — the only stdlib-killable design is a child process. General Python gotcha, discovered the hard way in l-optimize-robustness-cleanup (the first implementation hung the whole test suite).
- **Restart preserves the baseline on purpose.** `_handle_loop_restart` resets the loop (`results.tsv` archived, `best_*` cleared) but keeps `baseline_commit`/`baseline_metric`/`baseline_wall_clock_s` — the baseline commit is unchanged by a loop restart, and `check_cost_estimate` only runs at the `baseline` step so no stale projection is reachable (R2-O5 accept-with-note decision).
