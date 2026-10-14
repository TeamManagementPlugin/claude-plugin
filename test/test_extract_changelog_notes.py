#!/usr/bin/env python3
"""Tests for .github/scripts/extract_changelog_notes.py
(m-plugin-update-changelog).

The extractor prints the changelog section BODY for a version (release notes
for the CI release job) and stays silent (exit 0, empty stdout) when the file
or the section is absent so the workflow can fall back to --generate-notes.
Like check_release_integrity.py it resolves ROOT from its own location, so
subprocess tests copy it into a temp repo skeleton.

Run with: python3 -m pytest test/test_extract_changelog_notes.py -v
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "extract_changelog_notes.py"

CHANGELOG = """# Changelog

Preamble text.

## [1.2.3] - 2026-08-07

### Added
- New thing.

### Fixed
- Old bug.

## [1.2.2] - 2026-08-01

### Fixed
- Older bug.
"""


class ExtractChangelogNotesTest(TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        scripts_dir = self.tmp / ".github" / "scripts"
        scripts_dir.mkdir(parents=True)
        shutil.copy2(SCRIPT, scripts_dir / SCRIPT.name)
        self.changelog = self.tmp / "plugin" / "CHANGELOG.md"
        self.changelog.parent.mkdir(parents=True)
        self.changelog.write_text(CHANGELOG, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable,
             str(self.tmp / ".github" / "scripts" / SCRIPT.name), *args],
            capture_output=True, text=True, timeout=30,
        )

    def test_prints_section_body_without_heading_or_neighbours(self):
        result = self._run("1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("New thing", result.stdout)
        self.assertIn("Old bug", result.stdout)
        self.assertNotIn("## [1.2.3]", result.stdout)  # heading excluded
        self.assertNotIn("Older bug", result.stdout)   # neighbour excluded
        self.assertNotIn("Preamble", result.stdout)

    def test_absent_version_empty_exit_zero(self):
        result = self._run("9.9.9")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_changelog_empty_exit_zero(self):
        self.changelog.unlink()
        result = self._run("1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_arg_usage_error(self):
        result = self._run()
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)

    def test_ci_release_job_wired_to_notes_file(self):
        """Drift guard (codex plan-review): the release job must source notes
        via the extractor + notes_args in BOTH gh release create call sites,
        with the --generate-notes fallback kept."""
        ci_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        self.assertIn("extract_changelog_notes.py", ci_text)
        self.assertIn("notes_args=(--generate-notes)", ci_text)
        self.assertEqual(ci_text.count('"${notes_args[@]}"'), 2,
                         "both release branches must use notes_args")

    def test_real_repo_current_version_has_notes(self):
        """Integration guard: the real repo's plugin.json version must extract
        non-empty notes from the real changelog (mirrors the CI gate)."""
        import json
        version = json.loads(
            (REPO_ROOT / "plugin" / ".claude-plugin" / "plugin.json")
            .read_text(encoding="utf-8"))["version"]
        result = subprocess.run(
            [sys.executable, str(SCRIPT), version],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), "empty notes for shipped version")


if __name__ == "__main__":
    main()
