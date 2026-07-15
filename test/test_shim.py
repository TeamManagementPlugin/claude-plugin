#!/usr/bin/env python3
"""_shim.py tests (h-hook-port-boot-detector, commit 2).

Covers the hook launcher shim: venv-python discovery, system-interpreter
selection (incl. the Windows python/py fallback via injected params), main()
exec target, and an end-to-end subprocess run proving stdin and exit code pass
through the os.execv handoff.

Run with: python3 -m pytest test/test_shim.py -v
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
SHIM = HOOKS_DIR / "_shim.py"
sys.path.insert(0, str(HOOKS_DIR))

import _shim  # noqa: E402


def test_venv_python_none_without_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    assert _shim.venv_python_path() is None


def test_venv_python_found_when_present(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    vp = venv / ("Scripts/python.exe" if _shim.os.name == "nt" else "bin/python")
    vp.parent.mkdir(parents=True)
    vp.write_text("")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    assert _shim.venv_python_path() == vp


def test_system_python_prefers_executable():
    assert _shim.system_python(os_name="posix", executable="/usr/bin/python3.11") == "/usr/bin/python3.11"


def test_system_python_windows_fallback():
    # No launching interpreter recorded → fall back to python/py via which (Windows).
    which = {"python": "C:/Python/python.exe", "py": "C:/Windows/py.exe"}.get
    assert _shim.system_python(os_name="nt", which=which, executable="") == "C:/Python/python.exe"


def test_system_python_posix_fallback():
    which = {"python3": "/usr/bin/python3"}.get
    assert _shim.system_python(os_name="posix", which=which, executable="") == "/usr/bin/python3"


def test_main_execv_target(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)  # no venv → system python
    captured = {}
    _shim.main(argv=["_shim.py", "session-start.py"],
               execv=lambda p, a: captured.update(path=p, argv=a))
    assert captured["argv"][1] == str(HOOKS_DIR / "session-start.py")
    assert captured["argv"][0] == captured["path"]


def test_missing_hook_exits_zero_not_blocking(monkeypatch):
    """A typo/rename in hooks.json (missing hook file) must NOT hard-block:
    exec'ing a nonexistent script exits 2, which on PreToolUse blocks the tool
    with a cryptic message. The shim now emits a clear diagnostic and exits 0
    (fail-open). (m-enforcement-and-git-hardening)"""
    import os
    env = {**os.environ}
    env.pop("CLAUDE_PLUGIN_DATA", None)
    result = subprocess.run(
        [sys.executable, str(SHIM), "does_not_exist_hook_xyz.py"],
        input="{}", text=True, capture_output=True, env=env, timeout=30,
    )
    assert result.returncode == 0, f"missing hook must not block (rc={result.returncode})"
    assert "not found" in result.stderr.lower(), f"no clear diagnostic: {result.stderr!r}"


def test_main_missing_hook_does_not_execv():
    """main() short-circuits with sys.exit(0) before the execv handoff when the
    hook file is absent — the interpreter never runs a missing script."""
    import pytest
    called = {"execv": False}
    with pytest.raises(SystemExit) as exc:
        _shim.main(argv=["_shim.py", "nope_hook_abc.py"],
                   execv=lambda p, a: called.__setitem__("execv", True))
    assert exc.value.code == 0
    assert called["execv"] is False


def test_subprocess_passes_stdin_and_exit_code(tmp_path, monkeypatch):
    """End-to-end: the shim execs the real hook; stdin payload and exit code
    must survive the handoff. An absolute hook path joins cleanly (Path / abs)."""
    dummy = tmp_path / "dummy_hook.py"
    dummy.write_text(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "open(__file__ + '.out', 'w').write(data)\n"
        "sys.exit(7)\n"
    )
    env = {**__import__("os").environ}
    env.pop("CLAUDE_PLUGIN_DATA", None)  # force system python (= this interpreter)
    result = subprocess.run(
        [sys.executable, str(SHIM), str(dummy)],
        input="PING-123", text=True, capture_output=True, env=env, timeout=30,
    )
    assert result.returncode == 7, f"exit code not propagated: {result.stderr!r}"
    assert (tmp_path / "dummy_hook.py.out").read_text() == "PING-123"
