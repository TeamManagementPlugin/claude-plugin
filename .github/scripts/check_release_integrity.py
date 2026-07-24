#!/usr/bin/env python3
"""Release-integrity gate for the team-management plugin.

Run by CI (`.github/workflows/ci.yml`, the `validate` job). It hard-fails (exit 1)
when a real invariant is broken, and warns (exit 0) on a maintainer convention that
is not a schema requirement. It never edits files.

`plugin/.claude-plugin/plugin.json` is the single source of the plugin version.

Hard invariants (exit 1, GitHub `::error::`):
  - plugin.json and marketplace.json are valid JSON.
  - plugin.json has non-empty `name` and `version`.
  - marketplace.json `plugins[0].source` resolves to the directory holding plugin.json.
  - SECURITY.md lists the plugin's MAJOR.MINOR series (e.g. `0.4.x`) as supported.

Soft convention (exit 0, GitHub `::warning::`):
  - marketplace.json `metadata.version` differs from the plugin version. The
    marketplace catalog's own version is independent of the plugin version (the
    plugin version is resolved from plugin.json via `source`), so a mismatch is a
    warning, not a failure.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # .github/scripts/ -> repo root
PLUGIN_JSON = ROOT / "plugin" / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
SECURITY_MD = ROOT / "SECURITY.md"

errors: list[str] = []
warnings: list[str] = []


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{_rel(path)}: file not found")
    except json.JSONDecodeError as exc:
        errors.append(f"{_rel(path)}: invalid JSON ({exc})")
    return None


plugin = load_json(PLUGIN_JSON)
market = load_json(MARKETPLACE_JSON)

version = None
if isinstance(plugin, dict):
    for field in ("name", "version"):
        if not plugin.get(field):
            errors.append(f"{_rel(PLUGIN_JSON)}: missing required field '{field}'")
    version = plugin.get("version")

if isinstance(market, dict):
    plugins = market.get("plugins") or []
    if not plugins:
        errors.append(f"{_rel(MARKETPLACE_JSON)}: 'plugins' is empty or missing")
    else:
        source = (plugins[0] or {}).get("source")
        if not source:
            errors.append(f"{_rel(MARKETPLACE_JSON)}: plugins[0].source is missing")
        else:
            resolved = (ROOT / source / ".claude-plugin" / "plugin.json").resolve()
            if resolved != PLUGIN_JSON.resolve():
                errors.append(
                    f"{_rel(MARKETPLACE_JSON)}: plugins[0].source '{source}' does not "
                    f"resolve to the plugin manifest ({_rel(PLUGIN_JSON)})"
                )
    meta_version = (market.get("metadata") or {}).get("version")
    if version and meta_version and meta_version != version:
        warnings.append(
            f"{_rel(MARKETPLACE_JSON)} metadata.version ({meta_version}) != plugin "
            f"version ({version}). The marketplace catalog version is independent of "
            f"the plugin version, so this is a warning, not a failure."
        )

if version:
    match = re.match(r"^(\d+)\.(\d+)\.", str(version))
    if not match:
        errors.append(
            f"{_rel(PLUGIN_JSON)}: version '{version}' is not MAJOR.MINOR.PATCH"
        )
    else:
        series = f"{match.group(1)}.{match.group(2)}.x"
        try:
            security = SECURITY_MD.read_text(encoding="utf-8")
        except FileNotFoundError:
            errors.append(f"{_rel(SECURITY_MD)}: file not found")
            security = ""
        supported_row = re.compile(
            r"^\|\s*" + re.escape(series) + r"\s*\|\s*(?:✅|yes|supported)",
            re.IGNORECASE | re.MULTILINE,
        )
        if not supported_row.search(security):
            errors.append(
                f"{_rel(SECURITY_MD)}: no supported-versions row covering '{series}'. "
                f"Add one when bumping to a new minor/major series."
            )

for warning in warnings:
    print(f"::warning::{warning}")
for error in errors:
    print(f"::error::{error}")

if errors:
    print(f"\nRelease-integrity check FAILED: {len(errors)} error(s).")
    sys.exit(1)

print(
    "Release-integrity check passed"
    + (f" ({len(warnings)} warning(s))." if warnings else ".")
)
