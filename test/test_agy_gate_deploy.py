#!/usr/bin/env python3
"""Tests for the agy read-only gate deployment (shared_state.py).

Covers ``_agy_enabled`` (the dual-gate predicate mirroring the AI-provider
resolver), ``ensure_agy_readonly_gate_deployed`` (merge-aware, idempotent,
best-effort), and ``agy_gate_is_deployed`` (the wrapper preflight signal).

Run with: python3 -m pytest test/test_agy_gate_deploy.py -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO / "plugin" / "hooks"
for _p in (str(_REPO), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import shared_state  # noqa: E402

PLUGIN_ROOT = _REPO / "plugin"          # real source of templates/agy-readonly-gate.py
HOOK_NAME = shared_state._AGY_GATE_HOOK_NAME
SCRIPT = shared_state._AGY_GATE_SCRIPT


class AgyEnabledPredicate(unittest.TestCase):
    def test_true_only_when_dual_gated(self):
        cfg = {"ai_providers": {"enabled_providers": ["codex", "agy"]}, "agy": {"enabled": True}}
        self.assertTrue(shared_state._agy_enabled(cfg))

    def test_false_when_not_in_enabled_providers(self):
        cfg = {"ai_providers": {"enabled_providers": ["codex"]}, "agy": {"enabled": True}}
        self.assertFalse(shared_state._agy_enabled(cfg))

    def test_false_when_agy_enabled_flag_off(self):
        cfg = {"ai_providers": {"enabled_providers": ["agy"]}, "agy": {"enabled": False}}
        self.assertFalse(shared_state._agy_enabled(cfg))

    def test_false_on_missing_or_malformed_config(self):
        for bad in ({}, {"ai_providers": "x"}, {"agy": "y", "ai_providers": {"enabled_providers": ["agy"]}}, None, []):
            self.assertFalse(shared_state._agy_enabled(bad))


class DeployGate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.hooks_json = self.tmp / ".agents" / "hooks.json"
        self.script = self.tmp / ".agents" / SCRIPT

    def test_deploy_creates_both_artifacts(self):
        wrote = shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        self.assertTrue(wrote)
        self.assertTrue(self.script.exists(), "gate script deployed")
        data = json.loads(self.hooks_json.read_text())
        self.assertIn(HOOK_NAME, data)
        self.assertEqual(data[HOOK_NAME]["PreToolUse"][0]["matcher"], "*")
        # command is relative (portable, cwd=.agents/)
        cmd = data[HOOK_NAME]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, f"python3 {SCRIPT}")
        self.assertNotIn("/", cmd, "command must be relative, no baked absolute path")

    def test_deployed_script_matches_source_bytes(self):
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        src = (PLUGIN_ROOT / "templates" / SCRIPT).read_bytes()
        self.assertEqual(self.script.read_bytes(), src)

    def test_merge_preserves_user_hook(self):
        self.hooks_json.parent.mkdir(parents=True)
        self.hooks_json.write_text(json.dumps({
            "my-linter": {"PostToolUse": [{"matcher": "run_command",
                                           "hooks": [{"command": "./lint.sh"}]}]}
        }))
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        data = json.loads(self.hooks_json.read_text())
        self.assertIn("my-linter", data, "user hook preserved")
        self.assertIn(HOOK_NAME, data, "our hook added")

    def test_idempotent_second_call_no_rewrite(self):
        self.assertTrue(shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT))
        first = self.hooks_json.read_bytes()
        self.assertFalse(shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT),
                         "second call is a no-op (already current)")
        self.assertEqual(self.hooks_json.read_bytes(), first)

    def test_malformed_existing_hooks_json_left_untouched(self):
        self.hooks_json.parent.mkdir(parents=True)
        self.hooks_json.write_text("[ this is a json array not object ]")
        original = self.hooks_json.read_bytes()
        wrote = shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        self.assertFalse(wrote)
        self.assertEqual(self.hooks_json.read_bytes(), original, "not clobbered")

    def test_none_plugin_root_is_noop(self):
        self.assertFalse(shared_state.ensure_agy_readonly_gate_deployed(self.tmp, None))
        self.assertFalse(self.script.exists())

    def test_refresh_on_stale_script(self):
        self.script.parent.mkdir(parents=True)
        self.script.write_text("# stale gate contents")
        self.hooks_json.write_text(json.dumps({HOOK_NAME: shared_state._agy_gate_hooks_entry()}))
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        self.assertEqual(self.script.read_bytes(),
                         (PLUGIN_ROOT / "templates" / SCRIPT).read_bytes(),
                         "stale script refreshed to source")

    # --- .agents/ gitignore-on-deploy (m-fix-agy-gitignore-agents-dir) ---
    def _gitignore_lines(self):
        gi = self.tmp / ".gitignore"
        text = gi.read_text(encoding="utf-8") if gi.exists() else ""
        return [ln.rstrip() for ln in text.splitlines() if ln.rstrip()]

    def test_deploy_gitignores_agents_dir(self):
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        self.assertIn(".agents/", self._gitignore_lines(),
                      ".agents/ added to project .gitignore on deploy")

    def test_gitignore_idempotent_on_second_deploy(self):
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        self.assertEqual(self._gitignore_lines().count(".agents/"), 1,
                         "no duplicate .agents/ line across repeated deploys")

    def test_gitignore_preserves_other_lines(self):
        (self.tmp / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        lines = self._gitignore_lines()
        self.assertIn("node_modules/", lines)
        self.assertIn(".agents/", lines)

    def test_gitignore_recognizes_blanket_no_slash(self):
        (self.tmp / ".gitignore").write_text(".agents\n", encoding="utf-8")
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        lines = self._gitignore_lines()
        self.assertIn(".agents", lines)
        self.assertNotIn(".agents/", lines)  # already covered → no duplicate append

    def test_gitignore_recognizes_leading_slash_forms(self):
        # SC#2: a pre-existing blanket rule in ANY of the four covering forms is
        # left untouched. `.agents/` and `.agents` are exercised above; cover the
        # two root-anchored spellings directly (not just by analogy to `.claude/`).
        for form in ("/.agents/", "/.agents"):
            with self.subTest(form=form):
                (self.tmp / ".gitignore").write_text(form + "\n", encoding="utf-8")
                shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
                lines = self._gitignore_lines()
                self.assertIn(form, lines)
                self.assertNotIn(".agents/", lines)  # already covered → not appended

    def test_none_plugin_root_writes_no_gitignore(self):
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, None)
        self.assertFalse((self.tmp / ".gitignore").exists(),
                         "None plugin_root is a no-op → no spurious .gitignore")


class GateIsDeployed(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_true_after_deploy(self):
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        self.assertTrue(shared_state.agy_gate_is_deployed(self.tmp))

    def test_false_when_nothing_deployed(self):
        self.assertFalse(shared_state.agy_gate_is_deployed(self.tmp))

    def test_false_when_script_missing(self):
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        (self.tmp / ".agents" / SCRIPT).unlink()
        self.assertFalse(shared_state.agy_gate_is_deployed(self.tmp))

    def test_false_when_entry_tampered(self):
        shared_state.ensure_agy_readonly_gate_deployed(self.tmp, PLUGIN_ROOT)
        hj = self.tmp / ".agents" / "hooks.json"
        data = json.loads(hj.read_text())
        # point the hook at a different (malicious) script
        data[HOOK_NAME]["PreToolUse"][0]["hooks"][0]["command"] = "python3 evil.py"
        hj.write_text(json.dumps(data))
        self.assertFalse(shared_state.agy_gate_is_deployed(self.tmp),
                         "wrong command target must fail the preflight")

    def test_false_on_malformed_hooks_json(self):
        (self.tmp / ".agents").mkdir(parents=True)
        (self.tmp / ".agents" / SCRIPT).write_text("x")
        (self.tmp / ".agents" / "hooks.json").write_text("not json")
        self.assertFalse(shared_state.agy_gate_is_deployed(self.tmp))


if __name__ == "__main__":
    unittest.main()
