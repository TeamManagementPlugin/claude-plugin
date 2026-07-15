#!/usr/bin/env python3
"""Lock-acquisition-failure resilience for the best-effort _state_lock consumers
(m-fix-posttooluse-lock-failure-resilience).

On a filesystem without flock support (network / synced drive), inside a
permissions-restricted state dir, or under Windows msvcrt contention,
`shared_state._state_lock()` raises OSError on ACQUISITION. The best-effort
throttle-counter and subagent-depth helpers must degrade to an unlocked
best-effort read-modify-write instead of letting the exception crash the
calling hook — the harness treats a PostToolUse / SessionStart / UserPromptSubmit
traceback as a hook failure (this broke every Bash call on a real plugin host).

Integrity writers (`set_daic_mode` / `set_task_state` / `set_protocol_state` /
`edit_state`) deliberately KEEP raising on a lock failure (fail-closed for
protected state) — the two hook CALL SITES that were unguarded
(`session-start.py` no-protocol fallback, `user-messages.py` emergency stop) are
wrapped at the call site instead, so the hook survives while the writer's
contract is unchanged.

Two layers:
  * Subprocess survival — run the real hook scripts with a DIRECTORY at
    `.claude/state/current_task.lock` so `open(lock, 'a+')` raises OSError (no
    monkeypatch; reproduces the live-host failure). Assert exit 0, no traceback.
  * Unit — patch `shared_state._state_lock` to raise OSError and assert the
    best-effort helpers degrade while the integrity writers still raise.
"""
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"

sys.path.insert(0, str(HOOKS_DIR))
import shared_state  # noqa: E402


# ---------------------------------------------------------------------------
# Subprocess harness (mirrors test_hook_stdin_guards.py)
# ---------------------------------------------------------------------------
def _make_project(tmp_path):
    """Minimal valid project tree: valid state files, no active protocol."""
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (tmp_path / "team-management" / "tasks").mkdir(parents=True)
    (state / "daic-mode.json").write_text(json.dumps({"mode": "discussion"}),
                                          encoding="utf-8")
    (state / "current_task.json").write_text(json.dumps(
        {"task": None, "branch": None, "services": [], "updated": None}),
        encoding="utf-8")
    return tmp_path


def _break_lock(project):
    """Force `_state_lock()` acquisition to fail everywhere: a directory at the
    lock path makes `open(path, 'a+')` raise OSError on both the fcntl and the
    msvcrt branch — no monkeypatch, exactly what a flock-less FS surfaces."""
    lock = project / ".claude" / "state" / "current_task.lock"
    lock.mkdir(parents=True, exist_ok=True)


def _run(hook_name, project_root, raw_stdin):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_root)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / hook_name)],
        input=raw_stdin, capture_output=True, text=True,
        cwd=str(project_root), env=env, timeout=30)
    return result.returncode, result.stdout, result.stderr


@pytest.mark.parametrize("hook_name,payload", [
    ("post-tool-use.py",
     json.dumps({"tool_name": "Bash", "tool_input": {"command": "true"}, "cwd": "."})),
    ("session-start.py",
     json.dumps({"source": "startup", "hook_event_name": "SessionStart", "cwd": "."})),
    ("user-messages.py",
     json.dumps({"prompt": "STOP", "hook_event_name": "UserPromptSubmit", "cwd": "."})),
    # task-transcript-link.py:43 increments the subagent-depth counter OUTSIDE
    # its try/except — an unguarded hook call site that relies entirely on the
    # helper now degrading (codex R3). Dispatch payload has no transcript_path,
    # so after the increment the hook sys.exit(0)s.
    ("task-transcript-link.py",
     json.dumps({"tool_name": "Task", "tool_input": {}, "cwd": "."})),
])
def test_hook_survives_lock_acquisition_failure(tmp_path, hook_name, payload):
    """With the state lock unacquirable, each hook must degrade to a no-op /
    best-effort path and exit 0 — never crash with a traceback. Reproduces the
    remote-host symptom (every Bash call tripping a broken PostToolUse hook) and
    covers the two adjacent same-root-cause set_daic_mode call sites."""
    project = _make_project(tmp_path)
    _break_lock(project)
    rc, _out, err = _run(hook_name, project, payload)
    assert "Traceback (most recent call last)" not in err, \
        f"{hook_name} crashed on lock failure:\n{err}"
    assert rc == 0, \
        f"{hook_name} exited {rc} on lock failure (expected 0); stderr={err!r}"


@pytest.mark.parametrize("hook_name,payload", [
    ("session-start.py",
     json.dumps({"source": "startup", "hook_event_name": "SessionStart", "cwd": "."})),
    ("user-messages.py",
     json.dumps({"prompt": "STOP", "hook_event_name": "UserPromptSubmit", "cwd": "."})),
])
def test_stale_implementation_mode_forced_to_discussion_on_lock_failure(
        tmp_path, hook_name, payload):
    """P1 (both reviewers): with a VALID stale "implementation" daic-mode.json AND
    a broken lock, the no-protocol fallback / emergency STOP must degrade to an
    UNLOCKED write of discussion mode — not silently leave implementation active.
    Otherwise a STOP is ineffective and a no-protocol session runs with edits
    allowed on a flock-less FS, while the banner claims 'all tools locked'."""
    project = _make_project(tmp_path)
    daic_file = project / ".claude" / "state" / "daic-mode.json"
    daic_file.write_text(json.dumps({"mode": "implementation"}), encoding="utf-8")
    _break_lock(project)
    rc, _out, err = _run(hook_name, project, payload)
    assert "Traceback (most recent call last)" not in err, err
    assert rc == 0, f"{hook_name} exited {rc} on lock failure; stderr={err!r}"
    mode = json.loads(daic_file.read_text(encoding="utf-8"))["mode"]
    assert mode == "discussion", \
        f"{hook_name}: stale implementation mode not forced to discussion (got {mode!r})"


# ---------------------------------------------------------------------------
# Unit: best-effort helpers degrade; integrity writers still raise
# ---------------------------------------------------------------------------
@contextmanager
def _raising_lock():
    """Simulate a filesystem without flock support: acquisition raises."""
    raise OSError("simulated: _state_lock acquisition failed (no flock support)")
    yield  # unreachable — keeps this a generator for @contextmanager


@pytest.fixture
def broken_lock(monkeypatch, tmp_path):
    """shared_state with a tmp STATE_DIR and a `_state_lock` that always fails
    acquisition. Helpers read these as module globals at call time, so patching
    the attributes is sufficient (same technique as test_subagent_depth.py)."""
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(shared_state, "STATE_DIR", state)
    monkeypatch.setattr(shared_state, "SUBAGENT_DEPTH_FILE",
                        state / "subagent-depth.json")
    monkeypatch.setattr(shared_state, "TASK_STATE_LOCK_FILE",
                        state / "current_task.lock")
    monkeypatch.setattr(shared_state, "DAIC_STATE_FILE",
                        state / "daic-mode.json")
    monkeypatch.setattr(shared_state, "_state_lock", _raising_lock)
    return shared_state


def test_increment_counter_file_degrades_to_unlocked(broken_lock, tmp_path):
    counter = tmp_path / "counter.txt"
    assert broken_lock.increment_counter_file(counter) == 1
    assert broken_lock.increment_counter_file(counter) == 2
    assert counter.read_text(encoding="utf-8").strip() == "2"


def test_reset_counter_file_degrades_and_resets(broken_lock, tmp_path):
    counter = tmp_path / "counter.txt"
    broken_lock.increment_counter_file(counter)
    broken_lock.increment_counter_file(counter)
    broken_lock.reset_counter_file(counter)  # must not raise
    assert broken_lock.increment_counter_file(counter) == 1  # reset took effect


def test_subagent_depth_helpers_degrade(broken_lock):
    assert broken_lock.increment_subagent_depth() == 1
    assert broken_lock.increment_subagent_depth() == 2
    assert broken_lock.decrement_subagent_depth() == 1
    broken_lock.reset_subagent_depth()  # must not raise
    assert broken_lock.read_subagent_depth() == 0


def test_integrity_writers_still_raise_on_lock_failure(broken_lock):
    """Fail-closed: the degrade must NOT leak into protected-state writers.
    Each still propagates the lock-acquisition OSError."""
    with pytest.raises(OSError):
        broken_lock.set_daic_mode("discussion")
    with pytest.raises(OSError):
        broken_lock.set_task_state("t", "b", [])
    with pytest.raises(OSError):
        broken_lock.set_protocol_state("task", 0, "investigation",
                                       "2026-01-01T00:00:00+00:00")
    with pytest.raises(OSError):
        with broken_lock.edit_state():
            pass


def test_ensure_discussion_mode_degrades_to_unlocked(broken_lock):
    """The fail-SAFE helper must FORCE discussion mode even when the lock can't be
    acquired: with a stale valid "implementation" mode on disk, a lock failure must
    degrade to an UNLOCKED write of "discussion" (not silently leave implementation
    active). Only the restrictive direction is ever written, so it can't fail open."""
    daic = broken_lock.DAIC_STATE_FILE
    daic.write_text(json.dumps({"mode": "implementation"}), encoding="utf-8")
    broken_lock.ensure_discussion_mode_best_effort()  # must not raise
    assert json.loads(daic.read_text(encoding="utf-8"))["mode"] == "discussion"


# ---------------------------------------------------------------------------
# Locked-write failure (P2): a durable-write OSError INSIDE the lock is swallowed
# best-effort (consistent with the counter helper) — the depth helper returns the
# intended value and never crashes; it does not conflate write-failure with the
# release-failure path.
# ---------------------------------------------------------------------------
@pytest.fixture
def working_lock_broken_write(monkeypatch, tmp_path):
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(shared_state, "STATE_DIR", state)
    monkeypatch.setattr(shared_state, "SUBAGENT_DEPTH_FILE",
                        state / "subagent-depth.json")
    monkeypatch.setattr(shared_state, "TASK_STATE_LOCK_FILE",
                        state / "current_task.lock")

    def _raise(*_a, **_k):
        raise OSError("simulated durable-write failure inside the lock")

    monkeypatch.setattr(shared_state, "_write_json_durable", _raise)
    return shared_state


def test_depth_degrades_on_locked_write_failure(working_lock_broken_write):
    ss = working_lock_broken_write
    # Real lock acquires; the durable write raises INSIDE the lock → swallowed.
    assert ss.increment_subagent_depth() == 1  # returns intended value, no crash
    assert ss.read_subagent_depth() == 0  # write failed → depth unchanged on disk


# ---------------------------------------------------------------------------
# Exit-after-success: an OSError from lock RELEASE/close (after the body ran)
# must NOT re-run the mutation. `_state_lock`'s f.close() (shared_state.py:788 /
# :764) is outside the inner try, so this is a real path. Guards the
# double-increment (counters) and stale-overwrite (depth) bugs codex R3 flagged.
# ---------------------------------------------------------------------------
@contextmanager
def _exit_failing_lock():
    """Acquisition succeeds; the body runs; release/close raises OSError."""
    yield
    raise OSError("simulated: lock release/close failed after the body ran")


@pytest.fixture
def exit_failing(monkeypatch, tmp_path):
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(shared_state, "STATE_DIR", state)
    monkeypatch.setattr(shared_state, "SUBAGENT_DEPTH_FILE",
                        state / "subagent-depth.json")
    monkeypatch.setattr(shared_state, "_state_lock", _exit_failing_lock)
    return shared_state


def test_counter_no_double_increment_on_release_failure(exit_failing, tmp_path):
    """The locked body already wrote 1; a release-time OSError must NOT trigger a
    second (unlocked) increment to 2."""
    counter = tmp_path / "counter.txt"
    assert exit_failing.increment_counter_file(counter) == 1
    assert counter.read_text(encoding="utf-8").strip() == "1"


def test_depth_single_write_on_release_failure(exit_failing, monkeypatch):
    """The locked depth write already persisted; a release-time OSError must NOT
    trigger a second (unlocked, stale) write that could clobber a concurrent
    update. Spy on _write_json_durable and assert exactly one depth write."""
    writes = []
    orig = shared_state._write_json_durable

    def _counting(path, data):
        if Path(path).name == "subagent-depth.json":
            writes.append(data)
        return orig(path, data)

    monkeypatch.setattr(shared_state, "_write_json_durable", _counting)
    assert exit_failing.increment_subagent_depth() == 1
    assert exit_failing.read_subagent_depth() == 1
    assert len(writes) == 1, f"expected a single depth write, got {writes}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
