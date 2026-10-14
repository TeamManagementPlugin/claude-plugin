---
title: Plugin Update Notice
tags: [hooks, architecture, config]
created: 2026-08-07
updated: 2026-08-09
sources: [plugin/hooks/update_notice.py, plugin/hooks/session-start.py, plugin/CHANGELOG.md, .github/scripts/check_release_integrity.py, .github/scripts/extract_changelog_notes.py]
---

# Plugin Update Notice

After a plugin update, the next session start announces what changed: the shipped
`plugin/CHANGELOG.md` (Keep a Changelog format) is compared against a persistent
last-seen-version marker, and the update is delivered in **two halves** — a short
banner shown directly to the user, plus the missed `## [X.Y.Z]` sections handed to
Claude as context for elaboration on request. Fires **once per update**
(marker-gated), not once per session.

## Why Two Halves

`additionalContext` reaches the MODEL, never the user. Claude produces no output
until the user sends a message, and an instruction buried in context competes with
that message and with the louder protocol-first blocks around it — so a
context-only notice is delivered *probabilistically* while the marker that gates it
is spent *unconditionally*. The banner rides the universal `systemMessage` hook
field, which Claude Code shows to the user directly; it is the only deterministic
user-facing channel a hook has.

## Mechanism

- **Pure logic**: `build_update_notice in update_notice.py` — importable module
  (the hook itself reads stdin at import and is not unit-testable; same split as
  `hook_utils.py`). Reads the current version from
  `plugin.json` under the plugin root, the stored version from the marker, and the
  section list from the shipped changelog (`extract_changelog_sections`,
  `select_missed_sections` with `stored < v <= current`). On an upgrade it returns
  a dict — `banner`, `context`, `from_version`, `to_version`, `versions` — and
  `None` on every silent path.
- **Banner**: `format_banner` renders at most two ASCII lines naming both versions,
  the release count, and `/team-management:changelog`. ASCII because `json.dumps`
  escapes non-ASCII by default and the line lands in a terminal (Windows consoles
  included). The count covers ALL missed releases even when the context elides
  older ones — a test pins the two halves against disagreement.
- **Wiring**: a block in `session-start.py` after the gemini-replaced warning —
  entirely inside `try/except Exception` (fail-open: the notice must never break
  session start) with `_update_banner` initialised BEFORE the guard so the emit
  site is safe even if the block fails. The context half is APPENDED to the
  `context` string (the hook emits ONE JSON document at the end; printing
  separately would corrupt it); the banner becomes a top-level `systemMessage` key
  set only when truthy, so an ordinary session start stays quiet.
- **Display**: sections newest-first in file order, capped at `MAX_SECTIONS_SHOWN`
  (5) with an "...and N older version(s)" elision line. The context text states
  that a banner *was emitted* (not that the user saw it — a hook cannot observe
  whether the host rendered the field) and asks for a summary in the user's
  language on request, rather than commanding an immediate dump.

## Marker Semantics

Filename `tm-last-seen-version.json` (the `tm-` prefix guards against collisions
with other plugins sharing a directory — user-requested). Two storage tiers:

- `CLAUDE_PLUGIN_DATA/tm-last-seen-version.json` — primary; global per user per
  plugin, survives updates. The dir may not exist yet: `write_last_seen` mkdirs
  parents before the durable write (`_write_json_durable` mkstemps into the
  parent and does NOT create it).
- `.claude/state/tm-last-seen-version.json` — fallback when the env var is unset
  (dev/legacy runs). Semantics shift to once-per-project there.

State transitions in `build_update_notice`: no/unreadable marker → record
current, silent (fresh install announces nothing — except the transition window
below); equal → silent, no write; downgrade → re-sync silently; upgrade → show
missed sections, then sync. The marker is synced BEFORE returning the notice — a
failed write repeats the notice benignly next session, a skipped write would
repeat forever. Missing or undecodable changelog → sync-and-silence (`except
(OSError, ValueError)` — a `UnicodeDecodeError` is a ValueError; catching only
OSError would strand the marker and retry every session, the round-1 review
catch).

## The One-Release Transition Window

The notice mechanism itself shipped in 0.4.5, so every install made before it
has no marker at all — under the plain first-run rule 0.4.5 would be the one
release the mechanism could never announce, and the marker would be spent
announcing nothing. `_MECHANISM_RELEASE_VERSION` (0.4.5) and
`_ASSUMED_BASELINE_VERSION` (0.4.4) open a one-release window: a marker path
with **no directory entry** at exactly the mechanism release reads its stored
version as the last pre-mechanism release and runs the ordinary upgrade path.

- **Absence is `lstat`, not `Path.exists()`** — `exists()` follows symlinks, so
  a dangling symlink would read as absent even though the marker path was
  written (`follow_symlinks=False` is 3.12+). Only `FileNotFoundError` counts as
  absence; every other `OSError`/`ValueError` stays on the silent path.
- **Absent is not corrupt.** A marker that EXISTS but is unreadable (garbage,
  zero bytes, dangling symlink) is no evidence of a pre-mechanism install and
  must not dump a release backlog — it keeps the silent-record behaviour.
- **Tuples, not strings.** The version test compares parsed tuples, so a
  whitespace-padded manifest version still matches.
- **Deliberately not generalized.** At 0.4.6+ a missing marker means a genuinely
  fresh install again; both constants go inert on their own and are deletable
  once 0.4.5 is out of circulation. Going inert *too early* would be invisible —
  no test, no CI signal, indistinguishable from a correct retirement — so
  `test_transition_window_still_targets_the_shipped_version` blocks a version
  bump past the window while the constants still exist, and `pytest.skip`s
  itself once they are deleted (see Release-Pipeline Couplings).
- The window substitutes only the STORED version — every downstream branch
  (equal / downgrade / missing changelog / no matching section) keeps its
  existing semantics, marker sync included.
- Accepted for the window's one release: a truly fresh install also lacks a
  marker, so its banner reads `0.4.4 -> 0.4.5` for a version it never had. One
  release, one section — not worth a second banner shape. The two storage tiers
  are distinct stores, so a dev run switching between them during 0.4.5 can
  legitimately emit once per absent location.

## Release-Pipeline Couplings

- **CI gate**: `check_release_integrity.py` hard-fails when the changelog lacks a
  `## [<plugin.json version>]` section — the notice mechanism is only as good as
  the changelog, so a release cannot ship undocumented.
- **Release notes**: the ci.yml release job runs `extract_changelog_notes.py`
  (prints the section body) into `--notes-file`, falling back to
  `--generate-notes`, in both the create and backfill branches (drift-guarded by
  `test_ci_release_job_wired_to_notes_file`).
- **Browsing**: `/team-management:changelog` (`plugin/commands/changelog.md`)
  presents the shipped changelog on demand.
- **Version-bump gate**: while the transition window exists,
  `test_transition_window_still_targets_the_shipped_version` blocks the next
  version bump — the `test` CI job runs the suite and the `release` job declares
  `needs: [test, validate]`, so bumping `plugin.json` past
  `_MECHANISM_RELEASE_VERSION` hard-fails CI until the window is retargeted or
  deleted. Read this before a bump: a retarget that moves
  `_MECHANISM_RELEASE_VERSION` without also moving `_ASSUMED_BASELINE_VERSION`
  announces two sections instead of one (benign, unguarded).

## Gotchas

- The marker is deliberately the OPPOSITE of the session-start "one-time"
  migration flags: those are cleared on every session start (fire once per
  SESSION); the marker is never cleared there (fires once per UPDATE). Do not
  "fix" it to match the flag pattern.
- "Newest first" trusts changelog FILE ORDER (Keep a Changelog convention), not
  version sorting — a misordered future changelog would mislabel the elision.
- The `## [X.Y.Z]` heading grammar is deliberately triplicated
  (`update_notice.py`, `extract_changelog_notes.py`,
  `check_release_integrity.py`) — CI scripts must not import the plugin tree; a
  grammar change must touch all three. Fenced code blocks are NOT excluded, so
  never put a literal example heading at line start inside a changelog entry.
- Two concurrent session starts can race the marker; worst case the notice shows
  twice (durable atomic write, benign — accepted).
- On a host that ignores the top-level `systemMessage` field the banner silently
  vanishes and the marker is still spent. Unfixable from inside a hook: delivery
  acknowledgement is not observable, which is why the context wording claims only
  what the hook did.
- The marker's `tm-` prefix collides with the command-namespace drift guard's
  `/tm-` pattern. The guard excludes it by NAME (`/tm-(?!last-seen-version)`),
  never by preceding character — a preceding-character lookbehind also drops
  genuine legacy refs in path form (`plugin/commands/tm-wiki-ingest`).

Related: [State Files](pages/entities/state-files.md) (fallback marker row),
[Hooks System](pages/subsystems/hooks-system.md) (SessionStart mechanics).
