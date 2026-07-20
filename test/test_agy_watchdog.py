#!/usr/bin/env python3
"""Drift-guard for the agy-cli wrapper's shell-native watchdog (M9).

The agy wrapper is a markdown agent prompt — its bash snippet cannot be
imported, so this guard asserts the watchdog block survives edits to
`plugin/agents/agy-cli.md`. Live behaviour was verified manually
with a stub `agy` (see the task work log of m-harden-ai-provider-layer).

Run with: python3 -m pytest test/test_agy_watchdog.py -v
"""

import re
from pathlib import Path
from unittest import TestCase, main

PROJECT_ROOT = Path(__file__).parent.parent
AGY_CLI_MD = PROJECT_ROOT / "plugin" / "agents" / "agy-cli.md"


class AgyWatchdogDriftGuardTest(TestCase):
    """The no-gtimeout/no-timeout fallback must carry its own watchdog.

    Without it, a hang mode that agy's --print-timeout does not catch runs
    unbounded (live evidence: ~14 min hang on macOS without coreutils).
    """

    @classmethod
    def setUpClass(cls):
        cls.text = AGY_CLI_MD.read_text(encoding="utf-8")

    def test_else_branch_backgrounds_agy(self):
        """agy must run in the background (&) so the watchdog can kill it."""
        self.assertIn('AGY_PID=$!', self.text)

    def test_watchdog_subshell_present(self):
        """A detached (sleep N; kill $AGY_PID) subshell is the backstop."""
        self.assertRegex(
            self.text,
            re.compile(r'\(\s*sleep\s+330\s*;?\s*&?&?\s*kill\s+"\$AGY_PID"', re.S),
        )

    def test_watchdog_is_cleaned_up_after_normal_exit(self):
        self.assertIn('WATCHDOG_PID=$!', self.text)
        self.assertIn('kill "$WATCHDOG_PID"', self.text)

    def test_output_captured_to_tempfile_with_trap(self):
        """Backgrounding loses direct stdout — output goes through a temp file
        that a trap removes on exit."""
        self.assertIn('AGY_OUT=$(mktemp)', self.text)
        self.assertIn("trap 'rm -f \"$AGY_OUT\"' EXIT", self.text)

    def test_timeout_unavailable_message_documented(self):
        """Section 4 must tell the wrapper what to reply on a watchdog kill."""
        self.assertIn("timed out after 330s (watchdog)", self.text)

    def test_watchdog_stdio_detached(self):
        """The watchdog subshell must not inherit the script's stdout/stderr —
        an orphaned `sleep` holding the output pipe can stall harnesses that
        wait for EOF (code-review W2; not reproduced on this host, but the
        redirect removes the risk class for free)."""
        self.assertRegex(
            self.text,
            re.compile(r'kill\s+"\$AGY_PID"[^)]*\)\s*>/dev/null\s+2>&1\s+&'),
        )

    def test_exit_code_surfaced(self):
        """Shell state does not persist across the agent's Bash calls — the
        snippet must PRINT $AGY_RC for the rc>=128 detection in Section 4 to
        be actionable (code-review W3)."""
        self.assertIn('[wrapper] agy exit code:', self.text)
        self.assertIn('"$AGY_RC" -ge 128', self.text)

    def test_fallback_surfaces_sub_128_failures(self):
        """A sub-128 non-zero agy exit on the fallback branch must ALSO print a
        stderr marker; the false `if [ "$AGY_RC" -ge 128 ]` returns 0, so
        without the `elif` the snippet exits 0 and masks the failure (parity
        with the codex-cli fix, codex round-2 P2)."""
        self.assertIn('elif [ "$AGY_RC" -ne 0 ]', self.text)
        self.assertIn("(non-zero", self.text)


class AgyInvocationContractTest(TestCase):
    """The macOS fix (m-fix-agy-headless-review): drop --sandbox (it blocks git),
    bind the workspace with --add-dir, run --dangerously-skip-permissions
    CONTAINED by the project-local read-only gate (preflight-verified)."""

    @classmethod
    def setUpClass(cls):
        cls.text = AGY_CLI_MD.read_text(encoding="utf-8")

    def test_both_branches_add_dir(self):
        """--add-dir "$PWD" binds the repo as agy's workspace on BOTH watchdog
        branches (else agy runs git from a default dir → 'not a git repository',
        and the .agents/ gate is not discovered)."""
        self.assertEqual(self.text.count('--add-dir "$PWD" \\'), 2)

    def test_both_branches_skip_permissions(self):
        """--dangerously-skip-permissions gets past the headless soft-deny on
        BOTH branches (it is the only reliable way past print-mode soft-deny)."""
        self.assertEqual(self.text.count("--dangerously-skip-permissions \\"), 2)

    def test_no_sandbox_flag_in_invocation(self):
        """--sandbox must NOT be passed (it breaks git's $TMPDIR cache on macOS).
        It may still be MENTIONED in prose explaining the removal."""
        self.assertNotIn("--sandbox \\", self.text)
        self.assertNotIn("agy --sandbox", self.text)

    def test_print_timeout_still_passed(self):
        self.assertIn("--print-timeout 300s", self.text)

    def test_gate_preflight_present(self):
        """The wrapper must refuse to run uncontained: a preflight checks the
        project's read-only gate before invoking skip-permissions."""
        self.assertIn("team-management-readonly-gate", self.text)
        self.assertIn("agy-readonly-gate.py", self.text)
        self.assertIn("read-only gate not deployed", self.text)

    def test_env_scrub_preserved(self):
        self.assertEqual(self.text.count('env -i PATH="$PATH" HOME="$HOME" agy'), 2)


if __name__ == "__main__":
    main()
