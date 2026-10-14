#!/usr/bin/env python3
"""Tests for the deferred documentation criteria gate.

The spec audit runs at the end of the code-review step, but the task protocol
writes documentation in the next step. The audit marks such criteria DEFERRED
and the orchestrator records them in a `SPEC_DEFERRED:` carriage note; the
documentation step re-audits them and saves `SPEC_DEFERRED: PASSED`, which the
`require_deferred_criteria_done` post_func checks against the working tree.

Run with: python3 -m pytest test/test_spec_deferred_gate.py -v
"""

import json
from pathlib import Path
from unittest import main

from test_review_freshness_gates import FreshnessTestBase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TASK_JSON = PROJECT_ROOT / "plugin" / "protocol-configs" / "task.json"

FUNC = "require_deferred_criteria_done"


def _note(at, text, **extra):
    return {"at": at, "step": "code-review", "note": text, **extra}


class DeferredGateBase(FreshnessTestBase):
    def _run(self):
        return self.engine._func_require_deferred_criteria_done({})

    def _enter_documentation(self):
        self._write_json(self.root / ".claude" / "state" / "current_task.json", {
            "task": "m-demo-task", "branch": "fix/m-demo-task", "services": [],
            "updated": "2026-10-13",
            "protocol": {"name": "task", "current_step": 3, "step_name": "documentation"},
        })

    def _passed_now(self):
        """A real `SPEC_DEFERRED: PASSED` note, saved in the documentation step
        and stamped with the current tree."""
        self._enter_documentation()
        return self.engine.save_note("SPEC_DEFERRED: PASSED")["note"]


class TestCarriageStates(DeferredGateBase):
    def test_no_carriage_passes(self):
        """Tasks begun before the feature have no carriage note at all."""
        self._write_log([_note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED")])
        self.assertTrue(self._run()["success"])

    def test_none_carriage_passes(self):
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: none"),
        ])
        self.assertTrue(self._run()["success"])

    def test_carriage_older_than_latest_sentinel_blocks(self):
        """A re-audit that forgot its carriage note leaves a superseded list."""
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: SC-3"),
            _note("2026-10-13T11:00:00+00:00", "SPEC_REVIEW: PASSED"),
        ])
        result = self._run()
        self.assertFalse(result["success"])
        self.assertIn("SPEC_DEFERRED:", result["error"])

    def test_stale_none_carriage_blocks(self):
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: none"),
            _note("2026-10-13T11:00:00+00:00", "SPEC_REVIEW: PASSED"),
        ])
        self.assertFalse(self._run()["success"])

    def test_carriage_tied_with_sentinel_after_it_in_the_log_is_fresh(self):
        """Equal timestamps: log position decides, as in _latest_note."""
        at = "2026-10-13T10:00:00+00:00"
        self._write_log([_note(at, "SPEC_REVIEW: PASSED"), _note(at, "SPEC_DEFERRED: none")])
        self.assertTrue(self._run()["success"])

    def test_carriage_tied_with_sentinel_before_it_in_the_log_is_stale(self):
        at = "2026-10-13T10:00:00+00:00"
        self._write_log([_note(at, "SPEC_DEFERRED: none"), _note(at, "SPEC_REVIEW: PASSED")])
        self.assertFalse(self._run()["success"])


class TestDeferredItems(DeferredGateBase):
    def _carriage(self, ids="SC-3, SC-5"):
        return [
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            _note("2026-10-13T10:00:01+00:00", f"SPEC_DEFERRED: {ids}"),
        ]

    def test_listed_items_without_passed_note_block(self):
        self._write_log(self._carriage())
        result = self._run()
        self.assertFalse(result["success"])
        self.assertIn("SC-3, SC-5", result["error"])

    def test_fresh_passed_note_passes(self):
        self._write_log(self._carriage())
        self._passed_now()
        self.assertTrue(self._run()["success"])

    def test_doc_edit_after_passed_note_blocks(self):
        self._write_log(self._carriage())
        self._passed_now()
        self._edit_code()
        result = self._run()
        self.assertFalse(result["success"])
        self.assertIn("STALE", result["error"])

    def test_passed_note_older_than_carriage_blocks(self):
        notes = self._carriage()
        notes.insert(0, _note("2026-10-13T09:00:00+00:00", "SPEC_DEFERRED: PASSED",
                              tree_hash=self.engine._compute_review_tree_hash()))
        self._write_log(notes)
        self.assertFalse(self._run()["success"])

    def test_passed_note_without_hash_blocks(self):
        notes = self._carriage()
        notes.append(_note("2026-10-13T12:00:00+00:00", "SPEC_DEFERRED: PASSED"))
        self._write_log(notes)
        self.assertFalse(self._run()["success"])

    def test_passed_note_is_not_read_as_a_carriage(self):
        """`SPEC_DEFERRED: PASSED` shares the prefix; it must not become the
        latest carriage and hide the list it answers."""
        self._write_log(self._carriage())
        self._passed_now()
        result = self._run()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("deferred"), ["SC-3", "SC-5"])


class TestDeferralNeedsALaterGate(DeferredGateBase):
    """Whether a criterion may be deferred is decided in code-review.md, but the
    re-audit lives in the protocol JSON. A fork reconciled file by file can take
    the new sub-protocol and keep an old documentation step with no gate; the
    deferred criterion would then never be checked. require_spec_review_passed
    refuses a deferral list when no later step of the active protocol carries
    require_deferred_criteria_done."""

    def _set_protocol(self, name, step_index, step_name="code-review"):
        self._write_json(self.root / ".claude" / "state" / "current_task.json", {
            "task": "m-demo-task", "branch": "fix/m-demo-task", "services": [],
            "updated": "2026-10-13",
            "protocol": {"name": name, "current_step": step_index, "step_name": step_name},
        })

    def _audit(self, carriage):
        self.engine.save_note("SPEC_REVIEW: PASSED")
        self.engine.save_note(carriage)
        return self.engine._func_require_spec_review_passed({})

    def test_task_protocol_accepts_a_deferral_list(self):
        self._set_protocol("task", 2)
        self.assertTrue(self._audit("SPEC_DEFERRED: SC-3")["success"])

    def test_protocol_without_the_gate_refuses_a_deferral_list(self):
        self._set_protocol("refactoring", 4)
        result = self._audit("SPEC_DEFERRED: SC-3")
        self.assertFalse(result["success"])
        self.assertIn("require_deferred_criteria_done", result["error"])

    def test_protocol_without_the_gate_accepts_none(self):
        self._set_protocol("refactoring", 4)
        self.assertTrue(self._audit("SPEC_DEFERRED: none")["success"])

    def test_forked_task_without_the_gate_refuses_a_deferral_list(self):
        steps = json.loads(TASK_JSON.read_text(encoding="utf-8"))
        for step in steps["steps"]:
            if step["name"] == "documentation":
                step["post_funcs"] = ["check_claude_md_hygiene"]
        self._write_json(self.root / "team-management" / "protocol-configs" / "custom" / "task.json", steps)
        self._set_protocol("task", 2)
        self.assertFalse(self._audit("SPEC_DEFERRED: SC-3")["success"])

    def test_stale_carriage_without_a_later_gate_blocks(self):
        """Re-auditing without saving a new carriage leaves the old list as the
        latest one; with no later gate to catch it, completion would read it as
        confirmed. The recovery is SPEC_DEFERRED: none."""
        self._set_protocol("refactoring", 4)
        self.engine.save_note("SPEC_DEFERRED: SC-3")
        self.engine.save_note("SPEC_REVIEW: PASSED")
        self.assertFalse(self.engine._func_require_spec_review_passed({})["success"])
        self.engine.save_note("SPEC_DEFERRED: none")
        self.assertTrue(self.engine._func_require_spec_review_passed({})["success"])

    def test_stale_carriage_blocks_in_task_too(self):
        """The carriage must describe the final audit in every protocol."""
        self._set_protocol("task", 2)
        self.engine.save_note("SPEC_DEFERRED: SC-3")
        self.engine.save_note("SPEC_REVIEW: PASSED")
        self.assertFalse(self.engine._func_require_spec_review_passed({})["success"])

    def test_gate_on_the_current_step_does_not_count(self):
        """Only a step after code-review can re-audit what code-review deferred."""
        config = json.loads(TASK_JSON.read_text(encoding="utf-8"))
        for step in config["steps"]:
            if step["name"] == "documentation":
                step["post_funcs"] = ["check_claude_md_hygiene"]
            if step["name"] == "code-review":
                step["post_funcs"].append("require_deferred_criteria_done")
        self._write_json(self.root / "team-management" / "protocol-configs" / "custom" / "task.json", config)
        self._set_protocol("task", 2)
        self.assertFalse(self._audit("SPEC_DEFERRED: SC-3")["success"])

    def test_unreadable_protocol_config_counts_as_no_gate(self):
        self._set_protocol("no-such-protocol", 2)
        self.assertFalse(self._audit("SPEC_DEFERRED: SC-3")["success"])


class TestCarriageRequiredAtCodeReview(DeferredGateBase):
    """A forgotten carriage would let a deferred criterion skip the re-audit
    and reach completion as DONE, so code-review cannot exit without one. A
    task that was mid-review when deferral arrived saves SPEC_DEFERRED: none."""

    def _run_spec(self):
        return self.engine._func_require_spec_review_passed({})

    def test_fresh_sentinel_without_carriage_blocks(self):
        self.engine.save_note("SPEC_REVIEW: PASSED")
        result = self._run_spec()
        self.assertFalse(result["success"])
        self.assertIn("SPEC_DEFERRED: none", result["error"])

    def test_legacy_sentinel_without_carriage_blocks(self):
        self._write_log([_note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED")])
        self.assertFalse(self._run_spec()["success"])

    def test_sentinel_after_the_carriage_blocks(self):
        self.engine.save_note("SPEC_DEFERRED: none")
        self.engine.save_note("SPEC_REVIEW: PASSED")
        self.assertFalse(self._run_spec()["success"])

    def test_carriage_tied_with_sentinel_after_it_in_the_log_passes(self):
        at = "2026-10-13T10:00:00+00:00"
        self._write_log([_note(at, "SPEC_REVIEW: PASSED"), _note(at, "SPEC_DEFERRED: none")])
        self.assertTrue(self._run_spec()["success"])

    def test_none_passes_in_every_protocol(self):
        for name, index in (("task", 2), ("refactoring", 4), ("optimize", 5),
                            ("optimize-unattended", 5)):
            with self.subTest(protocol=name):
                self._write_json(self.root / ".claude" / "state" / "current_task.json", {
                    "task": "m-demo-task", "branch": "fix/m-demo-task", "services": [],
                    "updated": "2026-10-13",
                    "protocol": {"name": name, "current_step": index, "step_name": "code-review"},
                })
                self._write_log([])
                self.engine.save_note("SPEC_REVIEW: PASSED")
                self.engine.save_note("SPEC_DEFERRED: none")
                self.assertTrue(self._run_spec()["success"])


class TestCarriageBelongsToTheAuditStep(DeferredGateBase):
    """Only the step that ran the spec audit may say what it deferred. A
    `SPEC_DEFERRED: none` saved later, in the documentation step, would
    otherwise cancel the list without the re-audit."""

    def _doc_note(self, at, text):
        return {"at": at, "step": "documentation", "note": text}

    def test_none_saved_in_the_documentation_step_does_not_cancel_the_list(self):
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: SC-3"),
            self._doc_note("2026-10-13T10:05:00+00:00", "SPEC_DEFERRED: none"),
        ])
        result = self._run()
        self.assertFalse(result["success"])
        self.assertEqual(result.get("deferred"), ["SC-3"])

    def test_carriage_from_another_step_does_not_satisfy_code_review(self):
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            self._doc_note("2026-10-13T10:05:00+00:00", "SPEC_DEFERRED: none"),
        ])
        self.assertFalse(self.engine._func_require_spec_review_passed({})["success"])


class TestNotesCountOnlyInTheirOwnStep(DeferredGateBase):
    """The audit notes belong to the step that runs require_spec_review_passed,
    SPEC_DEFERRED: PASSED to the step that runs require_deferred_criteria_done
    (both read from the protocol config). A note saved anywhere else is invisible
    to both gates: it can neither satisfy nor cancel anything."""

    def _doc_note(self, at, text, **extra):
        return {"at": at, "step": "documentation", "note": text, **extra}

    def test_sentinel_saved_in_documentation_does_not_hide_the_list(self):
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: SC-3"),
            self._doc_note("2026-10-13T10:05:00+00:00", "SPEC_REVIEW: PASSED"),
        ])
        result = self._run()
        self.assertFalse(result["success"])
        self.assertEqual(result.get("deferred"), ["SC-3"])

    def test_confirmation_saved_in_code_review_does_not_count(self):
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: SC-3"),
            _note("2026-10-13T10:00:02+00:00", "SPEC_DEFERRED: PASSED",
                  tree_hash=self.engine._compute_review_tree_hash()),
        ])
        self.assertFalse(self._run()["success"])

    def test_confirmation_saved_in_documentation_counts(self):
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: SC-3"),
        ])
        self._passed_now()
        self.assertTrue(self._run()["success"])

    def test_other_step_notes_do_not_shift_the_tie_break(self):
        at = "2026-10-13T10:00:00+00:00"
        self._write_log([
            self._doc_note("2026-10-13T09:00:00+00:00", "x"),
            self._doc_note("2026-10-13T09:00:01+00:00", "y"),
            _note(at, "SPEC_REVIEW: PASSED"),
            _note(at, "SPEC_DEFERRED: none"),
        ])
        self.assertTrue(self._run()["success"])
        self.assertTrue(self.engine._func_require_spec_review_passed({})["success"])

    def test_sentinel_saved_outside_the_audit_step_does_not_pass_code_review(self):
        """A fresh sentinel from another step must not stand in for an audit
        the code-review step never ran on this tree."""
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED", tree_hash="stale"),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: none"),
            {"at": "2026-10-13T11:00:00+00:00", "step": "implementation",
             "note": "SPEC_REVIEW: PASSED", "tree_hash": self.engine._compute_review_tree_hash()},
        ])
        self.assertFalse(self.engine._func_require_spec_review_passed({})["success"])

    def test_unreadable_protocol_config_blocks_code_review_even_with_none(self):
        self._write_json(self.root / ".claude" / "state" / "current_task.json", {
            "task": "m-demo-task", "branch": "fix/m-demo-task", "services": [],
            "updated": "2026-10-13",
            "protocol": {"name": "no-such-protocol", "current_step": 2, "step_name": "code-review"},
        })
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED",
                  tree_hash=self.engine._compute_review_tree_hash()),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: none"),
        ])
        result = self.engine._func_require_spec_review_passed({})
        self.assertFalse(result["success"])
        self.assertIn("config", result["error"])

    def test_unreadable_protocol_config_blocks_the_documentation_gate(self):
        self._write_json(self.root / ".claude" / "state" / "current_task.json", {
            "task": "m-demo-task", "branch": "fix/m-demo-task", "services": [],
            "updated": "2026-10-13",
            "protocol": {"name": "no-such-protocol", "current_step": 3, "step_name": "documentation"},
        })
        self._write_log([
            _note("2026-10-13T10:00:00+00:00", "SPEC_REVIEW: PASSED"),
            _note("2026-10-13T10:00:01+00:00", "SPEC_DEFERRED: SC-3"),
        ])
        self.assertFalse(self._run()["success"])


class TestFailClosed(DeferredGateBase):
    def test_missing_log_blocks(self):
        self.log_path.unlink()
        self.assertFalse(self._run()["success"])


class TestStampingAndWiring(DeferredGateBase):
    def test_passed_note_is_stamped(self):
        self._write_log([])
        self.engine.save_note("SPEC_DEFERRED: PASSED")
        self.assertIn("tree_hash", self._notes()[-1])

    def test_carriage_note_is_not_stamped(self):
        self._write_log([])
        self.engine.save_note("SPEC_DEFERRED: SC-1")
        self.engine.save_note("SPEC_DEFERRED: none")
        for note in self._notes():
            self.assertNotIn("tree_hash", note)

    def test_registered_with_metadata(self):
        self.assertIn(FUNC, self.engine._build_handlers())
        funcs = self.engine.get_available_funcs()
        self.assertIn(FUNC, {f["name"] for f in funcs["funcs"]})
        self.assertFalse(funcs.get("discrepancies"))

    def test_task_documentation_step_runs_the_gate(self):
        steps = json.loads(TASK_JSON.read_text(encoding="utf-8"))["steps"]
        doc = next(s for s in steps if s["name"] == "documentation")
        self.assertIn(FUNC, doc["post_funcs"])
        self.assertTrue(doc.get("post_funcs_stop_on_failure"))
        self.assertIn("SPEC_DEFERRED: PASSED", doc["start"])
        self.assertIn(FUNC, doc["end"])
        self.assertIn("A SPEC_DEFERRED: list or none saved in this step does not count", doc["start"])


if __name__ == "__main__":
    main()
