# Task Investigation Sub-Protocol

SCOPE OF THIS STEP: Understand the request, explore the codebase, align with the user, and compose the complete task file content. Only investigation + task definition belongs here — no implementation.

## 0. Launch AI Providers IN PARALLEL (if configured)

**BEFORE you do anything else in this step — read the AI provider state and dispatch any configured providers. They explore the codebase concurrently with you and surface context you might miss.**

1. **Read `pre_funcs_results`** from the most recent `protocol_advance` response (or this step's `start_protocol` response if you just entered the protocol). Find the entry where `func == "resolve_ai_providers_for_investigation"`. Its `providers` field is the list of configured AI providers (e.g. `[]`, `["codex"]`, or `["codex", "agy"]`); its `instructions` field spells out the exact `subagent_type` and a ready-to-use `prompt` for each.
2. **If `providers == []` → skip this section entirely.** Empty list means no providers are configured for this phase; proceed directly to Section 1.
3. **If `providers` is non-empty → compose ONE message containing N parallel `Task` tool calls** (one per provider), using `subagent_type: "codex-cli"` for codex and `subagent_type: "agy-cli"` for agy. The wrapper agents load their system prompts from `.claude/agents/<name>.md` automatically.

**Treat output as advisory.** Providers can hallucinate file paths or invent issues — apply `@knowledge/receiving-feedback.md` (external-reviewer skepticism, file:line verification before action). A finding without a verifiable file:line citation is not actionable; either resolve it to a real file:line by reading the cited area yourself, or drop it.

**Record significant findings** in the task work log under `## AI Provider Input — Investigation`. «Significant» = either context you adopted into the task scope / success criteria (becomes a clarifying question or a Context Files entry), or one you explicitly rejected (note why in one line). Boilerplate or empty output may be elided. If a wrapper returns `<provider> review unavailable: …`, note the unavailability one-line and proceed.

**Then continue to Section 1.**

## 1. Understand and Explore

Investigate the codebase to find relevant patterns, existing implementations, and conventions. Use `code-explorer` agents for deep analysis if scope is complex.

### Question Discipline

Three-step procedure for clarifying questions:

1. **Compose the backlog up front.** Before asking any clarifying question, draft a `TodoWrite` list of everything you need answered across eight dimensions: task purpose, scope, affected modules, expected behaviour, edge cases / failure modes, non-functional requirements (performance, security, compatibility), terminology / data definitions, integration points. Seeing the list together exposes redundancy and ordering dependencies («X depends on Y — ask Y first»). Then prioritize by **Impact × Uncertainty** — ask first where a wrong guess is most expensive AND your own confidence is lowest. **Cap: at most 5 widget questions per investigation.** Close the remaining low-impact items as reasonable defaults recorded in `## User Notes` — the cap limits questions asked, not the zero-ambiguity end state.
2. **Ask via `AskUserQuestion`, strictly one question per call — recommendation first.** For every option-based question, the prose context BEFORE the widget must contain a line `Recommendation: <option> — <reasoning>`, and the recommended option is listed first in `options`. You have already investigated the codebase — stating your position is the value you add; a bare menu outsources the thinking to the user. (Free-text questions have no options, hence no recommendation line.) Use 2-4 options per question with a short `header` (≤12 chars) and `multiSelect: false` unless choices are not mutually exclusive. The tool auto-appends an «Other» option for free-text — do not add it manually. Use plain free-text in chat (no widget) only when valid options are unknowable in advance or prose is the answer (e.g. «describe the bug you're hitting»).
3. **Mark each TodoWrite item completed as the user answers.** Stop when zero ambiguity remains across all dimensions — every item either answered by the user or closed as a recorded reasonable default — not when the backlog is empty (one user answer often closes multiple questions at once).

**Field-content discipline** — the widget is the only thing the user sees while answering, so each field must stand alone:
- **`question`** — 1-2 sentences carrying enough context to make the choice intelligible *without re-reading the prior conversation*. A 5-word title forces the user to scroll back; a paragraph is too much. One or two sentences is the budget.
- **`description` (per option)** — explain not just *what* the option means but the *trade-off*: what you gain, what you give up. A bare label says «Polling»; a useful description says «Polling — simpler to implement, but adds latency and load».
- **`header`** — ≤12 chars, widget UI hard limit; do not relax.

**Output buffer** — before the `AskUserQuestion` tool call, output 2-3 blank lines after your prose context. The widget renders over the tail of the prior text and visually swallows the last few lines; blank-line padding keeps your context visible.

**One question per `AskUserQuestion` call. Large questions need context — bundling multiple into one widget forces the user to hold parallel state.**

#### Worked Example

```
... your prose context above (the user's request, what you've explored, what's still ambiguous) ...

Recommendation: Bug fix — the traceback points at existing behaviour regressing, not a missing capability.



AskUserQuestion(questions=[{
  "question": "Looking at the request and the surrounding code, the work could be framed in different ways. Which best matches the intent — that determines whether TDD applies and which branch prefix the protocol picks?",
  "header": "Type",
  "multiSelect": false,
  "options": [
    {
      "label": "Bug fix",
      "description": "Diagnose root cause and patch. TDD-applicable: failing test reproduces the bug, fix makes it pass. Branch prefix: fix/."
    },
    {
      "label": "Behaviour change",
      "description": "Modify an existing feature's contract. TDD-applicable: test the new contract first. Branch prefix: feature/."
    },
    {
      "label": "New feature",
      "description": "Add a capability that does not exist today. TDD-applicable when inputs/outputs are clear; if exploratory, treat as a spike. Branch prefix: feature/."
    },
    {
      "label": "Refactor",
      "description": "Restructure without behaviour change — existing test suite is the safety net. Branch prefix: feature/."
    }
  ]
}])
```

Note: «Other» is auto-appended by the tool — never add it as an explicit option.

## 2. Propose 2-3 Distinct Approaches

Do NOT present a single approach and proceed. For any non-trivial task, surface **2-3 distinct approaches with trade-offs**:

- **Approach A** — [one-line summary]. Trade-offs: [what you gain, what you give up].
- **Approach B** — [different strategy]. Trade-offs: […].
- **Approach C** — [alternative axis — simpler / faster / more future-proof]. Trade-offs: […].

If only one approach genuinely makes sense, state explicitly why alternatives were rejected. Surfacing alternatives exposes hidden assumptions before they turn into rework.

## 3. Advisory: TDD Applicability

If the task is TDD-applicable (bug fixes, behavior changes, feature work with clear inputs/outputs), consult `@knowledge/tdd-discipline.md` — frame success criteria in terms of verifiable tests. Not every task is TDD-applicable (spikes, scaffolding, docs-only changes are exempt); the applicability matrix is in the same file.

## 4. Compose the Implementation Plan — No Placeholders Rule

When writing `## Implementation Plan` (or success criteria), the plan must read like a diff script a future contributor can execute mechanically:

- **Exact paths**: `plugin/hooks/shared_state.py:754-775`, not «the shared state module».
- **Full code in each step**: include the literal snippet to be added/changed, not «add a helper» or «wire up the function».
- **Exact commands with expected output**: `pytest tests/test_foo.py::test_bar -v` → `1 passed`. Not «run the tests».
- **Forbidden markers**:
  - `TBD` / `TODO` / `FIXME` inside the plan itself
  - «similar to Task N» / «same pattern as before» without expanding the pattern inline
  - «add validation» / «handle edge cases» without enumerating which validations and which edge cases
  - Type drift: same concept called `dict` in one step and `JSON object` in another — pick one name and use it throughout.

**Success criteria are outcomes, not process.** Each criterion names an observable end state and how to verify it — not the activity that produces it:

- ✅ `SC-1: config_update rejects an unknown dotted key with a schema error (test: test_config_update_unknown_key)` — outcome + named verification.
- ❌ `SC-1: Refactor the config module` — process; no observable end state, nothing to verify.

**Drafting exception — the clarification marker.** While the task file is still a DRAFT (before `protocol_advance`), an unresolved point may be marked inline as `[NEEDS CLARIFICATION: <question>]` instead of being silently guessed. Rules:
- **Max 3 markers** per task file. More means you haven't prioritized — ask the top questions instead.
- **Priority when choosing what to mark**: scope > security/privacy > user experience > technical details.
- **Don't mark reasonable defaults** (data retention, performance targets, error-handling style, integration patterns) unless the user raised them — pick the sensible default and note it in `## User Notes`.
- **Every marker must be resolved before delivery.** The engine REJECTS a task file that still contains a marker (validation error on `protocol_advance`). Resolve each with the user via the Question Discipline above, or record the accepted unknown explicitly in `## User Notes` and remove the marker.

## 5. Plan Self-Review (before advancing)

Run this checklist inline before calling `protocol_advance`:

- **Spec coverage** — does every success-criteria bullet map to at least one plan step? Any orphan criterion = gap.
- **ID coverage** — with numbered criteria (`SC-N`), every SC-N must be referenced by at least one plan step's `[SC-N]` tag. A criterion no step claims = plan hole; a step tagged with no criterion = scope creep candidate.
- **Placeholder scan** — grep mentally for `TBD`, `TODO`, «similar», «etc.», «and so on», and unresolved clarification markers. Any hit = rewrite that step inline (markers: resolve with the user first).
- **Type consistency** — same concept referred to by the same name across all steps (no dict/object/mapping drift).

Fix gaps before advancing. A gap caught now costs seconds; a gap caught in implementation costs a `protocol_goto` + re-planning round-trip.

## 6. Task File Conventions

- **Priority prefix**: `h-` (high), `m-` (medium), `l-` (low), `r-` (research/investigate), `o-` (optimize), `b-` (brainstorm)
- **Task type → branch mapping**: `implement-` → `feature/`, `fix-` → `fix/`, `refactor-` → `feature/`, `research-` → none, `experiment-` → `experiment/`, `migrate-` / `test-` / `docs-` → `feature/`. Special prefixes that double as action prefixes: `o-` → `optimize/`, `b-` → `brainstorm/`.
- **File vs directory**: FILE for a single focused goal (<3 days, no subtasks); DIRECTORY for multi-phase work.
- **Numbered criteria and plan steps**: write Success Criteria as `- [ ] SC-1: ...` (stable IDs — never renumber, only append) and Implementation Plan steps as `- [ ] T1 [SC-1]: ...` with tags naming the criteria each step covers. The spec-compliance audit reports coverage per SC-ID.
- **Context gathering — hybrid rule**: if the task is a DIRECTORY task (its task file is `team-management/tasks/<task>/README.md`) OR its frontmatter `modules` lists more than 3 entries, dispatch the `context-gathering` agent AFTER writing the draft task file (write-file-first, §7) and BEFORE `protocol_advance`, passing the task file path — the agent requires an existing file and writes the Context Manifest into it directly. Otherwise fill `## Context Files` manually: for a small task the agent round-trip costs more than it saves.

## 7. Alignment Before Advance

Before calling `protocol_advance`, you MUST:

1. Present the user with a clear summary: task scope, affected modules, the chosen approach (among the 2-3 surfaced above), success criteria.
2. Wait for EXPLICIT user agreement (e.g. «looks good», «approved», «go ahead»). Silence ≠ agreement.
3. Read `team-management/tasks/TEMPLATE.md` and compose the full task file content (frontmatter + success criteria + context + `## Implementation Plan` if appropriate).
4. Deliver the task file to the engine. **Preferred for any substantial file — write-file-first:** write the full markdown to `team-management/tasks/<priority>-<name>.md` (DIRECTORY task: `team-management/tasks/<priority>-<name>/README.md` — the engine resolves both) with the Write tool (whitelisted in discussion mode), then advance with an **empty** `task_content` — `protocol_advance(args={"task": "<priority>-<name>", "branch": "<type>/<name>", "task_content": ""})`. The engine re-validates the on-disk file and keeps it exactly as written (it does not overwrite it). Only for a genuinely small file, pass the markdown inline instead (`"task_content": "<full markdown>"`). Large object-typed args are unreliable — a multi-KB `task_content` can arrive empty — so emit the `protocol_advance` call **bare** and keep the `args` object small. Because you wrote the file **before** advancing, `git_setup_branch` (which runs first) will pause with `needs_confirmation` on the now-dirty tree — that's expected; re-run `protocol_advance` with `carry_changes: true` added to carry the task file onto the new branch (see §8).

The protocol engine automatically: creates the task file, sets task state, creates + checks out the git branch, creates the provider issue (if enabled), and flips task status to in-progress.

## 8. Dirty Working Tree

If there are uncommitted changes when you call `protocol_advance`, `git_setup_branch` pauses with `needs_confirmation=true` and lists the dirty files. Ask the user whether to carry them onto the new branch (re-run `protocol_advance` with `carry_changes: true` in args) or to commit / stash them first.

## 9. Session Recovery

Call `protocol_save_note()` after each significant finding or decision. User messages are auto-saved; YOUR analysis, findings, and proposals are NOT. These notes are the lifeline for session recovery — without them, a restart loses all investigation context.

## Going Back

If during later steps the scope is unclear or an assumption breaks, use `protocol_goto(step_name="investigation", reason=...)` to return here and re-align with the user.
