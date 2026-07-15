#!/usr/bin/env python3
"""Smoke tests for frozen-paths enforcement infrastructure (T1).

Run with: python3 -m pytest test/test_frozen_paths_enforcement.py -v

Each test invokes sessions-enforce.py as a subprocess with a JSON stdin
payload (mimicking how Claude Code drives PreToolUse hooks). The hook's
exit code and stderr together describe its decision: 0 = allow, 2 = block.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "plugin" / "hooks" / "sessions-enforce.py"

# Protocol state to inject when a test needs frozen-path enforcement to fire.
# After fix/frozen-paths-experimentation-only, _load_frozen_paths gates on
# step_name == "experimentation"; tests that exercise frozen-path semantics
# must set this state explicitly.
EXPERIMENTATION_STATE = {
    "name": "optimize",
    "current_step": 3,
    "step_name": "experimentation",
    "started_at": "2026-05-05T00:00:00Z",
}


def _make_project(tmp_path: Path, *, task: str = None, branch: str = None,
                  daic_mode: str = "implementation",
                  optimize_state: dict = None,
                  protocol_state: dict = None,
                  task_frontmatter_branch: str = None,
                  task_layout: str = "file",
                  subagent_depth: int = None,
                  init_git: bool = False) -> Path:
    """Build a minimal project tree for hook execution.

    Layout:
        <tmp>/.claude/state/{daic-mode.json, current_task.json[, optimize-state.json]}
        <tmp>/team-management/tasks/<task>.md (if task supplied)

    Returns the project root path.
    """
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    tasks = tmp_path / "team-management" / "tasks"
    tasks.mkdir(parents=True)

    (state / "daic-mode.json").write_text(json.dumps({"mode": daic_mode}))
    current_task = {
        "task": task,
        "branch": branch,
        "services": [],
        "updated": "2026-05-05",
    }
    if protocol_state is not None:
        current_task["protocol"] = protocol_state
    (state / "current_task.json").write_text(json.dumps(current_task))

    if optimize_state is not None:
        (state / "optimize-state.json").write_text(json.dumps(optimize_state))

    if subagent_depth is not None:
        (state / "subagent-depth.json").write_text(json.dumps({"depth": subagent_depth}))

    if task:
        fm = task_frontmatter_branch if task_frontmatter_branch is not None else (branch or "")
        body = f"---\ntask: {task}\nbranch: {fm}\nstatus: in-progress\n---\n\n# Test task\n"
        if task_layout == "dir":
            tdir = tasks / task
            tdir.mkdir()
            (tdir / "README.md").write_text(body)
        else:
            (tasks / f"{task}.md").write_text(body)

    if init_git:
        # Minimal git repo so branch enforcement does not crash on subprocess.run
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "--allow-empty", "-m", "init", "-q"],
                       cwd=str(tmp_path), check=True)
        if branch and branch.lower() not in ("none", "null", ""):
            subprocess.run(["git", "checkout", "-q", "-b", branch],
                           cwd=str(tmp_path), check=True)

    return tmp_path


def _run_hook(project_root: Path, tool_name: str, tool_input: dict):
    """Invoke sessions-enforce.py with a stdin payload; return (rc, stderr)."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    # Run with cwd = project_root so PROJECT_ROOT detection finds .claude/
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload, capture_output=True, text=True,
        cwd=str(project_root), timeout=15,
    )
    return result.returncode, result.stderr


# ---------------------------------------------------------------------------
# Smoke test 3 — overwriting optimize-state.json is blocked at hook level
# ---------------------------------------------------------------------------

def test_overwrite_optimize_state_blocked(tmp_path):
    """Direct Edit/Write on .claude/state/optimize-state.json is blocked by
    PROTECTED_PATHS (covers Read/Grep, Edit/Write/MultiEdit, Bash sites).
    """
    project = _make_project(tmp_path,
                            task="o-test", branch="optimize/test",
                            optimize_state={"frozen_paths": []})
    target = str(project / ".claude" / "state" / "optimize-state.json")

    rc, stderr = _run_hook(project, "Write",
                           {"file_path": target, "content": "{}"})
    assert rc == 2, f"expected exit 2, got {rc}; stderr={stderr!r}"
    assert "[Protocol Engine]" in stderr, \
        f"expected '[Protocol Engine]' marker, got: {stderr!r}"


# ---------------------------------------------------------------------------
# Smoke test 5 — no overhead when optimize-state.json is absent
# ---------------------------------------------------------------------------

def test_no_optimize_state_no_overhead(tmp_path):
    """When optimize-state.json is absent, the frozen-path helper returns []
    and an Edit on a normal file does not produce any [Optimize: Frozen Path]
    stderr line. Exercises the fast-path: non-optimize users see zero impact.
    """
    project = _make_project(tmp_path,
                            task="m-foo", branch="feature/foo",
                            init_git=True)
    # Create a normal file inside the project
    target = project / "src" / "module.py"
    target.parent.mkdir()
    target.write_text("# normal file\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(target),
                            "old_string": "# normal file",
                            "new_string": "# edited"})
    # The frozen-path subsystem must NOT fire. Other enforcement might still
    # block (branch enforcement etc), but our marker must be absent.
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"unexpected frozen-path block on absent state: {stderr!r}"


# ---------------------------------------------------------------------------
# Smoke test 1 — Edit on a frozen_paths-listed file is blocked
# ---------------------------------------------------------------------------

def test_write_to_frozen_path_blocked(tmp_path):
    """Edit on a path that appears in optimize-state.json's frozen_paths
    list is blocked with the [Optimize: Frozen Path] marker, exit code 2.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    frozen_target = project / "src" / "frozen.py"
    frozen_target.parent.mkdir()
    frozen_target.write_text("# frozen training code\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(frozen_target),
                            "old_string": "# frozen training code",
                            "new_string": "# tampered"})
    assert rc == 2, f"expected exit 2, got {rc}; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr, \
        f"expected '[Optimize: Frozen Path]' marker, got: {stderr!r}"


# ---------------------------------------------------------------------------
# Smoke test 2 — Bash write to runtime-discovered TSV is blocked
# ---------------------------------------------------------------------------

def test_write_to_runtime_tsv_blocked(tmp_path):
    """Bash command with write semantics targeting a *.tsv file under the
    current task's directory is blocked by runtime frozen-path discovery,
    even when frozen_paths is empty.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": []},
        protocol_state=EXPERIMENTATION_STATE,
        task_layout="dir",
        init_git=True,
    )
    tsv = project / "team-management" / "tasks" / "o-test" / "results.tsv"
    tsv.write_text("iteration\tscore\n0\t0.0\n")

    cmd = f"echo 'oops' > team-management/tasks/o-test/results.tsv"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"expected exit 2, got {rc}; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr, \
        f"expected '[Optimize: Frozen Path]' marker, got: {stderr!r}"


def test_read_only_bash_on_tsv_not_blocked(tmp_path):
    """Counter-test for the writes-only Bash semantics: read-only access to a
    runtime-listed TSV passes through (the agent must be able to inspect
    history), even though writes are blocked.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": []},
        protocol_state=EXPERIMENTATION_STATE,
        task_layout="dir",
        init_git=True,
    )
    tsv = project / "team-management" / "tasks" / "o-test" / "results.tsv"
    tsv.write_text("iteration\tscore\n0\t0.0\n")

    # `cat` is in the read-only allowlist; the hook should exit 0 fast-path.
    rc, stderr = _run_hook(project, "Bash",
                           {"command": "cat team-management/tasks/o-test/results.tsv"})
    assert rc == 0, f"expected exit 0 (read pass-through), got {rc}; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" not in stderr


# ---------------------------------------------------------------------------
# Smoke test 4 — branch enforcement accepts optimize/<name> for task o-<name>
# ---------------------------------------------------------------------------

def test_optimize_branch_accepted(tmp_path):
    """Branch enforcement accepts the optimize/test-task branch when the
    current task is o-test-task. Verifies that the new branch_prefixes entry
    and infer_task_from_branch reverse mapping line up correctly.
    """
    project = _make_project(
        tmp_path,
        task="o-test-task", branch="optimize/test-task",
        init_git=True,
    )
    target = project / "src" / "module.py"
    target.parent.mkdir()
    target.write_text("# code under iteration\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(target),
                            "old_string": "# code under iteration",
                            "new_string": "# tweaked"})
    assert rc == 0, f"expected exit 0 (allowed), got {rc}; stderr={stderr!r}"
    # Sanity: branch enforcement should NOT complain about a mismatch.
    assert "Branch Mismatch" not in stderr
    assert "Branch Required" not in stderr


def test_brainstorm_branch_accepted(tmp_path):
    """Branch enforcement accepts the brainstorm/test-topic branch when the
    current task is b-brainstorm-test-topic. Mirrors test_optimize_branch_accepted
    for the b- → brainstorm/ mapping introduced alongside the validator
    extension (m-fix-prefix-validators-and-brainstorm-rename).
    """
    project = _make_project(
        tmp_path,
        task="b-brainstorm-test-topic", branch="brainstorm/test-topic",
        init_git=True,
    )
    target = project / "src" / "module.py"
    target.parent.mkdir()
    target.write_text("# code under brainstorm\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(target),
                            "old_string": "# code under brainstorm",
                            "new_string": "# tweaked"})
    assert rc == 0, f"expected exit 0 (allowed), got {rc}; stderr={stderr!r}"
    assert "Branch Mismatch" not in stderr
    assert "Branch Required" not in stderr


# ---------------------------------------------------------------------------
# Regression tests for path-component-boundary matching (warnings 1+2 from
# code-review) — substring overlap must NOT produce false positives.
# ---------------------------------------------------------------------------

def test_frozen_does_not_match_suffix_overlap(tmp_path):
    """Frozen 'src/foo.py' must NOT block 'src/foo.py.bak' — different files."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/foo.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    sibling = project / "src" / "foo.py.bak"
    sibling.parent.mkdir()
    sibling.write_text("# backup, not frozen\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(sibling),
                            "old_string": "# backup, not frozen",
                            "new_string": "# edited"})
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"unexpected block for sibling file: {stderr!r}"


def test_frozen_does_not_match_path_tail(tmp_path):
    """Frozen 'src/foo.py' must NOT block 'tests/src/foo.py' — distinct path."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/foo.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    other = project / "tests" / "src" / "foo.py"
    other.parent.mkdir(parents=True)
    other.write_text("# unit-test fixture\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(other),
                            "old_string": "# unit-test fixture",
                            "new_string": "# edited"})
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"unexpected block for nested path: {stderr!r}"


def test_frozen_directory_blocks_files_underneath(tmp_path):
    """Frozen 'src' (a directory entry) must block 'src/foo.py' — dir-prefix."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    inner = project / "src" / "foo.py"
    inner.parent.mkdir()
    inner.write_text("# inside frozen dir\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(inner),
                            "old_string": "# inside frozen dir",
                            "new_string": "# tampered"})
    assert rc == 2, f"expected exit 2, got {rc}; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


# ---------------------------------------------------------------------------
# Regression tests for the Bash write-target heuristic (codex W1 +
# code-review W2) — frozen path on the READ side of a pipeline / redirect
# must NOT block, but the same path on the WRITE side must block.
# ---------------------------------------------------------------------------

def test_bash_grep_frozen_redirect_to_other_not_blocked(tmp_path):
    """`grep frozen.py notes.txt > /tmp/out` reads frozen.py and writes
    /tmp/out. Frozen.py is only on the read side; writing /tmp/out is fine.
    Must NOT block.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("training_data = 1\n")
    (project / "notes.txt").write_text("hi\n")

    cmd = "grep src/frozen.py notes.txt > /tmp/out"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"false positive: frozen path on read side flagged as write: {stderr!r}"


def test_bash_pipe_to_tee_with_frozen_on_read_side_not_blocked(tmp_path):
    """`cat frozen.py | tee report.txt` writes report.txt; frozen.py is read.
    Must NOT block."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")

    cmd = "cat src/frozen.py | tee report.txt"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"false positive on pipeline read-side: {stderr!r}"


def test_bash_redirect_to_frozen_blocks(tmp_path):
    """`echo x > src/frozen.py` writes to a frozen path. Must block."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")

    cmd = "echo tampered > src/frozen.py"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"expected exit 2, got {rc}; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_subagent_bash_write_to_frozen_path_still_blocked(tmp_path):
    """A SUBAGENT's Bash write to a frozen path during experimentation must STILL be
    blocked. The frozen-path guard (_bash_targets_frozen) runs in the universal
    Bash-guard block BEFORE the subagent bypass (h-fix-subagent-bash-daic-bypass), so
    broadening the bypass to all tools must not let a subagent reach a frozen path via
    Bash. Locks in the one universal-guard invariant the other new tests don't cover."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        subagent_depth=1,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")

    cmd = "echo tampered > src/frozen.py"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"expected exit 2 (subagent frozen-path block), got {rc}; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr, \
        f"expected '[Optimize: Frozen Path]' marker for subagent Bash, got: {stderr!r}"


def test_bash_mv_frozen_blocks(tmp_path):
    """`mv src/frozen.py /tmp/` removes the frozen file from its location.
    Must block (mv on the source IS a write/delete on that path)."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")

    cmd = "mv src/frozen.py /tmp/"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"expected exit 2, got {rc}; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_frozen_normalisation_handles_dotslash_and_leading_slash(tmp_path):
    """Frozen entries written with `./` or leading `/` (project-relative
    quirks) must still match plain `src/foo.py` targets — silent-bypass
    regression guard for `_normalize_for_match`.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        # All three forms must canonicalise to "src/foo.py"
        optimize_state={"frozen_paths": ["./src/foo.py", "/src/bar.py", "src/baz.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    for name in ("foo.py", "bar.py", "baz.py"):
        (project / "src" / name).write_text(f"# {name}\n")

    for name in ("foo.py", "bar.py", "baz.py"):
        target = project / "src" / name
        rc, stderr = _run_hook(project, "Edit",
                               {"file_path": str(target),
                                "old_string": f"# {name}",
                                "new_string": "# tampered"})
        assert rc == 2, (
            f"silent bypass on {name!r} (frozen entry should canonicalise to "
            f"project-relative): rc={rc} stderr={stderr!r}"
        )
        assert "[Optimize: Frozen Path]" in stderr


# ---------------------------------------------------------------------------
# Codex round-3 regression tests: stderr/stdout fd-redirect bypass + cp
# source-arg false positive. The previous «last redirect wins» heuristic
# silently leaked writes when a fd-redirect (`2>&1`, `2>/tmp/log`) trailed
# the real `> FILE` redirect; the previous «whole segment is write side»
# fallback for cp/mv/rm conflated read sources with write destinations.
# ---------------------------------------------------------------------------

def test_bash_redirect_with_stderr_dup_still_blocks_frozen(tmp_path):
    """`echo x > src/frozen.py 2>&1` is a real write to frozen.py — the
    `2>&1` fd duplication must NOT shadow the real redirect. Iterating every
    redirect (not just the last) closes this silent-bypass."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")

    cmd = "echo tampered > src/frozen.py 2>&1"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"silent bypass on fd-redirect; rc={rc} stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_bash_redirect_with_stderr_to_file_still_blocks_frozen(tmp_path):
    """`cmd >> src/frozen.py 2>/tmp/log` writes to frozen.py; stderr-to-file
    is unrelated. Iterating every redirect catches the real target."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")

    cmd = "echo more >> src/frozen.py 2>/tmp/log"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"silent bypass on stderr-to-file; rc={rc} stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_bash_cp_frozen_as_source_not_blocked(tmp_path):
    """`cp src/frozen.py backup.py` reads frozen.py and writes backup.py.
    cp is last-positional-only: dest is backup.py, no match → no block."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")

    cmd = "cp src/frozen.py backup.py"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"false positive: cp source treated as write target: {stderr!r}"


def test_bash_cp_frozen_as_dest_blocks(tmp_path):
    """`cp other.py src/frozen.py` writes to frozen.py — last positional is
    the destination. Must block."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")
    (project / "other.py").write_text("payload\n")

    cmd = "cp other.py src/frozen.py"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"missed cp dest write; rc={rc} stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


# ---------------------------------------------------------------------------
# Codex round-4 regression tests: quoted redirect target bypass + NotebookEdit
# coverage. Round-4 fixed both — these tests guard against regression.
# ---------------------------------------------------------------------------

def test_bash_redirect_to_quoted_frozen_blocks(tmp_path):
    """`echo x > "src/frozen.py"` with double-quoted target must block.
    Without quote-stripping, the captured token retains literal quotes and
    defeats path-component matching — silent bypass.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")

    cmd = 'echo tampered > "src/frozen.py"'
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"silent bypass on quoted redirect target; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_bash_redirect_to_single_quoted_frozen_blocks(tmp_path):
    """Same as above but with single quotes (`>> 'src/frozen.py'`)."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")

    cmd = "echo more >> 'src/frozen.py'"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"silent bypass on single-quoted redirect target; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_notebook_edit_on_frozen_blocks(tmp_path):
    """NotebookEdit on a frozen .ipynb must be blocked. The tool uses
    `notebook_path` rather than `file_path`; the hook must inspect both."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["notebooks/frozen.ipynb"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    nb = project / "notebooks" / "frozen.ipynb"
    nb.parent.mkdir()
    nb.write_text('{"cells": []}')

    rc, stderr = _run_hook(project, "NotebookEdit",
                           {"notebook_path": str(nb),
                            "cell_id": "0",
                            "new_source": "tampered"})
    assert rc == 2, f"NotebookEdit not guarded; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_notebook_edit_on_optimize_state_blocks(tmp_path):
    """NotebookEdit on the protected optimize-state.json must be blocked
    by PROTECTED_PATHS — the same guard that covers Edit/Write/MultiEdit."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": []},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    target = str(project / ".claude" / "state" / "optimize-state.json")
    rc, stderr = _run_hook(project, "NotebookEdit",
                           {"notebook_path": target,
                            "cell_id": "0",
                            "new_source": "{}"})
    assert rc == 2, f"NotebookEdit not guarded against protected paths; stderr={stderr!r}"
    assert "[Protocol Engine]" in stderr


# ---------------------------------------------------------------------------
# Codex round-5 regression tests:
#   1. NotebookEdit must hit branch enforcement (was bypassing prior to fix).
#   2. Quoted redirect target with internal whitespace must block (regex `\S+`
#      stopped at the first space; round-5 changed to capture quoted strings).
# ---------------------------------------------------------------------------

def test_notebook_edit_wrong_branch_blocks(tmp_path):
    """NotebookEdit on a non-frozen .ipynb while on the WRONG branch must be
    blocked by branch-enforcement — round-5 codex critical was that the
    branch-enforcement gate omitted NotebookEdit, allowing free notebook
    writes off the task branch.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": []},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,  # creates branch optimize/test then checks it out
    )
    # Switch to a different branch (wrong for this task)
    subprocess.run(["git", "checkout", "-q", "-b", "feature/wrong"],
                   cwd=str(project), check=True)
    nb = project / "notebooks" / "free.ipynb"
    nb.parent.mkdir()
    nb.write_text('{"cells": []}')

    rc, stderr = _run_hook(project, "NotebookEdit",
                           {"notebook_path": str(nb),
                            "cell_id": "0",
                            "new_source": "x = 1"})
    assert rc == 2, f"NotebookEdit not subject to branch enforcement; stderr={stderr!r}"
    assert "Branch Mismatch" in stderr or "Branch Required" in stderr, \
        f"expected branch-enforcement message, got: {stderr!r}"


def test_bash_redirect_to_quoted_frozen_with_whitespace_blocks(tmp_path):
    """`echo x > "reports/frozen copy.tsv"` — frozen filename with internal
    whitespace inside double quotes. The redirect-target regex must capture
    the entire quoted string (not just up to the first whitespace). Round-5
    fix replaces `\\S+` with a regex alternation that handles quoted forms.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["reports/frozen copy.tsv"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "reports").mkdir()
    (project / "reports" / "frozen copy.tsv").write_text("score\n0\n")

    cmd = 'echo "tampered" > "reports/frozen copy.tsv"'
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"silent bypass on quoted target with whitespace; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_bash_redirect_to_single_quoted_target_with_whitespace_blocks(tmp_path):
    """Single-quote variant of the whitespace-quoted-target case."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["data/Frozen Models/v1.bin"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "data" / "Frozen Models").mkdir(parents=True)
    (project / "data" / "Frozen Models" / "v1.bin").write_text("blob\n")

    cmd = "cat new_v1.bin >> 'data/Frozen Models/v1.bin'"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"silent bypass on single-quoted target with whitespace; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


# ---------------------------------------------------------------------------
# Codex round-6 regression tests: GNU cp -t / --target-directory handling.
# `cp -t DIR SRC...` and `cp --target-directory=DIR SRC...` put the
# destination in the option value, not the last positional. Without
# explicit handling, frozen-file SOURCEs are spuriously blocked AND frozen
# DEST in the -t value is silently bypassed.
# ---------------------------------------------------------------------------

def test_bash_cp_target_directory_short_flag_dest_blocks(tmp_path):
    """`cp -t frozen_dir/ other.py` — destination is the -t value (frozen
    dir). Must block."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["frozen_dir"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "frozen_dir").mkdir()
    (project / "other.py").write_text("data\n")

    cmd = "cp -t frozen_dir/ other.py"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"missed cp -t dest write; rc={rc} stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_bash_cp_target_directory_short_flag_source_not_blocked(tmp_path):
    """`cp -t backup_dir src/frozen.py` — frozen.py is the SOURCE; backup_dir
    is the destination. Must NOT block (frozen path on read side)."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["src/frozen.py"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "src").mkdir()
    (project / "src" / "frozen.py").write_text("data\n")
    (project / "backup_dir").mkdir()

    cmd = "cp -t backup_dir src/frozen.py"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"false positive: cp -t source incorrectly treated as dest: {stderr!r}"


def test_bash_cp_target_directory_long_flag_eq_dest_blocks(tmp_path):
    """`cp --target-directory=frozen_dir/ other.py` — value embedded in the
    long flag form. Must block."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["frozen_dir"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "frozen_dir").mkdir()
    (project / "other.py").write_text("data\n")

    cmd = "cp --target-directory=frozen_dir/ other.py"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"missed cp --target-directory= dest write; rc={rc} stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_bash_cp_target_directory_long_flag_space_dest_blocks(tmp_path):
    """`cp --target-directory frozen_dir/ other.py` — separated form."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["frozen_dir"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "frozen_dir").mkdir()
    (project / "other.py").write_text("data\n")

    cmd = "cp --target-directory frozen_dir/ other.py"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert rc == 2, f"missed cp --target-directory <space> dest; rc={rc} stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr


def test_bash_cp_target_dir_with_options_separator_matches_real_shell(tmp_path):
    """`cp -t -- frozen_dir src.py` — per real GNU cp semantics, `-t` greedily
    consumes the next argument (`--`), so target=`--` and frozen_dir is a
    SOURCE (not a destination). The heuristic correctly does NOT block here,
    matching real-shell behaviour. Documented carve-out: best-effort heuristic.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["frozen_dir"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "frozen_dir").mkdir()
    (project / "src.py").write_text("data\n")

    cmd = "cp -t -- frozen_dir src.py"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    # The heuristic intentionally mirrors real shell semantics: target is
    # literally `--` (a file named "--"), frozen_dir and src.py are read
    # sources. Frozen-path block does NOT fire — the user's command writes
    # to a file named `--`, not to frozen_dir.
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"unexpected block on cp -t -- ... ; stderr={stderr!r}"


def test_bash_mv_unrelated_path_with_substring_overlap_not_blocked(tmp_path):
    """`mv myresults.tsv x` must NOT match frozen 'results.tsv' — different
    files. Verifies path-component matching at the token level inside the
    Bash heuristic."""
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["results.tsv"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    (project / "myresults.tsv").write_text("score\n0\n")

    cmd = "mv myresults.tsv archive.tsv"
    rc, stderr = _run_hook(project, "Bash", {"command": cmd})
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"false positive on suffix-overlap rename: {stderr!r}"


# ---------------------------------------------------------------------------
# Step-gate regression tests (fix/frozen-paths-experimentation-only):
# frozen-path enforcement is active only during the experimentation step.
# Earlier and later steps, plus the no-active-protocol case, must skip it.
# ---------------------------------------------------------------------------

def test_frozen_inactive_during_metric_script_step(tmp_path):
    """Edit on a frozen path during the metric-script step is NOT blocked.
    The metric-script step authors the measurement script, which may live
    inside a frozen directory; gating enforcement to experimentation only
    matches the documented contract in optimize-setup.md.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["tools/"]},
        protocol_state={"name": "optimize", "current_step": 1,
                        "step_name": "metric-script",
                        "started_at": "2026-05-05T00:00:00Z"},
        init_git=True,
    )
    tools_dir = project / "tools"
    tools_dir.mkdir()
    target = tools_dir / "measure.py"
    target.write_text("# initial\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(target),
                            "old_string": "# initial",
                            "new_string": "# edited"})
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"frozen-path block fired outside experimentation: {stderr!r}"


def test_frozen_active_during_experimentation_step(tmp_path):
    """Edit on a frozen path during the experimentation step IS blocked.
    Proves the step-gate restricts enforcement to a single step rather than
    disabling it entirely — companion to test_frozen_inactive_during_metric_script_step.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["tools/"]},
        protocol_state=EXPERIMENTATION_STATE,
        init_git=True,
    )
    tools_dir = project / "tools"
    tools_dir.mkdir()
    target = tools_dir / "measure.py"
    target.write_text("# initial\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(target),
                            "old_string": "# initial",
                            "new_string": "# tampered"})
    assert rc == 2, f"expected exit 2, got {rc}; stderr={stderr!r}"
    assert "[Optimize: Frozen Path]" in stderr, \
        f"expected frozen-path block in experimentation: {stderr!r}"


def test_frozen_inactive_when_no_protocol_state(tmp_path):
    """Edit on a frozen path when optimize-state.json is present but no
    protocol is active is NOT blocked. Covers the abandoned-state case:
    a stale optimize-state.json from a previous aborted run does not block
    unrelated subsequent work.
    """
    project = _make_project(
        tmp_path,
        task="o-test", branch="optimize/test",
        optimize_state={"frozen_paths": ["tools/"]},
        protocol_state=None,  # explicit: no protocol block in current_task.json
        init_git=True,
    )
    tools_dir = project / "tools"
    tools_dir.mkdir()
    target = tools_dir / "measure.py"
    target.write_text("# initial\n")

    rc, stderr = _run_hook(project, "Edit",
                           {"file_path": str(target),
                            "old_string": "# initial",
                            "new_string": "# edited"})
    assert "[Optimize: Frozen Path]" not in stderr, \
        f"frozen-path block fired without active protocol: {stderr!r}"
