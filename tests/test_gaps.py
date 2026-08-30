"""Coverage for tool-layer gaps, transports, and mailbox edge cases."""

from __future__ import annotations

import pytest
from mcp import Client

from mcp_email import imap_client, server, smtp_client
from mcp_email.server import mcp

from conftest import make_raw


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    record: list[dict] = []

    def fake_send(settings, message, *, recipients=None, auth=None):
        record.append({"message": message, "recipients": recipients})
        return {}

    monkeypatch.setattr(smtp_client, "send_message", fake_send)
    return record


def text_of(result) -> str:
    return " ".join(block.text for block in result.content if hasattr(block, "text"))


@pytest.mark.anyio
async def test_search_emails_applies_unread_and_subject_filters(configured, mailbox):
    mailbox.add(1, make_raw(subject="Invoice paid"), flags="\\Seen")
    mailbox.add(2, make_raw(subject="Invoice due"))
    mailbox.add(3, make_raw(subject="Unrelated"))

    async with Client(mcp) as client:
        result = await client.call_tool(
            "search_emails",
            {"unread_only": True, "subject_contains": "Invoice", "limit": 10},
        )

    rows = result.structured_content["result"]
    assert [r["subject"] for r in rows] == ["Invoice due"]


@pytest.mark.anyio
async def test_read_email_can_mark_the_message_seen(configured, mailbox):
    mailbox.add(9, make_raw(body="secret"))

    async with Client(mcp) as client:
        await client.call_tool("read_email", {"uid": "9", "mark_as_read": True})

    assert ("9", "\\Seen", True) in mailbox.flag_calls


@pytest.mark.anyio
async def test_download_attachment_via_the_tool(configured, mailbox):
    mailbox.add(8, make_raw(attachments=[("report.pdf", b"%PDF fake", "application/pdf")]))

    async with Client(mcp) as client:
        result = await client.call_tool("download_attachment", {"uid": "8"})

    payload = result.structured_content
    assert payload["filename"] == "report.pdf"
    assert payload["size"] == len(b"%PDF fake")
    assert payload["path"].endswith("report.pdf")


@pytest.mark.anyio
async def test_move_email_moves_to_the_destination(configured, mailbox):
    mailbox.add(4, make_raw())

    async with Client(mcp) as client:
        result = await client.call_tool("move_email", {"uid": "4", "destination": "Archive"})

    assert result.is_error is not True
    assert result.structured_content["action"] == "moved"
    assert "Archive" in result.structured_content["detail"]
    assert mailbox.move_calls == [("4", "Archive")]


@pytest.mark.anyio
async def test_move_email_is_refused_when_delete_is_disabled(settings_factory, mailbox):
    server.use_settings(settings_factory(email_allow_delete=False))
    try:
        mailbox.add(4, make_raw())
        async with Client(mcp) as client:
            result = await client.call_tool(
                "move_email", {"uid": "4", "destination": "Archive"}
            )
    finally:
        server.use_settings(None)

    assert result.is_error is True
    assert "EMAIL_ALLOW_DELETE" in text_of(result)
    assert mailbox.move_calls == []


@pytest.mark.anyio
async def test_delete_email_fails_when_there_is_no_trash_folder(configured, mailbox):
    mailbox.folders.pop("Trash", None)
    mailbox.add(3, make_raw())

    async with Client(mcp) as client:
        result = await client.call_tool("delete_email", {"uid": "3"})

    assert result.is_error is True
    assert "No Trash folder" in text_of(result)
    assert mailbox.move_calls == []


@pytest.mark.anyio
async def test_send_email_saves_a_copy_when_configured(settings_factory, mailbox, sent):
    server.use_settings(settings_factory(email_save_sent_copy=True))
    try:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "send_email",
                {"to": ["bob@example.com"], "subject": "Hello", "body": "Hi Bob"},
            )
    finally:
        server.use_settings(None)

    assert result.is_error is not True
    assert result.structured_content["saved_to_folder"] == "Sent"
    assert mailbox.append_calls[0][1] == "Sent"
    assert mailbox.append_calls[0][2] == ["\\Seen"]


@pytest.mark.anyio
async def test_send_email_does_not_fail_if_the_sent_copy_cannot_be_saved(
    settings_factory, mailbox, sent
):
    server.use_settings(settings_factory(email_save_sent_copy=True))
    mailbox.folders.pop("Sent", None)
    try:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "send_email",
                {"to": ["bob@example.com"], "subject": "Hello", "body": "Hi Bob"},
            )
    finally:
        server.use_settings(None)

    assert result.is_error is not True
    assert result.structured_content["saved_to_folder"] is None
    assert mailbox.append_calls == []


@pytest.mark.anyio
async def test_save_draft_fails_when_there_is_no_drafts_folder(configured, mailbox, sent):
    mailbox.folders.pop("Drafts", None)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "save_draft",
            {"to": ["bob@example.com"], "subject": "Later", "body": "Not yet sent"},
        )

    assert result.is_error is True
    assert "No Drafts folder" in text_of(result)
    assert mailbox.append_calls == []


def test_make_mailbox_picks_the_transport(monkeypatch, settings_factory):
    recorded: list[str] = []

    class _Fake:
        def __init__(self, host, port, timeout=None):
            recorded.append(type(self).__name__)

    class FakeSSL(_Fake):
        pass

    class FakeStartTls(_Fake):
        pass

    class FakePlain(_Fake):
        pass

    monkeypatch.setattr(imap_client, "MailBox", FakeSSL)
    monkeypatch.setattr(imap_client, "MailBoxStartTls", FakeStartTls)
    monkeypatch.setattr(imap_client, "MailBoxUnencrypted", FakePlain)

    imap_client._make_mailbox(settings_factory(imap_security="ssl"))
    imap_client._make_mailbox(settings_factory(imap_security="starttls"))
    imap_client._make_mailbox(settings_factory(imap_security="plain"))
    assert recorded == ["FakeSSL", "FakeStartTls", "FakePlain"]


def test_imap_probe_reports_inbox_size_without_the_password(settings, mailbox):
    mailbox.add(1, make_raw())
    mailbox.add(2, make_raw())
    detail = imap_client.probe(settings)
    assert "imap.test" in detail
    assert "2 message" in detail
    assert settings.imap_password not in detail


def test_connect_uses_smtp_ssl(monkeypatch, settings_factory):
    created: dict[str, object] = {}

    class FakeSSL:
        def __init__(self, host, port, timeout=None, context=None):
            created.update(host=host, port=port, timeout=timeout)
            self._host = host

    monkeypatch.setattr(smtp_client.smtplib, "SMTP_SSL", FakeSSL)
    settings = settings_factory(smtp_security="ssl", smtp_port=465)
    conn = smtp_client._connect(settings)
    assert created["host"] == "smtp.test"
    assert created["port"] == 465
    assert isinstance(conn, FakeSSL)


def test_connect_upgrades_with_starttls(monkeypatch, settings_factory):
    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.calls: list[str] = []

        def ehlo(self):
            self.calls.append("ehlo")

        def starttls(self, context=None):
            self.calls.append("starttls")

        def close(self):
            self.calls.append("close")

    monkeypatch.setattr(smtp_client.smtplib, "SMTP", FakeSMTP)
    settings = settings_factory(smtp_security="starttls", smtp_port=587)
    conn = smtp_client._connect(settings)
    assert conn.calls == ["ehlo", "starttls", "ehlo"]
