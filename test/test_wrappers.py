#!/usr/bin/env python3
"""Tests for the codex-cli and agy-cli wrapper agents.

The codex wrapper was refactored to a thin pass-through (h-ai-providers-foundation);
the agy wrapper replaced the retired gemini wrapper (m-replace-gemini-with-agy).
The tests guard the contract:
- codex-cli MUST NOT carry the Write tool in its frontmatter.
- Neither wrapper carries the old review-shape template markers
  (## Critical Issues / ## Warnings) — those are the caller's concern now.
- agy-cli passes --add-dir + --print-timeout (no permission flag) and
  NEVER --sandbox (the sandbox breaks git on macOS); it is contained by a
  project-local .agents/ read-only deny-gate (preflight-verified) and carries the
  git-status mutation check (detect & report) with the `agy review WARNING:` prefix
  contract. It still must never touch ~/.gemini/.
- Shell-injection via task name passed inside a heredoc must not trigger
  expansion (validates the literal-EOF `<<'PROMPT'` quoting policy).

Run with: python3 -m pytest test/test_wrappers.py -v
"""

import re
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, main

PROJECT_ROOT = Path(__file__).parent.parent


def _fenced_blocks(text):
    """Return only the fenced code blocks of an agent markdown — the INVOCATION.

    These wrapper prompts explain at length WHY `--sandbox` and
    `--dangerously-skip-permissions` were removed, so the words are permanently
    present in the prose and a whole-file `assertNotIn` cannot express the rule.
    Keying on an incidental spelling (a trailing line-continuation) let three
    natural spellings through (test-quality review, round 5); the fences let the
    assertion mean what it says.
    """
    out, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def _cli_invocations(text, binary):
    """Logical fenced lines that INVOKE `binary` under an `env -i` scrub.

    Backslash-continued lines are joined first, so a redirect placed on the
    last continuation line (`"$PROMPT" </dev/null 2>&1`) belongs to the
    invocation it ends. Comment lines are skipped, and the binary must appear
    as its own token after the scrub — the same filters as
    `test_provider_env_scrub._fenced_env_lines`.
    """
    logical, buf = [], ""
    for line in _fenced_blocks(text).splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        logical.append(buf + line)
        buf = ""
    out = []
    for line in logical:
        if line.strip().startswith("#") or "env -i" not in line:
            continue
        after_scrub = line.split("env -i", 1)[1]
        if re.search(rf"(?<![\w./-]){re.escape(binary)}(?![\w-])", after_scrub):
            out.append(line)
    return out


def _prose_states_stdin_rule(text):
    """True when a line OUTSIDE the fences names both stdin and `</dev/null`."""
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if not inside and "stdin" in line and "</dev/null" in line:
            return True
    return False
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

    def test_subcommand_table_never_pairs_a_scope_flag_with_a_prompt(self):
        """The §1 lookup table is what the wrapper agent actually reads to build
        the command — the engine's prompt is only an input to it.

        `codex review` accepts EITHER a positional [PROMPT] or one of
        `--uncommitted` / `--base` / `--commit`, never both (exit 2, verified on
        codex-cli 0.145.0 and 0.147.0). This wrapper is pass-through, so a
        prompt is always present; a table row naming a scope flag steers the
        agent into either the parse failure or — worse — dropping the caller's
        prompt and reviewing to codex's own default brief with no error at all.
        That row is exactly how the shipped code-review prompt stayed inert.
        """
        rows = [ln for ln in self.text.splitlines()
                if ln.startswith("|") and "codex" in ln]
        self.assertTrue(rows, "the §1 subcommand table went missing")
        for row in rows:
            for flag in ("--uncommitted", "--base", "--commit"):
                self.assertNotIn(flag, row, f"scope flag {flag} in table row: {row}")
        self.assertTrue(
            any('codex review "$PROMPT"' in row for row in rows),
            "no table row invokes `codex review \"$PROMPT\"` — the prompt-bearing "
            "form is the only correct review invocation for a pass-through wrapper",
        )

    def test_scope_flag_exclusion_is_stated_explicitly(self):
        """A table row alone is a weak instruction to an LLM wrapper; the rule
        must be spelled out, with the real error text, so the agent does not
        'helpfully' reintroduce a flag it has seen in other codex docs."""
        self.assertIn("cannot be used with '[PROMPT]'", self.text)
        self.assertIn("mutually exclusive", self.text)

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

    def test_codex_invocations_detach_stdin(self):
        """Every codex launch redirects stdin from /dev/null. On the foreground
        (gtimeout/timeout) branch codex otherwise inherits the caller's open
        pipe, and `codex exec` reads a non-TTY stdin until the watchdog fires."""
        calls = _cli_invocations(self.text, "codex")
        self.assertGreaterEqual(len(calls), 4, calls)
        for call in calls:
            self.assertIn("</dev/null", call, call)
        self.assertTrue(_prose_states_stdin_rule(self.text),
                        "codex-cli.md must state the stdin rule in prose")


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

    def test_agy_invocations_detach_stdin(self):
        """Every agy launch redirects stdin from /dev/null, on both watchdog
        branches — the foreground branch would otherwise inherit the caller's
        open pipe."""
        calls = _cli_invocations(self.text, "agy")
        self.assertGreaterEqual(len(calls), 2, calls)
        for call in calls:
            self.assertIn("</dev/null", call, call)
        self.assertTrue(_prose_states_stdin_rule(self.text),
                        "agy-cli.md must state the stdin rule in prose")

    def test_agy_no_review_template_markers(self):
        self.assertNotIn("## Critical Issues", self.text)
        self.assertNotIn("## Warnings", self.text)

    def test_agy_no_model_override(self):
        """Framework does not override agy's model — uses CLI default."""
        self.assertNotIn("default_model", self.text)
        self.assertNotIn("-m $MODEL", self.text)
        self.assertNotIn('--model', self.text)

    def test_agy_passes_no_permission_or_sandbox_flag(self):
        """Neither flag may reach the command line: --sandbox breaks git on macOS
        ($TMPDIR/xcrun block), and --dangerously-skip-permissions is blocked by
        harness security monitors and made unnecessary by the settings.json
        permission rules (m-fix-agy-launch-and-gate). Both may still be MENTIONED
        in prose explaining the removal, so assert on the invocation-flag forms."""
        self.assertIn("--add-dir", self.text)
        self.assertNotIn("--dangerously-skip-permissions", _fenced_blocks(self.text))
        self.assertNotIn("--sandbox", _fenced_blocks(self.text))

    def test_agy_passes_print_timeout(self):
        """agy's own deadline must always be passed (the external TIMEOUT_CMD
        is only a backstop and is absent on macOS without coreutils)."""
        self.assertIn("--print-timeout 480s", self.text)

    def test_agy_preflights_both_containment_layers(self):
        """Two preflights, two failure modes: the .agents/ read-only gate must be
        deployed (else agy could run uncontained), and the permission rules must
        exist (else agy runs, auto-denies everything, and returns an empty review
        that reads like a clean one)."""
        self.assertIn("team-management-readonly-gate", self.text)
        self.assertIn("read-only gate not deployed", self.text)
        self.assertIn("permission rules not configured", self.text)

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
        """The external watchdog (510s) must exceed --print-timeout (480s) so
        agy's own timeout fires first and yields a clean `unavailable:` reply
        instead of a SIGKILL mid-stream. agy runs under the SEC-003 env scrub,
        so the watchdog wraps `env -i ... agy`."""
        self.assertIn(
            '510s env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" agy',
            self.text)


class CliInvocationsHelperTest(TestCase):
    """The stdin guards are only as good as `_cli_invocations`; pin its filters."""

    SAMPLE = (
        "Prose: env -i PATH=x codex exec   <- outside any fence\n"
        "```bash\n"
        '# env -i PATH="$PATH" codex exec   <- a comment\n'
        'env -i PATH="$PATH" codex exec -s read-only \\\n'
        '    --output-schema "$SCHEMA" \\\n'
        '    "$PROMPT" </dev/null 2>&1\n'
        'env -i PATH="$PATH" codex-other run\n'
        'cat "$CODEX_OUT"\n'
        "```\n"
    )

    def test_continued_invocation_is_one_logical_line(self):
        calls = _cli_invocations(self.SAMPLE, "codex")
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("--output-schema", calls[0])
        self.assertIn("</dev/null", calls[0])

    def test_unredirected_continued_invocation_is_reported(self):
        sample = self.SAMPLE.replace(' </dev/null 2>&1', ' 2>&1')
        calls = _cli_invocations(sample, "codex")
        self.assertEqual(len(calls), 1, calls)
        self.assertNotIn("</dev/null", calls[0])


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
