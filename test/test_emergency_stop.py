"""Whole-word emergency-stop matching (h-retire-legacy-protocol-pointers, Defect 2).

`user-messages.py` used `any(word in prompt for word in ["SILENCE", "STOP"])` —
a raw substring match that fired on "BACKSTOP" / "unSTOPpable" / any report text
quoting the word (observed live twice). The matcher moved into
`shared_state.is_emergency_stop` (whole-word, case-sensitive) so it is unit
testable — user-messages.py runs `json.load(sys.stdin)` at import and cannot be
imported directly.
"""
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO / "plugin" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from shared_state import is_emergency_stop  # noqa: E402


@pytest.mark.parametrize("prompt", [
    "STOP",
    "SILENCE",
    "please STOP now",
    "STOP.",
    "STOP!",
    "we must STOP the work",
    "SILENCE, please",
    "align, then STOP and SILENCE",
])
def test_fires_on_whole_word(prompt):
    assert is_emergency_stop(prompt) is True


@pytest.mark.parametrize("prompt", [
    "BACKSTOP",
    "the backstop mechanism",
    "unstoppable",
    "unSTOPpable",           # embedded, no word boundary
    "STOPWATCH",             # uppercase but embedded — no boundary
    "SILENCED",              # uppercase but embedded — no boundary
    "stop",                  # lowercase — case-sensitive
    "silence",               # lowercase
    "stopping the server",
    "a report about the emergency-stop feature in lowercase",
    "",
])
def test_does_not_fire(prompt):
    assert is_emergency_stop(prompt) is False


def test_none_input_is_safe():
    assert is_emergency_stop(None) is False
