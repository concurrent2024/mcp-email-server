"""End-to-end smoke test: a real subprocess, real MCP over pipes, a real send.

Everything else in the suite talks to the server object in-process. This one
starts it the way a client actually does -- as a subprocess speaking JSON-RPC
over stdio -- and delivers a message to a real SMTP server, so a break in the
packaging, the CLI, or the transport shows up here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from email import message_from_bytes, policy

import pytest
from mcp import Client, StdioServerParameters

from test_smtp import SMTP_PASSWORD, SMTP_USER, _authenticator, _Collector, _free_port

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def live_smtp():
    from aiosmtpd.controller import Controller

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


def _server_params(controller, tmp_path, **overrides: str) -> StdioServerParameters:
    env = {
        "PATH": os.environ.get("PATH", ""),
        # Unreachable on purpose: this test covers the sending half, and the
        # server must still start and serve tools with IMAP unavailable.
        "IMAP_HOST": "127.0.0.1",
        "IMAP_PORT": "1",
        "IMAP_USERNAME": SMTP_USER,
        "IMAP_PASSWORD": SMTP_PASSWORD,
        "SMTP_HOST": controller.hostname,
        "SMTP_PORT": str(controller.port),
        "SMTP_SECURITY": "plain",
        "EMAIL_FROM": SMTP_USER,
        "EMAIL_FROM_NAME": "Me",
        "EMAIL_ALLOW_SEND": "true",
        "EMAIL_ATTACHMENT_DIR": str(tmp_path),
        "NETWORK_TIMEOUT": "3",
    }
    env.update(overrides)
    return StdioServerParameters(command=sys.executable, args=["-m", "mcp_email"], env=env)


async def test_a_client_can_start_the_server_and_send_mail(live_smtp, tmp_path):
    controller, collector = live_smtp

    async with Client(_server_params(controller, tmp_path)) as client:
        assert client.server_info.name == "email"

        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"send_email", "search_emails", "wait_for_new_emails"} <= tools

        result = await client.call_tool(
            "send_email",
            {
                "to": ["bob@example.com"],
                "bcc": ["hidden@example.com"],
                "subject": "端到端测试",
                "body": "Sent through a real subprocess.",
            },
        )

    assert result.is_error is not True
    assert sorted(result.structured_content["accepted"]) == [
        "bob@example.com",
        "hidden@example.com",
    ]

    assert len(collector.received) == 1
    delivered = collector.received[0]
    assert sorted(delivered["rcpt_tos"]) == ["bob@example.com", "hidden@example.com"]
    parsed = message_from_bytes(delivered["content"], policy=policy.default)
    assert parsed["Subject"] == "端到端测试"
    assert parsed["Bcc"] is None


async def test_imap_failure_is_reported_without_taking_the_server_down(live_smtp, tmp_path):
    controller, _ = live_smtp

    async with Client(_server_params(controller, tmp_path)) as client:
        status = (await client.call_tool("check_connection", {})).structured_content
        # A broken IMAP endpoint must not stop the SMTP half from working.
        failed = await client.call_tool("search_emails", {})

    assert status["smtp"]["ok"] is True
    assert status["imap"]["ok"] is False
    assert SMTP_PASSWORD not in str(status)
    assert failed.is_error is True


def test_the_server_refuses_to_start_without_configuration():
    """A misconfigured server must fail at startup, not on the first tool call."""
    result = subprocess.run(
        [sys.executable, "-m", "mcp_email"],
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "IMAP_HOST" in result.stderr
