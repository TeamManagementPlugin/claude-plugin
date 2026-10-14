#!/usr/bin/env python3
"""Tests for the CHANGELOG gate in .github/scripts/check_release_integrity.py
(m-plugin-update-changelog).

The release-integrity script must hard-fail (exit 1) when plugin/CHANGELOG.md
is missing or lacks a `## [<version>]` section for the version in plugin.json —
otherwise a release could ship with no notes for the update-notification
mechanism to show. The script resolves ROOT from its own location
(.github/scripts/ -> repo root), so each test copies it into a temp repo
skeleton and runs it there.

Run with: python3 -m pytest test/test_release_integrity_changelog.py -v
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_integrity.py"

VERSION = "1.2.3"


class ReleaseIntegrityChangelogTest(TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        scripts_dir = self.tmp / ".github" / "scripts"
        scripts_dir.mkdir(parents=True)
        shutil.copy2(SCRIPT, scripts_dir / SCRIPT.name)
        (self.tmp / "plugin" / ".claude-plugin").mkdir(parents=True)
        (self.tmp / "plugin" / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "team-management", "version": VERSION}),
            encoding="utf-8")
        (self.tmp / ".claude-plugin").mkdir()
        (self.tmp / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({
                "name": "team-management",
                "metadata": {"version": VERSION},
                "plugins": [{"name": "team-management", "source": "./plugin"}],
            }), encoding="utf-8")
        (self.tmp / "SECURITY.md").write_text(
            "| Version | Supported |\n|---------|-----------|\n| 1.2.x | ✅ |\n",
            encoding="utf-8")
        self.changelog = self.tmp / "plugin" / "CHANGELOG.md"
        self.changelog.write_text(
            f"# Changelog\n\n## [{VERSION}] - 2026-08-07\n\n### Added\n- Things.\n",
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        return subprocess.run(
            [sys.executable, str(self.tmp / ".github" / "scripts" / SCRIPT.name)],
            capture_output=True, text=True, timeout=30,
        )

    def test_passes_with_matching_changelog_section(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fails_when_section_for_version_missing(self):
        self.changelog.write_text(
            "# Changelog\n\n## [0.0.1] - 2020-01-01\n\n- Old.\n", encoding="utf-8")
        result = self._run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CHANGELOG.md", result.stdout)
        self.assertIn(VERSION, result.stdout)

    def test_fails_when_changelog_file_missing(self):
        self.changelog.unlink()
        result = self._run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CHANGELOG.md", result.stdout)


if __name__ == "__main__":
    main()
