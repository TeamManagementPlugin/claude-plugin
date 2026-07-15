# Receiving Code Review

Code review is technical evaluation, not social performance. Verify before implementing. Ask before assuming.

## Response Pattern

1. **Read** the complete feedback without reacting.
2. **Restate** the requirement in your own words (or ask if unclear).
3. **Verify** the claim against actual codebase state — file, line, test.
4. **Evaluate** whether it is technically correct *for this codebase*.
5. **Respond** with technical acknowledgment, a reasoned push-back, or a clarifying question.
6. **Implement** one item at a time, testing each before the next.

## Forbidden vs Preferred Phrases

**Forbidden** (performative, no technical content):

- "You're absolutely right!"
- "Great point!" / "Excellent feedback!"
- "Thanks for catching that!"
- Any gratitude — actions speak. The fixed code shows you heard.

**Preferred:**

- "Fixed. Extracted validation to `auth.py:validate_email`."
- "Good catch — off-by-one at line 47. Changed to `< n`."
- Just make the fix and let the diff show it.

If you catch yourself typing "Thanks" — delete it, state the fix instead.

## External Reviewer Skepticism

External reviewers (Codex, agy, a human without full project context) may hallucinate file paths, invent issues, or miss project-specific decisions. Before implementing external feedback:

1. Is it technically correct for *this* codebase?
2. Would it break existing functionality?
3. Is there a reason the current implementation is the way it is?
4. Does the reviewer understand the full context (CLAUDE.md, prior decisions, constraints)?
5. If you cannot easily verify, say so: "I can't verify this without X — should I investigate or skip?"

## YAGNI Grep Check

When a reviewer suggests "implement this properly" or expands scope:

```bash
grep -rn "<feature-name>" .
```

If unused: "This helper/endpoint/flag is not called anywhere — remove it (YAGNI)?"
If used: then implement properly.

## Push Back with Evidence

Push back when the suggestion breaks existing code, violates YAGNI, is technically wrong for the stack, or conflicts with prior decisions. How:

- Technical reasoning, not defensiveness.
- Reference the code: line number, existing test, concrete measurement.
- Ask specific questions rather than dismissing.

Do not avoid push-back out of discomfort — silent agreement produces bad code.

## When Your Push-Back Was Wrong

If verification proves the reviewer correct:

- "You were right — checked `config.py:42`, it does load before `init()`. Implementing now."
- No long apology, no over-explanation, no defense of the original push-back.

State the correction factually and move on.
