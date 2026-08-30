"""Authentication strategies for IMAP and SMTP.

Everything credential-related is funnelled through :class:`AuthProvider` so that
adding OAuth2 later means writing one more subclass here; ``imap_client`` and
``smtp_client`` never look at a password directly.
"""

from __future__ import annotations

import smtplib
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .config import ConfigError, Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from imap_tools.mailbox import BaseMailBox


class AuthProvider(ABC):
    """Knows how to log a connection in, whatever the mechanism."""

    @abstractmethod
    def authenticate_imap(self, mailbox: BaseMailBox, username: str, initial_folder: str) -> None:
        """Log into an already-connected imap-tools mailbox."""

    @abstractmethod
    def authenticate_smtp(self, smtp: smtplib.SMTP, username: str) -> None:
        """Log into an already-connected, already-secured SMTP session."""


class PasswordAuth(AuthProvider):
    """Plain ``LOGIN`` with a password or provider-issued app password."""

    def __init__(self, imap_password: str, smtp_password: str) -> None:
        self._imap_password = imap_password
        self._smtp_password = smtp_password

    def authenticate_imap(self, mailbox: BaseMailBox, username: str, initial_folder: str) -> None:
        mailbox.login(username, self._imap_password, initial_folder=initial_folder)

    def authenticate_smtp(self, smtp: smtplib.SMTP, username: str) -> None:
        smtp.login(username, self._smtp_password)


class XOAuth2Auth(AuthProvider):
    """SASL XOAUTH2, for Gmail and Microsoft 365.

    The wire format is already correct on both sides; what is missing is the
    token itself. Supply an access token (via ``token_provider``) and this works
    today. The unimplemented part is the surrounding OAuth2 dance -- obtaining
    the refresh token and exchanging it -- which is the second phase of this
    project.
    """

    def __init__(self, token_provider: object | None = None) -> None:
        self._token_provider = token_provider

    def _access_token(self, username: str) -> str:
        if self._token_provider is None:
            raise ConfigError(
                "OAuth2 is not configured. This server currently supports password "
                "and app-password authentication; set IMAP_PASSWORD / SMTP_PASSWORD."
            )
        return self._token_provider(username)  # type: ignore[operator]

    def authenticate_imap(self, mailbox: BaseMailBox, username: str, initial_folder: str) -> None:
        mailbox.xoauth2(username, self._access_token(username), initial_folder=initial_folder)

    def authenticate_smtp(self, smtp: smtplib.SMTP, username: str) -> None:
        token = self._access_token(username)
        payload = f"user={username}\x01auth=Bearer {token}\x01\x01"
        smtp.auth("XOAUTH2", lambda challenge=None: payload, initial_response_ok=True)


def build_auth(settings: Settings) -> AuthProvider:
    """Pick the authentication strategy the configuration calls for."""
    return PasswordAuth(
        imap_password=settings.imap_password,
        smtp_password=settings.effective_smtp_password,
    )


__all__ = ["AuthProvider", "PasswordAuth", "XOAuth2Auth", "build_auth"]
