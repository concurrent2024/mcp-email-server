"""Parsing real messages: decoding, HTML fallback, truncation, and search criteria."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from mcp_email import imap_client
from mcp_email.config import ConfigError
from mcp_email.imap_client import MessageNotFoundError, build_criteria, html_to_text

from conftest import make_message, make_raw

# --------------------------------------------------------------------------
# body extraction
# --------------------------------------------------------------------------


def test_html_to_text_keeps_the_words_and_drops_the_markup():
    html = """
    <html><head><style>p { color: red }</style><title>ignored</title></head>
    <body><p>First paragraph.</p><p>Second &amp; last.</p>
    <script>alert('no')</script>
    <ul><li>one</li><li>two</li></ul></body></html>
    """
    text = html_to_text(html)
    assert "First paragraph." in text
    assert "Second & last." in text
    assert "one" in text and "two" in text
    assert "color: red" not in text
    assert "alert" not in text
    assert "ignored" not in text
    assert "<" not in text


def test_paragraphs_are_separated_by_a_blank_line():
    assert html_to_text("<p>one</p><p>two</p>") == "one\n\ntwo"


def test_list_items_and_line_breaks_are_not_double_spaced():
    assert html_to_text("<ul><li>one</li><li>two</li></ul>") == "one\ntwo"
    assert html_to_text("line one<br>line two") == "line one\nline two"


def test_inline_tags_do_not_glue_words_together():
    assert html_to_text("<span>one</span> <span>two</span>") == "one two"
    assert html_to_text("a <b>bold</b> word") == "a bold word"


def test_html_to_text_collapses_runs_of_blank_lines():
    assert "\n\n\n" not in html_to_text("<div></div>" * 20 + "<p>x</p>")


def test_plain_text_body_is_preferred_over_html():
    message = make_message(1, make_raw(body="plain wins", html="<p>html loses</p>"))
    body, converted = imap_client.message_body(message)
    assert body == "plain wins"
    assert converted is False


def test_html_only_body_is_converted_and_flagged():
    message = make_message(1, make_raw(body="", html="<p>only html</p>"))
    body, converted = imap_client.message_body(message)
    assert body == "only html"
    assert converted is True


# --------------------------------------------------------------------------
# conversion to the tool's models
# --------------------------------------------------------------------------


def test_summary_decodes_encoded_headers():
    raw = make_raw(subject="会议纪要", sender="张三 <zhangsan@example.com>", body="正文内容")
    summary = imap_client.to_summary(make_message(7, raw, flags="\\Seen"), "INBOX")
    assert summary.uid == "7"
    assert summary.subject == "会议纪要"
    assert summary.sender == "zhangsan@example.com"
    assert summary.sender_name == "张三"
    assert summary.seen is True
    assert summary.flagged is False
    assert summary.preview == "正文内容"


def test_summary_reports_flags_whatever_their_casing():
    raw = make_raw()
    summary = imap_client.to_summary(make_message(1, raw, flags="\\seen \\FLAGGED"), "INBOX")
    assert summary.seen is True
    assert summary.flagged is True


def test_summary_preview_is_a_single_line_and_bounded():
    raw = make_raw(body="line one\n\nline two\t\tspaced\n" + "x" * 500)
    summary = imap_client.to_summary(make_message(1, raw), "INBOX")
    assert "\n" not in summary.preview
    assert summary.preview.startswith("line one line two spaced")
    assert len(summary.preview) == imap_client.PREVIEW_CHARS


def test_detail_carries_the_threading_headers():
    raw = make_raw(
        message_id="<abc@example.com>",
        extra_headers={"In-Reply-To": "<older@example.com>", "References": "<oldest@example.com>"},
    )
    detail = imap_client.to_detail(make_message(3, raw), "INBOX", 1000)
    assert detail.message_id == "<abc@example.com>"
    assert detail.in_reply_to == "<older@example.com>"
    assert detail.references == "<oldest@example.com>"


def test_detail_lists_attachments_without_their_payloads():
    raw = make_raw(
        attachments=[("report.pdf", b"%PDF-1.4 fake", "application/pdf")],
    )
    detail = imap_client.to_detail(make_message(3, raw), "INBOX", 1000)
    assert [a.filename for a in detail.attachments] == ["report.pdf"]
    assert detail.attachments[0].content_type == "application/pdf"
    assert detail.attachments[0].size == len(b"%PDF-1.4 fake")
    assert "%PDF" not in detail.body


def test_long_bodies_are_truncated_with_a_visible_marker():
    detail = imap_client.to_detail(make_message(1, make_raw(body="y" * 5000)), "INBOX", 100)
    assert detail.truncated is True
    assert "truncated at 100 characters" in detail.body
    assert detail.body.startswith("y" * 100)


def test_short_bodies_are_left_alone():
    detail = imap_client.to_detail(make_message(1, make_raw(body="short")), "INBOX", 100)
    assert detail.truncated is False
    assert detail.body == "short"


# --------------------------------------------------------------------------
# client identification (RFC 2971)
# --------------------------------------------------------------------------


class _StubImapClient:
    def __init__(self, capabilities: tuple[str, ...]) -> None:
        self.capabilities = capabilities
        self.commands: list[tuple[str, tuple]] = []

    def _simple_command(self, name: str, *args):
        self.commands.append((name, args))
        return ("OK", [b""])

    def _untagged_response(self, *args):
        return ("OK", [None])


class _StubMailBox:
    def __init__(self, capabilities: tuple[str, ...]) -> None:
        self.client = _StubImapClient(capabilities)


def test_client_identifies_itself_when_the_server_supports_id():
    """Netease servers refuse every command after login until this is sent."""
    box = _StubMailBox(("IMAP4REV1", "ID"))
    imap_client._identify(box)
    assert box.client.commands[0][0] == "ID"
    assert "mcp-email-server" in box.client.commands[0][1][0]


def test_no_id_command_is_sent_when_unsupported():
    box = _StubMailBox(("IMAP4REV1",))
    imap_client._identify(box)
    assert box.client.commands == []


def test_a_failing_id_command_does_not_break_the_connection():
    box = _StubMailBox(("IMAP4REV1", "ID"))
    box.client._simple_command = lambda *a: (_ for _ in ()).throw(OSError("refused"))
    imap_client._identify(box)


# --------------------------------------------------------------------------
# search criteria
# --------------------------------------------------------------------------


def test_no_filters_searches_everything():
    assert build_criteria() == "ALL"


def test_criteria_combine_with_and():
    criteria = build_criteria(
        from_contains="alice@example.com", subject_contains="invoice", unread_only=True
    )
    assert "alice@example.com" in criteria
    assert "invoice" in criteria
    assert "UNSEEN" in criteria


def test_date_filters_use_imap_date_syntax():
    criteria = build_criteria(since=date(2025, 3, 1), before=date(2025, 4, 1))
    assert "SINCE 1-Mar-2025" in criteria
    assert "BEFORE 1-Apr-2025" in criteria


# --------------------------------------------------------------------------
# operations against the fake mailbox
# --------------------------------------------------------------------------


def test_search_returns_newest_first(settings, mailbox):
    for uid in (1, 2, 3):
        mailbox.add(uid, make_raw(subject=f"msg {uid}"))
    results = imap_client.search(settings, folder="INBOX", criteria="ALL", limit=2)
    assert [r.uid for r in results] == ["3", "2"]


def test_read_raises_a_clear_error_for_an_unknown_uid(settings, mailbox):
    mailbox.add(1, make_raw())
    with pytest.raises(MessageNotFoundError, match="UID 99"):
        imap_client.read(settings, "99")


def test_set_flags_requires_something_to_change(settings, mailbox):
    mailbox.add(1, make_raw())
    with pytest.raises(ConfigError, match="Nothing to change"):
        imap_client.set_flags(settings, "1")


def test_set_flags_applies_both_changes(settings, mailbox):
    mailbox.add(1, make_raw())
    done = imap_client.set_flags(settings, "1", seen=True, flagged=False)
    assert done == ["marked read", "unflagged"]
    assert mailbox.flag_calls == [("1", "\\Seen", True), ("1", "\\Flagged", False)]


def test_move_refuses_a_folder_that_does_not_exist(settings, mailbox):
    mailbox.add(1, make_raw())
    with pytest.raises(ConfigError, match="does not exist"):
        imap_client.move(settings, "1", folder="INBOX", destination="Nowhere")


def test_special_folders_are_found_by_their_flags(settings, mailbox):
    assert imap_client.find_special_folder(settings, "trash") == "Trash"
    assert imap_client.find_special_folder(settings, "drafts") == "Drafts"
    assert imap_client.find_special_folder(settings, "sent") == "Sent"


def test_fetch_above_uid_ignores_the_boundary_message(settings, mailbox):
    """'UID n:*' always returns the highest UID, even when nothing is above n."""
    mailbox.add(5, make_raw(subject="old"))
    assert imap_client.fetch_above_uid(settings, after_uid=5) == []

    mailbox.add(6, make_raw(subject="new"))
    arrived = imap_client.fetch_above_uid(settings, after_uid=5)
    assert [m.subject for m in arrived] == ["new"]


def test_latest_uid_is_zero_for_an_empty_folder(settings, mailbox):
    assert imap_client.latest_uid(settings) == 0
    mailbox.add(4, make_raw())
    assert imap_client.latest_uid(settings) == 4


def test_download_attachment_writes_into_the_attachment_directory(settings, mailbox):
    mailbox.add(1, make_raw(attachments=[("report.pdf", b"%PDF fake", "application/pdf")]))
    result = imap_client.download_attachment(settings, "1")
    assert result.filename == "report.pdf"
    assert result.path.startswith(str(settings.attachment_dir))
    assert Path(result.path).read_bytes() == b"%PDF fake"


def test_download_attachment_sanitises_a_hostile_filename(settings, mailbox):
    mailbox.add(1, make_raw(attachments=[("../../evil.sh", b"rm -rf /", "text/x-sh")]))
    result = imap_client.download_attachment(settings, "1")
    assert result.filename == "evil.sh"
    assert result.path.startswith(str(settings.attachment_dir))


def test_download_attachment_asks_which_one_when_there_are_several(settings, mailbox):
    mailbox.add(
        1,
        make_raw(
            attachments=[
                ("a.txt", b"a", "text/plain"),
                ("b.txt", b"b", "text/plain"),
            ]
        ),
    )
    with pytest.raises(ConfigError, match="pass filename"):
        imap_client.download_attachment(settings, "1")

    result = imap_client.download_attachment(settings, "1", filename="b.txt")
    assert result.filename == "b.txt"


def test_download_attachment_reports_a_message_with_none(settings, mailbox):
    mailbox.add(1, make_raw())
    with pytest.raises(MessageNotFoundError, match="no attachments"):
        imap_client.download_attachment(settings, "1")
