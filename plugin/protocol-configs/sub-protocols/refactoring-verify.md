Verify that the refactoring introduced no regressions by comparing the current test results against the baseline.

SCOPE OF THIS STEP: Run the full test suite and compare results against the baseline captured in the `test-baseline` step. This is a formal regression gate before code review.

## Verification Process

The test baseline has been loaded automatically. You should have the following data available in `pre_funcs_results`:
- **test_command**: The exact command used to capture the baseline
- **baseline_summary**: The original test results (passes, failures, skips)
- **captured_on_branch**: The branch where the baseline was captured

1. Run the exact same test command from the baseline.
2. Compare the results:
   - **All previously-passing tests must still pass.** This is the core safety guarantee.
   - **Pre-existing failures** (failures that were in the baseline) are acceptable — they are NOT regressions.
   - **New failures** are regressions and MUST be fixed before proceeding.
   - **New passes** (tests that failed before but pass now) are a bonus — note them.

## If Regressions Found

If any previously-passing test now fails:
1. Document which tests regressed.
2. Use `protocol_goto(step_name="refactoring")` to return to the refactoring step.
3. Fix the regressions.
4. You will return to this verification step after fixing.

## If No Test Suite

If the baseline recorded `test_command: "none"`:
- Note that verification is limited due to the absence of a test suite.
- Consider whether the refactoring can be verified through other means (manual testing, type checking, linting).
- Document the verification approach in the work log.

## Flaky Tests

If tests appear flaky (pass sometimes, fail sometimes):
- Run the test suite multiple times (2-3 runs) to confirm.
- Document flaky tests in the work log — they are not regressions if they were flaky in the baseline too.
- If a test is newly flaky (was stable in baseline), treat it as a potential regression and investigate.

## Summarize Results

Present the verification results to the user:
- Total tests: passed / failed / skipped (now vs baseline)
- Any pre-existing failures carried forward
- Any new passes (improvements)
- Confirmation: "No regressions found" or list of regressions

When verification passes with no regressions, advance to the code-review step.
