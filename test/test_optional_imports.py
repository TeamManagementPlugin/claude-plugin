#!/usr/bin/env python3
"""Optional-import degradation tests (h-hook-port-boot-detector, commit 1).

On a cold plugin session the venv does not exist yet, so the hook code runs under
the *system* python where tiktoken / requests are absent. Before this change the
module-level `import tiktoken` (task-transcript-link.py) and the five module-level
`import requests` (jira_utils, gitlab/api, gitlab/releases, github/api,
github/releases) raised ImportError and crashed the hook on import.

These tests pin the post-change contract:

  (1) `_make_request` returns a structured `dependency_missing` error (NOT
      raising) when `requests` is None. The method is now single-sourced on
      `IssueTrackingProvider` (issue_provider_base.py), so the guard reads
      `issue_provider_base.requests` — patch THAT module's global. The guard MUST
      precede the `try` because the `except requests.exceptions.*` clauses resolve
      `requests.exceptions` at handler-evaluation time and would AttributeError on
      a None `requests`. (The three API modules keep their own guarded `import
      requests` only for backward-compat; the live guard lives in the base.)
  (2) Both release `_upload_release_asset` methods return None (their error
      contract) when `requests` is None — guard before any work.
  (3) `task-transcript-link.py` runs to exit 0 and still increments the
      subagent-depth counter when tiktoken is unavailable (the increment must not
      be skipped — it gates DAIC subagent bypass), degrading only the
      token-based transcript chunking to a char-length estimate.

Run with: python3 -m pytest test/test_optional_imports.py -v
"""

import importlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


def _instance(module_name, cls_name, **attrs):
    """Import the module and build a bare instance (bypassing __init__/config)."""
    mod = importlib.import_module(module_name)
    cls = getattr(mod, cls_name)
    obj = cls.__new__(cls)
    for k, v in attrs.items():
        setattr(obj, k, v)
    return mod, obj


# --- (1) _make_request dependency_missing guard ----------------------------

def test_gitlab_make_request_dependency_missing(monkeypatch):
    import issue_provider_base
    _, obj = _instance("gitlab.api", "GitLabAPI",
                       api_base="https://gitlab.example/api/v4", headers={})
    monkeypatch.setattr(issue_provider_base, "requests", None)
    result = obj._make_request("GET", "/projects/x")
    assert isinstance(result, dict)
    assert result.get("error") is True
    assert result.get("error_type") == "dependency_missing"


def test_github_make_request_dependency_missing(monkeypatch):
    import issue_provider_base
    _, obj = _instance("github.api", "GitHubAPI",
                       api_base="https://api.github.com", headers={})
    monkeypatch.setattr(issue_provider_base, "requests", None)
    result = obj._make_request("GET", "/repos/x/y")
    assert isinstance(result, dict)
    assert result.get("error") is True
    assert result.get("error_type") == "dependency_missing"


def test_jira_make_request_dependency_missing(monkeypatch):
    import issue_provider_base
    _, obj = _instance("jira_utils", "JiraProvider",
                       api_base="https://jira.example/rest/api/2", headers={})
    monkeypatch.setattr(issue_provider_base, "requests", None)
    result = obj._make_request("GET", "/issue/X-1")
    assert isinstance(result, dict)
    assert result.get("error") is True
    assert result.get("error_type") == "dependency_missing"


# --- (2) release upload guard returns None ---------------------------------

def test_gitlab_release_upload_none_without_requests(monkeypatch):
    mod, obj = _instance("gitlab.releases", "GitLabReleaseManager")
    monkeypatch.setattr(mod, "requests", None)
    assert obj._upload_release_asset("/nonexistent/file.bin") is None


def test_github_release_upload_none_without_requests(monkeypatch):
    mod, obj = _instance("github.releases", "GitHubReleaseManager")
    monkeypatch.setattr(mod, "requests", None)
    assert obj._upload_release_asset(1, "https://u/x{?name,label}", "/nonexistent/file.bin") is None


# --- (3) task-transcript-link runs without tiktoken ------------------------

def _write_transcript(path: Path):
    """A minimal transcript: an Edit tool_use (start marker) then two messages."""
    lines = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "hi there"}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def test_transcript_link_degrades_without_tiktoken(tmp_path):
    project = tmp_path
    (project / ".claude" / "state").mkdir(parents=True)
    transcript = project / "transcript.jsonl"
    _write_transcript(transcript)

    payload = json.dumps({
        "tool_name": "Task",
        "transcript_path": str(transcript),
        "tool_input": {"subagent_type": "tester", "description": "d", "prompt": "p"},
    })

    hook = HOOKS_DIR / "task-transcript-link.py"
    # Block tiktoken: sys.modules['tiktoken'] = None makes `import tiktoken` raise
    # ImportError, exactly as on a cold session without the venv.
    code = (
        "import sys; sys.modules['tiktoken'] = None; "
        "import runpy; runpy.run_path(sys.argv[1], run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(hook)],
        input=payload, capture_output=True, text=True,
        cwd=str(project),
        env={"CLAUDE_PROJECT_DIR": str(project), "PYTHONPATH": str(HOOKS_DIR),
             "PATH": __import__("os").environ.get("PATH", "")},
        timeout=30,
    )
    assert result.returncode == 0, f"hook crashed without tiktoken: {result.stderr!r}"

    depth_file = project / ".claude" / "state" / "subagent-depth.json"
    assert depth_file.exists(), "subagent-depth counter was not written"
    assert json.loads(depth_file.read_text())["depth"] == 1, "depth increment skipped"

    # Chunking still ran (char-length fallback) — at least one chunk file written.
    staged = list((project / ".claude" / "state" / "tester").rglob("current_transcript_*.json"))
    assert staged, "transcript chunking produced no output under tiktoken fallback"
