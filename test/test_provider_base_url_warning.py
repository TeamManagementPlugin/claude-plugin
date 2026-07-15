#!/usr/bin/env python3
"""l-fix-security-hardening-residuals — Finding 2: base_url scheme warning.

`IssueTrackingProvider._warn_if_insecure_base_url` loudly warns (stderr +
provider log) when a provider's `base_url` is non-https and the host is NOT
loopback — the API token would otherwise travel in a cleartext header. Decision
(Max, 2026-07-14): WARN, never reject (self-hosted-over-http stays supported);
loopback hosts (localhost / the whole 127.0.0.0/8 range / ::1) are exempt.

The method is exercised UNBOUND against a tiny object exposing `provider_name`
(the base class has many abstract methods — a full stub would be noise).

Run with: python3 -m pytest test/test_provider_base_url_warning.py -v
"""
import io
import sys
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _REPO / "plugin" / "hooks"
for _p in (str(_REPO), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import issue_provider_base  # noqa: E402
from issue_provider_base import IssueTrackingProvider  # noqa: E402

_WARN = IssueTrackingProvider._warn_if_insecure_base_url


class TestInsecureBaseUrlWarning(unittest.TestCase):
    def _run(self, base_url, glp=None):
        """Call the unbound warning method against a minimal fake `self`.
        Returns (stderr_text, logger_factory_mock)."""
        fake = types.SimpleNamespace(provider_name="gitlab")
        glp = glp if glp is not None else MagicMock()
        buf = io.StringIO()
        with patch.object(issue_provider_base, "get_provider_logger", glp), \
                redirect_stderr(buf):
            _WARN(fake, base_url)
        return buf.getvalue(), glp

    def _assert_warns(self, base_url):
        stderr, glp = self._run(base_url)
        self.assertIn("not https", stderr, base_url)
        self.assertIn("SECURITY", stderr, base_url)
        glp.assert_called_once_with("gitlab.log")     # provider-log channel fired
        glp.return_value.assert_called_once()

    def _assert_silent(self, base_url):
        stderr, glp = self._run(base_url)
        self.assertEqual(stderr, "", base_url)
        glp.assert_not_called()

    def test_external_http_warns_on_both_channels(self):
        self._assert_warns("http://gitlab.example.com")

    def test_external_http_private_ip_warns(self):
        # Private but NOT loopback → still exposed on the LAN → warn.
        self._assert_warns("http://192.168.1.10:8080")

    def test_scheme_less_base_url_warns(self):
        self._assert_warns("gitlab.example.com:8080")

    def test_https_is_silent(self):
        self._assert_silent("https://gitlab.example.com")

    def test_localhost_exempt(self):
        self._assert_silent("http://localhost:8080")

    def test_127_0_0_1_exempt(self):
        self._assert_silent("http://127.0.0.1:3000/api")

    def test_127_0_0_2_exempt(self):
        # Whole 127.0.0.0/8 loopback range is exempt (ipaddress-based check).
        self._assert_silent("http://127.0.0.2/api/v4")

    def test_ipv6_loopback_exempt(self):
        self._assert_silent("http://[::1]:8080/api/v4")

    def test_logger_failure_is_non_fatal(self):
        # If the provider-log factory raises, the warning still reaches stderr
        # and no exception escapes.
        boom = MagicMock(side_effect=RuntimeError("log dir unwritable"))
        stderr, _ = self._run("http://gitlab.example.com", glp=boom)
        self.assertIn("not https", stderr)

    def test_warning_redacts_userinfo_and_query(self):
        # The warning itself must NOT leak a token embedded in the base_url's
        # userinfo or query string into stderr / the provider log (codex P2).
        stderr, glp = self._run(
            "http://oauth2:s3cr3tTOKEN@gitlab.internal/api/v4?private_token=QUERYSECRET"
        )
        self.assertIn("not https", stderr)
        self.assertNotIn("s3cr3tTOKEN", stderr)
        self.assertNotIn("QUERYSECRET", stderr)
        self.assertIn("gitlab.internal", stderr)     # host still identifiable
        # Same redaction on the provider-log channel.
        logged = glp.return_value.call_args[0][0]
        self.assertNotIn("s3cr3tTOKEN", logged)
        self.assertNotIn("QUERYSECRET", logged)

    def test_warning_redacts_userinfo_scheme_less(self):
        # A scheme-less URL carrying userinfo (no `//`) must still be redacted —
        # the earlier `//`-anchored fallback leaked it (code-review W1).
        stderr, glp = self._run("oauth2:s3cr3tTOKEN@gitlab.internal/api/v4")
        self.assertIn("not https", stderr)
        self.assertNotIn("s3cr3tTOKEN", stderr)
        self.assertIn("gitlab.internal", stderr)
        logged = glp.return_value.call_args[0][0]
        self.assertNotIn("s3cr3tTOKEN", logged)

    def test_warning_redacts_userinfo_containing_slash(self):
        # A credential containing '/' (base64 tokens can) must not leak. The
        # earlier string partition assumed no '/' before the '@'; the hybrid
        # prefers urlparse's authority parse, sending the '/'-bearing token into
        # the (dropped) path so only scheme://host is shown.
        stderr, glp = self._run("http://user:tok/enSECRET@host.internal/api")
        self.assertIn("not https", stderr)
        self.assertNotIn("enSECRET", stderr)
        self.assertNotIn("tok/en", stderr)
        logged = glp.return_value.call_args[0][0]
        self.assertNotIn("enSECRET", logged)

    def test_warning_redacts_scheme_less_userinfo_with_slash(self):
        # Scheme-less URL whose userinfo itself contains '/' — the fallback now
        # strips everything up to the LAST '@', so no credential survives.
        stderr, glp = self._run("oauth2:tok/enSECRET@gitlab.internal/api/v4")
        self.assertIn("not https", stderr)
        self.assertNotIn("enSECRET", stderr)
        self.assertNotIn("tok/en", stderr)
        self.assertIn("gitlab.internal", stderr)
        logged = glp.return_value.call_args[0][0]
        self.assertNotIn("enSECRET", logged)

    def test_warning_still_fires_for_invalid_port(self):
        # An out-of-range port must NOT silently suppress the warning
        # (code-review N1: parsed.port would raise → swallowed by outer except).
        stderr, _ = self._run("http://gitlab.internal:99999/api")
        self.assertIn("not https", stderr)
        self.assertIn("gitlab.internal", stderr)

    def test_never_raises_on_malformed_url(self):
        # A garbage value must not break construction.
        stderr, _ = self._run("::::not a url::::")
        self.assertIsInstance(stderr, str)


if __name__ == "__main__":
    unittest.main()
