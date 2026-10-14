---
title: Review Output Contract
tags: [agents, protocols, security, testing]
created: 2026-07-30
updated: 2026-10-13
sources: [code-review.md, sub-protocols/code-review.md, risk-security-analyst.md, critic.md, testing-specialist.md, red-team.md]
---

# Review Output Contract

The unified output contract for review agents — the anti-false-positive layer of the
review chain. Prompt-text only (no engine machinery); the machine-readable schema is
deliberately shaped so a future engine phase (fingerprint dedup, cross-model consensus,
verdict verification) builds on it without rework. Carriers: `plugin/agents/code-review.md`
(the rules + output template) and `sub-protocols/code-review.md` §4/§4a (orchestration
documentation); `risk-security-analyst.md` and `critic.md` carry their own smaller pieces.

## Moving Parts (code-review agent)

- **Severity scale (three levels, shared by every code-review reviewer and both provider
  templates)**: Critical — security fault, data loss or corruption, crash, broken contract,
  unmet success criterion; Warning — a defect (wrong behaviour in a reachable case), a
  success criterion no test exercises at all, or changed behaviour no test exercises at
  all; Note — everything else (a test that could assert more, a mutation a test lets
  through, naming, style, refactor, findings in untouched code). Notes never block. The
  narrow Warning band is what keeps fresh reviewers from turning test-improvement ideas
  into blocking findings round after round. `critic` and `risk-security-analyst` keep
  their own four-level scales.
- **Confidence calibration (1–10)** with display rules: 7+ reported normally; 5–6 with a
  mandatory `Medium confidence — verify before acting` caveat; 3–4 appendix-only
  (`## 📎 Appendix — Low Confidence (N)`); 1–2 suppressed. **Critical exception**: a
  Critical-severity finding is never appendix'd or suppressed regardless of confidence —
  it always lands in the gate-parsed `## 🔴 Critical Issues (N)` (otherwise the completion
  gate would pass on a reported deployment-blocker).
- **Quote-gate (pre-emit)**: every displayed finding quotes the motivating code verbatim
  in a `**Quote**:` fence (meta-construct for ORM/metaclass symbols). No quote → confidence
  CAPPED at 5 (cap only lowers, never raises). **Secrets exception**: a credential-exposure
  finding quotes with the secret value replaced by `[REDACTED]` — no confidence penalty;
  the secret must not appear anywhere in the review (it reaches the work log and MR/PR).
- **AUTO-FIX vs ASK** classification — advisory metadata for the orchestrator; it does not
  change blocking rules (Critical blocks completion; in strict mode a confirmed Warning
  blocks too).
  Critical → always ASK; a finding carrying `test_stub` → ASK.
- **DO NOT Flag (FP suppressions)** — suppression list with pinpoint EXCEPT clauses per
  entry; pre-existing issues outside the diff are noted as "pre-existing", never findings.
- **Machine-readable findings (JSONL)** — `## Machine-Readable Findings` fenced block, one
  JSON object per finding: `{severity (critical|warning|note), confidence, path, line, category, summary, fix,
  fingerprint: "{path}:{line}:{category}", verdict: null (reserved), test_stub?}`; the
  single line `NO FINDINGS` when nothing is reportable. Scope: displayed + appendix
  findings; suppressed 1–2 excluded. Schema evolves by ADDING optional fields only.

## Sibling Agents

- `testing-specialist.md` and `red-team.md`: full contract carriers — both mirror the
  code-review agent's Confidence Calibration, Quote-Gate, AUTO-FIX/ASK, and JSONL sections
  (testing-specialist's JSONL `category` is typically `tests`; red-team adds a
  no-duplicate-round-1 rule and a `## Round-1 Coverage Map` section). Dispatch is
  classifier-gated — see [Review Dispatch Gate](pages/entities/review-dispatch-gate.md).
- `risk-security-analyst.md`: every Security Finding carries
  `**Status**: VERIFIED / UNVERIFIED / TENTATIVE` (traced-with-quote / plausible-untraced /
  speculative).
- `critic.md`: opens with a "Fresh context, no prior review bias" declaration and must end
  with the canonical verdict line `Recommendation: <action> because <one-line reason naming
  a specific finding>` — generic reasons disqualified.

## Load-Bearing Invariants (gotchas)

- **Sentinel headings are parsed by three consumers** and must stay byte-unchanged:
  `_func_validate_no_critical_issues_in_worklog` in `protocol_engine.py` (last
  `# Code Review:` block, `## (🔴) Critical Issues (N)` — emoji optional),
  `check_code_review_warnings` in `protocol_utils.py` (`## 🟡 Warnings (N)` — the 🟡 is
  REQUIRED; a plain heading silently blinds warning enforcement), and
  `_extract_code_review_results` in `issue_provider_base.py` (MR/PR comment extraction).
- **Column-0 `#` truncation hazard**: the MR/PR extraction regex terminates the block at
  ANY line starting `#` + non-`#` — a quoted shebang or shell comment at column 0 inside a
  Quote fence truncates the published review. The contract mandates indenting such quoted
  snippets by one space. (Empirically verified during review.)
- **No other top-level `#` heading** may appear inside the review block for the same reason.
- **JSONL is internal by convention only (v1 honesty)**: no engine code strips it — the
  orchestrator strips `## Machine-Readable Findings` before appending to the work log; if
  copied through by mistake, the MR/PR exporter publishes it verbatim. Engine-side
  stripping/validation is future engine-phase work.
- **Markdown is authoritative**: gates parse only the markdown headings; on any
  markdown/JSONL mismatch the JSONL is wrong.
- **Provider findings are normalized in**: codex/agy output adopted into the final review
  must be re-expressed in this same markdown + JSONL contract with its source noted.

## Drift Guards

- `test/test_review_contract_drift.py` — 14 content-level assertions over the contract text
  in all four carriers (sections present, cap wording, Critical exception, column-0 rule,
  `[REDACTED]` rule, structural `**Quote**:`+fence count, JSONL schema fields, critic
  verdict line, risk status labels).
- `test/test_code_review_heading_gates.py` — the pre-existing sentinel-heading guards.

## Related

- [Specialized Agents](pages/entities/specialized-agents.md) — agent catalog and dispatch.
- [task Protocol](pages/protocols/protocol-task.md) — the code-review step this contract serves.
- [AI Provider Integration](pages/subsystems/ai-provider-integration.md) — the codex/agy wrappers whose findings get normalized into the contract.
