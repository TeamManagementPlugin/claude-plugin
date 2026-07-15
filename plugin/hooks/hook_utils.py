#!/usr/bin/env python3
"""Small, dependency-free helpers shared across hooks.

Canonical home for ``normalize_command``: the boot-detector — a hook — imports it
here to dedup-canonicalise settings.json hook commands. Pure stdlib (the former
installer package that also used it was retired in m-installer-retirement).
"""
import re
import shlex


def command_is_read_only(command, read_only_commands):
    r"""True if EVERY &&/||/;/|-separated segment of ``command`` begins with an
    allowlisted read-only prefix on a WHOLE-TOKEN boundary — the segment equals
    a prefix, or starts with ``prefix + " "``.

    Word-boundary matching is the fix for a prior ``segment.startswith(prefix)``
    that false-matched write-capable commands merely sharing a prefix with a
    read-only entry: ``cdk deploy`` → ``cd``, ``catalog-cli push`` → ``cat``,
    ``lsof`` → ``ls``. It still accepts multi-token allowlist entries such as
    ``git status`` (so ``git status --short`` matches) and ``sed -n`` (so
    ``sed -n '1,5p'`` matches). Empty segments are skipped.

    Callers gate on the write-pattern set (BASH_WRITE_PATTERNS) BEFORE calling
    this — a segment carrying a redirect / rm / mv etc. never reaches here.
    """
    for part in re.split(r'(?:&&|\|\||;|\|)', command):
        part = part.strip()
        if not part:
            continue
        if not any(part == prefix or part.startswith(prefix + " ")
                   for prefix in read_only_commands):
            return False
    return True


def is_task_markdown_path(file_path):
    r"""True if ``file_path`` points at a task markdown file under
    ``team-management/tasks/``, tolerating Windows backslash separators.

    The bare ``"team-management/tasks/" in file_path`` substring check was
    silently dead on backslash paths (``team-management\tasks\x.md``), so
    task-file-edit detection — which drives auto-sync / auto-worklog — never
    fired on Windows. Normalize separators first (mirrors sessions-enforce.py).
    """
    norm = (file_path or "").replace("\\", "/")
    return "team-management/tasks/" in norm and norm.endswith(".md")


def normalize_command(cmd: str) -> str:
    r"""Normalize a hook command string for deduplication.

    Every quoting/separator spelling of the same command must collapse to one
    canonical form, otherwise the settings.json merge in _register_hooks fails
    to dedup and reinstalls accumulate duplicate hooks (primarily Windows):
    - "C:\Python\python.exe" .claude/hooks/script.py
    - C:\Python\python.exe .claude\hooks\script.py
    - "C:\Python\python.exe" "C:\proj\.claude\hooks\script.py"
    All normalize to: C:/Python/python.exe C:/proj/.claude/hooks/script.py

    Quote stripping is symmetric across ALL tokens (the previous version
    stripped quotes from the python path but not the script path).
    """
    if not cmd:
        return ""

    try:
        # posix=False keeps Windows backslashes intact and retains quotes on
        # the tokens, which we then strip uniformly.
        tokens = shlex.split(cmd.strip(), posix=False)
    except ValueError:
        # Unbalanced quotes — degrade to plain whitespace split.
        tokens = cmd.strip().split()

    cleaned = [token.strip('"').strip("'") for token in tokens]
    normalized = " ".join(cleaned)

    # Normalize path separators (Windows \ vs Unix /)
    return normalized.replace("\\", "/")
