#!/usr/bin/env python3
"""Pure logic behind the session-start "[Plugin updated]" notice.

session-start.py compares the shipped plugin version (plugin.json) against a
PERSISTENT last-seen marker and, when the version moved forward, appends the
missed `## [X.Y.Z]` sections of the shipped plugin/CHANGELOG.md to the hook's
additionalContext — once per update, not once per session (the marker is never
cleared at session start, unlike the per-session migration flags).

This lives outside session-start.py because the hook reads stdin at import and
is not unit-importable (hook_utils.py precedent: pure logic that needs unit
tests lives in an importable sibling). Every function degrades silently — the
caller wraps the call in try/except anyway, but foreseeable bad inputs
(malformed marker, missing changelog, unparseable version) are handled here
explicitly so their semantics are tested, not accidental.

Marker semantics (m-plugin-update-changelog):
- first run (no readable marker): record the current version, say nothing —
  a fresh install has no "update" to announce. ONE exception: the transition
  window below;
- current == stored: nothing to do;
- current < stored (downgrade): re-sync the marker silently;
- current > stored: show the sections with stored < v <= current (newest
  first, capped at MAX_SECTIONS_SHOWN), then sync the marker. A failed marker
  write is not fatal — the notice may benignly repeat next session.

Transition window (m-fix-update-notice-first-run-window): this mechanism
shipped in _MECHANISM_RELEASE_VERSION, so every install made before it has no
marker at all — which would make that one release the only release the
mechanism could never announce. When the marker path has NO DIRECTORY ENTRY
*and* the installed version is exactly that release, the stored version is read
as _ASSUMED_BASELINE_VERSION and the ordinary upgrade path runs. A marker that
EXISTS but is unreadable (garbage, empty, dangling symlink) is NOT absence — it
is no evidence of a pre-mechanism install and stays on the silent path.
Deliberately not generalized: at the next release a missing marker means a
genuinely fresh install again and both constants go inert on their own (delete
them once that release is out of circulation).
"""

import json
import re
from pathlib import Path

from shared_state import _write_json_durable

# Strict MAJOR.MINOR.PATCH — anything else (pre-release tags, partial
# versions) is treated as unparseable and the notice machinery stays silent.
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Keep a Changelog version heading: `## [X.Y.Z]` optionally followed by a date.
_SECTION_HEADING_RE = re.compile(r"^##\s*\[([^\]]+)\]", re.MULTILINE)

MAX_SECTIONS_SHOWN = 5

# One-release transition window (m-fix-update-notice-first-run-window).
# This notice mechanism shipped in 0.4.5, so every install made before it has
# no marker at all — making 0.4.5 the one release the mechanism could never
# announce. For that release ONLY, a marker path with no directory entry is
# read as the last pre-mechanism version. Deliberately NOT generalized: at
# 0.4.6+ a missing marker means a genuinely fresh install again and both
# constants go inert. Delete them once 0.4.5 is out of circulation.
_MECHANISM_RELEASE_VERSION = "0.4.5"
_ASSUMED_BASELINE_VERSION = "0.4.4"


def _marker_is_absent(marker_path):
    """True only when NO directory entry exists at marker_path.

    `lstat`, not `Path.exists()`: exists() follows symlinks, so a dangling
    symlink would read as absent even though the marker was written
    (`follow_symlinks=False` only exists from 3.12). Anything other than
    FileNotFoundError — permission denied, a file where a parent directory
    should be, an embedded NUL — is NOT absence and stays on the standard
    silent-record path.
    """
    try:
        Path(marker_path).lstat()
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return False


def parse_version(value):
    """`"0.4.5"` -> `(0, 4, 5)`; anything non-conforming -> None."""
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def extract_changelog_sections(text):
    """Return ordered `[(version_str, section_text)]` from changelog text.

    A section spans its `## [X.Y.Z]` heading line through the line before the
    next `## [...]` heading (file order preserved — Keep a Changelog files are
    newest-first). Headings whose bracket content is not MAJOR.MINOR.PATCH
    (e.g. `## [Unreleased]`) are skipped entirely.
    """
    matches = list(_SECTION_HEADING_RE.finditer(text))
    sections = []
    for i, match in enumerate(matches):
        version = match.group(1).strip()
        if parse_version(version) is None:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((version, text[match.start():end].rstrip()))
    return sections


def select_missed_sections(sections, stored_tuple, current_tuple):
    """Filter to sections with stored < v <= current (file order preserved)."""
    missed = []
    for version, body in sections:
        v = parse_version(version)
        if v is None:
            continue
        if stored_tuple < v <= current_tuple:
            missed.append((version, body))
    return missed


def read_last_seen(marker_path):
    """Read the stored version string, or None for missing/malformed markers."""
    try:
        data = json.loads(Path(marker_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    return version if isinstance(version, str) and version else None


def write_last_seen(marker_path, version):
    """Durably record the version; create parent dirs (CLAUDE_PLUGIN_DATA may
    not exist yet — _write_json_durable's mkstemp needs the parent). Best-effort:
    returns False instead of raising."""
    try:
        marker_path = Path(marker_path)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_durable(marker_path, {"version": version})
        return True
    except (OSError, ValueError):
        return False


def format_banner(stored_version, current_version, missed_sections):
    """Render the short user-facing banner (the hook's `systemMessage`).

    This is the DELIVERY GUARANTEE: `additionalContext` only reaches the model,
    which cannot speak first and may spend its first reply on the user's actual
    request — while the one-shot marker is already synced. Kept ASCII-only and
    at most two lines: `json.dumps` escapes non-ASCII by default and this lands
    in a terminal (Windows consoles included).
    """
    count = len(missed_sections)
    plural = "s" if count != 1 else ""
    return (
        f"team-management updated: {stored_version} -> {current_version} "
        f"({count} release{plural}).\n"
        "Ask what's new, or run /team-management:changelog for full notes."
    )


def format_notice(stored_version, current_version, missed_sections, changelog_path):
    """Render the additionalContext block for a set of missed sections."""
    shown = missed_sections[:MAX_SECTIONS_SHOWN]
    elided = len(missed_sections) - len(shown)
    lines = [
        f"[Plugin updated] team-management {stored_version} -> {current_version}. "
        "A one-line banner about this update was emitted to the user. If they "
        "ask what changed - or if your first reply has room for it - summarize "
        "these notes in their language:",
        "",
    ]
    for _version, body in shown:
        lines.append(body)
        lines.append("")
    if elided > 0:
        plural = "s" if elided != 1 else ""
        lines.append(f"...and {elided} older version{plural} - see the full changelog.")
    lines.append(f"Full changelog: {changelog_path}")
    return "\n".join(lines)


def _read_current_version(plugin_root):
    try:
        manifest = json.loads(
            (Path(plugin_root) / ".claude-plugin" / "plugin.json")
            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    version = manifest.get("version")
    return version if isinstance(version, str) else None


def build_update_notice(plugin_root, marker_path):
    """Orchestrator: return the hybrid-delivery payload, or None when there is
    nothing to announce.

    On an upgrade returns a dict: `banner` (short, user-facing — the hook emits
    it as the top-level `systemMessage`), `context` (full missed sections for
    `additionalContext`), `from_version`, `to_version`, `versions`. Syncs the
    marker as a side effect (see the module docstring for the full semantics).
    """
    plugin_root = Path(plugin_root)
    current_version = _read_current_version(plugin_root)
    current_tuple = parse_version(current_version)
    if current_tuple is None:
        return None  # nothing sensible to record — leave the marker alone

    stored_version = read_last_seen(marker_path)
    stored_tuple = parse_version(stored_version)
    if stored_tuple is None:
        if (current_tuple == parse_version(_MECHANISM_RELEASE_VERSION)
                and _marker_is_absent(marker_path)):
            # Transition window: announce the release that introduced the
            # notice to installs that predate the marker entirely.
            stored_version = _ASSUMED_BASELINE_VERSION
            stored_tuple = parse_version(stored_version)
        else:
            # First run or unreadable marker: record silently.
            write_last_seen(marker_path, current_version)
            return None
    if current_tuple == stored_tuple:
        return None
    if current_tuple < stored_tuple:
        write_last_seen(marker_path, current_version)
        return None

    changelog_path = plugin_root / "CHANGELOG.md"
    try:
        changelog_text = changelog_path.read_text(encoding="utf-8")
    # ValueError covers UnicodeDecodeError — same sync-and-silence semantics as
    # a missing file, and consistent with the module's sibling readers.
    except (OSError, ValueError):
        write_last_seen(marker_path, current_version)
        return None

    missed = select_missed_sections(
        extract_changelog_sections(changelog_text), stored_tuple, current_tuple)
    # Sync BEFORE returning: a failed write only means a benign repeat next
    # session, while a skipped write would repeat forever.
    write_last_seen(marker_path, current_version)
    if not missed:
        return None
    return {
        "banner": format_banner(stored_version, current_version, missed),
        "context": format_notice(stored_version, current_version, missed,
                                 changelog_path),
        "from_version": stored_version,
        "to_version": current_version,
        "versions": [version for version, _body in missed],
    }
