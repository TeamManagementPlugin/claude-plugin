#!/usr/bin/env python3
"""Pre-tool-use hook to enforce DAIC (Discussion, Alignment, Implementation, Check) workflow and branch consistency.

Failure model (h-fix-daic-enforcement-fail-open) — HYBRID:
Only exit code 2 blocks a PreToolUse tool call; any uncaught exception exits 1
(non-blocking) and SILENTLY disables all enforcement. To make that impossible:
  * Foreseeable corruption fails CLOSED. The state readers degrade to restrictive
    defaults (unreadable DAIC mode → 'discussion' blocks edits; unreadable bypass →
    not-bypassed; unreadable task state → null-task → branch fail-safe; unreadable
    config → DEFAULT_CONFIG keeps enforcement live), and the stdin parse degrades
    to {}.
  * Genuinely-unforeseen bugs fail OPEN, LOUDLY. The whole executable flow runs
    inside main(); the bottom try/except emits a stderr breadcrumb and sys.exit(0).
    exit 2 was REJECTED for this backstop because the matcher also covers
    Read|Grep|Bash — a blocking backstop would brick the session with no way to
    diagnose. `except Exception` (not BaseException) lets every deliberate
    sys.exit(2) verdict (token-bridge secret guard, protected/frozen-path guards)
    raise SystemExit straight through, so the fail-closed guards are NOT weakened.
"""
import json
import os
import re
import shlex
import sys
import subprocess
from pathlib import Path
from shared_state import (
    get_task_state, get_project_root, get_plugin_root, find_git_repo,
    parse_task_frontmatter, check_workflow_bypass, check_daic_mode_raw,
    get_protocol_state, in_subagent_context, is_plugin_mode,
    OPTIMIZE_STATE_FILE,
)
from boot_detector import detect_legacy_install
from hook_utils import command_is_read_only

# Load configuration from project's .claude directory
PROJECT_ROOT = get_project_root()
CONFIG_FILE = PROJECT_ROOT / "team-management" / "config.json"

# Default configuration (used if config file doesn't exist)
DEFAULT_CONFIG = {
    "blocked_tools": ["Edit", "Write", "MultiEdit", "NotebookEdit"],
    "branch_enforcement": {
        "enabled": True,
        "task_prefixes": ["implement-", "fix-", "refactor-", "migrate-", "test-", "docs-"],
        "branch_prefixes": {
            "implement-": "feature/",
            "fix-": "fix/",
            "refactor-": "feature/",
            "migrate-": "feature/",
            "test-": "feature/",
            "docs-": "feature/",
            "o-": "optimize/",
            "b-": "brainstorm/"
        }
    },
    "read_only_bash_commands": [
        "ls", "ll", "pwd", "cd", "echo", "cat", "head", "tail", "less", "more",
        "grep", "rg", "find", "which", "whereis", "type", "file", "stat",
        "du", "df", "tree", "basename", "dirname", "realpath", "readlink",
        "whoami", "env", "printenv", "date", "cal", "uptime", "ps", "top",
        "wc", "cut", "sort", "uniq", "comm", "diff", "cmp", "md5sum", "sha256sum",
        "git status", "git log", "git diff", "git show", "git branch", 
        "git remote", "git fetch", "git describe", "git rev-parse", "git blame",
        "docker ps", "docker images", "docker logs", "npm list", "npm ls",
        "pip list", "pip show", "yarn list", "curl", "wget", "jq", "awk",
        "sed -n", "tar -t", "unzip -l",
        # Windows equivalents
        "dir", "where", "findstr", "fc", "comp", "certutil -hashfile",
        "Get-ChildItem", "Get-Location", "Get-Content", "Select-String",
        "Get-Command", "Get-Process", "Get-Date", "Get-Item"
    ]
}

# Sentinel values indicating a task intentionally has no branch (e.g., research protocol)
BRANCHLESS_SENTINELS = {'none', 'null'}

def check_provider_workflow_required():
    """Check if provider workflow is required for task completion."""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # Check if any provider is enabled via issue_tracking.provider
            issue_tracking = config.get('issue_tracking', {})
            provider = issue_tracking.get('provider', 'disabled')

            return provider in ['gitlab', 'jira', 'github']
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return False

def block_manual_task_archival(tool_input):
    """Block manual task archival if provider workflow is required."""
    if tool_input.get("command", "").startswith("mv team-management/tasks/"):
        if "team-management/tasks/done/" in tool_input.get("command", ""):
            if check_provider_workflow_required():
                return True, "[TASK COMPLETION BLOCKED] Provider workflow is enabled and MANDATORY. Cannot manually archive task. Must use the task protocol's completion step (protocol_advance through completion) — it runs the provider workflow via completion_dispatch."
    return False, None

def load_config():
    """Load configuration from file or use defaults.

    Degrades to DEFAULT_CONFIG (which keeps enforcement live — blocked_tools +
    branch_enforcement) on ANY read failure: corrupt-bytes (UnicodeDecodeError ⊂
    ValueError) or valid-but-non-dict JSON that would crash a later `.get()`. A raise
    here would exit 1 from the enforcement hook (h-fix-daic-enforcement-fail-open)."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return cfg
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            pass
    return DEFAULT_CONFIG

# find_git_repo and parse_task_frontmatter are imported from shared_state at line 7


def _collapse_redundant_segments(text: str) -> str:
    """Collapse `/./` and `//` so spelling variants of a protected path (e.g.
    `.claude/./state`, `.claude//state`) cannot slip past the substring match in
    is_protected_path / check_bash_protected / the secret-bridge guard. Purely
    additive -- it can only ever produce MORE matches, never relax an existing
    block. Best-effort, NOT a full path canonicaliser: symlinks and `..` traversal
    are out of scope, and a Bash-subprocess read of any user-readable file is
    irreducible regardless (see h-fix-mcp-token-ondisk-bridge residual notes)."""
    while '/./' in text:
        text = text.replace('/./', '/')
    while '//' in text:
        text = text.replace('//', '/')
    return text


# The keychain-token bridge is a SECRET; it must stay unreadable even when workflow
# bypass disables the rest of team-management enforcement -- bypass is a WORKFLOW
# switch (DAIC / branch / protocol), not a secret-protection switch. So a tool call
# that DIRECTLY targets this one path is blocked BEFORE the bypass exit below
# (h-fix-mcp-token-ondisk-bridge, codex review P1).
_TOKEN_BRIDGE_PROTECTED = ".claude/state/provider-tokens.json"


def _path_resolves_to_token_bridge(target: str) -> bool:
    """True if a STRUCTURED-path tool arg points at the secret bridge. `os.path.normpath`
    resolves `.`, `..`, and `//`; we then match on a path-COMPONENT boundary (exact, or
    ends with `/<rel>`) so a sibling like `provider-tokens.json.bak` is NOT false-blocked
    (codex review P2). The trailing slash-normalise keeps it correct on Windows, where
    normpath emits backslashes."""
    norm = os.path.normpath(str(target).replace('\\', '/')).replace('\\', '/')
    return norm == _TOKEN_BRIDGE_PROTECTED or norm.endswith('/' + _TOKEN_BRIDGE_PROTECTED)


def _targets_token_bridge(tool_name: str, tool_input: dict) -> bool:
    """True if a tool call directly targets the secret provider-token bridge.

    Structured-path tools (Read/Grep/Edit/Write/MultiEdit/NotebookEdit) carry a single
    clean path, so they are fully normalised (`.`/`..`/`//` resolved) and matched on a
    component boundary. Bash is free-form: we catch a CONTIGUOUS literal of the bridge
    path (with `/./` `//` collapsed), but split forms (`cd .claude/state && cat
    provider-tokens.json`), `..` traversal mid-command, shell variables, command
    substitution, and `python -c` are the IRREDUCIBLE shell-subprocess residual --
    not blockable by static inspection (see the task `## Notes — Residuals`). The real
    protections are 0600 + gitignore + token scoping/rotation."""
    if not isinstance(tool_input, dict):
        return False
    if tool_name in ("Read", "Grep", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        target = (tool_input.get("file_path") or tool_input.get("path")
                  or tool_input.get("notebook_path") or "")
        return _path_resolves_to_token_bridge(target)
    if tool_name == "Bash":
        cmd = _collapse_redundant_segments(str(tool_input.get("command", "")).replace('\\', '/'))
        return _TOKEN_BRIDGE_PROTECTED in cmd
    return False



# ============================================================================
# PROTECTED PATH ENFORCEMENT (before subagent bypass and all other checks)
# ============================================================================
# State files and protocol configs are protected from direct AI access.
# Only MCP tools and hooks can read/write these paths.
# This runs BEFORE subagent bypass — subagents are also blocked.

PROTECTED_PATHS = [
    # SEC-006: protect the WHOLE .claude/state prefix so the LLM cannot author
    # state files directly (e.g. a future config-session.flag — self-authorising
    # the config intent-gate). The specific entries below are now subsumed by the
    # prefix but kept explicit for clarity / existing-test stability.
    ".claude/state",
    ".claude/state/current_task.json",
    ".claude/state/daic-mode.json",
    ".claude/state/protocol-logs",
    "team-management/protocol-configs/system",
    ".claude/state/optimize-state.json",
    # Holds resolved provider API tokens (secrets) bridged from the OS keychain for
    # the MCP server. Explicit so the secret file is a named, tested block, though the
    # .claude/state prefix above already subsumes it (h-fix-mcp-token-ondisk-bridge).
    ".claude/state/provider-tokens.json",
]

# Bash command patterns that indicate write semantics. Hoisted from the
# inline definition that previously lived inside the `tool_name == "Bash"`
# block (used by the read-only-fast-exit check, the documentation-mode
# block, and now the optimize frozen-path block).
BASH_WRITE_PATTERNS = [
    r'>\s*[^>]',          # Output redirection
    r'>>',                # Append redirection
    r'\btee\b',           # tee command
    r'\bmv\b',            # move/rename
    r'\bcp\b',            # copy
    r'\brm\b',            # remove
    r'\bmkdir\b',         # make directory
    r'\btouch\b',         # create/update file
    r'\bsed\s+(?!-n)',    # sed without -n flag
    r'\bnpm\s+install',
    r'\bpip\s+install',
    r'\bapt\s+install',
    r'\byum\s+install',
    r'\bbrew\s+install',
]


def is_protected_path(path_str: str) -> bool:
    """Check if a path targets a protected location."""
    if not path_str:
        return False
    normalized = _collapse_redundant_segments(path_str.replace('\\', '/'))
    if any(p in normalized for p in PROTECTED_PATHS):
        return True
    # Plugin-install system protocol-configs are read-only — users fork them to
    # custom/ rather than edit in place. Protect them ONLY when the plugin lives
    # OUTSIDE the project (a real plugin install). In a dev checkout PLUGIN_ROOT is
    # under the project and editing those configs IS the work, so it stays allowed.
    try:
        plugin_root = get_plugin_root().resolve()
        if not plugin_root.is_relative_to(PROJECT_ROOT.resolve()):
            target = Path(path_str)
            if target.is_absolute():
                sys_cfg = plugin_root / 'protocol-configs'
                if target == sys_cfg or target.is_relative_to(sys_cfg):
                    return True
    except (ValueError, OSError):
        pass
    return False

def check_bash_protected(command: str) -> bool:
    """Check if a Bash command accesses protected paths."""
    normalized = _collapse_redundant_segments(command.replace('\\', '/'))
    return any(p in normalized for p in PROTECTED_PATHS)


def _load_frozen_paths() -> list:
    """Read frozen paths from optimize-state.json plus runtime TSV additions.

    Fast path 1: returns [] when the optimize-state.json file is absent — non-
    optimize users incur near-zero overhead (one stat() per protected-write
    tool call).

    Fast path 2: returns [] when the active protocol step is not
    `experimentation`. Aligns runtime enforcement with the documented
    contract in optimize-setup.md ("frozen_paths protect *during
    experimentation*"). Earlier steps (metric-script, baseline) need to
    author/run the metric script itself, which may live inside a frozen
    directory; later steps (synthesis, code-review, completion) either run
    in documentation mode or do no code edits. If no protocol is active
    (a stale optimize-state.json from a previous run, for example) the
    gate also short-circuits — abandoned state must not block unrelated
    subsequent work.

    Runtime additions: any *.tsv files under team-management/tasks/<current_task>/.
    These are the optimization run's results files; the engine is the only
    legitimate writer.
    """
    if not OPTIMIZE_STATE_FILE.exists():
        return []
    protocol_state = get_protocol_state()
    if not protocol_state or protocol_state.get("step_name") != "experimentation":
        return []
    try:
        with open(OPTIMIZE_STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        return []
    paths = list(state.get("frozen_paths", []))
    # Defensive try/except: this helper runs on the hot path and must never
    # raise; if anything goes wrong we silently degrade to declared paths only.
    try:
        task_name = get_task_state().get("task")
        if task_name:
            task_dir = PROJECT_ROOT / "team-management" / "tasks" / task_name
            if task_dir.is_dir():
                for tsv in task_dir.rglob("*.tsv"):
                    try:
                        paths.append(str(tsv.relative_to(PROJECT_ROOT)))
                    except ValueError:
                        paths.append(str(tsv))
    except Exception:
        pass
    return paths


def _normalize_for_match(path_str: str) -> str:
    """Normalise a path for frozen-path comparison.

    Rules (applied in order):
      1. `\\` → `/` and trailing slash stripped.
      2. If absolute under `PROJECT_ROOT`, strip the project-root prefix.
      3. Leading `./` stripped.
      4. Single leading `/` (after the project-root strip) stripped — a user
         who writes `frozen_paths: ["/src/foo.py"]` means project-relative
         `src/foo.py`. System-absolute frozen entries (`/etc/passwd`) outside
         the project tree are not a supported use case for optimize tasks.

    Returns a project-relative path string, or "" for empty input. Critical:
    every variant the user might plausibly write into `frozen_paths` (`src/foo.py`,
    `./src/foo.py`, `/src/foo.py`, an absolute path under PROJECT_ROOT) collapses
    to the same canonical form so `_path_matches_frozen` cannot silently
    bypass enforcement on a normalisation mismatch.
    """
    if not path_str:
        return ""
    s = path_str.replace('\\', '/').rstrip('/')
    project_str = str(PROJECT_ROOT).replace('\\', '/').rstrip('/')
    if project_str and (s == project_str or s.startswith(project_str + '/')):
        s = s[len(project_str):]
    if s.startswith('./'):
        s = s[2:]
    if s.startswith('/'):
        s = s.lstrip('/')
    return s


def _path_matches_frozen(target: str, frozen: str) -> bool:
    """Path-component-boundary match: target equals frozen, or target lives
    underneath frozen treated as a directory. Excludes mere substring overlaps
    such as `src/foo.py.bak` matching `src/foo.py`, or `tests/src/foo.py`
    matching `src/foo.py` (frozen entries are anchored to project-relative
    paths, not arbitrary path-tail occurrences)."""
    t = _normalize_for_match(target)
    f = _normalize_for_match(frozen)
    if not f or not t:
        return False
    return t == f or t.startswith(f + '/')


def is_frozen_path(path_str: str) -> bool:
    """Path-component-boundary match against frozen paths declared in
    optimize-state.json plus runtime-discovered TSVs. See _path_matches_frozen."""
    if not path_str:
        return False
    return any(_path_matches_frozen(path_str, fp) for fp in _load_frozen_paths())


# Per-command write-target semantics for the Bash heuristic. Maps a basename
# (extracted from token[0] of a writing segment) to one of:
#   "all"       — every positional arg is touched (rename / delete / create)
#   "last"      — only the last positional is the destination (cp SRC... DEST)
#   "if_-i"     — only when -i / --in-place flag is present (sed in-place edit)
# Commands not in the table are not analysed for positional write targets;
# only their redirect targets (if any) participate.
_BASH_WRITE_TARGET_RULES = {
    'mv':    'all',
    'rm':    'all',
    'rmdir': 'all',
    'mkdir': 'all',
    'touch': 'all',
    'tee':   'all',     # tee FILE...; -a flag is filtered out by positional logic
    'cp':    'last',
    'sed':   'if_-i',
}


def _bash_targets_frozen(command: str, frozen: list) -> str:
    """Heuristic check: does this Bash command WRITE to a frozen path?

    Strategy:
      1. Split the command on shell pipeline / sequence separators
         (`|`, `;`, `&&`, `||`) so each segment is assessed in isolation.
      2. Skip read-only segments (no BASH_WRITE_PATTERNS match).
      3. For each writing segment, collect candidate write targets from two
         sources:
           a. **Redirect targets** — every `>` / `>>` (with an optional fd
              prefix like `2>`), MINUS fd duplications (`&1`, `&2`) and
              `/dev/null`. Iterating EVERY redirect (not just the last)
              closes the `echo x > frozen.py 2>&1` bypass — the trailing
              fd-redirect would otherwise hide the real write target.
           b. **Per-command positional args** — `cp` writes only its LAST
              positional (sources are reads), `mv`/`rm`/`mkdir`/`touch`/
              `tee` touch every positional, `sed -i FILE` writes FILE.
      4. Apply path-component-boundary match (`_path_matches_frozen`) per
         candidate token against each frozen entry.

    Avoids the original substring-on-full-command false positives:
        `grep src/frozen.py notes.txt > /tmp/out`
            → frozen on read side of redirect; redirect target is /tmp/out
            → no block.
        `cat src/frozen.py | tee report.txt`
            → frozen in segment 1 (no write pattern); segment 2 has tee
              with positional report.txt
            → no block.
        `cp src/frozen.py backup.py`
            → cp last-positional-only rule: dest is backup.py
            → no block.
    But still catches the genuine writes:
        `echo x > src/frozen.py`              → block
        `echo x > src/frozen.py 2>&1`         → block (fd redirect filtered)
        `cp other.py src/frozen.py`           → block (last positional)
        `mv src/frozen.py /tmp/`              → block (mv touches every arg)
        `rm src/frozen.py`                    → block

    Known limitations (best-effort heuristic, per brainstorm scope):
      * Indirected paths via shell variables (`TARGET=src/frozen.py; echo x > $TARGET`)
        are not resolved — the literal `$TARGET` token is checked, so the
        write slips through.
      * Command substitution (`echo x > $(date +%F)-frozen.py`) is not
        evaluated.
      * Embedded interpreter strings (`python -c "open('src/frozen.py','w')..."`,
        `bash -c "..."` without a literal redirect that names the path)
        cannot be inspected — the heuristic sees only the outer command.
      * Filenames whose first character is `-` (e.g. a frozen entry literally
        named `-output.tsv`) are skipped because dash-prefixed tokens are
        treated as option flags. Path-component matcher cannot disambiguate
        flag vs filename without a per-command parser; documented MVP gap.
      * Command prefixes such as `sudo`, `time`, or absolute paths
        (`/usr/bin/cp`) are normalised by basename-extraction; `sudo cp`
        is not (the first token is `sudo`, not in the rule table).

    Returns the matched frozen entry on a hit, "" on no hit. Caller decides
    how to format the block message.
    """
    if not frozen:
        return ""
    segments = re.split(r'\s*(?:\|\|?|&&|;)\s*', command)
    for seg in segments:
        seg = seg.strip()
        if not seg or not any(re.search(p, seg) for p in BASH_WRITE_PATTERNS):
            continue

        candidates = []

        # 1. Redirect targets — iterate every redirect, skip fd duplications
        #    and /dev/null. Captures `>FILE`, `>>FILE`, `2>FILE`, including
        #    quoted FILE forms with internal whitespace
        #    (`> "reports/frozen copy.tsv"`).
        for m in re.finditer(r'(?:\d+)?>>?\s*("[^"]*"|\'[^\']*\'|\S+)', seg):
            tgt = m.group(1)
            # Strip a single layer of matched surrounding quotes; without this,
            # `echo x > "src/frozen.py"` silently bypasses _path_matches_frozen
            # because the literal " characters defeat path-component matching.
            if len(tgt) >= 2 and tgt[0] == tgt[-1] and tgt[0] in ('"', "'"):
                tgt = tgt[1:-1]
            if tgt.startswith('&') or tgt == '/dev/null':
                continue
            candidates.append(tgt)

        # 2. Per-command positional analysis. Strip both write- and read-
        #    redirect clauses so positional tokenisation sees only the
        #    command + its real positional args.
        cmd_part = re.sub(r'(?:\d+)?[<>]>?\s*\S+\s*', '', seg).strip()
        try:
            tokens = shlex.split(cmd_part)
        except ValueError:
            # Mismatched quotes etc. — fall back to whitespace split.
            tokens = cmd_part.split()
        # Skip leading env-var assignments (FOO=bar cmd args ...).
        i = 0
        while i < len(tokens) and '=' in tokens[i] and not tokens[i].startswith('-'):
            i += 1
        if i < len(tokens):
            cmd_basename = Path(tokens[i].lstrip('\\')).name.lower()
            rest = tokens[i + 1:]
            rule = _BASH_WRITE_TARGET_RULES.get(cmd_basename)
            if rule is not None:
                # `-t DIR` / `--target-directory[=DIR]`: GNU coreutils form
                # where the destination is encoded in an option value rather
                # than the last positional. Detected for both `cp` and `mv`
                # so the destination is not silently bypassed.
                target_dir = None
                positional = []
                skip_next = False
                for j, tok in enumerate(rest):
                    if skip_next:
                        skip_next = False
                        continue
                    # NOTE on `cp -t -- DIR src`: real GNU cp greedily consumes
                    # `--` as the value of `-t`, so target=`--`, sources=[DIR,
                    # src]. Our extractor follows the same semantics — we do
                    # NOT special-case `--` here. A frozen-path DIR appearing
                    # after a `-t --` sequence is a SOURCE in real shell, not a
                    # destination, so not blocking is correct. (Best-effort
                    # heuristic; documented in module docstring.)
                    if tok == '-t' and j + 1 < len(rest):
                        target_dir = rest[j + 1]
                        skip_next = True
                    elif tok.startswith('--target-directory='):
                        target_dir = tok.split('=', 1)[1]
                    elif tok == '--target-directory' and j + 1 < len(rest):
                        target_dir = rest[j + 1]
                        skip_next = True
                    elif not tok.startswith('-'):
                        positional.append(tok)
                if rule == 'all':
                    if target_dir is not None:
                        candidates.append(target_dir)
                    candidates.extend(positional)
                elif rule == 'last':
                    if target_dir is not None:
                        # cp -t DIR src...: DIR is the only write target;
                        # sources are read-only.
                        candidates.append(target_dir)
                    elif positional:
                        candidates.append(positional[-1])
                elif rule == 'if_-i':
                    has_inplace = any(t == '-i' or t.startswith('-i')
                                      for t in rest if t.startswith('-'))
                    if has_inplace:
                        candidates.extend(positional)

        for tok in candidates:
            for fp in frozen:
                if _path_matches_frozen(tok, fp):
                    return fp
    return ""


def main():
    # Load input — guard against empty / malformed / non-dict stdin. An unguarded
    # json.load on empty stdin raises, and for THIS PreToolUse enforcement hook a
    # traceback exits 1 (non-blocking) and silently disables enforcement. An
    # unparseable payload gives no tool to identify, so degrade to {} and let the
    # normal flow fall through to the final allow (h-fix-daic-enforcement-fail-open).
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        input_data = {}
    if not isinstance(input_data, dict):
        input_data = {}
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    # Load configuration
    config = load_config()

    # ============================================================================
    # SECRET-FILE GUARD (before workflow bypass)
    # ============================================================================
    # The provider-token bridge stays blocked unconditionally -- workflow bypass
    # disables WORKFLOW enforcement (DAIC / branch / protocol), not secret protection
    # (h-fix-mcp-token-ondisk-bridge, codex review P1).
    if _targets_token_bridge(tool_name, tool_input):
        print("[Protocol Engine] Direct access to protected path blocked. Use MCP protocol tools instead.", file=sys.stderr)
        sys.exit(2)

    # ============================================================================
    # WORKFLOW BYPASS CHECK (HIGHEST PRIORITY - before all enforcement)
    # ============================================================================
    # If workflow bypass is enabled, skip ALL enforcement and allow tool to proceed.
    # This provides a way to temporarily disable team-management without uninstalling.
    if check_workflow_bypass():
        sys.exit(0)
    # Check Read and Grep for protected path access
    if tool_name in ("Read", "Grep"):
        check_path = tool_input.get("file_path", "") or tool_input.get("path", "")
        if is_protected_path(check_path):
            print("[Protocol Engine] Direct access to protected path blocked. Use MCP protocol tools instead.", file=sys.stderr)
            sys.exit(2)
        # Quick exit for non-protected Read/Grep (CRITICAL for performance)
        sys.exit(0)

    # Check Edit/Write/MultiEdit/NotebookEdit for protected path access. NotebookEdit
    # uses `notebook_path` instead of `file_path`; without including it here a
    # frozen `.ipynb` would slip through (DEFAULT_CONFIG.blocked_tools already
    # treats NotebookEdit as a write tool — keep enforcement aligned).
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        file_path_check = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if is_protected_path(file_path_check):
            print("[Protocol Engine] Direct modification of protected path blocked. Use MCP protocol tools instead.", file=sys.stderr)
            sys.exit(2)
        # Optimize protocol: block writes to user-declared frozen_paths and to
        # runtime-discovered TSV results files. Reads are NOT blocked here —
        # frozen training code must remain readable.
        if is_frozen_path(file_path_check):
            print(
                f"[Optimize: Frozen Path] '{file_path_check}' is in optimize "
                f"frozen_paths and cannot be modified during this iteration. "
                f"Update optimize-state.json (via the engine) to change the frozen list.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Check Bash for protected path access
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if check_bash_protected(command):
            print("[Protocol Engine] Bash command accessing protected path blocked. Use MCP protocol tools instead.", file=sys.stderr)
            sys.exit(2)
        # Optimize protocol: block Bash WRITE commands targeting frozen paths
        # (declared frozen_paths + runtime-discovered TSV results files). Read
        # operations and segments where the frozen path appears only on the
        # read side of a pipeline / redirect are intentionally allowed —
        # see _bash_targets_frozen for the matching heuristic.
        fp_hit = _bash_targets_frozen(command, _load_frozen_paths())
        if fp_hit:
            print(
                f"[Optimize: Frozen Path] Bash write command targets frozen "
                f"path '{fp_hit}'. Use the engine TSV writer for results; update "
                f"optimize-state.json to change the frozen list.",
                file=sys.stderr,
            )
            sys.exit(2)
        # Provider-workflow archival discipline: block a manual `mv … tasks/done/` when a
        # provider workflow (gitlab/jira/github) is required. Relocated here from after
        # DAIC enforcement (h-fix-subagent-bash-daic-bypass) so it runs BEFORE the subagent
        # bypass below — a guard that must bind EVERY actor (main agent AND subagents)
        # belongs with the other universal Bash guards, not after the bypass.
        archival_blocked, archival_msg = block_manual_task_archival(tool_input)
        if archival_blocked:
            print(archival_msg, file=sys.stderr)
            sys.exit(2)  # Exit code 2 blocks the operation

    # ============================================================================
    # CONFIG FILE PROTECTION
    # ============================================================================
    # config.json protocol_engine section is user-only.

    if tool_name in ("Edit", "Write", "MultiEdit"):
        file_path_cfg = tool_input.get("file_path", "")
        if file_path_cfg and "config.json" in file_path_cfg:
            content_to_check = ""
            if tool_name == "Write":
                content_to_check = tool_input.get("content", "")
            elif tool_name == "Edit":
                content_to_check = tool_input.get("old_string", "") + tool_input.get("new_string", "")
            elif tool_name == "MultiEdit":
                for edit in tool_input.get("edits", []):
                    content_to_check += edit.get("old_string", "") + edit.get("new_string", "")
            if "protocol_engine" in content_to_check:
                print("[Protocol Engine] Cannot modify protocol_engine configuration. This section is user-managed only.", file=sys.stderr)
                sys.exit(2)

    # For Bash commands, check if it's a read-only operation
    if tool_name == "Bash":
        command = tool_input.get("command", "").strip()

        has_write_pattern = any(re.search(pattern, command) for pattern in BASH_WRITE_PATTERNS)
    
        # Allow a chain of purely read-only commands without further checks.
        # command_is_read_only (hook_utils) matches each &&/||/;/|-segment on a
        # WHOLE-TOKEN boundary, so `cdk deploy`/`cd`, `catalog-cli push`/`cat`,
        # `lsof`/`ls` are no longer mis-classified as read-only, while multi-token
        # allowlist entries (`git status --short`, `sed -n '1,20p'`) still match.
        if not has_write_pattern and command_is_read_only(
            command,
            config.get("read_only_bash_commands", DEFAULT_CONFIG["read_only_bash_commands"]),
        ):
            # Allow read-only commands without checks
            sys.exit(0)

    # Check current DAIC mode from global state
    daic_mode = check_daic_mode_raw()
    discussion_mode = (daic_mode == "discussion")

    # ============================================================================
    # SUBAGENT BYPASS CHECK (MUST BE EARLY - before DAIC enforcement)
    # ============================================================================
    # Subagents operate in separate context and bypass DAIC enforcement entirely.
    # However, they are still blocked from editing .claude/state files.
    project_root = get_project_root()
    if in_subagent_context():
        # Subagents are still blocked from EDITING .claude/state files. (Bash/Read/Grep
        # access to protected paths is already blocked by the protected-path checks
        # above, which run before this bypass; this guard covers the edit tools, whose
        # target is matched as a path under .claude/state.)
        if tool_name in ["Write", "Edit", "MultiEdit", "NotebookEdit"]:
            file_path_str = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            if file_path_str:
                file_path = Path(file_path_str)
                state_dir = project_root / '.claude' / 'state'
                try:
                    # Check if file_path is under the state directory
                    file_path.resolve().relative_to(state_dir.resolve())
                    # If we get here, the file is under .claude/state
                    print(f"[Subagent Boundary Violation] Subagents are NOT allowed to modify .claude/state files.", file=sys.stderr)
                    print(f"Stay in your lane: You should only edit task-specific files, not system state.", file=sys.stderr)
                    sys.exit(2)  # Block with feedback
                except ValueError:
                    # Not under .claude/state - this is fine for subagents
                    pass

        # Allow ALL other subagent operations — Bash INCLUDED (bypass DAIC mode + branch
        # enforcement). AI-provider wrapper subagents (codex-cli / agy-cli) shell out via
        # Bash; gating this bypass on edit tools only let their Bash calls fall through to
        # the documentation-mode Bash block and be wrongly blocked in documentation-mode
        # steps — all brainstorm steps, research/exploration, refactoring/planning
        # (h-fix-subagent-bash-daic-bypass). The universal guards (protected path, frozen
        # path, manual task archival) all run EARLIER than this bypass, so broadening it
        # does NOT let a subagent reach .claude/state or frozen paths via Bash.
        sys.exit(0)

    # ============================================================================
    # BOOT DETECTOR — PreToolUse companion (plugin-mode only)
    # ============================================================================
    # When running AS A PLUGIN (CLAUDE_PLUGIN_ROOT set) alongside a legacy install
    # (residual .claude/hooks/ + settings.json hook entries / duplicate MCP name),
    # BOTH hook sets fire on every event (spike F1 = MERGE) → double DAIC / double
    # MCP → silent corruption. SessionStart can only advise (F4); this is the real
    # structural block. Gated on plugin-mode so the dual-purpose hook file stays a
    # no-op in legacy/dev. Subagents already exited at the bypass above; the explicit
    # in_subagent_context() check is belt-and-suspenders.
    if is_plugin_mode() and not in_subagent_context() and tool_name in ["Write", "Edit", "MultiEdit", "NotebookEdit"]:
        _legacy = detect_legacy_install(get_project_root())
        if _legacy:
            print(
                "[Boot Detector] A legacy team-management install was detected alongside "
                "the plugin: " + ", ".join(_legacy.get("signals", [])) + ". Both hook sets "
                "fire on every event, silently corrupting DAIC. Remove the legacy "
                ".claude/hooks/ directory and the team-management hook + mcpServers entries "
                "from .claude/settings.json (see the uninstall guide), then retry.",
                file=sys.stderr,
            )
            sys.exit(2)

    # ============================================================================
    # ADMINISTRATIVE FILE WHITELIST (MUST BE EARLY - before DAIC enforcement)
    # ============================================================================
    # These files must be editable in discussion mode to support task creation
    # and task startup protocols. This check MUST happen before DAIC enforcement.
    if tool_name in ["Write", "Edit", "MultiEdit", "NotebookEdit"]:
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if file_path:
            try:
                file_path_resolved = Path(file_path).resolve()
                project_root_resolved = get_project_root().resolve()

                # Allow ~/.claude/ (Claude Code memory, project settings) — outside project scope
                home_claude_dir = Path.home() / ".claude"
                try:
                    file_path_resolved.relative_to(home_claude_dir.resolve())
                    sys.exit(0)
                except ValueError:
                    pass

                # Note: current_task.json is now protected (managed by MCP tools only)

                # Allow task files (task creation/editing protocol)
                tasks_dir = (project_root_resolved / "team-management" / "tasks").resolve()
                try:
                    rel_path = file_path_resolved.relative_to(tasks_dir)
                    if str(rel_path).endswith(".md"):
                        sys.exit(0)
                except ValueError:
                    pass  # Not under this tasks directory

                # Allow wiki/ directory (LLM Wiki — unconditional, symlink-safe)
                wiki_dir = (project_root_resolved / "wiki").resolve()
                try:
                    file_path_resolved.relative_to(wiki_dir)
                    sys.exit(0)
                except ValueError:
                    pass

                # Allow /team-management:init's project-config targets in any DAIC
                # mode. The in-Claude-Code init command writes .claude/settings.json
                # (plugin enablement + statusLine) and the CLAUDE.tm.custom.md
                # custom-rules stub on a fresh project where the plugin is already
                # enforcing — without this, both writes are blocked (discussion mode,
                # or branch enforcement on a no-task tree). Exact resolved-path
                # equality so a `.bak` sibling or a same-named directory does NOT match.
                init_targets = {
                    (project_root_resolved / ".claude" / "settings.json").resolve(),
                    (project_root_resolved / "CLAUDE.tm.custom.md").resolve(),
                }
                if file_path_resolved in init_targets:
                    sys.exit(0)
            except Exception:
                pass  # Path resolution failed, continue to other checks

    # ============================================================================
    # DAIC MODE ENFORCEMENT (moved down from original position)
    # ============================================================================
    # Now that administrative files and subagents are whitelisted, enforce DAIC
    # mode for normal code editing operations.

    # Documentation mode: allow edits to CLAUDE.md files, task files, and docs
    if daic_mode == "documentation" and tool_name in config.get("blocked_tools", DEFAULT_CONFIG["blocked_tools"]):
        file_path_doc = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        # Anchor the tasks-root allowance to the real team-management/tasks/ tree
        # (slash-normalized for Windows) so a source file under any unrelated dir
        # named `tasks` (e.g. app/tasks/worker.py) is NOT editable in doc mode.
        # Task .md files stay covered by the .md branch; non-.md task-dir artefacts
        # (e.g. results.tsv, README under a directory-task) remain allowed here.
        _doc_norm = file_path_doc.replace('\\', '/')
        is_doc_file = (
            _doc_norm.endswith(".md") or
            "team-management/tasks/" in _doc_norm or
            "/docs/" in _doc_norm
        )
        if is_doc_file:
            sys.exit(0)  # Allow documentation edits
        else:
            print(
                "[DAIC: Documentation Mode] Only documentation files (.md, task files, docs/) "
                "can be edited. Use protocol_goto to return to implementation for code changes.",
                file=sys.stderr
            )
            sys.exit(2)

    # Documentation mode: block non-read-only Bash commands
    # Reuses the module-level BASH_WRITE_PATTERNS to avoid drift.
    if daic_mode == "documentation" and tool_name == "Bash":
        command = tool_input.get("command", "").strip()
        has_write = any(re.search(p, command) for p in BASH_WRITE_PATTERNS)
        if has_write:
            print(
                "[DAIC: Documentation Mode] Write Bash commands are blocked in documentation mode. "
                "Only read-only commands are allowed.",
                file=sys.stderr
            )
            sys.exit(2)

    # Block configured tools in discussion mode (for normal code files)
    if discussion_mode and tool_name in config.get("blocked_tools", DEFAULT_CONFIG["blocked_tools"]):
        print(f"[DAIC: Tool Blocked] You're in discussion mode. The {tool_name} tool is not available.\n\nAll implementation work must go through a workflow protocol.\nStart a protocol: mcp__plugin_team-management_tm__protocol_start(protocol_name=\"...\")\nThe protocol will manage DAIC mode, task files, and git branches automatically.\n\nDo NOT call daic_mode_switch tools directly.", file=sys.stderr)
        sys.exit(2)  # Block with feedback

    # NOTE: manual task-archival enforcement (block_manual_task_archival) was relocated
    # UP into the universal Bash-guard block (with the protected/frozen-path checks) so it
    # runs before the subagent bypass — see h-fix-subagent-bash-daic-bypass.

    # Branch enforcement for Write/Edit/MultiEdit/NotebookEdit tools (if enabled)
    branch_config = config.get("branch_enforcement", DEFAULT_CONFIG["branch_enforcement"])
    if branch_config.get("enabled", True) and tool_name in ["Write", "Edit", "MultiEdit", "NotebookEdit"]:
        # Get the file path being edited
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if file_path:
            file_path = Path(file_path)

            # Note: Administrative files (current_task.json, task files) were already
            # whitelisted earlier in the execution flow and would have exited before
            # reaching this branch enforcement section.

            # Get current task state
            task_state = get_task_state()
            current_task = task_state.get("task")
            expected_branch = task_state.get("branch")
            affected_services = task_state.get("services", [])
            branchless_task = False

            # Handle "none"/"null" already in task state branch field
            if expected_branch and expected_branch.lower() in BRANCHLESS_SENTINELS:
                branchless_task = True
                expected_branch = ""

            # If task state has no branch but task exists, read from frontmatter
            if current_task and not expected_branch and not branchless_task:
                frontmatter = parse_task_frontmatter(current_task)
                frontmatter_branch = frontmatter.get('branch', '')

                # Check if task requires a branch
                if frontmatter_branch and frontmatter_branch.lower() not in BRANCHLESS_SENTINELS:
                    expected_branch = frontmatter_branch

                    # Block with helpful error message
                    print(f"[DAIC: Branch Required] Task '{current_task}' requires branch '{expected_branch}' but task state has no branch set.", file=sys.stderr)
                    print(f"", file=sys.stderr)
                    print(f"To fix:", file=sys.stderr)
                    print(f"  1. Create and switch to the branch:", file=sys.stderr)
                    print(f"     git checkout -b {expected_branch}", file=sys.stderr)
                    print(f"", file=sys.stderr)
                    print(f"  2. Update task state with the branch:", file=sys.stderr)
                    print(f"     Update .claude/state/current_task.json to set \"branch\": \"{expected_branch}\"", file=sys.stderr)
                    sys.exit(2)
                elif frontmatter_branch.lower() in BRANCHLESS_SENTINELS:
                    branchless_task = True

            # Fail-safe: Block all other files when task state is null
            if not expected_branch and not branchless_task:
                # Get current branch for better error message
                try:
                    result = subprocess.run(
                        ["git", "branch", "--show-current"],
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False
                    )
                    current_branch = result.stdout.strip() if result.returncode == 0 else "unknown"
                except Exception:
                    current_branch = "unknown"

                print(f"[DAIC: Task State Required]", file=sys.stderr)
                print(f"", file=sys.stderr)
                print(f"Current state:", file=sys.stderr)
                print(f"  • Task: null (no active task)", file=sys.stderr)
                print(f"  • Branch: {current_branch}", file=sys.stderr)
                print(f"  • Attempting to edit: {file_path}", file=sys.stderr)
                print(f"", file=sys.stderr)
                print(f"Branch enforcement is blocking this operation to prevent accidental commits to master.", file=sys.stderr)
                print(f"", file=sys.stderr)
                print(f"To fix:", file=sys.stderr)
                print(f"  1. If creating a task: You can edit files in team-management/tasks/ (allowed)", file=sys.stderr)
                print(f"  2. If starting work on a task:", file=sys.stderr)
                print(f"     a) Start the task protocol: protocol_start(protocol_name=\"task\", task=\"<name>\")", file=sys.stderr)
                print(f"        (it creates/checks out the branch and sets task state for you)", file=sys.stderr)
                print(f"     b) Then try again", file=sys.stderr)
                sys.exit(2)  # Block the operation

            if expected_branch:
                # Find the git repo for this file. Resolve to an ABSOLUTE directory
                # first — find_git_repo walks up from its argument and stops at
                # PROJECT_ROOT, so a RELATIVE file path (walked as literal path
                # components against cwd) would return None and silently skip branch
                # enforcement. .parent because find_git_repo expects a directory.
                repo_path = find_git_repo(file_path.resolve().parent)
            
                if repo_path:
                    try:
                        # Get current branch
                        result = subprocess.run(
                            ["git", "branch", "--show-current"],
                            cwd=str(repo_path),
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        current_branch = result.stdout.strip()

                        # project_root already resolved once at module level via
                        # get_project_root() (line ~541) — no inline re-walk.

                        # Check if we're in a submodule
                        try:
                            # Try to make repo_path relative to project_root
                            repo_path.relative_to(project_root)
                            is_submodule = (repo_path != project_root)
                        except ValueError:
                            # Not a subdirectory
                            is_submodule = False
                    
                        if is_submodule:
                            # We're in a submodule
                            service_name = repo_path.name
                        
                            # Check both conditions: branch status and task inclusion
                            branch_correct = (current_branch == expected_branch)
                            in_task = (service_name in affected_services)
                        
                            # Handle all four scenarios with clear, specific error messages
                            if in_task and branch_correct:
                                # Scenario 1: Everything is correct - allow to proceed
                                pass
                            elif in_task and not branch_correct:
                                # Scenario 2: Service is in task but on wrong branch
                                print(f"[Branch Mismatch] Service '{service_name}' is part of this task but is on branch '{current_branch}' instead of '{expected_branch}'.", file=sys.stderr)
                                print(f"Please run: cd {repo_path.relative_to(project_root)} && git checkout {expected_branch}", file=sys.stderr)
                                sys.exit(2)
                            elif not in_task and branch_correct:
                                # Scenario 3: Service not in task but already on correct branch
                                print(f"[Service Not in Task] Service '{service_name}' is on the correct branch '{expected_branch}' but is not listed in the task file.", file=sys.stderr)
                                print(f"Please update the task file to include '{service_name}' in the services list.", file=sys.stderr)
                                sys.exit(2)
                            else:  # not in_task and not branch_correct
                                # Scenario 4: Service not in task AND on wrong branch
                                print(f"[Service Not in Task + Wrong Branch] Service '{service_name}' has two issues:", file=sys.stderr)
                                print(f"  1. Not listed in the task file's services", file=sys.stderr)
                                print(f"  2. On branch '{current_branch}' instead of '{expected_branch}'", file=sys.stderr)
                                print(f"To fix: cd {repo_path.relative_to(project_root)} && git checkout -b {expected_branch}", file=sys.stderr)
                                print(f"Then update the task file to include '{service_name}' in the services list.", file=sys.stderr)
                                sys.exit(2)
                        else:
                            # Single repo or main repo
                            if current_branch != expected_branch:
                                print(f"[Branch Mismatch] Repository is on branch '{current_branch}' but task expects '{expected_branch}'. Please checkout the correct branch.", file=sys.stderr)
                                sys.exit(2)
                            
                    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
                        # Can't check branch (incl. git binary missing → OSError); allow
                        # to proceed but warn — never crash the hook here.
                        print(f"Warning: Could not verify branch: {e}", file=sys.stderr)

    # Allow tool to proceed
    sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # UNFORESEEN internal error only. Every deliberate sys.exit(2) verdict
        # raises SystemExit — NOT an Exception subclass — so it passes straight
        # through this backstop: the token-bridge secret guard and the
        # protected/frozen-path guards stay fail-CLOSED. Here we fail OPEN,
        # LOUDLY. Exit 2 was rejected: this hook's PreToolUse matcher also covers
        # Read|Grep|Bash, so a blocking backstop would brick the whole session
        # with no way to even diagnose. Foreseeable corruption already fails
        # closed via the restrictive reader defaults, so only genuinely
        # unforeseen bugs reach here (h-fix-daic-enforcement-fail-open).
        print(f"[sessions-enforce] internal error — DAIC not enforced this call: {exc}", file=sys.stderr)
        sys.exit(0)