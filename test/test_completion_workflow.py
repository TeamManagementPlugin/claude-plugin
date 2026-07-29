#!/usr/bin/env python3
"""Tests for the completion-workflow funcs added in
m-completion-workflow (`_func_verify_tests_pass`,
`_func_present_completion_options`, `_func_require_discard_confirmation`,
`_func_completion_dispatch`).

Run with:
  python3 -m pytest test/test_completion_workflow.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

TEST_DIR = Path(__file__).parent
REPO_ROOT = TEST_DIR.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import shared_state  # noqa: E402
from protocol_engine import ProtocolEngine  # noqa: E402


class _TempEngineBase(TestCase):
    """Base class: spin up a ProtocolEngine against a temp project root with
    a real (tiny) git repo and a writable .claude/state layout.
    """

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state" / "protocol-logs").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "tasks").mkdir(parents=True)
        (self.temp_dir / "team-management" / "tasks").mkdir(parents=True)

        self._write_json(
            self.temp_dir / ".claude" / "state" / "current_task.json",
            {"task": None, "branch": None, "services": [], "updated": "2026-04-20"},
        )
        self._write_json(
            self.temp_dir / ".claude" / "state" / "daic-mode.json",
            {"mode": "discussion"},
        )

        self._orig_project_root = shared_state.PROJECT_ROOT
        self._orig_state_dir = shared_state.STATE_DIR
        self._orig_task_state_file = shared_state.TASK_STATE_FILE
        self._orig_task_state_lock_file = shared_state.TASK_STATE_LOCK_FILE
        self._orig_daic_state_file = shared_state.DAIC_STATE_FILE
        self._orig_protocol_logs_dir = shared_state.PROTOCOL_LOGS_DIR

        shared_state.PROJECT_ROOT = self.temp_dir
        shared_state.STATE_DIR = self.temp_dir / ".claude" / "state"
        shared_state.TASK_STATE_FILE = self.temp_dir / ".claude" / "state" / "current_task.json"
        shared_state.TASK_STATE_LOCK_FILE = self.temp_dir / ".claude" / "state" / "current_task.lock"
        shared_state.DAIC_STATE_FILE = self.temp_dir / ".claude" / "state" / "daic-mode.json"
        shared_state.PROTOCOL_LOGS_DIR = self.temp_dir / ".claude" / "state" / "protocol-logs"

        self.engine = ProtocolEngine(self.temp_dir)

    def tearDown(self):
        shared_state.PROJECT_ROOT = self._orig_project_root
        shared_state.STATE_DIR = self._orig_state_dir
        shared_state.TASK_STATE_FILE = self._orig_task_state_file
        shared_state.TASK_STATE_LOCK_FILE = self._orig_task_state_lock_file
        shared_state.DAIC_STATE_FILE = self._orig_daic_state_file
        shared_state.PROTOCOL_LOGS_DIR = self._orig_protocol_logs_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _write_config(self, **keys):
        cfg_path = self.temp_dir / "team-management" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(cfg_path, keys)

    def _set_task_state(self, task: str, branch: str):
        self._write_json(
            self.temp_dir / ".claude" / "state" / "current_task.json",
            {"task": task, "branch": branch, "services": [], "updated": "2026-04-20"},
        )


# ---------------------------------------------------------------------------
# _func_verify_tests_pass
# ---------------------------------------------------------------------------


class TestVerifyTestsPass(_TempEngineBase):
    def test_skipped_when_no_config_file(self):
        result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)
        self.assertIn("no config file", result.get("skipped", ""))

    def test_skipped_when_test_command_missing(self):
        self._write_config()
        result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)
        self.assertIn("not configured", result.get("skipped", ""))

    def test_skipped_when_test_command_null(self):
        self._write_config(test_command=None)
        result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)
        self.assertIn("not configured", result.get("skipped", ""))

    def test_skipped_when_test_command_empty(self):
        self._write_config(test_command="   ")
        result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)

    def test_skipped_when_config_is_unparseable(self):
        # Codex round-4 warning: a fat-finger edit elsewhere in config.json
        # must not block the optional test gate when test_command was never
        # configured.
        cfg_path = self.temp_dir / "team-management" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("{not valid json", encoding="utf-8")
        result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)
        self.assertIn("unreadable", result.get("skipped", ""))

    def test_skipped_when_config_is_non_dict(self):
        # Valid JSON but not an object — same defensive skip.
        cfg_path = self.temp_dir / "team-management" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("[]", encoding="utf-8")
        result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)
        self.assertIn("not an object", result.get("skipped", ""))

    def test_block_on_non_string_test_command(self):
        self._write_config(test_command=["pytest"])
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("must be a string", result["error"])

    # --- Metacharacter injection attempts ---
    def test_block_on_semicolon_injection(self):
        self._write_config(test_command="pytest; rm -rf /")
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("forbidden metacharacter", result["error"])
        self.assertIn(";", result["error"])

    def test_block_on_double_ampersand(self):
        self._write_config(test_command="pytest && curl evil.com")
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("&&", result["error"])

    def test_block_on_pipe(self):
        self._write_config(test_command="pytest | cat /etc/passwd")
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("|", result["error"])

    def test_block_on_backtick(self):
        self._write_config(test_command="pytest `id`")
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("`", result["error"])

    def test_block_on_command_substitution(self):
        self._write_config(test_command="pytest $(whoami)")
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("$(", result["error"])

    def test_block_on_redirect(self):
        self._write_config(test_command="pytest > /tmp/loot")
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn(">", result["error"])

    # --- Prefix allowlist ---
    def test_block_on_non_allowlisted_prefix(self):
        self._write_config(test_command="curl evil.com")
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("allowlisted prefixes", result["error"])

    def test_block_on_prefix_lookalike_binary(self):
        # 'pytesting' matches 'pytest' under a naive startswith() check. The
        # allowlist requires a word boundary (exact match or prefix followed
        # by a space), so this must be rejected.
        self._write_config(test_command="pytesting")
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("allowlist", result["error"].lower())

    def test_exact_prefix_passes_allowlist(self):
        self._write_config(test_command="pytest")
        with patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["pytest"], returncode=0, stdout="ok", stderr="",
            )
            result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)

    def test_bare_ruby_is_blocked(self):
        # Codex round-final security gap: bare `ruby` used to be on the
        # allowlist and `ruby -e "<arbitrary code>"` slipped past the checks
        # under shell=False. `ruby` was removed; Ruby runners live behind
        # `rspec` / `rake test` which cannot smuggle inline code.
        self._write_config(test_command="ruby -e 'puts 42'")
        result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("allowlist", result["error"].lower())

    def test_rspec_passes_allowlist(self):
        self._write_config(test_command="rspec spec/")
        with patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["rspec", "spec/"], returncode=0, stdout="2 examples, 0 failures", stderr="",
            )
            result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)

    def test_rake_test_passes_allowlist(self):
        self._write_config(test_command="rake test")
        with patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["rake", "test"], returncode=0, stdout="PASS", stderr="",
            )
            result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)

    def test_python3_m_pytest_passes_allowlist(self):
        # Regression guard: macOS/Linux users whose default Python 3 binary
        # is `python3` (not `python`) must be able to configure the gate.
        self._write_config(test_command="python3 -m pytest test/")
        with patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["python3", "-m", "pytest", "test/"], returncode=0,
                stdout="1 passed", stderr="",
            )
            result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)

    def test_python3_m_unittest_passes_allowlist(self):
        self._write_config(test_command="python3 -m unittest discover")
        with patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["python3", "-m", "unittest", "discover"], returncode=0,
                stdout="OK", stderr="",
            )
            result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)

    # --- Passing / failing real commands ---
    def test_passing_command_returns_success(self):
        self._write_config(test_command="pytest --help")
        with patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["pytest", "--help"], returncode=0,
                stdout="usage: pytest...", stderr="",
            )
            result = self.engine._func_verify_tests_pass()
        self.assertTrue(result["success"], result)
        self.assertIn("Tests passed", result["message"])

    def test_failing_command_returns_failure_with_output(self):
        self._write_config(test_command="pytest -k nonexistent")
        with patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["pytest", "-k", "nonexistent"], returncode=2,
                stdout="collected 0 items", stderr="errors: no tests",
            )
            result = self.engine._func_verify_tests_pass()
        self.assertFalse(result["success"])
        self.assertIn("exit 2", result["error"])
        self.assertIn("no tests", result["error"])
        self.assertEqual(result["exit_code"], 2)


# ---------------------------------------------------------------------------
# _func_present_completion_options
# ---------------------------------------------------------------------------


class TestPresentCompletionOptions(_TempEngineBase):
    def test_skipped_when_provider_gitlab(self):
        self._write_config(issue_tracking={"provider": "gitlab"})
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertIn("skipped", result)
        self.assertEqual(result["provider"], "gitlab")

    def test_skipped_when_provider_github(self):
        self._write_config(issue_tracking={"provider": "github"})
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result["provider"], "github")

    def test_menu_shown_when_provider_disabled(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "disabled")
        self.assertEqual(
            result.get("options"),
            ["merge_local", "push_pr", "keep", "discard"],
        )
        self.assertIn("merge_local", result["message"])
        self.assertIn("push_pr", result["message"])
        self.assertIn("keep", result["message"])
        self.assertIn("discard", result["message"])

    def test_missing_config_preserves_provider_flow(self):
        # Regression guard (Codex critical): when config.json does not exist,
        # we must NOT fall into the 4-option menu — the old behaviour let the
        # provider-driven chain run with individual funcs handling missing
        # config. Pre_func reports `provider: "unknown"` and a skipped reason.
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "unknown")
        self.assertIn("unreadable", result.get("skipped", ""))

    def test_malformed_config_preserves_provider_flow(self):
        # Regression guard (Codex critical): garbage in config.json must not
        # lock existing GitLab/GitHub/Jira users out of completion.
        cfg_path = self.temp_dir / "team-management" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("{this is not valid json", encoding="utf-8")
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "unknown")
        self.assertIn("unreadable", result.get("skipped", ""))

    def test_legacy_config_with_provider_enabled_preserves_provider_flow(self):
        # A config predating the `issue_tracking.provider` key but with a
        # provider actually ENABLED (genuinely-old GitLab/Jira install) must
        # STILL run the provider-driven chain — no menu, no regression
        # (m-fix-completion-strands-without-remote).
        self._write_config(developer_name="Legacy", gitlab={"enabled": True})
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "unknown")
        self.assertIn("legacy", result.get("skipped", ""))

    def test_no_tracker_no_provider_enabled_shows_menu(self):
        # Plugin-era fresh config: no `issue_tracking` section and no provider
        # enabled → infer "disabled" and show the 4-option local-completion
        # menu instead of forcing the remote provider chain
        # (m-fix-completion-strands-without-remote).
        self._write_config(developer_name="Fresh")  # no issue_tracking, no provider
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "disabled")
        self.assertEqual(
            result.get("options"),
            ["merge_local", "push_pr", "keep", "discard"],
        )

    def test_issue_tracking_without_provider_key_with_provider_enabled_preserves_flow(self):
        # `issue_tracking` present but `provider` key absent, WITH a provider
        # enabled → legacy provider flow preserved.
        self._write_config(issue_tracking={"auto_sync": True}, github={"enabled": True})
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "unknown")
        self.assertIn("legacy", result.get("skipped", ""))

    def test_issue_tracking_without_provider_key_no_provider_enabled_shows_menu(self):
        # Sibling of the fresh-config case: `issue_tracking` present, `provider`
        # key absent, no provider enabled → menu.
        self._write_config(issue_tracking={"auto_sync": True})
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "disabled")
        self.assertEqual(
            result.get("options"),
            ["merge_local", "push_pr", "keep", "discard"],
        )

    def test_enabled_probe_tolerates_malformed_provider_section(self):
        # A malformed provider section (non-dict) must not crash the enabled
        # probe; with no provider truly enabled the config shows the menu.
        self._write_config(gitlab="oops", github=5)
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "disabled")

    def test_non_dict_issue_tracking_preserves_provider_flow(self):
        # Corruption guard (codex plan review): a present-but-non-dict
        # `issue_tracking` section must NOT be inferred as disabled (menu) —
        # preserve the provider chain, mirroring the top-level malformed-config
        # contract (m-fix-completion-strands-without-remote).
        self._write_config(issue_tracking="oops")
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "unknown")
        self.assertIn("legacy", result.get("skipped", ""))

    def test_non_dict_config_preserves_provider_flow(self):
        # Codex round-4 warning: valid JSON but wrong shape (list, string,
        # null, number) must not raise AttributeError. Fall back to the
        # provider chain like unreadable files do.
        cfg_path = self.temp_dir / "team-management" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("[]", encoding="utf-8")
        result = self.engine._func_present_completion_options()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("provider"), "unknown")
        self.assertIn("unreadable", result.get("skipped", ""))


# ---------------------------------------------------------------------------
# _func_require_discard_confirmation
# ---------------------------------------------------------------------------


class TestRequireDiscardConfirmation(_TempEngineBase):
    def test_pass_through_when_option_not_discard(self):
        result = self.engine._func_require_discard_confirmation(
            args={"completion_option": "keep"}
        )
        self.assertTrue(result["success"])
        self.assertIn("skipped", result)

    def test_pass_through_when_no_option(self):
        result = self.engine._func_require_discard_confirmation(args={})
        self.assertTrue(result["success"])
        self.assertIn("skipped", result)

    def test_dry_run_block_on_first_call(self):
        self._set_task_state("m-foo", "feature/foo")
        result = self.engine._func_require_discard_confirmation(
            args={"completion_option": "discard"}
        )
        self.assertFalse(result["success"])
        self.assertIn("Dry-run", result["error"])
        self.assertIn("feature/foo", result["error"])
        self.assertIn("discard_confirmation", result["error"])
        self.assertIn("discard_confirmed_dry_run", result["error"])

    def test_block_when_dry_run_acknowledged_but_confirmation_missing(self):
        self._set_task_state("m-foo", "feature/foo")
        result = self.engine._func_require_discard_confirmation(args={
            "completion_option": "discard",
            "discard_confirmed_dry_run": True,
        })
        self.assertFalse(result["success"])
        self.assertIn("must equal the exact string 'discard'", result["error"])

    def test_block_when_dry_run_acknowledged_but_confirmation_wrong(self):
        self._set_task_state("m-foo", "feature/foo")
        result = self.engine._func_require_discard_confirmation(args={
            "completion_option": "discard",
            "discard_confirmation": "DISCARD",  # wrong case
            "discard_confirmed_dry_run": True,
        })
        self.assertFalse(result["success"])
        self.assertIn("'discard'", result["error"])

    def test_block_when_confirmation_present_but_dry_run_flag_missing(self):
        self._set_task_state("m-foo", "feature/foo")
        result = self.engine._func_require_discard_confirmation(args={
            "completion_option": "discard",
            "discard_confirmation": "discard",
        })
        self.assertFalse(result["success"])
        self.assertIn("Dry-run", result["error"])

    def test_pass_with_full_two_step_confirmation(self):
        self._set_task_state("m-foo", "feature/foo")
        result = self.engine._func_require_discard_confirmation(args={
            "completion_option": "discard",
            "discard_confirmation": "discard",
            "discard_confirmed_dry_run": True,
        })
        self.assertTrue(result["success"], result)
        self.assertEqual(result["branch"], "feature/foo")


# ---------------------------------------------------------------------------
# _func_completion_dispatch — dispatch-table smoke tests
# ---------------------------------------------------------------------------


class TestCompletionDispatchRouting(_TempEngineBase):
    """Verify dispatch picks the right branch without actually executing
    git / gh sub-funcs. Functional tests for each branch would need a real
    git repo; we assert routing by patching the leaf funcs.
    """

    def test_provider_branch_calls_provider_chain(self):
        self._write_config(issue_tracking={"provider": "gitlab"})
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(
            self.engine, "_run_completion_chain",
            return_value={"func": "completion_dispatch", "success": True,
                          "branch_taken": "provider", "sub_results": []},
        ) as chain_mock:
            result = self.engine._func_completion_dispatch(args={})
        self.assertTrue(result["success"])
        self.assertEqual(result["branch_taken"], "provider")
        chain_mock.assert_called_once()
        self.assertEqual(chain_mock.call_args.args[0], "provider")

    def test_missing_config_routes_to_provider_chain(self):
        # Regression guard (Codex critical): an absent config.json used to run
        # the provider chain. After the fix, unreadable config → provider
        # chain (not the 4-option menu).
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(
            self.engine, "_run_completion_chain",
            return_value={"func": "completion_dispatch", "success": True,
                          "branch_taken": "provider", "sub_results": []},
        ) as chain_mock:
            result = self.engine._func_completion_dispatch(args={})
        self.assertTrue(result["success"])
        self.assertEqual(result["branch_taken"], "provider")
        chain_mock.assert_called_once()

    def test_malformed_config_routes_to_provider_chain(self):
        cfg_path = self.temp_dir / "team-management" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("{broken", encoding="utf-8")
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(
            self.engine, "_run_completion_chain",
            return_value={"func": "completion_dispatch", "success": True,
                          "branch_taken": "provider", "sub_results": []},
        ) as chain_mock:
            result = self.engine._func_completion_dispatch(args={})
        self.assertTrue(result["success"])
        self.assertEqual(result["branch_taken"], "provider")
        chain_mock.assert_called_once()

    def test_legacy_config_with_provider_enabled_routes_to_provider_chain(self):
        # Config without `issue_tracking.provider` but WITH a provider enabled
        # (genuinely-old install) MUST run the legacy provider chain, not the
        # menu (m-fix-completion-strands-without-remote).
        self._write_config(developer_name="Legacy", gitlab={"enabled": True})
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(
            self.engine, "_run_completion_chain",
            return_value={"func": "completion_dispatch", "success": True,
                          "branch_taken": "provider", "sub_results": []},
        ) as chain_mock:
            result = self.engine._func_completion_dispatch(args={})
        self.assertTrue(result["success"])
        self.assertEqual(result["branch_taken"], "provider")
        chain_mock.assert_called_once()

    def test_legacy_no_provider_enabled_merge_local_routes_to_merge_local(self):
        # Fresh no-tracker config + explicit merge_local → the inferred-disabled
        # menu path runs the LOCAL merge (no provider chain, no git fetch), so
        # the protocol completes even with no remote
        # (m-fix-completion-strands-without-remote).
        self._write_config(developer_name="Fresh")
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch.object(
            self.engine, "_completion_merge_local",
            return_value={"func": "completion_dispatch", "success": True,
                          "branch_taken": "merge_local", "sub_results": []},
        ) as mock_fn:
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "merge_local"}
            )
        mock_fn.assert_called_once()
        self.assertEqual(result["branch_taken"], "merge_local")

    def test_push_pr_reuses_existing_pr_on_retry(self):
        # Codex round-3 warning: after a partial failure (PR created but
        # post-PR housekeeping failed), a retry must detect the existing PR
        # and reuse it instead of calling `gh pr create` again (which would
        # exit non-zero with "a pull request already exists").
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")

        existing_url = "https://github.com/owner/repo/pull/42"

        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch.object(self.engine, "_func_archive_task",
                          return_value={"func": "archive_task", "success": True}), \
             patch.object(self.engine, "_func_git_commit",
                          return_value={"func": "git_commit", "success": True}), \
             patch.object(self.engine, "_func_git_push",
                          return_value={"func": "git_push", "success": True}), \
             patch.object(self.engine, "_func_cleanup_task_scoped_state",
                          return_value={"func": "cleanup_task_scoped_state", "success": True}), \
             patch.object(self.engine, "_func_clear_task_state",
                          return_value={"func": "clear_task_state", "success": True}), \
             patch.object(self.engine, "_func_checkout_default_branch",
                          return_value={"func": "checkout_default_branch", "success": True,
                                        "branch": "main"}), \
             patch.object(self.engine, "_gh_find_existing_pr",
                          return_value=existing_url) as find_mock, \
             patch("shutil.which", return_value="/usr/local/bin/gh"), \
             patch("subprocess.run") as run_mock:
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "push_pr"}
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["pr_url"], existing_url)
        find_mock.assert_called_once_with("feature/foo")
        # `gh pr create` must NOT have been invoked.
        for call in run_mock.call_args_list:
            cmd = call.args[0] if call.args else []
            self.assertNotEqual(cmd[:3], ["gh", "pr", "create"],
                                f"unexpected gh pr create call: {cmd}")
        # Reused-existing marker must be in sub_results.
        gh_results = [r for r in result["sub_results"] if r.get("func") == "gh_pr_create"]
        self.assertEqual(len(gh_results), 1)
        self.assertTrue(gh_results[0].get("reused_existing"))

    def test_disabled_without_option_fails(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        result = self.engine._func_completion_dispatch(args={})
        self.assertFalse(result["success"])
        self.assertIn("completion_option is required", result["error"])

    def test_disabled_unknown_option_fails(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(self.engine, "_current_branch", return_value="feature/foo"):
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "bogus"}
            )
        self.assertFalse(result["success"])
        self.assertIn("Unknown completion_option", result["error"])

    def test_disabled_merge_local_routes_to_merge_local(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch.object(
            self.engine, "_completion_merge_local",
            return_value={"func": "completion_dispatch", "success": True,
                          "branch_taken": "merge_local", "sub_results": []},
        ) as mock_fn:
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "merge_local"}
            )
        mock_fn.assert_called_once()
        self.assertEqual(result["branch_taken"], "merge_local")

    def test_disabled_push_pr_routes_to_push_pr(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch.object(
            self.engine, "_completion_push_pr",
            return_value={"func": "completion_dispatch", "success": True,
                          "branch_taken": "push_pr", "sub_results": []},
        ) as mock_fn:
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "push_pr"}
            )
        mock_fn.assert_called_once()
        self.assertEqual(result["branch_taken"], "push_pr")

    def test_disabled_keep_routes_to_keep(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch.object(
            self.engine, "_completion_keep",
            return_value={"func": "completion_dispatch", "success": True,
                          "branch_taken": "keep", "sub_results": []},
        ) as mock_fn:
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "keep"}
            )
        mock_fn.assert_called_once()
        self.assertEqual(result["branch_taken"], "keep")

    def test_disabled_discard_routes_to_discard(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch.object(
            self.engine, "_completion_discard",
            return_value={"func": "completion_dispatch", "success": True,
                          "branch_taken": "discard", "sub_results": []},
        ) as mock_fn:
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "discard"}
            )
        mock_fn.assert_called_once()
        self.assertEqual(result["branch_taken"], "discard")

    def test_push_pr_missing_gh_fails_with_install_hint(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch("shutil.which", return_value=None):
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "push_pr"}
            )
        self.assertFalse(result["success"])
        self.assertIn("gh", result["error"])
        self.assertIn("brew install", result["error"])

    def test_disabled_flow_blocked_when_head_is_wrong_branch(self):
        # Codex round-5 critical: all 4 disabled-provider flows must refuse
        # to run when HEAD is not on the task's feature branch — otherwise
        # `discard` can `git reset --hard` a branch the user never intended
        # to touch (silent data loss).
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")

        for option in ("merge_local", "push_pr", "keep", "discard"):
            with patch.object(self.engine, "_current_branch", return_value="main"):
                result = self.engine._func_completion_dispatch(
                    args={"completion_option": option,
                          # discard also requires confirmation args; include
                          # them so we're testing the branch check, not the
                          # discard gate.
                          "discard_confirmation": "discard",
                          "discard_confirmed_dry_run": True}
                )
            self.assertFalse(result["success"], f"option={option} should be blocked")
            self.assertIn("Branch mismatch", result["error"], f"option={option}")
            self.assertEqual(result.get("current_branch"), "main")
            self.assertEqual(result.get("expected_branch"), "feature/foo")

    def test_disabled_flow_message_is_explicit_on_detached_head(self):
        # Claude round-6 cosmetic: detached HEAD used to surface "'None'"
        # literally in the error. Now reads as "detached HEAD (no branch)".
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")
        with patch.object(self.engine, "_current_branch", return_value=None):
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "keep"}
            )
        self.assertFalse(result["success"])
        self.assertIn("detached HEAD", result["error"])
        self.assertIsNone(result.get("current_branch"))

    def test_detect_default_branch_uses_origin_head(self):
        # Codex round-6 warning: `_detect_default_branch` must not hard-code
        # main/master — it must consult origin/HEAD first so custom default
        # branch names (develop/trunk) work in push_pr.
        called = {}

        def fake_run(cmd, *a, **kw):
            called["cmd"] = cmd
            if cmd[:2] == ["git", "symbolic-ref"]:
                return subprocess.CompletedProcess(cmd, 0, "origin/develop\n", "")
            # Should never be called — symbolic-ref returns first.
            raise AssertionError(f"fell through to fallback path: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            branch = self.engine._detect_default_branch()
        self.assertEqual(branch, "develop")

    def test_detect_default_branch_falls_back_when_symref_missing(self):
        # No origin/HEAD → falls through to local candidate probe.
        def fake_run(cmd, *a, **kw):
            if cmd[:2] == ["git", "symbolic-ref"]:
                return subprocess.CompletedProcess(cmd, 1, "", "fatal: no ref")
            if cmd[:3] == ["git", "rev-parse", "--verify"]:
                return subprocess.CompletedProcess(cmd, 0 if cmd[3] == "master" else 1, "", "")
            raise AssertionError(f"unexpected call: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            branch = self.engine._detect_default_branch()
        self.assertEqual(branch, "master")

    def test_disabled_flow_proceeds_when_head_matches(self):
        # Sanity check — with HEAD on the expected feature branch, dispatch
        # reaches the helper (mocked to succeed).
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")

        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch.object(self.engine, "_completion_keep",
                          return_value={"func": "completion_dispatch",
                                        "success": True,
                                        "branch_taken": "keep",
                                        "sub_results": []}) as keep_mock:
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "keep"}
            )

        self.assertTrue(result["success"])
        keep_mock.assert_called_once()

    def test_discard_fails_when_git_clean_fails(self):
        # Codex warning: git clean non-zero exit must short-circuit BEFORE the
        # branch is deleted, not continue silently.
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")

        observed_clean_cmd = []

        def fake_run(cmd, *a, **kw):
            if cmd[:2] == ["git", "reset"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:2] == ["git", "clean"]:
                observed_clean_cmd.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 1, "", "permission denied")
            # Any further git call would indicate the short-circuit failed.
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch("subprocess.run", side_effect=fake_run):
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "discard",
                      "discard_confirmation": "discard",
                      "discard_confirmed_dry_run": True}
            )
        self.assertFalse(result["success"])
        self.assertEqual(result.get("failed_at"), "git_clean")
        self.assertIn("permission denied", result["error"])
        # Codex round-final: discard must clean gitignored files too (-fdx).
        self.assertEqual(len(observed_clean_cmd), 1)
        clean_cmd = observed_clean_cmd[0]
        self.assertIn("-fdx", clean_cmd,
                      f"discard should use `git clean -fdx`, got {clean_cmd}")
        # h-fix-discard-clean-and-windows-transcript: must exclude the framework
        # working tree so a discard never wipes config / mappings / sibling tasks.
        self.assertIn("-e", clean_cmd, f"discard clean must carry -e excludes, got {clean_cmd}")
        self.assertIn("team-management/", clean_cmd,
                      f"discard clean must exclude team-management/, got {clean_cmd}")
        self.assertIn(".claude/", clean_cmd,
                      f"discard clean must exclude .claude/, got {clean_cmd}")

    def test_push_pr_fails_when_checkout_default_fails(self):
        # Codex warning: post-PR checkout failure must surface as dispatcher
        # failure, not false success.
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")

        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch.object(self.engine, "_func_archive_task",
                          return_value={"func": "archive_task", "success": True}), \
             patch.object(self.engine, "_func_git_commit",
                          return_value={"func": "git_commit", "success": True}), \
             patch.object(self.engine, "_func_git_push",
                          return_value={"func": "git_push", "success": True}), \
             patch.object(self.engine, "_func_cleanup_task_scoped_state",
                          return_value={"func": "cleanup_task_scoped_state", "success": True}), \
             patch.object(self.engine, "_func_clear_task_state",
                          return_value={"func": "clear_task_state", "success": True}), \
             patch.object(self.engine, "_func_checkout_default_branch",
                          return_value={"func": "checkout_default_branch",
                                        "success": False,
                                        "error": "uncommitted changes would be overwritten"}), \
             patch("shutil.which", return_value="/usr/local/bin/gh"), \
             patch("subprocess.run",
                   return_value=subprocess.CompletedProcess(
                       args=["gh", "pr", "create"], returncode=0,
                       stdout="https://github.com/owner/repo/pull/42\n", stderr="",
                   )):
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "push_pr"}
            )
        self.assertFalse(result["success"])
        self.assertEqual(result.get("failed_at"), "checkout_default_branch")
        self.assertIn("uncommitted", result["error"])

    def test_push_pr_preserves_task_state_when_checkout_fails(self):
        # Codex warning (round 2): if checkout_default_branch fails, task
        # state MUST remain intact so the user can retry cleanly. Order:
        # cleanup → checkout → clear. Checkout failure stops the chain
        # BEFORE clear runs.
        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")

        clear_mock = patch.object(
            self.engine, "_func_clear_task_state",
            return_value={"func": "clear_task_state", "success": True},
        )

        with patch.object(self.engine, "_current_branch", return_value="feature/foo"), \
             patch.object(self.engine, "_func_archive_task",
                          return_value={"func": "archive_task", "success": True}), \
             patch.object(self.engine, "_func_git_commit",
                          return_value={"func": "git_commit", "success": True}), \
             patch.object(self.engine, "_func_git_push",
                          return_value={"func": "git_push", "success": True}), \
             patch.object(self.engine, "_func_cleanup_task_scoped_state",
                          return_value={"func": "cleanup_task_scoped_state", "success": True}), \
             clear_mock as clear_patched, \
             patch.object(self.engine, "_func_checkout_default_branch",
                          return_value={"func": "checkout_default_branch",
                                        "success": False,
                                        "error": "default branch not found"}), \
             patch("shutil.which", return_value="/usr/local/bin/gh"), \
             patch("subprocess.run",
                   return_value=subprocess.CompletedProcess(
                       args=["gh", "pr", "create"], returncode=0,
                       stdout="https://github.com/owner/repo/pull/42\n", stderr="",
                   )):
            result = self.engine._func_completion_dispatch(
                args={"completion_option": "push_pr"}
            )

        self.assertFalse(result["success"])
        self.assertEqual(result.get("failed_at"), "checkout_default_branch")
        # KEY ASSERTION: clear_task_state MUST NOT have been called — the
        # user needs current_task.json intact to retry.
        clear_patched.assert_not_called()


# ---------------------------------------------------------------------------
# Handler registry cross-validation
# ---------------------------------------------------------------------------


class TestFuncRegistration(_TempEngineBase):
    def test_all_four_new_funcs_registered(self):
        handlers = self.engine._build_handlers()
        for name in (
            "verify_tests_pass",
            "present_completion_options",
            "require_discard_confirmation",
            "completion_dispatch",
        ):
            self.assertIn(name, handlers, f"handler missing: {name}")

    def test_get_available_funcs_reports_no_discrepancies(self):
        result = self.engine.get_available_funcs()
        self.assertTrue(result["success"])
        self.assertNotIn("discrepancies", result)


class TestTaskJsonWiring(TestCase):
    """Structural checks on the `task` protocol config itself — guards against
    a future edit that accidentally decouples the pre/post_funcs.
    """

    def setUp(self):
        task_json = REPO_ROOT / "plugin" / "protocol-configs" / "task.json"
        with open(task_json, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.steps_by_name = {s["name"]: s for s in self.config["steps"]}

    def test_code_review_has_verify_tests_pass_in_post_funcs(self):
        # Pre_funcs are cosmetic (advance_step does not check success), so
        # the test gate lives in post_funcs with post_funcs_stop_on_failure.
        # Round 3: dropped from pre_funcs to avoid running the suite twice
        # per code-review cycle.
        step = self.steps_by_name["code-review"]
        self.assertIn("verify_tests_pass", step.get("post_funcs", []))
        self.assertTrue(step.get("post_funcs_stop_on_failure"),
                        "code-review.post_funcs_stop_on_failure must be true "
                        "for the test gate to actually block advance.")
        self.assertNotIn("verify_tests_pass", step.get("pre_funcs", []),
                         "Round 3: verify_tests_pass was moved out of pre_funcs "
                         "so the test suite runs once per cycle, not twice.")

    def test_completion_has_present_options_in_pre_funcs(self):
        step = self.steps_by_name["completion"]
        self.assertIn("present_completion_options", step.get("pre_funcs", []))

    def test_completion_post_funcs_order(self):
        step = self.steps_by_name["completion"]
        post = step.get("post_funcs", [])
        self.assertEqual(
            post,
            ["require_discard_confirmation", "completion_dispatch"],
            "require_discard_confirmation MUST run before completion_dispatch "
            "so the dry-run friction gate short-circuits via "
            "post_funcs_stop_on_failure before the dispatcher runs the destructive path.",
        )
        self.assertTrue(step.get("post_funcs_stop_on_failure"))


# ---------------------------------------------------------------------------
# _read_issue_provider classification (m-fix-completion-strands-without-remote)
# ---------------------------------------------------------------------------


class TestReadIssueProviderClassification(_TempEngineBase):
    """Central classification contract. All THREE consumers (present-options,
    normal dispatch, optimize dispatch) key on
    (source == 'configured' and provider == 'disabled'), so testing
    `_read_issue_provider` directly covers the optimize-path coupling too.
    """

    def test_absent_section_no_provider_infers_disabled(self):
        self._write_config(developer_name="Fresh")
        self.assertEqual(self.engine._read_issue_provider(), ("disabled", "configured"))

    def test_absent_section_with_provider_enabled_is_legacy(self):
        self._write_config(developer_name="Old", jira={"enabled": True})
        self.assertEqual(self.engine._read_issue_provider(), ("unknown", "legacy"))

    def test_dict_without_provider_no_enabled_infers_disabled(self):
        self._write_config(issue_tracking={"auto_sync": True})
        self.assertEqual(self.engine._read_issue_provider(), ("disabled", "configured"))

    def test_dict_without_provider_with_enabled_is_legacy(self):
        self._write_config(issue_tracking={"auto_sync": True}, github={"enabled": True})
        self.assertEqual(self.engine._read_issue_provider(), ("unknown", "legacy"))

    def test_non_dict_issue_tracking_is_legacy(self):
        self._write_config(issue_tracking="oops")
        self.assertEqual(self.engine._read_issue_provider(), ("unknown", "legacy"))

    def test_explicit_disabled_unchanged(self):
        self._write_config(issue_tracking={"provider": "disabled"})
        self.assertEqual(self.engine._read_issue_provider(), ("disabled", "configured"))

    def test_explicit_gitlab_unchanged(self):
        self._write_config(issue_tracking={"provider": "gitlab"})
        self.assertEqual(self.engine._read_issue_provider(), ("gitlab", "configured"))

    def test_missing_config_still_unreadable(self):
        # No config.json at all → unreadable (unchanged).
        self.assertEqual(self.engine._read_issue_provider(), ("unknown", "unreadable"))

    def test_any_provider_enabled_helper_isinstance_guarded(self):
        self.assertFalse(self.engine._any_issue_provider_enabled({"gitlab": "x", "github": 5}))
        self.assertTrue(self.engine._any_issue_provider_enabled({"jira": {"enabled": True}}))
        self.assertFalse(self.engine._any_issue_provider_enabled({"gitlab": {"enabled": False}}))


# ---------------------------------------------------------------------------
# No-remote safety net (m-fix-completion-strands-without-remote)
# ---------------------------------------------------------------------------


class TestNoRemoteSafety(_TempEngineBase):
    """`_func_git_merge_main` / `_func_git_push` must skip gracefully in a
    local-only repo (no `origin` remote) instead of hard-failing on
    `git fetch origin` / `git push origin` and stranding the completion chain.
    """

    def test_origin_remote_exists_true_when_present(self):
        with patch("subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "origin\n", "")):
            self.assertTrue(self.engine._origin_remote_exists())

    def test_origin_remote_exists_false_when_absent(self):
        with patch("subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertFalse(self.engine._origin_remote_exists())

    def test_origin_remote_exists_true_on_git_error(self):
        # A failed `git remote` query (rc != 0 — not a repo / git missing) must
        # NOT be read as "no origin"; return True so the caller's fetch/push
        # surfaces the real error instead of silently skipping.
        with patch("subprocess.run",
                   return_value=subprocess.CompletedProcess([], 128, "", "fatal")):
            self.assertTrue(self.engine._origin_remote_exists())

    def test_git_merge_main_skips_when_no_origin(self):
        self._set_task_state("m-foo", "fix/foo")
        with patch.object(self.engine, "_origin_remote_exists", return_value=False), \
             patch.object(self.engine, "_detect_default_branch") as det_mock, \
             patch("subprocess.run") as run_mock:
            result = self.engine._func_git_merge_main()
        self.assertEqual(result["func"], "git_merge_main")
        self.assertTrue(result["success"])
        self.assertEqual(result.get("action"), "skipped")
        self.assertEqual(result.get("branch"), "fix/foo")
        self.assertIn("origin", result.get("message", ""))
        # Skip fires before default-branch detection and any git op.
        det_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_git_push_skips_when_no_origin(self):
        self._set_task_state("m-foo", "fix/foo")
        with patch.object(self.engine, "_origin_remote_exists", return_value=False), \
             patch("subprocess.run") as run_mock:
            result = self.engine._func_git_push()
        self.assertTrue(result["success"])
        self.assertEqual(result.get("action"), "skipped")
        run_mock.assert_not_called()

    def test_git_merge_main_runs_fetch_when_origin_present(self):
        # Regression: when origin exists, the func must NOT skip — it fetches
        # and merges as before.
        self._set_task_state("m-foo", "fix/foo")
        with patch.object(self.engine, "_origin_remote_exists", return_value=True), \
             patch.object(self.engine, "_detect_default_branch", return_value="main"), \
             patch("subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "", "")) as run_mock:
            result = self.engine._func_git_merge_main()
        self.assertTrue(result["success"])
        self.assertNotEqual(result.get("action"), "skipped")
        # First git call is the fetch.
        first_cmd = run_mock.call_args_list[0].args[0]
        self.assertEqual(first_cmd[:3], ["git", "fetch", "origin"])


class TestArchiveTaskStatusCompleted(_TempEngineBase):
    """l-fix-archive-task-status-completed: archiving flips frontmatter
    status to completed (+ paired completed: date), best-effort, on the
    file branch, the directory branch, and the already-archived retry."""

    FRONTMATTER = (
        "---\n"
        "task: {name}\n"
        "branch: fix/x\n"
        "status: in-progress\n"
        "created: 2026-07-28\n"
        "started: 2026-07-29\n"
        "---\n\n# Title\n\n## Success Criteria\n- [x] SC-1: done\n"
    )

    def _set_task(self, name):
        self._write_json(
            self.temp_dir / ".claude" / "state" / "current_task.json",
            {"task": name, "branch": "fix/x", "services": [], "updated": "2026-07-29"},
        )

    def test_archive_flips_status_to_completed(self):
        name = "l-file-task"
        task_file = self.temp_dir / "team-management" / "tasks" / f"{name}.md"
        task_file.write_text(self.FRONTMATTER.format(name=name), encoding="utf-8")
        self._set_task(name)

        result = self.engine._func_archive_task()

        self.assertTrue(result["success"])
        self.assertTrue(result.get("status_updated"))
        dest = self.temp_dir / "team-management" / "tasks" / "done" / f"{name}.md"
        self.assertTrue(dest.exists())
        content = dest.read_text(encoding="utf-8")
        self.assertIn("status: completed", content)
        self.assertNotIn("status: in-progress", content)
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        started_idx = content.index("started: 2026-07-29")
        completed_idx = content.index(f"completed: {today}")
        self.assertGreater(completed_idx, started_idx)
        self.assertEqual(content.count("completed:"), 1)
        self.assertIn("## Success Criteria", content)  # body preserved

    def test_archive_directory_task_flips_status(self):
        name = "l-dir-task"
        task_dir = self.temp_dir / "team-management" / "tasks" / name
        task_dir.mkdir(parents=True)
        (task_dir / "README.md").write_text(
            self.FRONTMATTER.format(name=name), encoding="utf-8"
        )
        self._set_task(name)

        result = self.engine._func_archive_task()

        self.assertTrue(result["success"])
        self.assertTrue(result.get("status_updated"))
        dest = self.temp_dir / "team-management" / "tasks" / "done" / name / "README.md"
        self.assertTrue(dest.exists())
        content = dest.read_text(encoding="utf-8")
        self.assertIn("status: completed", content)
        self.assertNotIn("status: in-progress", content)

    def test_archive_without_frontmatter_still_archives(self):
        name = "l-bare-task"
        task_file = self.temp_dir / "team-management" / "tasks" / f"{name}.md"
        task_file.write_text("# Just a title, no frontmatter\n", encoding="utf-8")
        self._set_task(name)

        result = self.engine._func_archive_task()

        self.assertTrue(result["success"])
        self.assertFalse(result.get("status_updated"))
        dest = self.temp_dir / "team-management" / "tasks" / "done" / f"{name}.md"
        self.assertTrue(dest.exists())
        self.assertEqual(
            dest.read_text(encoding="utf-8"), "# Just a title, no frontmatter\n"
        )

    def test_archive_retry_repairs_stale_status(self):
        name = "l-stale-task"
        done_dir = self.temp_dir / "team-management" / "tasks" / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / f"{name}.md").write_text(
            self.FRONTMATTER.format(name=name), encoding="utf-8"
        )
        self._set_task(name)  # no active copy — only the done/ one

        result = self.engine._func_archive_task()

        self.assertTrue(result["success"])
        self.assertTrue(result.get("already_archived"))
        self.assertTrue(result.get("status_updated"))
        content = (done_dir / f"{name}.md").read_text(encoding="utf-8")
        self.assertIn("status: completed", content)
        self.assertNotIn("status: in-progress", content)

    def test_archive_frontmatter_value_with_dashes_not_premature_close(self):
        # A "---" inside a frontmatter VALUE must not be taken as the closing
        # delimiter (codex plan review: raw substring find corrupted the file).
        name = "l-dashes-task"
        task_file = self.temp_dir / "team-management" / "tasks" / f"{name}.md"
        task_file.write_text(
            "---\n"
            f"task: {name}\n"
            "note: separator --- inside a value\n"
            "status: in-progress\n"
            "created: 2026-07-28\n"
            "started: 2026-07-29\n"
            "---\n\n# Title\n\n## Success Criteria\n- [x] SC-1: done\n",
            encoding="utf-8",
        )
        self._set_task(name)

        result = self.engine._func_archive_task()

        self.assertTrue(result["success"])
        self.assertTrue(result.get("status_updated"))
        dest = self.temp_dir / "team-management" / "tasks" / "done" / f"{name}.md"
        content = dest.read_text(encoding="utf-8")
        self.assertIn("note: separator --- inside a value\n", content)
        self.assertIn("status: completed", content)
        self.assertNotIn("status: in-progress", content)
        self.assertIn("## Success Criteria", content)

    def test_archive_indented_status_line_not_rewritten(self):
        # An INDENTED "status:" (e.g. inside a YAML block scalar) is not a
        # top-level key and must be left untouched (codex plan review).
        name = "l-indented-task"
        task_file = self.temp_dir / "team-management" / "tasks" / f"{name}.md"
        task_file.write_text(
            "---\n"
            f"task: {name}\n"
            "description: |\n"
            "  status: draft\n"
            "status: in-progress\n"
            "created: 2026-07-28\n"
            "started: 2026-07-29\n"
            "---\n\n# Title\n",
            encoding="utf-8",
        )
        self._set_task(name)

        result = self.engine._func_archive_task()

        self.assertTrue(result["success"])
        self.assertTrue(result.get("status_updated"))
        dest = self.temp_dir / "team-management" / "tasks" / "done" / f"{name}.md"
        content = dest.read_text(encoding="utf-8")
        self.assertIn("  status: draft\n", content)
        self.assertIn("\nstatus: completed\n", content)
        self.assertNotIn("status: in-progress", content)

    def test_archive_existing_completed_line_not_duplicated(self):
        name = "l-precompleted-task"
        task_file = self.temp_dir / "team-management" / "tasks" / f"{name}.md"
        task_file.write_text(
            "---\n"
            f"task: {name}\n"
            "status: in-progress\n"
            "created: 2026-07-28\n"
            "started: 2026-07-29\n"
            "completed: 2026-07-01\n"
            "---\n\n# Title\n",
            encoding="utf-8",
        )
        self._set_task(name)

        result = self.engine._func_archive_task()

        self.assertTrue(result["success"])
        self.assertTrue(result.get("status_updated"))
        dest = self.temp_dir / "team-management" / "tasks" / "done" / f"{name}.md"
        content = dest.read_text(encoding="utf-8")
        self.assertEqual(content.count("completed:"), 1)
        self.assertIn("completed: 2026-07-01", content)  # existing date kept
        self.assertIn("status: completed", content)

    def test_archive_completed_inserted_after_created_when_no_started(self):
        name = "l-nostarted-task"
        task_file = self.temp_dir / "team-management" / "tasks" / f"{name}.md"
        task_file.write_text(
            "---\n"
            f"task: {name}\n"
            "status: in-progress\n"
            "created: 2026-07-28\n"
            "---\n\n# Title\n",
            encoding="utf-8",
        )
        self._set_task(name)

        result = self.engine._func_archive_task()

        self.assertTrue(result["success"])
        self.assertTrue(result.get("status_updated"))
        dest = self.temp_dir / "team-management" / "tasks" / "done" / f"{name}.md"
        content = dest.read_text(encoding="utf-8")
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        created_idx = content.index("created: 2026-07-28")
        completed_idx = content.index(f"completed: {today}")
        self.assertGreater(completed_idx, created_idx)


if __name__ == "__main__":
    main()
