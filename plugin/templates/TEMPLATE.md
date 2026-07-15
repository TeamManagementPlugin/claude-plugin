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
- [ ] Specific, measurable outcome
- [ ] Another concrete goal

## Context Files
<!-- Added by context-gathering agent or manually -->
- @service/file.py:123-145  # Specific lines
- @other/module.py          # Whole file  
- patterns/auth-flow        # Pattern reference

## Implementation Plan
<!-- Optional. Use for complex tasks with multiple phases. Bite-sized checkbox steps — each one a concrete, verifiable action. No placeholders: every step must name real files, functions, or commands. -->
<!-- Example:
- [ ] Write failing test in tests/test_auth.py::test_invalid_token asserting 401 response
- [ ] Watch test fail: `pytest tests/test_auth.py::test_invalid_token -v`
- [ ] Add token validation to auth/middleware.py:validate_request
- [ ] Watch test pass
- [ ] Refactor: extract validate_token_format() helper if reused
-->

## User Notes
<!-- Any specific notes or requirements from the developer -->

## Work Log
<!-- Updated as work progresses -->
- [YYYY-MM-DD] Started task, initial research