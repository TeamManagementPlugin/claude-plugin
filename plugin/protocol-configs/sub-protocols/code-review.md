# Code Review Sub-Protocol

CRITICAL: Code review is REQUIRED before task completion. Task cannot be completed without passing code review.

## 0. READ Before Proceeding (MANDATORY)

**READ `@knowledge/receiving-feedback.md` before proceeding with this step. Do not continue without reading it.** The anti-sycophancy response pattern, forbidden/preferred phrases, and external-reviewer skepticism rules apply to every review finding you process below.

**READ the verification section of `@knowledge/debugging.md` before advancing.** The Gate Function (identify → run → read → verify → claim) is the contract you must satisfy before calling `protocol_advance` on this step. «Review passed» is not a claim until you have run the review command, read its output, and confirmed zero critical issues.

## 1. Order of This Step

1. **Code-review rounds first** (§2–§5): dispatch every reviewer, confirm and route the findings, record the round. Repeat only while a round leaves a confirmed blocking finding that was fixed in this step.
2. **Spec-compliance audit last** (§6): one audit of the final tree, after the last clean round.
3. **Advance** (§7).

Both stages are bound to the code they saw. `protocol_save_note` stamps the `REVIEW_ROUND:` and `SPEC_REVIEW: PASSED` notes with a hash of the working tree (code only — `team-management/`, `.claude/` and `.agents/` are excluded, so work-log edits never count). The post_funcs `verify_review_round` and `require_spec_review_passed` accept a note only while the tree still matches it: an unchanged tree is never reviewed twice, and a changed tree never passes on an old review. The hash anchors on `HEAD`, so a repository with no commits cannot pass either gate — make an initial commit first.

## 2. Run Code Review Agent + AI Providers + Fired Specialists IN PARALLEL (MANDATORY)

**BEFORE launching any agent — do these two things in order:**

1. **Read `pre_funcs_results`** from the most recent `protocol_advance` response. Find TWO entries:
   - `func == "resolve_ai_providers"` — its `providers` field is the list of configured AI providers (e.g. `["codex"]`, `["codex", "agy"]`, or `[]`); its `instructions` field spells out the exact `subagent_type` and a ready-to-use `prompt` for each.
   - `func == "classify_review_dispatch"` — the deterministic diff classifier (CR-2 gate). Its `dispatch` field carries per-specialist decisions: `dispatch["testing-specialist"].fire`, `dispatch["code-cleanliness"].fire` (first round, this section) and `dispatch["red-team"].fire` (second echelon, §2a). **If this entry is ABSENT** (optimize protocols, or a custom fork with the old pre_funcs list), skip all specialist dispatch — run only the Claude `code-review` agent + providers. The classifier's decisions are binding at entry: do NOT dispatch a specialist it skipped "just in case", and do NOT skip one it fired. The ONLY sanctioned addition is the deterministic add-only re-measurement on re-review rounds (below) — judgement never overrides the gate, re-measurement with the classifier's own commands may.
2. **Compose ONE message containing all first-round `Task` tool calls**: the mandatory Claude `code-review` agent PLUS one `Task` per listed provider PLUS `testing-specialist` if fired PLUS `code-cleanliness` if fired. Use `subagent_type: "code-review"` for the Claude agent, `subagent_type: "codex-cli"` for codex, `subagent_type: "agy-cli"` for agy, `subagent_type: "testing-specialist"` / `subagent_type: "code-cleanliness"` for the fired specialists. The wrapper agents load their system prompts from `.claude/agents/<name>.md` automatically. **Every specialist prompt must carry the COMPLETE changed-file set — tracked changes AND untracked files — plus the task file path.** The classifier counts untracked lines toward the gate, but the agents' default `git diff HEAD` does not show untracked files; an unlisted untracked file that fired the gate would be invisible to the very reviewer it summoned. `red-team` is NOT part of this message — it is the second echelon (§2a) and needs the merged round-1 findings as input.

**Optimize protocol note** (`optimize` / `optimize-unattended`): the working tree is clean at this step, so the Claude `code-review` agent's default `git diff HEAD` is empty. In the agent's `prompt`, tell it to review the cumulative diff **`optimize.baseline_commit..optimize.best_commit`** (both SHAs are in the task frontmatter / `optimize-state.json`). Known limitation: the codex/agy provider wrappers still run against the uncommitted (empty) tree, so their optimize-run output is low-signal — treat it as advisory only, never blocking, on these protocols (a follow-up will make the provider prompts range-aware).

**Anti-pattern (what NOT to do):**
- ❌ Launch code-review alone, read its verdict, then decide whether to run providers.
- ❌ Launch code-review in one message, providers in a follow-up message.
- ❌ Read `pre_funcs_results`, see the provider list, and still forget to include them because the Claude agent felt «complete» on its own.
- ❌ Re-run the review after a fix and dispatch only code-review «since the providers already ran». Every round must include every listed provider AND every fired specialist.
- ❌ Dispatch a specialist the classifier skipped «just in case» — the gate exists to control cost; overriding it defeats the telemetry.
- ❌ Include `red-team` in the first-round message — it has no merged findings to work on yet (§2a).

**Correct pattern:**
- ✅ In the **SAME** message: `Agent(subagent_type="code-review", ...)` + one call per listed provider + one call per fired first-round specialist (`testing-specialist`, `code-cleanliness`) — all in parallel.
- ✅ Every re-review round repeats this parallel dispatch, even if only one agent flagged an issue the previous time.

**Re-review rounds carry the history.** A fresh reader with no memory always finds a new weakness — often one the previous fix created — so a re-review round never starts from zero. Every reviewer prompt in a re-review round (agents AND providers) carries:
- the three-level severity scale, verbatim from the `### Severity Levels` section of `agents/code-review.md`;
- a `## Previous rounds` block listing every earlier finding with what was done about it — fixed (and how), not confirmed (and why), or recorded as a Note — plus the files the fix touched.

Write both into each prompt itself, never as a path to a file outside the repository: agy can read only the repository, and a reviewer told to read a file it cannot open returns nothing. If agy replies that the prompt asks it to read a path outside the repository, re-dispatch agy with that content inline.

The reader's job on a re-review is to check that those findings are closed and that the fix broke nothing. It does not reopen settled ground: a finding already recorded as not confirmed or as a Note is not raised again unless the fix changed the code it cites.

**Re-review rounds after in-step fixes — deterministic add-only re-measurement.** The classifier runs at step ENTRY; in-step fixes can grow the diff across a threshold it measured below. Before each re-review round that follows code edits, re-measure DIFF_LINES with the classifier's own commands (`git diff HEAD --numstat --no-renames` added+deleted, plus line counts of `git ls-files --others --exclude-standard` files; the engine adds `-z` for verbatim NUL-delimited paths — plain output is equivalent for ordinary filenames) and compare against the same thresholds (≥50 specialists / >200 red-team). **Drop every row whose path lies under `team-management/`, `.claude/` or `.agents/` before comparing** — the engine excludes the framework's own working tree from both measurement sources, so an unfiltered hand re-measurement over-counts and can escalate on task-file narrative alone; the entry decision's `framework_lines_excluded` in `pre_funcs_results` reports how many lines the engine dropped. A threshold newly crossed UPWARD adds the corresponding specialist to that round — record `protocol_save_note("DISPATCH: <agent> fired (in-step growth)")` — provided its config gate is not `disabled-by-config`. This escalation is ADD-ONLY: entry decisions are the floor, never revoked mid-step, and shrinking below a threshold never removes an already-fired specialist.

**Fallback (provider list empty):** if `providers == []` (no config or all disabled), run the Claude `code-review` agent plus any fired specialists. With no classifier entry either, the Claude `code-review` agent alone is the supported single-agent path.

Merge the findings from the `code-review` agent, every provider wrapper, and every fired specialist into one list. Every source enters at **equal weight** — as a claim; §3 decides which claims are real. If a wrapper reports `<provider> review unavailable: …`, treat it as a non-blocking failure and proceed with the remaining results.

## 2a. Second Echelon: red-team (AFTER round 1 merges)

The `red-team` agent reviews what the FIRST ROUND MISSED, so it runs sequentially, after the round-1 results (code-review + providers + specialists) are aggregated:

1. **Decide whether it fires.** Red-team runs when EITHER: the classifier said so (`dispatch["red-team"].fire` — the >200-line size trigger), OR any **Critical** finding appeared in the merged round-1 results (whatever the diff size). Two overriding skips: if `dispatch["red-team"].reason == "disabled-by-config"`, the explicit `code_review.red_team_enabled: false` opt-out wins over BOTH triggers — never dispatch red-team; and if the `classify_review_dispatch` entry is absent from `pre_funcs_results` entirely (optimize protocols, custom forks, a direct `protocol_goto` re-entry), skip this section like the rest of the specialist dispatch. If no trigger holds — skip this section.
2. **On the CRITICAL-escalation path** (fired because of a Critical while the classifier's size trigger said skip), record the canonical note verbatim first: `mcp__plugin_team-management_tm__protocol_save_note(note="DISPATCH: red-team fired (critical-finding)")` — the classifier's JSONL cost log only captured the size decision; this note is the telemetry for the escalation.
3. **Launch ONE `Task` with `subagent_type: "red-team"`.** The `prompt` must carry: the MERGED round-1 findings (markdown — strip each `## Machine-Readable Findings` block first), the changed files / line ranges, and the task file path.
4. **Merge its findings into the final review at equal weight.** A red-team finding is confirmed and routed like any other (§3).
5. **Run at most ONCE per code-review step entry.** If it already ran this step, do not re-dispatch on later fix rounds — its input (the round-1 findings landscape) does not meaningfully change within one step. A later round that surfaces the step's FIRST Critical when red-team has not yet run fires it per rule 1.

## 3. Confirm and Route Findings

**A reader's finding is a claim, not a verdict.** Do not fix anything on a reviewer's word alone.

1. **Confirm it in the code yourself.** Open the cited file and lines and check the claimed behaviour — trace the input that triggers it, or run a quick test. A finding you cannot confirm, or that the scale puts lower (the `### Severity Levels` section of `agents/code-review.md`), is recorded as such with the reason — `not confirmed: <why>` or `Note, not Warning: <why>` — and drives no change.
2. **Decide what blocks.** A confirmed Critical always blocks. A confirmed Warning blocks only in strict mode (§5). Notes never block and never trigger a re-review.
3. **Route each blocking finding by fix depth:**
   - **SIMPLE** — one file, no logic change (a typo, a wrong constant, a missing test for behaviour that is already correct): fix it in this step, run the tests, then run a fresh round (§2, with history).
   - **COMPLEX** — several files, or a change in logic: go back to the protocol's code-writing step with `protocol_goto(step_name="<target>", reason="<findings>")`.
   - The test that covers a fix is not a second file. A fix that changes what the code does — a different default, a new flag or branch, a changed condition — is a logic change: COMPLEX even when it is one line.
   - **AMBIGUOUS** — the correct behaviour has to be decided: go back to the protocol's planning step and ask the user there.
   - Targets per protocol:

     | Protocol | COMPLEX | AMBIGUOUS |
     |----------|---------|-----------|
     | `task` | `implementation` | `investigation` |
     | `refactoring` | `refactoring` | `planning` |
     | `optimize` / `optimize-unattended` | `experimentation` | no planning step — ask the user here with `AskUserQuestion`, then route by the answer |
   - With findings of different depths open at once, the deepest wins (AMBIGUOUS > COMPLEX > SIMPLE): route the whole round there instead of half-fixing it here.
4. **Record the round** — after EVERY round, clean or not: `mcp__plugin_team-management_tm__protocol_save_note(note="REVIEW_ROUND: <n> — <c> critical, <w> warnings; <one-line dispositions>")`. The engine stamps the note with the working-tree hash. A fix made after it makes the round stale, and `verify_review_round` blocks advance until a fresh round is recorded. A rejection by another gate with no code change leaves the round valid — do not re-run the review for it.

The review loop ends on the first round that leaves no confirmed blocking finding.

## 4. Document Results (MANDATORY)

After the final round (§3 — no confirmed blocking finding left), append its results to the task work log:

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
- List each finding you did not confirm, or moved down the scale, under `## 🟢 Notes (N)` with its reason
- **Strip the `## Machine-Readable Findings` section before appending** — JSONL is the internal agent→orchestrator channel and never goes into the work log (see §4a)
- Include `## 📎 Appendix — Low Confidence (N)` if the final review has one (no gate parses it; it is not counted in Warnings)
- No other top-level `#` heading may appear inside the block — MR/PR extraction (`issue_provider_base.py`) truncates the block at the next `#` heading
- Inside `**Quote**:` fences, no line may begin with `#` at column 0 (quoted shebangs, `#include`, shell/Python/YAML comments) — the extraction regex treats any column-0 `#` line as the block terminator; indent the quoted snippet by one space when needed

## 4a. Machine-Readable Findings Contract (JSONL)

The Claude `code-review` agent's report ends with a `## Machine-Readable Findings` section: a fenced block containing ONE JSON object per line (JSONL), or the single line `NO FINDINGS`.

**Schema (one object per finding):**

| Field | Type | Meaning |
|-------|------|---------|
| `severity` | `critical` \| `warning` \| `note` | Same three-level scale as the markdown report |
| `confidence` | integer 1–10 | Calibrated per the agent's Confidence Calibration display rules |
| `path` | string | Repo-relative file path |
| `line` | integer | 1-based line the finding anchors to |
| `category` | `security` \| `correctness` \| `reliability` \| `performance` \| `consistency` \| `tests` \| `other` | Finding type |
| `summary` | string | One-sentence description |
| `fix` | string | Proposed fix (empty string when none) |
| `fingerprint` | string | Exactly `"{path}:{line}:{category}"` — the future dedup/consensus key |
| `verdict` | null | RESERVED — always `null` in v1; a future engine phase writes verification verdicts here. Do not populate it |
| `test_stub` | string, optional | Failing-test sketch demonstrating the issue; a finding carrying it is always classified ASK |

**Semantics:**
- The JSONL mirrors the report: every displayed finding AND every appendix (confidence 3–4) finding gets a line; suppressed confidence 1–2 findings are excluded. `NO FINDINGS` therefore means "nothing reportable", not "nothing suspected".
- **JSONL is internal.** It is NEVER appended to the work log and never posted to MRs/PRs. The work log stays markdown: copy the review block and STRIP the `## Machine-Readable Findings` section. The sentinel headers (`# Code Review:`, `## 🔴 Critical Issues (N)`, `## 🟡 Warnings (N)`) are parsed by the completion gates and MUST stay byte-unchanged.
- **Markdown is authoritative.** The markdown sections and the JSONL must describe the same finding set; the completion gates parse ONLY the markdown headings, so on any mismatch fix the JSONL to match the markdown.
- **Secrets stay redacted.** For credential-exposure findings, quote content and the `summary` / `fix` fields carry `[REDACTED]` in place of the secret value — the review text reaches the work log and MR/PR comments (see the Quote-Gate secrets rule in the agent prompt).
- **Provider and specialist findings are normalized in.** codex/agy wrappers return raw stdout shaped by the caller's prompt; the chain specialists (`testing-specialist`, `code-cleanliness`, `red-team`) return their own contract-shaped reports (strip each one's `## Machine-Readable Findings` block when merging). Any finding adopted into the final review must be normalized into this same markdown + JSONL contract (quote-gated, classified, fingerprinted) with its source noted — the contract covers the merged result, not only the Claude agent's own findings.
- **v1 honesty — stripping is a convention.** No engine code strips the JSONL for you; if it is mistakenly copied into the work log, the MR/PR exporter publishes it verbatim (`issue_provider_base.py` extracts the entire `# Code Review:` block). Engine-side stripping/validation is a future engine-phase item.
- The schema is the stable foundation for future engine work (fingerprint dedup, cross-model consensus, verdict verification) — extend it by adding optional fields, never by renaming existing ones.

## 5. Warning Enforcement Check

Determine the enforcement mode — **through the MCP tool, not the config file**:

1. Call `mcp__plugin_team-management_tm__config_code_review_enforcement` with the current task file content. This is the required first step — do NOT read `team-management/config.json` (via Read, `cat`, or any other route) unless the error-only fallback below applies.
2. On `success: true`, check `analysis.has_warnings`:
   - If `false`: No warnings, proceed
   - If warnings exist, check `enforce_warnings`:
     - **Strict Mode** (`true`): every confirmed Warning blocks like a Critical — route it by depth (§3), then a fresh round. Only Warnings in the three-level sense (defects, or behaviour no test exercises at all) block; Notes never block and never trigger a re-review.
     - **Relaxed Mode** (`false`): report confirmed warnings, continue automatically

**Error-only fallback**: ONLY if the tool call itself fails (tool not registered / MCP server down) or returns `success: false`, read the single `code_review.enforce_warnings` key from `team-management/config.json` (default: false). Extract just that key — do not print or dump the rest of the file into the conversation. On this path `analysis` is unavailable — determine warning presence from the confirmed findings of the final round (§3), then apply the same Strict/Relaxed logic.

**Note**: Critical issues always block completion regardless of this setting.

**Strict mode behavior**: In strict mode a confirmed Warning is handled like a Critical — fixed without asking the user (an AMBIGUOUS one goes to the user through the protocol's AMBIGUOUS route in the §3 table). Notes are never fixed inside this loop, whatever the mode.

## 6. Spec Compliance Audit (LAST — on the final tree)

**Run this after the final code-review round (§3). This is a separate, read-only audit that catches well-written code that doesn't match the spec. It audits the final tree once — review fixes made earlier never re-trigger it.**

The `code-review` step's `post_funcs` run `require_spec_review_passed` on advance, so advance from code-review → documentation is **structurally blocked** without a sentinel that matches the current tree. Dispatch the agent now:

1. **Launch the `spec-compliance-reviewer` Task agent** (`subagent_type: "spec-compliance-reviewer"` — its system prompt + allowed tools are loaded from `.claude/agents/spec-compliance-reviewer.md`). The agent is READ-ONLY — it cannot write files.
   - `prompt`: provide the task file path (e.g. `team-management/tasks/<task-name>.md`) and tell the agent to compare the git diff (working tree + staging) against the `## Success Criteria` bullets.
   - **Optimize protocol note** (`optimize` / `optimize-unattended`): the working tree is clean at this step — every hypothesis was committed during experimentation — so "working tree + staging" yields an empty diff. Tell the agent to review the cumulative diff **`optimize.baseline_commit..optimize.best_commit`** instead (both SHAs live in the task frontmatter / `optimize-state.json`).
   - **Later documentation step**: name it in the prompt — the later step whose `post_funcs` include `require_deferred_criteria_done`, which re-audits what this audit defers. In the shipped protocols that is `task`: `documentation`; `refactoring`, `optimize`, `optimize-unattended` have none — say so, and nothing may be deferred. In a custom protocol check the step's `post_funcs`, not the protocol name. The agent may mark a documentation criterion it cannot see done yet `DEFERRED` only when a later step is named. `require_spec_review_passed` blocks advance when the carriage note lists deferred criteria and no later step carries that gate.
   - Optional pre-dispatch checkpoint: `mcp__plugin_team-management_tm__protocol_save_note(note="SPEC_REVIEW: DISPATCHED")` — helps a resumed session (after auto-compact) distinguish «fresh entry, never dispatched» from «mid-review, waiting on result».
2. **Read the agent's verdict** (`# Spec Compliance Review` → `## Verdict` → PASS / FAIL) AND the per-item statuses in `## Coverage`. Each criterion carries one of six statuses; handle them per this table:

   | Status | Handling |
   |--------|----------|
   | `DONE` | Nothing to do. |
   | `PARTIAL` / `NOT DONE` | Verdict is FAIL — fix loop (step 3). The item carries Investigation Depth (`cause:` + `IMPACT:`) — use it to size the fix. |
   | `CHANGED` | Verdict is FAIL — reconcile spec and code: either align the code to the criterion as written, or — with explicit user approval — amend the SC wording in the task file; then re-dispatch the reviewer. |
   | `UNVERIFIABLE` | Does not block PASS. Recorded in the carriage note (step 5 below); confirmed per-item at completion (`task-completion.md` §1a). |
   | `DEFERRED` | Does not block PASS. A documentation deliverable the later step owns; recorded in the `SPEC_DEFERRED:` carriage note (step 5 below) and re-audited in that step, whose gate blocks until it is confirmed. A `DEFERRED` item in a protocol with no later documentation step is a reviewer error — treat it as `NOT DONE`. |

3. **If FAIL** → route each gap like a confirmed finding (§3 depth rules and targets — a missing criterion is usually COMPLEX and goes back to the code-writing step). A SIMPLE fix made here changes the tree, so run one code-review round on it first (§2 with history, scoped to the fix; record its `REVIEW_ROUND:` note), then re-dispatch this agent. Do NOT ignore the verdict — `require_spec_review_passed` blocks advance without the sentinel.
4. **On PASS** → call `mcp__plugin_team-management_tm__protocol_save_note(note="SPEC_REVIEW: PASSED")` verbatim. Exact string, exact casing, exact colon-space. Any deviation defeats the sentinel check.
5. **On every PASS, also record the UNVERIFIABLE carriage note** immediately after the sentinel: `mcp__plugin_team-management_tm__protocol_save_note(note="SPEC_UNVERIFIABLE: SC-x, SC-y")` — the exact prefix `SPEC_UNVERIFIABLE: ` followed by the comma-separated SC-IDs from the review's `## Unverifiable Items`, or `SPEC_UNVERIFIABLE: none` when that section is absent/empty. Save it on EVERY PASS (including re-review rounds) so the latest note always reflects the final review — the completion step reads the latest note; an explicit `none` supersedes a stale earlier list. For tasks whose criteria carry no SC IDs (legacy), save `SPEC_UNVERIFIABLE: legacy — see work log` instead — only when the set is non-empty (positional generated labels would not be stable across review rounds); a legacy task with zero UNVERIFIABLE items saves `SPEC_UNVERIFIABLE: none` like any other. In BOTH cases, when the set is non-empty, ALSO append the carry-list to the task work log (one line per item: SC-ID or verbatim criterion quote + its Verification Mode + why it is unverifiable + what the user should check) — the completion step's fallback channel when the protocol log is unavailable, the primary channel for legacy tasks, and the mode source for the per-item confirmation questions after compaction.
   Then, also on every PASS, record the deferred carriage note: `mcp__plugin_team-management_tm__protocol_save_note(note="SPEC_DEFERRED: SC-x, SC-y")` with the SC-IDs from the review's `## Deferred Items`, or `SPEC_DEFERRED: none` when there are none — the carriage is required after every `SPEC_REVIEW: PASSED`, and `require_spec_review_passed` blocks advance until it exists. Save it here, in this step: a carriage saved in any other step does not count. The later documentation step re-audits the listed items and saves `SPEC_DEFERRED: PASSED`.
6. **Then advance** (§7).

**Audit failure is NEITHER PASS NOR FAIL (fail-open prohibition).** A dispatch that returns no parseable verdict — the Task errored, the output is empty, or there is no `## Verdict` section — is not a verdict. Re-dispatch once. If the second attempt also fails to produce a verdict, STOP and ask the user explicitly via `AskUserQuestion` with **"Stop and fix"** as the recommended first option (diagnose the agent failure, then re-run the audit); the only alternative is `protocol_abort` — the branch and the work survive for manual completion outside the protocol. There is deliberately NO proceed-without-audit path: `require_spec_review_passed` structurally blocks advance without the sentinel, and recording `SPEC_REVIEW: PASSED` without a real PASS verdict is prohibited, whatever the failure mode.

**Freshness is structural.** The sentinel carries the working-tree hash the engine stamped when you saved it (§1). Any code edit after the PASS — from a reviewer, a user request, anything — makes it stale, and `require_spec_review_passed` blocks advance until you re-dispatch the agent against the current diff and save a fresh sentinel plus carriage note.

**Why two stages?** This audit asks «does the diff match what the task promised?». The code-review rounds (§2–§5) ask «is the diff correct, secure, and consistent with house style?». Either alone misses the other failure mode.

## 7. Advance Summary Discipline

When you call `protocol_advance`, the `summary` must evidence that review passed — not predict it. Concrete claims only.

**This is now structurally enforced.** The `check_completion_evidence` post_func parses `summary` and blocks advance unless it contains one of: a fenced command-output block ( \`\`\` ... \`\`\` ), `N/N passed` or `N/N tests`, an `exit 0` / `exit code: 0` token, check-marks with counts, or the literal keyword `tests passed` / `tests passing`. Prose predictions («looks good», «should pass», «LGTM») are rejected.

**Good:**
- «code-review agent + codex-cli + agy-cli ran in parallel. Final round: 0 critical, 0 warnings. Spec audit PASS on the final tree. Findings appended to work log under `# Code Review: <title>`.»

**Bad (red flags from `@knowledge/debugging.md`):**
- «Review looks good.» «Probably no issues.» «LGTM.» — predictions, not verification.

**Escape hatch** for steps that genuinely cannot be verified by running a review (rare here, but possible for behavioral-content-only changes when no reviewer tooling applies):
- `no-verification-applicable: documentation-only`

Use the canonical token verbatim after the colon — any suffix is flagged as anomalous in the audit log. Canonical reasons are enumerated in `@knowledge/debugging.md`.

## Notes

- **Two-stage structure**: the code-quality rounds (§2–§5 — is the diff correct, secure, consistent?) run FIRST; the spec-compliance audit (§6 — does the diff match the task?) runs LAST, once, on the final tree.
- Documentation agents (service-documentation, logging) run in the separate **documentation** step, not here.
- Push-back on reviewer findings is welcome when the reviewer is wrong (Codex/agy/humans/spec-compliance-reviewer can hallucinate file paths or miss context). Follow the push-back-with-evidence pattern in `@knowledge/receiving-feedback.md`.
