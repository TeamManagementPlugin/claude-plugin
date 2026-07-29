# Code Review Sub-Protocol

CRITICAL: Code review is REQUIRED before task completion. Task cannot be completed without passing code review.

## 0. READ Before Proceeding (MANDATORY)

**READ `@knowledge/receiving-feedback.md` before proceeding with this step. Do not continue without reading it.** The anti-sycophancy response pattern, forbidden/preferred phrases, and external-reviewer skepticism rules apply to every review finding you process below.

**READ the verification section of `@knowledge/debugging.md` before advancing.** The Gate Function (identify → run → read → verify → claim) is the contract you must satisfy before calling `protocol_advance` on this step. «Review passed» is not a claim until you have run the review command, read its output, and confirmed zero critical issues.

## 1. Spec Compliance Review (FIRST, BEFORE CODE REVIEW)

**Run this before Section 2. This is a separate, read-only audit that catches well-written code that doesn't match the spec.**

The `code-review` step's `pre_funcs` include `require_spec_review_passed`, which emits a `[BLOCKED]` reminder on entry until you record the sentinel. The `post_funcs` re-check it on advance, so advance from code-review → documentation is **structurally blocked** without the sentinel. Dispatch the agent now:

1. **Launch the `spec-compliance-reviewer` Task agent** (`subagent_type: "spec-compliance-reviewer"` — its system prompt + allowed tools are loaded from `.claude/agents/spec-compliance-reviewer.md`). The agent is READ-ONLY — it cannot write files.
   - `prompt`: provide the task file path (e.g. `team-management/tasks/<task-name>.md`) and tell the agent to compare the git diff (working tree + staging) against the `## Success Criteria` bullets.
   - **Optimize protocol note** (`optimize` / `optimize-unattended`): the working tree is clean at this step — every hypothesis was committed during experimentation — so "working tree + staging" yields an empty diff. Tell the agent to review the cumulative diff **`optimize.baseline_commit..optimize.best_commit`** instead (both SHAs live in the task frontmatter / `optimize-state.json`).
   - Optional pre-dispatch checkpoint: `mcp__plugin_team-management_tm__protocol_save_note(note="SPEC_REVIEW: DISPATCHED")` — helps a resumed session (after auto-compact) distinguish «fresh entry, never dispatched» from «mid-review, waiting on result».
2. **Read the agent's verdict** (`# Spec Compliance Review` → `## Verdict` → PASS / FAIL).
3. **If FAIL** → fix gaps (implement missing criteria) and/or remove scope creep (delete unneeded changes); then re-dispatch the agent. Repeat until PASS. Do NOT ignore the verdict — `require_spec_review_passed` blocks advance without the sentinel.
4. **On PASS** → call `mcp__plugin_team-management_tm__protocol_save_note(note="SPEC_REVIEW: PASSED")` verbatim. Exact string, exact casing, exact colon-space. Any deviation defeats the sentinel check.
5. **Only now** proceed to Section 2. Do NOT launch the Claude `code-review` agent before the spec-compliance agent reports PASS.

**After in-step code edits — re-save the sentinel.** The structural staleness check catches sentinels invalidated by `protocol_goto` backward, but it does NOT catch edits made in-place within the code-review step (which runs in `implementation` mode). If you fix anything in the code after the spec-compliance agent returned PASS — whether from a code-review agent finding, a codex-cli warning, or a user request — the PASS verdict is stale. Re-dispatch the `spec-compliance-reviewer` against the updated diff and call `protocol_save_note("SPEC_REVIEW: PASSED")` again. Treat this as a discipline rule; a future task may replace it with a diff-hash-based structural check.

**Why two stages?** Section 1 (this one) asks «does the diff match what the task promised?». Section 2 asks «is the diff correct, secure, and consistent with house style?». Either alone misses the other failure mode.

## 2. Run Code Review Agent + AI Providers IN PARALLEL (MANDATORY)

**BEFORE launching any agent — do these two things in order:**

1. **Read `pre_funcs_results`** from the most recent `protocol_advance` response. Find the entry where `func == "resolve_ai_providers"`. Its `providers` field is the list of configured AI providers — e.g. `["codex"]`, `["codex", "agy"]`, or `[]`. Its `instructions` field spells out the exact `subagent_type` and a ready-to-use `prompt` for each.
2. **Compose ONE message containing N+1 `Task` tool calls**: the mandatory Claude `code-review` agent PLUS one `Task` per listed provider. Use `subagent_type: "code-review"` for the Claude agent, `subagent_type: "codex-cli"` for codex, `subagent_type: "agy-cli"` for agy. The wrapper agents load their system prompts from `.claude/agents/<name>.md` automatically.

**Optimize protocol note** (`optimize` / `optimize-unattended`): the working tree is clean at this step, so the Claude `code-review` agent's default `git diff HEAD` is empty. In the agent's `prompt`, tell it to review the cumulative diff **`optimize.baseline_commit..optimize.best_commit`** (both SHAs are in the task frontmatter / `optimize-state.json`). Known limitation: the codex/agy provider wrappers still run against the uncommitted (empty) tree, so their optimize-run output is low-signal — treat it as advisory only, never blocking, on these protocols (a follow-up will make the provider prompts range-aware).

**Anti-pattern (what NOT to do):**
- ❌ Launch code-review alone, read its verdict, then decide whether to run providers.
- ❌ Launch code-review in one message, providers in a follow-up message.
- ❌ Read `pre_funcs_results`, see the provider list, and still forget to include them because the Claude agent felt «complete» on its own.
- ❌ Re-run the review after a fix and dispatch only code-review «since the providers already ran». Every round must include every listed provider.

**Correct pattern:**
- ✅ In the **SAME** message: `Agent(subagent_type="code-review", ...)` + `Agent(subagent_type="codex-cli", ...)` + `Agent(subagent_type="agy-cli", ...)` — one call per listed provider, all in parallel.
- ✅ Every re-review round repeats this parallel dispatch, even if only one agent flagged an issue the previous time.

**Fallback (provider list empty):** if `providers == []` (no config or all disabled), run only the Claude `code-review` agent. That is the supported single-agent path.

Aggregate findings from the `code-review` agent and every provider wrapper with **equal weight**. If a wrapper reports `<provider> review unavailable: …`, treat it as a non-blocking failure and proceed with the remaining results.

## 3. Review Criteria

The code review must:
- PASS: Review all implemented code for security/quality issues
- PASS: Check consistency with existing project patterns
- PASS: Verify no critical security vulnerabilities
- FAIL: If any critical issues found -> fix them before proceeding
- ADVISORY (non-blocking): code cleanliness/maintainability is separate from this correctness/security review. On large diffs or when touching messy areas, consider running `/team-management:clean-check <paths>` (or Tasking the `code-cleanliness` agent) and folding any high-value fixes into this step. No config gate — judgement call.

**If code review FAILS**: Fix all identified issues and re-run code review until it passes.

## 4. Document Results (MANDATORY)

After code review PASSES, append results to the task work log:

```markdown
# Code Review: [Title from review]

## Summary
[Summary text]

## 🔴 Critical Issues (0)
[Issues or "None found"]

## 🟡 Warnings (N)
[Warnings if any]

## 🟢 Notes (N)
[Notes if any]
```

**IMPORTANT**:
- Use exact format `# Code Review:` (single `#`) as the heading
- This enables automatic code review documentation in MRs/PRs
- Only append the FINAL passing review (not intermediate failed reviews)

## 5. Warning Enforcement Check

Determine the enforcement mode — **through the MCP tool, not the config file**:

1. Call `mcp__plugin_team-management_tm__config_code_review_enforcement` with the current task file content. This is the required first step — do NOT read `team-management/config.json` (via Read, `cat`, or any other route) unless the error-only fallback below applies.
2. On `success: true`, check `analysis.has_warnings`:
   - If `false`: No warnings, proceed
   - If warnings exist, check `enforce_warnings`:
     - **Strict Mode** (`true`): Fix ALL warnings automatically (same as critical issues), then re-run code review. Repeat until clean pass (zero critical issues AND zero warnings).
     - **Relaxed Mode** (`false`): Report warnings, continue automatically

**Error-only fallback**: ONLY if the tool call itself fails (tool not registered / MCP server down) or returns `success: false`, read the single `code_review.enforce_warnings` key from `team-management/config.json` (default: false). Extract just that key — do not print or dump the rest of the file into the conversation. On this path `analysis` is unavailable — determine warning presence from the final code-review results you already collected in Section 2, then apply the same Strict/Relaxed logic.

**Note**: Critical issues always block completion regardless of this setting.

**Strict mode behavior**: In strict mode, warnings are treated the same as errors — they must be fixed automatically by the AI and the code review re-run until it passes clean. The user is NOT asked to decide on warnings. This ensures consistent code quality without manual intervention.

## 6. Advance Summary Discipline

When you call `protocol_advance`, the `summary` must evidence that review passed — not predict it. Concrete claims only.

**This is now structurally enforced.** The `check_completion_evidence` post_func parses `summary` and blocks advance unless it contains one of: a fenced command-output block ( \`\`\` ... \`\`\` ), `N/N passed` or `N/N tests`, an `exit 0` / `exit code: 0` token, check-marks with counts, or the literal keyword `tests passed` / `tests passing`. Prose predictions («looks good», «should pass», «LGTM») are rejected.

**Good:**
- «code-review agent + codex-cli + agy-cli ran in parallel. Final pass: 0 critical, 0 warnings. Findings appended to work log under `# Code Review: <title>`.»

**Bad (red flags from `@knowledge/debugging.md`):**
- «Review looks good.» «Probably no issues.» «LGTM.» — predictions, not verification.

**Escape hatch** for steps that genuinely cannot be verified by running a review (rare here, but possible for behavioral-content-only changes when no reviewer tooling applies):
- `no-verification-applicable: documentation-only`

Use the canonical token verbatim after the colon — any suffix is flagged as anomalous in the audit log. Canonical reasons are enumerated in `@knowledge/debugging.md`.

## Notes

- **Two-stage structure**: Section 1 is spec-compliance audit (does the diff match the task?). Section 2 is code-quality review (is the diff correct/secure/consistent?). Section 1 runs FIRST; Section 2 runs AFTER the `SPEC_REVIEW: PASSED` sentinel is recorded.
- Documentation agents (service-documentation, logging) run in the separate **documentation** step, not here.
- Push-back on reviewer findings is welcome when the reviewer is wrong (Codex/agy/humans/spec-compliance-reviewer can hallucinate file paths or miss context). Follow the push-back-with-evidence pattern in `@knowledge/receiving-feedback.md`.
