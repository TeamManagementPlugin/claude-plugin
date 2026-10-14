# Codex — Code Review

You are participating in **{phase}** for task `{task_name}` on branch `{branch}`.
Task file: `{task_file_path}`

## Context: Plan Summary
{plan_summary}

## Changed files
Repo-relative paths with their git porcelain status code (`??` untracked, ` M` modified in the
working tree, `M ` staged, `D` deleted, `A` added, `R` renamed):

{changed_files}

If that list is empty, the working tree is clean at this step — discover the scope yourself with
git before reviewing, and say in your reply what range you settled on.

## Your job
Run `codex review "<this prompt>"` against the repository and review the uncommitted changes —
staged, unstaged and untracked. Do NOT pass a scope flag alongside the prompt; the review
subcommand already defaults to the working-tree diff, and the two are mutually exclusive.

Review for correctness, security, and consistency with the patterns already in this codebase.
Your value is what a reviewer reading only the diff would miss: a change that is locally correct
but breaks a caller, a case the tests do not reach, an invariant documented elsewhere in the repo
that this diff quietly violates.

## Severity
Rate every finding on this three-level scale — the one every reviewer in this step uses:

- CRITICAL — security fault, data loss or corruption, crash, broken contract, unmet success
  criterion.
- WARNING — a defect: the code does the wrong thing in a case that can happen; or a success
  criterion no test exercises at all; or behaviour the change adds or alters that no test
  exercises at all.
- NOTE — everything else: a test that could assert more of a behaviour a test already covers,
  a mutation a test would let through, naming, style, refactor, a finding in code the change did
  not touch. Notes never block.

A finding between two levels takes the lower one unless you can name the input that makes the
code do the wrong thing.

## Output shape
Report each finding as one entry, most severe first:

- [CRITICAL|WARNING|NOTE] <one-line claim> — <path>:<line-range>
  <why it is wrong and what breaks, in two or three sentences>

Cite a real path and line range for every finding — an uncitable finding is not actionable and
will be dropped. Prefer three well-evidenced findings over ten speculative ones.

If you find nothing worth reporting, return:
> No findings; the diff reads correct and consistent with the surrounding code.
