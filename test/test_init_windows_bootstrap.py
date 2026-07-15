#!/usr/bin/env python3
"""Structural guard for the Windows runtime bootstrap in /team-management:init
(h-fix-windows-plugin-launch).

The provisioning lives in `plugin/commands/init.md` (a prompt the agent executes —
there is no Python function to unit-test), so these tests pin the invariants the prompt
must keep so a future edit cannot silently regress the Windows fix:

  - a "Windows runtime bootstrap" section exists and runs BEFORE the config_update step
    (config_update is MCP-gated; on a fresh broken-Windows install it cannot run yet);
  - it provisions a REAL `python3.exe` (copy of python.exe) — NOT a `.cmd` shim, which
    Windows cannot direct-spawn for the MCP server;
  - it requires the `py` launcher, documents the WindowsApps app-execution-alias
    shadowing risk, gives a manual copy fallback, and tells the user to restart;
  - the manifests intentionally keep the `python3` token (don't "fix" to python/py).

Run with: python3 -m pytest test/test_init_windows_bootstrap.py -v
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_MD = REPO_ROOT / "plugin" / "commands" / "init.md"


def _text():
    return INIT_MD.read_text(encoding="utf-8")


def test_windows_bootstrap_section_exists():
    assert "Windows runtime bootstrap" in _text()


def test_bootstrap_runs_before_config_update_step():
    """Provisioning must precede the MCP-gated config_update step — on a fresh
    broken-Windows install config_update cannot run until after the restart."""
    text = _text()
    boot = text.index("Windows runtime bootstrap")
    cfg = text.index("Apply the choice via `config_update`")
    assert boot < cfg, "Windows bootstrap section must appear before the config_update step"


def test_provisions_real_exe_not_cmd():
    """Must create a real python3.exe (copy of python.exe), and explicitly say a
    .cmd/.bat shim is insufficient because the MCP command is direct-spawned."""
    text = _text()
    assert "python3.exe" in text
    assert "python.exe" in text  # copied from
    low = text.lower()
    assert "direct-spawn" in low or "direct spawn" in low
    # .cmd must be named as NOT sufficient, not used as the shim
    assert "python3.cmd" in text  # referenced (as the rejected approach)


def test_requires_py_launcher():
    text = _text()
    assert "py launcher" in text.lower()
    assert "py --version" in text


def test_skip_check_requires_real_exe():
    """The 'python3 already works → skip' check must confirm python3 resolves to a real
    .exe (via `where python3`), not just that `--version` succeeds — a pre-existing
    python3.cmd/.bat passes `--version` but is not direct-spawnable for the MCP server
    (codex delta P2 #1)."""
    text = _text()
    assert "where python3" in text
    low = text.lower()
    assert "ends in" in low and ".exe" in low  # skip gated on a real .exe


def test_handles_python_dir_not_on_path():
    """If the Python install dir is not on PATH (installed without 'Add Python to
    PATH'), copying python3.exe there cannot make `python3` resolve — §0 must detect it
    (where python3 finds nothing) and tell the user to fix PATH (codex delta P2 #2)."""
    text = _text()
    assert "Add Python to PATH" in text
    assert "setx PATH" in text


def test_documents_windowsapps_shadowing():
    assert "WindowsApps" in _text()


def test_has_manual_copy_fallback():
    text = _text()
    assert "Copy-Item" in text and "copy " in text  # PowerShell + cmd forms


def test_tells_user_to_restart():
    assert "RESTART" in _text() or "restart Claude Code" in _text()


def test_config_update_step_defers_on_fresh_windows():
    text = _text()
    assert "SKIP this step" in text
    assert "config_intent_gate" in text  # explains why (gate hook also down pre-restart)


def test_no_clobber_idempotency_guard_documented():
    """The "create only if absent" guard is what keeps re-runs from re-copying and
    protects an unrelated user `python3.exe` — pin it in prose so a future edit can't
    silently drop it (code-review Note 2)."""
    low = _text().lower()
    assert "only if" in low and "absent" in low


def test_cmd_copy_is_non_interactive():
    """The cmd copy form must carry /Y so it never stalls on an Overwrite? prompt
    (code-review Note 1)."""
    assert "copy /Y" in _text()


def test_manifests_stay_python3_guard_documented():
    """A rule must warn against changing the manifest token to python/py (breaks macOS)."""
    low = _text().lower()
    assert "intentionally keep the `python3` token" in low or (
        "do not change it to `python`/`py`" in low)
