#!/usr/bin/env python3
"""Hook launcher shim — stdlib only.

hooks.json invokes this as::

    <python> ${CLAUDE_PLUGIN_ROOT}/hooks/_shim.py <hook-filename> [args...]

It picks the venv python (if a venv exists under ``${CLAUDE_PLUGIN_DATA}``), else
the interpreter that launched the shim, else a system python (Windows: python/py),
then hands off to the real hook with stdin/stdout/stderr intact so the hook can
read its JSON payload from stdin and its exit code reaches Claude Code unchanged.

Pure stdlib: runs before the venv exists.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent


def venv_python_path():
    """The venv python under ``${CLAUDE_PLUGIN_DATA}/venv``, or None if no venv yet."""
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not data:
        return None
    venv = Path(data) / "venv"
    cand = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return cand if cand.exists() else None


def _candidate_names(os_name):
    # Windows has no `python3` (spike F6) — only `python` / `py`.
    return ("python", "py") if os_name == "nt" else ("python3", "python")


def system_python(os_name=None, which=None, executable=None) -> str:
    """Best available system interpreter. Params are injectable for testing."""
    os_name = os.name if os_name is None else os_name
    which = shutil.which if which is None else which
    executable = sys.executable if executable is None else executable
    if executable:
        return executable
    for name in _candidate_names(os_name):
        found = which(name)
        if found:
            return found
    return "python"  # last resort; execv/subprocess surfaces the error if absent


def main(argv=None, execv=None) -> None:
    argv = sys.argv if argv is None else argv
    if len(argv) < 2:
        sys.stderr.write("[_shim] missing hook name\n")
        sys.exit(1)
    hook = HOOKS_DIR / argv[1]
    if not hook.exists():
        # A typo/rename in hooks.json (missing hook file) must NOT hard-block:
        # exec'ing a nonexistent script exits 2, which on PreToolUse BLOCKS the
        # tool call with a cryptic "can't open file" message. Emit a clear
        # diagnostic and exit 0 (fail-open) — a manifest typo is a config error,
        # not a policy verdict, and every event treats exit 0 as "proceed".
        sys.stderr.write(f"[_shim] hook file not found: {hook} — check hooks.json\n")
        sys.exit(0)
    interp = venv_python_path() or system_python()
    target = [str(interp), str(hook), *argv[2:]]
    if os.name == "nt":
        # os.execv has stdio/exit-status quirks on Windows — proxy via subprocess
        # so the hook's stdin payload and exit code propagate correctly.
        sys.exit(subprocess.run(target).returncode)
    (execv or os.execv)(str(interp), target)


if __name__ == "__main__":
    main()
