"""Content rules for CLAUDE.md files.

CLAUDE.md is re-read into every session, so it holds only current,
load-bearing facts. These rules flag the three kinds of noise that otherwise
accumulate one task at a time: task names, history wording, and code-review
chatter. Pure functions — the engine's `check_claude_md_hygiene` post_func
feeds them the lines a task ADDED; the repo's own test feeds them whole files.
"""

import re
from pathlib import Path
from typing import Iterable, List, NamedTuple, Set, Tuple

DEFAULT_MAX_LINES = 300

# Marker prefixes of the managed @-import block that session start wires into a
# project CLAUDE.md (full markers: shared_state._CLAUDE_MD_BEGIN / _CLAUDE_MD_END).
_MANAGED_BEGIN = "<!-- team-management:begin"
_MANAGED_END = "<!-- team-management:end"

# Narrow phrases, not keywords: "fixed-size buffer" and "a key used to sign"
# are present-tense prose and must pass.
_HISTORY_RE = re.compile(
    r"\b(?:previously|formerly|no longer|used to be"
    r"|(?:was|were) (?:removed|replaced|renamed|retired|added|fixed|deleted|dropped)"
    r"|renamed from|retired in|fixed in|introduced in)\b",
    re.IGNORECASE,
)
_REVIEW_RE = re.compile(
    r"\bround \d+\b"
    r"|\b(?:codex|agy|red-team|reviewer|review)\s+[CWP]\d+\b"
    r"|\bverified on \S+ v?\d+(?:\.\d+)+",
    re.IGNORECASE,
)


class HygieneHit(NamedTuple):
    line: int
    rule: str
    text: str


def collect_task_names(project_root: Path) -> Set[str]:
    """Names of every task under team-management/tasks/ (open, done, and
    directory tasks). Empty when the directory does not exist."""
    tasks_dir = Path(project_root) / "team-management" / "tasks"
    names: Set[str] = set()
    for base in (tasks_dir, tasks_dir / "done"):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.is_file() and entry.suffix == ".md" and entry.stem != "TEMPLATE":
                names.add(entry.stem)
            elif entry.is_dir() and (entry / "README.md").is_file():
                names.add(entry.name)
    return names


def _task_name_re(task_names: Set[str]):
    if not task_names:
        return None
    alternation = "|".join(re.escape(n) for n in sorted(task_names, key=len, reverse=True))
    return re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-])")


def find_hygiene_hits(lines: Iterable[Tuple[int, str]], task_names: Set[str]) -> List[HygieneHit]:
    """One hit per offending line (first matching rule wins), in line order."""
    task_re = _task_name_re(task_names)
    hits: List[HygieneHit] = []
    for number, text in lines:
        if task_re is not None and task_re.search(text):
            hits.append(HygieneHit(number, "task-name", text))
        elif _HISTORY_RE.search(text):
            hits.append(HygieneHit(number, "history", text))
        elif _REVIEW_RE.search(text):
            hits.append(HygieneHit(number, "review", text))
    return hits


def parse_added_lines(unified_zero_diff: str) -> List[Tuple[int, str]]:
    """(new line number, text) for every '+' line of a `git diff -U0` patch."""
    added: List[Tuple[int, str]] = []
    next_line = 0
    in_hunk = False  # the "+++ b/<path>" file header only precedes the first hunk
    for raw in unified_zero_diff.splitlines():
        if raw.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            next_line = int(match.group(1)) if match else 0
            in_hunk = True
        elif in_hunk and raw.startswith("+"):
            # Inside a hunk every "+" line is content — even one whose text starts with "++".
            added.append((next_line, raw[1:]))
            next_line += 1
    return added


def count_content_lines(text: str) -> int:
    """Non-blank lines of a CLAUDE.md that its authors wrote.

    Excluded: blank lines, managed-block marker lines, and `@` import lines
    between a begin and an end marker — exactly what session start wires in.
    Every other line counts, so no arrangement of markers (unclosed, stray,
    nested) can hide content from the budget."""
    count = 0
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_MANAGED_BEGIN):
            inside = True
        elif stripped.startswith(_MANAGED_END):
            inside = False
        elif stripped and not (inside and stripped.startswith("@")):
            count += 1
    return count
