#!/usr/bin/env python3
"""Tests for the codex-cli and agy-cli wrapper agents.

The codex wrapper was refactored to a thin pass-through (h-ai-providers-foundation);
the agy wrapper replaced the retired gemini wrapper (m-replace-gemini-with-agy).
The tests guard the contract:
- codex-cli MUST NOT carry the Write tool in its frontmatter.
- Neither wrapper carries the old review-shape template markers
  (## Critical Issues / ## Warnings) — those are the caller's concern now.
- agy-cli always passes --sandbox + --print-timeout, never
  --dangerously-skip-permissions, and carries the git-status mutation check
  (detect & report) with the `agy review WARNING:` prefix contract.
- Shell-injection via task name passed inside a heredoc must not trigger
  expansion (validates the literal-EOF `<<'PROMPT'` quoting policy).

Run with: python3 -m pytest test/test_wrappers.py -v
"""

import subprocess
import sys
from pathlib import Path
from unittest import TestCase, main

PROJECT_ROOT = Path(__file__).parent.parent
AGENTS_DIR = PROJECT_ROOT / "plugin" / "agents"


def _parse_frontmatter(path: Path) -> dict:
    """Parse a markdown file's YAML-ish frontmatter (key: value lines only)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    block = parts[1]
    out = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


class CodexWrapperTest(TestCase):

    def setUp(self):
        self.path = AGENTS_DIR / "codex-cli.md"
        self.text = self.path.read_text(encoding="utf-8")
        self.fm = _parse_frontmatter(self.path)

    def test_codex_no_write_tool(self):
        """The Write tool must NOT appear in the codex-cli tools frontmatter.

        Previously the wrapper carried Write to materialise the schema tmpfile;
        the new flow uses Bash heredoc redirection instead, so Write is
        unnecessary and would broaden the wrapper's blast radius."""
        tools = self.fm.get("tools", "")
        tool_list = [t.strip() for t in tools.split(",")]
        self.assertNotIn("Write", tool_list,
                         f"Write should not be in tools frontmatter (got: {tools})")
        # Sanity — the minimum expected tools are present
        for required in ("Read", "Bash", "Grep", "Glob"):
            self.assertIn(required, tool_list, f"missing required tool {required}")

    def test_codex_no_review_template_markers(self):
        """The hardcoded `## Critical Issues` / `## Warnings` template was
        removed — the caller (pre_func) now owns the output shape."""
        self.assertNotIn("## Critical Issues", self.text)
        self.assertNotIn("## Warnings", self.text)
        self.assertNotIn("## Notes (", self.text)

    def test_codex_has_timeout_fallback(self):
        """gtimeout/timeout fallback documented; explicit kill-after grace."""
        self.assertIn("gtimeout", self.text)
        self.assertIn("--kill-after=10s", self.text)
        self.assertIn("300s", self.text)

    def test_codex_timeout_fallback_has_conditional(self):
        """Codex Warning #1 regression: every codex invocation must branch
        on `[ -n "$TIMEOUT_CMD" ]` so the empty-fallback path invokes codex
        directly (not as `--kill-after=10s ... codex ...`)."""
        self.assertIn('if [ -n "$TIMEOUT_CMD" ]', self.text)
        # The direct-invocation branch must exist (else clause without $TIMEOUT_CMD prefix)
        self.assertIn("else", self.text)

    def test_codex_has_trap_cleanup(self):
        self.assertIn("trap", self.text)
        self.assertIn("rm -f", self.text)

    def test_codex_documents_sigkill_leak_caveat(self):
        self.assertIn("SIGKILL", self.text)

    def test_codex_preserves_read_only_sandbox_boundary(self):
        """The boundary block still mandates -s read-only for codex exec."""
        self.assertIn("-s read-only", self.text)
        self.assertIn("codex apply", self.text)  # Negative — boundary mentions it


class AgyWrapperTest(TestCase):

    def setUp(self):
        self.path = AGENTS_DIR / "agy-cli.md"
        self.text = self.path.read_text(encoding="utf-8")
        self.fm = _parse_frontmatter(self.path)

    def test_agy_replaces_gemini_wrapper(self):
        """The retired gemini wrapper must be gone from package source."""
        self.assertFalse((AGENTS_DIR / "gemini-cli.md").exists(),
                         "gemini-cli.md should be deleted (replaced by agy-cli.md)")

    def test_agy_no_write_tool(self):
        tools = self.fm.get("tools", "")
        tool_list = [t.strip() for t in tools.split(",")]
        self.assertNotIn("Write", tool_list)
        for required in ("Read", "Bash", "Grep", "Glob"):
            self.assertIn(required, tool_list, f"missing required tool {required}")

    def test_agy_no_review_template_markers(self):
        self.assertNotIn("## Critical Issues", self.text)
        self.assertNotIn("## Warnings", self.text)

    def test_agy_no_model_override(self):
        """Framework does not override agy's model — uses CLI default."""
        self.assertNotIn("default_model", self.text)
        self.assertNotIn("-m $MODEL", self.text)
        self.assertNotIn('--model', self.text)

    def test_agy_preserves_sandbox_flag(self):
        self.assertIn("--sandbox", self.text)

    def test_agy_passes_print_timeout(self):
        """agy's own deadline must always be passed (the external TIMEOUT_CMD
        is only a backstop and is absent on macOS without coreutils)."""
        self.assertIn("--print-timeout 300s", self.text)

    def test_agy_forbids_dangerously_skip_permissions(self):
        """The boundary block must explicitly forbid the auto-approve flag."""
        self.assertIn("Never use `--dangerously-skip-permissions`", self.text)

    def test_agy_has_mutation_check(self):
        """Detect & report contract: porcelain snapshot before/after the call,
        WARNING prefix on diff. agy's own write_file tool is NOT blocked by
        --sandbox, so this check is the wrapper's compliance verification."""
        self.assertIn("git status --porcelain --untracked-files=all", self.text)
        self.assertIn("agy review WARNING: agy modified files during read-only run", self.text)

    def test_agy_mutation_check_covers_dirty_tracked_files(self):
        """Codex review P1 regression: porcelain set-difference alone misses
        in-place edits to files that were ALREADY dirty before the run (their
        porcelain line does not change). The wrapper must also compare a
        content hash of `git diff HEAD` before/after."""
        self.assertIn("git diff HEAD", self.text)
        self.assertIn("cksum", self.text)

    def test_agy_mutation_check_never_reverts(self):
        """Mutation handling is report-only — auto-revert could destroy the
        user's own uncommitted edits in the same files."""
        self.assertIn("do NOT auto-revert", self.text)

    def test_agy_has_unavailable_fallback(self):
        self.assertIn("agy review unavailable:", self.text)

    def test_agy_forbids_settings_mutation(self):
        """The wrapper must never touch the user's global agy config — a
        malformed permissions rule hangs agy print mode indefinitely."""
        self.assertIn("Never touch `~/.gemini/", self.text)

    def test_agy_has_timeout_fallback(self):
        self.assertIn("gtimeout", self.text)
        self.assertIn("--kill-after=10s", self.text)

    def test_agy_timeout_fallback_has_conditional(self):
        """Regression guard: every agy invocation must branch on
        `[ -n "$TIMEOUT_CMD" ]` so the empty-fallback path invokes agy
        directly (not as `--kill-after=10s ... agy ...`)."""
        self.assertIn('if [ -n "$TIMEOUT_CMD" ]', self.text)

    def test_agy_external_timeout_exceeds_print_timeout(self):
        """The external watchdog (330s) must exceed --print-timeout (300s) so
        agy's own timeout fires first and yields a clean `unavailable:` reply
        instead of a SIGKILL mid-stream. agy runs under the SEC-003 env scrub,
        so the watchdog wraps `env -i ... agy`."""
        self.assertIn('330s env -i PATH="$PATH" HOME="$HOME" agy', self.text)


class TaskNameInjectionTest(TestCase):
    """Verify that a malicious task name passed inside a literal-EOF heredoc
    does NOT trigger shell expansion. This is a smoke test of the policy that
    pre_func-built bash snippets must use `<<'EOF'` (single-quoted) not `<<EOF`.

    The wrappers themselves don't build the heredoc — but the pre_func that
    feeds the wrapper does. We simulate the contract here.
    """

    def test_injection_via_task_name_under_heredoc(self):
        """task name `m-fix/foo$(id)` inside a literal-EOF heredoc must remain
        literal — no `id` command execution."""
        malicious = "m-fix/foo$(id)"
        # Build a literal-EOF heredoc the way a pre_func would
        script = f"""
cat <<'PROMPT'
Working on task {malicious} on branch feature/test.
PROMPT
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        # The literal `$(id)` must survive un-expanded
        self.assertIn("foo$(id)", result.stdout)
        # And there must be NO uid=... output (which `id` would produce if executed)
        self.assertNotIn("uid=", result.stdout)


if __name__ == "__main__":
    main()
