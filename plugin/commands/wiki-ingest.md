---
description: Ingest a local file into the wiki knowledge base
argument-hint: "<path-to-file>"
---

# Wiki Ingest

Process a local file into structured wiki pages.

## Input

The argument is a path to a file to ingest. The file should be in `wiki/raw/` or will be referenced from there.

## Workflow

1. Read `CLAUDE.wiki.md` for wiki rules and page format
2. Read `wiki/schema.md` for domain focus, conventions, and the category list (`## Categories`)
3. Read `wiki/index.md` for the current page catalog
4. Read the source file specified in the argument
5. Discuss the key takeaways with the user — what's important, what to emphasize
6. Choose the category (subdirectory under `wiki/pages/`) for the page — an existing one from `wiki/schema.md`, or propose a new category and confirm with the user before creating it
7. Create a summary page at `wiki/pages/<category>/<slug>.md` with proper YAML frontmatter:
   ```yaml
   ---
   title: <descriptive title>
   tags: [<relevant-tags>]
   created: <today's date>
   updated: <today's date>
   sources: [<source-filename>]
   ---
   ```
8. Check existing pages under `wiki/pages/` — update any that reference the same concepts with new cross-references or revised information
9. Add an entry to `wiki/index.md` under the category's heading: `- [Page Title](pages/<category>/<slug>.md) — one-line description`
10. Append to `wiki/log.md`: `## [<date>] ingest | <source filename>`

## Rules

- Accept only local file paths — do not fetch URLs
- Use wiki-root-relative markdown links for cross-references: `[Title](pages/<category>/slug.md)`
- Page slugs are lowercase, hyphen-separated: `authentication-flow.md`; the slug lives under a category directory (`pages/<category>/<slug>.md`). Flat `pages/<slug>.md` remains valid but prefer a category.
- Tags must follow conventions in wiki/schema.md
- If the source contradicts existing wiki pages, note the contradiction explicitly in the affected pages
- Do not ingest content that duplicates CLAUDE.md or CLAUDE.tm.md
