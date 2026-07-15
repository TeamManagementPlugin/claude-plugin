Implement the refactoring using an incremental change methodology.

SCOPE OF THIS STEP: Make refactoring changes one logical increment at a time, verifying tests after each increment. Only refactoring work belongs here — no new features, no unrelated bug fixes.

## 0. READ Before Proceeding (MANDATORY)

**READ `@knowledge/tdd-discipline.md` before proceeding. RED-GREEN-REFACTOR discipline is active on EACH increment.** Refactoring without a test safety net is not refactoring — it is untracked rewriting. If the code being refactored has no tests, `protocol_abort` this refactoring and open a separate test-authoring task first (the `test-baseline` and `planning` steps of this protocol are both discussion-mode — they cannot be used to write new tests).

## RED-GREEN-REFACTOR Checklist (per increment)

Run this inline before moving to the next increment:

- [ ] **Baseline GREEN** — test command from `test-baseline` step passes BEFORE you edit this increment. If it already fails, stop and fix the baseline first.
- [ ] **Make the single change** — one logical transformation only (rename, extract, move, simplify). No bundled changes.
- [ ] **GREEN** — re-run the test command. All tests pass, including any pre-existing characterization tests.
- [ ] **No new warnings** — compiler/linter output is pristine (no «was this always here?» warnings introduced by your change).
- [ ] **Save note** — `protocol_save_note()` with a one-line description of what changed and «tests passed».

If GREEN fails: fix immediately, before starting the next increment. Do NOT accumulate broken state.

## Incremental Change Methodology

**One logical change at a time.** Each increment should be a single, cohesive transformation:
- Rename a module/class/function
- Extract a method or class
- Move code to a new location
- Simplify a conditional chain
- Replace an implementation while preserving the interface

Never combine multiple unrelated changes in one increment.

## Mandatory Inter-Increment Testing

After EACH increment:
1. Run the test command from the baseline (captured in the test-baseline step).
2. Verify all tests pass.
3. If tests FAIL: fix immediately before proceeding to the next increment. Do NOT accumulate broken state.
4. Call `protocol_save_note()` with a brief description of what was changed and that tests passed.

This ensures that if something breaks, you know exactly which increment caused it.

## Session Recovery

Call `protocol_save_note()` after each successful increment. Notes are your lifeline if the session restarts — they record exactly which increments are complete and verified.

## Scope Guard

If during refactoring you discover:
- **A bug unrelated to the refactoring** → Document it, create a follow-up task. Do NOT fix it here.
- **A need for new functionality** → Document it, create a follow-up task. Do NOT implement it here.
- **The refactoring scope needs to grow significantly** → Use `protocol_goto(step_name="planning")` to return to planning and re-scope with the user.

The refactoring protocol is about safe, verified structural changes — not feature development.

## Work Log

Update the task work log as you complete increments. Each increment should have a brief entry noting what was changed and that tests passed.

## Completion

When all planned refactoring changes are complete and all tests pass after the final increment, advance to the test-verify step for formal regression verification.
