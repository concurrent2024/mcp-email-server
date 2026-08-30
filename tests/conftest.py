"""Shared fixtures: a fake IMAP mailbox and a settings factory."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path

import pytest
from imap_tools.message import MailMessage

from mcp_email import imap_client, server
from mcp_email.config import Settings


def make_raw(
    *,
    subject: str = "Hello",
    sender: str = "Alice <alice@example.com>",
    to: str = "bob@example.com",
    body: str = "Body text",
    html: str | None = None,
    message_id: str = "<original@example.com>",
    attachments: list[tuple[str, bytes, str]] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    """Build a real RFC 5322 message, so parsing is exercised for real."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Mon, 03 Mar 2025 10:00:00 +0000"
    for key, value in (extra_headers or {}).items():
        message[key] = value
    if body:
        message.set_content(body)
    if html:
        if body:
            message.add_alternative(html, subtype="html")
        else:
            message.set_content(html, subtype="html")
    for filename, payload, content_type in attachments or []:
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


def make_message(uid: int, raw: bytes, flags: str = "") -> MailMessage:
    """Wrap raw bytes the way imap-tools receives them from a FETCH response."""
    header = f"1 (UID {uid} FLAGS ({flags}))".encode()
    return MailMessage([(header, raw)])


class FakeFolderManager:
    def __init__(self, mailbox: FakeMailBox) -> None:
        self._mailbox = mailbox

    def list(self, folder: str = "", search_args: str = "*", subscribed_only: bool = False):
        from imap_tools.folder import FolderInfo

        return [
            FolderInfo(name=name, delim="/", flags=tuple(flags))
            for name, flags in self._mailbox.folders.items()
        ]

    def exists(self, folder: str) -> bool:
        return folder in self._mailbox.folders


class FakeMailBox:
    """Enough of the imap-tools mailbox surface for the code under test."""

    def __init__(self, messages: dict[str, list[MailMessage]] | None = None) -> None:
        self.messages: dict[str, list[MailMessage]] = messages or {"INBOX": []}
        self.folders: dict[str, list[str]] = {
            "INBOX": [],
            "Drafts": ["\\Drafts"],
            "Sent": ["\\Sent"],
            "Trash": ["\\Trash"],
            "Archive": [],
        }
        self.selected = "INBOX"
        self.fetch_count = 0
        # Lets a test have mail arrive part-way through a poll loop.
        self.on_fetch: Callable[[FakeMailBox, int], None] | None = None
        self.flag_calls: list[tuple[str, str, bool]] = []
        self.move_calls: list[tuple[str, str]] = []
        self.append_calls: list[tuple[bytes, str, list[str] | None]] = []

    # -- helpers used by the fixtures ------------------------------------

    def add(self, uid: int, raw: bytes, *, folder: str = "INBOX", flags: str = "") -> None:
        self.messages.setdefault(folder, []).append(make_message(uid, raw, flags))

    def _current(self) -> list[MailMessage]:
        return self.messages.setdefault(self.selected, [])

    def _matching(self, criteria: str) -> list[MailMessage]:
        criteria = str(criteria).strip()
        messages = self._current()
        if criteria.startswith("UID "):
            spec = criteria[4:].strip()
            if spec.endswith(":*"):
                low = int(spec[:-2])
                return [m for m in messages if int(m.uid or 0) >= low]
            return [m for m in messages if m.uid == spec]
        return list(messages)

    # -- imap-tools API ---------------------------------------------------

    def fetch(
        self,
        criteria: str = "ALL",
        charset: str = "US-ASCII",
        *,
        limit=None,
        mark_seen: bool = True,
        reverse: bool = False,
        headers_only: bool = False,
        bulk=False,
        sort=None,
        uid_list=None,
    ):
        self.fetch_count += 1
        if self.on_fetch is not None:
            self.on_fetch(self, self.fetch_count)
        found = self._matching(criteria)
        found.sort(key=lambda m: int(m.uid or 0), reverse=reverse)
        if isinstance(limit, int):
            found = found[:limit]
        return iter(found)

    def uids(self, criteria: str = "ALL", charset=None, sort=None) -> list[str]:
        return [m.uid or "" for m in self._matching(criteria)]

    def flag(self, uid_list, flag_set, value: bool, chunks=None):
        flags = [flag_set] if isinstance(flag_set, str) else list(flag_set)
        for flag in flags:
            self.flag_calls.append((str(uid_list), flag, value))

    def move(self, uid_list, destination_folder, chunks=None):
        self.move_calls.append((str(uid_list), str(destination_folder)))

    def append(self, message, folder="INBOX", dt=None, flag_set=None):
        raw = message if isinstance(message, bytes) else message.obj.as_bytes()
        self.append_calls.append((raw, str(folder), list(flag_set) if flag_set else None))
        return ("OK", [b"APPEND completed"])

    def logout(self):
        return ("BYE", [b"logout"])

    @property
    def folder(self) -> FakeFolderManager:
        return FakeFolderManager(self)


@pytest.fixture
def settings_factory(tmp_path: Path):
    """Build Settings that ignore any .env on disk, so tests are hermetic."""

    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "imap_host": "imap.test",
            "imap_port": 993,
            "imap_username": "me@example.com",
            "imap_password": "s3cret-app-password",
            "smtp_host": "smtp.test",
            "smtp_port": 465,
            "email_from": "me@example.com",
            "email_from_name": "Me",
            "email_attachment_dir": tmp_path / "attachments",
            "email_allow_send": True,
            "email_allow_delete": True,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def settings(settings_factory) -> Settings:
    return settings_factory()


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> FakeMailBox:
    """Install a fake mailbox in place of every real IMAP connection."""
    fake = FakeMailBox()

    @contextmanager
    def fake_open(settings, folder: str = "INBOX", *, auth=None) -> Iterator[FakeMailBox]:
        fake.selected = folder or "INBOX"
        yield fake

    monkeypatch.setattr(imap_client, "open_mailbox", fake_open)
    return fake


@pytest.fixture
def configured(settings: Settings):
    """Point the MCP server at the test settings for the duration of a test."""
    server.use_settings(settings)
    try:
        yield settings
    finally:
        server.use_settings(None)
