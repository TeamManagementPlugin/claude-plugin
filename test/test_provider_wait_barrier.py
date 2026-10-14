#!/usr/bin/env python3
"""Drift guard: sub-protocols that dispatch AI providers wait for them.

Four sub-protocols dispatch codex/agy in §0 and then carry on with their own
work. Without an explicit barrier the orchestrator can act — edit files,
present a plan, advance — before the providers return, and the providers
then review a tree or plan that no longer exists. Each §0 therefore carries a
`**Wait for every provider.**` paragraph naming what the step must not do
until every provider has returned.

Assertions run on the barrier paragraph alone: §0 already says `unavailable`
and talks about recording findings, so a section-wide search would credit the
older text instead of the barrier.

Run with: python3 -m pytest test/test_provider_wait_barrier.py -v
"""

from pathlib import Path
from unittest import TestCase, main

SUB_PROTOCOLS = (Path(__file__).parent.parent
                 / "plugin" / "protocol-configs" / "sub-protocols")

BARRIER_MARKER = "**Wait for every provider.**"

# Tokens every barrier must state, checked AFTER the marker (which itself
# says "every"): read-only work on the repository, every provider, returned or
# unavailable, findings adopted or rejected, background dispatch does not lift
# it — and the bookkeeping that stays allowed: protocol_save_note, questions to
# the user, the provider-input record in an existing task work log, with a
# protocol_save_note fallback when no work log exists yet.
REQUIRED = ("read-only", "every", "returned", "unavailable",
            "adopted", "rejected", "background",
            "protocol_save_note", "questions to the user", "task work log",
            "exists yet")

# The step's gated action, per file.
GATED = {
    "task-investigation.md": ("protocol_advance", "alignment summary"),
    # The task file always exists during implementation, so the work log is
    # the one file the edit ban exempts.
    "task-implementation.md": ("edit any file other than the task work log", "tests"),
    "refactoring-planning.md": ("protocol_advance", "approval"),
    "research-exploration.md": ("protocol_advance", "synthesis"),
}


def _section0(text):
    """`## 0.` up to the next `## ` heading."""
    start = text.index("## 0.")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _barrier(text):
    """The barrier paragraph's body: the text after the marker, up to the next
    blank line. Empty when there is no barrier."""
    start = text.find(BARRIER_MARKER)
    if start == -1:
        return ""
    end = text.find("\n\n", start)
    body_start = start + len(BARRIER_MARKER)
    return text[body_start:] if end == -1 else text[body_start:end]


def _missing(barrier, tokens):
    return [tok for tok in tokens if tok not in barrier]


class ProviderWaitBarrierTest(TestCase):

    def test_each_dispatching_sub_protocol_has_a_complete_barrier(self):
        for fname, gated in GATED.items():
            with self.subTest(sub_protocol=fname):
                text = (SUB_PROTOCOLS / fname).read_text(encoding="utf-8")
                section = _section0(text)
                barrier = _barrier(section)
                self.assertTrue(barrier, f"{fname}: no barrier paragraph in §0")
                self.assertLess(section.index(BARRIER_MARKER),
                                section.index("Then continue"),
                                f"{fname}: barrier must precede 'Then continue'")
                self.assertEqual(_missing(barrier, REQUIRED + gated), [],
                                 f"{fname}: barrier is missing tokens")

    def test_implementation_intro_states_the_wait(self):
        """The implementation §0 intro once said providers 'run in parallel
        with your plan review', which reads as licence to start editing."""
        text = (SUB_PROTOCOLS / "task-implementation.md").read_text(encoding="utf-8")
        intro = _section0(text).split("\n\n")[1]
        self.assertNotIn("run in parallel with your plan review", intro)
        self.assertIn("read-only", intro)
        self.assertIn("until they return", intro)


class BarrierCheckMutationTest(TestCase):
    """The token check must catch a barrier that lost one of its clauses."""

    SAMPLE = (
        "## 0. Launch AI Providers IN PARALLEL\n\n"
        "**Wait for every provider.** Do not edit any file other than the task "
        "work log, tests included, until every dispatched provider has returned "
        "a reply or `unavailable`, and its findings are recorded as adopted or "
        "rejected. Only read-only work on the repository while they run; "
        "`protocol_save_note`, questions to the user and the record in an "
        "existing task work log stay allowed; if no task work log exists yet, "
        "use `protocol_save_note`. A background dispatch does not lift this.\n\n"
        "**Then continue to Section 1.**\n"
    )

    def test_complete_sample_passes(self):
        barrier = _barrier(_section0(self.SAMPLE))
        self.assertEqual(_missing(barrier, REQUIRED + GATED["task-implementation.md"]), [])

    def test_dropping_a_clause_is_caught(self):
        for token in ("read-only", "adopted", "rejected", "exists yet",
                      "questions to the user"):
            with self.subTest(dropped=token):
                mutated = self.SAMPLE.replace(token, "x")
                barrier = _barrier(_section0(mutated))
                self.assertIn(token, _missing(barrier, REQUIRED))

    def test_marker_alone_does_not_satisfy_every(self):
        sample = self.SAMPLE.replace("until every dispatched", "until the dispatched")
        barrier = _barrier(_section0(sample))
        self.assertIn("every", _missing(barrier, REQUIRED))

    def test_dropping_the_work_log_exemption_is_caught(self):
        sample = self.SAMPLE.replace("other than the task work log", "at all")
        barrier = _barrier(_section0(sample))
        self.assertIn("edit any file other than the task work log",
                      _missing(barrier, GATED["task-implementation.md"]))

    def test_tokens_outside_the_barrier_do_not_count(self):
        sample = self.SAMPLE.replace("adopted or rejected", "recorded") + (
            "\nOutside text: adopted or rejected.\n")
        barrier = _barrier(_section0(sample))
        self.assertEqual(_missing(barrier, ("adopted", "rejected")),
                         ["adopted", "rejected"])


if __name__ == "__main__":
    main()
