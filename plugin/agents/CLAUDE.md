# Agents Module CLAUDE.md

## Purpose
Subagent definitions shipped with the plugin. Each agent runs in its own context window, receives a lightweight prompt from the orchestrator, and returns a structured result.

## Conventions
- Format: YAML frontmatter (`name`, `description`, `tools`, optional `color`/`model`) followed by the instructions.
- Every shipped agent (all `*.md` here except this file) carries the do-not-edit HTML banner right under its frontmatter. Agents are replaced on every plugin update; users customize a renamed copy in their project's `.claude/agents/` (`test/test_agent_banner_drift.py`).
- Reviewers never edit files; they report. The orchestrator applies fixes.

## Review Chain (code-review step)
All reviewers share one output contract, defined in `code-review.md` and `sub-protocols/code-review.md` §4/§4a (drift-guarded by `test/test_review_contract_drift.py` and `test/test_review_severity_scale.py`):
- **Severity**, three levels:
  - Critical — security fault, data loss, crash, broken contract, unmet success criterion;
  - Warning — a defect, or a success criterion / changed behaviour that no test exercises at all;
  - Note — everything else; Notes never block.
- Findings group under the gate-parsed headings `## 🔴 Critical Issues (N)`, `## 🟡 Warnings (N)`, `## 🟢 Notes (N)`.
- **Confidence** 1–10:
  - 7+ shown;
  - 5–6 shown with a caveat;
  - 3–4 go to the appendix;
  - 1–2 are suppressed;
  - a Critical is never moved to the appendix or suppressed.
- **Quote-Gate**: a finding without a verbatim quote is capped at confidence 5. Secret values are quoted as `[REDACTED]`.
- **AUTO-FIX vs ASK** classification is advisory; a Critical is always ASK.
- **Machine-readable output**: a closing `## Machine-Readable Findings` JSONL block — `severity` ∈ {critical, warning, note}, `fingerprint` = `path:line:category`, `verdict` is always `null`. The orchestrator strips it before the work log.
- **Structure**: no extra top-level `#` heading inside a review, and no column-0 `#` line inside quote fences — MR/PR extraction truncates at either.

Agents:
- `code-review.md` — mandatory reviewer for every round.
- `testing-specialist.md` — test quality. A behaviour with no test at all is a Warning; "a test could assert more" is a Note. Fires when the diff classifier fires (≥50 changed lines, not docs/config-only).
- `code-cleanliness.md` — maintainability. Findings are Notes unless they hide a named defect. Fires with testing-specialist; also on demand via `/team-management:clean-check`.
- `red-team.md` — second echelon. Gets the merged round-1 findings and hunts only for what they missed. Fires on >200 changed lines or any round-1 Critical, at most once per step entry; `code_review.red_team_enabled: false` wins over both triggers.
- `spec-compliance-reviewer.md` — read-only audit of the diff, untracked files included, against the task's Success Criteria. Runs LAST, on the final tree.
  - Each criterion gets a Verification Mode (`DIFF-VERIFIABLE` / `CROSS-REPO` / `EXTERNAL-STATE` / `CONTENT-SHAPE`) and a status (`DONE` / `PARTIAL` / `NOT DONE` / `CHANGED` / `UNVERIFIABLE` / `DEFERRED`).
  - PASS iff every criterion is DONE, UNVERIFIABLE or DEFERRED and there is no scope creep.
  - DEFERRED is only for documentation (`CLAUDE.md` in any directory, `README*.md`, `CHANGELOG.md`, `wiki/`, `docs/`) owned by a later step the prompt names (`documentation` in `task`) — never code, tests, or an agent, command, skill, sub-protocol or prompt template; a criterion missing a code or test part is never DEFERRED.
  - Deferred-check mode (dispatched by the task documentation step): audits only the listed SC-IDs; PASS when each is DONE or UNVERIFIABLE.
  - UNVERIFIABLE items are carried to completion for per-item user confirmation.
- Every specialist prompt must list the complete changed-file set, untracked files included. On a re-review round every reviewer also gets the scale and the previous rounds' findings with their dispositions.

## AI Provider Wrappers
- `codex-cli.md` — runs `codex review "<prompt>"` or `codex exec -s read-only`.
  - Never pass a scope flag alongside a prompt: the CLI rejects the pair, and the wrapper would silently drop the instructions.
  - Uses `gtimeout`/`timeout` when available, else a shell-native 300s watchdog.
  - Runs under an `env -i PATH HOME TMPDIR` scrub.
  - Every launch ends with `</dev/null`. On the foreground branch codex would otherwise wait on the caller's open stdin pipe until the watchdog fires.
- `agy-cli.md` — runs `agy --add-dir "$PWD" --print-timeout 480s -p ...`. Never `--sandbox` (breaks git on macOS) and never `--dangerously-skip-permissions` (security monitors block it).
  - Preflight refuses to run unless the `.agents/hooks.json` read-only deny-gate is deployed unaltered AND the `permissions.allow` rules are present.
  - Refuses to launch when the prompt asks agy to read a path outside the repository (Preflight 3); it never reads that file to inline it.
  - Classifies headless denials by the tool agy names: `"command"` → `permission rules not configured`; `"read_file"` / `"list_dir"` → a path outside the repository; any other tool → a generic auto-deny reply.
  - Detects agy's own mid-turn print timeout (`print timeout after … turn in progress`, exit 0, no review) and reports it as unavailable. Both exit-0 failure signatures are checked before stdout is passed through.
  - Snapshots `git status --porcelain` and a `git diff HEAD` checksum before and after, and prepends a WARNING when agy modified files.
  - 510s watchdog on both branches (`gtimeout`/`timeout` or shell-native), the whole snippet in one Bash call with `timeout: 600000`, same env scrub, and stdin detached with `</dev/null` on every launch.
- Both wrappers are pass-through: the caller owns the prompt. A failure returns `<provider> review unavailable: <reason>` and never blocks the workflow.

## Investigation, Design and Analysis
- `context-gathering.md` — writes a Context Manifest into a task file. Consults `wiki/` first when it exists.
- `context-refinement.md` — updates the manifest with session discoveries.
- `code-explorer.md` — traces execution paths and maps architecture for a feature.
- `code-architect.md` — produces an implementation blueprint from existing patterns.
- Brainstorm analysts, launched in parallel by the brainstorm `analysis` step and usable standalone:
  - `critic.md` — devil's advocate with YAGNI checks; ends with `Recommendation: <action> because <specific finding>`;
  - `user-perspective.md` — user impact;
  - `risk-security-analyst.md` — risk and threat model; every security finding carries `VERIFIED` / `UNVERIFIED` / `TENTATIVE`;
  - `scope-strategist.md` — scope and phasing.
- `critic.md` and `risk-security-analyst.md` keep their own four-level severity scales; the three-level scale applies to code-review reviewers only.

## Records and Documentation
- `logging.md` — consolidates the task work log. Runs at the documentation step and during compaction.
- `service-documentation.md` — maintains CLAUDE.md files.
  - It receives the CURRENT behaviour of the changed modules, never the story of the task.
  - It applies the content test: "would a developer starting a new task tomorrow need this?".
  - It references code by symbol name, not line number.
  - The `check_claude_md_hygiene` gate blocks added lines that name tasks, use history wording or carry review chatter.

## Subagent Runtime
- Subagent context is tracked by the depth counter in `.claude/state/subagent-depth.json` (see `plugin/hooks/CLAUDE.md`).
- Inside a subagent the DAIC and branch checks are bypassed for all tools, Bash included. Protected-path, frozen-path and safety-guard checks still apply.
- Parent-transcript chunks are staged per Task invocation under `.claude/state/{subagent_type}/{key}/` and archived into the task's state dir afterwards.
