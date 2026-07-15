# Debugging Discipline

## Core Principle

Find the root cause, not a symptom that quiets the error message. Random patches waste time and create new bugs in adjacent code.

## When to Use

Any technical issue: test failure, production bug, unexpected behavior, performance regression, build failure, integration glitch. Especially when under time pressure — systematic debugging is faster than guess-and-check.

## Four-Phase Methodology

### Phase 1 — Root Cause Investigation

Complete this phase before proposing any fix.

1. **Read the error carefully.** Stack traces, line numbers, error codes. The exact solution is often in the message itself.
2. **Reproduce consistently.** What are the exact steps? Every time, or intermittent? If not reproducible, gather more data — do not guess.
3. **Check recent changes.** `git log`, `git diff`, recent dependency bumps, config changes, environmental differences between where it works and where it fails.
4. **Instrument at component boundaries (multi-component systems).**
   For each boundary in the chain (CI → build → signing, API → service → database, hook → MCP → engine), log what enters and what exits. Run once with instrumentation, read the evidence, then investigate the specific layer that fails. This reveals *where* the break is before you speculate *why*.

   Example:
   ```bash
   # Layer 1: Workflow
   echo "IDENTITY: ${IDENTITY:+SET}${IDENTITY:-UNSET}"
   # Layer 2: Build script
   env | grep IDENTITY || echo "IDENTITY not in environment"
   # Layer 3: Signing
   security find-identity -v
   ```
5. **Trace the bad value backward.** When the error is deep in the stack, walk up: where does the bad value originate? What called this with the bad value? Keep tracing until you hit the source. Fix at the source, not at the symptom site.

### Phase 2 — Pattern Analysis

- Find working code that does a similar thing in the same codebase.
- If implementing an external pattern, read the reference implementation completely — do not skim.
- List every difference between the working example and the broken code, however small. "That can't matter" is a common source of bugs.
- Understand what the code depends on: config, environment, state, ordering.

### Phase 3 — Hypothesis and Test

- State the hypothesis explicitly: "I think X is the root cause because Y."
- Make the smallest possible change to test it. One variable at a time.
- Did it work? → Phase 4. Didn't? → form a *new* hypothesis. Do **not** stack additional fixes on top of an unconfirmed one.
- If you genuinely do not understand something, say so — do not pretend knowledge you lack.

### Phase 4 — Implementation and Verification

1. **Write a failing test that reproduces the bug** (see TDD discipline — @team-management/knowledge/tdd-discipline.md). If no test framework applies, at minimum a one-off reproduction script.
2. **Implement one fix** — addressing the root cause, nothing else. No "while I'm here" refactoring.
3. **Verify the fix** — test passes, no other tests broken, original symptom actually resolved.

## 3-Fix Escalation Rule

**After three failed fix attempts on the same bug, stop.**

Count the fix attempts. The failure mode is not one more hypothesis away — it is in the architecture, the pattern, or an assumption you have not questioned. Signs of architectural-level problem:

- Each fix reveals a new coupling or shared-state issue in a different place.
- Fixes keep requiring "massive refactoring" to implement.
- Every fix creates new symptoms elsewhere.

This is not a failed hypothesis — this is the wrong architecture or the wrong mental model. Return to discussion with the user (`protocol_goto(step_name="investigation", reason=...)`). Do not attempt fix #4 without an architecture-level conversation.

## Verification Before Completion

Verification is **Phase 4 completion**, not a separate skill. Claiming work is done without running the verification is not efficiency — it is a false report that costs trust.

### Gate Function

Before claiming any status or expressing satisfaction:

1. **Identify** — what command proves this claim?
2. **Run** — execute the full command, fresh, in this session.
3. **Read** — the full output. Check the exit code. Count failures.
4. **Verify** — does the output actually confirm the claim?
5. **Only then** — make the claim, and make it with the evidence attached.

Skipping any step is not verification — it is prediction.

### Common Failures

| Claim | Requires | Not sufficient |
|-------|----------|----------------|
| Tests pass | Test command output showing 0 failures, in this session | Previous run, "should pass" |
| Linter clean | Linter output: 0 errors | Partial check |
| Build succeeds | Build exit code 0 | Linter passing, logs "look good" |
| Bug fixed | Original symptom no longer reproduces | Code changed, assumed fixed |
| Regression test works | Red-green cycle verified (see below) | Test passes once |
| Agent completed work | VCS diff shows the actual changes | Agent's self-report |
| Requirements met | Line-by-line checklist against Success Criteria | "Tests pass" |

### Regression Test Red-Green Verification

A regression test is only trustworthy if you saw it catch the bug:

1. Write the test → run → it passes with the fix in place.
2. Revert the fix → run → **the test MUST fail**.
3. Restore the fix → run → passes again.

If step 2 still passes, the test is not testing the fix.

### Red Flags

Any of these wordings means you have not verified:

- "should work", "probably fine", "seems to", "looks correct"
- "tests likely pass", "looks good to me"
- Expressing satisfaction ("", "Perfect!", "Done!") without corresponding evidence
- Trusting an agent's success report without checking the diff

### Escape Hatch — `no-verification-applicable`

Some steps genuinely cannot be verified with a command (documentation-only changes, discussion/planning steps, changes to a codebase with no test suite). For those, state the escape hatch explicitly in the form:

```
no-verification-applicable: <reason>
```

Canonical reasons: `documentation-only`, `planning-step`, `discussion-step`, `no-test-suite-exists`. Anomalous reasons appear in the audit log and can be reviewed. This is not a default — most completion steps *can* be verified and must be.

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Issue is simple, no process needed." | Simple bugs have root causes too. The process is fast for simple bugs. |
| "Emergency, no time for process." | Systematic debugging is faster than thrashing. Guessing under pressure multiplies the problem. |
| "One more fix attempt" (after 2 failures). | Three failures = architectural problem. Stop and question the pattern. |
| "I'll write the test after confirming the fix works." | Untested fixes don't stick. A passing test written after is not a regression test — you never saw it fail. |
