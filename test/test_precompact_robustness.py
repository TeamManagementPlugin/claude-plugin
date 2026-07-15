#!/usr/bin/env python3
"""m-enforcement-and-git-hardening — pre-compact.py entrypoint robustness.

pre-compact.py must never disrupt /compact: the harness treats ANY non-zero
PreCompact exit as a hook failure that errors out compaction. It must survive
empty/malformed stdin and a malformed protocol block (non-dict, or a non-int
current_step that would TypeError on `current_step + 1`).

Run with: python3 -m pytest test/test_precompact_robustness.py -v
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "plugin" / "hooks" / "pre-compact.py"


def _project(tmp_path, protocol=None):
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    task = {"task": "m-x", "branch": "fix/x", "services": [], "updated": "2026-07-04"}
    if protocol is not None:
        task["protocol"] = protocol
    (state / "current_task.json").write_text(json.dumps(task), encoding="utf-8")
    (state / "daic-mode.json").write_text(json.dumps({"mode": "discussion"}), encoding="utf-8")
    return tmp_path


def _run(project, stdin_text):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text, text=True,
                          capture_output=True, cwd=str(project), env=env, timeout=20)


def test_empty_stdin_exits_zero(tmp_path):
    r = _run(_project(tmp_path), "")
    assert r.returncode == 0, f"empty stdin crashed pre-compact: {r.stderr!r}"


def test_malformed_stdin_exits_zero(tmp_path):
    r = _run(_project(tmp_path), "not json at all {{{")
    assert r.returncode == 0, f"malformed stdin crashed pre-compact: {r.stderr!r}"


def test_non_int_current_step_exits_zero(tmp_path):
    proj = _project(tmp_path, protocol={
        "name": "task", "current_step": "oops",
        "current_step_name": "x", "started_at": "t"})
    r = _run(proj, '{"trigger":"manual"}')
    assert r.returncode == 0, f"non-int current_step crashed pre-compact: {r.stderr!r}"
    assert "Traceback" not in r.stderr


def test_malformed_protocol_block_exits_zero(tmp_path):
    # A protocol block present but missing the name/current_step keys must not
    # KeyError when building the status line.
    proj = _project(tmp_path, protocol={"unexpected": True})
    r = _run(proj, '{"trigger":"auto"}')
    assert r.returncode == 0, f"malformed protocol block crashed pre-compact: {r.stderr!r}"
    assert "Traceback" not in r.stderr


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
