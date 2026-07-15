---
title: LLM Wiki Feature
tags: [wiki, architecture, config]
created: 2026-05-31
updated: 2026-07-03
sources: [plugin/commands/wiki-ingest.md, plugin/commands/wiki-tune.md, plugin/commands/wiki-lint.md, plugin/commands/config.md, plugin/templates/CLAUDE.wiki.md, plugin/templates/wiki-schema.md, plugin/hooks/sessions-enforce.py, plugin/hooks/protocol_engine.py, plugin/hooks/session-start.py, plugin/hooks/shared_state.py, plugin/agents/context-gathering.md]
---

# LLM Wiki Feature

The LLM Wiki is an opt-in, in-repo knowledge base that Claude writes and maintains while the user curates source documents. It is the subsystem this very page lives in. The division of labour is deliberate: the user drops raw documents into `wiki/raw/` and asks questions; Claude turns those documents into structured, cross-linked pages under `wiki/pages/` (organized into per-category subdirectories), updates an index, and keeps an append-only operation log. It compounds over sessions because the pages persist in git, unlike conversation context. The feature is disabled by default (`wiki.enabled: false`) and is entirely prompt-and-template driven — there is no Python "wiki engine" — only seed files (created by the config flow when the feature is enabled), a one-line DAIC whitelist, and a single protocol pre_func reminder.

## Components and how they fit together

Three layers make up the feature:

1. **Behavioral template** — `CLAUDE.wiki.md`, deployed to the project root and wired into the project `CLAUDE.md` via a native `@CLAUDE.wiki.md` include when `wiki.enabled` is true (the same durable `@`-include delivery as `CLAUDE.tm.md`; the SessionStart hook + `config_update` self-heal it — see [Hooks System](pages/subsystems/hooks-system.md), h-durable-guidance-via-claude-md). A project-root `CLAUDE.md` and its `@`-imports survive `/compact`, unlike the former one-shot SessionStart injection. Defines operations, page organization (category subdirectories), page format, wiki-root-relative cross-reference conventions, the read+self-verify usage rule, and the `wiki/raw/` security policy. It ends with an `@wiki/schema.md` reference; because `CLAUDE.wiki.md` is itself `@`-imported, that ref recursively pulls the domain schema into context (2 hops from `CLAUDE.md`, within Claude Code's 4-hop import limit).
2. **Slash commands** — `wiki-ingest.md`, `wiki-tune.md`, `wiki-lint.md` (namespaced `/team-management:wiki-*`). These are pure prompt files (no code); each defines a workflow Claude executes when the command is invoked.
3. **Enforcement / setup touch points** — the config-flow wiki seeding (`/team-management:config`), the unconditional `wiki/` DAIC whitelist in `sessions-enforce.py`, and the `wiki_update_reminder` pre_func in the protocol engine.

## Setup (config-flow seeding)

Setup is part of the in-Claude-Code config flow. `/team-management:config` toggles `wiki.enabled` and, if `wiki/` is absent, offers to seed the structure (`plugin/commands/config.md`). Seeding is idempotent — it never clobbers existing user content.

The seeded structure is two directories (`wiki/raw/`, `wiki/pages/`) and four seed files:

- `wiki/index.md` — the page catalog.
- `wiki/log.md` — chronological operation record header.
- `wiki/raw/README.md` — drop-zone instructions pointing at `/team-management:wiki-ingest`.
- `wiki/schema.md` — the domain schema, seeded from the shipped template `plugin/templates/wiki-schema.md` (copied, not generated).

## Slash command workflows

### `/team-management:wiki-ingest <file>` (`wiki-ingest.md`)

Processes one local file into wiki pages. The fixed order (`wiki-ingest.md`): read `CLAUDE.wiki.md` → read `wiki/schema.md` → read `wiki/index.md` → read the source file → discuss takeaways with the user → choose the category (an existing one from schema.md `## Categories`, or propose a new one with user confirmation) → create `wiki/pages/<category>/<slug>.md` with frontmatter → update any existing pages referencing the same concepts → add an index entry under the category's heading → append to `wiki/log.md`. Rules: local paths only (no URL fetching), lowercase hyphenated slugs under a category directory (flat `pages/<slug>.md` still valid), schema-conformant tags, explicit contradiction notes when a source conflicts with an existing page, and no duplication of `CLAUDE.md`/`CLAUDE.tm.md` content.

### `/team-management:wiki-tune [section]` (`wiki-tune.md`)

Customizes `wiki/schema.md` via Q&A about domain, key entities, tag categories, ingest emphasis, and exclusions. Hard constraints: edits `wiki/schema.md` ONLY — never `CLAUDE.wiki.md` (package-managed); preserves un-targeted content; shows the planned diff before writing; keeps suggestions consistent with existing pages. Keep schema under 200 lines because it is auto-loaded every session via `@-ref`.

### `/team-management:wiki-lint` (`wiki-lint.md`)

Health-check. First counts `wiki/pages/*.md`; if more than 50, it delegates the whole lint to a `general-purpose` subagent to avoid reading every page into the main context. Nine checks (nested-aware — pages compared by normalized wiki-root-relative path): orphan pages (file not in index), missing pages (index entry with no file), broken internal links (resolved from the wiki root), missing frontmatter fields, stale content (`updated` > 90 days), tag inconsistency vs schema, index entries lacking descriptions, uncategorized (flat) pages (info), and category-consistency vs schema `## Categories` (info). The same slug under two categories is allowed (full-path addressing), not an error. Output is grouped Critical / Warning / Info; afterward it appends a `lint` summary line to `wiki/log.md` and offers to fix critical issues.

## Page format and conventions

Every page in `wiki/pages/` carries YAML frontmatter — `title`, `tags` (list), `created`, `updated`, `sources` (list of raw filenames) — followed by markdown (`CLAUDE.wiki.md`). Pages live under category subdirectories (`wiki/pages/<category>/<slug>.md`; flat legacy pages remain valid). Cross-references use **wiki-root-relative** markdown links `[Title](pages/<category>/slug.md)` (logical navigation/lint references, not always click-through on GitHub — an accepted trade-off). Index entries are `[Title](pages/<category>/slug.md) — one-line description` grouped under a `## <Category>` heading. Code is referenced by symbol/file name, never line numbers. Log entries are `## [YYYY-MM-DD] operation | title` (`CLAUDE.wiki.md`). The page-type vocabulary (summary / entity / topic / analysis) and tag conventions (lowercase-hyphenated, <30 unique tags) live in `wiki-schema.md` and are user-customizable via `/team-management:wiki-tune`.

## DAIC whitelist

`sessions-enforce.py` unconditionally whitelists the `wiki/` directory in the administrative-whitelist block, which (per the corrected hook execution order) runs BEFORE DAIC mode enforcement. The check resolves `<project_root>/wiki`, calls `file_path_resolved.relative_to(wiki_dir)`, and `sys.exit(0)` (allow) if the target is inside it. Because it operates on resolved paths it is symlink-safe. The practical effect: wiki edits are permitted in any DAIC mode — discussion, implementation, or documentation — so ingest/tune/lint work without first entering a protocol or implementation mode. See [DAIC Enforcement](pages/topics/daic-enforcement.md) for the surrounding whitelist/enforcement ordering.

## Documentation-step reminder (`wiki_update_reminder`)

`_func_wiki_update_reminder` (`protocol_engine.py`) is a protocol func wired as a `pre_func` on the `task` protocol's documentation step — and only there (`team-management/protocol-configs/task.json`, `"pre_funcs": ["wiki_update_reminder"]`). It guards in three layers, each returning `action: "skipped"` rather than failing:

1. No `team-management/config.json` → skip.
2. `config["wiki"]["enabled"]` is falsy (default `False`) → skip.
3. `wiki/` directory absent → skip.

Only when all three pass does it return `action: "reminder"` with an enriched capture prompt (m-wiki-nesting-read-verify-doc-reminder): WHAT to capture (architecture decisions / domain concepts / non-obvious patterns / protocol details / external integrations), WHERE (`wiki/pages/<category>/`, proposing a new category in schema.md if none fits), THEN update `wiki/index.md` under the category heading and append to `wiki/log.md`, reference code by symbol/file (not line numbers), avoid duplicating `CLAUDE.md`/`CLAUDE.tm.md`, and skip for pure refactors or bug fixes with no new durable knowledge. The whole body is wrapped in `try/except` that also returns a skip, so a malformed config can never break the documentation step. It is registered in the handler map at `protocol_engine.py` and described in `get_available_funcs`.

## Read + self-verify usage

When a `wiki/` exists it is the first place to look for project knowledge (m-wiki-nesting-read-verify-doc-reminder). `CLAUDE.wiki.md` now carries an ambient **Using the Wiki (Consult + Verify)** section: consult `wiki/index.md` → relevant pages before broad code search, follow each page's symbol/file code references to the real source, treat CODE as ground truth, and continuously self-verify — fix small/unambiguous drift in place (bump `updated:`), flag large/ambiguous drift. The `context-gathering` agent applies the same consult-first rule, gated on `wiki/` existence (no-op for non-wiki projects). This ambient read/verify guidance REPLACED the former reactive `## Update Guidance` section of `CLAUDE.wiki.md`; the "update the wiki while working" nudge now lives solely in the documentation-step reminder above, so it no longer sits in every wiki-enabled session's loaded context.

## Security: `wiki/raw/` git permanence

`wiki/raw/` is tracked in git, so any secret committed there lives in history permanently. `CLAUDE.wiki.md` enumerates what must never be placed there (API keys, tokens, connection strings, private keys, cookies, `.env` contents) and states plainly that there is NO automated secret scanning anywhere in wiki operations — the prose warning is the only defense. Remediation if a secret was committed: rotate the credential, `git filter-repo --path wiki/raw/<file> --invert-paths` to purge history, then force-push.

## Design decisions and rationale

- **No code engine, all prompt/template.** The entire feature is markdown commands + config-flow seeding + one whitelist line + one reminder func. Rationale: wiki maintenance is judgment work (what to summarize, how to cross-link), which the LLM does directly; a code engine would add surface area without doing the hard part.
- **Idempotent, never-clobber scaffolding.** Every seed write is `exists()`-guarded so re-installs and upgrades preserve user content — the same philosophy as `CLAUDE.tm.custom.md` (created once, never overwritten).
- **schema.md is user-owned, CLAUDE.wiki.md is package-owned.** `/team-management:wiki-tune` may edit only schema.md; the behavioral template is replaced on update. This keeps domain customization safe across upgrades while letting the framework evolve the operating rules.
- **Whitelist is unconditional and ordered before DAIC.** Knowledge capture is orthogonal to the discuss-then-implement gate, so wiki edits should never be blocked by DAIC mode. Placing the check in the pre-DAIC administrative whitelist achieves that.
- **Reminder is a soft skip, not a gate.** `wiki_update_reminder` never returns `success: False`; it only nudges. A disabled or absent wiki must not impede task completion.
- **Lint delegates above 50 pages.** Reading every page inline would blow the context budget on a mature wiki, so large-wiki lint runs in an isolated subagent.

## Gotchas

- **schema.md is template-seeded, not generated.** It is copied from the shipped template `plugin/templates/wiki-schema.md` during config-flow seeding (alongside index/log/raw-README), rather than generated from your sources — run `/team-management:wiki-tune` to customize it.
- **Keep schema.md small.** `CLAUDE.wiki.md` ends with an `@wiki/schema.md` reference intended to pull the schema into context each session; a bloated schema inflates every session's context — hence the <200-line (~4KB) ceiling, enforced only by convention and the `/team-management:wiki-tune` prompt, not by code.
- **`wiki_update_reminder` fires only on the `task` protocol's documentation step.** Other protocols (brainstorm, research, refactoring, optimize) have no wiki reminder wired. If you add a documentation-equivalent step elsewhere and want the nudge, you must add the pre_func to that JSON yourself.
- **The whitelist matches the literal `wiki/` directory at project root only.** A wiki nested elsewhere, or reached through a path that does not resolve under `<project_root>/wiki`, is not whitelisted and will be subject to normal DAIC enforcement.
- **`wiki.enabled` defaults to false and is checked independently of the directory's existence.** Both `config["wiki"]["enabled"]` true AND the `wiki/` directory present are required for the reminder to fire; a stray `wiki/` dir with the flag off (or vice versa) yields a silent skip.
- **No secret scanning exists at all.** The security guidance is documentation, not enforcement — there is no hook, no lint check, and no installer step that inspects `wiki/raw/` contents. Treat the warning as the sole safeguard.
- **Lint's stale/tag/orphan checks are LLM-evaluated, not deterministic.** `/team-management:wiki-lint` is a prompt, so results depend on the model correctly applying the checklist; it is not a parser with guaranteed coverage.
