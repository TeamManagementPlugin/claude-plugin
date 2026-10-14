# Task Completion Sub-Protocol

## Purpose

This sub-protocol handles user verification and confirmation before automated completion.
All git/MR/archive/cleanup operations are handled automatically by the engine when
the user calls `protocol_advance` after confirming everything works.

## 0. READ Before Proceeding (MANDATORY)

**READ the verification section of `@knowledge/debugging.md` before advancing.** The Gate Function is the completion contract: identify what must be verified → run the command → read the actual output → confirm output matches expectation → only then claim success. Claims like «should work», «probably fine», «tests likely pass» are disqualifying red flags.

## 0a. TDD Self-Acknowledgment

Before summarizing changes for the user, answer explicitly: **Was TDD applied, or was the task TDD-exempt?**

- **Applied** — point at the failing test(s) that were written first and now pass. «Test `test_foo::test_validates_empty_input` was written RED, went GREEN after the fix in `foo.py:42`.»
- **Exempted** — state the exemption category per `@knowledge/tdd-discipline.md`: exploratory spike / scaffolding / one-off script / documentation-only change / pure refactor with existing coverage. No hand-waving — pick a named category.

If neither applies, the task has verification gaps — `protocol_goto(step_name="implementation")` and close them before completing.

## 1. Present Changes for User Testing

Summarize what was implemented:
- List all success criteria with their per-item status and Verification Mode from the FINAL spec-compliance review (`DONE` or `UNVERIFIABLE [<mode>]` — `PARTIAL` / `NOT DONE` / `CHANGED` never reach this step; they fail the code-review gate). After compaction this list is reconstructible from the notes alone: the strict verdict guarantees every criterion outside the carriage note's set is `DONE`, and the noted items are the UNVERIFIABLE set (per-item mode tags are presentational — include them when the final review is still in context). Items the final review marked `DEFERRED` are listed in the latest `SPEC_DEFERRED:` note. They are `DONE` here only when a `SPEC_DEFERRED: PASSED` note is newer than a `SPEC_DEFERRED:` list that is itself at-or-after the latest `SPEC_REVIEW: PASSED` — unless that re-audit added them to the UNVERIFIABLE set; an unconfirmed deferred item goes to Section 1b
- List all files modified/created
- Highlight any notable decisions or trade-offs made during implementation

Ask the user to test the changes.

**MANDATORY: Notify the user** that their review is needed — they are likely not watching the terminal. Call `mcp__plugin_team-management_tm__notify_user` with a short summary of what was done and that you are waiting for their confirmation. Do this EVERY time you reach this step, without exception.

## 1a. Per-Item Confirmation of UNVERIFIABLE Items (BEFORE any generic confirmation)

Criteria that passed the spec audit as `UNVERIFIABLE` have their evidence outside the diff (external state, another repo, semantic content quality). They MUST be confirmed by the user one by one, BEFORE the generic confirmation of Section 3. **Blanket confirmation is prohibited for these items** — a global "everything works" / "LGTM" does NOT cover them.

0. **Scope.** This section applies to protocols whose code-review step records the `SPEC_REVIEW: PASSED` sentinel (the spec-compliance stage) — currently ALL FOUR shipped consumers of this sub-protocol: `task`, `refactoring`, `optimize`, `optimize-unattended`. Only a custom-fork protocol without that stage produces no UNVERIFIABLE classifications — skip this section there.
1. **Find the set.** Source of truth: the latest `SPEC_UNVERIFIABLE:` note in the protocol log (`mcp__plugin_team-management_tm__protocol_log`). **Staleness check first:** a carriage note is valid only if recorded at-or-after the latest `SPEC_REVIEW: PASSED` note (every spec-audit PASS saves both, carriage second; note timestamps are in the log). Then resolve by case:
   - **Fresh `SPEC_UNVERIFIABLE: none`** → no UNVERIFIABLE items; skip this section.
   - **Fresh note with SC-IDs** → that is the set. ALSO read the work-log carry-list (recorded at PASS time for every non-empty set): it carries each item's Verification Mode, why it is unverifiable, and what the user should check — step 2's questions must name these; the criterion text itself is in the task file's `## Success Criteria`. A fresh ID note WITHOUT a matching carry-list is an audit-data inconsistency — Section 1b applies.
   - **Fresh `SPEC_UNVERIFIABLE: legacy — see work log`** (tasks without SC IDs) → the carry-list IS the set; missing → audit-data inconsistency — Section 1b applies.
   - **STALE note** (a carriage note OLDER than the latest sentinel belongs to a superseded round) → the interruption that orphaned the sentinel also predates the carry-list, so BOTH are superseded — serve neither; Section 1b applies directly (re-run the audit or ask).
   - **No note at all** → fall back to the work-log carry-list alone (pre-feature / lost-log runs). Neither note nor carry-list → do NOT assume an empty set: a forgotten carriage note is indistinguishable from a pre-feature run, so Section 1b applies — its explicit question is where the user may knowingly answer "proceed — this review pre-dates the carriage contract".
2. **1–3 items** → ONE `AskUserQuestion` call with one question per item. Each question quotes the criterion, names its Verification Mode (WHY the diff cannot prove it), and says what the user should check. Options: "Confirmed — verified externally" (first) / "Not confirmed — needs work".
3. **More than 3 items** → ONE `AskUserQuestion` call using `multiSelect: true` — "select the items you have verified" — one option per item, grouped into questions of at most 4 options each and at most 4 questions per call (widget limits: 16 items max per call), instead of a series of single-item calls. **More than 16 items** → successive multiSelect calls of up to 16 items each, every item still resolved individually.
4. **Every not-confirmed item** → either `mcp__plugin_team-management_tm__protocol_goto(step_name="implementation")` to close it, or the user explicitly accepts it as known-unverified — record the acceptance per-item in the task's `## User Notes` and the work log. Do NOT advance with an unconfirmed item that has neither resolution.

## 1b. Fail-Open Prohibition (audit failure ≠ audit pass)

If a completion audit that was supposed to run for this protocol fails to RUN — the per-SC status list cannot be produced (e.g. no final spec review evidence in the work log AND no usable `SPEC_UNVERIFIABLE:` / `SPEC_REVIEW:` notes in the protocol log), a completion-time verification command errors out rather than failing, or **Section 1a routed here** (missing/inconsistent carriage data — this routing applies even when other audit evidence exists) — NEVER proceed as if it passed. Raise an explicit `AskUserQuestion` with **"Stop and fix"** as the recommended first option (diagnose why the audit could not run, then re-run it). A silent pass is prohibited.

Distinguish the two failure kinds:
- **The audit ran and FAILED** (spec review FAIL, tests red) → the normal fix loop: `protocol_goto` back to implementation / re-run the review.
- **The audit COULD NOT RUN** (agent crash, missing report, errored command) → this section's explicit question. Absence of a verdict is not a verdict.

## 2. User Testing Phase

The user tests the implementation. During this phase:
- Answer any questions about the implementation
- If the user finds issues: use `mcp__plugin_team-management_tm__protocol_goto(step_name="implementation")` to go back and fix them
- If the user requests minor fixes that don't require going back: fix them in the current step (documentation mode allows CLAUDE.md edits; for code fixes, go back to implementation)

**CRITICAL**: Do NOT call `protocol_advance` until the user explicitly confirms everything works.

## 3. User Confirmation

Wait for explicit user confirmation. Acceptable confirmations:
- "Everything works", "looks good", "approved", "ship it", "LGTM"
- Any clear positive confirmation

These generic confirmations cover the overall change-set and are acceptable ONLY after the per-item confirmations of Section 1a have been collected (when UNVERIFIABLE items exist) — a generic confirmation never substitutes for them.

**NOT acceptable**:
- Silence or no response
- "I'll test later" (wait for actual testing)
- Ambiguous responses (ask for clarification)

## 4. Automated Completion

When the user confirms and you call `protocol_advance`, the `completion_dispatch` post-func picks one of two paths based on `issue_tracking.provider` in `team-management/config.json`:

**Optimize protocol note** (`optimize` / `optimize-unattended`): `completion_dispatch` routes to `_completion_optimize`, which does NOT stage-all-and-commit the working tree. It **squashes** the experimentation history from `optimize.best_commit` onto `optimize.baseline_commit` (`_squash_to_best`) and ships an MR/PR with a metric leaderboard. The provider-driven vs. 4-option-menu split below still applies (the menu's `merge_local` / `push_pr` / `keep` choices wrap the squash; `discard` skips the squash — the branch is being deleted); only the "stages all changes" mechanics in §4a differ.

### 4a. Provider-driven flow (`provider` is `gitlab` / `github` / `jira`)

No `args` needed. The engine automatically:

1. Archives the task file to `team-management/tasks/done/`
2. Stages all changes, commits with a descriptive message
3. Merges the default branch in
4. Pushes the feature branch to remote
5. Creates an MR/PR linked to the provider issue
6. Updates the provider issue to completed/closed
7. Cleans up task-scoped state files
8. Resets current task state and checks out the default branch

This is identical to the pre-dispatcher behaviour — existing GitLab / GitHub / Jira users see no change.

### 4b. Provider-disabled flow (`provider: "disabled"`)

On step entry, `present_completion_options` prints a 4-option menu. Pick one by passing `completion_option` in `protocol_advance` args:

- **`merge_local`** — `archive_task` → `git_commit` → checkout default → merge feature → delete feature branch → cleanup. Ends on the default branch with the work merged locally and the feature branch removed. No remote push.
  ```
  mcp__plugin_team-management_tm__protocol_advance(
    summary="User confirmed. Merging locally.",
    args={"completion_option": "merge_local"}
  )
  ```
- **`push_pr`** — `archive_task` → `git_commit` → `git_push` → `gh pr create` → cleanup → checkout default. Requires the `gh` CLI on `PATH` (error with install hint if missing). Feature branch is pushed and a pull request is opened against the default branch.
  ```
  mcp__plugin_team-management_tm__protocol_advance(
    summary="User confirmed. Opening PR via gh.",
    args={"completion_option": "push_pr"}
  )
  ```
- **`keep`** — `archive_task` → `git_commit` → cleanup. Feature branch is preserved as-is, no merge, no push, no checkout. Use when the work should stay on the branch for further local review.
  ```
  mcp__plugin_team-management_tm__protocol_advance(
    summary="User confirmed. Keeping branch for further review.",
    args={"completion_option": "keep"}
  )
  ```
- **`discard`** — throws away uncommitted work, checks out default, force-deletes the feature branch. No archive, no commit, no push — the task file goes away with the branch. Requires a **two-step typed confirmation** (see below).

### 4c. Discard: Friction, Not Security

`discard` is gated by `_func_require_discard_confirmation`. The first call returns a dry-run block with the branch name and commit count. The second call must include the typed confirmation and the dry-run acknowledgment:

```
# 1. Dry-run — returns success=False with the re-advance instructions.
mcp__plugin_team-management_tm__protocol_advance(
  summary="User confirmed discard.",
  args={"completion_option": "discard"}
)

# 2. Confirmed run — all three keys required, exact match on 'discard'.
mcp__plugin_team-management_tm__protocol_advance(
  summary="User confirmed discard (dry-run reviewed).",
  args={
    "completion_option": "discard",
    "discard_confirmation": "discard",
    "discard_confirmed_dry_run": true
  }
)
```

**This is a friction mechanism, not an authentication control.** An LLM can trivially produce the string `"discard"` and the boolean `true`; the purpose is to slow down accidental invocation by both the model and the operator who pasted a `protocol_advance` call — not to stop a motivated actor. Security sits upstream: the user must choose `discard` out of four visible options, and the protected-state hooks prevent external processes from bypassing the menu.

## 5. Post-Completion

After automated completion, present results to the user:
- Commit hash and branch
- MR/PR URL (if created)
- Issue status update (if applicable)
- Archived task location

Offer to start the next task or create a new one.

## Provider-Aware Behavior

The engine handles provider routing internally:

- **GitLab**: commit → push → MR → close issue → archive
- **GitHub/Gitea**: commit → push → PR → close issue → archive
- **Jira**: update Jira issue → (optional GitLab/GitHub git-only MR) → archive
- **Hybrid** (Jira + GitLab): Jira issue tracking + GitLab git-only workflow
- **No provider (`issue_tracking.provider: "disabled"`)**: 4-option menu (`merge_local` / `push_pr` / `keep` / `discard`) — user picks by passing `completion_option` in `protocol_advance` args. See Section 4b.

### Limitations

- **Super-repo/submodules**: The automated commit operates in the project root only. If the project uses git submodules, you must commit submodules manually before calling `protocol_advance`.
- **Experiment branches**: Committed and pushed like regular branches. To keep for reference without merging, do not create MR/PR — close the issue manually after archiving.
- **Research tasks (no branch)**: If no branch is set in task state, the automated commit will fail. For research tasks, use `protocol_abort` instead of `protocol_advance` and archive findings manually.

## Going Back (protocol_goto)

If user testing reveals issues:

```
User: "The validation isn't working correctly"
AI: Let me go back to implementation to fix this.
    → calls mcp__plugin_team-management_tm__protocol_goto(step_name="implementation", reason="Fix validation bug found during user testing")
```

This sets the protocol back to the implementation step with implementation DAIC mode.
After fixing, the AI must go through code-review and documentation again before returning to completion.

## Handling Automated Step Failures

If an automated step fails (e.g., suspicious files detected during commit), the protocol does NOT advance. The AI must fix the issue and retry `protocol_advance`.

**Problem**: Completion step is in discussion mode — AI cannot Edit files directly.

**Solution**: AI asks user to approve `mcp__plugin_team-management_tm__daic_mode_switch_implementation` to temporarily switch to implementation mode. The user sees the MCP tool approval prompt and confirms. AI fixes the issue (e.g., adds .env to .gitignore), switches DAIC back to discussion, and retries `protocol_advance`.

```
AI: The automated commit found suspicious file ".env". I need to add it to .gitignore.
    → mcp__plugin_team-management_tm__daic_mode_switch_implementation()
    [User approves tool call]
    → Edit .gitignore (add .env)
    → mcp__plugin_team-management_tm__daic_mode_switch_discussion()
    → mcp__plugin_team-management_tm__protocol_advance(summary="User confirmed, .env excluded")
```

This avoids protocol_goto — the fix is a one-off operational fix, not a scope change.

## Advance Summary — `no-verification-applicable` Escape Hatch

When the change has no commandable verification (by design, not by oversight), include the escape-hatch marker in the `protocol_advance` summary. Canonical reasons:

- `no-verification-applicable: documentation-only`
- `no-verification-applicable: planning-step`
- `no-verification-applicable: discussion-step`
- `no-verification-applicable: no-test-suite-exists`

Use the canonical token verbatim after the colon — any suffix (e.g. `documentation-only change`) is treated as an anomalous reason and flagged in the audit log.

Worked examples:

- «Updated service CLAUDE.md; no runtime code touched. `no-verification-applicable: documentation-only`.»
- «Scaffolding-only task (new empty module + CI stub); first real test lands in follow-up task. `no-verification-applicable: no-test-suite-exists`.»

The escape hatch is informational at this step — the structural evidence check (`check_completion_evidence`) runs at the `code-review` step, not here. At completion, verification is captured by the user-confirmation exchange in this sub-protocol. Use the canonical token to keep the audit trail honest about why no command verification was run. The escape hatch is not a default — most tasks have something verifiable and must verify it.

## Behavioral-Content Pressure-Test (optional)

If this task modified runtime behavioral knowledge (files under `team-management/knowledge/`, `CLAUDE.tm.md`, sub-protocol markdown), consider running the pressure-testing methodology from `docs/knowledge/writing-behavioral-content.md` before advancing: RED-GREEN-REFACTOR for documentation, rationalization-table check, line-budget check. This is advisory, not mandatory — skip cleanly when the content passes on first read.

## Important Notes

- NEVER call protocol_advance without explicit user confirmation
- The engine handles ALL git/provider/cleanup operations — do not duplicate manually
- If an automated step fails, fix the issue and retry protocol_advance (see above)
- Task files in done/ serve as historical record
- If task is abandoned incomplete, use protocol_abort instead of protocol_advance
