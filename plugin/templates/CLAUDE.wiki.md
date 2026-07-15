# LLM Wiki

This file defines how Claude maintains the project wiki. The wiki is a persistent, compounding knowledge base — Claude writes and maintains all pages, the user curates sources and asks questions.

## Getting Started

No pages yet? Drop a document in `wiki/raw/` and run `/team-management:wiki-ingest <filename>` to create your first wiki pages. Or run `/team-management:wiki-tune` to customize what this wiki should focus on.

## Security: What Not to Put in wiki/raw/

wiki/raw/ is tracked in git. Do not place documents containing:
- API keys, bearer tokens, OAuth credentials
- Database connection strings, private keys (PEM/SSH)
- Session cookies, webhook secrets, `.env` file contents
- Passwords or any authentication material

**Why this matters:** `git rm` does not remove a file from history. Once committed, a secret lives in the repository permanently unless `git filter-repo` is run. There is no automated secret scanning in wiki operations — this warning is the only defense.

**If a secret was committed:** (1) Rotate the credential immediately, (2) run `git filter-repo --path wiki/raw/<file> --invert-paths` to purge from history, (3) force-push if the repository has remotes.

## Operations

### Ingest (`/team-management:wiki-ingest`)
Process a local file from wiki/raw/ into wiki pages. Workflow:
1. Read wiki/schema.md for domain context and the category list
2. Read wiki/index.md for current catalog
3. Read the source file
4. Discuss key takeaways with the user
5. Choose the category (subdirectory) the page belongs to — an existing one from wiki/schema.md, or propose a new one and confirm with the user
6. Create summary page in wiki/pages/<category>/<slug>.md with YAML frontmatter
7. Update related existing pages if they reference the same concepts
8. Add entry to wiki/index.md under the category's heading
9. Append to wiki/log.md

### Query
When answering questions about wiki content:
1. Read wiki/index.md to find relevant pages
2. Read the relevant pages
3. Synthesize an answer with citations to specific pages
4. If the answer is valuable as a reusable artifact, offer to save it as a new wiki page

See **Using the Wiki (Consult + Verify)** below — the wiki is the first place to look for project knowledge, and every read is also a freshness check.

### Lint (`/team-management:wiki-lint`)
Periodic health check. Finds: orphan pages (not in index), broken internal links, missing frontmatter fields, stale content, contradictions between pages. Handles nested category directories under wiki/pages/.

## Page Organization

Wiki pages live in category subdirectories under `wiki/pages/`:

```
wiki/pages/<category>/<slug>.md
```

- A **category** is a top-level grouping — a subsystem, domain area, or theme. The known categories are listed in `wiki/schema.md` (`## Categories`).
- At ingest, place each page in the best-fitting existing category. If none fits, propose a new category to the user; on confirmation, create the directory and add the category to `wiki/schema.md`.
- Category directories are created **lazily** — the first page in a category creates its directory.
- **Backward compatible:** pages written flat at `wiki/pages/<slug>.md` (no category) remain valid. Prefer categories for new pages; migrate a flat page into a category opportunistically when you next touch it.
- The **same slug may appear in different categories** (e.g. `pages/api/auth.md` and `pages/security/auth.md`) — pages are addressed by full path, so this is unambiguous. Do not rely on a bare slug being unique.
- `wiki/index.md` **mirrors this structure**: group entries under a `## <Category>` heading (one per category) so the index is browsable by category. A page with no category is listed under a fallback heading such as `## Uncategorized`.

## Page Format

Every wiki page uses this frontmatter:

```yaml
---
title: Page Title
tags: [tag-one, tag-two]
created: YYYY-MM-DD
updated: YYYY-MM-DD
sources: [source-filename.md]
---
```

Body uses standard markdown. Reference project code by **symbol and file name** (e.g. `resolve_provider_token in shared_state.py`), never by line number — line numbers drift and make pages stale. Internal cross-references use the wiki-root-relative link convention below.

## Cross-Reference Conventions

All wiki links are written **relative to the wiki root** (the directory containing `index.md`), so they are unambiguous to write and to lint:

- Link to another page: `[Entity Name](pages/<category>/<slug>.md)`
- Link from index.md to a page: `[Page Title](pages/<category>/<slug>.md) — one-line description`
- Log entries: `## [YYYY-MM-DD] operation | title`

These are **logical references** for navigation and linting. Because they are wiki-root-relative rather than file-relative, a link inside a nested page is not always click-through-resolvable on GitHub — the wiki is navigated by reading the index and following paths, not by click-through. (Accepted trade-off, chosen for LLM navigation and simple linting.)

## Using the Wiki (Consult + Verify)

When the wiki exists, it is the **first place to look** for project knowledge — and every read doubles as a freshness check.

**Consult first.** Before answering a question about the project/domain, or doing a broad code search for how something works, read `wiki/index.md` and the relevant pages. The wiki is the fast path to accumulated knowledge; use it before re-deriving from scratch.

**Follow references to code.** Wiki pages cite code by symbol/file. When a page's claim informs your answer or your work, open the referenced source and confirm the page still matches reality — do not trust the page blindly.

**Code is ground truth.** If a page contradicts the code or is out of date:
- **Small, unambiguous drift** (a renamed symbol, a moved file, a changed default) → fix the page in place, bump its `updated:` date, and — if the change is notable — append a line to `wiki/log.md`. `wiki/` is whitelisted, so these edits are allowed in any mode.
- **Large, ambiguous, or out-of-scope drift** → do not derail the current work. Flag it to the user and offer to update, or leave it for `/team-management:wiki-lint`.

The principle: after you rely on a page, leave it at least as accurate as you found it. This continuous self-verification is what keeps the wiki trustworthy as it compounds.

## Branch Strategy

Wiki commits land on the current git branch. For teams using merge requests, consider ingesting sources on the default branch (main/master) to avoid wiki noise in feature branch MR diffs.

## Schema Size

wiki/schema.md is auto-loaded at session start via @-ref. Keep it under 200 lines (~4KB) to avoid excessive context consumption. If your domain requires a larger ontology, keep schema.md as a high-level summary and put detailed taxonomies in wiki/pages/.

## Domain Rules

@wiki/schema.md
