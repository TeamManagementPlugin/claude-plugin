#!/usr/bin/env python3
"""Tests for the safety-guards destructive-Bash-command classifier
(h-gstack-safety-guards) in plugin/hooks/sessions-enforce.py.

Contract under test (task User Notes D1-D10):
  * Destructive pattern set blocks with exit 2 + a `[Safety Guard]` stderr
    message carrying a SAFETY_CONFIRM=<nonce> re-run line (SC-1, SC-2).
  * Whole-command allowlist passes disposable-dir rm -rf; chains/comments
    defeat it (SC-1).
  * Two-step typed confirmation: nonce is single-use, TTL-bound, and bound to
    the exact RAW command string (SC-2, D13 — normalization removed).
  * Sensitivity: off / enabled:false fully disable; warn-all adds every
    write-pattern command and disables the own-branch exception (SC-4, D2/D7).
  * Own-branch exception: reflog-recoverable git ops only, on the task's own
    branch with no remote-tracking ref; cd/-C foreign-repo guard (D4/D5).
  * Fail-closed: unparseable syntax still classified; internal errors block
    without leaking to the hook's exit-0 backstop (SC-3, D9).
  * Ordering: subagents are still bound; workflow bypass wins (D6).

Each test invokes sessions-enforce.py as a subprocess with a JSON stdin
payload (mimicking how Claude Code drives PreToolUse hooks).

Run with: python3 -m pytest test/test_safety_guards.py -v
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "plugin" / "hooks" / "sessions-enforce.py"

_NONCE_RE = re.compile(r"SAFETY_CONFIRM=([0-9a-f]{16}) ")


def _make_project(tmp_path, *, daic_mode="discussion", task=None, branch=None,
                  init_git=False, subagent_depth=None, safety_guards=None,
                  raw_config=None, workflow_bypass=False):
    """Build a minimal project tree for hook execution; return project root.

    safety_guards: value for config.json's "safety_guards" key (any JSON type —
      malformed shapes are part of the contract under test).
    raw_config: when set, written VERBATIM as config.json (overrides
      safety_guards; used for the corrupt-config case).
    """
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (tmp_path / "team-management" / "tasks").mkdir(parents=True)

    (state / "daic-mode.json").write_text(
        json.dumps({"mode": daic_mode}), encoding="utf-8")
    (state / "current_task.json").write_text(json.dumps(
        {"task": task, "branch": branch, "services": [],
         "updated": "2026-08-01"}), encoding="utf-8")

    if subagent_depth is not None:
        (state / "subagent-depth.json").write_text(
            json.dumps({"depth": subagent_depth}), encoding="utf-8")

    if workflow_bypass:
        (state / "workflow-bypass.json").write_text(
            json.dumps({"enabled": True}), encoding="utf-8")

    if raw_config is not None:
        (tmp_path / "team-management" / "config.json").write_text(
            raw_config, encoding="utf-8")
    elif safety_guards is not None:
        (tmp_path / "team-management" / "config.json").write_text(
            json.dumps({"safety_guards": safety_guards}), encoding="utf-8")

    if task:
        body = (f"---\ntask: {task}\nbranch: {branch or ''}\n"
                f"status: in-progress\n---\n\n# Test task\n")
        (tmp_path / "team-management" / "tasks" / f"{task}.md").write_text(
            body, encoding="utf-8")

    if init_git:
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "--allow-empty", "-m", "init", "-q"],
                       cwd=str(tmp_path), check=True)
        if branch:
            subprocess.run(["git", "checkout", "-q", "-b", branch],
                           cwd=str(tmp_path), check=True)

    return tmp_path


def _run_hook(project_root, command):
    """Invoke sessions-enforce.py with a Bash payload; return (rc, out, err).

    CLAUDE_* env vars are scrubbed so get_project_root() resolves the tmp
    project via the cwd walk regardless of the invoking environment."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT",
                        "CLAUDE_PLUGIN_DATA")}
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}})
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload, capture_output=True, text=True,
        cwd=str(project_root), timeout=15, env=env,
    )
    return result.returncode, result.stdout, result.stderr


def _assert_guard_blocked(rc, stderr, command=""):
    assert rc == 2, f"expected safety-guard block (rc 2) for {command!r}, " \
                    f"got rc={rc}; err={stderr!r}"
    assert "[Safety Guard]" in stderr, \
        f"blocked but not by the safety guard: {stderr!r}"


def _assert_allowed(rc, stdout, stderr, command=""):
    assert rc == 0, f"expected allow (rc 0) for {command!r}, got rc={rc}; " \
                    f"out={stdout!r} err={stderr!r}"
    assert "[Safety Guard]" not in stderr


def _extract_nonce(stderr):
    m = _NONCE_RE.search(stderr)
    assert m, f"no SAFETY_CONFIRM nonce in block message: {stderr!r}"
    return m.group(1)


# ---------------------------------------------------------------------------
# Destructive pattern set (SC-1)
# ---------------------------------------------------------------------------

DESTRUCTIVE_COMMANDS = [
    "rm -rf /tmp/whatever",
    "rm -fr src",
    "rm -r -f src",
    "rm --recursive --force src",
    "sudo rm -rf /var/data",
    'psql -c "DROP TABLE users"',
    "mysql -e 'TRUNCATE logs'",
    'psql -c "DROP DATABASE prod"',
    "git push --force",
    "git push -f origin feature/x",
    "git push --force-with-lease",
    "git reset --hard HEAD~3",
    "git checkout .",
    "git checkout -- .",
    "git restore .",
    "git restore --staged --worktree .",
    "git clean -fd",
    "git clean --force",
    "kubectl delete pod mypod",
    "kubectl -n prod delete deployment api",
    "docker rm -f mycontainer",
    "docker system prune",
    # round-1 fixes: wrapper value-flags and docker global value-options
    "sudo -u root rm -rf /tmp/data",
    "xargs -I {} rm -rf",
    "timeout 5 rm -rf /var/data",
    "timeout 5s rm -rf /var/data",
    "gtimeout -k 10 30 rm -rf /var/data",
    "env rm -rf /var/data",
    "setsid rm -rf /var/data",
    "nice -n 10 rm -rf /var/data",
    "stdbuf -o L rm -rf /var/data",
    "docker --context production rm -f app",
    "docker --context production system prune",
    "docker -H tcp://remote:2375 rm -f app",
    # round-2 fixes: docker container alias + --force=<bool> form
    "docker container rm -f app",
    "docker container rm --force app",
    "docker rm --force=true app",
    # round-3 fix: sudo long-form value flags (separate-value spellings)
    "sudo --user root rm -rf /tmp/data",
]

SAFE_COMMANDS = [
    "rm file.txt",
    "rm -r somedir",
    "rm -f file.txt",
    "git push",
    "git push origin feature/x",
    "git reset --soft HEAD~1",
    "git checkout main",
    "git checkout -b feature/y",
    "git restore staged.txt",
    "git clean -n",
    "kubectl get pods",
    "docker rm mycontainer",
    "docker container rm mycontainer",
    "docker container ls",
    "docker system df",
    "docker --context production ps",
    "sudo -u root ls -la",
    "timeout 5 git status",
    "git clean -fdn",
    "git clean -nf",
    "ls -la",
    "mkdir newdir",
]


class TestDestructivePatterns:
    @pytest.mark.parametrize("command", DESTRUCTIVE_COMMANDS)
    def test_destructive_blocked(self, tmp_path, command):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, command)
        _assert_guard_blocked(rc, err, command)
        _extract_nonce(err)

    @pytest.mark.parametrize("command", SAFE_COMMANDS)
    def test_safe_allowed(self, tmp_path, command):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, command)
        _assert_allowed(rc, out, err, command)

    def test_chain_second_segment_blocked(self, tmp_path):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "echo ok && git reset --hard")
        _assert_guard_blocked(rc, err)

    def test_newline_chain_blocked(self, tmp_path):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "echo ok\ngit reset --hard")
        _assert_guard_blocked(rc, err)

    def test_pipe_segment_blocked(self, tmp_path):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "cat list.txt | xargs rm -rf")
        _assert_guard_blocked(rc, err)

    def test_quoted_separator_severing_is_a_documented_limitation(self, tmp_path):
        """Review round-1 W2, accepted as a documented bypass (frozen-path
        precedent): a chain separator inside a quoted ARGUMENT severs the
        segment, so trailing-positional rules (checkout/restore `.`) miss.
        This test PINS the current behavior — if a future quote-aware
        splitter closes the gap, flip this assertion and remove the bypass
        from the documented list (USAGE_GUIDE.md + task User Notes)."""
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, 'git checkout "a;b" .')
        _assert_allowed(rc, out, err)

    def test_coreutils_truncate_blocks_as_documented_fp(self, tmp_path):
        """Round-2 note: coreutils `truncate` trips the parse-independent SQL
        TRUNCATE regex. Documented safe-side FP (arguably destructive anyway)
        — this test pins it as EXPECTED behavior, not a bug."""
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "truncate -s 0 big.log")
        _assert_guard_blocked(rc, err)


# ---------------------------------------------------------------------------
# Whole-command allowlist (SC-1)
# ---------------------------------------------------------------------------

class TestAllowlist:
    @pytest.mark.parametrize("command", [
        "rm -rf node_modules",
        "rm -rf dist",
        "rm -rf build __pycache__",
        "rm -rf ./node_modules",
        "rm -rf .cache coverage",
        "rm -fr node_modules",
        "rm  -rf   node_modules",
    ])
    def test_builtin_allowlist_passes(self, tmp_path, command):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, command)
        _assert_allowed(rc, out, err, command)

    @pytest.mark.parametrize("command", [
        "rm -rf / # rm -rf node_modules",
        "rm -rf node_modules && rm -rf /",
        "rm -rf src",
        "rm -rf node_modules ../escape",
        "rm -rf /node_modules",
    ])
    def test_allowlist_tricks_blocked(self, tmp_path, command):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, command)
        _assert_guard_blocked(rc, err, command)

    def test_custom_allowlist_entry_passes(self, tmp_path):
        project = _make_project(
            tmp_path, safety_guards={"allowlist": ["rm -rf .tox"]})
        rc, out, err = _run_hook(project, "rm -rf .tox")
        _assert_allowed(rc, out, err)

    def test_custom_allowlist_is_exact_whole_command(self, tmp_path):
        project = _make_project(
            tmp_path, safety_guards={"allowlist": ["rm -rf .tox"]})
        rc, out, err = _run_hook(project, "rm -rf .tox extra")
        _assert_guard_blocked(rc, err)


# ---------------------------------------------------------------------------
# Two-step typed confirmation (SC-2)
# ---------------------------------------------------------------------------

class TestConfirmationFlow:
    def test_full_cycle_confirm_then_consume(self, tmp_path):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "git reset --hard")
        _assert_guard_blocked(rc, err)
        nonce = _extract_nonce(err)

        rc2, out2, err2 = _run_hook(
            project, f"SAFETY_CONFIRM={nonce} git reset --hard")
        _assert_allowed(rc2, out2, err2)

        # Single-use: the same confirmed command blocks again with a NEW nonce.
        rc3, out3, err3 = _run_hook(
            project, f"SAFETY_CONFIRM={nonce} git reset --hard")
        _assert_guard_blocked(rc3, err3)
        assert _extract_nonce(err3) != nonce

    def test_confirmation_binds_raw_command_exactly(self, tmp_path):
        """codex R3: whitespace normalization is not semantics-preserving —
        the binding is an exact raw-string match on the displayed command."""
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "git   reset    --hard")
        nonce = _extract_nonce(err)
        assert f"SAFETY_CONFIRM={nonce} git   reset    --hard" in err

        # A collapsed-whitespace variant is a DIFFERENT string — rejected.
        rc2, out2, err2 = _run_hook(
            project, f"SAFETY_CONFIRM={nonce} git reset --hard")
        _assert_guard_blocked(rc2, err2)

        # The fresh nonce from the rejection binds the collapsed spelling;
        # a verbatim copy of the new display line passes.
        nonce3 = _extract_nonce(err2)
        rc3, out3, err3 = _run_hook(
            project, f"SAFETY_CONFIRM={nonce3} git reset --hard")
        _assert_allowed(rc3, out3, err3)

    def test_confirmation_preserves_quoted_whitespace(self, tmp_path):
        """`rm -rf "two  spaces"` must replay against the SAME path — the
        double space inside quotes survives the round-trip verbatim."""
        project = _make_project(tmp_path)
        command = 'rm -rf "two  spaces"'
        rc, out, err = _run_hook(project, command)
        nonce = _extract_nonce(err)
        assert f'SAFETY_CONFIRM={nonce} {command}' in err
        rc2, out2, err2 = _run_hook(
            project, f"SAFETY_CONFIRM={nonce} {command}")
        _assert_allowed(rc2, out2, err2)

    def test_confirmation_preserves_newline_chain(self, tmp_path):
        """A newline-separated chain replays as the same shell program."""
        project = _make_project(tmp_path)
        command = "echo prep\ngit reset --hard"
        rc, out, err = _run_hook(project, command)
        nonce = _extract_nonce(err)
        rc2, out2, err2 = _run_hook(
            project, f"SAFETY_CONFIRM={nonce} {command}")
        _assert_allowed(rc2, out2, err2)

    def test_wrong_nonce_blocked(self, tmp_path):
        project = _make_project(tmp_path)
        _run_hook(project, "git reset --hard")
        rc, out, err = _run_hook(
            project, "SAFETY_CONFIRM=" + "0" * 16 + " git reset --hard")
        _assert_guard_blocked(rc, err)
        assert "NEW nonce" in err

    def test_command_mismatch_blocked(self, tmp_path):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "git reset --hard")
        nonce = _extract_nonce(err)
        rc2, out2, err2 = _run_hook(
            project, f"SAFETY_CONFIRM={nonce} git clean -fd")
        _assert_guard_blocked(rc2, err2)
        assert "NEW nonce" in err2

    def test_expired_nonce_blocked(self, tmp_path):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "git reset --hard")
        nonce = _extract_nonce(err)
        state_file = project / ".claude" / "state" / "safety-guard-confirm.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["expires_at"] = time.time() - 1
        state_file.write_text(json.dumps(state), encoding="utf-8")
        rc2, out2, err2 = _run_hook(
            project, f"SAFETY_CONFIRM={nonce} git reset --hard")
        _assert_guard_blocked(rc2, err2)

    def test_confirm_prefix_on_safe_command_allowed(self, tmp_path):
        """A stray prefix on a non-destructive command must not block."""
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(
            project, "SAFETY_CONFIRM=" + "a" * 16 + " ls -la")
        _assert_allowed(rc, out, err)

    def test_block_message_names_friction(self, tmp_path):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "git reset --hard")
        assert "Friction guard, not a security control" in err
        assert "ASK THE USER" in err


# ---------------------------------------------------------------------------
# Sensitivity levels (SC-4: off / warn-destructive-only / warn-all)
# ---------------------------------------------------------------------------

class TestSensitivityLevels:
    def test_enabled_false_disables(self, tmp_path):
        project = _make_project(tmp_path, safety_guards={"enabled": False})
        rc, out, err = _run_hook(project, "rm -rf /")
        _assert_allowed(rc, out, err)

    def test_sensitivity_off_disables(self, tmp_path):
        project = _make_project(tmp_path, safety_guards={"sensitivity": "off"})
        rc, out, err = _run_hook(project, "rm -rf /")
        _assert_allowed(rc, out, err)

    def test_warn_all_blocks_write_pattern_command(self, tmp_path):
        project = _make_project(
            tmp_path, safety_guards={"sensitivity": "warn-all"})
        rc, out, err = _run_hook(project, "mkdir newdir")
        _assert_guard_blocked(rc, err)
        assert "warn-all" in err

    def test_warn_all_passes_readonly_command(self, tmp_path):
        project = _make_project(
            tmp_path, safety_guards={"sensitivity": "warn-all"})
        rc, out, err = _run_hook(project, "ls -la")
        _assert_allowed(rc, out, err)

    def test_warn_all_passes_allowlist(self, tmp_path):
        project = _make_project(
            tmp_path, safety_guards={"sensitivity": "warn-all"})
        rc, out, err = _run_hook(project, "rm -rf node_modules")
        _assert_allowed(rc, out, err)

    @pytest.mark.parametrize("command", [
        "echo data > out.txt",
        "tee capture.log",
        "cp a.txt b.txt",
    ])
    def test_warn_all_write_pattern_contract(self, tmp_path, command):
        """Contract pin on the BASH_WRITE_PATTERNS coupling (codex R3): these
        representative write shapes must stay gated under warn-all — a future
        edit to the shared pattern list that drops them fails here loudly."""
        project = _make_project(
            tmp_path, safety_guards={"sensitivity": "warn-all"})
        rc, out, err = _run_hook(project, command)
        _assert_guard_blocked(rc, err, command)

    def test_warn_all_readonly_pipeline_allowed(self, tmp_path):
        project = _make_project(
            tmp_path, safety_guards={"sensitivity": "warn-all"})
        rc, out, err = _run_hook(project, "cat a.txt | grep -c pattern")
        _assert_allowed(rc, out, err)

    def test_default_config_is_enabled(self, tmp_path):
        """No config.json at all → guard active (D1 default-on)."""
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, "rm -rf /")
        _assert_guard_blocked(rc, err)


# ---------------------------------------------------------------------------
# Own-branch exception (SC-4, D4/D5)
# ---------------------------------------------------------------------------

def _own_branch_project(tmp_path, **kwargs):
    return _make_project(tmp_path, task="h-x", branch="feature/x",
                         init_git=True, **kwargs)


class TestOwnBranchException:
    def test_reset_hard_on_own_unshared_branch_allowed(self, tmp_path):
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(project, "git reset --hard HEAD~1")
        _assert_allowed(rc, out, err)

    def test_push_force_on_own_unshared_branch_allowed(self, tmp_path):
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(project, "git push --force")
        _assert_allowed(rc, out, err)

    def test_push_force_own_branch_refspec_allowed(self, tmp_path):
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(
            project, "git push --force origin feature/x")
        _assert_allowed(rc, out, err)

    def test_push_force_foreign_refspec_blocked(self, tmp_path):
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(project, "git push --force origin main")
        _assert_guard_blocked(rc, err)

    def test_push_delete_refspec_blocked(self, tmp_path):
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(
            project, "git push --force origin :feature/x")
        _assert_guard_blocked(rc, err)

    @pytest.mark.parametrize("command", [
        "git push --force --all",
        "git push --force --branches",
        "git push --force --mirror",
        "git push --force --tags",
        "git push --force --prune origin",
        "git push --force --delete origin feature/x",
        "git push -f -d origin feature/x",
    ])
    def test_push_broad_modes_blocked(self, tmp_path, command):
        """codex R3: broad push modes are unvettable — the exception must not
        qualify vacuously on an empty positional-refspec list."""
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(project, command)
        _assert_guard_blocked(rc, err, command)

    def test_bare_push_force_blocked_under_push_default_matching(self, tmp_path):
        """A refspec-less `git push --force` under push.default=matching can
        force-push OTHER branches — the exception is withheld (D12)."""
        project = _own_branch_project(tmp_path)
        subprocess.run(["git", "config", "push.default", "matching"],
                       cwd=str(project), check=True)
        rc, out, err = _run_hook(project, "git push --force")
        _assert_guard_blocked(rc, err)

    def test_bare_push_force_allowed_under_push_default_simple(self, tmp_path):
        project = _own_branch_project(tmp_path)
        subprocess.run(["git", "config", "push.default", "simple"],
                       cwd=str(project), check=True)
        rc, out, err = _run_hook(project, "git push --force")
        _assert_allowed(rc, out, err)

    def test_combined_fd_spelling_denied_like_separate(self, tmp_path):
        """Round-2 W1: `-fd` (combined force+delete) must be denied exactly
        like the `-f -d` spelling — a remote-branch delete is unvettable."""
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(project, "git push -fd origin feature/x")
        _assert_guard_blocked(rc, err)

    @pytest.mark.parametrize("command", [
        "env -C /other/repo git reset --hard",
        "env --chdir=/other/repo git reset --hard",
        "sudo -D /other/repo git reset --hard",
        "sudo --chdir /other/repo git reset --hard",
    ])
    def test_wrapper_chdir_flags_disable_exception(self, tmp_path, command):
        """Round-2 W2: chdir-class wrapper flags retarget the wrapped command
        at another directory — the own-branch exception must not vouch."""
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(project, command)
        _assert_guard_blocked(rc, err, command)

    def test_remote_tracking_ref_blocks(self, tmp_path):
        project = _own_branch_project(tmp_path)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/feature/x", "HEAD"],
            cwd=str(project), check=True)
        rc, out, err = _run_hook(project, "git reset --hard")
        _assert_guard_blocked(rc, err)

    def test_wrong_current_branch_blocks(self, tmp_path):
        project = _own_branch_project(tmp_path)
        subprocess.run(["git", "checkout", "-q", "-b", "other"],
                       cwd=str(project), check=True)
        rc, out, err = _run_hook(project, "git reset --hard")
        _assert_guard_blocked(rc, err)

    def test_no_task_branch_blocks(self, tmp_path):
        project = _make_project(tmp_path, init_git=True)
        rc, out, err = _run_hook(project, "git reset --hard")
        _assert_guard_blocked(rc, err)

    def test_working_tree_destruction_never_exempt(self, tmp_path):
        project = _own_branch_project(tmp_path)
        for command in ("git checkout .", "git restore .", "git clean -fd"):
            rc, out, err = _run_hook(project, command)
            _assert_guard_blocked(rc, err, command)

    def test_cd_segment_disables_exception(self, tmp_path):
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(project, "cd /tmp && git reset --hard")
        _assert_guard_blocked(rc, err)

    def test_git_c_flag_disables_exception(self, tmp_path):
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(project, "git -C /other/repo reset --hard")
        _assert_guard_blocked(rc, err)

    def test_exception_disabled_under_warn_all(self, tmp_path):
        project = _own_branch_project(
            tmp_path, safety_guards={"sensitivity": "warn-all"})
        rc, out, err = _run_hook(project, "git reset --hard")
        _assert_guard_blocked(rc, err)

    def test_mixed_reversible_and_irreversible_blocks(self, tmp_path):
        project = _own_branch_project(tmp_path)
        rc, out, err = _run_hook(
            project, "git reset --hard && rm -rf src")
        _assert_guard_blocked(rc, err)


# ---------------------------------------------------------------------------
# Fail-closed behaviour (SC-3, D7, D9)
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_unbalanced_quote_still_classified(self, tmp_path):
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(project, 'git reset --hard "x')
        _assert_guard_blocked(rc, err)

    def test_malformed_safety_guards_defaults_on(self, tmp_path):
        project = _make_project(tmp_path, safety_guards=[])
        rc, out, err = _run_hook(project, "rm -rf /")
        _assert_guard_blocked(rc, err)

    def test_unknown_sensitivity_defaults_on(self, tmp_path):
        project = _make_project(
            tmp_path, safety_guards={"sensitivity": "bogus"})
        rc, out, err = _run_hook(project, "rm -rf /")
        _assert_guard_blocked(rc, err)

    def test_corrupt_config_defaults_on(self, tmp_path):
        project = _make_project(tmp_path, raw_config="{not json")
        rc, out, err = _run_hook(project, "rm -rf /")
        _assert_guard_blocked(rc, err)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                        reason="root ignores directory permissions")
    def test_state_write_failure_blocks_fail_closed(self, tmp_path):
        """D9: an internal error (unwritable state dir → _write_json_durable
        raises) blocks WITHOUT a nonce and never reaches the exit-0 backstop."""
        project = _make_project(tmp_path)
        state_dir = project / ".claude" / "state"
        state_dir.chmod(0o555)
        try:
            rc, out, err = _run_hook(project, "git reset --hard")
        finally:
            state_dir.chmod(0o755)
        _assert_guard_blocked(rc, err)
        assert "fail-closed" in err
        assert "SAFETY_CONFIRM=" not in err


# ---------------------------------------------------------------------------
# Ordering: subagents bound, workflow bypass wins (D6)
# ---------------------------------------------------------------------------

class TestOrdering:
    def test_subagent_still_blocked(self, tmp_path):
        project = _make_project(tmp_path, subagent_depth=1)
        rc, out, err = _run_hook(project, "rm -rf /")
        _assert_guard_blocked(rc, err)

    def test_workflow_bypass_disables_guard(self, tmp_path):
        project = _make_project(tmp_path, workflow_bypass=True)
        rc, out, err = _run_hook(project, "rm -rf /")
        _assert_allowed(rc, out, err)

    def test_protected_path_check_precedes_guard(self, tmp_path):
        """A Bash command touching .claude/state is blocked by the protected-
        path guard, not the safety guard — ordering regression check."""
        project = _make_project(tmp_path)
        rc, out, err = _run_hook(
            project, "rm -rf .claude/state")
        assert rc == 2
        assert "[Safety Guard]" not in err
        assert "protected path" in err
