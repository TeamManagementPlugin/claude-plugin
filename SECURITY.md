# Security Policy

## Supported versions

team.management is pre-1.0 and ships as a single Claude Code plugin. Security fixes
land on the latest release only.

| Version | Supported |
| ------- | --------- |
| 0.4.x   | ✅        |
| < 0.4   | ❌        |

## Reporting a vulnerability

Please report security issues privately — do **not** open a public issue.

Use GitHub's private reporting on this repository: **Security → Report a
vulnerability**. It reaches the maintainers privately, with no email needed.

Include what you found, how to reproduce it, and the impact you expect. We'll
acknowledge your report, work with you on a fix, and credit you when it ships (unless
you'd rather stay anonymous). Please give us reasonable time to release a fix before
disclosing publicly.

## What to keep in mind

A few things about how the plugin handles trust and secrets:

- **The plugin runs local code.** Its Python hooks and MCP server run on your machine
  with your permissions, inside your Claude Code session. Install it the same way you'd
  install any tool that runs code — from a source you trust.
- **Provider tokens stay out of the repo.** API tokens live in the per-project
  `.claude/state/provider-tokens.json` file, which is git-ignored and owner-only
  (`0600`) and is a protected path the agent cannot read directly. The plugin never writes tokens
  to `config.json`, and they never enter the chat transcript. Task descriptions are also credential-filtered
  before they're passed to any external AI provider.
- **DAIC enforcement is a workflow guardrail, not a security sandbox.** The hooks are
  designed to keep an aligned workflow honest — block edits before discussion, keep work
  on the right branch. They are not an isolation boundary and should not be relied on to
  contain untrusted code or a hostile agent.
- **Never put secrets in `wiki/raw/`.** That directory is committed to git. A secret
  committed there lives in history until the history is rewritten. See the security note
  in `CLAUDE.wiki.md`.
