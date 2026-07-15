#!/usr/bin/env python3
"""Drift-guard for the codex-cli wrapper's shell-native watchdog.

The codex wrapper is a markdown agent prompt — its bash snippet cannot be
imported, so this guard asserts the watchdog block survives edits to
`plugin/agents/codex-cli.md`. Live behaviour is verified by the red-green
harness in the task work log of m-fix-ai-provider-wrapper-timeout.

codex `exec`/`review` has NO internal deadline (unlike agy's `--print-timeout`),
so on a host without gtimeout/timeout the ONLY backstop is this shell-native
watchdog. Without it codex runs unbounded (live evidence: a ~29-minute run on a
macOS host without coreutils). This guard mirrors `test/test_agy_watchdog.py`.

Run with: python3 -m pytest test/test_codex_watchdog.py -v
"""

import re
from pathlib import Path
from unittest import TestCase, main

PROJECT_ROOT = Path(__file__).parent.parent
CODEX_CLI_MD = PROJECT_ROOT / "plugin" / "agents" / "codex-cli.md"


class CodexWatchdogDriftGuardTest(TestCase):
    """The no-gtimeout/no-timeout fallback must carry its own watchdog.

    Without it, codex (which has no internal deadline) runs unbounded.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = CODEX_CLI_MD.read_text(encoding="utf-8")

    def test_else_branch_backgrounds_codex(self):
        """codex must run in the background (&) so the watchdog can kill it."""
        self.assertIn("CODEX_PID=$!", self.text)

    def test_codex_backgrounded_to_tempfile(self):
        """Backgrounding loses direct stdout — codex output is redirected to a
        temp file with a trailing `&` (the background marker)."""
        self.assertRegex(
            self.text,
            re.compile(r'>"\$CODEX_OUT"\s+2>&1\s+&'),
        )

    def test_watchdog_subshell_present(self):
        """A detached (sleep 300; kill $CODEX_PID) subshell is the backstop."""
        self.assertRegex(
            self.text,
            re.compile(r'\(\s*sleep\s+300\s*;?\s*&?&?\s*kill\s+"\$CODEX_PID"', re.S),
        )

    def test_watchdog_is_cleaned_up_after_normal_exit(self):
        self.assertIn("WATCHDOG_PID=$!", self.text)
        self.assertIn('kill "$WATCHDOG_PID"', self.text)

    def test_output_captured_to_tempfile_with_trap(self):
        """The fallback captures output to a temp file that a trap removes."""
        self.assertIn("CODEX_OUT=$(mktemp)", self.text)
        self.assertIn("trap 'rm -f \"$CODEX_OUT\"' EXIT", self.text)

    def test_schema_branch_uses_combined_trap(self):
        """The --output-schema variant already owns a `trap ... "$SCHEMA" ... EXIT`.
        A 2nd `trap ... EXIT` REPLACES the first, so the fallback's trap must
        clean up BOTH the schema tmpfile and the output tmpfile."""
        self.assertIn("trap 'rm -f \"$SCHEMA\" \"$CODEX_OUT\"' EXIT", self.text)

    def test_timeout_unavailable_message_documented(self):
        """Section 4 must tell the wrapper what to reply on a watchdog kill."""
        self.assertIn("timed out after 300s (watchdog)", self.text)

    def test_watchdog_stdio_detached(self):
        """The watchdog subshell must not inherit the script's stdout/stderr —
        an orphaned `sleep` holding the output pipe can stall harnesses that
        wait for EOF."""
        self.assertRegex(
            self.text,
            re.compile(r'kill\s+"\$CODEX_PID"[^)]*\)\s*>/dev/null\s+2>&1\s+&'),
        )

    def test_exit_code_surfaced(self):
        """Shell state does not persist across the agent's Bash calls — the
        snippet must PRINT $CODEX_RC for the rc>=128 detection in Section 4 to
        be actionable."""
        self.assertIn("[wrapper] codex exit code:", self.text)
        self.assertIn('"$CODEX_RC" -ge 128', self.text)

    def test_fallback_surfaces_sub_128_failures(self):
        """A sub-128 non-zero codex exit on the fallback branch (auth error,
        missing binary=127, schema error) must ALSO print a stderr marker. The
        `if [ "$CODEX_RC" -ge 128 ]` with no else returns 0 when false, so
        without the `elif` the snippet exits 0 and MASKS the failure, breaking
        the graceful `codex review unavailable:` contract (codex round-2 P2)."""
        self.assertIn('elif [ "$CODEX_RC" -ne 0 ]', self.text)
        self.assertIn("(non-zero", self.text)

    def test_env_scrub_preserved_on_all_codepaths(self):
        """SEC-003: codex must run under the `env -i PATH HOME` scrub on EVERY
        codepath — the gtimeout primary AND the shell-native fallback, in BOTH
        the §2 skeleton and the §3 --output-schema variant (4 total). A regex
        anchored to the single-line §2 fallback would pass on §2 alone and miss
        a dropped scrub on §3's multi-line fallback, so assert the count."""
        self.assertGreaterEqual(
            self.text.count('env -i PATH="$PATH" HOME="$HOME" codex'), 4,
            "the env -i PATH/HOME scrub must wrap codex on all four codepaths "
            "(§2 primary + fallback, §3 primary + fallback); a count below 4 "
            "means a SEC-003 scrub was dropped from one path.",
        )

    def test_watchdog_present_in_both_invocation_sites(self):
        """The watchdog must be added to BOTH codex invocation sites — the §2
        skeleton AND the §3 --output-schema variant. `CODEX_PID=$!` therefore
        appears at least twice; a single occurrence means one site was missed."""
        self.assertGreaterEqual(
            self.text.count("CODEX_PID=$!"), 2,
            "watchdog present in only one invocation site — the --output-schema "
            "variant (§3) or the skeleton (§2) was not updated.",
        )


if __name__ == "__main__":
    main()
