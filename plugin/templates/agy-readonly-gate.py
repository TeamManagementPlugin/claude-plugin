#!/usr/bin/env python3
"""team-management read-only gate for the Antigravity CLI (`agy`).

Deployed by team-management into a project's `.agents/hooks.json` as a
`PreToolUse` hook (named `team-management-readonly-gate`). It CONTAINS the
`agy --dangerously-skip-permissions` run that the `agy-cli` wrapper uses for
headless code review: skip-permissions makes agy actually execute tools
(headless print mode otherwise soft-denies every command), and THIS gate is
the security boundary — it hard-`deny`s every tool call outside a read-only
allowlist, so agy can inspect the repo but cannot mutate it or reach the
network.

Contract (agy lifecycle hooks): read a JSON tool-call payload on stdin, write
`{"decision": "allow"|"deny", "reason": "..."}` on stdout. A `deny` decision
hard-blocks the tool before it runs. Fail CLOSED: any malformed input or
unexpected tool → `deny`.

Load-bearing agy-runtime assumptions this containment relies on (verified live in
m-fix-agy-headless-review; a future agy version could change them):
  - agy runs this PreToolUse hook with cwd = the `.agents/` dir, so the relative
    `python3 agy-readonly-gate.py` command in hooks.json resolves;
  - a hook `deny` OVERRIDES any `allow` (from a user's own named PreToolUse hook);
  - agy fails CLOSED if the hook command errors (missing python3, non-zero exit).
The wrapper's read-only guarantee = this gate (prevention) + the wrapper's
before/after git-status mutation check (detection); neither alone is sufficient
(the mutation check does not see network egress or out-of-repo writes).

Stdlib only. Do NOT edit the deployed copy — it is refreshed by
team-management on change; customize the allowlist in the plugin source
(`plugin/templates/agy-readonly-gate.py`).
"""
import json
import re
import sys

# Read-only shell commands agy may run during a review. Anchored at the start of
# the command; a trailing `\b` keeps `git status` from also matching `git stashx`.
# Deliberately tight: only genuinely read-only tools. Pipe-only filters
# (sort/uniq/cut/tr/column) are omitted — they need a `|` to be useful and `|` is
# a denied metachar, so a standalone one is pointless — and `sort -o` writes.
_READONLY_CMD_RE = re.compile(
    r"^\s*("
    r"git\s+(status|diff|log|show|rev-parse|ls-files|blame|cat-file|describe|shortlog)"
    r"|ls|cat|head|tail|wc|grep|egrep|fgrep|rg|find|pwd|file|stat|nl"
    r")\b"
)

# Any of these disqualifies a command outright — they enable chaining a second
# command, redirection, command substitution, or a subshell past the read-only
# prefix (e.g. `git diff; rm x`, `cat $(whoami)`, `git log ` + backtick).
_METACHAR_RE = re.compile(r"[;&|<>`$(){}\n\r]")

# Write/exec-enabling flags that turn an ALLOWLISTED command into a mutation or
# code-execution vector even with no shell metachar. Denied regardless of the base
# command (belt-and-suspenders over the allowlist). Covers three classes:
#   1. find write/exec action predicates — `-exec`/`-execdir`/`-ok`/`-okdir`
#      (run a command), `-delete` (unlink), `-fprint`/`-fprintf`/`-fprint0`/`-fls`
#      (write a file). NOTE `-fprint[f0]?`: a bare `-fprint\b` MISSES `-fprint0`
#      because `0` is a word char, so there is no `\b` after `fprint`.
#   2. write-to-file flags — `git diff --output=<file>`. Matched only as a complete
#      flag (`--output` + `=`/space/EOL) so it does NOT over-block a benign read
#      flag like `--output-indicator-new`.
#   3. external-command execution FLAGS — git's `--ext-diff`/`--textconv` and
#      ripgrep's `--pre`/`--pre-glob`/`--hostname-bin` force a config-defined driver
#      to run. NOTE (accepted limitation): this closes the FLAG-forced path only. git
#      also honors a `diff.external` in `.git/config` or a `.gitattributes` textconv
#      driver on a PLAIN `git diff`/`log -p`/`show` with NO flag — that driver runs
#      INSIDE git, not as a tool call agy makes, so this gate cannot intercept it.
#      Safe for the intended use (reviewing the user's OWN repo, whose `.git/config`
#      is trusted). Do NOT point agy at a repo whose `.git/config`/`.gitattributes`
#      you do not trust. (`env -i` already neutralizes the env `GIT_EXTERNAL_DIFF`.)
# `-o` is intentionally NOT here: on the allowlist it only appears as `ls -o`
# (read) and `find … -o …` (the OR operator) — the write vector is `--output`.
_DANGEROUS_FLAG_RE = re.compile(
    r"(?:^|\s)-(?:exec|execdir|delete|fprint[f0]?|fls|ok|okdir)\b"
    r"|(?:^|\s)--output(?:=|\s|$)"
    r"|(?:^|\s)--(?:ext-diff|textconv|pre|pre-glob|hostname-bin)(?:=|\b)"
)

# agy tool names (lowercased CORTEX_STEP_TYPE, prefix stripped) that only read.
_READ_TOOLS = frozenset({
    "view_file", "read_file", "list_directory", "grep_search",
    "codebase_search", "find_files", "read_files", "view_code_item",
})


def decide(payload):
    """Return "allow" or "deny" for a single agy tool-call payload.

    Fail closed: anything not positively recognised as read-only → "deny".
    """
    if not isinstance(payload, dict):
        return "deny"
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return "deny"
    name = tool_call.get("name") or ""

    if name in _READ_TOOLS:
        return "allow"

    if name == "run_command":
        args = tool_call.get("args")
        if not isinstance(args, dict):
            return "deny"
        # agy payloads use CamelCase (CommandLine); accept the lowerCamel spelling too.
        cmd = args.get("CommandLine") or args.get("commandLine") or ""
        if not isinstance(cmd, str) or not cmd.strip():
            return "deny"
        if _METACHAR_RE.search(cmd):
            return "deny"
        if _DANGEROUS_FLAG_RE.search(cmd):
            return "deny"
        if _READONLY_CMD_RE.match(cmd):
            return "allow"
        return "deny"

    # Any other tool (write_file, edit_file, replace, apply_patch, browser_*, …)
    # is not read-only.
    return "deny"


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception:
        payload = None  # fail closed
    decision = decide(payload)
    reason = "team-management read-only gate: {}".format(
        "read-only tool/command permitted" if decision == "allow"
        else "denied non-read-only tool call"
    )
    sys.stdout.write(json.dumps({"decision": decision, "reason": reason}))


if __name__ == "__main__":
    main()
