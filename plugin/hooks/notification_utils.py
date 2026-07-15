"""
Notification utilities for team-management.

Provides non-blocking notification delivery through extensible channels.
Currently supports Telegram. New channels can be added by subclassing NotificationChannel.

Single source of truth: lives in hooks/, imported by both hooks and MCP server.
"""

import html
import json
import os
import ssl
import threading
import urllib.request
import urllib.parse
import urllib.error
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared_state import resolve_provider_token, get_project_root


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

_config_cache: Optional[Dict[str, Any]] = None
_config_mtime: Optional[float] = None


def _get_project_root() -> Path:
    """Get project root using the canonical shared_state resolver.

    ``shared_state`` is a same-dir sibling imported unconditionally at module top
    (``resolve_provider_token``), so it is always importable here — the former
    ``except ImportError`` fallback was dead code (l-refactor-code-quality-cleanup).
    """
    return get_project_root()


def _load_config() -> Dict[str, Any]:
    """Load config.json with mtime-based cache invalidation."""
    global _config_cache, _config_mtime

    root = _get_project_root()
    config_file = root / "team-management" / "config.json"

    if not config_file.exists():
        return {}

    try:
        current_mtime = config_file.stat().st_mtime
    except OSError:
        return _config_cache or {}

    if _config_cache is not None and _config_mtime == current_mtime:
        return _config_cache

    try:
        _config_cache = json.loads(config_file.read_text(encoding="utf-8"))
        _config_mtime = current_mtime
    except (json.JSONDecodeError, OSError):
        _config_cache = _config_cache or {}

    return _config_cache


def _get_notification_config() -> Dict[str, Any]:
    """Return the notifications section from config (always a dict).

    Guards a non-dict config root and a non-dict ``notifications`` value so
    every caller (incl. the "never raises" Telegram helpers) gets the dict its
    ``-> Dict`` annotation promises, even against a fat-finger config.json.
    """
    cfg = _load_config()
    if not isinstance(cfg, dict):
        return {}
    section = cfg.get("notifications", {})
    return section if isinstance(section, dict) else {}


def _get_prefix() -> str:
    """Return configured message prefix or derive from project directory name."""
    cfg = _get_notification_config()
    prefix = cfg.get("prefix", "")
    if prefix:
        return prefix
    # Fallback: basename of project root
    return _get_project_root().name


# ---------------------------------------------------------------------------
# Telegram TLS context
# ---------------------------------------------------------------------------
#
# The plugin's MCP runtime venv can be built on a Python whose default OpenSSL
# trust store is EMPTY — e.g. a python.org macOS build where "Install
# Certificates.command" was never run. Then ssl.create_default_context() has no
# trust anchors and EVERY chain fails as "self-signed certificate in certificate
# chain", even though the network path is fine (no proxy/MITM). certifi ships in
# the plugin venv (transitive via requests) and carries a valid public CA
# bundle, so we fall back to it. A user genuinely behind a TLS-inspecting proxy
# can point ca_bundle / SSL_CERT_FILE / REQUESTS_CA_BUNDLE at a bundle that
# includes their proxy root.

_ETC_SSL_CERT_FILE = "/etc/ssl/cert.pem"  # macOS/BSD system bundle (what curl uses)


def _telegram_ca_bundle() -> str:
    """Explicitly-configured CA bundle path for Telegram TLS, or ``""``.

    Precedence: config ``notifications.channels.telegram.ca_bundle`` → env
    ``SSL_CERT_FILE`` → env ``REQUESTS_CA_BUNDLE``. Guards every config level
    against a non-dict value so it never raises.
    """
    cfg = _get_notification_config()
    channels = cfg.get("channels", {}) if isinstance(cfg, dict) else {}
    tg = channels.get("telegram", {}) if isinstance(channels, dict) else {}
    if isinstance(tg, dict):
        configured = tg.get("ca_bundle")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    for env_name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        val = os.environ.get(env_name)
        if val and val.strip():
            return val.strip()
    return ""


def _context_has_anchors(ctx: ssl.SSLContext) -> bool:
    """True when ``ctx`` has at least one loaded trust anchor."""
    try:
        return ctx.cert_store_stats().get("x509", 0) > 0
    except Exception:
        return False


def _telegram_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that can verify api.telegram.org across the common
    "empty default CA store" gotcha.

    Precedence: explicit CA bundle (``_telegram_ca_bundle``) → platform default
    context IF it already has trust anchors → ``certifi`` (already shipped in the
    plugin venv via ``requests``) → ``/etc/ssl/cert.pem`` → last-resort default.
    Never raises — any failure returns a plain default context so the caller
    still attempts the connection (and ``_telegram_api_call`` classifies the
    result honestly).
    """
    bundle = _telegram_ca_bundle()
    if bundle:
        try:
            if os.path.isfile(bundle):
                return ssl.create_default_context(cafile=bundle)
        except Exception:
            # An existing-but-invalid CA file makes create_default_context raise
            # (SSLError / OSError) — fall through to auto-detection. (A NUL-byte
            # path can't reach that call: os.path.isfile returns False on Py3.8+
            # rather than raising, so the isfile-inside-try is belt-and-suspenders.)
            pass

    ctx = ssl.create_default_context()
    if _context_has_anchors(ctx):
        return ctx

    # Empty default store: find a valid public bundle.
    try:
        import certifi  # lazy — not importable on a cold session before the venv
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    if os.path.isfile(_ETC_SSL_CERT_FILE):
        try:
            return ssl.create_default_context(cafile=_ETC_SSL_CERT_FILE)
        except Exception:
            pass
    return ctx


# ---------------------------------------------------------------------------
# Abstract channel
# ---------------------------------------------------------------------------

class NotificationChannel(ABC):
    """Interface for a notification delivery channel."""

    @abstractmethod
    def send(self, message: str) -> bool:
        """Send *message* through this channel. Return True on success."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if the channel has all required configuration."""

    @abstractmethod
    def channel_name(self) -> str:
        """Human-readable channel identifier (e.g. 'telegram')."""

    def status(self) -> Dict[str, Any]:
        """Return diagnostic info for this channel."""
        return {
            "channel": self.channel_name(),
            "configured": self.is_configured(),
        }


# ---------------------------------------------------------------------------
# Telegram channel
# ---------------------------------------------------------------------------

class TelegramChannel(NotificationChannel):
    """Deliver notifications via Telegram Bot API (stdlib only)."""

    API_TIMEOUT = 5  # seconds

    def __init__(self, bot_token: str, chat_id: str):
        self._bot_token = bot_token
        self._chat_id = chat_id

    def channel_name(self) -> str:
        return "telegram"

    def is_configured(self) -> bool:
        return bool(self._bot_token) and bool(self._chat_id)

    def send(self, message: str) -> bool:
        if not self.is_configured():
            return False
        try:
            url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": self._chat_id,
                "text": message,
                "parse_mode": "HTML",
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=self.API_TIMEOUT,
                                        context=_telegram_ssl_context()) as resp:
                return resp.status == 200
        except Exception:
            return False

    def status(self) -> Dict[str, Any]:
        base = super().status()
        if self._bot_token:
            # Mask token for display
            masked = self._bot_token[:4] + "..." + self._bot_token[-4:] if len(self._bot_token) > 8 else "****"
            base["bot_token"] = masked
        base["chat_id"] = self._chat_id or ""
        return base


# ---------------------------------------------------------------------------
# Channel factory
# ---------------------------------------------------------------------------

def _build_channels() -> List[NotificationChannel]:
    """Instantiate configured channels from config."""
    cfg = _get_notification_config()
    channels_cfg = cfg.get("channels", {})
    channels: List[NotificationChannel] = []

    # Telegram
    tg = channels_cfg.get("telegram", {})
    if tg.get("enabled", False):
        channels.append(TelegramChannel(
            bot_token=resolve_provider_token("telegram", tg.get("bot_token", "")),
            chat_id=str(tg.get("chat_id", "")),
        ))

    return channels


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_notification(message: str, category: str = "info") -> None:
    """Send a notification through all configured channels (non-blocking).

    Args:
        message: The notification text.
        category: Informational tag (unused by channels today, reserved for
                  future filtering).

    Errors are silently swallowed — notifications must never block the workflow.
    """
    cfg = _get_notification_config()
    if not cfg.get("enabled", False):
        return

    prefix = _get_prefix()
    formatted = f"[{prefix}] {message}" if prefix else message

    channels = _build_channels()
    if not channels:
        return

    def _deliver():
        for ch in channels:
            if ch.is_configured():
                try:
                    ch.send(formatted)
                except Exception:
                    pass

    t = threading.Thread(target=_deliver, daemon=True)
    t.start()


def send_protocol_notification(
    protocol_name: str,
    step_index: int,
    step_name: str,
    total_steps: int,
    is_complete: bool = False,
    summary: str = "",
    task_name: str = "",
) -> None:
    """Send a protocol step transition notification.

    Args:
        protocol_name: Name of the active protocol.
        step_index: 0-based index of the step being entered (or completed).
        step_name: Human-readable step name.
        total_steps: Total number of steps in the protocol.
        is_complete: True when the protocol has finished all steps.
        summary: Step completion summary provided by the AI.
        task_name: Current task name (optional).
    """
    # Per-step notifications can be opted out via notifications.mode == "off".
    # The completion ping (is_complete=True) is never suppressed by mode. A
    # missing `mode` key defaults to "per_step" so old / silently-migrated
    # configs keep today's behaviour.
    if not is_complete and _get_notification_config().get("mode", "per_step") == "off":
        return

    sn = html.escape(step_name)
    task_label = f"[{html.escape(task_name)}] " if task_name else ""
    if is_complete:
        msg = f"{task_label}completed ({total_steps}/{total_steps} steps)"
    else:
        msg = f"{task_label}step {step_index + 1}/{total_steps} — <b>{sn}</b>"
    if summary:
        msg += f"\n{html.escape(summary)}"
    send_notification(msg, category="protocol")


def send_user_notification(message: str) -> None:
    """Send an AI-initiated notification to the user.

    Use this when the AI wants to alert the user about something important
    (e.g. a long-running task finished, a question needs answering).
    The message is HTML-escaped before sending to prevent parse errors.
    """
    send_notification(html.escape(message), category="user")


def get_notification_status() -> Dict[str, Any]:
    """Return diagnostic information about the notification subsystem."""
    cfg = _get_notification_config()
    enabled = cfg.get("enabled", False)
    prefix = _get_prefix()
    channels = _build_channels() if enabled else []

    return {
        "enabled": enabled,
        "prefix": prefix,
        "channels": [ch.status() for ch in channels],
        "configured_channel_count": sum(1 for ch in channels if ch.is_configured()),
    }


# ---------------------------------------------------------------------------
# Telegram chat discovery (config-flow helper)
# ---------------------------------------------------------------------------
#
# Telegram has NO Bot API endpoint that lists all groups a bot belongs to. The
# only discovery path is getUpdates, which returns *recent* updates; we extract
# the distinct chat from each. A chat therefore appears only if it recently sent
# a message the bot can see (privacy-mode groups: a command/mention; DMs: the
# user pressed Start) or the bot was just added to it (my_chat_member). A
# configured webhook makes getUpdates return HTTP 409. These helpers back the
# `notification_discover_telegram_chats` MCP tool used by /team-management:config
# so users can pick a chat id instead of hand-copying it.

_TELEGRAM_API_TIMEOUT = 5  # seconds

# Update-object keys that carry a `chat` we can surface for discovery, in
# preference order. (callback_query is handled separately — its chat is nested
# under .message.)
_TELEGRAM_UPDATE_CHAT_KEYS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
    "chat_member",
)


def _resolve_telegram_token() -> str:
    """Resolve the Telegram bot token (file-first, config fallback).

    Independent of ``notifications.enabled`` and of ``chat_id`` — usable during
    first-time setup before either is configured. Returns ``""`` when no token
    is configured. Guards every config level against a non-dict value (a
    fat-finger edit like ``"notifications": true``) so callers keep their
    "never raises" contract.
    """
    cfg = _get_notification_config()
    channels = cfg.get("channels", {}) if isinstance(cfg, dict) else {}
    tg = channels.get("telegram", {}) if isinstance(channels, dict) else {}
    if not isinstance(tg, dict):
        tg = {}
    return resolve_provider_token("telegram", tg.get("bot_token", "")) or ""


def _telegram_api_call(token: str, method: str,
                       params: Optional[Dict[str, Any]] = None,
                       post: bool = False):
    """Call one Telegram Bot API method (stdlib only).

    Returns ``(payload, error, kind)``. On success ``error``/``kind`` are
    ``None`` and ``payload`` is the parsed JSON dict. On failure ``error`` is a
    human string, ``kind`` classifies it (``"http"`` = a Telegram HTTP status /
    auth rejection, ``"tls"`` = certificate verification failed, ``"transport"``
    = network/DNS/timeout), and ``payload`` is the parsed error body when one was
    returned (Telegram sends a JSON body with ``description``/``error_code`` even
    on 4xx) else ``{}`` — so callers can still inspect ``error_code`` (e.g. 409
    webhook) AND distinguish a bad token from an unreachable/untrusted endpoint.
    """
    url = f"https://api.telegram.org/bot{token}/{method}"
    if post:
        data = urllib.parse.urlencode(params or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
    else:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=_TELEGRAM_API_TIMEOUT,
                                    context=_telegram_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8")), None, None
    except urllib.error.HTTPError as e:
        body: Dict[str, Any] = {}
        try:
            parsed = json.loads(e.read().decode("utf-8"))
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            pass
        desc = body.get("description")
        return body, desc or f"HTTP {e.code}", "http"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        # SSLCertVerificationError subclasses ssl.SSLError; URLError wraps it as
        # .reason. A TLS failure is a trust/proxy problem, NOT a bad token.
        if isinstance(reason, ssl.SSLError):
            return {}, f"TLS verification failed: {reason}", "tls"
        return {}, f"network error: {reason}", "transport"
    except ssl.SSLError as e:
        # A directly-raised SSLError (not wrapped in URLError) is still a TLS
        # failure, not a generic transport error. ssl.SSLError subclasses OSError,
        # so this must precede the OSError catch below.
        return {}, f"TLS verification failed: {e}", "tls"
    except (TimeoutError, ValueError, OSError) as e:
        return {}, f"request failed: {e}", "transport"


def _telegram_chat_title(chat: Dict[str, Any]) -> str:
    """Human label for a chat. Groups/channels carry a ``title``; private chats
    do not, so synthesize one from first/last name and/or username."""
    title = chat.get("title")
    if title:
        return title
    name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
    username = chat.get("username")
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return "(no title)"


def _chat_from_update(upd: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract the ``chat`` dict from one update object, if present."""
    for key in _TELEGRAM_UPDATE_CHAT_KEYS:
        sub = upd.get(key)
        if isinstance(sub, dict) and isinstance(sub.get("chat"), dict):
            return sub["chat"]
    cq = upd.get("callback_query")
    if isinstance(cq, dict):
        msg = cq.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("chat"), dict):
            return msg["chat"]
    return None


def _extract_chats_from_updates(updates: List[Any]) -> List[Dict[str, Any]]:
    """Distinct ``{id, type, title}`` chats from a getUpdates result list,
    deduped by chat id, first-seen order preserved."""
    seen: Dict[Any, bool] = {}
    chats: List[Dict[str, Any]] = []
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        chat = _chat_from_update(upd)
        if not chat:
            continue
        cid = chat.get("id")
        if cid is None or cid in seen:
            continue
        seen[cid] = True
        chats.append({
            "id": cid,
            "type": chat.get("type", "unknown"),
            "title": _telegram_chat_title(chat),
        })
    return chats


def _telegram_transport_error(kind: str, detail: Optional[str],
                              bot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Structured result for a TLS/transport failure — explicitly NOT an auth
    rejection, so the user is not sent to check a token that is fine."""
    if kind == "tls":
        error = detail or "TLS verification failed reaching api.telegram.org."
        hint = ("Python could not verify the Telegram TLS certificate — this is "
                "a trust-store problem, NOT a bad token. Most common cause: a "
                "python.org Python whose CA store was never initialised. Fixes: "
                "run the python.org 'Install Certificates.command'; or set "
                "notifications.channels.telegram.ca_bundle (or the SSL_CERT_FILE "
                "/ REQUESTS_CA_BUNDLE env var) to a CA bundle that includes the "
                "signing root (e.g. your corporate proxy root).")
    else:
        error = detail or "Network error reaching api.telegram.org."
        hint = "Check network connectivity to api.telegram.org, then retry."
    return {"ok": False, "bot": bot, "chats": [], "error": error, "hint": hint}


def discover_telegram_chats() -> Dict[str, Any]:
    """Discover Telegram chats the configured bot can currently reach.

    Validates the token with ``getMe`` (also yields the bot identity), then does
    a NON-mutating ``getUpdates`` read (no ``offset``) and returns the distinct
    chats seen in recent updates. See the module comment for why this is the
    only discovery path and what "recently-active" means.

    Returns a dict::

        {
          "ok": bool,
          "bot": {"id", "username", "first_name"} | None,
          "chats": [{"id", "type", "title"}],   # deduped, may be empty
          "error": str,   # present when ok is False
          "hint": str,    # actionable guidance (token / webhook / empty)
        }

    Never raises — every failure is a structured result so the config flow can
    explain it and fall back to manual entry.
    """
    token = _resolve_telegram_token()
    if not token:
        return {
            "ok": False, "bot": None, "chats": [],
            "error": "No Telegram bot token configured.",
            "hint": "Add your bot token to .claude/state/provider-tokens.json "
                    "under key 'telegram', then retry.",
        }

    # 1. Validate the token (and learn the bot username) via getMe.
    me, err, kind = _telegram_api_call(token, "getMe")
    if kind in ("tls", "transport"):
        return _telegram_transport_error(kind, err, bot=None)
    if err or not (isinstance(me, dict) and me.get("ok")):
        detail = err or (me.get("description") if isinstance(me, dict) else None)
        return {
            "ok": False, "bot": None, "chats": [],
            "error": f"Telegram token rejected: {detail or 'unknown error'}.",
            "hint": "Check the bot token in .claude/state/provider-tokens.json "
                    "(key 'telegram').",
        }
    bot_result = (me.get("result") or {}) if isinstance(me, dict) else {}
    bot = {
        "id": bot_result.get("id"),
        "username": bot_result.get("username"),
        "first_name": bot_result.get("first_name"),
    }

    # 2. Read recent updates (non-mutating: no offset), extract distinct chats.
    updates, err, kind = _telegram_api_call(token, "getUpdates")
    if kind in ("tls", "transport"):
        return _telegram_transport_error(kind, err, bot=bot)
    if err or not (isinstance(updates, dict) and updates.get("ok")):
        code = updates.get("error_code") if isinstance(updates, dict) else None
        if code == 409:
            return {
                "ok": False, "bot": bot, "chats": [],
                "error": "getUpdates is unavailable while a webhook is set (HTTP 409).",
                "hint": "This bot has a webhook configured, so getUpdates cannot "
                        "list chats. Remove it (deleteWebhook) to discover chats, "
                        "or set the chat id manually.",
            }
        detail = err or (updates.get("description") if isinstance(updates, dict) else None)
        return {
            "ok": False, "bot": bot, "chats": [],
            "error": f"getUpdates failed: {detail or 'unknown error'}.",
            "hint": "Retry, or set the chat id manually.",
        }

    chats = _extract_chats_from_updates(updates.get("result") or [])
    result: Dict[str, Any] = {"ok": True, "bot": bot, "chats": chats}
    if not chats:
        result["hint"] = (
            "No chats found in recent updates. Message the bot first (DM: press "
            "Start; group: add the bot and send a message or @mention it), then "
            "retry — Telegram only exposes recently-active chats."
        )
    return result


def send_telegram_test_message(chat_id: str,
                               text: Optional[str] = None) -> Dict[str, Any]:
    """Send one confirmation message to an explicit ``chat_id``.

    Backs the confirm-before-save step of the config flow: after the user picks
    a discovered chat, ping it so they can verify it is the right one before
    ``notifications.channels.telegram.chat_id`` is persisted. Resolves the bot
    token itself. Returns ``{"ok": bool, "error"?: str}``. Never raises.
    """
    token = _resolve_telegram_token()
    if not token:
        return {"ok": False,
                "error": "No Telegram bot token configured "
                         "(.claude/state/provider-tokens.json key 'telegram')."}
    if not str(chat_id).strip():
        return {"ok": False, "error": "chat_id is required."}

    message = text or (
        "✅ team-management: this chat is now set to receive notifications."
    )
    payload, err, kind = _telegram_api_call(
        token, "sendMessage",
        params={"chat_id": str(chat_id).strip(), "text": message,
                "parse_mode": "HTML"},
        post=True,
    )
    if err or not (isinstance(payload, dict) and payload.get("ok")):
        detail = err or (payload.get("description") if isinstance(payload, dict) else None)
        result: Dict[str, Any] = {
            "ok": False, "error": f"send failed: {detail or 'unknown error'}."}
        if kind == "tls":
            result["hint"] = (
                "TLS verification failed — a trust-store problem, not a bad chat "
                "id. Run python.org's 'Install Certificates.command', or set "
                "notifications.channels.telegram.ca_bundle / SSL_CERT_FILE / "
                "REQUESTS_CA_BUNDLE.")
        return result
    return {"ok": True}
