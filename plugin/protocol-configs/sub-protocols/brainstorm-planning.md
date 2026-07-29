# Brainstorm Task Planning Sub-Protocol

## Purpose

Plan implementation tasks based on the approved brainstorm results, create task files, and complete the brainstorm protocol.

## 1. Task Creation

Using the task plan approved in the results step (Implementation Plan section):

1. **Create task files** in `team-management/tasks/`:
   - Each task follows the standard task file format (TEMPLATE.md)
   - Use appropriate priority prefixes: `h-` (high), `m-` (medium), `l-` (low)
   - Include `## Context Files` section referencing the brainstorm results document
   - Add `## Success Criteria` based on the feature details

2. **If multiple tasks** — add dependency and ordering info to each task file:
   - `## Dependencies` — which tasks must complete first
   - `## Parallelizable With` — which tasks can run concurrently

3. **Present created task files** to user and get confirmation

## 2. Coverage Verification (Traceability Audit)

The brainstorm recorded decisions and discussed points across two documents (the task file's `## Decisions`; the results document's `## Feature Details` and `## Scope Definition`). The task files you just created are a lossy synthesis of them — this audit proves nothing load-bearing was dropped on the way into the tasks. **Run it after the task files exist (Section 1) and before you advance.** It is the Verification Before Completion gate (`@knowledge/debugging.md`) applied to task creation.

### 2.1 Enumerate the source items

List everything that MUST end up accounted for:

1. **Confirmed decisions** — every `[x]` item in the brainstorm task file's `## Decisions` section.
2. **Feature details** — every `### <Feature>` entry under `## Feature Details` in the results document.
3. **In-scope items** — everything declared in-scope under `## Scope Definition` in the results document. That section is free prose, not a list — decompose it into discrete in-scope capabilities, one row each. If it is genuinely a single coherent boundary, say so explicitly rather than collapsing detail into one vague row.
4. **Deprecated / dead code** — every entry under `## Deprecated / Dead Code` in the results document. Each names code that becomes unnecessary, which is actionable work: it must map to a task that removes or migrates it, or be explicitly waived. (`## Reusable Code` entries are advisory implementation hints, not deliverables — they do not need their own rows.)
5. **Stray open questions** — any `[ ]` item still left in `## Decisions`. There should be none (the discussion step's readiness check clears them); a survivor is a gap by definition and must be resolved here.

### 2.2 Map each source item to a task

For each source item, find the created task(s) that cover it and build a traceability table:

```markdown
## Task Coverage

| # | Source item (decision / feature / scope / dead code) | Origin | Covered by | Where |
|---|------------------------------------------|----------|-----------------------|---------------------|
| 1 | Event-driven, not polling | Decision | h-implement-event-bus | SC-2 |
| 2 | Config in team-management/config.json | Decision | h-implement-event-bus | SC-4 |
| 3 | GitLab-only in phase 1 | Scope | OUT OF SCOPE | phase 2, user-confirmed |
| 4 | old-poller.py now dead | Dead code | l-cleanup-poller | SC-1 |
```

Rules:
- **Covered** = the item is embodied in a **created task file** — its `## Success Criteria` (cite the criterion's stable ID, e.g. `SC-2`) or an explicit step in that task file's own `## Implementation Plan` (cite the step, e.g. `T3`). The brainstorm results document's `## Implementation Plan` does NOT count: that is the plan this audit checks *against*, so citing it would pass an item that never reached a real task — the exact regression this audit exists to catch. Cite the task name and the specific line — not just "task X".
- One source item may map to several tasks, and one task may cover several items. That is fine.
- **No silent drops.** Every source item gets its own row. A row with no covering task MUST put `OUT OF SCOPE` or `DEFERRED → <follow-up task name>` in the **Covered by** column (with the reason in **Where**) — never leave **Covered by** blank.

### 2.3 Close the gaps

For every row that is neither covered nor explicitly waived:
- **Add or expand a task** so the item is covered — edit the task file's `## Success Criteria`; or
- **Get explicit user sign-off to waive it** — then mark the row `OUT OF SCOPE` / `DEFERRED` with the user's stated reason. Silence is not a waiver; ask.

Repeat 2.2 until every row is covered-or-waived. **100% = every source item covered or user-waived. Do not advance below 100%.**

### 2.4 Present and record

1. Present the finished table to the user with an explicit count: "N source items — X covered by tasks, Y waived (out-of-scope / deferred), 0 unaccounted."
2. Write the final table into the results document (`docs/brainstorm-results/<name>.md`) as a `## Task Coverage` section, immediately after `## Implementation Plan`.
3. Wait for the user to confirm the coverage is acceptable before advancing.

## 3. Update Work Log

Add to the brainstorm task file:
```markdown
- [YYYY-MM-DD] Implementation tasks created: <list of task names>
- [YYYY-MM-DD] Coverage audit complete: <N> source items, all covered or user-waived (see results doc `## Task Coverage`)
```

## 4. Follow-Up Offer

**If a single implementation task was created:**
- Ask: "One implementation task was created. Would you like to start the task protocol for it now?"
- If yes: note the task name — the user can start `protocol_start(protocol_name="task")` after this protocol completes

**If multiple tasks were created:**
- Present the recommended starting task (first in execution order)
- Ask if the user wants to start working on it
- Note: each task should be handled in its own protocol run

## 5. Completion

When the user confirms, call `protocol_advance`. The `completion_dispatch` post-func picks one of two paths based on `issue_tracking.provider` in `team-management/config.json`:

### 5a. Provider-driven flow (`provider` is `gitlab` / `github` / `jira`)

No `args` needed. The engine automatically:
1. Archives the brainstorm task file to `tasks/done/`
2. Stages all changes, commits (results document + task files + brainstorm task file)
3. Merges the default branch in
4. Pushes the feature branch to remote
5. Creates an MR/PR linked to the provider issue
6. Updates the provider issue to completed/closed
7. Cleans up task-scoped state and checks out the default branch

### 5b. Provider-disabled flow (`provider: "disabled"`)

On step entry, `present_completion_options` prints a 4-option menu. Pick one by passing `completion_option` in `protocol_advance` args:

- **`merge_local`** — archive → commit → checkout default → merge feature → delete feature branch → cleanup. No remote push.
  ```
  mcp__plugin_team-management_tm__protocol_advance(
    summary="User confirmed. Merging locally.",
    args={"completion_option": "merge_local"}
  )
  ```
- **`push_pr`** — archive → commit → push → `gh pr create` → cleanup → checkout default.
  ```
  mcp__plugin_team-management_tm__protocol_advance(
    summary="User confirmed. Opening PR via gh.",
    args={"completion_option": "push_pr"}
  )
  ```
- **`keep`** — archive → commit → cleanup. Feature branch preserved as-is.
  ```
  mcp__plugin_team-management_tm__protocol_advance(
    summary="User confirmed. Keeping branch.",
    args={"completion_option": "keep"}
  )
  ```
- **`discard`** — throws away uncommitted work, checks out default, force-deletes the feature branch. Requires a two-step typed confirmation (see task-completion.md Section 4c).

SESSION RECOVERY: Call `protocol_save_note()` after creating task files and after completing the coverage audit.
