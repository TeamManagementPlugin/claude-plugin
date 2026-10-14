# Changelog

All notable changes to the team-management plugin are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the version numbers follow [Semantic Versioning](https://semver.org/).
Each version gets a `## [X.Y.Z] - YYYY-MM-DD` section; at session start the
plugin shows the sections you missed since your last used version.

## [0.4.5] - 2026-10-14

### Added
- **Update notifications.** After an update, session start shows a one-line banner
  with the old and new version and hands Claude the changelog sections you missed.
  `/team-management:changelog` shows the release notes at any time.
- **Review team.** `testing-specialist` and `red-team` join `code-cleanliness` in code
  review, dispatched by a deterministic diff classifier (`code_review.*_enabled`). All
  reviewers share one output contract: Critical / Warning / Note severity,
  confidence-calibrated and quote-gated findings, machine-readable JSONL.
- **Completion honesty contract.** The spec audit classifies every Success Criterion
  by status and verification mode; UNVERIFIABLE items are confirmed with you one by
  one at completion. A documentation criterion that `task` writes after review is
  marked DEFERRED and re-audited in the documentation step, which cannot advance
  until it passes.
- **Retro protocol.** `protocol_start(protocol_name="retro")` builds a team
  retrospective from git history and protocol logs: deterministic metrics for the
  window, a narrative report and a JSON trend snapshot in `docs/retros/`. Works in
  repositories without a remote.
- **Safety guards.** Destructive Bash commands (`rm -rf`, `git reset --hard`, forced
  pushes, `DROP TABLE`, `kubectl delete`, ...) need a typed one-time confirmation
  (`safety_guards.*`; a friction guard, on by default).
- **CLAUDE.md hygiene.** The documentation step blocks added lines that name tasks,
  use history wording or carry review chatter, and growth past 300 lines
  (`documentation.claude_md_hygiene`, `documentation.claude_md_max_lines`).
- **agy setup.** `/team-management:config` offers to add read-only
  `permissions.allow` rules to agy's global settings (merge-only); session start
  warns when agy is enabled without them.
- Behaviour rules in the shipped templates: user sovereignty, session hygiene,
  question grounding.
- CI: release integrity requires a changelog section for the shipped version;
  GitHub Release notes come from this file.

### Changed
- **Code review converges.** Notes never block, so strict mode
  (`code_review.enforce_warnings`) no longer loops on style and test ideas. Each
  finding is confirmed in the code, re-review rounds carry the earlier findings, and
  fixes are routed by depth: simple ones in the step, behaviour changes back to
  implementation, open questions to you.
- **The spec audit runs once, last**, on the final tree. Review and audit notes are
  bound to a working-tree hash, so changed code cannot pass on an old review. A
  repository needs at least one commit.
- **Steps that dispatch codex/agy wait for them** before editing, presenting a plan
  or advancing.
- The specialized agents are presented as a team with human-recognizable roles.

### Fixed
- **agy reviews run and finish.** The read-only gate allows agy's real tool names,
  read-only pipelines and read-only git subcommands; `--dangerously-skip-permissions`
  is gone (headless agy honours `permissions.allow`). agy gets 480 s (510 s
  watchdog) and a budget in every prompt. Its timeouts and denials are reported as
  "review unavailable" with the cause, never as a clean review. Re-review history is
  passed inline, because agy cannot read outside the repository.
- **Read-only gate hardening.** It expands globs before judging a command, refuses
  `git cat-file --filters` and `--show-signature`, answers within its time budget
  and denies on any internal error.
- **Codex no longer hangs until its 300 s timeout**: both provider wrappers read
  stdin from `/dev/null`.

### Upgrading
- If you forked a protocol with `/team-management:custom-protocol-create`, run
  `/team-management:custom-protocol-update-after-reinstall`. A fork also copies the
  shared `sub-protocols/code-review.md`, which then serves the unforked `task`
  protocol too.
- A task that is in code review when you update re-runs its review round and spec
  audit before it can advance.

## [0.4.4] - 2026-07-29

### Added
- Spec traceability: numbered Success Criteria (`SC-N`) with `[SC-N]`-tagged
  implementation plan steps and coverage auditing.
- Question discipline for investigations: up-front backlog, impact-ordered
  `AskUserQuestion` widgets with recommendation-first options, 5-question cap.
- Tool-first review configuration (`code_review.*` dispatch gates in the
  config schema).

### Fixed
- Archived tasks now get their frontmatter status flipped to `completed`
  (with a `completed:` date) when moved to `done/`.

---

Earlier releases predate this changelog; see the git history
(`git log --oneline`) for details.
