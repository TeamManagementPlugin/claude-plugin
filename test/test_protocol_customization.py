#!/usr/bin/env python3
"""Tests for protocol customization: bootstrap copy + drift-update + hardening.

Covers (task m-protocol-customization-override):
  - list_protocols enrich (per-step description+mode) + malformed-JSON hardening
  - load_protocol_config malformed-JSON hardening
  - ProtocolEngine.customize_protocol (bootstrap copy + provenance sidecar)
  - ProtocolEngine.check_drift (drift detect / staging / acknowledge)
  - end-to-end: a copied custom protocol overrides system

Run with: python3 -m pytest test/test_protocol_customization.py -v
"""

import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


class CustomizationTestBase(TestCase):
    """Temp project root with system/ + custom/ protocol-config trees."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        cfg = self.temp_dir / "team-management" / "protocol-configs"
        # Three-root model (plugin conversion): system protocol-configs live under
        # PLUGIN_ROOT, not the project's team-management/. Point system_dir there
        # and isolate get_plugin_root() to this temp plugin dir so the real repo's
        # configs cannot leak into the tests.
        self.plugin_root = self.temp_dir / "plugin"
        self.system_dir = self.plugin_root / "protocol-configs"
        self.custom_dir = cfg / "custom"
        (self.system_dir / "sub-protocols").mkdir(parents=True)
        (self.system_dir / "providers").mkdir(parents=True)
        (self.custom_dir / "sub-protocols").mkdir(parents=True)
        (self.custom_dir / "providers").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state").mkdir(parents=True)
        self._orig_plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT")
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(self.plugin_root)

        import shared_state
        self._orig_root = shared_state.PROJECT_ROOT
        shared_state.PROJECT_ROOT = self.temp_dir
        self._shared_state = shared_state

        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.temp_dir)

    def tearDown(self):
        self._shared_state.PROJECT_ROOT = self._orig_root
        if self._orig_plugin_root_env is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = self._orig_plugin_root_env
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ---- helpers -----------------------------------------------------------

    def _write_system_protocol(self, name="task"):
        """A representative protocol: one @sub-protocol step with an AI-provider
        phase (investigation) + one inline step (documentation)."""
        steps = [
            {
                "name": "investigation",
                "mode": "discussion",
                "description": "Investigate the request",
                "start": "@sub-protocols/task-investigation.md",
                "pre_funcs": ["resolve_ai_providers_for_investigation"],
            },
            {
                "name": "documentation",
                "mode": "documentation",
                "description": "Update docs",
                "start": "Update documentation inline.",
            },
        ]
        (self.system_dir / f"{name}.json").write_text(
            json.dumps({"name": name, "description": f"{name} protocol", "steps": steps}),
            encoding="utf-8",
        )
        (self.system_dir / "sub-protocols" / "task-investigation.md").write_text(
            "SYSTEM investigation text", encoding="utf-8"
        )
        (self.system_dir / "providers" / "codex-investigation.md").write_text(
            "SYSTEM codex investigation", encoding="utf-8"
        )
        (self.system_dir / "providers" / "agy-investigation.md").write_text(
            "SYSTEM agy investigation", encoding="utf-8"
        )
        return steps

    def _write_two_protocols_sharing(self):
        """Two system protocols (aproto, bproto) that both reference the same
        @sub-protocols/shared.md — mirrors how task/refactoring/optimize share
        code-review.md and task-completion.md."""
        for name in ("aproto", "bproto"):
            steps = [{"name": "review", "mode": "discussion", "description": "r",
                      "start": "@sub-protocols/shared.md"}]
            (self.system_dir / f"{name}.json").write_text(
                json.dumps({"name": name, "description": name, "steps": steps}),
                encoding="utf-8")
        (self.system_dir / "sub-protocols" / "shared.md").write_text(
            "SYSTEM shared v1", encoding="utf-8")


class TestListProtocolsEnrich(CustomizationTestBase):
    def test_list_protocols_includes_step_detail(self):
        self._write_system_protocol("task")
        result = self.engine.list_protocols()
        self.assertTrue(result["success"])
        entry = next(p for p in result["protocols"] if p["name"] == "task")
        # Existing contract preserved.
        self.assertEqual(entry["step_names"], ["investigation", "documentation"])
        # New: per-step detail for the create command's explanations.
        self.assertEqual(
            entry["steps"],
            [
                {"name": "investigation", "description": "Investigate the request", "mode": "discussion"},
                {"name": "documentation", "description": "Update docs", "mode": "documentation"},
            ],
        )


class TestListProtocolsHardening(CustomizationTestBase):
    def test_list_protocols_warns_on_malformed_custom_json(self):
        self._write_system_protocol("task")
        # A malformed custom protocol must not be silently dropped.
        (self.custom_dir / "broken.json").write_text("{ not valid json", encoding="utf-8")
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            result = self.engine.list_protocols()
        self.assertTrue(result["success"])
        # Other protocols still listed.
        self.assertIn("task", [p["name"] for p in result["protocols"]])
        # Surfaced, not swallowed.
        self.assertIn("warnings", result)
        self.assertTrue(any("broken.json" in w for w in result["warnings"]))
        self.assertIn("broken.json", err.getvalue())

    def test_list_protocols_no_warnings_key_on_clean(self):
        self._write_system_protocol("task")
        result = self.engine.list_protocols()
        # Happy-path response shape unchanged (no warnings key).
        self.assertNotIn("warnings", result)

    def test_list_protocols_skips_drift_staging_and_sidecar(self):
        """new-*.json staging + .forked-from.json sidecar must NOT register as
        protocols (they live in custom/ but are not protocol configs)."""
        self._write_system_protocol("task")
        # Bootstrap-copy artefacts that share the custom/ dir.
        (self.custom_dir / "new-task.json").write_text(
            json.dumps({"name": "task", "description": "staged", "steps": []}),
            encoding="utf-8",
        )
        (self.custom_dir / ".forked-from.json").write_text(
            json.dumps({"task": {"files": {}}}), encoding="utf-8"
        )
        result = self.engine.list_protocols()
        names = [p["name"] for p in result["protocols"]]
        self.assertEqual(names.count("task"), 1)
        self.assertNotIn(".forked-from", names)
        # No warnings: skipped files are reserved, not errors.
        self.assertNotIn("warnings", result)

    def test_list_protocols_warns_on_non_dict_json(self):
        """Valid JSON that is not an object (e.g. a top-level list) must not
        crash the listing with an AttributeError."""
        self._write_system_protocol("task")
        (self.custom_dir / "weird.json").write_text("[1, 2, 3]", encoding="utf-8")
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            result = self.engine.list_protocols()
        self.assertTrue(result["success"])
        self.assertIn("task", [p["name"] for p in result["protocols"]])
        self.assertIn("warnings", result)
        self.assertTrue(any("weird.json" in w for w in result["warnings"]))

    def test_list_protocols_warns_on_non_list_steps(self):
        """A dict config whose `steps` is not a list (e.g. null) must warn +
        skip, not crash the whole listing with a TypeError."""
        self._write_system_protocol("task")
        (self.custom_dir / "weird2.json").write_text(
            json.dumps({"name": "weird2", "steps": None}), encoding="utf-8")
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            result = self.engine.list_protocols()
        self.assertTrue(result["success"])
        self.assertIn("task", [p["name"] for p in result["protocols"]])
        self.assertNotIn("weird2", [p["name"] for p in result["protocols"]])
        self.assertIn("warnings", result)
        self.assertTrue(any("weird2.json" in w for w in result["warnings"]))


class TestLoadProtocolConfigHardening(CustomizationTestBase):
    def test_load_protocol_config_warns_on_malformed_custom(self):
        self._write_system_protocol("task")
        # A malformed CUSTOM fork must surface, not silently fall back to system.
        (self.custom_dir / "task.json").write_text("{ broken", encoding="utf-8")
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            config = self._shared_state.load_protocol_config("task")
        # Resilient fallback preserved: system config still returned.
        self.assertIsNotNone(config)
        self.assertEqual(config["name"], "task")
        # But the broken custom fork is no longer silent.
        self.assertIn("task.json", err.getvalue())

    def test_load_protocol_config_handles_non_dict_custom(self):
        self._write_system_protocol("task")
        (self.custom_dir / "task.json").write_text("[]", encoding="utf-8")
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            config = self._shared_state.load_protocol_config("task")
        # Falls through to the valid system config instead of returning a list.
        self.assertIsInstance(config, dict)
        self.assertEqual(config["name"], "task")
        self.assertIn("task.json", err.getvalue())


class TestCustomizeProtocol(CustomizationTestBase):
    def _sha(self, path):
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_customize_copies_json_subprotocols_providers(self):
        self._write_system_protocol("task")
        result = self.engine.customize_protocol("task")
        self.assertTrue(result["success"], result)

        # JSON copied byte-identical.
        cj = self.custom_dir / "task.json"
        self.assertTrue(cj.exists())
        self.assertEqual(cj.read_bytes(), (self.system_dir / "task.json").read_bytes())

        # Referenced @sub-protocol copied (inline 'documentation' step has none).
        csub = self.custom_dir / "sub-protocols" / "task-investigation.md"
        self.assertEqual(csub.read_text(encoding="utf-8"), "SYSTEM investigation text")

        # Provider templates for the investigation phase (codex + agy) copied.
        self.assertTrue((self.custom_dir / "providers" / "codex-investigation.md").exists())
        self.assertTrue((self.custom_dir / "providers" / "agy-investigation.md").exists())

        # Provenance sidecar records sha256 of each system source.
        sidecar = json.loads((self.custom_dir / ".forked-from.json").read_text(encoding="utf-8"))
        self.assertIn("task", sidecar)
        recs = {r["custom"]: r["sha256"] for r in sidecar["task"]["files"]}
        json_rel = str((self.custom_dir / "task.json").relative_to(self.temp_dir))
        self.assertEqual(recs[json_rel], self._sha(self.system_dir / "task.json"))

        self.assertIn(json_rel, result["created"])

    def test_customize_unknown_protocol(self):
        result = self.engine.customize_protocol("does-not-exist")
        self.assertFalse(result["success"])
        self.assertIn("does-not-exist", result["error"])

    def test_customize_no_clobber(self):
        self._write_system_protocol("task")
        cj = self.custom_dir / "task.json"
        cj.write_text("MINE — do not overwrite", encoding="utf-8")
        result = self.engine.customize_protocol("task")
        self.assertTrue(result["success"])
        self.assertEqual(cj.read_text(encoding="utf-8"), "MINE — do not overwrite")
        json_rel = str(cj.relative_to(self.temp_dir))
        self.assertIn(json_rel, result["skipped"])
        self.assertNotIn(json_rel, result["created"])

    def test_customize_force_overwrites(self):
        self._write_system_protocol("task")
        cj = self.custom_dir / "task.json"
        cj.write_text("MINE", encoding="utf-8")
        result = self.engine.customize_protocol("task", force=True)
        self.assertTrue(result["success"])
        self.assertEqual(cj.read_bytes(), (self.system_dir / "task.json").read_bytes())

    def test_customize_skips_missing_provider_template(self):
        """A phase whose template is inline (code_review -> no codex-code-review.md
        on disk) must not error and must not appear as copied."""
        steps = [{
            "name": "code-review",
            "mode": "discussion",
            "description": "Review",
            "start": "@sub-protocols/code-review.md",
            "pre_funcs": ["resolve_ai_providers"],
        }]
        (self.system_dir / "rev.json").write_text(
            json.dumps({"name": "rev", "description": "rev", "steps": steps}), encoding="utf-8"
        )
        (self.system_dir / "sub-protocols" / "code-review.md").write_text(
            "SYSTEM review text", encoding="utf-8"
        )
        # Note: NO codex-code-review.md / agy-code-review.md created.
        result = self.engine.customize_protocol("rev")
        self.assertTrue(result["success"], result)
        self.assertTrue((self.custom_dir / "rev.json").exists())
        self.assertTrue((self.custom_dir / "sub-protocols" / "code-review.md").exists())
        self.assertFalse((self.custom_dir / "providers" / "codex-code-review.md").exists())

    def test_customize_rejects_path_traversal(self):
        self._write_system_protocol("task")
        for bad in ("../task", "sub/task", ".hidden"):
            result = self.engine.customize_protocol(bad)
            self.assertFalse(result["success"], bad)
            self.assertIn("Invalid protocol name", result["error"])

    def test_customize_errors_on_corrupt_sidecar(self):
        self._write_system_protocol("task")
        sc = self.custom_dir / ".forked-from.json"
        sc.write_text("{ corrupt", encoding="utf-8")
        result = self.engine.customize_protocol("task")
        self.assertFalse(result["success"])
        self.assertIn(".forked-from.json", result["error"])
        # The corrupt sidecar is NOT overwritten (no provenance wipe).
        self.assertEqual(sc.read_text(encoding="utf-8"), "{ corrupt")

    def test_customize_preexisting_fork_records_unknown_baseline(self):
        """A manual fork with no prior provenance must not get the CURRENT system
        hash invented as its baseline (which would mask real drift)."""
        self._write_system_protocol("task")
        (self.custom_dir / "task.json").write_text("MY MANUAL FORK", encoding="utf-8")
        result = self.engine.customize_protocol("task")
        self.assertTrue(result["success"])
        sidecar = json.loads((self.custom_dir / ".forked-from.json").read_text(encoding="utf-8"))
        recs = {r["custom"]: r["sha256"] for r in sidecar["task"]["files"]}
        json_rel = str((self.custom_dir / "task.json").relative_to(self.temp_dir))
        self.assertIsNone(recs[json_rel])


class TestCheckDrift(CustomizationTestBase):
    def _sha(self, path):
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_check_drift_detects_changed_system(self):
        self._write_system_protocol("task")
        self.engine.customize_protocol("task")
        # Upstream reinstall changes the system protocol JSON.
        sysj = self.system_dir / "task.json"
        sysj.write_text(sysj.read_text(encoding="utf-8") + "\n// upstream change", encoding="utf-8")

        result = self.engine.check_drift()
        self.assertTrue(result["success"])
        changed = [d for d in result["drifted"] if d["custom"].endswith("custom/task.json")]
        self.assertEqual(len(changed), 1)
        staged = self.custom_dir / "new-task.json"
        self.assertTrue(staged.exists())
        # Staged copy carries the NEW system content.
        self.assertEqual(staged.read_bytes(), sysj.read_bytes())
        self.assertEqual(result["unknown"], [])

    def test_check_drift_clean(self):
        self._write_system_protocol("task")
        self.engine.customize_protocol("task")
        result = self.engine.check_drift()
        self.assertTrue(result["success"])
        self.assertEqual(result["drifted"], [])
        self.assertFalse((self.custom_dir / "new-task.json").exists())

    def test_check_drift_unknown_provenance(self):
        self._write_system_protocol("task")
        self.engine.customize_protocol("task")  # creates the sidecar
        # A custom protocol with no provenance record (hand-made fork).
        (self.custom_dir / "orphan.json").write_text(
            json.dumps({"name": "orphan", "steps": []}), encoding="utf-8"
        )
        result = self.engine.check_drift()
        self.assertIn("orphan", result["unknown"])
        self.assertEqual(result["drifted"], [])

    def test_check_drift_acknowledge_finalizes(self):
        self._write_system_protocol("task")
        self.engine.customize_protocol("task")
        sysj = self.system_dir / "task.json"
        sysj.write_text(sysj.read_text(encoding="utf-8") + "\n// upstream", encoding="utf-8")
        self.engine.check_drift()  # stages new-task.json
        self.assertTrue((self.custom_dir / "new-task.json").exists())

        ack = self.engine.check_drift(acknowledge=True)
        self.assertTrue(ack["success"])
        self.assertIn("task", ack["acknowledged"])
        # Staging removed, provenance refreshed to current system.
        self.assertFalse((self.custom_dir / "new-task.json").exists())
        sidecar = json.loads((self.custom_dir / ".forked-from.json").read_text(encoding="utf-8"))
        recs = {r["custom"]: r["sha256"] for r in sidecar["task"]["files"]}
        json_rel = str((self.custom_dir / "task.json").relative_to(self.temp_dir))
        self.assertEqual(recs[json_rel], self._sha(sysj))
        # And a re-check is now clean.
        self.assertEqual(self.engine.check_drift()["drifted"], [])


class TestCheckDriftRobustness(CustomizationTestBase):
    def _sha(self, path):
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_check_drift_errors_on_corrupt_sidecar(self):
        (self.custom_dir / ".forked-from.json").write_text("{ corrupt", encoding="utf-8")
        result = self.engine.check_drift()
        self.assertFalse(result["success"])
        self.assertIn(".forked-from.json", result["error"])

    def test_check_drift_reports_unknown_baseline(self):
        self._write_system_protocol("task")
        (self.custom_dir / "task.json").write_text("MANUAL", encoding="utf-8")
        self.engine.customize_protocol("task")  # records None baseline for task.json
        result = self.engine.check_drift()
        statuses = {d["status"] for d in result["drifted"]
                    if d["custom"].endswith("custom/task.json")}
        self.assertIn("unknown-baseline", statuses)
        self.assertFalse((self.custom_dir / "new-task.json").exists())

    def test_check_drift_acknowledge_clears_system_removed(self):
        self._write_system_protocol("task")
        self.engine.customize_protocol("task")
        (self.system_dir / "task.json").unlink()  # upstream deletes the protocol
        d1 = self.engine.check_drift()
        self.assertTrue(any(x["status"] == "system-removed" for x in d1["drifted"]))
        self.engine.check_drift(acknowledge=True)
        d2 = self.engine.check_drift()
        self.assertFalse(any(x.get("status") == "system-removed" for x in d2["drifted"]))

    def test_check_drift_skips_staging_when_custom_removed(self):
        self._write_system_protocol("task")
        self.engine.customize_protocol("task")
        sysj = self.system_dir / "task.json"
        sysj.write_text(sysj.read_text(encoding="utf-8") + "\n// changed", encoding="utf-8")
        (self.custom_dir / "task.json").unlink()  # user deleted their fork
        result = self.engine.check_drift()
        statuses = {d["status"] for d in result["drifted"]
                    if d["custom"].endswith("custom/task.json")}
        self.assertIn("custom-removed", statuses)
        self.assertFalse((self.custom_dir / "new-task.json").exists())

    def test_check_drift_acknowledge_uses_staged_snapshot_not_live_system(self):
        """Closes the detect->acknowledge TOCTOU: if system changes again before
        the user acknowledges, the baseline must be the merged (staged) snapshot,
        not the newer live system — so the second change is still caught."""
        self._write_system_protocol("task")
        self.engine.customize_protocol("task")
        sysj = self.system_dir / "task.json"
        v2 = sysj.read_text(encoding="utf-8") + "\n// v2"
        sysj.write_text(v2, encoding="utf-8")
        self.engine.check_drift()  # stages new-task.json (v2)
        v2_sha = self._sha(self.custom_dir / "new-task.json")
        sysj.write_text(v2 + "\n// v3", encoding="utf-8")  # changes again pre-ack
        self.engine.check_drift(acknowledge=True)
        sidecar = json.loads((self.custom_dir / ".forked-from.json").read_text(encoding="utf-8"))
        recs = {r["custom"]: r["sha256"] for r in sidecar["task"]["files"]}
        json_rel = str((self.custom_dir / "task.json").relative_to(self.temp_dir))
        self.assertEqual(recs[json_rel], v2_sha)
        self.assertNotEqual(recs[json_rel], self._sha(sysj))
        # The un-merged v3 change is now correctly reported.
        d = self.engine.check_drift()
        self.assertTrue(any(x["status"] == "changed" and x["custom"].endswith("custom/task.json")
                            for x in d["drifted"]))


class TestSharedFileProvenance(CustomizationTestBase):
    def _sha(self, path):
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_second_protocol_inherits_shared_file_provenance(self):
        """Forking a 2nd protocol that shares a file must inherit the real
        fork-time hash, not record None (which would lose drift tracking)."""
        self._write_two_protocols_sharing()
        self.engine.customize_protocol("aproto")  # copies shared.md, records its hash
        shared_sha = self._sha(self.system_dir / "sub-protocols" / "shared.md")
        self.engine.customize_protocol("bproto")  # shared.md exists -> skip, inherit hash
        sidecar = json.loads((self.custom_dir / ".forked-from.json").read_text(encoding="utf-8"))
        brecs = {r["custom"]: r["sha256"] for r in sidecar["bproto"]["files"]}
        shared_rel = str((self.custom_dir / "sub-protocols" / "shared.md").relative_to(self.temp_dir))
        self.assertEqual(brecs[shared_rel], shared_sha)  # NOT None
        # And drift is tracked for bproto's shared file (not unknown-baseline).
        (self.system_dir / "sub-protocols" / "shared.md").write_text("v2", encoding="utf-8")
        drift = self.engine.check_drift()
        b_shared = [d for d in drift["drifted"]
                    if d["protocol"] == "bproto" and d["custom"].endswith("shared.md")]
        self.assertEqual(len(b_shared), 1)
        self.assertEqual(b_shared[0]["status"], "changed")

    def test_acknowledge_shared_staged_file_uses_one_snapshot(self):
        """A staged file shared by multiple protocols must yield the SAME merged
        snapshot baseline for every protocol — even if system changes again
        between detect and acknowledge."""
        self._write_two_protocols_sharing()
        self.engine.customize_protocol("aproto")
        self.engine.customize_protocol("bproto")
        shared = self.system_dir / "sub-protocols" / "shared.md"
        shared.write_text("SYSTEM shared v2", encoding="utf-8")
        self.engine.check_drift()  # stages new-shared.md (v2) once
        v2_sha = self._sha(self.custom_dir / "sub-protocols" / "new-shared.md")
        shared.write_text("SYSTEM shared v3", encoding="utf-8")  # changes again pre-ack
        self.engine.check_drift(acknowledge=True)
        sidecar = json.loads((self.custom_dir / ".forked-from.json").read_text(encoding="utf-8"))
        shared_rel = str((self.custom_dir / "sub-protocols" / "shared.md").relative_to(self.temp_dir))
        for proto in ("aproto", "bproto"):
            recs = {r["custom"]: r["sha256"] for r in sidecar[proto]["files"]}
            self.assertEqual(recs[shared_rel], v2_sha, proto)  # merged snapshot, not live v3
        self.assertNotEqual(v2_sha, self._sha(shared))  # confirm v2 != v3


class TestCustomWinsEndToEnd(CustomizationTestBase):
    def test_custom_fork_overrides_system_end_to_end(self):
        """A forked + edited custom protocol must win over system across all
        three resolution paths — proving the bootstrap copy needs no resolver
        change."""
        self._write_system_protocol("task")
        self.engine.customize_protocol("task")
        # Edit the custom copies so they differ from system.
        (self.custom_dir / "task.json").write_text(
            json.dumps({"name": "task", "description": "CUSTOM", "steps": [
                {"name": "investigation", "mode": "discussion", "description": "c",
                 "start": "@sub-protocols/task-investigation.md"}]}),
            encoding="utf-8")
        (self.custom_dir / "sub-protocols" / "task-investigation.md").write_text(
            "CUSTOM investigation text", encoding="utf-8")
        (self.custom_dir / "providers" / "codex-investigation.md").write_text(
            "CUSTOM codex investigation", encoding="utf-8")

        # JSON resolution (load_protocol_config) — custom wins.
        cfg = self._shared_state.load_protocol_config("task")
        self.assertEqual(cfg["description"], "CUSTOM")
        # Sub-protocol resolution (resolve_protocol_start_text) — custom wins.
        self.assertEqual(
            self._shared_state.resolve_protocol_start_text(
                "@sub-protocols/task-investigation.md", "task"),
            "CUSTOM investigation text",
        )
        # Provider template resolution (_load_provider_template) — custom wins.
        self.assertEqual(
            self.engine._load_provider_template("investigation", "codex", {}),
            "CUSTOM codex investigation",
        )


class TestCustomizeExternalPluginRoot(TestCase):
    """W3 regression (codex code-review): when PLUGIN_ROOT lives OUTSIDE the project
    (a real plugin install), customize_protocol must not crash on
    ``src.relative_to(self.project_root)`` — provenance records a portable
    ``${PLUGIN_ROOT}/...`` marker and check_drift resolves it. The other
    customization tests put PLUGIN_ROOT *under* the project, so they miss this."""

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp())
        self.plugin = Path(tempfile.mkdtemp())  # separate tree, outside the project
        (self.proj / "team-management" / "protocol-configs" / "custom" / "sub-protocols").mkdir(parents=True)
        (self.proj / ".claude" / "state").mkdir(parents=True)
        sysd = self.plugin / "protocol-configs"
        (sysd / "sub-protocols").mkdir(parents=True)
        (sysd / "task.json").write_text(json.dumps({
            "name": "task", "description": "d",
            "steps": [{"name": "investigation", "mode": "discussion",
                       "start": "@sub-protocols/task-investigation.md"}],
        }), encoding="utf-8")
        (sysd / "sub-protocols" / "task-investigation.md").write_text("SYS", encoding="utf-8")
        self._orig_env = os.environ.get("CLAUDE_PLUGIN_ROOT")
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(self.plugin)
        import shared_state
        self._orig_root = shared_state.PROJECT_ROOT
        shared_state.PROJECT_ROOT = self.proj
        self._ss = shared_state
        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.proj)

    def tearDown(self):
        self._ss.PROJECT_ROOT = self._orig_root
        if self._orig_env is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = self._orig_env
        shutil.rmtree(self.proj, ignore_errors=True)
        shutil.rmtree(self.plugin, ignore_errors=True)

    def test_customize_does_not_crash_and_records_plugin_marker(self):
        result = self.engine.customize_protocol("task")
        self.assertTrue(result["success"], result)
        sidecar = json.loads(
            (self.proj / "team-management" / "protocol-configs" / "custom"
             / ".forked-from.json").read_text())
        systems = [r["system"] for r in sidecar["task"]["files"]]
        self.assertTrue(any(s.startswith("${PLUGIN_ROOT}/") for s in systems), systems)
        # check_drift must resolve the marker without crashing
        drift = self.engine.check_drift()
        self.assertTrue(drift["success"], drift)


if __name__ == "__main__":
    main()
