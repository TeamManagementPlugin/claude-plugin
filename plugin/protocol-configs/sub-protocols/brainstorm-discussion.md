# Brainstorm Deep Discussion Sub-Protocol

## Purpose

Conduct a detailed discussion with the user about the brainstorm topic, recording every accepted decision in the task file. This step also serves as the conflict resolution step when returning from multi-perspective analysis.

## Discussion Mode

This step operates in **documentation mode** — you can edit the brainstorm task file to record decisions, but cannot edit source code.

## 1. Structured Discussion

Systematically explore the topic with the user:

### Key Questions to Address
- **Functional requirements**: What exactly should this do? What are the key features?
- **Boundaries**: What is explicitly NOT part of this? Where does the scope end?
- **Integration**: How does this fit with existing systems and workflows?
- **Users**: Who benefits from this? How will they interact with it?
- **Constraints**: What technical, time, or resource constraints exist?
- **Priorities**: If not everything can be done, what matters most?

### Question Discipline

Three-step procedure — apply it every time, not just for «big» topics:

1. **Compose the backlog up front.** Before asking any clarifying question, draft a `TodoWrite` list of every question you want answered across the dimensions above. Seeing them together exposes redundancy («two of these collapse into one») and ordering («X depends on Y — ask Y first»).
2. **Ask via `AskUserQuestion`, strictly one question per call.** Use 2-4 options per question with a short `header` (≤12 chars) and `multiSelect: false` unless choices are not mutually exclusive. The tool auto-appends an «Other» option for free-text — do not add it manually. Use plain free-text in chat (no widget) only when valid options are unknowable in advance or prose is the answer (e.g. «describe the bug you're hitting»).
3. **After each answer, summarize what you heard and confirm with the user *before* recording the decision under `## Decisions`** (see Section 2). The summarize→confirm→record order matters — once a decision is in `## Decisions` future analysis treats it as load-bearing, so the user must catch misinterpretations *before* it's committed. After the user confirms, mark the TodoWrite item completed and move to the next question.

**Field-content discipline** — the widget is the only thing the user sees while answering, so each field must stand alone:
- **`question`** — 1-2 sentences carrying enough context to make the choice intelligible *without re-reading the prior conversation*. A 5-word title forces the user to scroll back; a paragraph is too much. One or two sentences is the budget.
- **`description` (per option)** — explain not just *what* the option means but the *trade-off*: what you gain, what you give up. A bare label says «Polling»; a useful description says «Polling — simpler to implement, but adds latency and load».
- **`header`** — ≤12 chars, widget UI hard limit; do not relax.

**Output buffer** — before the `AskUserQuestion` tool call, output 2-3 blank lines after your prose context. The widget renders over the tail of the prior text and visually swallows the last few lines; blank-line padding keeps your context visible.

**One question per `AskUserQuestion` call. Large questions need context — bundling multiple into one widget forces the user to hold parallel state.**

If you anticipate the user being unsure, reserve ONE of the 2-4 option slots for a «Not sure — let's discuss» choice (replacing the lowest-priority concrete option, not adding a 5th — that would violate the schema). If picked, drop the widget and continue in plain prose chat until the topic is clear, then re-ask via `AskUserQuestion` with options informed by the discussion. Keep the conversation flowing naturally; don't force rigid structure for its own sake.

#### Worked Example

```
... your prose context above (what you've explored, why this choice matters now) ...



AskUserQuestion(questions=[{
  "question": "Several brainstorm steps run in discussion mode and could adopt the sequential-question discipline. Should it apply only here, or sweep more broadly across other protocols' discussion-mode steps?",
  "header": "Scope",
  "multiSelect": false,
  "options": [
    {
      "label": "Brainstorm only",
      "description": "Smallest blast radius — fastest to ship, easiest to revert. Trade-off: other protocols keep the old terse pattern, so the inconsistency persists."
    },
    {
      "label": "Brainstorm + task",
      "description": "Covers the two highest-traffic protocols. Trade-off: still leaves research/refactoring on the old pattern, only a partial sweep."
    },
    {
      "label": "All discussion-mode steps",
      "description": "Maximum consistency across the framework. Trade-off: larger surface to audit; risk of regressions in protocols you don't actively use."
    }
  ]
}])
```

Note: «Other» is auto-appended by the tool — never add it as an explicit option.

### Propose 2-3 Distinct Approaches (with trade-offs)

Whenever the user faces a non-trivial design decision, do NOT present a single option and ask «sound good?». Surface **2-3 distinct approaches with explicit trade-offs** so the user can reason about the axis being chosen:

- **Approach A** — [summary]. Trade-offs: [what you gain, what you give up].
- **Approach B** — [different strategy]. Trade-offs: […].
- **Approach C** — [alternative axis — simpler / faster / more future-proof]. Trade-offs: […].

Record the chosen approach — and the rejected alternatives — under `## Decisions`. Preserving the rejected options documents *why* the accepted one won, which is load-bearing context for later analysis and implementation.

## 2. Recording Decisions

After each accepted decision, **immediately** update the task file's `## Decisions` section:

```markdown
## Decisions
- [x] Feature X will use event-driven architecture (not polling)
- [x] Configuration stored in team-management/config.json, not separate file
- [x] Support only GitLab initially, other providers in future phase
- [ ] Caching strategy: TBD — needs performance analysis
```

**Rules**:
- Use `[x]` for confirmed decisions
- Use `[ ]` for questions that still need resolution
- Each decision should be a clear, actionable statement
- Include the reasoning briefly if it's not obvious

## 3. Conflict Resolution (when returning from analysis)

If this step was reached via `protocol_goto` from the analysis step:

1. Read the `## Conflicts` section in the task file — it contains the conflicting opinions
2. Present each conflict to the user with the competing viewpoints
3. For each conflict:
   - Explain both sides clearly
   - Suggest a resolution if you have one
   - Let the user decide
4. Record the resolution in `## Decisions` as a new confirmed decision
5. Update the `## Conflicts` table with the resolution
6. Clear all `[ ]` items — every question must be resolved before re-running analysis

## 4. Readiness Check

Before advancing, ensure:
- All `[ ]` items in `## Decisions` are resolved (changed to `[x]` or removed)
- The user explicitly confirms they're ready for multi-perspective analysis
- If coming from conflict resolution: all conflicts have resolutions documented

## When Ready

Call `protocol_advance` when the user confirms all decisions are made and they're ready for expert analysis.

SESSION RECOVERY: Call `protocol_save_note()` after recording significant decisions. Notes survive context compaction.
