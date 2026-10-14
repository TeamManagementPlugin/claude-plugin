---
title: Safety Guards
tags: [hooks, daic, security, config, testing]
created: 2026-08-01
updated: 2026-08-01
sources: [sessions-enforce.py, config.template.json, USAGE_GUIDE.md]
---

# Safety Guards

The destructive-Bash-command guard in the PreToolUse enforcement hook: a
segment-aware classifier (`check_safety_guard in sessions-enforce.py`) that blocks
the destructive pattern set behind an exit-2 block plus a two-step typed
confirmation. The properly-enforced version of gstack's advisory regex (brainstorm
block I, CR-5/CR-12). **Friction guard, NOT a security control** — the confirmation
is same-turn replayable and `safety_guards.enabled` is a UX toggle.

## Placement and Precedence

- Lives in `main()`'s **universal Bash-guard block** (after `block_manual_task_archival`,
  before the read-only fast-exit and the subagent bypass) → binds subagents too.
- **Workflow bypass wins** (D6): `check_workflow_bypass()` exits earlier — the guard
  follows DAIC/branch/frozen workflow semantics; only the secret token-bridge guard
  precedes bypass.
- Engine git operations (completion/discard) run in the MCP server process and are
  never hooked — no interference with `completion_dispatch`.

## Classification

- **Pattern set**: `rm` with recursive+force flags; SQL `DROP TABLE|DATABASE` /
  `TRUNCATE` (parse-independent regex per segment); `git push --force`/`-f`/
  `--force-with-lease`; `git reset --hard`; `git checkout .` / `git restore .`
  (incl. after `--`); `git clean` with a force flag (combined dry-run spellings
  `-fdn`/`-nf` stay allowed); `kubectl delete` (greedy bare-token match);
  `docker rm -f` / `container rm -f` / `--force=<bool>` / `docker system prune`.
- **Segment-aware**: splits on `| || & && ; newline`; per-segment shlex tokenization
  with whitespace-split degrade on parse failure (SC-3 was deliberately NARROWED —
  literal block-on-unparseable would block heredoc bodies containing apostrophes).
- **Wrapper stripping**: `_WRAPPER_COMMANDS` (sudo/doas/env/timeout/gtimeout/setsid/
  stdbuf/nice/ionice/time/nohup/command/exec/xargs) with per-wrapper
  `_WRAPPER_VALUE_FLAGS` (separate-value flags incl. sudo long forms
  `--chdir/--user/--group`) and `_WRAPPER_CHDIR_FLAGS` (chdir-class flags mark the
  segment foreign). docker/git global value-options skipped via
  `_subcommand_after_flags` / `_git_subcommand`.
- **Whole-command allowlist**: anchored `rm -rf` of node_modules/dist/build/
  __pycache__/.cache/coverage — chains/comments defeat it; plus
  `safety_guards.allowlist` exact (whitespace-normalized) strings.

## Confirmation Flow (the nonce carrier)

A Bash hook has no advance-args channel, so the discard-gate pattern got a new
carrier: `.claude/state/safety-guard-confirm.json` (under the PROTECTED prefix,
written by the hook via `_write_json_durable` after `ensure_state_dir`). Block
message delivers a `secrets.token_hex(8)` nonce; the agent asks the user, then
re-runs `SAFETY_CONFIRM=<nonce> <command>` (a harmless env-assignment prefix —
the command executes verbatim). Binding is an **exact RAW string match** (D13 —
whitespace normalization was removed because it collapsed quoted-path spaces and
folded newline chains into a different program), TTL 600s, single-use. An invalid/
expired/mismatched confirmation re-blocks with a fresh nonce.

## Sensitivity and the Own-Branch Exception

- `safety_guards.enabled` (default **true** — D1 default-on, the code-cleanliness
  lesson) and `safety_guards.sensitivity`: `off` ≡ `enabled: false`;
  `warn-destructive-only` (default); `warn-all` = destructive set + every
  `BASH_WRITE_PATTERNS` match, own-branch exception disabled (and AI-provider
  wrapper subagents will degrade — their compound Bash trips the write patterns).
  Malformed config degrades to the defaults (fail-enabled, D7).
- **Own-branch exception** (warn-destructive-only only): exempts ONLY
  reflog-recoverable ops — `git reset --hard` and `git push --force` (refspecs ⊆
  {task branch, HEAD}; broad modes `--all/--branches/--mirror/--tags/--prune/
  --delete/-d` incl. combined `-fd` always denied; bare force push vetted via
  `git config push.default` ∈ {unset, simple, current}). Requires current branch ==
  task-state branch AND no remote-tracking ref (`git for-each-ref
  refs/remotes/*/<branch>` empty — any remote counts as shared). Denied on ANY git
  failure and on foreign-repo signals: `cd` segment, `git -C/--git-dir/--work-tree`,
  wrapper chdir flags (D5). Working-tree destruction (checkout/restore `.`, clean)
  is never exempted.

## Fail-Closed Boundary (SC-3)

Everything after the config read runs in one `try/except Exception`: an internal
error blocks with a no-nonce message and NEVER leaks to the hook's outer exit-0
fail-open backstop. Config corruption cannot disable the guard (defaults win);
state-write failure produces the fail-closed block, not an allow.

## Accepted Limitations (v1, documented in three carriers)

Task User Notes + `docs/USAGE_GUIDE.md` "Safety Guards" + `_comment_safety_guards`
in `config.template.json`: shell variables, command substitution, `bash -c`/
`python -c` strings, `find -exec`, base64 piping, unexpanded globs, unlisted
wrappers, quoted chain separators severing trailing-positional rules
(`git checkout "a;b" .` — behavior-pinned by test), persistent shell cwd from a
PREVIOUS Bash call, attached-short chdir spellings escaping only the foreign
marker, `git clean` n-scan false-negative spellings, `-c push.default` override.
Expected FPs (coreutils `truncate`, `git push --force --dry-run`, quoted SQL
keywords) cost one confirmation round-trip — accepted deliberately (CR-12d).

## Drift Guards

- `test/test_safety_guards.py` — 133 subprocess-driven tests: 38 destructive + 22
  safe parametrized commands, allowlist tricks, full confirmation lifecycle
  (raw binding, replay, expiry, mismatch), sensitivity levels, own-branch exception
  (incl. push.default, broad modes, wrapper-chdir), fail-closed paths, ordering
  (subagent bound / bypass wins / protected-path precedence), and behavior pins for
  two documented limitations.

## Related

- [Configuration Schema](pages/entities/configuration-schema.md) — the `safety_guards.*` keys.
- [Hook System](pages/subsystems/hook-system.md) — the enforcement hook this guard lives in.
- [Completion and Git Flow](pages/procedures/completion-and-git-flow.md) — the discard gate whose typed-confirmation pattern this reuses with a new carrier.
