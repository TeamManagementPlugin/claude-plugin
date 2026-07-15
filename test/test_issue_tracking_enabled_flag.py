import json
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from protocol_engine import ProtocolEngine  # noqa: E402


def _write_config(tmp_path: Path, payload: dict) -> Path:
    cfg_dir = tmp_path / "team-management"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(json.dumps(payload), encoding="utf-8")
    return cfg_file


@pytest.fixture
def engine(tmp_path):
    eng = ProtocolEngine.__new__(ProtocolEngine)
    eng.project_root = tmp_path
    return eng


@pytest.mark.parametrize("provider", ["gitlab", "github", "jira"])
def test_create_skipped_when_flag_false(engine, tmp_path, provider):
    _write_config(tmp_path, {
        "issue_tracking": {"provider": provider},
        provider: {"issue_tracking_enabled": False},
    })
    result = engine._func_create_issue_if_enabled({"task": "x-foo"})
    assert result["success"] is True
    assert result.get("action") == "skipped"
    assert "issue_tracking_enabled" in result.get("message", "")


@pytest.mark.parametrize("provider", ["gitlab", "github", "jira"])
def test_update_skipped_when_flag_false(engine, tmp_path, provider, monkeypatch):
    _write_config(tmp_path, {
        "issue_tracking": {"provider": provider},
        provider: {"issue_tracking_enabled": False},
    })
    monkeypatch.setattr("protocol_engine.get_task_state", lambda: {"task": "x-foo"})
    result = engine._func_update_issue_status({})
    assert result["success"] is True
    assert result.get("action") == "skipped"
    assert "issue_tracking_enabled" in result.get("message", "")


def test_create_passes_flag_check_when_key_missing(engine, tmp_path):
    _write_config(tmp_path, {
        "issue_tracking": {"provider": "gitlab"},
        "gitlab": {},
    })
    result = engine._func_create_issue_if_enabled({"task": "nonexistent-task"})
    blob = json.dumps(result)
    assert "issue_tracking_enabled" not in blob, blob


def test_create_passes_flag_check_when_true(engine, tmp_path):
    _write_config(tmp_path, {
        "issue_tracking": {"provider": "gitlab"},
        "gitlab": {"issue_tracking_enabled": True},
    })
    result = engine._func_create_issue_if_enabled({"task": "nonexistent-task"})
    blob = json.dumps(result)
    assert "issue_tracking_enabled" not in blob, blob
