#!/usr/bin/env python3
"""Drift-guard for the provider wrappers' `env -i` scrub (SEC-003 + TMPDIR).

The wrappers are markdown agent prompts — their bash snippets cannot be
imported, so this guard asserts the scrub survives edits to
`plugin/agents/codex-cli.md` and `plugin/agents/agy-cli.md`.

Why TMPDIR belongs in the scrub: `env -i` starts from an empty environment, so
without it the provider CLI inherits no temp directory. On macOS the observed
consequence was not cosmetic — a live codex run produced
`FileNotFoundError: No usable temporary directory found in ['/tmp', '/var/tmp',
'/usr/tmp', ...]` from a Python tool codex spawned, because codex's own
read-only sandbox denies writes to `/tmp` while permitting the real per-user
`$TMPDIR` under `/var/folders/`. Defaulting to `/tmp` therefore does NOT fix it
— the INHERITED value is what matters, and `${TMPDIR:-/tmp}` only supplies a
last resort for hosts where the var is genuinely unset. A path is not a secret,
so passing it through does not weaken SEC-003, which exists to keep provider
tokens out of the provider CLI's environment.

Only lines inside fenced code blocks are scanned: the SEC-003 prose paragraphs
quote the scrub inline, and a prose mention is not an invocation.

Run with: python3 -m pytest test/test_provider_env_scrub.py -v
"""

import re
from pathlib import Path
from unittest import TestCase, main

PROJECT_ROOT = Path(__file__).parent.parent
AGENTS_DIR = PROJECT_ROOT / "plugin" / "agents"

# Invocation-site counts, so a NEW unscrubbed call fails the guard rather than
# passing unnoticed because every pre-existing site is still correct.
EXPECTED_SITES = {
    # §2 primary + fallback, §3 --output-schema primary + fallback
    "codex-cli.md": ("codex", 4),
    # primary + shell-native fallback
    "agy-cli.md": ("agy", 2),
}

TMPDIR_FORM = 'TMPDIR="${TMPDIR:-/tmp}"'


def _fenced_env_lines(text, binary):
    """Fenced lines that actually INVOKE `binary` under an `env -i` scrub.

    Three filters, each load-bearing (the second and third close a hole codex
    found in the first version of this guard): the line must be inside a ```
    fence, must not be a shell comment, and must name the provider executable
    after the scrub. Without them, commented-out or illustrative lines counted
    toward the site total — so replacing the real invocations with unscrubbed
    ones while leaving matching comments behind would satisfy both the count
    and the per-variable assertions while the behaviour this guard claims to
    pin was broken.
    """
    out, in_fence = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence or stripped.startswith("#") or "env -i" not in line:
            continue
        after_scrub = line.split("env -i", 1)[1]
        if re.search(rf"(?<![\w./-]){re.escape(binary)}(?![\w-])", after_scrub):
            out.append(line)
    return out


class ProviderEnvScrubDriftGuardTest(TestCase):

    def test_expected_invocation_site_count(self):
        for fname, (binary, expected) in EXPECTED_SITES.items():
            with self.subTest(wrapper=fname):
                lines = _fenced_env_lines(
                    (AGENTS_DIR / fname).read_text(encoding="utf-8"), binary)
                self.assertEqual(
                    len(lines), expected,
                    f"{fname}: expected {expected} `env -i … {binary}` invocation "
                    f"sites, found {len(lines)} — a site was added or removed; update "
                    f"this guard deliberately and make sure the new site carries the "
                    f"full scrub.",
                )

    def test_every_site_carries_path_home_and_tmpdir(self):
        for fname, (binary, _) in EXPECTED_SITES.items():
            text = (AGENTS_DIR / fname).read_text(encoding="utf-8")
            for line in _fenced_env_lines(text, binary):
                with self.subTest(wrapper=fname, line=line.strip()):
                    self.assertIn('PATH="$PATH"', line)
                    self.assertIn('HOME="$HOME"', line)
                    self.assertIn(TMPDIR_FORM, line)

    def test_tmpdir_uses_the_defaulted_form(self):
        """A bare `TMPDIR="$TMPDIR"` would export an EMPTY value on a host where
        the var is unset, which is not the same as supplying a fallback."""
        for fname, (binary, _) in EXPECTED_SITES.items():
            text = (AGENTS_DIR / fname).read_text(encoding="utf-8")
            for line in _fenced_env_lines(text, binary):
                with self.subTest(wrapper=fname, line=line.strip()):
                    self.assertNotIn('TMPDIR="$TMPDIR"', line)

    def test_prose_scrub_matches_the_fenced_form(self):
        """The wrappers are LLM prompts — their prose is as instruction-bearing
        as their snippets. Both SEC-003 paragraphs and both Boundaries bullets
        state the scrub in words; an edit that updates the fenced snippets and
        not the prose (or the reverse) would leave the agent two conflicting
        instructions, and the fenced-only guard above would pass."""
        for fname in EXPECTED_SITES:
            text = (AGENTS_DIR / fname).read_text(encoding="utf-8")
            in_fence = False
            for line in text.splitlines():
                if line.strip().startswith("```"):
                    in_fence = not in_fence
                    continue
                # Only lines that ENUMERATE the scrub can drift from it. A bare
                # reference ("the `env -i` scrub on both branches") names no
                # variable, so it stays true whatever the set is; requiring
                # TMPDIR there would be a false positive.
                if in_fence or "env -i" not in line or "PATH" not in line:
                    continue
                with self.subTest(wrapper=fname, line=line.strip()[:80]):
                    self.assertIn("TMPDIR", line)

    def test_helper_rejects_comments_and_non_invocations(self):
        """The guard's own filter, pinned. A commented-out or illustrative line
        must not count toward the site total — otherwise the real invocations
        could be stripped of their scrub while the counts still passed."""
        sample = (
            "```bash\n"
            '# env -i PATH="$PATH" codex exec   <- a comment, not an invocation\n'
            'env -i PATH="$PATH" HOME="$HOME" echo hello\n'
            'env -i PATH="$PATH" HOME="$HOME" codex review "$PROMPT"\n'
            "```\n"
            'env -i PATH="$PATH" HOME="$HOME" codex exec   <- outside any fence\n'
        )
        self.assertEqual(len(_fenced_env_lines(sample, "codex")), 1)


if __name__ == "__main__":
    main()
