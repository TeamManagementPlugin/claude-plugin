#!/usr/bin/env python3
"""Tests for agy's headless permission rules (shared_state.py).

agy's headless print mode auto-denies every `run_command` tool call that no
`permissions.allow` rule in `~/.gemini/antigravity-cli/settings.json` grants —
there is no prompt to answer. Until m-fix-agy-launch-and-gate the framework got
past that with `--dangerously-skip-permissions`, which harness security monitors
block. These rules replace the flag: agy may ATTEMPT only the listed binaries,
and the read-only deny-gate still decides which invocations are actually reads.

Covers ``_AGY_PERMISSION_RULES`` (canonical list),
``agy_permission_rules_present`` (the wrapper/session-start signal) and
``ensure_agy_permission_rules`` (consent-gated merge writer).

Run with: python3 -m pytest test/test_agy_permission_rules.py -v
"""
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO / "plugin" / "hooks"
for _p in (str(_REPO), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import shared_state  # noqa: E402

SETTINGS_REL = Path(".gemini") / "antigravity-cli" / "settings.json"


def _load_gate():
    """Load the hyphenated gate template so its allowlist can be cross-checked."""
    path = _REPO / "plugin" / "templates" / "agy-readonly-gate.py"
    spec = importlib.util.spec_from_file_location("agy_readonly_gate_for_rules", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PermissionRulesWriter(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.home, ignore_errors=True))
        self.settings = self.home / SETTINGS_REL

    def _read(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_creates_file_with_canonical_rules(self):
        self.assertTrue(shared_state.ensure_agy_permission_rules(self.home))
        allow = self._read()["permissions"]["allow"]
        for rule in shared_state._AGY_PERMISSION_RULES:
            self.assertIn(rule, allow)

    def test_present_signal_tracks_the_write(self):
        self.assertFalse(shared_state.agy_permission_rules_present(self.home))
        shared_state.ensure_agy_permission_rules(self.home)
        self.assertTrue(shared_state.agy_permission_rules_present(self.home))

    def test_preserves_unrelated_keys_and_user_rules(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({
            "editorMode": "vim",
            "permissions": {"allow": ["command(npm test)"], "deny": ["command(rm)"]},
        }), encoding="utf-8")
        shared_state.ensure_agy_permission_rules(self.home)
        data = self._read()
        self.assertEqual(data["editorMode"], "vim")
        self.assertEqual(data["permissions"]["deny"], ["command(rm)"])
        self.assertIn("command(npm test)", data["permissions"]["allow"])
        self.assertIn("command(git status)", data["permissions"]["allow"])

    def test_git_is_never_granted_as_a_bare_binary(self):
        """`command(git)` prefix-matches `git push --force` / `git reset --hard`.

        This file is GLOBAL: in the user's other repositories the rule applies and
        no deny-gate is deployed to refuse those, so the grant has to be defensible
        without assuming containment (code-review W1)."""
        shared_state.ensure_agy_permission_rules(self.home)
        allow = self._read()["permissions"]["allow"]
        self.assertNotIn("command(git)", allow)
        for destructive in ("push", "reset", "commit", "checkout", "clean", "rebase"):
            self.assertNotIn(f"command(git {destructive})", allow)
        self.assertIn("command(git status)", allow)

    def test_never_removes_a_rule_it_did_not_add(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["command(docker)", "command(make)"]},
        }), encoding="utf-8")
        shared_state.ensure_agy_permission_rules(self.home)
        allow = self._read()["permissions"]["allow"]
        self.assertIn("command(docker)", allow)
        self.assertIn("command(make)", allow)

    def test_idempotent_second_call_is_a_noop(self):
        self.assertTrue(shared_state.ensure_agy_permission_rules(self.home))
        before = self.settings.read_text(encoding="utf-8")
        self.assertFalse(shared_state.ensure_agy_permission_rules(self.home))
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)

    def test_no_duplicate_rules_on_rerun(self):
        shared_state.ensure_agy_permission_rules(self.home)
        shared_state.ensure_agy_permission_rules(self.home)
        allow = self._read()["permissions"]["allow"]
        self.assertEqual(len(allow), len(set(allow)))

    def test_malformed_settings_left_untouched(self):
        """A hand-broken settings.json must not be clobbered — agy hangs on bad rules."""
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text("{not json at all", encoding="utf-8")
        self.assertFalse(shared_state.ensure_agy_permission_rules(self.home))
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json at all")

    def test_non_dict_settings_left_untouched(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertFalse(shared_state.ensure_agy_permission_rules(self.home))
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_non_dict_permissions_block_left_untouched(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({"permissions": "everything"}), encoding="utf-8")
        self.assertFalse(shared_state.ensure_agy_permission_rules(self.home))
        self.assertEqual(self._read()["permissions"], "everything")

    def test_present_is_false_on_partial_rules(self):
        """A user with only a couple of hand-written rules is NOT configured."""
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["command(git status)", "command(cat)"]},
        }), encoding="utf-8")
        self.assertFalse(shared_state.agy_permission_rules_present(self.home))

    def test_present_is_false_on_malformed_file(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text("{broken", encoding="utf-8")
        self.assertFalse(shared_state.agy_permission_rules_present(self.home))


class RulesMirrorGate(unittest.TestCase):
    """The two layers must agree, or agy silently loses commands the gate allows.

    agy's permission check runs BEFORE the gate hook: a command with no allow rule
    is auto-denied and the gate never sees it. So every base command the gate is
    willing to allow needs a rule whose first token matches, or the gate's allowlist
    is quietly narrower in practice than it reads on the page.

    SCOPE, stated honestly: this checks BASE commands only. Rules are compared on
    their first token, so the 16 `command(git <sub>)` entries all collapse to `git`
    and a gate that gained a new git SUBcommand with no matching rule would not trip
    this guard. That is deliberate — `git remote` and `git config` are allowed by the
    gate and intentionally NOT granted (their mutating forms rewrite .git/config, and
    the rules file is global) — but it means this guard is weaker than a reader might
    assume from its name (spec-audit round 3).
    """

    def test_rules_mirror_gate_base_commands(self):
        gate = _load_gate()
        # The gate now keeps its base commands in an explicit frozenset (the
        # prefix-regex it replaced is what let `find . "-delete"` slip past a guard),
        # so the mirror is a direct set comparison — no regex archaeology.
        bases = set(gate._READONLY_COMMANDS)
        bases.add("git")   # any command(git <sub>) rule satisfies the base
        granted = {r[len("command("):-1].split()[0]
                   for r in shared_state._AGY_PERMISSION_RULES}
        missing = bases - granted
        self.assertEqual(missing, set(),
                         f"gate allows {sorted(missing)} but no permission rule grants them")

    def test_rules_mirror_gate_git_subcommands(self):
        """Close the gap the base-command mirror deliberately leaves open.

        The test above collapses every `command(git <sub>)` rule to `git`, so a gate
        that gained a new git SUBcommand with no matching rule passed it. That is the
        likelier drift of the two: the gate's subcommand set is where a reviewer adds
        a read command. Compare the sets directly, with the two deliberate omissions
        named so removing a grant cannot masquerade as intent.
        """
        gate = _load_gate()
        ungranted_by_design = {"remote", "config"}
        gate_subs = set(gate._READONLY_GIT_SUBCOMMANDS)
        granted_subs = {r[len("command(git "):-1]
                        for r in shared_state._AGY_PERMISSION_RULES
                        if r.startswith("command(git ")}
        self.assertEqual(
            gate_subs - ungranted_by_design, granted_subs,
            "gate git subcommands and command(git <sub>) rules have drifted; "
            "a gate-allowed subcommand with no rule is auto-denied before the gate runs")
        self.assertTrue(
            ungranted_by_design.isdisjoint(granted_subs),
            "git remote / git config must stay ungranted — their mutating forms "
            "rewrite .git/config and the rules file is global")

    def test_wrapper_canon_literal_matches_the_deployed_hooks_entry(self):
        """The wrapper's preflight 1 deep-equals a HARDCODED copy of the hooks.json
        entry against what is actually deployed. The two are stated to be
        byte-identical in prose and nothing enforced it.

        Failure mode is silent and total: change `timeout` in `_agy_gate_hooks_entry`
        and every correctly deployed gate stops matching the wrapper's `CANON`, so the
        preflight refuses to run agy — `read-only gate not deployed or altered` — on a
        perfectly good install, and agy contributes nothing to every review from then
        on. Demonstrated by a reviewer: patching that one value left the whole suite
        green (round 7, W2). Round 5 closed exactly this class for the permission-rule
        literal and left this one open.
        """
        import ast
        wrapper = (_REPO / "plugin" / "agents" / "agy-cli.md").read_text(encoding="utf-8")
        lines = [ln for ln in wrapper.splitlines() if ln.strip().startswith("CANON = ")]
        self.assertEqual(len(lines), 1,
                         "expected exactly one CANON literal in the wrapper preflight")
        canon = ast.literal_eval(lines[0].split("=", 1)[1].strip())
        self.assertEqual(canon, shared_state._agy_gate_hooks_entry(),
                         "agy-cli.md's CANON has drifted from _agy_gate_hooks_entry(); "
                         "the preflight would refuse a correctly deployed gate")

    def test_wrapper_preflight_hook_name_matches_shared_state(self):
        """The preflight compares TWO hardcoded literals against shared_state, and the
        round-7 guard covered only one of them.

        The other is the hook NAME in `d.get("team-management-readonly-gate")`. Rename
        `_AGY_GATE_HOOK_NAME` and the deploy writes the new key while the wrapper still
        looks up the old one, so `d.get(...)` returns `None != CANON` and every
        correctly deployed gate fails preflight — agy silently contributes nothing to
        every review. Mutation-verified by a reviewer: renaming it left the full suite
        green (round 8, W2). Verbatim the failure the round-7 guard existed to prevent,
        one literal over.
        """
        import re as _re
        wrapper = (_REPO / "plugin" / "agents" / "agy-cli.md").read_text(encoding="utf-8")
        # Anchor on the CANON comparison: the wrapper has a second `d.get("permissions")`
        # in preflight 2, so a bare `d.get(...)` scan matches the wrong literal too.
        compare_lines = [ln for ln in wrapper.splitlines() if "== CANON" in ln]
        self.assertEqual(len(compare_lines), 1,
                         f"expected exactly one CANON comparison, found {compare_lines}")
        names = _re.findall(r'd\.get\(\s*"([^"]+)"\s*\)', compare_lines[0])
        self.assertEqual(len(names), 1,
                         f"expected exactly one hook-name lookup in the preflight, found {names}")
        self.assertEqual(names[0], shared_state._AGY_GATE_HOOK_NAME,
                         "agy-cli.md's preflight looks up a hook name that no longer "
                         "matches _AGY_GATE_HOOK_NAME; every deployed gate would fail it")

    def test_wrapper_preflight_probe_matches_a_real_rule(self):
        """The wrapper's preflight greps the settings file for ONE literal rule.

        That literal is hardcoded in agy-cli.md while its source of truth is
        `_AGY_PERMISSION_RULES`. Rename or drop that rule and the preflight fails
        closed on a CORRECTLY configured machine — agy never runs, and no test
        noticed (test-quality review, round 5).
        """
        import re as _re
        wrapper = (_REPO / "plugin" / "agents" / "agy-cli.md").read_text(encoding="utf-8")
        # Anchored on the preflight LINE, not the document. Searching the whole markdown
        # made this pass on PROSE: Section 1 lists `command(git status)` as an example,
        # so mutating the actual probe to `command(git st4tus)` kept the test green
        # while preflight 2 would fail on every correctly configured machine and agy
        # would silently contribute nothing to every review (round 9, W1). Third time a
        # hardcoded preflight literal turned out to be unguarded (rounds 7, 8, now 9).
        probe_lines = [ln for ln in wrapper.splitlines() if "in allow else 1" in ln]
        self.assertEqual(len(probe_lines), 1,
                         f"expected exactly one preflight-2 probe line, found {probe_lines}")
        probes = _re.findall(r'"([^"]+)"\s+in allow', probe_lines[0])
        self.assertEqual(len(probes), 1,
                         f"expected exactly one probed rule literal, found {probes}")
        self.assertIn(
            probes[0], shared_state._AGY_PERMISSION_RULES,
            "agy-cli.md preflight 2 probes for a permission rule that no longer exists "
            "in _AGY_PERMISSION_RULES; it would fail on a correctly configured machine")

    def test_rules_are_command_scoped_and_never_wildcard(self):
        """`command(*)` would re-create the skip-permissions blast radius."""
        for rule in shared_state._AGY_PERMISSION_RULES:
            self.assertTrue(rule.startswith("command(") and rule.endswith(")"), rule)
            self.assertNotIn("*", rule)


class ConsentBoundary(unittest.TestCase):
    """Writing to the user's GLOBAL agy config is consent-gated, not automatic."""

    def test_session_start_does_not_write_rules(self):
        src = (_REPO / "plugin" / "hooks" / "session-start.py").read_text(encoding="utf-8")
        self.assertNotIn("ensure_agy_permission_rules(", src,
                         "session-start must NEVER write the user's global agy settings")
        self.assertIn("agy_permission_rules_present", src,
                      "session-start should still NOTICE the missing rules")

    def test_config_update_writes_rules_on_consent(self):
        src = (_REPO / "plugin" / "mcp" / "tools" / "config.py").read_text(encoding="utf-8")
        self.assertIn("ensure_agy_permission_rules", src,
                      "the config flow is the only sanctioned writer")


class ShouldWriteAgyRulesGate(unittest.TestCase):
    """Behavioural coverage for the two-disjunct consent gate in config.py.

    Previously this decision had only a source-grep: deleting either half kept the
    suite green, and a real end-to-end test was impossible because the writer's
    `home` defaults to the developer's actual home (test-quality review, round 5).
    Extracting the predicate makes both halves assertable without touching ~.
    """

    @staticmethod
    def _gate():
        # config.py imports its siblings as `core.*`, so `plugin/mcp` must be on the
        # path — the same setup test_config_mcp.py uses. Loading the file directly
        # via importlib fails with ModuleNotFoundError: core.
        mcp_dir = str(_REPO / "plugin" / "mcp")
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)
        from tools import config as config_tool
        return config_tool._should_write_agy_rules

    def test_fires_when_the_call_enables_agy(self):
        self.assertTrue(self._gate()({"agy.enabled": True}, rules_present=True))

    def test_fires_when_the_call_touches_ai_providers(self):
        self.assertTrue(self._gate()(
            {"ai_providers.enabled_providers": ["agy"]}, rules_present=True))

    def test_fires_when_rules_are_missing_even_on_an_unrelated_edit(self):
        """The SessionStart notice names this command as the fix; a user whose only
        edit is elsewhere must still get the rules, or the notice never clears."""
        self.assertTrue(self._gate()({"developer_name": "max"}, rules_present=False))

    def test_silent_on_an_unrelated_edit_once_rules_exist(self):
        """A write to another tool's global config must not happen on every later
        config edit, long after the consent disclosure was shown."""
        self.assertFalse(self._gate()({"developer_name": "max"}, rules_present=True))

    def test_silent_on_an_empty_update_once_rules_exist(self):
        self.assertFalse(self._gate()({}, rules_present=True))

    def test_prefix_match_does_not_fire_on_a_lookalike_key(self):
        self.assertFalse(self._gate()(
            {"ai_providers_legacy.foo": 1, "agy.enabled_at": 2}, rules_present=True))



# --- SessionStart notice (module-level so the node id matches the criterion) ---
# These run session-start.py as a SUBPROCESS with HOME redirected (Path.home()
# honours $HOME), matching test_session_start_task_template.py's harness.

_PLUGIN_DIR = _REPO / "plugin"
_SESSION_START = _PLUGIN_DIR / "hooks" / "session-start.py"


def _agy_project(tmp_path):
    """A plugin-mode project with agy enabled (the dual gate the resolver uses)."""
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    tm = tmp_path / "team-management"
    tm.mkdir()
    (tm / "config.json").write_text(json.dumps({
        "developer_name": "tester",
        "ai_providers": {"enabled_providers": ["agy"]},
        "agy": {"enabled": True},
    }), encoding="utf-8")
    return tmp_path


def _run_session_start(project, home):
    import os
    import subprocess
    env = {**os.environ,
           "CLAUDE_PROJECT_DIR": str(project),
           "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_DIR),
           # Path.home() reads HOME on POSIX but USERPROFILE on Windows
           # (ntpath.expanduser ignores HOME), so redirecting only HOME let a
           # Windows run read the developer's REAL ~/.gemini settings and fail
           # deterministically. Set both (test-quality review, round 5).
           "HOME": str(home),
           "USERPROFILE": str(home)}
    # session-start writes the update-notice marker under CLAUDE_PLUGIN_DATA when it
    # is set; inheriting the developer's value clobbers their real marker and pulls
    # the update banner into this subprocess's output.
    env.pop("CLAUDE_PLUGIN_DATA", None)
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    proc = subprocess.run([sys.executable, str(_SESSION_START)], input=payload,
                          capture_output=True, text=True, cwd=str(project),
                          env=env, timeout=30)
    # Assert on the DECODED context, not raw stdout: json.dumps escapes non-ASCII
    # (the notice's em dash becomes \u2014), so a substring check against stdout
    # silently fails on a notice that is in fact present.
    try:
        emitted = json.loads(proc.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")
    except (ValueError, AttributeError):
        emitted = proc.stdout
    return proc, emitted


def test_session_start_notice_on_missing_rules(tmp_path):
    """agy enabled + rules absent is a live misconfiguration that fails SILENTLY
    at review time (agy answers with nothing and it reads like a clean review),
    so session start must say so and name the fix."""
    project = _agy_project(tmp_path / "proj")
    home = tmp_path / "home"
    home.mkdir()

    missing, context = _run_session_start(project, home)
    assert missing.returncode == 0, missing.stderr
    assert "agy — setup required" in context
    assert "/team-management:config" in context
    # Hybrid delivery: additionalContext is model-only with no delivery guarantee,
    # and the action item is the USER's, so it must also ride the one channel
    # Claude Code shows them directly (code-review W3).
    banner = json.loads(missing.stdout).get("systemMessage", "")
    assert "agy is enabled but its permission rules are missing" in banner
    assert "/team-management:config" in banner

    # Once configured, the notice must disappear — a permanent nag trains the
    # reader to ignore it, which is how the next real warning gets missed.
    shared_state.ensure_agy_permission_rules(home)
    configured, context_after = _run_session_start(project, home)
    assert configured.returncode == 0, configured.stderr
    assert "agy — setup required" not in context_after
    assert "permission rules are missing" not in json.loads(configured.stdout).get("systemMessage", "")


def test_session_start_silent_when_agy_disabled(tmp_path):
    """No notice for projects that do not use agy at all."""
    project = tmp_path / "proj"
    (project / ".claude" / "state").mkdir(parents=True)
    tm = project / "team-management"
    tm.mkdir()
    (tm / "config.json").write_text(json.dumps({
        "developer_name": "tester",
        "ai_providers": {"enabled_providers": ["codex"]},
        "agy": {"enabled": False},
    }), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    result, context = _run_session_start(project, home)
    assert result.returncode == 0, result.stderr
    assert "agy — setup required" not in context


# NOTE placement: `unittest.main()` must stay at the BOTTOM of the file. It used to sit
# above the module-level pytest tests below, so `python3 test/test_agy_permission_rules.py`
# never even defined them and reported a clean run over a subset (test-quality review,
# round 5). Only pytest collected the SessionStart coverage.
if __name__ == "__main__":
    unittest.main()
