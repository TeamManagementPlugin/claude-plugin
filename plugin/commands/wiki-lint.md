---
description: Health-check the wiki for orphans, broken links, and quality issues
argument-hint: ""
---

# Wiki Lint

Audit wiki/pages/ for structural and content quality issues.

## Scale Check

First, count the number of .md files in wiki/pages/:

```bash
find wiki/pages -name "*.md" | wc -l
```

**If more than 50 pages:** Delegate this entire lint operation to the Agent tool (subagent_type: "general-purpose") with the prompt below. This prevents context overflow from reading all pages inline.

**If 50 or fewer pages:** Perform the lint inline.

## Checks

Pages may be nested in category subdirectories (`wiki/pages/<category>/<slug>.md`) or flat (`wiki/pages/<slug>.md`, legacy). `find wiki/pages -name "*.md"` already recurses. Normalize every page and link target to its **wiki-root-relative** path (e.g. `pages/<category>/<slug>.md`) before comparing — links are written relative to the wiki root, not to the page's own directory.

1. **Orphan pages** — files under wiki/pages/ (any depth) not listed in wiki/index.md (compare normalized wiki-root-relative paths, not bare slugs)
2. **Missing pages** — entries in wiki/index.md pointing to files that don't exist
3. **Broken internal links** — markdown links in pages pointing to non-existent pages; resolve `pages/...` link targets from the wiki root
4. **Missing frontmatter** — pages lacking required YAML fields (title, tags, created, sources)
5. **Stale content** — pages whose `updated` date is more than 90 days old (flag for review)
6. **Tag inconsistency** — tags used in pages but not defined in schema.md conventions
7. **Index completeness** — pages that exist but have no description in the index
8. **Uncategorized pages** (info) — pages sitting flat in wiki/pages/ rather than under a category; valid but suggest moving into a category
9. **Category consistency** (info) — category directories under wiki/pages/ not listed in schema.md `## Categories`, or schema categories with no pages yet

Note: the same slug under two different categories (`pages/api/auth.md` and `pages/security/auth.md`) is **allowed** — pages are addressed by full path. Do not report a duplicate-basename collision as an error; at most note it as info if it looks unintentional.

## Output Format

Report findings grouped by severity:

```
## Critical
- [broken-link] pages/architecture/auth-flow.md → pages/architecture/nonexistent.md (line 15)

## Warning
- [orphan] pages/domain/old-notes.md — not in index.md
- [stale] pages/architecture/api-design.md — last updated 2025-11-01

## Info
- [tag] "microservice" used in 3 pages but not in schema.md tag conventions
- [uncategorized] pages/loose-note.md — sitting flat; consider moving under a category
- [category] pages/legacy/ not listed in schema.md ## Categories
```

## Post-Lint

After reporting, append to wiki/log.md:
```
## [<date>] lint | <N> critical, <N> warning, <N> info
```

Offer to fix critical issues (add missing pages to index, repair broken link paths — remove a link only if its target is truly gone) if the user agrees.
