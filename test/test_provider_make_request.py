#!/usr/bin/env python3
"""Consolidated `_make_request` behavior (m-provider-layer-dedup).

`_make_request` used to be three near-identical per-provider copies
(`gitlab/api.py`, `github/api.py`, `jira_utils.py`). It now lives ONCE on
`IssueTrackingProvider` (`issue_provider_base.py`); each provider supplies
`self.api_base` / `self.headers` / `self.request_timeout` in its `__init__`.

These tests pin the shared behavior across all three providers, including the
capabilities Jira GAINED for free by inheriting the base method: 204 No Content
handling, DELETE, and PATCH (the old Jira copy supported only GET/POST/PUT and
crashed on a 204 via `response.json()`).

Run with: python3 -m pytest test/test_provider_make_request.py -v
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests as real_requests

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import issue_provider_base  # noqa: E402  (path set up above)


# (module, class, api_base) for each provider — a bare instance is built with
# __new__ so no config/network is touched.
PROVIDERS = [
    ("gitlab.api", "GitLabAPI", "https://gl.example/api/v4"),
    ("github.api", "GitHubAPI", "https://api.github.com"),
    ("jira_utils", "JiraProvider", "https://jira.example/rest/api/2"),
]
PROVIDER_IDS = ["gitlab", "github", "jira"]

TOKEN = "glpat-SUPERSECRETVALUE"


def _provider(module_name, cls_name, api_base):
    mod = importlib.import_module(module_name)
    cls = getattr(mod, cls_name)
    obj = cls.__new__(cls)  # bypass __init__ (no config / network)
    obj.api_base = api_base
    obj.headers = {"X-Test": "1"}
    obj.request_timeout = 30
    obj.api_token = TOKEN  # consumed by _redact_token
    return obj


def _ok_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = "OK"
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"ok": True} if json_body is None else json_body
    return resp


def _http_error_response(status_code, text, reason="Bad Request"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = reason
    resp.text = text
    err = real_requests.exceptions.HTTPError(response=resp)
    resp.raise_for_status.side_effect = err
    return resp


@pytest.mark.parametrize("module_name,cls_name,api_base", PROVIDERS, ids=PROVIDER_IDS)
def test_get_builds_url_headers_timeout(monkeypatch, module_name, cls_name, api_base):
    obj = _provider(module_name, cls_name, api_base)
    mock_get = MagicMock(return_value=_ok_response(json_body={"id": 1}))
    monkeypatch.setattr(issue_provider_base.requests, "get", mock_get)

    result = obj._make_request("GET", "/thing/1")

    assert result == {"id": 1}
    mock_get.assert_called_once_with(
        f"{api_base}/thing/1", headers=obj.headers, timeout=30
    )


@pytest.mark.parametrize("module_name,cls_name,api_base", PROVIDERS, ids=PROVIDER_IDS)
def test_204_no_content_returns_success(monkeypatch, module_name, cls_name, api_base):
    """204 must NOT call response.json() (that was the old Jira crash)."""
    obj = _provider(module_name, cls_name, api_base)
    resp = _ok_response(status_code=204)
    resp.json.side_effect = AssertionError("json() must not be called on a 204")
    monkeypatch.setattr(issue_provider_base.requests, "post",
                        MagicMock(return_value=resp))

    result = obj._make_request("POST", "/thing", {"a": 1})
    assert result == {"success": True}


@pytest.mark.parametrize("module_name,cls_name,api_base", PROVIDERS, ids=PROVIDER_IDS)
def test_delete_supported(monkeypatch, module_name, cls_name, api_base):
    """DELETE works for all three (Jira's old copy only had GET/POST/PUT)."""
    obj = _provider(module_name, cls_name, api_base)
    mock_delete = MagicMock(return_value=_ok_response(status_code=204))
    monkeypatch.setattr(issue_provider_base.requests, "delete", mock_delete)

    result = obj._make_request("DELETE", "/thing/1")
    assert result == {"success": True}
    mock_delete.assert_called_once_with(
        f"{api_base}/thing/1", headers=obj.headers, json=None, timeout=30
    )


@pytest.mark.parametrize("module_name,cls_name,api_base", PROVIDERS, ids=PROVIDER_IDS)
def test_patch_supported(monkeypatch, module_name, cls_name, api_base):
    """PATCH works for all three (Jira gained it via the base)."""
    obj = _provider(module_name, cls_name, api_base)
    mock_patch = MagicMock(return_value=_ok_response(json_body={"patched": True}))
    monkeypatch.setattr(issue_provider_base.requests, "patch", mock_patch)

    result = obj._make_request("PATCH", "/thing/1", {"x": 2})
    assert result == {"patched": True}
    mock_patch.assert_called_once_with(
        f"{api_base}/thing/1", headers=obj.headers, json={"x": 2}, timeout=30
    )


@pytest.mark.parametrize("module_name,cls_name,api_base", PROVIDERS, ids=PROVIDER_IDS)
def test_http_error_redacts_token_in_body(monkeypatch, module_name, cls_name, api_base):
    obj = _provider(module_name, cls_name, api_base)
    body = f"unauthorized for token {TOKEN} — retry"
    monkeypatch.setattr(issue_provider_base.requests, "get",
                        MagicMock(return_value=_http_error_response(401, body, "Unauthorized")))

    result = obj._make_request("GET", "/thing/1")
    assert result["error"] is True
    assert result["error_type"] == "http_error"
    assert result["status_code"] == 401
    assert TOKEN not in result["response_body"]
    assert "[REDACTED]" in result["response_body"]


@pytest.mark.parametrize("module_name,cls_name,api_base", PROVIDERS, ids=PROVIDER_IDS)
def test_timeout_maps_to_error_dict(monkeypatch, module_name, cls_name, api_base):
    obj = _provider(module_name, cls_name, api_base)
    monkeypatch.setattr(issue_provider_base.requests, "get",
                        MagicMock(side_effect=real_requests.exceptions.Timeout()))

    result = obj._make_request("GET", "/thing/1")
    assert result["error"] is True
    assert result["error_type"] == "timeout"


@pytest.mark.parametrize("module_name,cls_name,api_base", PROVIDERS, ids=PROVIDER_IDS)
def test_unsupported_method_maps_to_error(monkeypatch, module_name, cls_name, api_base):
    obj = _provider(module_name, cls_name, api_base)
    result = obj._make_request("HEAD", "/thing/1")
    assert result["error"] is True
    assert result["error_type"] == "unexpected_error"


def test_bcc_timeout_env_override(monkeypatch):
    """BCC_HTTP_TIMEOUT_SECONDS overrides the per-instance request_timeout."""
    obj = _provider(*PROVIDERS[0])  # gitlab, default 30
    monkeypatch.setenv("BCC_HTTP_TIMEOUT_SECONDS", "7")
    mock_get = MagicMock(return_value=_ok_response())
    monkeypatch.setattr(issue_provider_base.requests, "get", mock_get)

    obj._make_request("GET", "/x")
    assert mock_get.call_args.kwargs["timeout"] == 7


def test_github_shorter_default_timeout(monkeypatch):
    """GitHub keeps its shorter 15s default when the env var is unset."""
    monkeypatch.delenv("BCC_HTTP_TIMEOUT_SECONDS", raising=False)
    obj = _provider("github.api", "GitHubAPI", "https://api.github.com")
    obj.request_timeout = 15  # what GitHubAPI.__init__ sets
    mock_get = MagicMock(return_value=_ok_response())
    monkeypatch.setattr(issue_provider_base.requests, "get", mock_get)

    obj._make_request("GET", "/x")
    assert mock_get.call_args.kwargs["timeout"] == 15
