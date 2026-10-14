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


def _fenced_blocks(text):
    """Return only the fenced code blocks — the INVOCATION, without the prose.

    A flag-absence guard that greps the whole markdown is not a guard: this file
    explains at length why `--dangerously-skip-permissions` and `--sandbox` were
    removed, so the word is always present and the assertion can only key on some
    incidental spelling detail. Pinning the trailing line-continuation was exactly
    that, and three natural spellings walked past it — the flag before `-p`, a line
    ending in a redirect, a tab before the backslash (test-quality review, round 5).
    Extracting the fences lets the assertion say what it means: the flag is not in
    the command, while the prose stays free to name it.
    """
    out, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


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
            re.compile(r'\(\s*sleep\s+510\s*;?\s*&?&?\s*kill\s+"\$AGY_PID"', re.S),
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
        self.assertIn("timed out after 510s (watchdog)", self.text)

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
    """The invocation contract after m-fix-agy-launch-and-gate: no --sandbox (it
    blocks git on macOS), no --dangerously-skip-permissions (harness security
    monitors flag it, and settings.json permission rules replace it), workspace
    bound with --add-dir, contained by the project-local read-only gate."""

    @classmethod
    def setUpClass(cls):
        cls.text = AGY_CLI_MD.read_text(encoding="utf-8")

    def test_both_branches_add_dir(self):
        """--add-dir "$PWD" binds the repo as agy's workspace on BOTH watchdog
        branches (else agy runs git from a default dir → 'not a git repository',
        and the .agents/ gate is not discovered)."""
        self.assertEqual(self.text.count('--add-dir "$PWD" \\'), 2)

    def test_no_skip_permissions_flag_in_invocation(self):
        """--dangerously-skip-permissions must NOT be passed. A harness security
        monitor flags it as an unauthorized invocation (observed live), and it was
        verified unnecessary: headless runs honor settings.json permission rules.
        Prose may still MENTION the flag to explain why it was removed, so this
        asserts on the INVOCATION (the fenced blocks), not on the whole file."""
        self.assertNotIn("--dangerously-skip-permissions", _fenced_blocks(self.text))

    def test_no_sandbox_flag_in_invocation(self):
        """--sandbox must NOT be passed (it breaks git's $TMPDIR cache on macOS).
        It may still be MENTIONED in prose explaining the removal."""
        self.assertNotIn("--sandbox", _fenced_blocks(self.text))

    def test_wrapper_handles_soft_deny_signature(self):
        """If the permission rules are missing, agy runs, auto-denies every command
        and returns a content-free reply — indistinguishable from a clean review
        unless the wrapper looks for the signature and says so."""
        self.assertIn("headless mode cannot prompt for", self.text)
        self.assertIn("permission rules not configured", self.text)
        self.assertIn("/team-management:config", self.text)

    def test_rc_handling_on_both_branches(self):
        """agy's OWN --print-timeout expiry (rc=1, 'timeout waiting for response')
        is a DIFFERENT failure from the external watchdog kill (rc>=128) and must
        not be reported as it. The gtimeout branch used to not capture rc at all,
        so the distinction was unavailable on the branch that actually runs on any
        host with coreutils."""
        self.assertIn("timeout waiting for response", self.text)
        # rc captured on the gtimeout branch too, not only in the fallback.
        self.assertGreaterEqual(self.text.count("AGY_RC=$?"), 2)
        # "124" alone occurs four times in this file, so deleting the gtimeout
        # branch's classification line left the assertion green (test-quality
        # review, round 5). Pin the emitted classification itself.
        self.assertIn("124 = external watchdog", _fenced_blocks(self.text))
        self.assertIn("rc 124, or rc >= 128", self.text)   # the Section-4 table row


    def test_print_timeout_still_passed(self):
        self.assertIn("--print-timeout 480s", self.text)

    def test_gate_preflight_present(self):
        """The wrapper must refuse to run uncontained: a preflight checks the
        project's read-only gate before invoking skip-permissions."""
        self.assertIn("team-management-readonly-gate", self.text)
        self.assertIn("agy-readonly-gate.py", self.text)
        self.assertIn("read-only gate not deployed", self.text)

    def test_env_scrub_preserved(self):
        # TMPDIR joined the scrub because `env -i` otherwise leaves the CLI with
        # no writable temp dir; see test/test_provider_env_scrub.py.
        self.assertEqual(
            self.text.count(
                'env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" agy'), 2)


class AgyTemplateContractTest(TestCase):
    """The 6 shipped phase templates tell the caller how to invoke agy; a stale
    one would put the removed flag back into the prompt and trip the flag check."""

    def test_agy_templates_carry_no_skip_permissions(self):
        providers = AGY_CLI_MD.parent.parent / "protocol-configs" / "providers"
        templates = sorted(providers.glob("agy-*.md"))
        self.assertEqual(len(templates), 6, f"expected 6 agy templates, found {templates}")
        for path in templates:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("--dangerously-skip-permissions", text, f"{path.name} is stale")
            self.assertNotIn("--sandbox", text, f"{path.name} carries the git-breaking flag")


def _agy_launches(text):
    """The fenced `agy` launches as logical lines (backslash continuations joined)."""
    joined = re.sub(r"\\\n\s*", " ", _fenced_blocks(text))
    return [line for line in joined.splitlines()
            if re.search(r"\bagy\b", line) and "--add-dir" in line]


def _section4(text):
    start = text.index("## 4.")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


class AgyDeadlineTest(TestCase):
    """agy's deadlines. 300s was too short in a live run: agy 1.3.4 on a
    high-effort model ran out mid-turn twice out of four dispatches and returned
    nothing. The wrapper runs inside one Bash call, so every deadline must also
    fit under that call's 600s ceiling."""

    @classmethod
    def setUpClass(cls):
        cls.text = AGY_CLI_MD.read_text(encoding="utf-8")
        cls.launches = _agy_launches(cls.text)

    def test_each_launch_passes_the_print_timeout(self):
        self.assertEqual(len(self.launches), 2, self.launches)
        for launch in self.launches:
            self.assertIn("--print-timeout 480s", launch)

    def test_each_branch_carries_the_watchdog(self):
        self.assertIn('"$TIMEOUT_CMD" --kill-after=10s 510s env -i', self.text)
        self.assertRegex(self.text, r'\(\s*sleep\s+510\s*;\s*kill\s+"\$AGY_PID"')

    def test_no_stale_deadline_remains(self):
        for stale in ("300s", "330s", "sleep 330"):
            self.assertNotIn(stale, self.text)

    def test_deadline_ordering(self):
        """agy's own deadline fires first; the watchdog plus its kill grace ends
        before the Bash call's 600s ceiling."""
        prints = {int(m) for launch in self.launches
                  for m in re.findall(r"--print-timeout (\d+)s", launch)}
        self.assertEqual(len(prints), 1, prints)
        print_timeout = prints.pop()
        code = _fenced_blocks(self.text)   # the launch, not the prose about it
        kill_after, watchdog = map(int, re.search(
            r"--kill-after=(\d+)s (\d+)s", code).groups())
        sleep = int(re.search(r"\(\s*sleep\s+(\d+)", code).group(1))
        self.assertEqual(sleep, watchdog)
        self.assertLess(print_timeout, watchdog)
        self.assertLess(watchdog + kill_after, 600)

    def test_bash_call_timeout_pinned(self):
        """Left unspecified, the wrapper agent picks its own Bash timeout (400s in
        the live run) and the harness cuts agy off before either deadline."""
        self.assertIn("timeout: 600000", self.text)


class AgyPrintTimeoutSignatureTest(TestCase):
    """agy 1.3.4 reports its own deadline as `print timeout after … with turn in
    progress; returning partial output` and exits 0 with no review. Passed
    through, that reads as a clean review."""

    @classmethod
    def setUpClass(cls):
        cls.section = _section4(AGY_CLI_MD.read_text(encoding="utf-8"))

    def test_one_table_row_classifies_it(self):
        rows = [line for line in self.section.splitlines()
                if line.startswith("|") and "print timeout after" in line]
        self.assertEqual(len(rows), 1, rows)
        for token in ("[agy] print timeout after", "returning partial output",
                      "including 0", "agy review unavailable:", "expired mid-turn"):
            self.assertIn(token, rows[0])

    def test_matches_agy_status_line_not_quoted_words(self):
        """A review of agy-related code can quote the words; only agy's own
        status line marks the failure (codex review, round 1)."""
        self.assertIn("not\nthe same words quoted inside a review", self.section)

    def test_signature_row_precedes_rc_rows(self):
        self.assertLess(self.section.index("[agy] print timeout after"),
                        self.section.index("any other rc != 0"))

    def test_checked_before_stdout_is_passed_through(self):
        passthrough = self.section.index("whatever `agy` emitted on stdout")
        self.assertLess(self.section.index("print timeout after"), passthrough)

    def test_rc1_timeout_row_kept(self):
        self.assertIn("timeout waiting for response", self.section)
        self.assertIn("--print-timeout (480s) expired before it answered", self.section)


class AgyPromptBudgetTest(TestCase):
    """Each phase template tells agy, before it starts, what the gate will deny
    and to answer before the deadline — the live runs spent steps on denied
    commands and paged whole files until time ran out."""

    REQUIRED = ("test and build runners", "`;`", "`&&`", "`||`", "redirection",
                "do not try them", "`$`", "parentheses", "braces", "rg -F", "line ranges",
                "8 minutes", "early")

    def test_every_template_carries_the_budget(self):
        providers = AGY_CLI_MD.parent.parent / "protocol-configs" / "providers"
        templates = sorted(providers.glob("agy-*.md"))
        self.assertEqual(len(templates), 6)
        for path in templates:
            with self.subTest(template=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("--print-timeout 480s", text)
                self.assertNotIn("300s", text)
                start = text.find("## Budget")
                self.assertNotEqual(start, -1, "no ## Budget section")
                end = text.find("\n## ", start + 1)
                budget = text[start:] if end == -1 else text[start:end]
                self.assertEqual([t for t in self.REQUIRED if t not in budget], [])

    def test_budget_survives_rendering(self):
        """Templates are rendered with str.format_map: a brace in the Budget
        text would be read as a placeholder and either swallow words or make the
        renderer return the template unrendered."""
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / "plugin" / "hooks"))
        from ai_providers import _render_provider_template
        providers = AGY_CLI_MD.parent.parent / "protocol-configs" / "providers"
        for path in sorted(providers.glob("agy-*.md")):
            with self.subTest(template=path.name):
                text = path.read_text(encoding="utf-8")
                start = text.find("## Budget")
                end = text.find("\n## ", start + 1)
                budget = text[start:end]
                rendered = _render_provider_template(text, {"task_name": "t-x"})
                self.assertIn(budget, rendered)
                self.assertIn("t-x", rendered)


if __name__ == "__main__":
    main()


def _section3(text):
    start = text.index("## 3.")
    return text[start:text.index("\n## ", start + 1)]


class AgyDenialSignatureTest(TestCase):
    """Headless agy names the tool it denied: `a tool required the "<tool>"
    permission`. Only a denied `command` means the permission rules are missing;
    a denied `read_file` was a path outside the repository (a scratchpad history
    file in a live run), and calling that "rules not configured" sent the user
    to fix rules that were already installed."""

    @classmethod
    def setUpClass(cls):
        cls.section = _section4(AGY_CLI_MD.read_text(encoding="utf-8"))
        cls.rows = [line for line in cls.section.splitlines()
                    if line.startswith("|") and "a tool required the" in line]

    def test_one_row_per_denied_tool_form(self):
        self.assertEqual(len(self.rows), 3, self.rows)
        command, read, other = self.rows
        self.assertIn('a tool required the "command" permission', command)
        self.assertIn("permission rules not configured", command)
        self.assertIn('"read_file"', read)
        self.assertIn('"list_dir"', read)
        self.assertIn("was denied <tool> on a path outside the repository", read)
        self.assertNotIn("permission rules not configured", read)
        self.assertIn("any other tool", other)
        self.assertIn("agy auto-denied a tool in headless mode — <the denied tool>", other)
        self.assertNotIn("permission rules not configured", other)

    def test_rows_match_agy_status_line(self):
        for row in self.rows:
            self.assertIn("jetski: no output produced", row)

    def test_bare_auto_denied_no_longer_selects_rules_reply(self):
        self.assertNotIn("(or `auto-denied`)", self.section)

    def test_denial_rows_precede_rc_rows(self):
        """agy exits 0 on a denial, so the rows must not sit behind the rc rows."""
        for row in self.rows:
            self.assertLess(self.section.index(row), self.section.index("any other rc != 0"))


class AgyOutOfRepoPreflightTest(TestCase):
    """agy's workspace is the repository (`--add-dir "$PWD"`); headless agy
    denies a read outside it and returns nothing. The wrapper refuses such a
    prompt up front and never reads the file itself: the prompt carries task
    text, and a path named there would ship that file to agy's backend."""

    @classmethod
    def setUpClass(cls):
        cls.section = _section3(AGY_CLI_MD.read_text(encoding="utf-8"))

    def test_preflight_present_before_the_launch(self):
        heading = "**Preflight 3 — the prompt stays inside the repository.**"
        self.assertIn(heading, self.section)
        self.assertLess(self.section.index("**Preflight 2"), self.section.index(heading))
        self.assertLess(self.section.index(heading), self.section.index("**SEC-003"))

    def test_reply_names_the_path(self):
        self.assertIn("agy review unavailable: the prompt asks agy to read <path>, "
                      "outside the repository — agy can read only the repository; "
                      "pass the content inline", self.section)

    def test_never_reads_the_file(self):
        self.assertIn("Never read such a file yourself", self.section)

    def test_path_boundary_semantics(self):
        self.assertIn("path-component boundary", self.section)
        self.assertIn("`/repo-copy` is outside `/repo`", self.section)

    def test_quoted_data_exemption(self):
        self.assertIn("quoted as data", self.section)
