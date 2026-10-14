# Retro Snapshot Sub-Protocol

SCOPE OF THIS STEP: Final review and confirmation. The report and snapshot are already written and filtered.

## 1. Present the Result

Show the user:

- The report location: `docs/retros/retro-<report_date>.md` (note any redaction count reported by `retro_finalize_snapshot`).
- The snapshot location: `docs/retros/retro-<report_date>.json` — this is what the NEXT retro run reads to render week-over-week trends, so it should be committed.
- A one-paragraph recap of the retro's headline: top praise, the growth opportunity, the three habits.

## 2. Uncommitted by Design

The retro protocol makes **no git commits**. The report, the snapshot, and (after advancing) the archived task file under `team-management/tasks/done/` are left in the working tree for the user to review and commit — remind them explicitly. Suggested commit message: `docs: team retro <report_date>`.

## 3. Confirm and Complete

Advance only after the user explicitly confirms the retro is complete. On advance the engine archives the retro task, cleans task-scoped state, and clears the task — the protocol ends.
