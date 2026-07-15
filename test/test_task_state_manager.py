#!/usr/bin/env python3
"""TaskStateManager guard tests (l-dead-code-removal).

After the multi-session scaffold was removed, the live surface is
get_task_state_dir / get_transcripts_dir / cleanup_task_state. The
empty-task-name guard is the load-bearing new behaviour: without it,
get_task_state_dir("") resolves to the tasks root and cleanup_task_state("")
would rmtree the entire tasks tree.

Run with: python3 -m pytest test/test_task_state_manager.py -v
"""

import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugin" / "hooks"))

import task_state_manager  # noqa: E402
from task_state_manager import TaskStateManager  # noqa: E402


def _manager(tmp: Path) -> TaskStateManager:
    return TaskStateManager(tmp / ".claude" / "state")


# --- empty-name guard (the rmtree hazard) ---------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "\t", None, ".", "./"])
def test_get_task_state_dir_rejects_empty_or_root(bad):
    # "" / whitespace / None → empty-name guard; "." / "./" → resolve-to-root guard.
    with tempfile.TemporaryDirectory() as d:
        mgr = _manager(Path(d))
        with pytest.raises(ValueError):
            mgr.get_task_state_dir(bad)


@pytest.mark.parametrize("bad", ["", "   ", None, ".", "./"])
def test_cleanup_task_state_returns_false_on_empty_or_root(bad):
    with tempfile.TemporaryDirectory() as d:
        mgr = _manager(Path(d))
        assert mgr.cleanup_task_state(bad) is False


@pytest.mark.parametrize("bad", ["", ".", "./"])
def test_cleanup_bad_name_does_not_wipe_tasks_root(bad):
    """cleanup_task_state("" | "." | "./") must NOT rmtree the whole tasks tree."""
    with tempfile.TemporaryDirectory() as d:
        mgr = _manager(Path(d))
        # Seed a sibling task dir + a file directly under tasks/
        sibling = mgr.tasks_dir / "h-other-task"
        sibling.mkdir(parents=True)
        (sibling / "session.json").write_text("{}", encoding="utf-8")

        assert mgr.cleanup_task_state(bad) is False
        # The tasks root and the sibling must still be intact.
        assert mgr.tasks_dir.exists()
        assert sibling.exists()
        assert (sibling / "session.json").exists()


# --- path traversal still guarded -----------------------------------------

def test_get_task_state_dir_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as d:
        mgr = _manager(Path(d))
        with pytest.raises(ValueError):
            mgr.get_task_state_dir("../escape")


# --- happy path: cleanup targets only the named task ----------------------

def test_cleanup_removes_only_named_task():
    with tempfile.TemporaryDirectory() as d:
        mgr = _manager(Path(d))
        target = mgr.tasks_dir / "m-target"
        keep = mgr.tasks_dir / "m-keep"
        for t in (target, keep):
            t.mkdir(parents=True)
            (t / "marker").write_text("x", encoding="utf-8")

        assert mgr.cleanup_task_state("m-target") is True
        assert not target.exists()
        assert keep.exists()


def test_cleanup_missing_task_returns_false():
    with tempfile.TemporaryDirectory() as d:
        mgr = _manager(Path(d))
        assert mgr.cleanup_task_state("never-existed") is False


# --- transcripts dir path -------------------------------------------------

def test_get_transcripts_dir_path():
    with tempfile.TemporaryDirectory() as d:
        mgr = _manager(Path(d))
        tdir = mgr.get_transcripts_dir("h-task")
        # get_task_state_dir resolves symlinks (path-traversal guard), so compare
        # against the resolved task dir + the appended "transcripts" component.
        assert tdir == (mgr.tasks_dir / "h-task").resolve() / "transcripts"
        assert tdir.name == "transcripts"
        assert tdir.parent.name == "h-task"


def test_module_level_get_task_state_manager_removed():
    """The dead module-level duplicate is gone; shared_state owns the factory."""
    assert not hasattr(task_state_manager, "get_task_state_manager")
