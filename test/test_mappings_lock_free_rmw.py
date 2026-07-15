#!/usr/bin/env python3
"""Provider mappings files: lock-free read-modify-write regression (m-fix-mappings-lock-free-rmw).

`.claude/state/<provider>-mappings.json` is read-modify-written from two
independent processes (the PostToolUse auto-sync hook and the long-lived MCP
server). `_save_mappings` is atomic (no torn files) but the unlocked
`_load_mappings() -> mutate -> _save_mappings()` sequence lost one writer's
update when two RMWs interleaved. The fix routes every mutation through
`IssueTrackingTaskSync._locked_mapping_update(mutator)`, which serialises the
whole RMW under `shared_state._state_lock()`.

Failure model (two markers, stricter than the counter helpers because
`_save_mappings` is a durable integrity write that does NOT swallow its own
OSError):
  * lock ACQUISITION OSError  -> one UNLOCKED best-effort RMW (never crash a hook)
  * OSError INSIDE the held lock -> PROPAGATE (never silent unlocked fallback)
  * release/close OSError after the body persisted -> return the persisted result
  * corrupt-file RuntimeError from _load_mappings -> propagate unchanged

Also covers the `_ensure_mappings_file()` check-then-write TOCTOU fix
(atomic create-only `open(path, 'x')`).

Run with: python3 -m pytest test/test_mappings_lock_free_rmw.py -v
"""

import contextlib
import json
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import issue_provider_base  # noqa: E402
import shared_state  # noqa: E402
from issue_provider_base import IssueTrackingTaskSync  # noqa: E402


class _StubSync(IssueTrackingTaskSync):
    """Concrete subclass that skips the provider-backed base __init__ — the only
    attribute the mapping machinery needs is `mappings_file`."""

    def __init__(self, mappings_file):
        self.mappings_file = mappings_file

    def link_task_to_issue(self, *a, **k):
        return True

    def import_issue_as_task(self, *a, **k):
        return ""

    def create_issue_from_task(self, *a, **k):
        return None

    def sync_task_status_to_issue(self, *a, **k):
        return None


@pytest.fixture
def sync(tmp_path):
    return _StubSync(tmp_path / "gitlab-mappings.json")


def _read(f):
    return json.loads(Path(f).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# SC1 — every mutation path runs under _state_lock()                          #
# --------------------------------------------------------------------------- #

def test_unlink_task_runs_under_state_lock(sync, monkeypatch):
    """unlink_task (base) must route its RMW through _state_lock()."""
    sync._save_mappings({"m-x": {"issue_id": "1"}})

    entered = []

    @contextlib.contextmanager
    def spy_lock():
        entered.append(True)
        yield

    monkeypatch.setattr(issue_provider_base, "_state_lock", spy_lock)
    sync.unlink_task("m-x")

    assert entered, "unlink_task did not acquire _state_lock()"
    assert sync._load_mappings() == {}  # the delete still persisted


def test_locked_mapping_update_runs_under_state_lock(sync, monkeypatch):
    """The shared helper acquires the lock and persists the mutation."""
    entered = []

    @contextlib.contextmanager
    def spy_lock():
        entered.append(True)
        yield

    monkeypatch.setattr(issue_provider_base, "_state_lock", spy_lock)

    def mut(mappings):
        mappings["m-y"] = {"issue_id": "9"}
        return True

    assert sync._locked_mapping_update(mut) is True
    assert entered, "_locked_mapping_update did not acquire _state_lock()"
    assert _read(sync.mappings_file) == {"m-y": {"issue_id": "9"}}


def test_gitlab_link_runs_under_state_lock(tmp_path, monkeypatch):
    """A representative provider site (GitLab link) routes through the lock."""
    from gitlab.task_sync import GitLabTaskSync

    s = object.__new__(GitLabTaskSync)  # skip the heavy provider __init__
    s.mappings_file = tmp_path / "gitlab-mappings.json"

    class _Gitlab:
        def get_issue(self, issue_id):
            return {"id": 1, "iid": 2, "web_url": "u", "created_at": "t"}

    s.gitlab = _Gitlab()

    entered = []

    @contextlib.contextmanager
    def spy_lock():
        entered.append(True)
        yield

    monkeypatch.setattr(issue_provider_base, "_state_lock", spy_lock)
    s.link_task_to_issue("m-z", 2)

    assert entered, "GitLab link_task_to_issue did not acquire _state_lock()"
    assert "m-z" in _read(s.mappings_file)


def test_jira_link_runs_under_state_lock(tmp_path, monkeypatch):
    """Jira link (no network in the RMW) routes through the lock too."""
    from jira_utils import JiraTaskSync

    s = object.__new__(JiraTaskSync)
    s.mappings_file = tmp_path / "jira-mappings.json"

    entered = []

    @contextlib.contextmanager
    def spy_lock():
        entered.append(True)
        yield

    monkeypatch.setattr(issue_provider_base, "_state_lock", spy_lock)
    assert s.link_task_to_issue("m-j", "PROJ-1") is True

    assert entered, "Jira link_task_to_issue did not acquire _state_lock()"
    assert "m-j" in _read(s.mappings_file)


# --------------------------------------------------------------------------- #
# SC2 — degrade / propagate semantics                                          #
# --------------------------------------------------------------------------- #

def test_lock_acquisition_failure_degrades_without_raising(sync, monkeypatch):
    """A lock that cannot be ACQUIRED must not raise out of the sync path — the
    RMW still runs unlocked (best-effort) and persists."""

    @contextlib.contextmanager
    def failing_lock():
        raise OSError("no flock on this FS")
        yield  # pragma: no cover — unreachable

    monkeypatch.setattr(issue_provider_base, "_state_lock", failing_lock)

    def mut(mappings):
        mappings["m-degraded"] = {"issue_id": "7"}
        return True

    # Must NOT raise.
    assert sync._locked_mapping_update(mut) is True
    # And the unlocked degrade still wrote the update.
    assert _read(sync.mappings_file) == {"m-degraded": {"issue_id": "7"}}


def test_unlink_survives_lock_acquisition_failure(sync, monkeypatch):
    """End-to-end: unlink_task on a flock-less FS degrades, never raises."""
    sync._save_mappings({"m-x": {"issue_id": "1"}})

    @contextlib.contextmanager
    def failing_lock():
        raise OSError("no flock")
        yield  # pragma: no cover

    monkeypatch.setattr(issue_provider_base, "_state_lock", failing_lock)
    sync.unlink_task("m-x")  # must not raise
    assert sync._load_mappings() == {}


def test_body_oserror_under_held_lock_propagates(sync, monkeypatch):
    """An OSError raised INSIDE the held lock is a real integrity failure — it
    must PROPAGATE, never be misclassified as an acquisition failure and retried
    with an UNLOCKED write (which would reintroduce the race)."""

    @contextlib.contextmanager
    def working_lock():
        yield  # acquisition succeeds

    monkeypatch.setattr(issue_provider_base, "_state_lock", working_lock)

    calls = {"n": 0}

    def boom_save(mappings):
        calls["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(sync, "_save_mappings", boom_save)

    with pytest.raises(OSError):
        sync._locked_mapping_update(lambda m: (m.__setitem__("k", 1), True)[1])

    # Exactly one save attempt — the helper did NOT fall through to an unlocked retry.
    assert calls["n"] == 1


def test_release_failure_after_persist_returns_result(sync, monkeypatch):
    """An OSError on lock RELEASE/close AFTER the body already persisted returns
    the persisted result without re-running the RMW (no double write)."""

    @contextlib.contextmanager
    def close_fails_lock():
        yield
        raise OSError("close failed")  # after the body ran

    monkeypatch.setattr(issue_provider_base, "_state_lock", close_fails_lock)

    calls = {"n": 0}
    real_save = sync._save_mappings

    def counting_save(mappings):
        calls["n"] += 1
        return real_save(mappings)

    monkeypatch.setattr(sync, "_save_mappings", counting_save)

    def mut(mappings):
        mappings["m-once"] = {"issue_id": "3"}
        return True

    assert sync._locked_mapping_update(mut) is True
    assert calls["n"] == 1  # body ran exactly once; no unlocked retry
    assert _read(sync.mappings_file) == {"m-once": {"issue_id": "3"}}


def test_corrupt_mappings_raises_runtimeerror(sync, monkeypatch):
    """A corrupt-file RuntimeError from _load_mappings is NOT an OSError and must
    escape the helper unchanged (never swallowed as a lock failure)."""
    sync.mappings_file.write_text("{ corrupt not json", encoding="utf-8")

    @contextlib.contextmanager
    def working_lock():
        yield

    monkeypatch.setattr(issue_provider_base, "_state_lock", working_lock)

    with pytest.raises(RuntimeError):
        sync._locked_mapping_update(lambda m: True)


# --------------------------------------------------------------------------- #
# SC3 — no lost update under concurrent writers (real flock)                   #
# --------------------------------------------------------------------------- #

def test_concurrent_writers_no_lost_update(tmp_path, monkeypatch):
    """Two threads each increment their OWN key N times through the helper using
    the REAL _state_lock (redirected to a tmp lock file). Serialisation must
    leave BOTH keys at N — a lost RMW would leave one below N.

    (fcntl flock on POSIX serialises even two threads of one process because each
    _state_lock() opens the lock file afresh -> separate open file descriptions.)
    """
    state_dir = tmp_path / ".claude" / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(shared_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(shared_state, "TASK_STATE_LOCK_FILE", state_dir / "current_task.lock")

    mappings_file = tmp_path / "gitlab-mappings.json"
    s = _StubSync(mappings_file)
    s._save_mappings({"a": 0, "b": 0})

    N = 200

    def worker(key):
        for _ in range(N):
            def mut(mappings, k=key):
                mappings[k] = mappings.get(k, 0) + 1
                return True
            s._locked_mapping_update(mut)

    ta = threading.Thread(target=worker, args=("a",))
    tb = threading.Thread(target=worker, args=("b",))
    ta.start(); tb.start()
    ta.join(); tb.join()

    final = _read(mappings_file)
    assert final == {"a": N, "b": N}, f"lost update: {final}"


# --------------------------------------------------------------------------- #
# _ensure_mappings_file — check-then-write TOCTOU fix (atomic create-only)     #
# --------------------------------------------------------------------------- #

def test_ensure_mappings_file_seeds_when_absent(sync):
    """When the file is absent, _ensure_mappings_file seeds an empty map."""
    assert not sync.mappings_file.exists()
    sync._ensure_mappings_file()
    assert _read(sync.mappings_file) == {}


def test_empty_mappings_file_reads_as_empty(sync):
    """A zero-length file (the create-only seed's transient pre-write state, or a
    truncated write) has no links to preserve — _load_mappings returns {} rather
    than raising a corrupt-file RuntimeError + writing a spurious sidecar."""
    sync.mappings_file.write_text("", encoding="utf-8")
    assert sync._load_mappings() == {}
    assert not list(sync.mappings_file.parent.glob("*.corrupt-*"))


def test_whitespace_only_mappings_file_reads_as_empty(sync):
    sync.mappings_file.write_text("  \n\t ", encoding="utf-8")
    assert sync._load_mappings() == {}
    assert not list(sync.mappings_file.parent.glob("*.corrupt-*"))


def test_ensure_mappings_file_does_not_clobber_existing(sync, monkeypatch):
    """Simulate the TOCTOU: the file appears (a real mapping) AFTER the absence
    check but before the seed write. An atomic create-only seed must refuse to
    overwrite it — the mapping survives. (Pre-fix, the unconditional
    _write_json_durable({}) clobbered it.)"""
    real_mapping = {"m-live": {"issue_id": "42"}}
    sync.mappings_file.write_text(json.dumps(real_mapping), encoding="utf-8")

    # Force the absence check to (wrongly) see the file as missing, reproducing
    # the window where another process created it between check and write.
    monkeypatch.setattr(Path, "exists", lambda self: False)

    sync._ensure_mappings_file()

    assert _read(sync.mappings_file) == real_mapping, "seed clobbered a live mapping"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
