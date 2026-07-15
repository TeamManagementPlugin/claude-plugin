#!/usr/bin/env python3
"""Regression guard for the protocol_engine.py mixin split (l-structural-refactors).

Locks the two invariants that make the split test-safe:

  INV1: protocol_engine.py keeps `import subprocess` / `import os`, so the
        72 `patch("protocol_engine.subprocess.run")` + `patch("protocol_engine.os.environ")`
        sites in the optimize test suites still resolve their target.
  INV2: the extracted mixin methods keep qualified `subprocess.run(...)`, so a patch
        applied via protocol_engine's reference (the shared singleton subprocess module)
        still intercepts calls made inside OptimizeCompletionMixin / AIProvidersMixin.

A future refactor that switches the mixin to `from subprocess import run`, or drops
`import subprocess` from protocol_engine, would silently break those 72 patches; this
test fails loudly instead.

Run with: python3 -m pytest test/test_engine_split_patch_invariant.py -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

HOOKS_DIR = Path(__file__).parent.parent / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


class TestEngineSplitStructure(unittest.TestCase):
    def test_mixins_composed_into_engine(self):
        import protocol_engine as pe
        mro = [c.__name__ for c in pe.ProtocolEngine.__mro__]
        self.assertIn("OptimizeCompletionMixin", mro)
        self.assertIn("AIProvidersMixin", mro)

    def test_private_globals_reexported_from_protocol_engine(self):
        # test_ai_providers_config_drift.py imports these FROM protocol_engine.
        import protocol_engine as pe
        self.assertTrue(hasattr(pe, "_PHASE_REGISTRY"))
        self.assertTrue(hasattr(pe, "_PHASES_TEMPLATE_DRIVEN"))

    def test_protocol_engine_keeps_subprocess_and_os_imports(self):
        # INV1: the patch target `protocol_engine.subprocess` / `.os` must resolve.
        import protocol_engine as pe
        self.assertTrue(hasattr(pe, "subprocess"))
        self.assertTrue(hasattr(pe, "os"))


class TestPatchInvariant(unittest.TestCase):
    def _engine(self):
        import protocol_engine as pe
        engine = pe.ProtocolEngine.__new__(pe.ProtocolEngine)
        engine.project_root = Path("/tmp")
        return engine

    def test_patch_protocol_engine_subprocess_intercepts_mixin_call(self):
        """patch('protocol_engine.subprocess.run') must intercept a subprocess call
        made inside an OptimizeCompletionMixin method living in optimize_completion.py."""
        engine = self._engine()
        mock_result = MagicMock(returncode=0, stdout="feature/x\n", stderr="")
        with patch("protocol_engine.subprocess.run", return_value=mock_result) as m:
            branch = engine._current_branch()
        self.assertTrue(
            m.called,
            "patch('protocol_engine.subprocess.run') did NOT intercept the mixin's "
            "subprocess.run call — the split broke the patch invariant (INV1/INV2).",
        )
        self.assertEqual(branch, "feature/x")


if __name__ == "__main__":
    unittest.main()
