"""Configuration, loaded from the environment, plus the safety policy."""

from __future__ import annotations

from email.utils import parseaddr
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Security = Literal["ssl", "starttls", "plain"]


class ConfigError(Exception):
    """Raised when the environment does not describe a usable mail account."""


class Settings(BaseSettings):
    """Everything the server needs, read from environment variables or a .env file.

    Field names map to upper-case environment variables, so ``imap_host`` is
    ``IMAP_HOST``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_security: Security = "ssl"

    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_security: Security = "ssl"

    email_from: str = ""
    email_from_name: str = ""

    email_allow_send: bool = False
    email_allow_delete: bool = False
    email_save_sent_copy: bool = False
    email_recipient_allowlist: list[str] = Field(default_factory=list)
    email_attachment_dir: Path = Path("./attachments")
    email_max_body_chars: int = Field(default=20_000, ge=500, le=1_000_000)

    email_drafts_folder: str = ""
    email_trash_folder: str = ""

    network_timeout: float = Field(default=30.0, gt=0)

    @field_validator("email_recipient_allowlist", mode="before")
    @classmethod
    def _split_allowlist(cls, value: object) -> object:
        """Accept the allowlist as a comma-separated string, which is all env vars can carry."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("imap_security", "smtp_security", mode="before")
    @classmethod
    def _normalize_security(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    # -- derived values ----------------------------------------------------

    @property
    def effective_smtp_username(self) -> str:
        return self.smtp_username or self.imap_username

    @property
    def effective_smtp_password(self) -> str:
        return self.smtp_password or self.imap_password

    @property
    def from_address(self) -> str:
        return self.email_from or self.effective_smtp_username or self.imap_username

    @property
    def attachment_dir(self) -> Path:
        return self.email_attachment_dir.expanduser().resolve()

    @property
    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in output, however an error is worded."""
        return tuple(v for v in (self.imap_password, self.smtp_password) if v)

    # -- validation --------------------------------------------------------

    def require_imap(self) -> None:
        missing = [
            name
            for name, value in (
                ("IMAP_HOST", self.imap_host),
                ("IMAP_USERNAME", self.imap_username),
                ("IMAP_PASSWORD", self.imap_password),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "IMAP is not configured. Set " + ", ".join(missing) + ". See .env.example."
            )

    def require_smtp(self) -> None:
        if not self.smtp_host:
            raise ConfigError("SMTP is not configured. Set SMTP_HOST. See .env.example.")
        if not self.effective_smtp_username or not self.effective_smtp_password:
            raise ConfigError(
                "SMTP has no credentials. Set SMTP_USERNAME and SMTP_PASSWORD, "
                "or the IMAP_* equivalents to reuse them."
            )
        if not self.from_address:
            raise ConfigError("No sender address. Set EMAIL_FROM.")

    # -- safety policy -----------------------------------------------------

    def require_send_allowed(self) -> None:
        if not self.email_allow_send:
            raise ConfigError(
                "Sending is disabled. Set EMAIL_ALLOW_SEND=true to let this server send mail."
            )

    def require_delete_allowed(self) -> None:
        if not self.email_allow_delete:
            raise ConfigError(
                "Moving and deleting messages is disabled. Set EMAIL_ALLOW_DELETE=true to enable it."
            )

    def check_recipients(self, recipients: list[str]) -> None:
        """Reject any recipient outside the allowlist. An empty allowlist allows everyone."""
        if not self.email_recipient_allowlist:
            return
        rejected = [r for r in recipients if not self._is_allowed(r)]
        if rejected:
            raise ConfigError(
                "Recipients not permitted by EMAIL_RECIPIENT_ALLOWLIST: "
                + ", ".join(rejected)
                + ". Allowed: "
                + ", ".join(self.email_recipient_allowlist)
            )

    def _is_allowed(self, recipient: str) -> bool:
        address = _bare_address(recipient).lower()
        if not address:
            return False
        domain = address.rpartition("@")[2]
        for entry in self.email_recipient_allowlist:
            rule = entry.strip().lower()
            if not rule:
                continue
            if rule.startswith("@"):
                if domain == rule[1:]:
                    return True
            elif "@" in rule:
                if address == rule:
                    return True
            elif domain == rule:
                return True
        return False

    def resolve_attachment_path(self, path: str | Path, *, must_exist: bool) -> Path:
        """Resolve a path inside the attachment directory, refusing to escape it."""
        base = self.attachment_dir
        candidate = Path(path).expanduser()
        resolved = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if not resolved.is_relative_to(base):
            raise ConfigError(
                f"Path {path!r} is outside the attachment directory {base}. "
                "Set EMAIL_ATTACHMENT_DIR if you meant a different location."
            )
        if must_exist and not resolved.is_file():
            raise ConfigError(f"No such file: {resolved}")
        return resolved

    def redact(self, text: str) -> str:
        """Strip credentials out of text on its way to the model or a log."""
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text


def _bare_address(recipient: str) -> str:
    """Pull ``a@b.com`` out of ``Name <a@b.com>``."""
    return parseaddr(recipient)[1]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings so a new environment takes effect. Used by tests."""
    get_settings.cache_clear()


def settings_from_env(**overrides: object) -> Settings:
    """Build settings from the current environment, with explicit overrides on top."""
    return Settings(**overrides)  # type: ignore[arg-type]


__all__ = [
    "ConfigError",
    "Security",
    "Settings",
    "get_settings",
    "reset_settings_cache",
    "settings_from_env",
]
