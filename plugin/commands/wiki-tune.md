---
description: Tune wiki schema and conventions interactively
argument-hint: "[section-to-tune]"
---

# Wiki Tune

Customize `wiki/schema.md` through interactive Q&A.

## Workflow

1. Read the current `wiki/schema.md`
2. If an argument specifies a section (e.g., "tags", "focus", "page-types"), focus on that section
3. Ask the user questions about their domain and what the wiki should track:
   - What is this project/domain about?
   - What entities or concepts are most important?
   - What tag categories make sense?
   - What should be emphasized during ingest?
   - What topics should be excluded?
4. Update `wiki/schema.md` based on the user's answers
5. Keep schema.md under 200 lines (auto-loaded at session start via @-ref)

## Rules

- This modifies `wiki/schema.md` only — never edit `CLAUDE.wiki.md` (that is package-managed)
- Preserve any existing content the user hasn't explicitly asked to change
- Show the user what you plan to change before writing
- If wiki/pages/ already has content, suggest tag and convention changes that are consistent with existing pages
