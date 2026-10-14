---
name: team-management-tdd-discipline
description: Test-driven development discipline — when TDD applies, when forcing it is wrong, and the RED-GREEN-REFACTOR loop with an explicit "watch it fail" step. Use when writing or reviewing tests, fixing a bug, changing behaviour, or deciding whether a change needs a test at all. Triggers on bug fixes, new validation or contracts, refactoring, and any moment the temptation is to write the test after the code.
license: MIT
metadata:
  author: team-management
  version: "1.0.0"
---

# TDD When Applicable

## Core Principle

Write the test first. Watch it fail. Write minimal code to pass.

**If you didn't watch the test fail, you don't know it tests the right thing.** Tests written
after the code pass immediately — and passing immediately proves nothing: the test might assert
the wrong behaviour, might test the implementation instead of the contract, might miss the cases
you already forgot. Only a test you saw fail, for the reason you expected, is evidence of
anything.

## Applicability Matrix

TDD is a tool, not a loyalty oath. Forcing it where it does not fit produces ceremonial tests
that assert nothing.

**Applies to:**

- **Bug fixes** — the failing test reproduces the bug, then the fix makes it pass. This proves
  the fix *and* leaves a regression guard behind.
- **Behaviour changes** — new required output, new validation, a changed contract.
- **Feature work with clear inputs and outputs.**
- **Refactoring of already-tested code** — the existing tests are the safety net.

**Does not apply (do not force it):**

- Exploratory spikes where the goal is learning, not shipping.
- Framework or config scaffolding (`setup.py`, CI YAML, `.gitignore`, Dockerfile boilerplate).
- One-off migration or analysis scripts, run once and discarded.
- Documentation-only changes.
- Pure refactoring with zero behaviour change where the existing suite already covers the
  surface — that suite is the oracle.

When uncertain which category applies, default to writing the test. The thirty seconds it takes
to find out is almost always cheaper than the debugging session it replaces.

## RED-GREEN-REFACTOR

### RED — write the failing test

One test, one behaviour, a clear name, exercising real code. Be suspicious of a test that
exercises mocks rather than the production path — it will pass forever regardless of the code.

### Verify RED — watch it fail

Run it. Confirm three things:

- It **fails**, rather than erroring out from a typo or a bad import.
- The failure message is the one you expected.
- It fails because the behaviour is missing — not because of an unrelated bug elsewhere.

If it passes instead of failing, you are testing behaviour that already exists; fix the test.
If it errors with a traceback, fix the typo or import and re-run until it fails *correctly*.

### GREEN — minimal code

Write the simplest thing that makes the test pass. No extra parameters "for future use", no
defensive branches for cases no test covers, no adjacent refactoring smuggled in.

### Verify GREEN — watch it pass

Run the test command and confirm:

- The new test passes.
- Every previously-passing test still passes.
- The output is pristine — no new warnings, no newly-printed errors.

### REFACTOR — clean up

After green, and only after green. Remove duplication, improve names, extract helpers. Do not
add behaviour. The tests stay green throughout; if they go red, the refactor introduced a change
you did not intend.

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "I'll write the test after — it's faster." | Tests written after the code pass immediately. Passing immediately is not evidence. |
| "I already manually verified every case." | Ad-hoc manual checks are not repeatable. The next change breaks them silently. |
| "It's too simple to need a test." | Simple code breaks. Writing the test takes thirty seconds. |
| "I've spent hours on this code — restarting with TDD is wasteful." | Sunk cost. Keeping unverified code is debt you will pay later, with interest. |

## When Stuck

- **You don't know how to test it** — write the API you *wish* existed in the test first, then
  make it real.
- **The test is hard to write** — the design is probably too coupled. Listen to the test; it is
  reporting a design problem, not a testing problem.
- **You have to mock everything** — dependency injection is missing. Inject the dependency
  rather than mocking around its absence.

---

*Maintained alongside `plugin/knowledge/tdd-discipline.md` in [TeamManagementPlugin/claude-plugin](https://github.com/TeamManagementPlugin/claude-plugin), from which it is adapted to stand alone. Change one, change both.*
