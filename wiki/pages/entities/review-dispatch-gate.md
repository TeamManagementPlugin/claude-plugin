---
title: Review Dispatch Gate
tags: [agents, protocols, config, testing]
created: 2026-08-01
updated: 2026-08-11
sources: [protocol_engine.py, sub-protocols/code-review.md, config.template.json]
---

# Review Dispatch Gate

The CR-2 minimal dispatch gate: a deterministic diff classifier that decides which
review-chain specialists (`testing-specialist`, `code-cleanliness`, `red-team`) the
code-review step dispatches — config-gated and diff-thresholded, never ungated always-on.
Engine half: `_func_classify_review_dispatch` in `protocol_engine.py` (a pre_func on the
`code-review` step of the task and refactoring protocols — deliberately NOT the optimize
pair, whose tree is clean at review time). Prompt half: `sub-protocols/code-review.md`
§2/§2a. This is the wave-1 minimal slice of the K.3 orchestration; Phase 2 moves
re-evaluation and telemetry engine-side.

## Decision Rules

- **DIFF_LINES** = added + deleted from `git diff HEAD --numstat --no-renames -z`
  (binary rows count 0) + line counts of untracked files
  (`git ls-files --others --exclude-standard -z`; NUL-sniffed binaries and non-regular
  files count 0). `-z` NUL-delimited output keeps every path VERBATIM; `--no-renames`
  keeps rename rows suffix-checkable.
- **Framework paths are excluded from both sources.** A path whose FIRST component is in
  `_FRAMEWORK_PATH_ROOTS` (`team-management`, `.claude`, `.agents`) contributes nothing to
  DIFF_LINES and never enters the path set behind docs/config-only; its lines are tallied
  separately and reported as `framework_lines_excluded`. The framework's own tree is
  bookkeeping ABOUT the change, not the change under review — a task markdown routinely runs
  to several hundred lines of narrative, and since red-team is not docs-gated, a docs-only
  change carrying a long task file could otherwise cross the 200-line trigger on narrative
  alone. `wiki/` and `docs/` are deliberately NOT excluded (project content), and the
  first-component match keeps `src/team-management/x.py` measured.
- **docs/config-only** = every changed/untracked PROJECT path (framework paths already
  dropped) has a docs/config suffix (`_DOCS_CONFIG_SUFFIXES`: .md .rst .txt .json .yaml
  .yml .toml .ini .cfg) or lives under top-level `docs/`.
- `testing-specialist` and `code-cleanliness` fire at DIFF_LINES ≥ 50
  (`_SPECIALIST_MIN_DIFF_LINES`) and never on a docs/config-only diff; `red-team` fires
  STRICTLY ABOVE 200 (`_RED_TEAM_MIN_DIFF_LINES`) with no docs gate.
- Config gates `code_review.{testing_specialist,red_team,cleanliness}_enabled` — absent
  key = enabled; only exact JSON `false` disables (fail-enabled: config trouble never
  silently disables the roster). See [Configuration Schema](pages/entities/configuration-schema.md).
- Reason strings are stable API for the telemetry: `disabled-by-config` / `empty-diff` /
  `docs-config-only` / `below-threshold (...)` / `fired (N lines)` / `classifier-error: ...`.

## Orchestrator Contract (sub-protocol §2/§2a)

- First round: ONE message = mandatory `code-review` agent + AI providers + fired
  specialists; specialist prompts must carry the complete changed-file set INCLUDING
  untracked files (their default `git diff HEAD` cannot see them).
- Entry decisions are binding; the ONLY sanctioned addition is the deterministic
  **add-only re-measurement** before post-fix re-review rounds (same commands, same
  thresholds; a newly crossed threshold ADDS the specialist with note
  `DISPATCH: <agent> fired (in-step growth)`).
- **Second echelon (§2a)**: `red-team` runs sequentially AFTER round 1 merges, on the
  classifier size trigger OR any round-1 Critical; the explicit config opt-out wins over
  BOTH triggers; at most once per step entry; the Critical escalation is recorded via the
  canonical note `DISPATCH: red-team fired (critical-finding)`.
- Absent classifier entry in `pre_funcs_results` (optimize protocols, custom forks,
  `protocol_goto` re-entry — goto runs no pre_funcs) → the whole specialist dispatch is
  skipped.

## Telemetry (the Phase 1→2 checkpoint signal)

`_append_dispatch_log` appends one JSONL record per agent per evaluation (fired AND
skipped, on every path including `classifier-error`) to the **project-scoped**
`.claude/state/review-dispatch-log.jsonl` — deliberately NOT under
`.claude/state/tasks/<task>/`, which `cleanup_task_scoped_state` wipes at completion.
Record: `{ts, task, protocol, step, agent, diff_lines, framework_lines_excluded,
docs_config_only, enabled, fired, reason}`. `framework_lines_excluded` makes the log explain
its own numbers — without it a reader cannot tell a genuinely small change from a large one
whose framework bookkeeping was filtered away — and doubles as the rollout marker: a record
carrying the key is post-exclusion by construction, so the pre- and post-change `diff_lines`
series stay distinguishable. It is `0` on the `classifier-error` path, present rather than
omitted. The file sits under the protected `.claude/state` prefix: engine-writable
(MCP server process is not hook-gated), session-unreadable — the checkpoint reads it via
a future server-side func, never via agent tools.

## Invariants and Gotchas

- **Never blocks the review step**: every failure path returns `success: True` with all
  specialists skipped (`classifier-error`); the mandatory code-review agent is unaffected.
  A config-disabled agent keeps its honest `disabled-by-config` reason even on the error
  path (the enabled map is read before measurement).
- The prose `instructions` channel branches on `disabled-by-config` so it can never
  contradict §2a's opt-out-wins rule.
- FIFO/socket/device untracked entries are skipped via `is_file()` before `open()` —
  an untracked FIFO would otherwise block the pre_func forever.
- Accepted limitations of the measurement: CR-containing filenames
  under-measure (universal-newline translation); non-UTF-8 filenames (Linux) take the
  skip-all error path; the untracked line count is time-unbounded for giant text files;
  `is_file()`→`open()` TOCTOU. All fail toward skip/under-dispatch, never toward blocking.
- Accepted limitation of the framework-path exclusion: the three roots are excluded
  WHOLESALE, so user-authored custom protocols and custom funcs under
  `team-management/protocol-configs/custom/` are excluded too. A change consisting only of
  those measures as an empty diff and summons no chain specialist — the mandatory
  `code-review` agent is not classifier-gated and still reviews it. A carve-out was
  considered and declined in favour of one unconditional predicate. Note also that
  `git ls-files --exclude-standard` already drops gitignored files, so the exclusion
  principally protects TRACKED framework files and installs where those roots are not
  gitignored.
- Drift guards: `test/test_review_dispatch_gate.py` (44 tests: threshold boundaries
  49/50/200/201, docs-only, config gating, error paths, telemetry shape, `-z` verbatim
  paths, FIFO, framework-path exclusion incl. tracked/untracked/deletion/mixed-diff and the
  excluded-FIFO-and-binary tally, the contract-text drift guard, wiring incl. optimize
  exclusion) and the `test/test_refactoring_gates_drift.py` pre_funcs pin.

## Related

- [Review Output Contract](pages/entities/review-output-contract.md) — the contract every
  dispatched specialist emits.
- [Specialized Agents](pages/entities/specialized-agents.md) — the catalog entries for the
  three gated specialists.
- [task Protocol](pages/protocols/protocol-task.md) — the code-review step this gate serves.
