"""De-noised protocol-intent detection (h-retire-legacy-protocol-pointers, Defect 3).

`user-messages.py:300-319` used loose substring matches (`"compact" in prompt`,
`"create a task" in prompt`, ...) that fired on ordinary report text
("I created a task earlier", "the compaction runbook") — observed live. The
matcher moved into `shared_state.detect_protocol_intent` (word-boundary,
present-tense-imperative anchored) so it is unit testable.
"""
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO / "plugin" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from shared_state import detect_protocol_intent  # noqa: E402


@pytest.mark.parametrize("prompt,expected", [
    ("create a task for the auth bug", "task-creation"),
    ("Create a new task", "task-creation"),   # uppercase start — re.IGNORECASE
    ("please make a new task", "task-creation"),
    ("add a task to the backlog", "task-creation"),
    ("let's create a task", "task-creation"),
    ("new task for the auth bug", "task-creation"),   # noun-first directive (codex P2)
    ("new task for implementing OAuth", "task-creation"),
    ("switch to task h-foo", "task-startup"),
    ("resume task m-bar", "task-startup"),
    ("change to task l-baz", "task-startup"),
    ("finish the task", "task-completion"),
    ("complete the task now", "task-completion"),
    ("close the task please", "task-completion"),
    ("wrap up the task", "task-completion"),
    ("the task is done", "task-completion"),
    ("mark as complete", "task-completion"),          # no "task" word
    ("mark the task as done", "task-completion"),
    ("/compact", "context-compaction"),
    ("let's run context compaction", "context-compaction"),
    ("compact the context please", "context-compaction"),
    ("restart the session", "context-compaction"),
])
def test_directive_detected(prompt, expected):
    assert detect_protocol_intent(prompt) == expected


@pytest.mark.parametrize("prompt", [
    # Past-tense / descriptive report text — the whole reason for the rewrite.
    "I created a task earlier for this",
    "the compaction runbook is legacy",
    "compaction reduces the context window",
    "we should implement the backstop",
    "the task-creation protocol used to do X",
    "a task was completed last week",
    "this is a compact representation",
    "here is my report on how tasks finished",
    "create a taskforce for the event",   # 'taskforce' is not 'task'
    "the new task force met on friday",    # 'task force' != 'new task for' (\\bfor\\b anchor)
    "finish this tasking order",           # 'tasking' is not 'task'
    "compact car rental",                  # bare 'compact' no longer fires
    "the benchmark completed successfully",
    "",
])
def test_report_text_does_not_fire(prompt):
    assert detect_protocol_intent(prompt) is None


def test_none_input_is_safe():
    assert detect_protocol_intent(None) is None
