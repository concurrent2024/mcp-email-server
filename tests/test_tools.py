"""The tools as a client sees them, over an in-memory MCP connection."""

from __future__ import annotations

import pytest
from mcp import Client

from mcp_email import imap_client, server, smtp_client
from mcp_email.server import mcp

from conftest import make_raw

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture outgoing mail instead of delivering it."""
    record: list[dict] = []

    def fake_send(settings, message, *, recipients=None, auth=None):
        record.append({"message": message, "recipients": recipients})
        return {}

    monkeypatch.setattr(smtp_client, "send_message", fake_send)
    return record


def text_of(result) -> str:
    return " ".join(block.text for block in result.content if hasattr(block, "text"))


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


async def test_every_tool_is_advertised():
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {
        "check_connection",
        "list_folders",
        "search_emails",
        "read_email",
        "wait_for_new_emails",
        "download_attachment",
        "mark_email",
        "move_email",
        "delete_email",
        "send_email",
        "save_draft",
    }


async def test_read_tools_are_annotated_read_only():
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["search_emails"].annotations.read_only_hint is True
    assert tools["read_email"].annotations.read_only_hint is True
    assert tools["send_email"].annotations.read_only_hint is False
    assert tools["delete_email"].annotations.destructive_hint is True


# --------------------------------------------------------------------------
# receiving
# --------------------------------------------------------------------------


async def test_search_emails_returns_summaries(configured, mailbox):
    mailbox.add(1, make_raw(subject="Invoice 1"), flags="\\Seen")
    mailbox.add(2, make_raw(subject="Invoice 2"))

    async with Client(mcp) as client:
        result = await client.call_tool("search_emails", {"limit": 5})

    rows = result.structured_content["result"]
    assert [r["subject"] for r in rows] == ["Invoice 2", "Invoice 1"]
    assert rows[1]["seen"] is True
    assert "body" not in rows[0]


async def test_read_email_returns_the_body(configured, mailbox):
    mailbox.add(9, make_raw(subject="Report", body="The full body."))

    async with Client(mcp) as client:
        result = await client.call_tool("read_email", {"uid": "9"})

    detail = result.structured_content
    assert detail["subject"] == "Report"
    assert detail["body"] == "The full body."
    assert detail["truncated"] is False


async def test_read_email_honours_a_smaller_body_limit(configured, mailbox):
    mailbox.add(9, make_raw(body="z" * 3000))

    async with Client(mcp) as client:
        result = await client.call_tool("read_email", {"uid": "9", "max_body_chars": 200})

    assert result.structured_content["truncated"] is True


async def test_read_email_reports_a_missing_uid_to_the_model(configured, mailbox):
    async with Client(mcp) as client:
        result = await client.call_tool("read_email", {"uid": "404"})

    assert result.is_error is True
    assert "404" in text_of(result)


async def test_list_folders_reports_special_use_flags(configured, mailbox):
    async with Client(mcp) as client:
        result = await client.call_tool("list_folders", {})

    folders = {f["name"]: f["flags"] for f in result.structured_content["result"]}
    assert folders["Trash"] == ["\\Trash"]


async def test_wait_for_new_emails_reports_arrivals(configured, mailbox):
    mailbox.add(10, make_raw(subject="already here"))

    def deliver_on_the_second_poll(box, poll: int) -> None:
        if poll == 2:
            box.add(11, make_raw(subject="just arrived"))

    mailbox.on_fetch = deliver_on_the_second_poll

    async with Client(mcp) as client:
        result = await client.call_tool(
            "wait_for_new_emails", {"timeout_seconds": 20, "poll_interval_seconds": 2}
        )

    payload = result.structured_content
    assert payload["timed_out"] is False
    # Mail already in the folder is the baseline, not an arrival.
    assert [m["subject"] for m in payload["messages"]] == ["just arrived"]
    assert payload["waited_seconds"] >= 2


async def test_wait_for_new_emails_times_out_without_new_mail(configured, mailbox):
    mailbox.add(10, make_raw(subject="already here"))

    async with Client(mcp) as client:
        result = await client.call_tool(
            "wait_for_new_emails", {"timeout_seconds": 5, "poll_interval_seconds": 5}
        )

    assert result.structured_content["timed_out"] is True
    assert result.structured_content["messages"] == []


# --------------------------------------------------------------------------
# mailbox state
# --------------------------------------------------------------------------


async def test_mark_email_sets_the_seen_flag(configured, mailbox):
    mailbox.add(3, make_raw())

    async with Client(mcp) as client:
        result = await client.call_tool("mark_email", {"uid": "3", "seen": True})

    assert result.structured_content["action"] == "marked read"
    assert mailbox.flag_calls == [("3", "\\Seen", True)]


async def test_delete_email_moves_the_message_to_trash(configured, mailbox):
    mailbox.add(3, make_raw())

    async with Client(mcp) as client:
        result = await client.call_tool("delete_email", {"uid": "3"})

    assert result.structured_content["detail"] == "Moved to Trash."
    assert mailbox.move_calls == [("3", "Trash")]


async def test_delete_is_refused_when_not_enabled(settings_factory, mailbox):
    server.use_settings(settings_factory(email_allow_delete=False))
    try:
        mailbox.add(3, make_raw())
        async with Client(mcp) as client:
            result = await client.call_tool("delete_email", {"uid": "3"})
    finally:
        server.use_settings(None)

    assert result.is_error is True
    assert "EMAIL_ALLOW_DELETE" in text_of(result)
    assert mailbox.move_calls == []


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


async def test_send_email_delivers_and_reports(configured, mailbox, sent):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "send_email",
            {"to": ["bob@example.com"], "subject": "Hello", "body": "Hi Bob"},
        )

    assert result.is_error is not True
    assert result.structured_content["accepted"] == ["bob@example.com"]
    assert result.structured_content["rejected"] == []
    message = sent[0]["message"]
    assert message["Subject"] == "Hello"
    assert message.get_content().strip() == "Hi Bob"


async def test_send_email_is_refused_when_not_enabled(settings_factory, mailbox, sent):
    server.use_settings(settings_factory(email_allow_send=False))
    try:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "send_email", {"to": ["bob@example.com"], "subject": "x", "body": "y"}
            )
    finally:
        server.use_settings(None)

    assert result.is_error is True
    assert "EMAIL_ALLOW_SEND" in text_of(result)
    assert sent == []


async def test_send_email_is_refused_outside_the_allowlist(settings_factory, mailbox, sent):
    server.use_settings(settings_factory(email_recipient_allowlist="@example.com"))
    try:
        async with Client(mcp) as client:
            allowed = await client.call_tool(
                "send_email", {"to": ["bob@example.com"], "subject": "x", "body": "y"}
            )
            blocked = await client.call_tool(
                "send_email", {"to": ["stranger@evil.com"], "subject": "x", "body": "y"}
            )
    finally:
        server.use_settings(None)

    assert allowed.is_error is not True
    assert blocked.is_error is True
    assert "stranger@evil.com" in text_of(blocked)
    assert len(sent) == 1


async def test_send_email_needs_a_recipient(configured, mailbox, sent):
    async with Client(mcp) as client:
        result = await client.call_tool("send_email", {"subject": "x", "body": "y"})

    assert result.is_error is True
    assert "at least one recipient" in text_of(result)
    assert sent == []


async def test_reply_threads_onto_the_original(configured, mailbox, sent):
    mailbox.add(
        5,
        make_raw(
            subject="Quarterly numbers",
            sender="Alice <alice@example.com>",
            message_id="<original@example.com>",
        ),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "send_email", {"subject": "", "body": "Thanks!", "reply_to_uid": "5"}
        )

    assert result.is_error is not True
    message = sent[0]["message"]
    assert message["To"] == "alice@example.com"
    assert message["Subject"] == "Re: Quarterly numbers"
    assert message["In-Reply-To"] == "<original@example.com>"
    assert message["References"] == "<original@example.com>"


async def test_reply_does_not_stack_re_prefixes(configured, mailbox, sent):
    mailbox.add(5, make_raw(subject="Re: Already a reply", message_id="<o@example.com>"))

    async with Client(mcp) as client:
        await client.call_tool(
            "send_email", {"subject": "", "body": "ack", "reply_to_uid": "5"}
        )

    assert sent[0]["message"]["Subject"] == "Re: Already a reply"


async def test_bcc_recipients_reach_the_envelope_but_not_the_headers(configured, mailbox, sent):
    async with Client(mcp) as client:
        await client.call_tool(
            "send_email",
            {
                "to": ["bob@example.com"],
                "bcc": ["hidden@example.com"],
                "subject": "x",
                "body": "y",
            },
        )

    assert sorted(sent[0]["recipients"]) == ["bob@example.com", "hidden@example.com"]
    assert sent[0]["message"]["Bcc"] is None


async def test_save_draft_appends_to_the_drafts_folder(configured, mailbox, sent):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "save_draft",
            {"to": ["bob@example.com"], "subject": "Later", "body": "Not yet sent"},
        )

    assert result.structured_content["folder"] == "Drafts"
    assert sent == []
    raw, folder, flags = mailbox.append_calls[0]
    assert folder == "Drafts"
    assert flags == ["\\Draft"]
    assert b"Not yet sent" in raw


async def test_save_draft_works_even_when_sending_is_disabled(settings_factory, mailbox, sent):
    server.use_settings(settings_factory(email_allow_send=False))
    try:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "save_draft", {"to": ["bob@example.com"], "subject": "x", "body": "y"}
            )
    finally:
        server.use_settings(None)

    assert result.is_error is not True
    assert len(mailbox.append_calls) == 1


# --------------------------------------------------------------------------
# resource, prompt, and diagnostics
# --------------------------------------------------------------------------


async def test_email_resource_renders_a_message(configured, mailbox):
    mailbox.add(2, make_raw(subject="Readable", body="Body here"))

    async with Client(mcp) as client:
        result = await client.read_resource("email://INBOX/2")

    text = result.contents[0].text
    assert "Subject: Readable" in text
    assert "Body here" in text


async def test_draft_reply_prompt_quotes_the_original(configured, mailbox):
    mailbox.add(2, make_raw(subject="Question", body="Can you confirm?"))

    async with Client(mcp) as client:
        result = await client.get_prompt(
            "draft_reply", {"uid": "2", "instructions": "Say yes."}
        )

    text = result.messages[0].content.text
    assert "> Can you confirm?" in text
    assert "Say yes." in text
    assert "reply_to_uid='2'" in text


async def test_check_connection_never_leaks_the_password(configured, monkeypatch):
    def boom(settings, **kwargs):
        raise RuntimeError(f"LOGIN failed with {settings.imap_password}")

    monkeypatch.setattr(imap_client, "probe", boom)
    monkeypatch.setattr(smtp_client, "probe", boom)

    async with Client(mcp) as client:
        result = await client.call_tool("check_connection", {})

    payload = result.structured_content
    assert payload["imap"]["ok"] is False
    assert "s3cret-app-password" not in text_of(result)
    assert "***" in payload["imap"]["detail"]
    assert payload["sending_enabled"] is True
