# TDD When Applicable

## Core Principle

Write the test first. Watch it fail. Write minimal code to pass.

**If you didn't watch the test fail, you don't know it tests the right thing.** Tests written after code pass immediately — passing immediately proves nothing: the test might test the wrong behavior, might test implementation instead of contract, might miss cases you forgot. Only a test you saw fail for the expected reason is evidence of anything.

## Applicability Matrix

**Applies to:**
- Bug fixes — failing test reproduces the bug, then the fix makes it pass (proves the fix and prevents regression).
- Behavior changes — new required output, new validation, changed contract.
- Feature work with clear inputs and outputs.
- Refactoring of already-tested code — tests are the safety net.

**Does not apply (do not force it):**
- Exploratory spikes where the goal is learning, not shipping.
- Framework/config scaffolding (`setup.py`, CI YAML, `.gitignore`, Dockerfile boilerplate).
- One-off migration or analysis scripts run once and discarded.
- Documentation-only changes.
- Pure refactoring with zero behavior change where existing tests cover the surface (the existing suite is the oracle).

When uncertain which category applies, default to writing the test — the 30 seconds to find out usually costs less than debugging later.

## RED-GREEN-REFACTOR

### RED — Write Failing Test

One test, one behavior, clear name, real code. Avoid tests that exercise mocks instead of production code.

### Verify RED — Watch It Fail

Run the test. Confirm:
- It fails (not errors out from typos).
- The failure message is what you expected.
- It fails because the feature is missing, not because of an unrelated bug.

Test passes instead of failing? You are testing behavior that already exists — fix the test.
Test errors out with a traceback? Fix the typo/import, re-run until it fails correctly.

### GREEN — Minimal Code

Write the simplest code that makes the test pass. No extra parameters "for future use", no defensive branches for cases the test does not cover, no adjacent refactoring.

### Verify GREEN — Watch It Pass

Run the test command. Confirm:
- The new test passes.
- All previously-passing tests still pass.
- Output is pristine — no new warnings, no new errors printed.

### REFACTOR — Clean Up

After green only. Remove duplication, improve names, extract helpers. Do not add behavior. Tests must stay green throughout.

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "I'll write the test after — faster." | Tests written after code pass immediately. Passing immediately is not evidence. |
| "Already manually verified all cases." | Ad-hoc manual checks are not repeatable. The next change breaks them silently. |
| "Too simple to need a test." | Simple code breaks. Writing the test takes 30 seconds. |
| "I already spent hours on this code — deleting and restarting with TDD is wasteful." | Sunk cost. Keeping unverified code is technical debt you will pay later, with interest. |

## When Stuck

- **Don't know how to test it** — write the wished-for API in the test first, then make it real.
- **Test is hard to write** — the design is probably too coupled. Listen to the test.
- **Must mock everything** — dependency injection is missing. Inject, don't mock.
