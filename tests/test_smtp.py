"""Message composition, and a real send against a local SMTP server."""

from __future__ import annotations

import asyncio
import smtplib
import socket
from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest
from aiosmtpd.controller import Controller

from mcp_email import smtp_client
from mcp_email.config import ConfigError
from mcp_email.smtp_client import OutgoingMessage, build_message, normalize_recipients

# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------


def test_normalize_recipients_splits_a_packed_string():
    assert normalize_recipients(["a@x.com, b@x.com"]) == ["a@x.com", "b@x.com"]


def test_normalize_recipients_keeps_display_names_and_drops_duplicates():
    assert normalize_recipients(["Alice <a@x.com>", "a@x.com ", "", "Alice <a@x.com>"]) == [
        "Alice <a@x.com>",
        "a@x.com",
    ]


def test_build_message_sets_the_envelope(settings):
    message = build_message(
        settings,
        OutgoingMessage(to=["bob@example.com"], subject="Hi", body="Hello there"),
    )
    assert message["From"] == "Me <me@example.com>"
    assert message["To"] == "bob@example.com"
    assert message["Subject"] == "Hi"
    assert message["Message-ID"].endswith("example.com>")
    assert message.get_content().strip() == "Hello there"


def test_build_message_refuses_a_message_with_no_recipients(settings):
    with pytest.raises(ConfigError, match="at least one recipient"):
        build_message(settings, OutgoingMessage(to=[], subject="Hi", body="x"))


def test_build_message_bcc_is_not_written_into_the_headers(settings):
    message = build_message(
        settings,
        OutgoingMessage(
            to=["bob@example.com"], bcc=["secret@example.com"], subject="Hi", body="x"
        ),
    )
    assert message["Bcc"] is None
    assert "secret@example.com" not in message.as_string()


def test_build_message_adds_an_html_alternative(settings):
    message = build_message(
        settings,
        OutgoingMessage(
            to=["bob@example.com"],
            subject="Hi",
            body="plain version",
            html="<p>rich version</p>",
        ),
    )
    assert message.get_content_type() == "multipart/alternative"
    types = [part.get_content_type() for part in message.iter_parts()]
    assert types == ["text/plain", "text/html"]


def test_build_message_attaches_files_from_the_attachment_directory(settings):
    settings.attachment_dir.mkdir(parents=True, exist_ok=True)
    (settings.attachment_dir / "notes.txt").write_bytes(b"file contents")

    message = build_message(
        settings,
        OutgoingMessage(
            to=["bob@example.com"], subject="Hi", body="see attached", attachments=["notes.txt"]
        ),
    )
    attachments = list(message.iter_attachments())
    assert [a.get_filename() for a in attachments] == ["notes.txt"]
    assert attachments[0].get_payload(decode=True) == b"file contents"
    assert attachments[0].get_content_type() == "text/plain"


def test_build_message_refuses_an_attachment_outside_the_directory(settings):
    settings.attachment_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ConfigError, match="outside the attachment directory"):
        build_message(
            settings,
            OutgoingMessage(
                to=["bob@example.com"], subject="Hi", body="x", attachments=["../../etc/passwd"]
            ),
        )


def test_build_message_threads_a_reply(settings):
    message = build_message(
        settings,
        OutgoingMessage(
            to=["alice@example.com"],
            subject="Re: Question",
            body="Answer",
            in_reply_to="<original@example.com>",
            references="<older@example.com> <original@example.com>",
        ),
    )
    assert message["In-Reply-To"] == "<original@example.com>"
    assert message["References"] == "<older@example.com> <original@example.com>"


# --------------------------------------------------------------------------
# a real send
# --------------------------------------------------------------------------


class _Collector:
    """aiosmtpd handler that keeps whatever it is given."""

    def __init__(self) -> None:
        self.received: list[dict[str, object]] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 - aiosmtpd's API
        self.received.append(
            {
                "mail_from": envelope.mail_from,
                "rcpt_tos": list(envelope.rcpt_tos),
                "content": bytes(envelope.content),
            }
        )
        return "250 Message accepted"


def _free_port() -> int:
    """Pick a port up front: aiosmtpd's readiness probe dials the port it was
    given, so it cannot be left as 0."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


SMTP_USER = "me@example.com"
SMTP_PASSWORD = "s3cret-app-password"


def _authenticator(server, session, envelope, mechanism, auth_data):
    """Accept only the test credentials, so the login path is really exercised."""
    from aiosmtpd.smtp import AuthResult, LoginPassword

    if not isinstance(auth_data, LoginPassword):
        return AuthResult(success=False, handled=False)
    username = auth_data.login.decode()
    password = auth_data.password.decode()
    if username == SMTP_USER and password == SMTP_PASSWORD:
        return AuthResult(success=True)
    return AuthResult(success=False, handled=False)


@pytest.fixture
def smtp_server():
    """A local SMTP server on a free port, so sending is exercised end to end."""
    collector = _Collector()
    controller = Controller(
        collector,
        hostname="127.0.0.1",
        port=_free_port(),
        authenticator=_authenticator,
        auth_require_tls=False,
    )
    controller.start()
    try:
        yield controller, collector
    finally:
        controller.stop()


def test_send_message_delivers_to_a_real_server(settings_factory, smtp_server):
    controller, collector = smtp_server
    settings = settings_factory(
        smtp_host=controller.hostname,
        smtp_port=controller.port,
        smtp_security="plain",
    )
    outgoing = OutgoingMessage(
        to=["bob@example.com", "Carol <carol@example.com>"],
        cc=["dave@example.com"],
        bcc=["hidden@example.com"],
        subject="Integration",
        body="Real delivery",
    )
    message = build_message(settings, outgoing)
    recipients = smtp_client.bare_addresses(
        smtp_client.normalize_recipients(outgoing.to + outgoing.cc + outgoing.bcc)
    )

    refused = smtp_client.send_message(settings, message, recipients=recipients)

    assert refused == {}
    assert len(collector.received) == 1
    delivered = collector.received[0]
    assert delivered["mail_from"] == "me@example.com"
    # Bcc must reach the envelope even though it never appears in the headers.
    assert sorted(delivered["rcpt_tos"]) == [
        "bob@example.com",
        "carol@example.com",
        "dave@example.com",
        "hidden@example.com",
    ]
    parsed = message_from_bytes(delivered["content"])
    assert parsed["Subject"] == "Integration"
    assert parsed["Bcc"] is None


def test_send_message_carries_a_non_ascii_subject_and_body(settings_factory, smtp_server):
    controller, collector = smtp_server
    settings = settings_factory(
        smtp_host=controller.hostname,
        smtp_port=controller.port,
        smtp_security="plain",
        email_from_name="张三",
    )
    message = build_message(
        settings,
        OutgoingMessage(to=["bob@example.com"], subject="会议纪要", body="你好，这是正文。"),
    )

    smtp_client.send_message(settings, message, recipients=["bob@example.com"])

    raw = collector.received[0]["content"]
    # The wire format must be 7-bit-safe RFC 2047, and decode back to the original.
    assert "会议纪要".encode() not in raw
    parsed: EmailMessage = message_from_bytes(raw, policy=policy.default)
    assert parsed["Subject"] == "会议纪要"
    assert "张三" in str(parsed["From"])
    assert parsed.get_content().strip() == "你好，这是正文。"


def test_probe_reports_the_server_it_reached(settings_factory, smtp_server):
    controller, _ = smtp_server
    settings = settings_factory(
        smtp_host=controller.hostname,
        smtp_port=controller.port,
        smtp_security="plain",
    )
    detail = smtp_client.probe(settings)
    assert controller.hostname in detail
    assert SMTP_PASSWORD not in detail


def test_probe_fails_loudly_on_a_bad_password(settings_factory, smtp_server):
    controller, _ = smtp_server
    settings = settings_factory(
        smtp_host=controller.hostname,
        smtp_port=controller.port,
        smtp_security="plain",
        smtp_password="wrong-password",
    )
    with pytest.raises(smtplib.SMTPAuthenticationError):
        smtp_client.probe(settings)


def test_asyncio_loop_is_not_left_running(smtp_server):
    """Guards against the controller leaking a loop into later tests."""
    controller, _ = smtp_server
    assert controller.port > 0
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()
