# Writing Behavioral Content

**Audience:** maintainers of the team-management framework.
**Scope:** how to add, edit, and prune behavioral rules that live in `CLAUDE.tm.md` and the runtime knowledge files under `plugin/knowledge/`.

This document is *not* loaded at runtime and is not copied to deployed projects — it captures the editorial discipline used to keep the behavioral layer thin, concrete, and actually load-bearing.

## Why Behavioral Content Is Different

Behavioral content is read by Claude on every session or protocol step. Unlike library docs or architecture notes, it directly shapes what Claude does next. This has two consequences:

1. **Every line costs tokens forever.** Fluff in behavioral docs is not free — it is a rent payment on every future session's context window.
2. **Clarity beats completeness.** A rule that is ambiguous will be interpreted inconsistently. A rule that is forgotten because it was buried in three paragraphs has negative value.

The editorial job is: make each rule land, then stop.

## Line Budget

- **`CLAUDE.tm.md` sections: 5–7 lines each, max.** If a section needs more, it probably belongs in a knowledge file with a `@knowledge/*.md` reference from `sessions.md`.
- **Runtime knowledge files**: no hard line cap, but every paragraph should earn its place. Delete examples that duplicate prose, collapse redundant tables, prefer one crisp rule over three fuzzy ones.
- **`receiving-feedback.md` specifically: under 400 words runtime.** It is read every code review — concision is mandatory, not aspirational.
- **Maintainer docs (this file, and others under `docs/knowledge/`)**: no budget — these are read rarely and consumed by humans.

## RED-GREEN-REFACTOR for Documentation

The same discipline that applies to code applies to behavioral text.

### RED — State the failure mode

Before writing a rule, name the specific anti-pattern it prevents. "Users write X when they should write Y, which causes Z."

If you cannot name the anti-pattern, the rule does not belong in the framework — it is personal style, not shared discipline.

### GREEN — Write the minimum rule

Write the shortest version of the rule that reliably prevents the anti-pattern. Not a paragraph — a sentence. Cite the mechanism: *why* it fails without the rule.

### REFACTOR — Prune rationalizations

After the rule is in place, collect the 3–5 most common *rationalizations* you (or reviewers, or the user) have used to skip it. Put them in a table: excuse on the left, reality on the right. Two purposes:

1. Named rationalizations are easier to catch in your own reasoning than unnamed ones.
2. The table compresses a lot of lived experience into a scannable format.

Keep the table tight — if you need more than ~5 rows, the rule is doing too many things.

## Rationalization Table Pattern

```markdown
| Excuse | Reality |
|--------|---------|
| "Too simple to need a test." | Simple code breaks. The test takes 30 seconds. |
| "I'll write the test after." | Tests written after pass immediately — passing immediately is not evidence. |
```

Rules of thumb:

- Left column is the *actual words* you have heard or said. Not a paraphrase.
- Right column is a *short* counter. One sentence. No hedging ("it depends", "sometimes").
- If the counter is longer than the excuse, the excuse is winning — tighten it.

## Pressure-Testing Methodology

Before merging a new behavioral rule:

1. **Can you produce a concrete example where this rule fires?** If no, cut it.
2. **Can you produce a concrete example where this rule should *not* fire (the anti-rule)?** If no, the rule is too broad — add a scope carve-out.
3. **Does an existing rule already cover this case?** If yes, edit the existing rule rather than adding a new one. Rule duplication fragments attention.
4. **Is the rule actionable in the moment it fires?** "Be careful with database migrations" is not actionable. "Before running a migration on a table with >1M rows, add the column nullable, backfill in batches, then set NOT NULL" is.
5. **What is the failure cost if the rule is wrong?** Rules that block work should have higher evidentiary bars than rules that merely suggest. If this rule gates advancement (via `_func_*`), it needs a clear escape hatch.

## Testing Types

Behavioral content can be "tested" in three ways:

1. **Resolution test** — does `@knowledge/foo.md` actually resolve? (See `resolve_protocol_start_text` in `shared_state.py`. Add a base path if a new location is introduced.)
2. **Load test** — does a fresh Claude session with the installed template produce the expected behavior on the first relevant prompt? If adding a rule to `CLAUDE.tm.md`, spot-check by running the scenario.
3. **Prune test** — six months later, is the rule still firing? Git-grep recent task files for the phrases the rule would produce. If zero hits, either the rule is not being triggered or Claude has internalized it — in the first case, cut; in the second, consider whether the rule can move out of the always-on layer into a knowledge file.

## Style Constraints (team-management-specific)

- **No "Iron Law" framing.** Rules are not commandments; they are engineering discipline with reasons. Readers who understand the reason can judge edge cases; readers who follow a commandment cannot.
- **No "your human partner" phrasing.** We say "the user" or address the operator directly.
- **DAIC-native language.** Reference protocols (`protocol_goto`, `protocol_advance`) and modes (discussion, implementation) directly. Do not invent parallel vocabulary.
- **No emojis in behavioral text** unless they are functional markers (e.g., 🔴/🟡/🟢 severity headers in code-review output). Decorative emojis waste tokens and do not aid judgment.
- **Code blocks for commands, not for emphasis.** Reserve fenced blocks for things the reader would actually type.

## When to Split a Rule Into a Knowledge File

If the rule needs any of:

- A table of cases (applicability matrix, common failures).
- Example code snippets beyond one or two lines.
- Cross-references to multiple other rules.
- A rationalization table.

…move the body to `plugin/knowledge/<topic>.md` and leave only the headline rule + a `@knowledge/<topic>.md` reference in `CLAUDE.tm.md`.

## When to Delete a Rule

- The rule is no longer being violated in practice (Claude internalized it or the surrounding process removed the failure mode).
- The rule has been superseded by a structural gate (a `_func_*` hook, a protocol step, a linter). Structural enforcement beats textual enforcement — keep the text only if it adds *why* beyond what the structure communicates.
- The rule overlaps another rule and the overlap is confusing readers.

Deleting a rule is a normal editorial action. The behavioral layer should shrink as the framework matures.
