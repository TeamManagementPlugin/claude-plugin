---
title: Completion and Git Flow
tags: [git, issue-tracking, protocols]
created: 2026-05-31
updated: 2026-07-15
sources: [plugin/hooks/optimize_completion.py, plugin/hooks/protocol_engine.py, plugin/protocol-configs/sub-protocols/task-completion.md, plugin/hooks/git_operations.py, plugin/mcp/tools/git_operations.py]
---

# Completion and Git Flow

The completion step is the last step of any protocol that ends in git work (`task`, `brainstorm` planning, `refactoring`, `optimize`, `optimize-unattended`). Instead of a fixed straight-line chain of git/issue post_funcs, every such step now wires `pre_funcs: [present_completion_options]` and `post_funcs: [require_discard_confirmation, completion_dispatch]`. The dispatcher branches on `issue_tracking.provider` so that GitLab/GitHub/Jira users keep the exact pre-dispatcher automation, while users with no issue tracker get a 4-option menu (`merge_local` / `push_pr` / `keep` / `discard`). All of the dispatch logic lives in the `OptimizeCompletionMixin` in `plugin/hooks/optimize_completion.py` (composed into `ProtocolEngine`). The two MCP tools in `plugin/mcp/tools/git_operations.py` (`git_commit`, `git_push`) and the shared helpers in `plugin/hooks/git_operations.py` are *operator-facing* git utilities, not part of the automated chain — see [Issue Tracking Providers](pages/subsystems/issue-tracking-providers.md) for the provider-side MR/PR creation tools.

## Mechanics

### Provider detection — the branch fork

`_read_issue_provider` (`optimize_completion.py`) is the single decision point. It reads `team-management/config.json` and returns a `(provider, source)` tuple. `("disabled", "configured")` is the ONLY value that reaches the menu — but it is arrived at two ways (explicit and inferred):

- `(value, "configured")` — reached (1) when `issue_tracking.provider` is explicitly set (`("gitlab", "configured")`, `("disabled", "configured")`, …); (2) INFERRED disabled — the config has no `issue_tracking.provider` key (section ABSENT, or a dict missing `provider`) **AND no issue provider is enabled** (`_any_issue_provider_enabled` → gitlab/jira/github `.enabled`), so a fresh plugin project with no tracker returns `("disabled", "configured")` and gets the local-completion menu (m-fix-completion-strands-without-remote).
- `("unknown", "unreadable")` — file missing, not valid JSON, or valid JSON of the wrong shape (a list/string/number — the `isinstance(config, dict)` guard exists because `config.get("issue_tracking")` would raise `AttributeError` on a list).
- `("unknown", "legacy")` — run the legacy provider-driven chain. Reached when the config lacks `issue_tracking.provider` but a provider **is** enabled (genuinely-old GitLab/Jira install), or when `issue_tracking` is present-but-non-dict (corruption — preserve the "malformed never forces the menu" contract).

`_any_issue_provider_enabled(config)` is the boolean gate for the inference: True iff any of gitlab/jira/github has a truthy `.enabled`, isinstance-guarded so a malformed provider section (`gitlab: "x"`) cannot crash the probe. It mirrors the MCP provider resolver's enabled-flag checks (`mcp/core/config.py`). Before this change a config with no `issue_tracking` key ALWAYS fell through to the provider chain — which stranded a fresh no-tracker/no-remote project, since the chain's `git fetch`/`git push` both fail without a remote. The inference now routes those projects to the local menu instead; genuinely-old enabled-provider installs still get their straight-line chain (they would otherwise fail with `completion_option is required`).

### present_completion_options (pre_func)

`_func_present_completion_options` runs on step entry. For `unreadable`/`legacy`/any non-`disabled` provider it returns `{success: True, skipped: ...}` — no menu. Only for `("disabled", "configured")` does it print the 4-option menu text with worked `protocol_advance` payloads — including the INFERRED-disabled case (no `issue_tracking.provider`, no provider enabled), so a fresh no-tracker project sees the menu.

### completion_dispatch (post_func)

`_func_completion_dispatch` executes on advance. Control flow:

1. **Optimize short-circuit**: if the active protocol is `optimize` / `optimize-unattended`, hand off to `_completion_optimize` before anything else.
2. **Provider-driven chain**: if `source != "configured"` or `provider != "disabled"`, run the 9-func chain via `_run_completion_chain("provider", …)`: `archive_task → git_commit → git_merge_main → git_push → create_merge_request → update_issue_status → cleanup_task_scoped_state → clear_task_state → checkout_default_branch`. This is byte-for-byte the old behaviour. `_run_completion_chain` stops at the first sub-func failure and returns it. **No-remote safety** (m-fix-completion-strands-without-remote): `_func_git_merge_main` and `_func_git_push` (in `protocol_engine.py`) probe for an `origin` remote via `_origin_remote_exists()` and return `{success: True, action: "skipped"}` in a local-only repo instead of hard-failing on `git fetch origin` / `git push origin` (which stranded the chain — committed + archived, never merged/cleaned). The probe skips ONLY on a demonstrably-absent origin: a failed `git remote` query (rc≠0 / not-a-repo / git missing) returns True so the fetch/push still runs and surfaces the real error.
3. **Disabled-menu dispatch** (onward): require `completion_option`; enforce the branch-safety precondition; route to one of the four `_completion_*` helpers.

### Branch-safety precondition

Before any of the four local flows runs, the dispatcher reads `expected_branch` from task state and compares it against `_current_branch()` (a `git branch --show-current` wrapper returning `None` on detached HEAD / non-repo). A missing `expected_branch` or a mismatch hard-fails with a remediation message. Rationale: the local flows run `git reset --hard` / `git clean -fdx` / `git commit` against the branch they *think* is checked out — if the operator manually checked out `main` before advancing, these would be silent data loss on the wrong branch. `_completion_optimize` repeats the same precondition before its squash.

### The four local flows

- **`merge_local`** (`_completion_merge_local`): `archive_task → git_commit → checkout default → git merge --no-ff feature → git branch -d feature (safe delete) → cleanup → clear state`. Ends on the default branch with work merged locally; no remote push. `_git_merge_feature` uses `--no-ff` to preserve history; `_git_delete_feature(force=False)` uses `-d` which refuses unmerged branches.
- **`push_pr`** (`_completion_push_pr`): `gh`-presence check → `archive_task → git_commit → git_push → gh pr create → cleanup → checkout default → clear state`. Missing `gh` returns an install-hint error. Post-PR housekeeping order is deliberate: cleanup → checkout → clear, so a checkout failure leaves `current_task.json` intact for a clean retry (an earlier order stranded users on the feature branch with state already wiped).
- **`keep`** (`_completion_keep`): `archive_task → git_commit → cleanup → clear state`. No merge, no push, no checkout — the feature branch stays checked out for further review.
- **`discard`** (`_completion_discard`): `git reset --hard HEAD → git clean -fdx -e team-management/ -e .claude/ → checkout default → git branch -D feature (force) → cleanup → clear state`. No archive, no commit — the task's tracked work goes away with the branch. Note `-fdx` not `-fd`: gitignored junk (build artifacts, `.env.local`, editor caches) is removed too, matching the "throw away all work" contract and preventing a later "untracked files would be overwritten" checkout failure. **But the framework's OWN working tree is excluded** (`-e team-management/ -e .claude/`, h-fix-discard-clean-and-windows-transcript): the framework gitignores `team-management/config.json` and all of `.claude/`, so an un-excluded `-fdx` would irrecoverably wipe config, every `.claude/state/*-mappings.json` (task↔issue links), logs, and any UNTRACKED sibling task or custom protocol under `team-management/`. Discarding ONE task must never destroy config, mappings, other tasks, or custom protocols. Accepted nuance: the discarded task's own untracked task file may remain behind as harmless litter — strictly better than nuking siblings. (Task-scoped `.claude/state/tasks/<task>/` is still removed by `cleanup_task_scoped_state`; the exclude only spares the git-clean, and shared mappings/config/logs live at top-level `.claude/state/*` + `.claude/logs/`.)

Every flow appends per-step dicts to `sub_results` and short-circuits through `_completion_fail` on the first failure, surfacing `failed_at` and the partial `sub_results`.

### push_pr idempotency precheck

`_gh_find_existing_pr` runs `gh pr view <branch> --json url,state` *before* `gh pr create`. If an OPEN PR already exists (e.g. a prior dispatch opened the PR then failed in housekeeping), its URL is reused (`reused_existing: True`) instead of letting `gh pr create` exit non-zero with "a pull request for branch X already exists". Silent best-effort: any `gh`/auth/JSON error returns `None` and the caller proceeds to `gh pr create`.

### Default-branch detection

`_detect_default_branch` is the canonical resolver, used by `push_pr`, `discard`'s dry-run count, and the merge flows. Preference order:

1. `git symbolic-ref --short refs/remotes/origin/HEAD` (strip the `origin/` prefix) — authoritative for any clone.
2. Probe local candidates `main` / `master` / `develop` / `trunk` / `stable` via `git rev-parse --verify`.
3. Hard-coded `"main"` fallback.

This replaced a hardcoded `main`/`master` that broke repos with custom default branches.

### Discard confirmation gate — friction, not security

`_func_require_discard_confirmation` is the first post_func, gating the dispatcher's discard path:

- `completion_option != "discard"` → pass-through skip.
- First call (no `discard_confirmed_dry_run`) → `success: False` with a **dry-run** block: the branch name, the commit count (`git rev-list --count <default>..HEAD` — uses `_detect_default_branch` so custom defaults get an accurate count), and the exact re-advance args to type.
- Second call must carry all three: `completion_option == "discard"` AND `discard_confirmed_dry_run is True` AND `discard_confirmation == "discard"` (exact string match).

The docstring is explicit that this is friction, not authentication: an LLM trivially produces `"discard"` and `True`. The real safety net is that the user must pick `discard` from four visible options, and the protected-state hooks prevent external processes from bypassing the menu.

### Optimize completion

`_completion_optimize` reads `optimize.best_commit` and `optimize.baseline_commit` from task frontmatter and errors if either is absent, `-`, or equal (no improvement to squash). It builds a markdown leaderboard via `_build_leaderboard` (top-10 from `results.tsv`), composes a squash commit message, then — **validates the chosen option and runs the discard gate BEFORE squashing**: a missing-option error or a discard dry-run must not rewrite history. `_squash_to_best` is the three-step transform `git reset --hard <best>` → `git reset --soft <baseline>` → `git commit --allow-empty`, capturing the original HEAD up front and `reset --hard`-ing back to it on any partial failure. **Discard skips the squash entirely** — the branch is being deleted, so squashing first would mutate history pointlessly and risk stranding. After squash (or after skipping it for discard), it dispatches into the same `_completion_*` helpers / provider chain as the non-optimize path, but with `git_commit` placed *after* `archive_task` so the archive rename is committed before the merge. See [Optimize Protocols](pages/protocols/optimize-protocols.md) for the experimentation loop that produces `best_commit`.

## Design Decisions

- **Dispatcher over straight-line chain.** The old completion step was a hardcoded post_func list. Replacing it with `completion_dispatch` lets one code path serve five protocols and three+ provider modes while guaranteeing zero behaviour change for existing GitLab/GitHub/Jira users — every non-`("disabled","configured")` case re-runs the identical 9-func chain.
- **A no-tracker config infers `disabled`; an enabled-provider or corrupt config falls through.** Blanket-treating an absent `issue_tracking.provider` key as "menu" would have broken genuinely-old GitLab/Jira upgrades; blanket-treating it as "provider chain" stranded fresh plugin projects with no tracker + no remote (`git fetch`/`git push` fail). The resolution keys on whether a provider is actually enabled (`_any_issue_provider_enabled`): no key + no provider enabled → infer `disabled` (menu); no key + provider enabled → `legacy` (chain); non-dict `issue_tracking` → `legacy` (corruption, preserve the malformed-never-forces-menu contract); `unreadable` → chain. Only the explicit-or-inferred `("disabled","configured")` gets the menu (m-fix-completion-strands-without-remote).
- **Validate-then-mutate ordering.** Both the disabled dispatch and the optimize path validate the option (and run the discard gate) before any irreversible git operation. An earlier optimize implementation squashed first, which defeated the typed-confirmation gate.
- **Housekeeping order (cleanup → checkout → clear).** Clearing task state is the last and most expensive thing to lose, so it runs last; checkout runs before clear so a checkout failure is recoverable.
- **`gh pr view` idempotency.** Completion can fail mid-housekeeping; making `push_pr` re-runnable avoids a hard error on the second attempt.

## Gotchas

- **The 4-option menu is reached only via `("disabled", "configured")`** — explicitly set, OR inferred when there is no `issue_tracking.provider` key and no provider enabled. A malformed `config.json` (fat-finger edit → `unreadable`) or a non-dict `issue_tracking` section still silently runs the provider chain — completion will not block on a broken config.
- **The provider-driven chain no longer strands a local-only repo.** `git_merge_main` / `git_push` skip gracefully (`action: "skipped"`) when `origin` is demonstrably absent; before the fix, `git fetch origin` hard-failed and left the task committed + archived but never merged/cleaned. The skip fires ONLY on a demonstrably-absent origin — a `git remote` error returns True (don't skip) so a real failure still surfaces.
- **A valid-JSON-but-wrong-shape config (`[]`, `"oops"`, `42`, `null`) is classified `unreadable`, not `legacy`.** The `isinstance(config, dict)` guard is what prevents an `AttributeError` crash before the fallback can absorb it.
- **`discard` uses `git clean -fdx -e team-management/ -e .claude/`** — it deletes untracked + gitignored junk (build artifacts, `.env.local`, local caches) but EXCLUDES the framework working tree so config / issue-mappings / sibling tasks / custom protocols survive (h-fix-discard-clean-and-windows-transcript). `-x` still removes gitignored files OUTSIDE those two dirs. The excludes use gitignore "match at any level" semantics (no leading/mid slash), so a nested `team-management`/`.claude` dir is spared too — strictly more protective. The dry-run message names both the removed and the preserved sets.
- **The branch-safety precondition only guards the disabled local flows and optimize.** The provider-driven chain does not run the HEAD-vs-task-branch check before its operations.
- **`_func_present_completion_options` and `_func_require_discard_confirmation` read provider/branch state independently of the dispatcher** — they each call `_read_issue_provider` / `get_task_state` again rather than receiving it. A config change between pre_func and post_func within the same step is theoretically possible.
- **The MCP `git_commit` / `git_push` tools (`mcp/tools/git_operations.py`) are NOT part of the completion chain.** Their docstrings explicitly say "Only call when the user explicitly asks … Do NOT call autonomously as part of a workflow." The chain's commit/push are the engine's own `_func_git_commit` / `_func_git_push`. Do not confuse the two surfaces.
- **The shared helpers in `hooks/git_operations.py`** (`run_git`, `create_commit`, `push_to_remote`, etc.) are used by the *provider sync utilities*, not the completion dispatcher, which inlines its own `subprocess.run` calls with `engine_constants` timeouts.
- **The legacy `plugin/protocols/task-completion.md` was deleted** (h-retire-legacy-protocol-pointers) — it was never deployed and documented a pre-dispatcher world (manual `mv` to `done/`, the now-retired jira-sync/gitlab-sync agents, super-repo submodule ordering). The authoritative runtime path is the sub-protocol `protocol-configs/sub-protocols/task-completion.md` (Sections 4a/4b/4c) driven by the `completion_dispatch` func.
- **Research tasks with no branch cannot complete via `protocol_advance`** — the automated commit needs a branch. Use `protocol_abort` and archive findings manually.
