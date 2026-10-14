---
title: retro Protocol
tags: [protocols, git, security, testing]
created: 2026-08-06
updated: 2026-10-14
sources: [plugin/protocol-configs/retro.json, retro.py]
---

# retro Protocol

Team retrospective from the enforced audit trail: git history + protocol logs
turned into a human-readable report with deterministic metrics and
week-over-week trends (brainstorm block E; the infrastructure for the phased
rollout checkpoints). 4 steps: **data-collection → metrics → narrative →
snapshot**. All engine logic lives in `RetroMixin` (`retro.py`), composed into
the engine as `ProtocolEngine(RetroMixin, OptimizeCompletionMixin,
AIProvidersMixin)` — same one-way-dependency mixin conventions as
[Optimize Protocols](pages/protocols/optimize-protocols.md).

## Identity Model

Branchless, like [research](pages/protocols/protocol-research.md): each run
creates a per-run task `r-retro-<YYYY-MM-DD>` (frontmatter `branch: none`)
whose date is **engine-supplied UTC** — `retro_collect_data` returns
`report_date` and `suggested_task_name`; the LLM never derives a date. The
structural gate `require_retro_anchor in retro.py` rejects a task name whose
date deviates more than 1 day from engine UTC today. The task file is also the
window channel: frontmatter `retro_window_days` (default 7, clamp 1–90) is how
a window reaches the engine, because pre_funcs have no args channel.

## Step Wiring

| Step | Mode | pre_funcs | post_funcs (stop_on_failure) |
|------|------|-----------|------------------------------|
| data-collection | discussion | `retro_collect_data` | `require_retro_anchor`, `set_task_state`, `create_task_file`, `update_task_status_in_progress` (true) |
| metrics | discussion | `retro_compute_metrics` | — |
| narrative | documentation | — | `retro_finalize_snapshot` (true) |
| snapshot | discussion | — | `archive_task`, `cleanup_task_scoped_state`, `clear_task_state` (false) |

No `git_setup_branch`, no `create_issue_if_enabled`, and **no git commits** —
the report, snapshot, and archived task are left uncommitted for the user
(suggested message: `docs: team retro <date>`).

## Stale-Anchor Guard

`_retro_guard in retro.py` blocks on: not a repository / detached HEAD /
default-branch tip older than the window / fewer than 3 non-merge window commits
("scarce"). It reads only the local default branch, so a repository without a
remote passes. The guard never contacted a remote, so requiring one blocked
local-only repositories without making the data any fresher.
Any git failure blocks — fail toward blocking; a retro must not narrate from
nothing. The guard runs twice: advisorily in `retro_collect_data` (window 7)
and structurally in `require_retro_anchor` (task-file window,
`post_funcs_stop_on_failure`). A block whose reasons are ALL window-dependent
("stale anchor" / "scarce data" prefixes — `_WINDOW_DEPENDENT_PREFIXES`) is
returned `recoverable: true` with two sanctioned paths: abort, or — with
explicit user approval — a wider `retro_window_days` in the task file, which
the structural gate re-validates. Hard reasons (detached HEAD, git failure) are
always abort-only.

## Typed Cross-Task Log Reader (injection boundary)

`_read_protocol_logs_typed in retro.py` aggregates
`.claude/state/protocol-logs/*.json` and surfaces ONLY typed engine-derived
fields: task (filename stem), protocol, timestamps (ISO-validated), step
names + step timestamps, goto from/to/at, abort markers, note COUNTS — every
name sanitized (control-chars stripped, capped 64). It NEVER surfaces step
`summary`, note text, goto `reason`, abort reasons, or `pre/post_funcs_executed`
payloads: protocol logs accumulate free text from many past tasks, and a
poisoned entry must not inject instructions into a later retro session.
It reads the window that starts at the git harvest's `since`, so protocol stats
and commit metrics cover the same period. Each event counts by its own
timestamp: a protocol started by `started_at`, a completion or an abort by
`completed_at` (an abort sets it too), a goto by its `at`; a task started
before the window and finished inside it counts as completed, not started.
Naive timestamps are read as UTC; missing or unparseable ones never count. Only
logs with some activity in the window (top-level, step or goto timestamp) are
listed. Payload is bounded (100 logs / 50 steps / 50 gotos, `logs_truncated`
flag) with `totals` computed over every window event before the caps, so
protocol stats stay exact under truncation.

## Deterministic v1 Metrics

Computed server-side by `retro_compute_metrics in retro.py` from the default
branch's non-merge window commits; the LLM presents but never recomputes:

- **Sessions** — per author, a gap strictly over 45 minutes opens a new session.
- **Focus score** — share of file-touches in the top top-level directory
  (root files → `(root)`; touch-based, not commit-majority, for determinism).
- **Weighted commits** — bucket weights 0.5 (<5 LOC) / 1.0 / 1.5 (>300 LOC);
  LOC deliberately does not scale linearly ("AI inflates it").
- **Hotspots** — files with 5+ touches.
- **Protocol stats** — tasks completed / gotos / aborts from the reader totals.

Persisted to `.claude/state/tasks/<task>/retro-metrics.json`; the surfaced
commit list (for praise anchoring) carries sha/author/date/subject/loc only —
the internal `files` paths are stripped, and every display string that derives
from untrusted text (subjects, author identities, hotspot paths, focus area)
is sanitized + credential-filtered via the shared `_filter_credentials`
(single pattern source — see
[AI Provider Integration](pages/subsystems/ai-provider-integration.md)).

## Harvest Parser (forgery resistance)

`_retro_harvest_git in retro.py` uses a NUL (`%x00`) record separator — git
forbids NUL in commit messages and filenames, so a crafted subject cannot forge
a record boundary; NUL cannot ride in argv, so the format string carries git's
escapes and the output is split on the bytes. A `\x01` (field separator) inside
a subject breaks only that record's field count → deterministic skip.
`-c core.quotePath=false`; numstat binary rows (`-`) count 0 LOC;
control-character paths arrive C-quoted and are bucketed under that inert
spelling (accepted limitation).

## Snapshot and Trends

`retro_finalize_snapshot in retro.py` (structural, `stop_on_failure`) runs when
the narrative step advances: re-reads the LLM-written
`docs/retros/retro-<date>.md`, applies the credential filter and **rewrites the
file in place** when redactions occur (D2: the post-write structural pass —
the args channel is unreliable for multi-KB text), verifies the required
sections (`## Praise`, `## Growth Opportunity`, `## Habits`, `## Trends` —
heading presence only; item counts are prompt-level rules), and writes the
typed snapshot `docs/retros/retro-<date>.json` (`schema_version: 1`).
`_read_previous_snapshot in retro.py` gives the next run its baseline through a
strict typed schema: the date comes from the FILENAME (calendar-validated via
strptime — a shape-valid `retro-2026-02-99.json` cannot win the lexicographic
latest pick), metrics are `RETRO_DELTA_KEYS` numerics only, malformed → absent.

## Gotchas

- The collect-step guard always evaluates window 7 (no task file exists yet);
  only the structural gate sees the chosen window. Inline `task_content`
  delivery (instead of write-file-first) means no frontmatter on disk at gate
  time → gate runs at window 7; failure direction is a spurious block.
- Same-day rerun overwrites that day's report/snapshot (previous task already
  archived) — accepted.
- Review-dispatch telemetry is deliberately NOT a v1 data source (Phase 2 per
  the brainstorm); the collect payload keeps a `sources` key as the extension
  point. Work-log text is never parsed by the engine reader.
- Report rewrite is a plain `write_text` (non-atomic) — regenerable
  working-tree doc, accepted divergence from the durable-write pattern.

## Related

- [Workflow Protocols](pages/protocols/workflow-protocols.md) — the hub (shared schema, family gotchas).
- [Protocol Engine](pages/subsystems/protocol-engine.md) — the state machine executing the wiring.
- [research Protocol](pages/protocols/protocol-research.md) — the branchless-protocol template retro follows.
- [State Files](pages/entities/state-files.md) — the protocol-logs directory the typed reader consumes.
