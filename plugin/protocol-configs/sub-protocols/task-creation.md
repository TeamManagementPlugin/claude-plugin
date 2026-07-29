# Task Naming Conventions Reference

This file is a reference for the AI when composing task file content during the investigation step. Task file creation is handled automatically by the engine.

## Delivering the Task File Content

Two ways to hand the composed task file to the engine:

- **Write-file-first (PREFERRED for any substantial task file).** Write the full markdown directly to `team-management/tasks/<task>.md` with the Write tool (task files there are whitelisted in discussion mode), THEN advance with an **empty** `task_content`:
  `protocol_advance(args={"task": "<name>", "branch": "<branch>", "task_content": ""})`.
  The engine re-validates the on-disk file (frontmatter / `status` / prefix / branch / `## Success Criteria` / no unresolved NEEDS-CLARIFICATION markers) and keeps your file as-is — it does not overwrite it. Use this whenever the file is more than a few lines: large object-typed tool args are unreliable (a multi-KB `task_content` can silently arrive empty), so keeping the `args` object tiny is the robust path. In a branch-creating protocol (e.g. `task`), `git_setup_branch` runs first and will pause with `needs_confirmation` because the pre-written file makes the tree dirty — re-run `protocol_advance` with `carry_changes: true` to carry the file onto the new branch. (Research scoping uses `branch: none`, so there is no such pause.)
- **Inline `task_content` (fine for genuinely small files).** Pass the full markdown inline:
  `protocol_advance(args={"task": "<name>", "branch": "<branch>", "task_content": "<full markdown>"})`. The engine writes the file and injects `**Author:**` if missing.

**General rule for object-typed tool args:** emit the tool call **bare** (no preamble prose in the same turn) and keep the `args` object **small** — offload large payloads to a file first.

## Priority Prefix System

Priority prefixes (no longer configurable — these defaults are authoritative):
- `h-` High priority
- `m-` Medium priority
- `l-` Low priority
- `r-` Research/Investigate
- `o-` Optimize
- `b-` Brainstorm

## Task Type Prefix -> Branch Mappings

- `implement-` -> `feature/` branch
- `fix-` -> `fix/` branch
- `refactor-` -> `feature/` branch
- `research-` -> No branch needed
- `experiment-` -> `experiment/` branch
- `migrate-` -> `feature/` branch
- `test-` -> `feature/` branch
- `docs-` -> `feature/` branch

## File vs Directory Decision

**Use a FILE** when: single focused goal, < 3 days work, no obvious subtasks.
**Use a DIRECTORY** when: multiple phases, clear subtasks from start, > 3 days work.

## Task File Format

The AI must read `team-management/tasks/TEMPLATE.md` to get the exact template format. The task file content passed to `protocol_advance(args={task_content: "..."})` must include:

1. **Complete frontmatter** with:
   - `task`: Must match the task arg (including priority prefix)
   - `branch`: Based on task type prefix (or 'none' for research)
   - `status`: Start as `pending`
   - `created`: Today's date
   - `modules`: List all services/modules that will be touched

2. **Author line**: `**Author:** [developer_name]` immediately after the `# Title` line (the engine injects this automatically from config if missing)

3. **Clear success criteria**: Specific, measurable, with checkboxes. Number them with stable IDs (`- [ ] SC-1: ...`; never renumber, only append) and tag Implementation Plan steps with the criteria they cover (`- [ ] T1 [SC-1]: ...`). Any drafting-time clarification markers must be resolved before delivery — the engine rejects a file that still contains one.

4. **Context section**: Relevant files, dependencies, considerations

## Task Evolution

If a file task needs subtasks during work:
1. Create directory with same name
2. Move original file to directory as README.md
3. Add subtask files
4. Update active task reference if needed

## Note

This file lives at `plugin/protocol-configs/sub-protocols/task-creation.md` (resolved at runtime via the plugin root; a legacy install also finds it under `team-management/protocol-configs/system/sub-protocols/`) and can be referenced by protocol steps via `@sub-protocols/task-creation.md`. The investigation step in `task.json` inlines the naming conventions in its `start` prompt, so this file serves primarily as a detailed reference.
