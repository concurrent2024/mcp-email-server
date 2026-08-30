"""Composing and sending mail over SMTP."""

from __future__ import annotations

import mimetypes
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr

from .auth import AuthProvider, build_auth
from .config import ConfigError, Settings


@dataclass(slots=True)
class OutgoingMessage:
    """The parts of a message that a caller chooses, before it becomes MIME."""

    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    html: str | None = None
    attachments: list[str] = field(default_factory=list)
    in_reply_to: str = ""
    references: str = ""


def normalize_recipients(values: list[str]) -> list[str]:
    """Split anything address-like into individual addresses, dropping blanks.

    Models routinely pass ``["a@x.com, b@x.com"]`` as one string; ``getaddresses``
    handles that as well as the display-name form.
    """
    parsed = getaddresses([v for v in values if v and v.strip()])
    out: list[str] = []
    for name, address in parsed:
        address = address.strip()
        if not address:
            continue
        formatted = formataddr((name.strip(), address)) if name.strip() else address
        if formatted not in out:
            out.append(formatted)
    return out


def bare_addresses(values: list[str]) -> list[str]:
    """Reduce ``Name <a@b.com>`` entries to ``a@b.com``."""
    return [addr for _, addr in getaddresses(values) if addr]


def build_message(
    settings: Settings, outgoing: OutgoingMessage, *, include_bcc_header: bool = False
) -> EmailMessage:
    """Turn an :class:`OutgoingMessage` into a MIME message ready for the wire.

    Bcc is left out of the headers by default so that blind recipients cannot
    leak through any copy of the message; delivery uses the explicit recipient
    list instead. Drafts pass ``include_bcc_header`` so the user still sees who
    they meant to blind-copy when they open the draft.
    """
    to = normalize_recipients(outgoing.to)
    cc = normalize_recipients(outgoing.cc)
    bcc = normalize_recipients(outgoing.bcc)
    if not (to or cc or bcc):
        raise ConfigError("A message needs at least one recipient.")

    sender_address = parseaddr(settings.from_address)[1] or settings.from_address
    sender = (
        formataddr((settings.email_from_name, sender_address))
        if settings.email_from_name
        else settings.from_address
    )

    message = EmailMessage()
    message["From"] = sender
    if to:
        message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc and include_bcc_header:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = outgoing.subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender_address.rpartition("@")[2] or None)

    # Threading headers, so a reply lands inside the original conversation
    # instead of starting a new one.
    if outgoing.in_reply_to:
        message["In-Reply-To"] = outgoing.in_reply_to
        references = outgoing.references or outgoing.in_reply_to
        message["References"] = references

    message.set_content(outgoing.body or "")
    if outgoing.html:
        message.add_alternative(outgoing.html, subtype="html")

    for raw_path in outgoing.attachments:
        path = settings.resolve_attachment_path(raw_path, must_exist=True)
        content_type, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=path.name,
        )

    return message


def _connect(settings: Settings) -> smtplib.SMTP:
    """Open a secured SMTP session, without logging in."""
    timeout = settings.network_timeout
    if settings.smtp_security == "ssl":
        context = ssl.create_default_context()
        return smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=timeout, context=context
        )

    smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    try:
        smtp.ehlo()
        if settings.smtp_security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
    except Exception:
        smtp.close()
        raise
    return smtp


def send_message(
    settings: Settings,
    message: EmailMessage,
    *,
    recipients: list[str] | None = None,
    auth: AuthProvider | None = None,
) -> dict[str, tuple[int, bytes]]:
    """Deliver a built message, returning the recipients the server refused.

    ``recipients`` is the envelope. It must be passed whenever the message has
    Bcc recipients, because they are absent from the headers that smtplib would
    otherwise infer the envelope from.
    """
    auth = auth or build_auth(settings)
    smtp = _connect(settings)
    try:
        username = settings.effective_smtp_username
        if username:
            auth.authenticate_smtp(smtp, username)
        if recipients:
            return smtp.send_message(message, to_addrs=recipients)
        return smtp.send_message(message)
    finally:
        try:
            smtp.quit()
        except Exception:  # pragma: no cover - the send already succeeded or failed
            smtp.close()


def probe(settings: Settings, *, auth: AuthProvider | None = None) -> str:
    """Connect and authenticate without sending, returning the server's greeting."""
    settings.require_smtp()
    auth = auth or build_auth(settings)
    smtp = _connect(settings)
    try:
        username = settings.effective_smtp_username
        if username:
            auth.authenticate_smtp(smtp, username)
        greeting = getattr(smtp, "_host", settings.smtp_host)
        return f"Authenticated with {greeting}:{settings.smtp_port} as {username}"
    finally:
        try:
            smtp.quit()
        except Exception:  # pragma: no cover
            smtp.close()


__all__ = [
    "OutgoingMessage",
    "bare_addresses",
    "build_message",
    "normalize_recipients",
    "probe",
    "send_message",
]
