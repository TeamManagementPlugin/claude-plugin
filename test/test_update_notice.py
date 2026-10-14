#!/usr/bin/env python3
"""Unit tests for plugin/hooks/update_notice.py (m-plugin-update-changelog).

update_notice.py is the pure-logic module behind the session-start
"[Plugin updated]" notice: semver parsing, `## [X.Y.Z]` section extraction
from the shipped plugin/CHANGELOG.md, missed-section selection
(stored < v <= current), the persistent last-seen marker (JSON, durable
write, mkdir-parents — CLAUDE_PLUGIN_DATA may not exist yet), and the
build_update_notice orchestrator. It lives outside session-start.py because
the hook reads stdin at import and is not unit-importable (hook_utils.py
precedent).

Run with: python3 -m pytest test/test_update_notice.py -v
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugin" / "hooks"))

import update_notice  # noqa: E402


SAMPLE_CHANGELOG = """# Changelog

All notable changes to the team-management plugin are documented here.
The format is based on Keep a Changelog.

## [0.4.5] - 2026-08-07

### Added
- Update notification at session start.

## [0.4.4] - 2026-08-01

### Changed
- Spec traceability and question discipline.

## [0.4.3] - 2026-07-20

### Fixed
- Some earlier fix.
"""


# ---------------------------------------------------------------- parse_version

def test_parse_version_valid():
    assert update_notice.parse_version("0.4.5") == (0, 4, 5)
    assert update_notice.parse_version("10.20.30") == (10, 20, 30)


def test_parse_version_garbage_returns_none():
    for bad in ("abc", "0.4", "0.4.5.6", "", "v0.4.5", "0.4.x", None, 45):
        assert update_notice.parse_version(bad) is None, bad


# ---------------------------------------------------- extract_changelog_sections

def test_extract_sections_order_and_content():
    sections = update_notice.extract_changelog_sections(SAMPLE_CHANGELOG)
    versions = [v for v, _ in sections]
    assert versions == ["0.4.5", "0.4.4", "0.4.3"]
    # Section text carries its own heading line and body, not the neighbours'.
    v045 = sections[0][1]
    assert v045.startswith("## [0.4.5]")
    assert "Update notification" in v045
    assert "Spec traceability" not in v045


def test_extract_sections_ignores_non_semver_headings():
    text = "## [Unreleased]\n- wip\n\n## [0.4.5] - 2026-08-07\n- real\n"
    sections = update_notice.extract_changelog_sections(text)
    assert [v for v, _ in sections] == ["0.4.5"]


def test_extract_sections_empty_input():
    assert update_notice.extract_changelog_sections("") == []
    assert update_notice.extract_changelog_sections("# Changelog\nno sections") == []


# ------------------------------------------------------- select_missed_sections

def _sections():
    return update_notice.extract_changelog_sections(SAMPLE_CHANGELOG)


def test_select_missed_between_stored_and_current():
    missed = update_notice.select_missed_sections(_sections(), (0, 4, 3), (0, 4, 5))
    assert [v for v, _ in missed] == ["0.4.5", "0.4.4"]


def test_select_missed_excludes_stored_and_above_current():
    missed = update_notice.select_missed_sections(_sections(), (0, 4, 4), (0, 4, 4))
    assert missed == []
    missed = update_notice.select_missed_sections(_sections(), (0, 4, 2), (0, 4, 4))
    assert [v for v, _ in missed] == ["0.4.4", "0.4.3"]


def test_select_missed_skips_unparseable_section_versions():
    sections = [("0.4.5", "## [0.4.5]\n- a"), ("bogus", "## [bogus]\n- b")]
    missed = update_notice.select_missed_sections(sections, (0, 4, 0), (0, 4, 5))
    assert [v for v, _ in missed] == ["0.4.5"]


# ------------------------------------------------------------- format_notice

def _fake_sections(n):
    return [(f"0.4.{i}", f"## [0.4.{i}] - 2026-08-07\n- change {i}")
            for i in range(n, 0, -1)]  # newest first


def test_format_notice_basic_shape():
    notice = update_notice.format_notice(
        "0.4.4", "0.4.5", _fake_sections(1), Path("/x/CHANGELOG.md"))
    assert "[Plugin updated]" in notice
    assert "0.4.4" in notice and "0.4.5" in notice
    assert "## [0.4.1]" in notice
    assert "CHANGELOG.md" in notice


def test_format_notice_caps_at_five_sections():
    notice = update_notice.format_notice(
        "0.0.1", "0.4.7", _fake_sections(7), Path("/x/CHANGELOG.md"))
    # Newest five shown (0.4.7 .. 0.4.3), two older elided with a count line.
    for shown in ("0.4.7", "0.4.6", "0.4.5", "0.4.4", "0.4.3"):
        assert f"## [{shown}]" in notice
    for elided in ("0.4.2", "0.4.1"):
        assert f"## [{elided}]" not in notice
    assert "2 older version" in notice


def test_format_notice_no_elision_line_when_five_or_fewer():
    notice = update_notice.format_notice(
        "0.0.1", "0.4.5", _fake_sections(5), Path("/x/CHANGELOG.md"))
    assert "older version" not in notice


def test_format_notice_defers_to_the_banner_instead_of_commanding_a_dump():
    # The old wording ordered an immediate "present these release notes", which
    # the model routinely skipped in favour of the user's first request — and the
    # one-shot marker was spent regardless. The banner is now the delivery
    # guarantee, so the context asks for a summary ON REQUEST.
    notice = update_notice.format_notice(
        "0.4.4", "0.4.5", _fake_sections(1), Path("/x/CHANGELOG.md"))
    assert "Briefly present these release notes" not in notice
    assert "banner" in notice.lower()
    assert "summarize" in notice.lower()


# ------------------------------------------------------------- format_banner

def test_format_banner_names_both_versions_and_release_count():
    banner = update_notice.format_banner("0.4.3", "0.4.5", _fake_sections(2))
    assert "0.4.3" in banner and "0.4.5" in banner
    assert "2 releases" in banner
    assert "/team-management:changelog" in banner


def test_format_banner_singular_release():
    banner = update_notice.format_banner("0.4.4", "0.4.5", _fake_sections(1))
    assert "1 release" in banner
    assert "1 releases" not in banner


def test_format_banner_counts_all_missed_releases_not_the_shown_cap():
    # The context caps at MAX_SECTIONS_SHOWN (5) and elides the rest, but the
    # banner must report the TRUE number of missed releases — otherwise the two
    # halves of the hybrid delivery would contradict each other.
    missed = _fake_sections(9)
    banner = update_notice.format_banner("0.0.1", "0.4.9", missed)
    context = update_notice.format_notice(
        "0.0.1", "0.4.9", missed, Path("/x/CHANGELOG.md"))
    assert "9 releases" in banner
    assert "4 older version" in context


def test_format_banner_is_ascii_and_at_most_two_lines():
    # json.dumps escapes non-ASCII by default and the banner is rendered in a
    # terminal (Windows consoles included) — keep it plain and short.
    banner = update_notice.format_banner("0.4.3", "0.4.5", _fake_sections(9))
    assert banner.isascii(), banner
    assert len(banner.splitlines()) <= 2, banner


# --------------------------------------------------- read/write last-seen marker

def test_marker_roundtrip_creates_parent_dirs(tmp_path):
    marker = tmp_path / "deep" / "nested" / "last-seen-version.json"
    assert update_notice.write_last_seen(marker, "0.4.5") is True
    assert update_notice.read_last_seen(marker) == "0.4.5"
    # Durable-write contract: valid JSON of shape {"version": ...}.
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["version"] == "0.4.5"


def test_read_last_seen_missing_file(tmp_path):
    assert update_notice.read_last_seen(tmp_path / "absent.json") is None


def test_read_last_seen_malformed(tmp_path):
    p = tmp_path / "m.json"
    for content in ("not json", "[1, 2]", "{}", '{"version": 42}'):
        p.write_text(content, encoding="utf-8")
        assert update_notice.read_last_seen(p) is None, content


# ------------------------------------------------------------ build_update_notice

def _make_plugin_root(tmp_path, version="0.4.5", changelog=SAMPLE_CHANGELOG):
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "team-management", "version": version}),
        encoding="utf-8")
    if changelog is not None:
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return root


def test_build_absent_marker_at_mechanism_release_emits_notice(tmp_path):
    # One-release transition window: the notice mechanism shipped in 0.4.5, so
    # every pre-0.4.5 install has no marker at all — without this branch, 0.4.5
    # would be the one release the mechanism could never announce.
    root = _make_plugin_root(tmp_path)
    marker = tmp_path / "data" / "last-seen-version.json"
    notice = update_notice.build_update_notice(root, marker)
    assert notice is not None
    assert notice["from_version"] == "0.4.4"
    assert notice["to_version"] == "0.4.5"
    assert notice["versions"] == ["0.4.5"]
    assert "## [0.4.5]" in notice["context"]
    assert "## [0.4.4]" not in notice["context"]
    assert "0.4.4" in notice["banner"] and "0.4.5" in notice["banner"]
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_absent_marker_padded_version_still_opens_window(tmp_path):
    # The window compares parsed TUPLES, not the raw manifest string, so a
    # whitespace-padded version still matches the mechanism release.
    root = _make_plugin_root(tmp_path, version=" 0.4.5 ")
    marker = tmp_path / "data" / "last-seen-version.json"
    notice = update_notice.build_update_notice(root, marker)
    assert notice is not None
    assert notice["from_version"] == "0.4.4"
    assert notice["versions"] == ["0.4.5"]


def test_build_absent_marker_after_mechanism_release_is_silent(tmp_path):
    # The window is ONE release wide: past it, a missing marker means a
    # genuinely fresh install again and must stay silent.
    root = _make_plugin_root(tmp_path, version="0.4.6")
    marker = tmp_path / "data" / "last-seen-version.json"
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.6"


def test_build_absent_marker_before_mechanism_release_is_silent(tmp_path):
    root = _make_plugin_root(tmp_path, version="0.4.4")
    marker = tmp_path / "data" / "last-seen-version.json"
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.4"


def test_build_update_emits_notice_and_syncs_marker(tmp_path):
    root = _make_plugin_root(tmp_path)
    marker = tmp_path / "last-seen-version.json"
    update_notice.write_last_seen(marker, "0.4.3")
    notice = update_notice.build_update_notice(root, marker)
    assert notice is not None
    # Hybrid delivery contract: a short banner for the user (systemMessage) plus
    # the full sections for the model (additionalContext).
    assert set(notice) == {"banner", "context", "from_version", "to_version",
                           "versions"}
    assert notice["from_version"] == "0.4.3"
    assert notice["to_version"] == "0.4.5"
    assert notice["versions"] == ["0.4.5", "0.4.4"]
    assert "0.4.3" in notice["banner"] and "0.4.5" in notice["banner"]
    assert "[Plugin updated]" in notice["context"]
    assert "## [0.4.5]" in notice["context"] and "## [0.4.4]" in notice["context"]
    # stored version's own section is not repeated
    assert "## [0.4.3]" not in notice["context"]
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_equal_version_silent(tmp_path):
    root = _make_plugin_root(tmp_path)
    marker = tmp_path / "last-seen-version.json"
    update_notice.write_last_seen(marker, "0.4.5")
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_downgrade_resyncs_marker_silently(tmp_path):
    root = _make_plugin_root(tmp_path, version="0.4.4")
    marker = tmp_path / "last-seen-version.json"
    update_notice.write_last_seen(marker, "0.9.9")
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.4"


def test_build_malformed_marker_first_run_semantics(tmp_path):
    # Guards the absent-vs-corrupt split: the version here IS the mechanism
    # release, so only the missing DIRECTORY ENTRY opens the transition window.
    # A marker that exists but holds garbage is no evidence of a pre-mechanism
    # install — it stays on the silent-record path.
    root = _make_plugin_root(tmp_path)
    marker = tmp_path / "last-seen-version.json"
    marker.write_text("not json at all", encoding="utf-8")
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_empty_marker_file_is_silent(tmp_path):
    # Zero bytes: read_last_seen returns None like the garbage case, but the
    # directory entry exists, so the window must stay shut.
    root = _make_plugin_root(tmp_path)
    marker = tmp_path / "last-seen-version.json"
    marker.write_text("", encoding="utf-8")
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_dangling_symlink_marker_is_silent(tmp_path):
    # The reason the probe is lstat() and not Path.exists(): exists() follows
    # symlinks, so a broken link would read as absence and wrongly open the
    # window even though the marker path was written.
    root = _make_plugin_root(tmp_path)
    marker = tmp_path / "last-seen-version.json"
    try:
        marker.symlink_to(tmp_path / "nowhere.json")
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    assert update_notice.build_update_notice(root, marker) is None
    # Silent AND recorded — the full silent-path contract, so an implementation
    # that skipped the record attempt could not pass on the no-notice half alone.
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_missing_changelog_syncs_silently(tmp_path):
    root = _make_plugin_root(tmp_path, changelog=None)
    marker = tmp_path / "last-seen-version.json"
    update_notice.write_last_seen(marker, "0.4.3")
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_invalid_utf8_changelog_syncs_silently(tmp_path):
    # A UnicodeDecodeError is a ValueError, not an OSError — it must follow the
    # same sync-and-silence path as a missing changelog, not escape and leave
    # the marker stale (which would retry the failing read every session).
    root = _make_plugin_root(tmp_path, changelog=None)
    (root / "CHANGELOG.md").write_bytes(b"\xff\xfe broken \xff bytes")
    marker = tmp_path / "last-seen-version.json"
    update_notice.write_last_seen(marker, "0.4.3")
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_no_missed_sections_syncs_silently(tmp_path):
    # Version bumped to 0.5.0 but the changelog has no section newer than 0.4.5:
    # nothing to show — sync the marker so it does not retry forever.
    root = _make_plugin_root(tmp_path, version="0.5.0")
    marker = tmp_path / "last-seen-version.json"
    update_notice.write_last_seen(marker, "0.4.5")
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.5.0"


def test_build_window_missing_changelog_syncs_silently(tmp_path):
    # The window only substitutes the stored version — every downstream silent
    # path must behave exactly as it does on an ordinary upgrade, marker sync
    # included, or the notice would retry the failing read every session.
    root = _make_plugin_root(tmp_path, changelog=None)
    marker = tmp_path / "data" / "last-seen-version.json"
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_window_invalid_utf8_changelog_syncs_silently(tmp_path):
    root = _make_plugin_root(tmp_path, changelog=None)
    (root / "CHANGELOG.md").write_bytes(b"\xff\xfe broken \xff bytes")
    marker = tmp_path / "data" / "last-seen-version.json"
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_build_window_no_matching_section_syncs_silently(tmp_path):
    # Window opens (no marker, version 0.4.5) but the changelog carries nothing
    # newer than the assumed baseline: stay quiet, still sync.
    changelog = "# Changelog\n\n## [0.4.4] - 2026-08-01\n\n### Changed\n- Older.\n"
    root = _make_plugin_root(tmp_path, changelog=changelog)
    marker = tmp_path / "data" / "last-seen-version.json"
    assert update_notice.build_update_notice(root, marker) is None
    assert update_notice.read_last_seen(marker) == "0.4.5"


def test_transition_window_still_targets_the_shipped_version():
    """Fail-on-purpose drift guard for the one-release window.

    `_MECHANISM_RELEASE_VERSION` must equal the version this code actually
    ships under. Bump `plugin.json` past it while the constants still exist and
    the window goes inert for everyone — a pre-mechanism install has no marker,
    the version no longer matches, and the release the window exists to announce
    is never announced. That failure is INVISIBLE: no test fails, no CI job
    fails, and the behaviour is indistinguishable from the window having
    correctly retired. So assert the two stay in step; when they stop, the fix
    is an explicit decision — retarget the window, or delete it (both constants,
    the branch in `build_update_notice`, and this guard), which is the intended
    end state once the window release is out of circulation.
    """
    window = getattr(update_notice, "_MECHANISM_RELEASE_VERSION", None)
    if window is None:
        pytest.skip("transition window removed — this guard has retired with it")
    manifest = json.loads(
        (REPO_ROOT / "plugin" / ".claude-plugin" / "plugin.json")
        .read_text(encoding="utf-8"))
    shipped = update_notice.parse_version(manifest["version"])
    assert shipped is not None, (
        f"unparseable version in plugin.json: {manifest['version']!r}")
    assert shipped <= update_notice.parse_version(window), (
        f"plugin.json ships {manifest['version']} but the transition window is "
        f"pinned to {window} — the window is now inert and announces nothing. "
        "Retarget it, or delete the window (both constants, the branch in "
        "build_update_notice, and this guard).")


def test_build_unreadable_plugin_version_no_marker_write(tmp_path):
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("broken{", encoding="utf-8")
    marker = tmp_path / "last-seen-version.json"
    assert update_notice.build_update_notice(root, marker) is None
    assert not marker.exists()
