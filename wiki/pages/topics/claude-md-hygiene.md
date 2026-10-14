---
title: CLAUDE.md Hygiene
tags: [protocols, agents, config]
created: 2026-10-13
updated: 2026-10-13
sources: [claude_md_hygiene.py, protocol_engine.py, task.json, service-documentation.md]
---

# CLAUDE.md Hygiene

CLAUDE.md files are re-read into every session, so each line costs context for the life of the project. The framework keeps them a lean reference to the current code. Code: `claude_md_hygiene.py` (rules, line counting) and `_func_check_claude_md_hygiene` in `protocol_engine.py`.

## Content test
For every line, ask: *would a developer starting a new task tomorrow need this to find the code or avoid a mistake?*
- **Keep:** module purpose, key files one line each, non-obvious invariants and pitfalls, config keys.
- **Leave out:**
  - review results and rounds;
  - task names;
  - versions something was verified on;
  - history wording;
  - rationale narratives.
- **Where those go instead:** rationale to wiki decision pages like this one; changes to `plugin/CHANGELOG.md`; history to git and the task work log.

The rule lives in the `task.json` documentation step text and in `agents/service-documentation.md`. That agent receives the current behaviour of the changed modules, not the story of the task, and references code by symbol name rather than by line number.

## Why prose rules were not enough
The lean-documentation rules existed long before the gate, as prose only. The repository's five CLAUDE.md files still reached 2545 lines of task tags, review findings and history: authors write from the task's point of view, and nothing pushed back. With the rules made structural and the files rewritten under them, the five files total about 545 lines.

## The gate
`check_claude_md_hygiene` runs as a blocking post_func on the task documentation step and on the optimize synthesis step. It judges only lines a task ADDED, so a legacy file never blocks unrelated work. It blocks on:
- **task names** — real names collected from `team-management/tasks/` (open, `done/`, directory tasks), matched with word boundaries; skipped when the directory is absent;
- **history wording** — narrow phrases (`previously`, `no longer`, `was removed`, `renamed from`, `used to be`, …), so present-tense prose like "a key used to sign" passes;
- **review chatter** — `round N`, reviewer finding IDs such as `Codex W2`, `verified on <tool> <version>`;
- **budget** — a file that grew and exceeds `documentation.claude_md_max_lines` (default 300).

Growth and the budget count non-blank lines, excluding marker lines and `@`-imports inside the managed block. Session start wires that block into a project CLAUDE.md, so without the exclusion its lines would be charged to the next task. Only those lines are excluded, so no arrangement of markers — unclosed, stray, nested — can hide content.

`documentation.claude_md_hygiene: false` disables the gate. A git failure degrades to a pass with a warning: it is a quality gate and must not strand the documentation step.

## Gotchas
- The optimize synthesis gate diffs against HEAD. It sees only CLAUDE.md edits left uncommitted, not those committed during experimentation.
- The `round N` rule can flag rounding prose ("round 2 decimal places"). Rephrase the line.
- The repository's own five CLAUDE.md files are checked in full by `test/test_claude_md_hygiene.py`, which is stricter than the gate.

## Related
- [Review Loop Convergence](pages/topics/review-loop-convergence.md)
- [Specialized Agents](pages/entities/specialized-agents.md) — service-documentation.
