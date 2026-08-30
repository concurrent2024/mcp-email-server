"""The MCP server: every tool, resource, and prompt this package exposes."""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, TypeVar

import anyio
from anyio import to_thread
from mcp.server import MCPServer
from mcp.server.mcpserver import UserMessage
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import imap_client, smtp_client
from .config import ConfigError, Settings, get_settings
from .imap_client import MessageNotFoundError
from .models import (
    ActionResult,
    ConnectionStatus,
    DownloadResult,
    DraftResult,
    EmailDetail,
    EmailSummary,
    EndpointStatus,
    FolderInfo,
    SendResult,
    WaitResult,
)
from .smtp_client import OutgoingMessage

INSTRUCTIONS = """\
Reads and sends email for the configured account over IMAP and SMTP.

Start with check_connection when anything is unclear: it reports whether the
credentials work and whether sending is even enabled.

Use search_emails to find messages and read_email to open one; search returns
short previews rather than full bodies, so read the messages that matter rather
than widening the search.

Sending is a real, irreversible action. Show the user the recipients, subject,
and body and get their agreement before calling send_email. When the user wants
to review first, save_draft puts the message in their Drafts folder instead.
"""

mcp = MCPServer(
    "email",
    title="Email",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)

_settings_override: Settings | None = None

F = TypeVar("F", bound=Callable[..., Any])


def current_settings() -> Settings:
    """The active configuration. Tests install their own with :func:`use_settings`."""
    return _settings_override if _settings_override is not None else get_settings()


def use_settings(settings: Settings | None) -> None:
    """Replace the active configuration, or restore the environment-derived one."""
    global _settings_override
    _settings_override = settings


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------

# Only a ToolError's message reaches the model; anything else is reported to it
# as a generic failure with the traceback left in the server log. Expected
# problems are therefore translated here, with credentials stripped out on the
# way, so the model gets something it can act on.
_EXPECTED = (ConfigError, MessageNotFoundError, OSError, ValueError)


def _as_tool_error(exc: Exception) -> ToolError:
    message = current_settings().redact(str(exc) or exc.__class__.__name__)
    return ToolError(message)


def handles_errors(fn: F) -> F:
    """Translate expected mail failures into messages the model can read."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except _EXPECTED as exc:
                raise _as_tool_error(exc) from exc
            except Exception as exc:
                if _is_mail_error(exc):
                    raise _as_tool_error(exc) from exc
                raise

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except _EXPECTED as exc:
            raise _as_tool_error(exc) from exc
        except Exception as exc:
            if _is_mail_error(exc):
                raise _as_tool_error(exc) from exc
            raise

    return wrapper  # type: ignore[return-value]


def _is_mail_error(exc: Exception) -> bool:
    """True for the protocol-level errors imaplib, smtplib and imap-tools raise."""
    module = type(exc).__module__.split(".")[0]
    return module in {"imaplib", "smtplib", "imap_tools", "ssl", "socket"}


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------


@mcp.tool(
    title="Check the mail connection",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def check_connection() -> ConnectionStatus:
    """Verify the IMAP and SMTP credentials and report which actions are permitted.

    Run this first when a tool fails or the account setup is unknown. Credentials
    are never included in the result.
    """
    settings = current_settings()

    def probe(fn: Callable[[], str]) -> EndpointStatus:
        try:
            return EndpointStatus(ok=True, detail=settings.redact(fn()))
        except Exception as exc:  # noqa: BLE001 - the point is to report any failure
            detail = settings.redact(str(exc) or exc.__class__.__name__)
            return EndpointStatus(ok=False, detail=f"{type(exc).__name__}: {detail}")

    return ConnectionStatus(
        imap=probe(lambda: imap_client.probe(settings)),
        smtp=probe(lambda: smtp_client.probe(settings)),
        account=settings.imap_username or settings.effective_smtp_username,
        sending_enabled=settings.email_allow_send,
        deleting_enabled=settings.email_allow_delete,
        recipient_allowlist=list(settings.email_recipient_allowlist),
    )


# --------------------------------------------------------------------------
# receiving
# --------------------------------------------------------------------------


@mcp.tool(
    title="List mail folders",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
@handles_errors
def list_folders() -> list[FolderInfo]:
    """List the folders in the mailbox, with their IMAP special-use flags."""
    return imap_client.list_folders(current_settings())


@mcp.tool(
    title="Search email",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
@handles_errors
def search_emails(
    folder: Annotated[str, Field(description="Folder to search.")] = "INBOX",
    from_contains: Annotated[
        str | None, Field(description="Match the From header against this substring.")
    ] = None,
    to_contains: Annotated[
        str | None, Field(description="Match the To header against this substring.")
    ] = None,
    subject_contains: Annotated[
        str | None, Field(description="Match the subject against this substring.")
    ] = None,
    text_contains: Annotated[
        str | None, Field(description="Match anywhere in the headers or body.")
    ] = None,
    since: Annotated[
        date | None, Field(description="Only messages sent on or after this date.")
    ] = None,
    before: Annotated[
        date | None, Field(description="Only messages sent strictly before this date.")
    ] = None,
    unread_only: Annotated[bool, Field(description="Only unread messages.")] = False,
    flagged_only: Annotated[bool, Field(description="Only flagged/starred messages.")] = False,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum results.")] = 20,
) -> list[EmailSummary]:
    """Find messages, newest first, returning short summaries rather than full bodies.

    Every filter is combined with AND, and omitting all of them returns the most
    recent messages in the folder. Reading a message costs a separate
    read_email call, using the uid from these results.
    """
    settings = current_settings()
    criteria = imap_client.build_criteria(
        from_contains=from_contains,
        to_contains=to_contains,
        subject_contains=subject_contains,
        text_contains=text_contains,
        since=since,
        before=before,
        unread_only=unread_only,
        flagged_only=flagged_only,
    )
    return imap_client.search(settings, folder=folder, criteria=criteria, limit=limit)


@mcp.tool(
    title="Read an email",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
@handles_errors
def read_email(
    uid: Annotated[str, Field(description="IMAP UID, as returned by search_emails.")],
    folder: Annotated[str, Field(description="Folder the message is in.")] = "INBOX",
    max_body_chars: Annotated[
        int | None,
        Field(ge=200, le=200_000, description="Override the configured body length limit."),
    ] = None,
    mark_as_read: Annotated[
        bool, Field(description="Set the Seen flag as a side effect of reading.")
    ] = False,
) -> EmailDetail:
    """Open one message and return its body, headers, and attachment list.

    Bodies are returned as plain text; HTML-only messages are converted, and
    long bodies are truncated with a marker rather than silently cut.
    """
    return imap_client.read(
        current_settings(),
        uid,
        folder=folder,
        max_body_chars=max_body_chars,
        mark_seen=mark_as_read,
    )


@mcp.tool(
    title="Wait for new email",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
@handles_errors
async def wait_for_new_emails(
    folder: Annotated[str, Field(description="Folder to watch.")] = "INBOX",
    timeout_seconds: Annotated[
        int, Field(ge=5, le=600, description="How long to wait before giving up.")
    ] = 60,
    poll_interval_seconds: Annotated[
        int, Field(ge=2, le=60, description="Seconds between checks.")
    ] = 5,
    limit: Annotated[int, Field(ge=1, le=50, description="Maximum messages to return.")] = 10,
) -> WaitResult:
    """Block until mail arrives in the folder, then return the new messages.

    Use this to wait for something expected, such as a verification code. It
    returns as soon as anything arrives, or empty-handed when the timeout
    elapses; nothing already in the folder is reported.
    """
    settings = current_settings()
    started = time.monotonic()
    baseline = await to_thread.run_sync(
        functools.partial(imap_client.latest_uid, settings, folder=folder)
    )

    while True:
        arrived = await to_thread.run_sync(
            functools.partial(
                imap_client.fetch_above_uid,
                settings,
                folder=folder,
                after_uid=baseline,
                limit=limit,
            )
        )
        elapsed = time.monotonic() - started
        if arrived:
            return WaitResult(timed_out=False, waited_seconds=round(elapsed, 1), messages=arrived)
        if elapsed + poll_interval_seconds > timeout_seconds:
            return WaitResult(timed_out=True, waited_seconds=round(elapsed, 1), messages=[])
        await anyio.sleep(poll_interval_seconds)


@mcp.tool(
    title="Download an attachment",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
)
@handles_errors
def download_attachment(
    uid: Annotated[str, Field(description="IMAP UID of the message.")],
    folder: Annotated[str, Field(description="Folder the message is in.")] = "INBOX",
    filename: Annotated[
        str | None,
        Field(description="Which attachment to save. Required when the message has several."),
    ] = None,
) -> DownloadResult:
    """Save an attachment into the server's attachment directory and return its path.

    Writes are confined to EMAIL_ATTACHMENT_DIR, and the sender's filename is
    sanitised before use.
    """
    return imap_client.download_attachment(
        current_settings(), uid, folder=folder, filename=filename
    )


# --------------------------------------------------------------------------
# mailbox state
# --------------------------------------------------------------------------


@mcp.tool(
    title="Flag an email",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    ),
)
@handles_errors
def mark_email(
    uid: Annotated[str, Field(description="IMAP UID of the message.")],
    folder: Annotated[str, Field(description="Folder the message is in.")] = "INBOX",
    seen: Annotated[
        bool | None, Field(description="True marks it read, False marks it unread.")
    ] = None,
    flagged: Annotated[
        bool | None, Field(description="True stars/flags it, False clears the flag.")
    ] = None,
) -> ActionResult:
    """Mark a message read or unread, and flag or unflag it."""
    done = imap_client.set_flags(
        current_settings(), uid, folder=folder, seen=seen, flagged=flagged
    )
    return ActionResult(uid=uid, folder=folder, action=", ".join(done))


@mcp.tool(
    title="Move an email",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    ),
)
@handles_errors
def move_email(
    uid: Annotated[str, Field(description="IMAP UID of the message.")],
    destination: Annotated[str, Field(description="Folder to move the message into.")],
    folder: Annotated[str, Field(description="Folder the message is currently in.")] = "INBOX",
) -> ActionResult:
    """Move a message to another folder. Requires EMAIL_ALLOW_DELETE=true."""
    settings = current_settings()
    settings.require_delete_allowed()
    imap_client.move(settings, uid, folder=folder, destination=destination)
    return ActionResult(
        uid=uid, folder=folder, action="moved", detail=f"Moved to {destination}."
    )


@mcp.tool(
    title="Delete an email",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    ),
)
@handles_errors
def delete_email(
    uid: Annotated[str, Field(description="IMAP UID of the message.")],
    folder: Annotated[str, Field(description="Folder the message is in.")] = "INBOX",
) -> ActionResult:
    """Move a message to Trash. Requires EMAIL_ALLOW_DELETE=true.

    This is a move, not an expunge, so the user can still recover the message
    from their Trash folder.
    """
    settings = current_settings()
    settings.require_delete_allowed()
    trash = settings.email_trash_folder or imap_client.find_special_folder(settings, "trash")
    if not trash:
        raise ToolError(
            "No Trash folder found on this account. Set EMAIL_TRASH_FOLDER, "
            "or use move_email with an explicit destination."
        )
    imap_client.move(settings, uid, folder=folder, destination=trash)
    return ActionResult(
        uid=uid, folder=folder, action="deleted", detail=f"Moved to {trash}."
    )


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


def _resolve_reply(
    settings: Settings, reply_to_uid: str | None, reply_to_folder: str
) -> EmailDetail | None:
    if not reply_to_uid:
        return None
    return imap_client.read(settings, reply_to_uid, folder=reply_to_folder, max_body_chars=1000)


def _compose(
    settings: Settings,
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str],
    bcc: list[str],
    html: str | None,
    attachments: list[str],
    original: EmailDetail | None,
) -> OutgoingMessage:
    """Assemble an outgoing message, filling in reply details from the original."""
    if original is not None:
        if not to:
            to = original.reply_to or [original.sender]
        if not subject:
            subject = original.subject
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

    outgoing = OutgoingMessage(
        to=list(to),
        subject=subject,
        body=body,
        cc=list(cc),
        bcc=list(bcc),
        html=html,
        attachments=list(attachments),
    )
    if original is not None and original.message_id:
        outgoing.in_reply_to = original.message_id
        outgoing.references = (
            f"{original.references} {original.message_id}".strip()
            if original.references
            else original.message_id
        )

    recipients = smtp_client.bare_addresses(
        smtp_client.normalize_recipients(outgoing.to + outgoing.cc + outgoing.bcc)
    )
    if not recipients:
        raise ConfigError("A message needs at least one recipient.")
    settings.check_recipients(recipients)
    return outgoing


@mcp.tool(
    title="Send an email",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
)
@handles_errors
def send_email(
    subject: Annotated[str, Field(description="Subject line.")],
    body: Annotated[str, Field(description="Plain-text body.")],
    to: Annotated[
        list[str],
        Field(description="Recipients. Plain addresses or 'Name <a@b.com>' both work."),
    ] = [],
    cc: Annotated[list[str], Field(description="Cc recipients.")] = [],
    bcc: Annotated[list[str], Field(description="Bcc recipients.")] = [],
    html: Annotated[
        str | None,
        Field(description="Optional HTML alternative. Always send body as well."),
    ] = None,
    attachments: Annotated[
        list[str],
        Field(description="Filenames inside the server's attachment directory."),
    ] = [],
    reply_to_uid: Annotated[
        str | None,
        Field(description="UID of a message to reply to; threads the reply and fills in To."),
    ] = None,
    reply_to_folder: Annotated[
        str, Field(description="Folder holding the message being replied to.")
    ] = "INBOX",
) -> SendResult:
    """Send an email. This is irreversible: confirm the content with the user first.

    Sending only works when EMAIL_ALLOW_SEND=true, and every recipient must pass
    the configured allowlist. Pass reply_to_uid to answer an existing message:
    the reply is threaded correctly and the recipient and subject are filled in
    from the original.
    """
    settings = current_settings()
    settings.require_send_allowed()
    settings.require_smtp()

    original = _resolve_reply(settings, reply_to_uid, reply_to_folder)
    outgoing = _compose(
        settings,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html=html,
        attachments=attachments,
        original=original,
    )
    message = smtp_client.build_message(settings, outgoing)
    all_recipients = smtp_client.bare_addresses(
        smtp_client.normalize_recipients(outgoing.to + outgoing.cc + outgoing.bcc)
    )
    refused = smtp_client.send_message(settings, message, recipients=all_recipients)
    rejected = sorted(refused)
    saved_to = _save_sent_copy(settings, message)

    return SendResult(
        message_id=message["Message-ID"] or "",
        sender=str(message["From"] or ""),
        accepted=[r for r in all_recipients if r not in refused],
        rejected=rejected,
        subject=outgoing.subject,
        attachment_count=len(outgoing.attachments),
        saved_to_folder=saved_to,
    )


def _save_sent_copy(settings: Settings, message: Any) -> str | None:
    """Append a copy to Sent when configured. Never fails the send itself."""
    if not settings.email_save_sent_copy:
        return None
    try:
        folder = imap_client.find_special_folder(settings, "sent")
        if not folder:
            return None
        imap_client.append(settings, message.as_bytes(), folder=folder, flag_set=["\\Seen"])
        return folder
    except Exception:  # noqa: BLE001 - the mail is already delivered
        return None


@mcp.tool(
    title="Save a draft",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
)
@handles_errors
def save_draft(
    subject: Annotated[str, Field(description="Subject line.")],
    body: Annotated[str, Field(description="Plain-text body.")],
    to: Annotated[list[str], Field(description="Intended recipients.")] = [],
    cc: Annotated[list[str], Field(description="Cc recipients.")] = [],
    bcc: Annotated[list[str], Field(description="Bcc recipients.")] = [],
    html: Annotated[str | None, Field(description="Optional HTML alternative.")] = None,
    attachments: Annotated[
        list[str], Field(description="Filenames inside the server's attachment directory.")
    ] = [],
    reply_to_uid: Annotated[
        str | None, Field(description="UID of a message this draft replies to.")
    ] = None,
    reply_to_folder: Annotated[
        str, Field(description="Folder holding the message being replied to.")
    ] = "INBOX",
) -> DraftResult:
    """Write a message to the Drafts folder without sending it.

    This is the safe way to prepare mail: the user opens their own mail client,
    reviews it, and sends it themselves. It works even when EMAIL_ALLOW_SEND is
    false, because nothing leaves the account.
    """
    settings = current_settings()
    original = _resolve_reply(settings, reply_to_uid, reply_to_folder)
    outgoing = _compose(
        settings,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html=html,
        attachments=attachments,
        original=original,
    )
    message = smtp_client.build_message(settings, outgoing, include_bcc_header=True)

    folder = settings.email_drafts_folder or imap_client.find_special_folder(settings, "drafts")
    if not folder:
        raise ToolError(
            "No Drafts folder found on this account. Set EMAIL_DRAFTS_FOLDER to name it."
        )
    imap_client.append(settings, message.as_bytes(), folder=folder, flag_set=["\\Draft"])
    return DraftResult(
        folder=folder, message_id=message["Message-ID"] or "", subject=outgoing.subject
    )


# --------------------------------------------------------------------------
# resource and prompt
# --------------------------------------------------------------------------


@mcp.resource(
    "email://{folder}/{uid}",
    title="Email message",
    description="One email as plain text, addressable by folder and UID.",
    mime_type="text/plain",
)
def email_resource(folder: str, uid: str) -> str:
    """Return a message rendered as text, for attaching to the conversation."""
    settings = current_settings()
    detail = imap_client.read(settings, uid, folder=folder)
    return render_message(detail)


def render_message(detail: EmailDetail) -> str:
    """Format a message the way a mail client shows it."""
    lines = [
        f"From: {detail.sender_name + ' ' if detail.sender_name else ''}<{detail.sender}>",
        f"To: {', '.join(detail.to)}",
    ]
    if detail.cc:
        lines.append(f"Cc: {', '.join(detail.cc)}")
    lines.append(f"Date: {detail.date.isoformat() if detail.date else 'unknown'}")
    lines.append(f"Subject: {detail.subject}")
    if detail.attachments:
        names = ", ".join(f"{a.filename} ({a.size} bytes)" for a in detail.attachments)
        lines.append(f"Attachments: {names}")
    lines.append("")
    lines.append(detail.body)
    return "\n".join(lines)


@mcp.prompt(
    title="Draft a reply",
    description="Load a message and ask for a reply to be drafted, with the original quoted.",
)
def draft_reply(uid: str, folder: str = "INBOX", instructions: str = "") -> list[UserMessage]:
    """Draft a reply to the message with this UID."""
    settings = current_settings()
    detail = imap_client.read(settings, uid, folder=folder)
    quoted = "\n".join(f"> {line}" for line in detail.body.splitlines())
    guidance = f"\n\nWhat the reply should say:\n{instructions}" if instructions else ""
    return [
        UserMessage(
            f"Draft a reply to this email. Match its tone and language, keep it concise, "
            f"and do not send anything: show me the draft first.{guidance}\n\n"
            f"From: {detail.sender}\n"
            f"Date: {detail.date.isoformat() if detail.date else 'unknown'}\n"
            f"Subject: {detail.subject}\n\n"
            f"{quoted}\n\n"
            f"To send it once I approve, call send_email with reply_to_uid={uid!r} "
            f"and reply_to_folder={folder!r}."
        )
    ]


__all__ = ["current_settings", "mcp", "render_message", "use_settings"]
