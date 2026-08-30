"""The safety policy: allowlists, path containment, and credential redaction."""

from __future__ import annotations

import pytest

from mcp_email.config import ConfigError, Settings


def test_allowlist_empty_permits_everyone(settings_factory):
    settings = settings_factory(email_recipient_allowlist=[])
    settings.check_recipients(["stranger@elsewhere.com"])


def test_allowlist_matches_exact_address(settings_factory):
    settings = settings_factory(email_recipient_allowlist="me@example.com")
    settings.check_recipients(["me@example.com"])
    with pytest.raises(ConfigError, match="not permitted"):
        settings.check_recipients(["someone@example.com"])


@pytest.mark.parametrize("rule", ["@example.com", "example.com"])
def test_allowlist_matches_domain(settings_factory, rule):
    settings = settings_factory(email_recipient_allowlist=rule)
    settings.check_recipients(["anyone@example.com"])
    with pytest.raises(ConfigError):
        settings.check_recipients(["anyone@evil.com"])


def test_allowlist_is_not_fooled_by_a_similar_domain(settings_factory):
    settings = settings_factory(email_recipient_allowlist="@example.com")
    with pytest.raises(ConfigError):
        settings.check_recipients(["anyone@notexample.com"])
    with pytest.raises(ConfigError):
        settings.check_recipients(["anyone@example.com.evil.net"])


def test_allowlist_reads_the_address_not_the_display_name(settings_factory):
    settings = settings_factory(email_recipient_allowlist="@example.com")
    settings.check_recipients(["Me <me@example.com>"])
    with pytest.raises(ConfigError):
        settings.check_recipients(["me@example.com <attacker@evil.com>"])


def test_allowlist_is_parsed_from_a_comma_separated_string(settings_factory):
    settings = settings_factory(email_recipient_allowlist="a@x.com, @y.com ,")
    assert settings.email_recipient_allowlist == ["a@x.com", "@y.com"]


def test_send_and_delete_are_refused_by_default(settings_factory):
    settings = settings_factory(email_allow_send=False, email_allow_delete=False)
    with pytest.raises(ConfigError, match="EMAIL_ALLOW_SEND"):
        settings.require_send_allowed()
    with pytest.raises(ConfigError, match="EMAIL_ALLOW_DELETE"):
        settings.require_delete_allowed()


def test_missing_imap_settings_are_named(settings_factory):
    settings = settings_factory(imap_host="", imap_password="")
    with pytest.raises(ConfigError) as excinfo:
        settings.require_imap()
    assert "IMAP_HOST" in str(excinfo.value)
    assert "IMAP_PASSWORD" in str(excinfo.value)
    assert "IMAP_USERNAME" not in str(excinfo.value)


def test_smtp_credentials_fall_back_to_imap(settings_factory):
    settings = settings_factory(smtp_username="", smtp_password="")
    assert settings.effective_smtp_username == "me@example.com"
    assert settings.effective_smtp_password == "s3cret-app-password"
    settings.require_smtp()


def test_attachment_paths_stay_inside_the_directory(settings):
    settings.attachment_dir.mkdir(parents=True, exist_ok=True)
    (settings.attachment_dir / "report.pdf").write_bytes(b"pdf")

    resolved = settings.resolve_attachment_path("report.pdf", must_exist=True)
    assert resolved.parent == settings.attachment_dir

    with pytest.raises(ConfigError, match="outside the attachment directory"):
        settings.resolve_attachment_path("../../etc/passwd", must_exist=False)
    with pytest.raises(ConfigError, match="outside the attachment directory"):
        settings.resolve_attachment_path("/etc/passwd", must_exist=False)


def test_missing_attachment_is_reported(settings):
    settings.attachment_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ConfigError, match="No such file"):
        settings.resolve_attachment_path("absent.pdf", must_exist=True)


def test_redact_removes_passwords(settings_factory):
    settings = settings_factory(imap_password="hunter2", smtp_password="swordfish")
    text = "LOGIN failed for hunter2 / swordfish"
    assert settings.redact(text) == "LOGIN failed for *** / ***"
    assert "hunter2" not in settings.redact(text)


def test_security_values_are_normalised(settings_factory):
    assert settings_factory(imap_security="SSL").imap_security == "ssl"
    assert settings_factory(smtp_security=" StartTLS ").smtp_security == "starttls"


def test_defaults_are_conservative():
    settings = Settings(_env_file=None)
    assert settings.email_allow_send is False
    assert settings.email_allow_delete is False
    assert settings.email_save_sent_copy is False
