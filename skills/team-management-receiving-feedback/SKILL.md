---
name: team-management-receiving-feedback
description: How to receive code review as technical evaluation rather than social performance — verify claims against the codebase before implementing, push back with evidence, and drop the performative agreement. Use when responding to a human reviewer, a PR comment, or another AI reviewer, and when deciding whether a suggestion is actually right for this codebase. Triggers on review feedback, "you're absolutely right", scope-expanding suggestions, and hallucinated file paths from external reviewers.
license: MIT
metadata:
  author: team-management
  version: "1.0.0"
---

# Receiving Code Review

Code review is technical evaluation, not social performance. Verify before implementing. Ask
before assuming.

## Response Pattern

1. **Read** the complete feedback without reacting.
2. **Restate** the requirement in your own words — or ask, if it is unclear.
3. **Verify** the claim against the actual state of the codebase: the file, the line, the test.
4. **Evaluate** whether it is technically correct *for this codebase*.
5. **Respond** with a technical acknowledgment, a reasoned push-back, or a clarifying question.
6. **Implement** one item at a time, testing each before starting the next.

## Forbidden vs Preferred Phrases

**Forbidden** — performative, carrying no technical content:

- "You're absolutely right!"
- "Great point!" / "Excellent feedback!"
- "Thanks for catching that!"
- Gratitude generally. Actions speak; the fixed code shows you heard.

**Preferred:**

- "Fixed. Extracted validation to `auth.py:validate_email`."
- "Good catch — off-by-one at line 47. Changed to `< n`."
- Or just make the fix and let the diff show it.

If you catch yourself typing "Thanks" — delete it and state the fix instead.

## External-Reviewer Skepticism

External reviewers — another AI reviewer, or a human without full project context — may
hallucinate file paths, invent issues, or miss decisions that were made deliberately. Before
implementing external feedback, ask:

1. Is it technically correct for *this* codebase?
2. Would it break existing functionality?
3. Is there a reason the current implementation is the way it is?
4. Does the reviewer have the full context — the project's conventions, prior decisions,
   constraints?
5. If you cannot easily verify it, say so: "I can't verify this without X — should I
   investigate, or skip it?"

A finding with no verifiable file:line citation is not actionable. Resolve it to a real location
by reading the code yourself, or drop it.

## The YAGNI Grep Check

When a reviewer suggests "implement this properly" or otherwise expands scope:

```bash
grep -rn "<feature-name>" .
```

If it is unused: "This helper/endpoint/flag isn't called anywhere — remove it (YAGNI)?"
If it is used: then implement it properly.

## Push Back With Evidence

Push back when the suggestion breaks existing code, violates YAGNI, is technically wrong for the
stack, or conflicts with a prior decision. How:

- Technical reasoning, not defensiveness.
- Reference the code: a line number, an existing test, a concrete measurement.
- Ask specific questions rather than dismissing the point.

Do not avoid push-back out of discomfort. Silent agreement produces bad code, and it wastes the
reviewer's time on a conclusion nobody actually holds.

## When Your Push-Back Was Wrong

If verification proves the reviewer correct:

- "You were right — checked `config.py:42`, it does load before `init()`. Implementing now."
- No long apology, no over-explanation, no defence of the original position.

State the correction factually and move on.

---

*Maintained alongside `plugin/knowledge/receiving-feedback.md` in [TeamManagementPlugin/claude-plugin](https://github.com/TeamManagementPlugin/claude-plugin), from which it is adapted to stand alone. Change one, change both.*
