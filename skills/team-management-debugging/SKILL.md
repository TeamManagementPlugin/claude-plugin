---
name: team-management-debugging
description: Systematic debugging — a four-phase root-cause methodology, boundary instrumentation for multi-component systems, the 3-fix escalation rule, and a verification gate to run before claiming anything is fixed. Use on any test failure, production bug, build failure, performance regression, or integration glitch, and especially before saying work is done. Triggers on stack traces, intermittent failures, "should work", and the third failed fix attempt on the same bug.
license: MIT
metadata:
  author: team-management
  version: "1.0.0"
---

# Debugging Discipline

## Core Principle

Find the root cause, not a symptom that quiets the error message. Random patches waste time and
seed new bugs in adjacent code.

## When to Use

Any technical issue: test failure, production bug, unexpected behaviour, performance regression,
build failure, integration glitch. Especially under time pressure — systematic debugging is
*faster* than guess-and-check, not slower.

## Four-Phase Methodology

### Phase 1 — Root-cause investigation

Complete this phase before proposing any fix.

1. **Read the error carefully.** Stack traces, line numbers, error codes. The solution is
   frequently stated in the message itself.
2. **Reproduce consistently.** What are the exact steps? Every time, or intermittently? If you
   cannot reproduce it, gather more data — do not guess.
3. **Check recent changes.** `git log`, `git diff`, dependency bumps, config changes, and the
   environmental differences between where it works and where it fails.
4. **Instrument at component boundaries.** For each boundary in the chain (CI → build →
   signing; API → service → database; client → proxy → worker), log what enters and what
   exits. Run *once* with instrumentation, read the evidence, then investigate the specific
   layer that failed. This tells you *where* the break is before you speculate about *why*.

   ```bash
   # Layer 1: Workflow
   echo "IDENTITY: ${IDENTITY:+SET}${IDENTITY:-UNSET}"
   # Layer 2: Build script
   env | grep IDENTITY || echo "IDENTITY not in environment"
   # Layer 3: Signing
   security find-identity -v
   ```

5. **Trace the bad value backward.** When the error surfaces deep in the stack, walk up: where
   does the bad value originate? What called this with it? Keep tracing until you reach the
   source, and fix it there — not at the symptom site.

### Phase 2 — Pattern analysis

- Find working code that does something similar in the same codebase.
- If you are implementing an external pattern, read the reference implementation *completely*.
  Do not skim it.
- List every difference between the working example and the broken code, however small. "That
  can't matter" is a reliable source of bugs.
- Understand what the code depends on: config, environment, state, ordering.

### Phase 3 — Hypothesis and test

- State the hypothesis out loud: "I think X is the root cause, because Y."
- Make the smallest change that tests it. One variable at a time.
- Did it work? Go to Phase 4. Did it not? Form a *new* hypothesis. Do **not** stack another fix
  on top of an unconfirmed one — you will lose track of which change did what.
- If you genuinely do not understand something, say so rather than performing knowledge you
  lack.

### Phase 4 — Implementation and verification

1. **Write a failing test that reproduces the bug.** If no test framework applies, at minimum a
   one-off reproduction script.
2. **Implement one fix**, addressing the root cause and nothing else. No "while I'm here"
   refactoring.
3. **Verify**: the test passes, no other test broke, and the original symptom no longer
   reproduces.

## The 3-Fix Escalation Rule

**After three failed fix attempts on the same bug, stop.**

Count them. The failure is not one more hypothesis away — it is in the architecture, the
pattern, or an assumption nobody has questioned. The tells:

- Each fix reveals a new coupling or shared-state problem somewhere else.
- Fixes keep requiring "just a bit of refactoring" to land.
- Every fix creates a new symptom elsewhere.

That is not a failed hypothesis; it is the wrong mental model. Escalate to an
architecture-level conversation instead of attempting fix #4.

## Verification Before Completion

Verification is the completion of Phase 4, not a separate activity. Claiming work is done
without running the check is not efficiency — it is a false report, and it costs trust that is
expensive to rebuild.

### Gate function

Before claiming any status, or expressing satisfaction:

1. **Identify** — which command proves this claim?
2. **Run** — execute it in full, fresh, in this session.
3. **Read** — the whole output. Check the exit code. Count the failures.
4. **Verify** — does the output actually confirm the claim, or merely fail to contradict it?
5. **Only then** — make the claim, with the evidence attached.

Skipping any step is prediction, not verification.

### Common failures

| Claim | Requires | Not sufficient |
|-------|----------|----------------|
| Tests pass | Test output showing 0 failures, from this session | A previous run; "should pass" |
| Linter clean | Linter output: 0 errors | A partial check |
| Build succeeds | Build exit code 0 | Linter passing; logs that "look good" |
| Bug fixed | The original symptom no longer reproduces | Code changed, fix assumed |
| Regression test works | A verified red-green cycle (below) | The test passing once |
| Sub-agent completed work | The VCS diff showing actual changes | The agent's own self-report |
| Requirements met | Line-by-line check against the success criteria | "Tests pass" |

### Regression-test red-green verification

A regression test is trustworthy only if you watched it catch the bug:

1. Write the test → run → it passes with the fix in place.
2. **Revert the fix** → run → the test **must fail**.
3. Restore the fix → run → it passes again.

If step 2 still passes, the test is not testing the fix.

### Red flags

Any of these wordings means verification has not happened:

- "should work", "probably fine", "seems to", "looks correct"
- "tests likely pass", "looks good to me"
- Expressing satisfaction ("Perfect!", "Done!") with no evidence attached
- Trusting a sub-agent's success report without reading the diff

### Escape hatch — `no-verification-applicable`

Some steps genuinely cannot be verified by a command: documentation-only changes,
discussion or planning steps, a codebase with no test suite. For those, state the escape hatch
explicitly:

```
no-verification-applicable: <reason>
```

Canonical reasons: `documentation-only`, `planning-step`, `discussion-step`,
`no-test-suite-exists`. This is not a default — most completion steps *can* be verified, and
therefore must be.

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "The issue is simple, no process needed." | Simple bugs have root causes too, and the process is fast for simple bugs. |
| "It's an emergency, no time for process." | Systematic debugging is faster than thrashing. Guessing under pressure multiplies the problem. |
| "Just one more fix attempt" (after two failures). | Three failures means an architectural problem. Stop and question the pattern. |
| "I'll write the test once I've confirmed the fix." | Untested fixes don't stick, and a test written afterwards is not a regression test — you never saw it fail. |

---

*Maintained alongside `plugin/knowledge/debugging.md` in [TeamManagementPlugin/claude-plugin](https://github.com/TeamManagementPlugin/claude-plugin), from which it is adapted to stand alone. Change one, change both.*
