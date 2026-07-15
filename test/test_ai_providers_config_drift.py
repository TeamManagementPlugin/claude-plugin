#!/usr/bin/env python3
"""Drift guard: the per-phase `include_in_*` keys writable through the in-Claude
config flow (config_update → `_CONFIG_SCHEMA` in plugin/mcp/tools/config.py) MUST
exactly match the keys the protocol engine reads via
`_resolve_ai_providers_via_registry` (`_PHASE_REGISTRY` config_flags).

Originally this compared the pip installer's `AIIntegrationConfigurator.PHASE_FLAGS`
against the engine. The installer was retired (m-installer-retirement); the writer
of those keys is now `config_update`, whose allowlist is `_CONFIG_SCHEMA`. The guard
still prevents the `include_in_architecture` precedent — a key written by the config
flow but never read by any pre_func, sitting in config.json as dead UI surface.

Run with: python3 -m pytest test/test_ai_providers_config_drift.py -v
"""

import ast
import sys
from pathlib import Path
from unittest import TestCase, main

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "plugin" / "hooks"))

CONFIG_PY = PROJECT_ROOT / "plugin" / "mcp" / "tools" / "config.py"
_CONFIG_PREFIX = "ai_providers."


def _config_schema_include_flags():
    """AST-parse the `_CONFIG_SCHEMA` literal out of config.py and return the bare
    `include_in_*` flag names (with the `ai_providers.` prefix stripped).

    Reads config.py as data rather than importing it: config.py does
    `from core.project import ...` (an absolute import resolvable only with
    plugin/mcp on sys.path) and has import side-effects — a drift guard wants the
    file's declared keys, not its runtime.
    """
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # `_CONFIG_SCHEMA: Dict[str, Any] = {...}` is an annotated assignment
        # (ast.AnnAssign), so handle both plain and annotated assignments.
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        else:
            continue
        if "_CONFIG_SCHEMA" not in target_names or not isinstance(value, ast.Dict):
            continue
        flags = set()
        for key_node in value.keys:
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                k = key_node.value
                if k.startswith(_CONFIG_PREFIX + "include_in_"):
                    flags.add(k[len(_CONFIG_PREFIX):])
        return flags
    raise AssertionError("_CONFIG_SCHEMA dict literal not found in config.py")


class ConfigDriftTest(TestCase):

    def test_config_keys_match_engine_keys(self):
        """The 6 per-phase include_in_* keys writable via config_update must be
        identical to the keys the engine reads."""
        from protocol_engine import _PHASE_REGISTRY

        config_keys = _config_schema_include_flags()
        engine_keys = {entry["config_flag"] for entry in _PHASE_REGISTRY.values()}

        self.assertEqual(
            config_keys, engine_keys,
            f"Drift between config_update-writable keys and engine-read keys: "
            f"config-only = {config_keys - engine_keys}, "
            f"engine-only = {engine_keys - config_keys}. "
            "Update either _CONFIG_SCHEMA (config.py) or _PHASE_REGISTRY to align them."
        )

    def test_phase_count_is_six(self):
        from protocol_engine import _PHASE_REGISTRY
        self.assertEqual(len(_config_schema_include_flags()), 6,
                         "Expected 6 include_in_* keys in config.py _CONFIG_SCHEMA.")
        self.assertEqual(len(_PHASE_REGISTRY), 6, "Expected 6 phase_registry entries.")

    def test_no_legacy_keys_in_config_schema(self):
        """Legacy keys must not appear in config.py _CONFIG_SCHEMA — if they did,
        config_update would re-write them as live keys, defeating the deprecation
        policy."""
        config_keys = _config_schema_include_flags()
        legacy = {"include_in_architecture", "include_in_exploration"}
        self.assertEqual(config_keys & legacy, set(),
                         "Legacy keys leaked back into config.py _CONFIG_SCHEMA.")

    def test_template_subpaths_unique_and_present(self):
        """Each phase has a unique template_subpath; the 10 markdown templates
        (5 phases × 2 providers) exist in plugin source."""
        from protocol_engine import _PHASE_REGISTRY, _PHASES_TEMPLATE_DRIVEN
        subpaths = [entry["template_subpath"] for entry in _PHASE_REGISTRY.values()]
        self.assertEqual(len(subpaths), len(set(subpaths)), "duplicate template_subpath")

        dev_providers_dir = PROJECT_ROOT / "plugin" / "protocol-configs" / "providers"
        for phase_key in _PHASES_TEMPLATE_DRIVEN:
            entry = _PHASE_REGISTRY[phase_key]
            for provider in ("codex", "agy"):
                fname = f"{provider}-{entry['template_subpath']}.md"
                self.assertTrue(
                    (dev_providers_dir / fname).exists(),
                    f"missing template {fname} in {dev_providers_dir}"
                )

    def test_registered_handlers_match_registry(self):
        """Every phase_registry entry must have a registered handler."""
        from protocol_engine import ProtocolEngine, _PHASE_REGISTRY
        engine = ProtocolEngine(PROJECT_ROOT)
        handlers = engine._build_handlers()
        for entry in _PHASE_REGISTRY.values():
            self.assertIn(entry["func_name"], handlers,
                          f"handler {entry['func_name']} not registered")

    def test_get_available_funcs_lists_all_six(self):
        """get_available_funcs metadata must include all 6 AI provider entries."""
        from protocol_engine import ProtocolEngine, _PHASE_REGISTRY
        engine = ProtocolEngine(PROJECT_ROOT)
        meta = engine.get_available_funcs()
        names = {f["name"] for f in meta["funcs"]}
        for entry in _PHASE_REGISTRY.values():
            self.assertIn(entry["func_name"], names,
                          f"{entry['func_name']} missing from metadata")
        # No discrepancies between handlers and metadata
        self.assertNotIn("discrepancies", meta,
                         f"handler/metadata discrepancy: {meta.get('discrepancies')}")


if __name__ == "__main__":
    main()
