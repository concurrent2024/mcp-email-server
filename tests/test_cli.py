"""CLI entry points: --check and refusing to start without configuration."""

from __future__ import annotations

from mcp_email import server
from mcp_email.__main__ import main
from mcp_email.config import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        imap_host="imap.test",
        imap_username="me@example.com",
        imap_password="s3cret-app-password",
        smtp_host="smtp.test",
        email_from="me@example.com",
        email_allow_send=True,
    )


def test_check_exits_zero_when_both_probes_succeed(monkeypatch, capsys):
    monkeypatch.setattr(
        "mcp_email.imap_client.probe",
        lambda settings: f"Authenticated with {settings.imap_host}",
    )
    monkeypatch.setattr(
        "mcp_email.smtp_client.probe",
        lambda settings: f"Authenticated with {settings.smtp_host}",
    )
    server.use_settings(_settings())
    try:
        code = main(["--check"])
    finally:
        server.use_settings(None)

    captured = capsys.readouterr()
    assert code == 0
    assert "IMAP: ok" in captured.out
    assert "SMTP: ok" in captured.out
    assert "s3cret-app-password" not in captured.out


def test_check_exits_one_when_a_probe_fails(monkeypatch, capsys):
    server.use_settings(_settings())
    monkeypatch.setattr(
        "mcp_email.imap_client.probe",
        lambda _settings: (_ for _ in ()).throw(
            RuntimeError("LOGIN failed with s3cret-app-password")
        ),
    )
    monkeypatch.setattr("mcp_email.smtp_client.probe", lambda _settings: "ok")
    try:
        code = main(["--check"])
    finally:
        server.use_settings(None)

    captured = capsys.readouterr()
    assert code == 1
    assert "IMAP: FAILED" in captured.out
    assert "s3cret-app-password" not in captured.out
    assert "***" in captured.out


def test_main_refuses_to_start_without_imap_settings(monkeypatch):
    monkeypatch.setattr("mcp_email.__main__.get_settings", lambda: Settings(_env_file=None))
    assert main([]) == 2
