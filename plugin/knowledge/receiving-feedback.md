# Receiving Code Review

Code review is technical evaluation, not social performance. Verify before implementing. Ask before assuming.

## Response Pattern

1. **Read** the complete feedback without reacting.
2. **Restate** the requirement in your own words.
3. **Verify** the claim against actual codebase state — file, line, test.
4. **Evaluate** whether it is technically correct *for this codebase*.
5. **Respond** with technical acknowledgment, a reasoned push-back, or a clarifying question.
6. **Implement** one item at a time, testing each before the next.

## Forbidden vs Preferred Phrases

**Forbidden** (performative):

- "You're absolutely right!"
- "Great point!" / "Thanks for catching that!"
- Any gratitude — the fixed code shows you heard.

**Preferred:**

- "Fixed. Extracted validation to `auth.py:validate_email`."
- "Good catch — off-by-one at line 47. Changed to `< n`."

Catch yourself typing "Thanks"? Delete it; state the fix.

## External Reviewer Skepticism

External reviewers (Codex, agy, humans without full context) may hallucinate file paths, invent issues, or miss project-specific decisions. Before implementing external feedback:

1. Is it technically correct for *this* codebase?
2. Would it break existing functionality?
3. Is the current implementation this way for a reason?
4. Does the reviewer know the full context (CLAUDE.md, prior decisions, constraints)?
5. If you cannot verify, say so: "I can't verify this without X — investigate or skip?"

## User Sovereignty

Two models agreeing is a signal, not permission to act — the user's direction is the default; the burden of proof is on the models.
To challenge the user's explicit direction, present a User Challenge — never auto-decide: what the user said; what the models recommend and why; what context they may be missing; the cost if they are wrong.

## YAGNI Grep Check

When a reviewer suggests "implement this properly" or expands scope:

```bash
grep -rn "<feature-name>" .
```

If unused: "Not called anywhere — remove it (YAGNI)?"
If used: implement properly.

## Push Back with Evidence

Push back when the suggestion breaks existing code, violates YAGNI, is wrong for the stack, or conflicts with prior decisions — with technical reasoning: line numbers, existing tests, concrete measurements, specific questions. Do not avoid push-back out of discomfort — silent agreement produces bad code.

## When Your Push-Back Was Wrong

If verification proves the reviewer correct, state the correction factually and move on:

- "You were right — checked `config.py:42`, it does load before `init()`. Implementing now."
