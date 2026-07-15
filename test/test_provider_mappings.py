#!/usr/bin/env python3
"""`_load_mappings` corrupt-file safety (m-provider-layer-dedup).

The old `_load_mappings` used a bare `except: return {}`, so a corrupt mappings
file read as an empty map and the next `_save_mappings` OVERWROTE it — silently
destroying every task↔issue link. The hardened version:
  - missing file → {} (tolerant: first run / nothing linked yet)
  - valid file   → its contents
  - corrupt JSON → copy the bytes to a `.corrupt-<ts>` sidecar, leave the live
                   file untouched, and RAISE (so nothing overwrites the links).

Run with: python3 -m pytest test/test_provider_mappings.py -v
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from issue_provider_base import IssueTrackingTaskSync  # noqa: E402


class _StubSync(IssueTrackingTaskSync):
    """Concrete subclass that skips the provider-backed base __init__."""

    def __init__(self, mappings_file):
        self.mappings_file = mappings_file  # the only attr _load_mappings needs

    def link_task_to_issue(self, *a, **k):
        return True

    def import_issue_as_task(self, *a, **k):
        return ""

    def create_issue_from_task(self, *a, **k):
        return None

    def sync_task_status_to_issue(self, *a, **k):
        return None


def test_missing_file_returns_empty(tmp_path):
    s = _StubSync(tmp_path / "gitlab-mappings.json")
    assert s._load_mappings() == {}


def test_valid_file_loads(tmp_path):
    f = tmp_path / "gitlab-mappings.json"
    data = {"m-x": {"gitlab_issue_iid": 5}}
    f.write_text(json.dumps(data), encoding="utf-8")
    assert _StubSync(f)._load_mappings() == data


def test_corrupt_file_raises_and_links_survive(tmp_path):
    f = tmp_path / "gitlab-mappings.json"
    original = '{"m-x": {"gitlab_issue_iid": 5} CORRUPT-NOT-JSON'
    f.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError):
        _StubSync(f)._load_mappings()

    # The live file is NEVER overwritten with {} — the links survive in place.
    assert f.read_text(encoding="utf-8") == original
    # A backup copy of the corrupt bytes was written alongside it.
    backups = list(tmp_path.glob("gitlab-mappings.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_corrupt_then_save_does_not_wipe(tmp_path):
    """End-to-end: the raise means _save_mappings never runs on the empty map,
    so a subsequent read of the live file still returns the original links."""
    f = tmp_path / "gitlab-mappings.json"
    original = '{"m-y": {"gitlab_issue_iid": 9}} trailing junk }'
    f.write_text(original, encoding="utf-8")
    s = _StubSync(f)

    with pytest.raises(RuntimeError):
        # A caller that would have done: m = _load_mappings(); ...; _save_mappings(m)
        m = s._load_mappings()
        s._save_mappings(m)  # unreachable — the load raised first

    assert f.read_text(encoding="utf-8") == original
