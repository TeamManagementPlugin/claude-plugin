# Brainstorm Results Sub-Protocol

## Purpose

Generate the final brainstorm results document and get user approval before proceeding to task planning.

## 1. Generate Results Document

Create the file `docs/brainstorm-results/<name>.md` (where `<name>` matches the brainstorm task name without the `b-brainstorm-` prefix).

The document must contain:

```markdown
# [Brainstorm Title] — Results

## Topic
[Short description of what was brainstormed and why]

## Key Features
- [Bulleted list of main features/capabilities]

## Justification
[Why this should be implemented — synthesize evidence from expert analysis, user perspective, and discussion decisions]

## Scope Definition
[Clear boundaries: what is in scope, what is out of scope]

## Feature Details

### [Feature 1 Name]
[Detailed description with technical context from architecture analysis]

### [Feature 2 Name]
[Detailed description with technical context]

## Reusable Code
[From Code Impact Analyst — specific file:line references]
- `path/to/file.py:42` — description of what can be reused and how

## Deprecated / Dead Code
[From Code Impact Analyst — specific file:line references]
- `path/to/old.py:15` — description of what becomes unnecessary and why
```

**Source data**: Pull from the brainstorm task file's `## Expert Analysis`, `## Decisions`, and `## Conflicts` sections. This document is a clean synthesis, not a copy.

## 2. Task Granularity Options

Before user review, propose **3 implementation granularity options**. For each, list the concrete tasks with one-line descriptions:

### Option A — Minimal (large consolidated tasks)
Group work by logical area. Each task covers a broad scope and may take multiple sessions. Fewer context switches, but each task is heavier.

### Option B — Balanced (moderate split)
Split by functional boundaries. Each task is a meaningful deliverable completable in 1-2 sessions.

### Option C — Fine-grained (many small tasks)
Maximum decomposition. Each task is a single focused change completable in one session. Easy to track and parallelize, but more coordination overhead.

**Present all three as tables**, then ask the user to pick A, B, or C (or adjust). Add the chosen option to the results document as an `## Implementation Plan` section with the task list.

## 3. User Review

Present the complete results document (including the chosen task plan) to the user:
- Highlight the key features and justification
- Show the scope boundaries
- Summarize the reusable code and deprecation impact
- Confirm the chosen task granularity

**If the user wants changes:**
- For minor edits: update the results document directly
- For significant changes: `protocol_goto("discussion")` to revisit decisions
- For re-analysis: `protocol_goto("analysis")` (if goto is backward from current step)

Wait for explicit user confirmation before advancing.

## 4. Spec Self-Review Checklist (before advancing)

Run this checklist against the results document + task plan inline before calling `protocol_advance`. Any failure = fix it here, not in the task that consumes this spec.

- [ ] **Placeholder scan** — grep the document for `TBD`, `TODO`, `FIXME`, «similar to», «etc.», «and so on», «details later». Any hit = rewrite that section with concrete content.
- [ ] **Internal consistency** — same concept called by the same name across all sections (no «event bus» → «message queue» → «dispatcher» drift). Same file paths and line ranges cited consistently.
- [ ] **Scope check** — `## Scope Definition` actually bounds `## Feature Details`. Nothing in features exceeds the declared scope; nothing declared out-of-scope reappears in the plan.
- [ ] **Ambiguity check** — each Feature Detail entry answers «what exactly is built?» (not «what direction to explore?»). Anything that reads like a research question = either convert to a concrete feature or move to a follow-up research task.
- [ ] **Decision coverage** — every `[x]` decision in the brainstorm task file's `## Decisions` and every `## Feature Details` entry maps to at least one task in the `## Implementation Plan`. Any decision with no planned task is pulled into the plan now, or moved under `## Scope Definition` as explicitly out-of-scope. This is the lighter plan-level pre-check (decisions + features); the planning step's full audit additionally covers in-scope `## Scope Definition` items and `## Deprecated / Dead Code` entries against the actual task files (`brainstorm-planning.md` Section 2 — Coverage Verification).

Fix gaps before advancing. The downstream task protocol treats this spec as authoritative — ambiguity here becomes `protocol_goto` churn there.

## 5. Update Work Log

Add to the brainstorm task file's `## Work Log`:
```markdown
- [YYYY-MM-DD] Results document created: docs/brainstorm-results/<name>.md
```

SESSION RECOVERY: Call `protocol_save_note()` after creating the results document.
