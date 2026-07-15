"""Regression tests for issue-tracking integration robustness.

Task: m-fix-issue-tracking-integration-robustness

B1 (idempotency): `_func_create_issue_if_enabled` (protocol_engine.py) created a
duplicate provider issue on a step re-run because it never consulted
`get_task_mapping` before `create_issue_from_task`. The fix adds a per-provider
"already linked" check keyed on that provider's issue-id mapping key
(gitlab -> gitlab_issue_iid, github -> issue_id, jira -> jira_issue_key/id).

B2 (GitLab id vs iid): `create_gitlab_issue_from_task` returned GitLab's *global*
issue `id`, but every GitLab consumer builds the REST path from the project
`iid`, so round-tripping the create-returned number into issue_set_status /
issue_comment 404'd. The fix surfaces the iid from the producer. GitHub returns
`issue['number']` (= path = user-facing) and Jira uses keys, so both are
unaffected.

Red-green: against the pre-fix code the idempotency test sees a second
`create_issue_from_task` call (no skip), and the B2 test sees the global id 2026
returned instead of the iid 31 -- both assertions fail; with the fix they pass.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO / "plugin" / "hooks"
_MCP_DIR = _REPO / "plugin" / "mcp"
for _p in (str(_HOOKS_DIR), str(_MCP_DIR), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from protocol_engine import ProtocolEngine  # noqa: E402
from gitlab_utils import GitLabTaskSync  # noqa: E402  (re-exported from gitlab.task_sync)

# Module the GitLab sync class actually lives in, for patching find_task_file.
_GL_SYNC_MOD = sys.modules[GitLabTaskSync.__module__]


def _write_config(tmp_path: Path, payload: dict) -> None:
    cfg_dir = tmp_path / "team-management"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_task_file(tmp_path: Path, task_name: str) -> Path:
    tasks_dir = tmp_path / "team-management" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    f = tasks_dir / f"{task_name}.md"
    f.write_text(f"---\ntask: {task_name}\n---\n\n# Title\n\nBody.\n", encoding="utf-8")
    return f


@pytest.fixture
def engine(tmp_path):
    eng = ProtocolEngine.__new__(ProtocolEngine)
    eng.project_root = tmp_path
    return eng


# --------------------------------------------------------------------------- #
# B1 -- idempotency: create_issue_if_enabled must skip when already linked
# --------------------------------------------------------------------------- #

# provider -> (get_<provider>_sync target, an already-linked mapping for it)
_B1_CASES = {
    "gitlab": ("gitlab_utils.get_gitlab_sync", {"gitlab_issue_iid": 7, "gitlab_issue_id": 2026}),
    "github": ("github_utils.get_github_sync", {"issue_id": 99}),
    "jira": ("jira_utils.get_jira_sync", {"jira_issue_key": "PROJ-1"}),
}


@pytest.mark.parametrize("provider", ["gitlab", "github", "jira"])
def test_create_issue_idempotent_skips_when_already_linked(engine, tmp_path, monkeypatch, provider):
    """Second create for an already-linked task must skip, not create a duplicate."""
    patch_target, linked_mapping = _B1_CASES[provider]
    _write_config(tmp_path, {
        "issue_tracking": {"provider": provider},
        provider: {"issue_tracking_enabled": True},
    })
    _write_task_file(tmp_path, "x-foo")

    mock_sync = MagicMock()
    # First call: not yet linked. Second call: already linked.
    mock_sync.get_task_mapping.side_effect = [None, dict(linked_mapping)]
    mock_sync.create_issue_from_task.return_value = 123
    monkeypatch.setattr(patch_target, lambda: mock_sync)

    first = engine._func_create_issue_if_enabled({"task": "x-foo"})
    second = engine._func_create_issue_if_enabled({"task": "x-foo"})

    assert first.get("action") == "created", first
    assert second.get("action") == "skipped", second
    assert mock_sync.create_issue_from_task.call_count == 1


# --------------------------------------------------------------------------- #
# B2 -- create_gitlab_issue_from_task must return the project iid, not the
#       global id, so the number round-trips through the iid-based REST path.
# --------------------------------------------------------------------------- #

def test_create_gitlab_issue_returns_iid_not_global_id(tmp_path):
    """Producer must surface the project iid (31), not the global id (2026)."""
    s = GitLabTaskSync.__new__(GitLabTaskSync)
    s.project_root = tmp_path
    s.gitlab = MagicMock()
    s.gitlab.create_issue.return_value = {
        "id": 2026,
        "iid": 31,
        "web_url": "https://gitlab.example.com/x/-/issues/31",
        "created_at": "2026-06-10T00:00:00Z",
    }
    store: dict = {}
    s._load_mappings = lambda: dict(store)

    def _save(mappings):
        store.clear()
        store.update(mappings)

    s._save_mappings = _save

    task_file = _write_task_file(tmp_path, "m-foo")
    with patch.object(_GL_SYNC_MOD, "find_task_file", return_value=task_file):
        result = s.create_gitlab_issue_from_task("m-foo")

    assert result == 31, "must return the project iid (31), not the global id 2026"
    mapping = store["m-foo"]
    assert mapping["gitlab_issue_id"] == 2026   # global id still stored
    assert mapping["gitlab_issue_iid"] == 31    # iid still stored
