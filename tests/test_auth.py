"""Authentication providers: password login and the XOAUTH2 skeleton."""

from __future__ import annotations

import pytest

from mcp_email.auth import PasswordAuth, XOAuth2Auth, build_auth
from mcp_email.config import ConfigError


class _FakeImapBox:
    def __init__(self) -> None:
        self.login_calls: list[tuple[str, str, str]] = []
        self.xoauth_calls: list[tuple[str, str, str]] = []

    def login(self, username: str, password: str, initial_folder: str = "INBOX") -> None:
        self.login_calls.append((username, password, initial_folder))

    def xoauth2(self, username: str, token: str, initial_folder: str = "INBOX") -> None:
        self.xoauth_calls.append((username, token, initial_folder))


class _FakeSmtp:
    def __init__(self) -> None:
        self.login_calls: list[tuple[str, str]] = []
        self.auth_calls: list[tuple[str, str, bool]] = []

    def login(self, username: str, password: str) -> None:
        self.login_calls.append((username, password))

    def auth(self, mechanism: str, callback, initial_response_ok: bool = True) -> None:
        self.auth_calls.append((mechanism, callback(), initial_response_ok))


def test_password_auth_logs_into_imap_and_smtp():
    auth = PasswordAuth(imap_password="imap-secret", smtp_password="smtp-secret")
    box = _FakeImapBox()
    smtp = _FakeSmtp()

    auth.authenticate_imap(box, "me@example.com", "INBOX")
    auth.authenticate_smtp(smtp, "me@example.com")

    assert box.login_calls == [("me@example.com", "imap-secret", "INBOX")]
    assert smtp.login_calls == [("me@example.com", "smtp-secret")]


def test_xoauth2_without_a_token_provider_explains_itself():
    auth = XOAuth2Auth()
    with pytest.raises(ConfigError, match="OAuth2 is not configured"):
        auth.authenticate_smtp(_FakeSmtp(), "me@example.com")


def test_xoauth2_uses_the_token_provider_on_both_sides():
    auth = XOAuth2Auth(token_provider=lambda username: f"token-for-{username}")
    box = _FakeImapBox()
    smtp = _FakeSmtp()

    auth.authenticate_imap(box, "me@example.com", "Drafts")
    auth.authenticate_smtp(smtp, "me@example.com")

    assert box.xoauth_calls == [("me@example.com", "token-for-me@example.com", "Drafts")]
    mechanism, payload, initial = smtp.auth_calls[0]
    assert mechanism == "XOAUTH2"
    assert initial is True
    assert payload == "user=me@example.com\x01auth=Bearer token-for-me@example.com\x01\x01"


def test_build_auth_returns_password_auth(settings):
    auth = build_auth(settings)
    assert isinstance(auth, PasswordAuth)
