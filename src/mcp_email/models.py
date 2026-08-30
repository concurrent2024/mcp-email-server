"""Return types for the tools.

These are what the client receives as structured output, so the field names and
descriptions are part of the server's contract with the model.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AttachmentInfo(BaseModel):
    """An attachment's metadata. The payload is only produced by ``download_attachment``."""

    filename: str = Field(description="Filename as declared by the sender.")
    content_type: str = Field(description="MIME type, e.g. application/pdf.")
    size: int = Field(description="Size in bytes.")


class EmailSummary(BaseModel):
    """One search hit. Deliberately small: no full body, so listings stay cheap."""

    uid: str = Field(description="IMAP UID, stable within a folder. Pass it to read_email.")
    folder: str = Field(description="Folder the message lives in.")
    subject: str = Field(default="", description="Subject line, already MIME-decoded.")
    sender: str = Field(default="", description="From address.")
    sender_name: str = Field(default="", description="From display name, if any.")
    to: list[str] = Field(default_factory=list, description="To addresses.")
    date: datetime | None = Field(default=None, description="Date the message was sent.")
    seen: bool = Field(default=False, description="Whether the message is marked read.")
    flagged: bool = Field(default=False, description="Whether the message is starred/flagged.")
    has_attachments: bool = Field(default=False)
    size: int = Field(default=0, description="Message size in bytes.")
    preview: str = Field(default="", description="First few lines of the body, for triage.")


class EmailDetail(BaseModel):
    """A single message with its body."""

    uid: str
    folder: str
    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    reply_to: list[str] = Field(default_factory=list)
    date: datetime | None = None
    seen: bool = False
    flagged: bool = False
    size: int = 0
    message_id: str = Field(default="", description="RFC 5322 Message-ID, used for threading replies.")
    in_reply_to: str = ""
    references: str = ""
    body: str = Field(default="", description="Plain-text body. HTML-only mail is converted to text.")
    body_is_converted_html: bool = Field(
        default=False,
        description="True when the message had no plain-text part and the body was derived from HTML.",
    )
    truncated: bool = Field(
        default=False,
        description="True when the body was cut short at the configured limit.",
    )
    attachments: list[AttachmentInfo] = Field(default_factory=list)


class FolderInfo(BaseModel):
    """An IMAP folder."""

    name: str
    delimiter: str = ""
    flags: list[str] = Field(default_factory=list)


class SendResult(BaseModel):
    """Outcome of a send. ``accepted`` is what the SMTP server agreed to deliver."""

    message_id: str = Field(description="Message-ID assigned to the outgoing mail.")
    sender: str
    accepted: list[str] = Field(description="Recipients the server accepted.")
    rejected: list[str] = Field(default_factory=list, description="Recipients the server refused.")
    subject: str = ""
    attachment_count: int = 0
    saved_to_folder: str | None = Field(
        default=None,
        description="Folder a copy was appended to, when the account keeps sent mail server-side.",
    )


class DraftResult(BaseModel):
    """Outcome of saving a draft."""

    folder: str = Field(description="Folder the draft was appended to.")
    message_id: str
    subject: str = ""


class ActionResult(BaseModel):
    """Outcome of a state change such as flagging, moving, or deleting."""

    uid: str
    folder: str
    action: str = Field(description="What was done, e.g. 'moved' or 'marked read'.")
    detail: str = ""


class DownloadResult(BaseModel):
    """Where an attachment ended up on disk."""

    path: str = Field(description="Absolute path of the saved file.")
    filename: str
    content_type: str = ""
    size: int = 0


class WaitResult(BaseModel):
    """Outcome of waiting for new mail."""

    timed_out: bool = Field(description="True when the wait elapsed with nothing new arriving.")
    waited_seconds: float
    messages: list[EmailSummary] = Field(
        default_factory=list, description="Messages that arrived during the wait, oldest first."
    )


class EndpointStatus(BaseModel):
    """Result of probing one of the two services."""

    ok: bool
    detail: str = Field(description="Server greeting on success, or the reason it failed.")


class ConnectionStatus(BaseModel):
    """Result of ``check_connection``. Never contains credentials."""

    imap: EndpointStatus
    smtp: EndpointStatus
    account: str = Field(default="", description="Username the server is configured to use.")
    sending_enabled: bool = False
    deleting_enabled: bool = False
    recipient_allowlist: list[str] = Field(default_factory=list)


__all__ = [
    "ActionResult",
    "AttachmentInfo",
    "ConnectionStatus",
    "DownloadResult",
    "DraftResult",
    "EmailDetail",
    "EmailSummary",
    "EndpointStatus",
    "FolderInfo",
    "SendResult",
    "WaitResult",
]
