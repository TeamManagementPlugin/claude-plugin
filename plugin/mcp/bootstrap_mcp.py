#!/usr/bin/env python3
"""MCP cold-start bootstrap — stdlib only.

Runs under the *system* python (named in .mcp.json) BEFORE the plugin venv
exists. Builds/validates a venv under ``${CLAUDE_PLUGIN_DATA}/venv`` keyed to the
sha256 of ``requirements.lock``, then hands the process to the venv python
running ``server.py``. The handoff happens BEFORE any MCP handshake, so replacing
the process (``os.execv`` on POSIX) is safe — unlike a mid-session exec.

Pure stdlib: this file must import and run on a machine that has only the system
python and no third-party packages. Do NOT import shared_state or anything under
the venv here.
"""
import contextlib
import errno
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

# PLUGIN_ROOT: read-only install dir (replaced on update). The env var is injected
# by Claude Code for plugin processes; the __file__ fallback covers a dev run.
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
SERVER_PY = PLUGIN_ROOT / "mcp" / "server.py"
LOCKFILE = PLUGIN_ROOT / "requirements.lock"

# Generous outer bound (seconds) on each cold-start build subprocess so a wedged
# venv/pip call cannot hang the bootstrap forever. pip already carries its own
# 15s socket timeout + 5 retries; this is the belt-and-suspenders ceiling.
# stdlib-only: a plain literal, NOT engine_constants (which lives under the
# not-yet-built venv and is not importable here).
BUILD_STEP_TIMEOUT = 600


def plugin_data() -> Path:
    """Persistent per-plugin data dir (``${CLAUDE_PLUGIN_DATA}``). The venv lives
    here because PLUGIN_ROOT is replaced on plugin update (spike F3)."""
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not data:
        sys.stderr.write("[bootstrap_mcp] CLAUDE_PLUGIN_DATA is not set — cannot build a persistent venv.\n")
        sys.exit(1)
    return Path(data)


def venv_python(venv: Path) -> Path:
    """Path to the venv's python (Windows uses Scripts/, POSIX uses bin/)."""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def lock_hash(lockfile: Path) -> str:
    """sha256 hex of the lockfile bytes; "" when the file is absent."""
    return hashlib.sha256(lockfile.read_bytes()).hexdigest() if lockfile.exists() else ""


def needs_build(venv_py: Path, hash_file: Path, current_hash: str) -> bool:
    """Rebuild when the venv python is missing or the recorded lock hash differs.

    Known Windows limitation: on POSIX the venv python is a symlink to the system
    interpreter, so a system-Python upgrade/removal breaks it and forces a
    rebuild. On Windows ``python.exe`` is a COPY, so a system-Python upgrade does
    NOT invalidate the venv here — staleness is keyed only on the lockfile hash.
    This is benign (the copied interpreter keeps working); to force a rebuild
    after a Python upgrade, delete ``${CLAUDE_PLUGIN_DATA}/venv``.
    """
    if not venv_py.exists():
        return True
    try:
        return hash_file.read_text(encoding="utf-8").strip() != current_hash
    except OSError:
        return True


def _run_capture(args, label):
    """Run one build subprocess capturing output, under BUILD_STEP_TIMEOUT.

    Returns the CompletedProcess so callers keep their own return-code handling.
    On timeout, writes an actionable breadcrumb to stderr and exits — never leaks
    a bare TimeoutExpired traceback onto the pre-handshake MCP stdio channel.
    """
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=BUILD_STEP_TIMEOUT)
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"[bootstrap_mcp] {label} timed out after {BUILD_STEP_TIMEOUT}s "
            f"— check network/proxy.\n"
        )
        sys.exit(1)


def build_venv(venv: Path, venv_py: Path, hash_file: Path, current_hash: str) -> None:
    """Recreate the venv from scratch and install the pinned, hashed deps.

    The lock hash is written LAST so a crashed/interrupted install leaves no
    (or a stale) hash — the next run sees needs_build() True and rebuilds.
    """
    if sys.version_info < (3, 10):
        sys.stderr.write(f"[bootstrap_mcp] Python >=3.10 required to build the venv; "
                         f"got {sys.version.split()[0]}.\n")
        sys.exit(1)
    if venv.exists():
        # Surface deletion failures (locked files / concurrent process) instead of
        # masking them with ignore_errors — a half-deleted venv must not look valid.
        shutil.rmtree(venv)

    # Every subprocess call here CAPTURES output (capture_output=True): this
    # process runs on the MCP stdio channel, and any pip/venv bytes on stdout are
    # pre-handshake garbage that can break strict clients. On failure we surface
    # the captured output on STDERR and exit — never a bare CalledProcessError
    # traceback, and never stdout. (No check=True, so we control the error path.)
    r = _run_capture([sys.executable, "-m", "venv", str(venv)], "venv creation")
    if r.returncode != 0:
        sys.stderr.write("[bootstrap_mcp] venv creation failed:\n")
        sys.stderr.write((r.stdout or "") + (r.stderr or ""))
        sys.exit(1)
    # Some distros ship `venv` without ensurepip — verify pip before relying on it.
    r = _run_capture([str(venv_py), "-m", "pip", "--version"], "pip check")
    if r.returncode != 0:
        sys.stderr.write("[bootstrap_mcp] pip is missing in the new venv — install the "
                         "python venv/ensurepip package and retry.\n")
        sys.stderr.write((r.stdout or "") + (r.stderr or ""))
        sys.exit(1)
    r = _run_capture([str(venv_py), "-m", "pip", "install", "--require-hashes",
                      "-r", str(LOCKFILE)], "dependency install")
    if r.returncode != 0:
        sys.stderr.write("[bootstrap_mcp] dependency install failed "
                         "(pip install --require-hashes -r requirements.lock):\n")
        sys.stderr.write((r.stdout or "") + (r.stderr or ""))
        sys.exit(1)
    hash_file.write_text(current_hash, encoding="utf-8")


@contextlib.contextmanager
def build_lock(lock_path: Path):
    """Exclusive interprocess lock around the venv build.

    Two cold-starts of the same plugin (e.g. two sessions right after a plugin
    update, both seeing a hash mismatch) would otherwise rmtree + rebuild the same
    ``${CLAUDE_PLUGIN_DATA}/venv`` on top of each other. Serialising the build
    avoids that race; callers re-check needs_build() inside the lock so the loser
    skips the redundant rebuild. Degrades to a no-op lock on filesystems that do
    not support locking (the hash-written-last design still self-heals).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    # Non-blocking probe first so an UNcontended build never
                    # prints the "waiting" breadcrumb.
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EDEADLOCK):
                        raise  # not a contention signal — treat as unsupported
                    sys.stderr.write("[bootstrap_mcp] waiting for a concurrent venv build to finish…\n")
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # block
            else:
                import fcntl
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise  # not a contention signal — treat as unsupported
                    sys.stderr.write("[bootstrap_mcp] waiting for a concurrent venv build to finish…\n")
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # block until released
            locked = True
        except OSError:
            pass  # locking unsupported — proceed; next run self-heals on hash mismatch
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def main(execv=None) -> None:
    data = plugin_data()
    data.mkdir(parents=True, exist_ok=True)
    venv = data / "venv"
    venv_py = venv_python(venv)
    hash_file = data / "venv.lock.sha256"
    current_hash = lock_hash(LOCKFILE)
    if needs_build(venv_py, hash_file, current_hash):
        # A build is required but the pinned lockfile is missing — fail with an
        # actionable message BEFORE any pip install (a bare `pip install -r
        # <missing>` dies with an opaque CalledProcessError). Scoped to the build
        # path: an already-current venv (needs_build False) needs no lockfile.
        if not LOCKFILE.exists():
            sys.stderr.write(
                f"[bootstrap_mcp] requirements.lock is missing at {LOCKFILE} — the "
                f"team-management plugin install looks incomplete or corrupt. "
                f"Reinstall or repair the plugin, then restart.\n"
            )
            sys.exit(1)
        with build_lock(data / "venv.build.lock"):
            # Re-check inside the lock: a concurrent cold-start may have finished
            # the build while we waited (double-checked locking).
            if needs_build(venv_py, hash_file, current_hash):
                build_venv(venv, venv_py, hash_file, current_hash)
    target = [str(venv_py), str(SERVER_PY)]
    if os.name == "nt":
        # os.execv has stdio/exit-status quirks on Windows — proxy via subprocess
        # so the MCP stdio transport and exit code propagate correctly.
        sys.exit(subprocess.run(target).returncode)
    (execv or os.execv)(str(venv_py), target)


if __name__ == "__main__":
    main()
