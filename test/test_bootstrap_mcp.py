#!/usr/bin/env python3
"""bootstrap_mcp.py tests (h-hook-port-boot-detector, commit 2).

Covers the cold-start MCP bootstrap: lockfile hashing, venv-python path,
rebuild decision, and the main() orchestration (build-then-execv on first run,
fast-path on the second) with subprocess + execv stubbed so no real venv/pip/
network is needed. The real dependency install is exercised at #6.

Run with: python3 -m pytest test/test_bootstrap_mcp.py -v
"""

import errno
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "plugin" / "mcp"
sys.path.insert(0, str(MCP_DIR))

import bootstrap_mcp  # noqa: E402


def test_lock_hash_of_known_bytes(tmp_path):
    import hashlib
    lock = tmp_path / "requirements.lock"
    lock.write_bytes(b"abc")
    assert bootstrap_mcp.lock_hash(lock) == hashlib.sha256(b"abc").hexdigest()


def test_lock_hash_missing_file(tmp_path):
    assert bootstrap_mcp.lock_hash(tmp_path / "nope.lock") == ""


def test_venv_python_posix_shape(tmp_path):
    vp = bootstrap_mcp.venv_python(tmp_path / "venv")
    # On this (POSIX) test host the venv python is under bin/.
    assert vp.name in ("python", "python.exe")
    assert vp.parent.name in ("bin", "Scripts")


def test_needs_build_missing_venv(tmp_path):
    assert bootstrap_mcp.needs_build(tmp_path / "venv/bin/python",
                                     tmp_path / "h.sha256", "deadbeef") is True


def test_needs_build_hash_mismatch(tmp_path):
    vp = tmp_path / "python"; vp.write_text("")
    hf = tmp_path / "h.sha256"; hf.write_text("OLD")
    assert bootstrap_mcp.needs_build(vp, hf, "NEW") is True


def test_needs_build_up_to_date(tmp_path):
    vp = tmp_path / "python"; vp.write_text("")
    hf = tmp_path / "h.sha256"; hf.write_text("SAME\n")
    assert bootstrap_mcp.needs_build(vp, hf, "SAME") is False


def test_build_lock_acquire_and_release(tmp_path):
    """build_lock is a working context manager; a clean release lets the next
    acquisition proceed without blocking (codex code-review: concurrent-build guard)."""
    lock = tmp_path / "venv.build.lock"
    with bootstrap_mcp.build_lock(lock):
        assert lock.exists()
    with bootstrap_mcp.build_lock(lock):  # would hang if the release leaked
        assert lock.exists()


def test_cold_start_then_fast_path(tmp_path, monkeypatch):
    """First main(): build venv + execv into venv-python server.py.
    Second main(): hash matches + venv exists → no rebuild, still execv."""
    data = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    lock = tmp_path / "requirements.lock"; lock.write_text("pkg==1.0 --hash=sha256:x\n")
    server = tmp_path / "server.py"; server.write_text("")
    monkeypatch.setattr(bootstrap_mcp, "LOCKFILE", lock)
    monkeypatch.setattr(bootstrap_mcp, "SERVER_PY", server)

    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))
        # Simulate `python -m venv <venv>` by materialising the venv python.
        if len(args) >= 4 and args[1:3] == ["-m", "venv"]:
            vp = bootstrap_mcp.venv_python(Path(args[3]))
            vp.parent.mkdir(parents=True, exist_ok=True)
            vp.write_text("")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(bootstrap_mcp.subprocess, "run", fake_run)

    captured = {}
    def fake_execv(path, argv):
        captured["path"] = path
        captured["argv"] = argv

    venv_py = bootstrap_mcp.venv_python(data / "venv")

    # --- first run: cold start ---
    bootstrap_mcp.main(execv=fake_execv)
    joined = [" ".join(c) for c in calls]
    assert any(c[1:3] == ["-m", "venv"] for c in calls), "venv was not created"
    assert any("install" in c and "--require-hashes" in c for c in calls), \
        f"pip install --require-hashes not invoked: {joined}"
    assert (data / "venv.lock.sha256").read_text() == bootstrap_mcp.lock_hash(lock)
    assert captured["argv"] == [str(venv_py), str(server)]

    # --- second run: fast-path (venv exists + hash matches) ---
    calls.clear(); captured.clear()
    bootstrap_mcp.main(execv=fake_execv)
    assert calls == [], f"unexpected rebuild on fast-path: {calls}"
    assert captured["argv"] == [str(venv_py), str(server)]


# ------------------------------------- stdout hygiene + missing-lock diagnostics
# (m-statusline-and-test-infra, SC#5). bootstrap runs on the MCP stdio channel:
# pip/venv output must NEVER reach stdout (pre-handshake garbage breaks strict
# clients), and a missing requirements.lock when a build is required must fail
# with an actionable message BEFORE any pip install (not a bare traceback).


def _cold_start_env(tmp_path, monkeypatch, lockfile_present=True):
    data = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    lock = tmp_path / "requirements.lock"
    if lockfile_present:
        lock.write_text("pkg==1.0 --hash=sha256:x\n")
    server = tmp_path / "server.py"; server.write_text("")
    monkeypatch.setattr(bootstrap_mcp, "LOCKFILE", lock)
    monkeypatch.setattr(bootstrap_mcp, "SERVER_PY", server)
    return data, lock, server


def test_build_subprocess_calls_capture_output(tmp_path, monkeypatch):
    """Every non-execv subprocess call in the build path must capture output so
    nothing leaks onto the MCP stdout channel."""
    data, lock, server = _cold_start_env(tmp_path, monkeypatch)
    seen = []

    def fake_run(args, **kw):
        seen.append((list(args), kw))
        if len(args) >= 4 and args[1:3] == ["-m", "venv"]:
            vp = bootstrap_mcp.venv_python(Path(args[3]))
            vp.parent.mkdir(parents=True, exist_ok=True)
            vp.write_text("")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bootstrap_mcp.subprocess, "run", fake_run)
    bootstrap_mcp.main(execv=lambda *a: None)

    assert seen, "no subprocess calls were made"
    for args, kw in seen:
        assert kw.get("capture_output") is True, f"call did not capture output: {args} {kw}"
        # _run_capture must preserve text=True and add the generous outer bound.
        assert kw.get("text") is True, f"call did not pass text=True: {args} {kw}"
        assert kw.get("timeout") == bootstrap_mcp.BUILD_STEP_TIMEOUT, \
            f"call did not carry BUILD_STEP_TIMEOUT: {args} {kw}"


def test_missing_lockfile_when_build_required_errors_before_install(tmp_path, monkeypatch, capsys):
    """No venv yet + missing requirements.lock -> exit(1) with an actionable
    message, and pip install is NEVER reached."""
    data, lock, server = _cold_start_env(tmp_path, monkeypatch, lockfile_present=False)
    install_calls = []

    def fake_run(args, **kw):
        if "install" in args:
            install_calls.append(list(args))
        if len(args) >= 4 and args[1:3] == ["-m", "venv"]:
            vp = bootstrap_mcp.venv_python(Path(args[3]))
            vp.parent.mkdir(parents=True, exist_ok=True)
            vp.write_text("")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bootstrap_mcp.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        bootstrap_mcp.main(execv=lambda *a: None)

    assert exc.value.code == 1
    assert install_calls == [], "pip install must not run when the lockfile is missing"
    err = capsys.readouterr().err
    assert "requirements.lock" in err, err


def test_missing_lockfile_fast_path_no_error(tmp_path, monkeypatch):
    """(codex) An already-current venv needs no lockfile: a missing
    requirements.lock on the fast path must NOT error — it just execs the server."""
    data, lock, server = _cold_start_env(tmp_path, monkeypatch, lockfile_present=False)
    # Pre-materialise a venv python + a hash file matching lock_hash(missing) == "".
    venv_py = bootstrap_mcp.venv_python(data / "venv")
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    (data / "venv.lock.sha256").write_text("")  # == lock_hash(missing lockfile)

    def fake_run(args, **kw):
        raise AssertionError(f"no subprocess should run on the fast path: {args}")

    monkeypatch.setattr(bootstrap_mcp.subprocess, "run", fake_run)
    captured = {}
    bootstrap_mcp.main(execv=lambda path, argv: captured.update(path=path, argv=argv))
    assert captured["argv"] == [str(venv_py), str(server)]


# ------------------------------------- l-fix-subprocess-timeout-hardening
# Every build subprocess carries BUILD_STEP_TIMEOUT; a hung step exits(1) with an
# actionable stderr breadcrumb instead of hanging the cold-start forever. The
# build_lock probe prints a breadcrumb ONLY on real contention, never on an
# uncontended acquire or a locking-unsupported filesystem.


def _args_contain(args, sub):
    s = [str(a) for a in args]
    sub = [str(x) for x in sub]
    return any(s[i:i + len(sub)] == sub for i in range(len(s) - len(sub) + 1))


@pytest.mark.parametrize("fail_sub, label", [
    (["-m", "venv"], "venv creation"),
    (["-m", "pip", "--version"], "pip check"),
    (["-m", "pip", "install"], "dependency install"),
])
def test_build_step_timeout_exits_with_breadcrumb(tmp_path, monkeypatch, capsys, fail_sub, label):
    """A hung build subprocess must exit(1) with an actionable stderr breadcrumb,
    covered for each of the three build steps."""
    data, lock, server = _cold_start_env(tmp_path, monkeypatch)

    def fake_run(args, **kw):
        if _args_contain(args, fail_sub):
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))
        # Materialize the venv python so the pip steps are reachable.
        if len(args) >= 4 and args[1:3] == ["-m", "venv"]:
            vp = bootstrap_mcp.venv_python(Path(args[3]))
            vp.parent.mkdir(parents=True, exist_ok=True)
            vp.write_text("")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bootstrap_mcp.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        bootstrap_mcp.main(execv=lambda *a: None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "timed out" in err
    assert label in err
    assert "check network/proxy" in err


def test_build_returncode_nonzero_still_exits(tmp_path, monkeypatch, capsys):
    """_run_capture must preserve the existing nonzero-returncode handling:
    surface the captured stderr and exit(1) (routing through the helper changed
    nothing about the failure path)."""
    data, lock, server = _cold_start_env(tmp_path, monkeypatch)

    def fake_run(args, **kw):
        if len(args) >= 4 and args[1:3] == ["-m", "venv"]:
            return types.SimpleNamespace(returncode=1, stdout="out-x", stderr="err-y")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bootstrap_mcp.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as exc:
        bootstrap_mcp.main(execv=lambda *a: None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "venv creation failed" in err
    assert "err-y" in err  # captured stderr surfaced unchanged


@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl lock path")
class TestBuildLockProbe:
    """errno-guarded non-blocking probe: contention → breadcrumb + block;
    uncontended → single acquire, no breadcrumb; unsupported → degrade quietly."""

    def test_uncontended_probe_no_breadcrumb(self, tmp_path, monkeypatch, capsys):
        import fcntl
        ops = []

        def fake_flock(fd, op):
            ops.append(op)  # LOCK_NB probe succeeds (no raise)

        monkeypatch.setattr(fcntl, "flock", fake_flock)
        with bootstrap_mcp.build_lock(tmp_path / "venv.build.lock"):
            pass
        err = capsys.readouterr().err
        assert "waiting" not in err
        assert (fcntl.LOCK_EX | fcntl.LOCK_NB) in ops  # the probe ran
        assert fcntl.LOCK_EX not in ops                # NO bare blocking acquire

    def test_contended_probe_breadcrumb_then_blocks(self, tmp_path, monkeypatch, capsys):
        import fcntl
        ops = []

        def fake_flock(fd, op):
            ops.append(op)
            if op == (fcntl.LOCK_EX | fcntl.LOCK_NB):
                raise OSError(errno.EAGAIN, "would block")

        monkeypatch.setattr(fcntl, "flock", fake_flock)
        with bootstrap_mcp.build_lock(tmp_path / "venv.build.lock"):
            pass
        err = capsys.readouterr().err
        assert "waiting for a concurrent venv build" in err
        assert fcntl.LOCK_EX in ops  # escalated to a blocking acquire

    def test_unsupported_lock_degrades_no_breadcrumb(self, tmp_path, monkeypatch, capsys):
        import fcntl
        ops = []

        def fake_flock(fd, op):
            ops.append(op)
            if op == (fcntl.LOCK_EX | fcntl.LOCK_NB):
                raise OSError(errno.ENOSYS, "not supported")

        monkeypatch.setattr(fcntl, "flock", fake_flock)
        with bootstrap_mcp.build_lock(tmp_path / "venv.build.lock"):
            pass
        err = capsys.readouterr().err
        assert "waiting" not in err
        assert fcntl.LOCK_EX not in ops                 # never escalated to blocking
        assert ops == [fcntl.LOCK_EX | fcntl.LOCK_NB]   # only the probe; no unlock
