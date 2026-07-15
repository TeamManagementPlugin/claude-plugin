#!/usr/bin/env python3
"""Tests for Telegram chat-id discovery (m-telegram-chat-id-discovery).

Covers notification_utils.discover_telegram_chats() and
send_telegram_test_message(): getMe validation, non-mutating getUpdates,
distinct-chat extraction (incl. my_chat_member and private-chat title
synthesis), the 409-webhook and empty-updates hints, the missing-token
no-network guard, and the confirmation-ping send path.

`urllib.request.urlopen` is mocked, and `resolve_provider_token` is patched in
every test so a developer's real .claude/state/provider-tokens.json is never
read.

Run with: python3 -m pytest test/test_telegram_discover.py -v
"""

import io
import json
import ssl
import sys
import types
import urllib.error
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch, MagicMock

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
MCP_DIR = PROJECT_ROOT / "plugin" / "mcp"
for _p in (str(HOOKS_DIR), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import notification_utils  # noqa: E402
from tools import notifications as notifications_tool  # noqa: E402


class MockMCP:
    """Captures @mcp.tool()-decorated functions by name (matches server pattern)."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _FakeResp:
    """Minimal context-manager stand-in for an http.client.HTTPResponse."""

    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, payload):
    """Build a urllib HTTPError whose .read() yields a Telegram JSON body."""
    body = io.BytesIO(json.dumps(payload).encode("utf-8"))
    return urllib.error.HTTPError(
        url="https://api.telegram.org/botX/getUpdates",
        code=code, msg="err", hdrs=None, fp=body,
    )


def _router(responses, calls=None):
    """Return a urlopen replacement that dispatches by Telegram method path.

    responses: {method_name: payload_dict | Exception}. A payload is returned
    as a _FakeResp; an Exception is raised. Records each method name in `calls`.
    """
    def _fn(req, timeout=None, context=None):
        url = getattr(req, "full_url", req)
        for method, payload in responses.items():
            if f"/{method}" in url:
                if calls is not None:
                    calls.append(method)
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResp(payload)
        raise AssertionError(f"unexpected telegram call: {url}")
    return _fn


class DiscoverTelegramChatsTest(TestCase):

    def _patch_token(self, token):
        return patch.object(notification_utils, "resolve_provider_token",
                            return_value=token)

    def _patch_config(self, cfg=None):
        return patch.object(notification_utils, "_get_notification_config",
                            return_value=(cfg or {}))

    # --- missing token -----------------------------------------------------

    def test_no_token_makes_no_network_call(self):
        never = MagicMock(side_effect=AssertionError("no network on missing token"))
        with self._patch_token(""), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen", never):
            result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])
        self.assertIn("token", result["error"].lower())
        self.assertIn("provider-tokens.json", result["hint"])
        never.assert_not_called()

    # --- getMe validation --------------------------------------------------

    def test_invalid_token_does_not_call_getupdates(self):
        calls = []
        responses = {"getMe": {"ok": False, "description": "Unauthorized"}}
        with self._patch_token("BAD"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router(responses, calls)):
            result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])
        self.assertEqual(calls, ["getMe"])  # never reached getUpdates

    # --- discovery ---------------------------------------------------------

    def test_discovers_distinct_chats(self):
        me = {"ok": True, "result": {"id": 1, "username": "bot", "first_name": "B"}}
        updates = {"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 100, "type": "group", "title": "Team"}}},
            {"update_id": 2, "edited_message": {"chat": {"id": 100, "type": "group", "title": "Team"}}},
            {"update_id": 3, "channel_post": {"chat": {"id": 200, "type": "channel", "title": "News"}}},
        ]}
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": updates})):
            result = notification_utils.discover_telegram_chats()
        self.assertTrue(result["ok"])
        self.assertEqual(result["bot"]["username"], "bot")
        ids = [c["id"] for c in result["chats"]]
        self.assertEqual(ids, [100, 200])  # deduped, order preserved
        self.assertEqual(result["chats"][0]["title"], "Team")
        self.assertEqual(result["chats"][1]["type"], "channel")

    def test_my_chat_member_chat_surfaced(self):
        me = {"ok": True, "result": {"id": 1, "username": "bot"}}
        updates = {"ok": True, "result": [
            {"update_id": 5, "my_chat_member": {"chat": {"id": 300, "type": "supergroup", "title": "Added"}}},
        ]}
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": updates})):
            result = notification_utils.discover_telegram_chats()
        self.assertTrue(result["ok"])
        self.assertEqual([c["id"] for c in result["chats"]], [300])
        self.assertEqual(result["chats"][0]["title"], "Added")

    def test_private_chat_title_synthesized(self):
        me = {"ok": True, "result": {"id": 1, "username": "bot"}}
        updates = {"ok": True, "result": [
            {"update_id": 6, "message": {"chat": {
                "id": 42, "type": "private",
                "first_name": "Ada", "last_name": "Lovelace", "username": "ada"}}},
        ]}
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": updates})):
            result = notification_utils.discover_telegram_chats()
        self.assertEqual(result["chats"][0]["title"], "Ada Lovelace (@ada)")

    # --- error / empty paths ----------------------------------------------

    def test_webhook_409_returns_hint(self):
        me = {"ok": True, "result": {"id": 1, "username": "bot"}}
        err = _http_error(409, {"ok": False, "error_code": 409,
                                "description": "Conflict: can't use getUpdates"})
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": err})):
            result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])
        self.assertIn("webhook", result["hint"].lower())

    def test_empty_updates_returns_hint(self):
        me = {"ok": True, "result": {"id": 1, "username": "bot"}}
        updates = {"ok": True, "result": []}
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": updates})):
            result = notification_utils.discover_telegram_chats()
        self.assertTrue(result["ok"])
        self.assertEqual(result["chats"], [])
        self.assertIn("hint", result)

    def test_callback_query_without_message_ignored(self):
        me = {"ok": True, "result": {"id": 1, "username": "bot"}}
        updates = {"ok": True, "result": [
            {"update_id": 11, "callback_query": {"id": "cq"}},  # no .message → skip
            {"update_id": 12, "message": {"chat": {"id": 8, "type": "group", "title": "G"}}},
        ]}
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": updates})):
            result = notification_utils.discover_telegram_chats()
        self.assertTrue(result["ok"])
        self.assertEqual([c["id"] for c in result["chats"]], [8])

    def test_discovery_independent_of_notifications_disabled(self):
        me = {"ok": True, "result": {"id": 1, "username": "bot"}}
        updates = {"ok": True, "result": [
            {"update_id": 9, "message": {"chat": {"id": 7, "type": "group", "title": "G"}}},
        ]}
        with self._patch_token("T"), self._patch_config({"enabled": False}), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": updates})):
            result = notification_utils.discover_telegram_chats()
        self.assertTrue(result["ok"])
        self.assertEqual([c["id"] for c in result["chats"]], [7])

    def test_non_dict_notifications_config_does_not_raise(self):
        # "notifications" set to a non-dict (e.g. `true`) must not break the
        # "never raises" contract — return a structured result, not AttributeError.
        never = MagicMock(side_effect=AssertionError("no network without token"))
        with self._patch_token(""), self._patch_config(True), \
                patch.object(notification_utils.urllib.request, "urlopen", never):
            result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])
        never.assert_not_called()

    def test_non_dict_channels_config_still_discovers(self):
        # A malformed `channels` value must not prevent discovery when the token
        # resolves from the token file (mirrored here by the patched resolver).
        me = {"ok": True, "result": {"id": 1, "username": "bot"}}
        updates = {"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 5, "type": "group", "title": "G"}}},
        ]}
        with self._patch_token("T"), self._patch_config({"channels": "nope"}), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": updates})):
            result = notification_utils.discover_telegram_chats()
        self.assertTrue(result["ok"])
        self.assertEqual([c["id"] for c in result["chats"]], [5])

    def test_non_dict_telegram_value_does_not_raise(self):
        # channels.telegram set to a non-dict must not raise (guard's third level).
        never = MagicMock(side_effect=AssertionError("no network without token"))
        with self._patch_token(""), self._patch_config({"channels": {"telegram": "foo"}}), \
                patch.object(notification_utils.urllib.request, "urlopen", never):
            result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])
        never.assert_not_called()

    def test_non_dict_config_root_does_not_raise(self):
        # A config.json whose ROOT is a list/str must not break the guaranteed-dict
        # contract of _get_notification_config (closes the helper "never raises" gap).
        with patch.object(notification_utils, "_load_config", return_value=[1, 2, 3]):
            self.assertEqual(notification_utils._get_notification_config(), {})
            with self._patch_token(""), \
                    patch.object(notification_utils.urllib.request, "urlopen",
                                 MagicMock(side_effect=AssertionError("no network"))):
                result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])

    def test_null_result_from_telegram_does_not_raise(self):
        # A spec-violating {"ok": true, "result": null} must not raise.
        me = {"ok": True, "result": None}
        updates = {"ok": True, "result": None}
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": updates})):
            result = notification_utils.discover_telegram_chats()
        self.assertTrue(result["ok"])
        self.assertEqual(result["chats"], [])


class SendTelegramTestMessageTest(TestCase):

    def _patch_token(self, token):
        return patch.object(notification_utils, "resolve_provider_token",
                            return_value=token)

    def _patch_config(self):
        return patch.object(notification_utils, "_get_notification_config",
                            return_value={})

    def test_send_ok(self):
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"sendMessage": {"ok": True, "result": {}}})):
            result = notification_utils.send_telegram_test_message("555")
        self.assertTrue(result["ok"])

    def test_send_failure_reports_error(self):
        err = _http_error(400, {"ok": False, "error_code": 400,
                                "description": "chat not found"})
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"sendMessage": err})):
            result = notification_utils.send_telegram_test_message("555")
        self.assertFalse(result["ok"])
        self.assertIn("chat not found", result["error"])

    def test_send_no_token(self):
        never = MagicMock(side_effect=AssertionError("no network on missing token"))
        with self._patch_token(""), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen", never):
            result = notification_utils.send_telegram_test_message("555")
        self.assertFalse(result["ok"])
        never.assert_not_called()

    def test_send_requires_chat_id(self):
        with self._patch_token("T"), self._patch_config():
            result = notification_utils.send_telegram_test_message("   ")
        self.assertFalse(result["ok"])


class NotificationDiscoverToolTest(TestCase):
    """The MCP wrapper notification_discover_telegram_chats."""

    def _tool(self):
        mcp = MockMCP()
        notifications_tool.register_tools(mcp)
        return mcp.tools

    def test_tool_registered(self):
        self.assertIn("notification_discover_telegram_chats", self._tool())

    def test_tool_never_accepts_a_token_param(self):
        import inspect
        fn = self._tool()["notification_discover_telegram_chats"]
        params = set(inspect.signature(fn).parameters)
        # API contract: tokens never enter the transcript.
        self.assertNotIn("token", params)
        self.assertNotIn("bot_token", params)
        self.assertEqual(params, {"test_chat_id"})

    def test_empty_arg_dispatches_to_discovery(self):
        fn = self._tool()["notification_discover_telegram_chats"]
        with patch.object(notification_utils, "discover_telegram_chats",
                          return_value={"ok": True, "bot": None, "chats": []}) as disc, \
                patch.object(notification_utils, "send_telegram_test_message") as send:
            out = fn("")
        disc.assert_called_once()
        send.assert_not_called()
        self.assertTrue(out["success"])

    def test_chat_id_arg_dispatches_to_test_ping(self):
        fn = self._tool()["notification_discover_telegram_chats"]
        with patch.object(notification_utils, "send_telegram_test_message",
                          return_value={"ok": True}) as send, \
                patch.object(notification_utils, "discover_telegram_chats") as disc:
            out = fn("12345")
        send.assert_called_once_with("12345")
        disc.assert_not_called()
        self.assertTrue(out["success"])

    def test_test_ping_failure_surfaces_error(self):
        fn = self._tool()["notification_discover_telegram_chats"]
        with patch.object(notification_utils, "send_telegram_test_message",
                          return_value={"ok": False, "error": "chat not found"}):
            out = fn("999")
        self.assertFalse(out["success"])
        self.assertIn("chat not found", out["error"])

    def test_wrapper_catches_helper_exception(self):
        # A raised helper must surface as the structured {success: False} shape,
        # matching the sibling tools (notify_user / notification_status).
        fn = self._tool()["notification_discover_telegram_chats"]
        with patch.object(notification_utils, "discover_telegram_chats",
                          side_effect=RuntimeError("boom")):
            out = fn("")
        self.assertFalse(out["success"])
        self.assertIn("error", out)


class _FakeCtx:
    """Stand-in for an ssl.SSLContext with a controllable trust-anchor count."""

    def __init__(self, x509=0):
        self._x509 = x509

    def cert_store_stats(self):
        return {"x509": self._x509, "crl": 0, "x509_ca": 0}


class TelegramSSLContextTest(TestCase):
    """_telegram_ca_bundle precedence + _telegram_ssl_context fallback ladder
    (m-fix-telegram-tls-verification)."""

    def _patch_config(self, cfg=None):
        return patch.object(notification_utils, "_get_notification_config",
                            return_value=(cfg or {}))

    # --- _telegram_ca_bundle precedence -----------------------------------

    def test_ca_bundle_config_beats_env(self):
        cfg = {"channels": {"telegram": {"ca_bundle": "/cfg.pem"}}}
        with self._patch_config(cfg), \
                patch.dict(notification_utils.os.environ,
                           {"SSL_CERT_FILE": "/env.pem"}, clear=False):
            self.assertEqual(notification_utils._telegram_ca_bundle(), "/cfg.pem")

    def test_ca_bundle_ssl_cert_file_beats_requests(self):
        with self._patch_config(), \
                patch.dict(notification_utils.os.environ,
                           {"SSL_CERT_FILE": "/a.pem",
                            "REQUESTS_CA_BUNDLE": "/b.pem"}, clear=False):
            self.assertEqual(notification_utils._telegram_ca_bundle(), "/a.pem")

    def test_ca_bundle_requests_when_no_ssl_cert_file(self):
        env = {k: v for k, v in notification_utils.os.environ.items()
               if k != "SSL_CERT_FILE"}
        env["REQUESTS_CA_BUNDLE"] = "/b.pem"
        with self._patch_config(), \
                patch.dict(notification_utils.os.environ, env, clear=True):
            self.assertEqual(notification_utils._telegram_ca_bundle(), "/b.pem")

    def test_ca_bundle_empty_when_unset(self):
        env = {k: v for k, v in notification_utils.os.environ.items()
               if k not in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")}
        with self._patch_config(), \
                patch.dict(notification_utils.os.environ, env, clear=True):
            self.assertEqual(notification_utils._telegram_ca_bundle(), "")

    def test_ca_bundle_guards_non_dict_levels(self):
        # A fat-finger telegram value must not raise (mirrors _resolve_telegram_token).
        env = {k: v for k, v in notification_utils.os.environ.items()
               if k not in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")}
        with self._patch_config({"channels": {"telegram": "nope"}}), \
                patch.dict(notification_utils.os.environ, env, clear=True):
            self.assertEqual(notification_utils._telegram_ca_bundle(), "")

    # --- _telegram_ssl_context fallback ladder ----------------------------

    def test_context_prefers_configured_bundle(self):
        calls = []

        def fake_cdc(cafile=None):
            calls.append(cafile)
            return _FakeCtx(x509=5)

        cfg = {"channels": {"telegram": {"ca_bundle": "/my/bundle.pem"}}}
        with self._patch_config(cfg), \
                patch.object(notification_utils.os.path, "isfile", return_value=True), \
                patch.object(notification_utils.ssl, "create_default_context",
                             side_effect=fake_cdc):
            notification_utils._telegram_ssl_context()
        self.assertEqual(calls[0], "/my/bundle.pem")

    def test_context_uses_default_when_anchors_present(self):
        calls = []

        def fake_cdc(cafile=None):
            calls.append(cafile)
            return _FakeCtx(x509=42)  # default store already populated

        with self._patch_config(), \
                patch.dict(notification_utils.os.environ, {}, clear=True), \
                patch.object(notification_utils.ssl, "create_default_context",
                             side_effect=fake_cdc):
            notification_utils._telegram_ssl_context()
        self.assertEqual(calls, [None])  # no fallback needed

    def test_context_empty_store_uses_certifi(self):
        calls = []

        def fake_cdc(cafile=None):
            calls.append(cafile)
            return _FakeCtx(x509=0 if cafile is None else 5)

        fake_certifi = types.ModuleType("certifi")
        fake_certifi.where = lambda: "/fake/certifi/cacert.pem"
        with self._patch_config(), \
                patch.dict(notification_utils.os.environ, {}, clear=True), \
                patch.object(notification_utils.ssl, "create_default_context",
                             side_effect=fake_cdc), \
                patch.dict(sys.modules, {"certifi": fake_certifi}):
            notification_utils._telegram_ssl_context()
        self.assertEqual(calls, [None, "/fake/certifi/cacert.pem"])

    def test_context_empty_store_falls_to_etc_ssl_without_certifi(self):
        calls = []

        def fake_cdc(cafile=None):
            calls.append(cafile)
            return _FakeCtx(x509=0 if cafile is None else 5)

        etc = notification_utils._ETC_SSL_CERT_FILE
        with self._patch_config(), \
                patch.dict(notification_utils.os.environ, {}, clear=True), \
                patch.object(notification_utils.ssl, "create_default_context",
                             side_effect=fake_cdc), \
                patch.dict(sys.modules, {"certifi": None}), \
                patch.object(notification_utils.os.path, "isfile",
                             side_effect=lambda p: p == etc):
            notification_utils._telegram_ssl_context()
        self.assertEqual(calls, [None, etc])

    def test_context_falls_through_on_missing_bundle_file(self):
        # A configured ca_bundle path that does not exist must be skipped, not crash.
        calls = []

        def fake_cdc(cafile=None):
            calls.append(cafile)
            return _FakeCtx(x509=5)

        cfg = {"channels": {"telegram": {"ca_bundle": "/nonexistent/bundle.pem"}}}
        with self._patch_config(cfg), \
                patch.object(notification_utils.os.path, "isfile", return_value=False), \
                patch.object(notification_utils.ssl, "create_default_context",
                             side_effect=fake_cdc):
            notification_utils._telegram_ssl_context()
        self.assertEqual(calls, [None])  # missing bundle skipped; default used

    def test_context_survives_nul_byte_bundle(self):
        # A NUL-byte config ca_bundle must not crash discovery. On Py3.8+ the REAL
        # os.path.isfile returns False for a NUL path (it does NOT raise), so the
        # bundle is skipped and we fall through to auto-detection — never reaching
        # create_default_context(cafile=NUL) (which WOULD raise). Real os.path is
        # used here (not patched) to exercise that actual behavior.
        calls = []

        def fake_cdc(cafile=None):
            calls.append(cafile)
            return _FakeCtx(x509=5)

        cfg = {"channels": {"telegram": {"ca_bundle": "/bad\x00path.pem"}}}
        with self._patch_config(cfg), \
                patch.dict(notification_utils.os.environ, {}, clear=True), \
                patch.object(notification_utils.ssl, "create_default_context",
                             side_effect=fake_cdc):
            ctx = notification_utils._telegram_ssl_context()  # must not raise
        self.assertEqual(calls, [None])  # NUL path -> isfile False -> default used
        self.assertIsInstance(ctx, _FakeCtx)

    def test_context_swallows_invalid_bundle_content(self):
        # The REAL raise the try/except guards: an EXISTING CA file whose content
        # makes create_default_context raise (e.g. invalid PEM). isfile is True,
        # create_default_context(cafile=...) raises, and the helper must swallow it
        # and fall through to auto-detection.
        calls = []

        def fake_cdc(cafile=None):
            calls.append(cafile)
            if cafile == "/exists/but/invalid.pem":
                raise ssl.SSLError("bad PEM")
            return _FakeCtx(x509=5)

        cfg = {"channels": {"telegram": {"ca_bundle": "/exists/but/invalid.pem"}}}
        with self._patch_config(cfg), \
                patch.dict(notification_utils.os.environ, {}, clear=True), \
                patch.object(notification_utils.os.path, "isfile", return_value=True), \
                patch.object(notification_utils.ssl, "create_default_context",
                             side_effect=fake_cdc):
            ctx = notification_utils._telegram_ssl_context()  # must not raise
        self.assertEqual(calls, ["/exists/but/invalid.pem", None])
        self.assertIsInstance(ctx, _FakeCtx)


class TelegramTLSClassificationTest(TestCase):
    """TLS/transport failures are classified distinctly from HTTP auth — a
    trust-store problem must NEVER read as 'token rejected'
    (m-fix-telegram-tls-verification)."""

    def _patch_token(self, token):
        return patch.object(notification_utils, "resolve_provider_token",
                            return_value=token)

    def _patch_config(self):
        return patch.object(notification_utils, "_get_notification_config",
                            return_value={})

    def test_getme_tls_error_is_not_token_rejected(self):
        url_err = urllib.error.URLError(ssl.SSLCertVerificationError("bad cert"))
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": url_err})):
            result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])
        self.assertNotIn("token rejected", result["error"].lower())
        self.assertIn("tls", result["error"].lower())
        self.assertIn("Install Certificates", result["hint"])
        self.assertIsNone(result["bot"])

    def test_getme_direct_sslerror_is_classified_tls(self):
        # A directly-raised ssl.SSLError (not wrapped in URLError) is still TLS.
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": ssl.SSLError("raw ssl")})):
            result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])
        self.assertIn("tls", result["error"].lower())

    def test_getme_network_error_is_classified_transport(self):
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": urllib.error.URLError("no route")})):
            result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])
        self.assertNotIn("token rejected", result["error"].lower())
        self.assertIn("network", result["error"].lower())

    def test_getupdates_tls_error_preserves_bot_and_classifies(self):
        me = {"ok": True, "result": {"id": 1, "username": "bot"}}
        url_err = urllib.error.URLError(ssl.SSLError("bad"))
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"getMe": me, "getUpdates": url_err})):
            result = notification_utils.discover_telegram_chats()
        self.assertFalse(result["ok"])
        self.assertEqual(result["bot"]["username"], "bot")
        self.assertIn("tls", result["error"].lower())

    def test_send_tls_failure_includes_hint(self):
        url_err = urllib.error.URLError(ssl.SSLError("bad"))
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"sendMessage": url_err})):
            result = notification_utils.send_telegram_test_message("555")
        self.assertFalse(result["ok"])
        self.assertIn("hint", result)
        self.assertIn("Install Certificates", result["hint"])

    def test_send_http_failure_has_no_tls_hint(self):
        err = _http_error(400, {"ok": False, "description": "chat not found"})
        with self._patch_token("T"), self._patch_config(), \
                patch.object(notification_utils.urllib.request, "urlopen",
                             _router({"sendMessage": err})):
            result = notification_utils.send_telegram_test_message("555")
        self.assertFalse(result["ok"])
        self.assertNotIn("hint", result)

    def test_tool_send_failure_preserves_hint(self):
        mcp = MockMCP()
        notifications_tool.register_tools(mcp)
        fn = mcp.tools["notification_discover_telegram_chats"]
        with patch.object(notification_utils, "send_telegram_test_message",
                          return_value={"ok": False, "error": "send failed: TLS ...",
                                        "hint": "run Install Certificates.command"}):
            out = fn("123")
        self.assertFalse(out["success"])
        self.assertEqual(out["hint"], "run Install Certificates.command")


if __name__ == "__main__":
    main()
