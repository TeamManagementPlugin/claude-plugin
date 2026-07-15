# Wiki Schema

## Focus

Knowledge base for the **team-management framework's own internals** — the architecture
of this repository. It documents how the framework is built and why: DAIC enforcement,
the JSON-driven protocol engine, the hook system, the MCP server, AI-provider
integration, multi-provider issue tracking, the installer pipeline, and the LLM Wiki
feature itself.

This is engineering/architecture knowledge about the code — not end-user usage docs
(those live in docs/) and not workflow rules (those live in CLAUDE.tm.md).

## Page Types

- **subsystem** — A major framework component: protocol engine, hook system, MCP server,
  installer, AI-provider integration, issue-tracking providers. The backbone pages.
- **entity** — A specific named artifact: a single hook, an MCP tool, a config key, an
  agent, a state file, or a protocol.
- **decision** — An architecture or design decision with its rationale: why it is built
  this way, the trade-offs, and the alternatives that were rejected (ADR-style).
- **procedure** — A how-to or operational workflow: adding a new AI provider, authoring a
  custom protocol, the install/upgrade pipeline.
- **topic** — A cross-cutting theme synthesized across multiple subsystems: DAIC mode
  transitions, credential filtering, cross-platform/Windows compatibility.
- **summary** — Summary of a single source document placed in raw/.

## Categories

Wiki pages live in category subdirectories under `wiki/pages/<category>/`. A category is a
top-level grouping. Because this wiki documents the framework's own internals, the categories
mirror the architectural layers — the same groupings already used as headings in `wiki/index.md`:

- **overview** — the navigation hub and whole-system architecture map.
- **subsystems** — the major framework components: hook system, protocol engine, MCP server,
  plugin runtime, AI-provider integration, issue-tracking providers, the LLM Wiki.
- **protocols** — the workflow protocols (task / brainstorm / research / refactoring /
  optimize + optimize-unattended) and their shared engine mechanics.
- **topics** — cross-cutting themes synthesized across subsystems: DAIC enforcement,
  context preservation.
- **entities** — specific named artifacts: agents, state files, the configuration schema.
- **procedures** — operational / how-to workflows: the completion-and-git flow.

Place each page in the best-fitting category (its directory is created lazily on first use).
A page that fits none may sit flat in `wiki/pages/` (backward-compatible), but prefer a
category. The same slug may appear in different categories — pages are addressed by full path.
`wiki/index.md` mirrors this structure with one `## <Category>` heading per category.

## Page Format Example

```yaml
---
title: Protocol Engine
tags: [architecture, protocols, mcp]
created: 2026-05-31
updated: 2026-05-31
sources: [protocol_engine.py]
---
```

## Tag Conventions

- Lowercase, hyphen-separated: `ai-providers`, `issue-tracking`.
- Prefer **broad category tags**; add a narrow tag only when a page genuinely needs it.
- Seed vocabulary (extend deliberately, keep the total under 30):
  `architecture`, `protocols`, `hooks`, `daic`, `mcp`, `installer`, `ai-providers`,
  `issue-tracking`, `git`, `config`, `agents`, `wiki`, `security`, `cross-platform`,
  `testing`.

## Ingest Focus

When ingesting sources, emphasize:
- **Decisions and their rationale** — what was chosen, the trade-offs, rejected alternatives.
- **Named entities / glossary** — hooks, MCP tools, config keys, agents, protocols, state files.
- **Procedures / how-to** — operational and extension workflows, step by step.
- **Gotchas and pitfalls** — non-obvious bugs, edge cases, ordering constraints, limitations.

Also capture relationships between components and anything that contradicts or extends
existing wiki pages.

## Excluded Topics

- Workflow and collaboration rules already defined in CLAUDE.md / CLAUDE.tm.md — do not
  duplicate them here.
- Secrets of any kind (see the security note in CLAUDE.wiki.md).
- Marketing-style feature copy from README.md — capture the underlying design instead.
