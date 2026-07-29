---
task: [prefix]-[descriptive-name]
branch: feature/[name]|fix/[name]|experiment/[name]|none
status: pending|in-progress|completed|blocked
created: YYYY-MM-DD
modules: [list of services/modules involved]
---

# [Human-Readable Title]

**Author:** [developer_name]

## Problem/Goal
[Clear description of what we're solving/building]

## Success Criteria
<!-- Stable IDs: number criteria SC-1, SC-2, ... Never renumber existing criteria - append new ones.
     While drafting during investigation you may mark an unresolved point with a
     [NEEDS ... CLARIFICATION: question] marker (canonical form: a space instead of the ellipsis; max 3,
     priority scope > security > UX > technical details). Every marker must be resolved before the task
     file is delivered - the engine rejects files that still contain one.
     Write criteria as verifiable OUTCOMES, not process:
     ✅ SC-1: config_update rejects an unknown dotted key with a schema error (test: test_config_update_unknown_key)
     ❌ SC-1: Refactor the config module   <- process, no observable end state, unverifiable -->
- [ ] SC-1: Specific, measurable outcome
- [ ] SC-2: Another concrete goal

## Context Files
<!-- Added by context-gathering agent or manually -->
- @service/file.py:123-145  # Specific lines
- @other/module.py          # Whole file  
- patterns/auth-flow        # Pattern reference

## Implementation Plan
<!-- Optional. Use for complex tasks with multiple phases. Bite-sized checkbox steps — each one a concrete, verifiable action. No placeholders: every step must name real files, functions, or commands. Number steps T1, T2, ... and tag each with the criteria it covers ([SC-1], [SC-1, SC-2]) so every SC maps to at least one step. -->
<!-- Example:
- [ ] T1 [SC-1]: Write failing test in tests/test_auth.py::test_invalid_token asserting 401 response
- [ ] T2 [SC-1]: Watch test fail: `pytest tests/test_auth.py::test_invalid_token -v`
- [ ] T3 [SC-1]: Add token validation to auth/middleware.py:validate_request
- [ ] T4 [SC-1]: Watch test pass
- [ ] T5 [SC-2]: Refactor: extract validate_token_format() helper if reused
-->

## User Notes
<!-- Any specific notes or requirements from the developer -->

## Work Log
<!-- Updated as work progresses -->
- [YYYY-MM-DD] Started task, initial research