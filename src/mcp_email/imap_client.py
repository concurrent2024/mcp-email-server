"""Reading mail over IMAP.

Every operation opens a connection, does its work, and logs out. IMAP servers
drop idle connections aggressively and an MCP server can sit untouched for
hours between tool calls, so a pooled connection would mostly be a stale one.
"""

from __future__ import annotations

import imaplib
import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import date
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

from imap_tools import AND, MailBox, MailBoxStartTls, MailBoxUnencrypted, MailMessageFlags

from .auth import AuthProvider, build_auth
from .config import ConfigError, Settings
from .models import (
    AttachmentInfo,
    DownloadResult,
    EmailDetail,
    EmailSummary,
    FolderInfo,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from imap_tools.mailbox import BaseMailBox
    from imap_tools.message import MailMessage

PREVIEW_CHARS = 200

# Folder flags that identify special-use mailboxes (RFC 6154), so we do not have
# to guess between "Trash", "Deleted Items", "已删除", and friends.
_SPECIAL_USE = {
    "drafts": "\\drafts",
    "trash": "\\trash",
    "sent": "\\sent",
}


class MessageNotFoundError(Exception):
    """Raised when a UID does not resolve to a message in the given folder."""


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------


def _make_mailbox(settings: Settings) -> BaseMailBox:
    """Open a transport-level connection, before login."""
    if settings.imap_security == "ssl":
        return MailBox(settings.imap_host, settings.imap_port, timeout=settings.network_timeout)
    if settings.imap_security == "starttls":
        return MailBoxStartTls(
            settings.imap_host, settings.imap_port, timeout=settings.network_timeout
        )
    return MailBoxUnencrypted(
        settings.imap_host, settings.imap_port, timeout=settings.network_timeout
    )


def _identify(mailbox: BaseMailBox) -> None:
    """Send the RFC 2971 ID command when the server supports it.

    Netease (163, 126, yeah.net) rejects every command after login with
    "Unsafe Login" until the client identifies itself this way. Elsewhere the
    command is harmless, so it is driven off the advertised capability rather
    than off a hostname list.
    """
    client = mailbox.client
    if "ID" not in getattr(client, "capabilities", ()):  # pragma: no cover - server-dependent
        return
    imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
    try:
        client._simple_command(  # noqa: SLF001 - imaplib exposes no public ID command
            "ID", '("name" "mcp-email-server" "version" "0.1.0")'
        )
        client._untagged_response("OK", [None], "ID")  # noqa: SLF001
    except Exception:  # pragma: no cover - identification is best effort
        pass


@contextmanager
def open_mailbox(
    settings: Settings,
    folder: str = "INBOX",
    *,
    auth: AuthProvider | None = None,
) -> Iterator[BaseMailBox]:
    """Connect, authenticate, select ``folder``, and always log out afterwards."""
    settings.require_imap()
    auth = auth or build_auth(settings)
    mailbox = _make_mailbox(settings)
    try:
        auth.authenticate_imap(mailbox, settings.imap_username, folder or "INBOX")
        _identify(mailbox)
        yield mailbox
    finally:
        # A failed logout cannot undo the work that just succeeded.
        with suppress(Exception):
            mailbox.logout()


# --------------------------------------------------------------------------
# body text
# --------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Good-enough HTML to text, so HTML-only mail is still readable."""

    # Paragraph-level tags get a blank line after them; line-level tags only
    # get a line break, so lists and table rows do not come out double-spaced.
    _PARAGRAPH_TAGS = {
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "blockquote", "section", "article", "header", "footer",
    }
    _LINE_TAGS = {"br", "tr", "li"}
    _SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        # Breaks are held back rather than written immediately, so that the
        # nesting of </li><li> or </p><div> produces one separator, not several.
        self._pending_breaks = 0

    def _want_break(self, count: int) -> None:
        self._pending_breaks = max(self._pending_breaks, count)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._PARAGRAPH_TAGS or tag in self._LINE_TAGS:
            self._want_break(1)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._PARAGRAPH_TAGS:
            self._want_break(2)
        elif tag in self._LINE_TAGS:
            self._want_break(1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if not data.strip():
            # Whitespace between inline tags still separates words.
            if not self._pending_breaks and self._parts:
                self._parts.append(" ")
            return
        if self._pending_breaks and self._parts:
            self._parts.append("\n" * self._pending_breaks)
        self._pending_breaks = 0
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    """Flatten an HTML body into readable plain text."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
        text = parser.text()
    except Exception:  # pragma: no cover - malformed HTML falls back to a crude strip
        text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def message_body(message: MailMessage) -> tuple[str, bool]:
    """Return the best plain-text body and whether it was derived from HTML."""
    text = (message.text or "").strip()
    if text:
        return text, False
    html = (message.html or "").strip()
    if html:
        return html_to_text(html), True
    return "", False


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return (
        text[:limit].rstrip()
        + f"\n\n[... truncated at {limit} characters; "
        "raise EMAIL_MAX_BODY_CHARS or pass a larger max_body_chars to see more]",
        True,
    )


# --------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------


def _attachments(message: MailMessage) -> list[AttachmentInfo]:
    return [
        AttachmentInfo(
            filename=att.filename or "(unnamed)",
            content_type=att.content_type or "application/octet-stream",
            size=att.size or 0,
        )
        for att in message.attachments
    ]


def _first_header(message: MailMessage, name: str) -> str:
    values = message.headers.get(name.lower()) or ()
    return values[0].strip() if values else ""


def _has_flag(message: MailMessage, flag: str) -> bool:
    """Servers differ on flag casing, so compare case-insensitively."""
    return flag.lower() in {f.lower() for f in message.flags}


def to_summary(message: MailMessage, folder: str) -> EmailSummary:
    body, _ = message_body(message)
    preview = re.sub(r"\s+", " ", body).strip()[:PREVIEW_CHARS]
    return EmailSummary(
        uid=message.uid or "",
        folder=folder,
        subject=message.subject or "",
        sender=message.from_ or "",
        sender_name=message.from_values.name if message.from_values else "",
        to=list(message.to),
        date=message.date,
        seen=_has_flag(message, MailMessageFlags.SEEN),
        flagged=_has_flag(message, MailMessageFlags.FLAGGED),
        has_attachments=bool(message.attachments),
        size=message.size or 0,
        preview=preview,
    )


def to_detail(message: MailMessage, folder: str, max_body_chars: int) -> EmailDetail:
    body, converted = message_body(message)
    body, truncated = _truncate(body, max_body_chars)
    return EmailDetail(
        uid=message.uid or "",
        folder=folder,
        subject=message.subject or "",
        sender=message.from_ or "",
        sender_name=message.from_values.name if message.from_values else "",
        to=list(message.to),
        cc=list(message.cc),
        reply_to=list(message.reply_to),
        date=message.date,
        seen=_has_flag(message, MailMessageFlags.SEEN),
        flagged=_has_flag(message, MailMessageFlags.FLAGGED),
        size=message.size or 0,
        message_id=_first_header(message, "message-id"),
        in_reply_to=_first_header(message, "in-reply-to"),
        references=_first_header(message, "references"),
        body=body,
        body_is_converted_html=converted,
        truncated=truncated,
        attachments=_attachments(message),
    )


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------


def list_folders(settings: Settings, *, auth: AuthProvider | None = None) -> list[FolderInfo]:
    with open_mailbox(settings, auth=auth) as mailbox:
        return [
            FolderInfo(name=f.name, delimiter=f.delim or "", flags=list(f.flags))
            for f in mailbox.folder.list()
        ]


def find_special_folder(
    settings: Settings, kind: str, *, auth: AuthProvider | None = None
) -> str | None:
    """Locate the Drafts/Trash/Sent folder by its RFC 6154 flag, falling back to its name."""
    flag = _SPECIAL_USE[kind]
    fallback = None
    with open_mailbox(settings, auth=auth) as mailbox:
        for folder in mailbox.folder.list():
            if any(f.lower() == flag for f in folder.flags):
                return folder.name
            if folder.name.lower().endswith(kind):
                fallback = fallback or folder.name
    return fallback


def build_criteria(
    *,
    from_contains: str | None = None,
    to_contains: str | None = None,
    subject_contains: str | None = None,
    text_contains: str | None = None,
    since: date | None = None,
    before: date | None = None,
    unread_only: bool = False,
    flagged_only: bool = False,
) -> str:
    """Translate the tool's filters into an IMAP SEARCH expression."""
    params: dict[str, object] = {}
    if from_contains:
        params["from_"] = from_contains
    if to_contains:
        params["to"] = to_contains
    if subject_contains:
        params["subject"] = subject_contains
    if text_contains:
        params["text"] = text_contains
    if since:
        params["date_gte"] = since
    if before:
        params["date_lt"] = before
    if unread_only:
        params["seen"] = False
    if flagged_only:
        params["flagged"] = True
    if not params:
        return "ALL"
    return str(AND(**params))  # type: ignore[arg-type]


def search(
    settings: Settings,
    *,
    folder: str = "INBOX",
    criteria: str = "ALL",
    limit: int = 20,
    auth: AuthProvider | None = None,
) -> list[EmailSummary]:
    """Return the newest ``limit`` messages matching ``criteria``, newest first."""
    with open_mailbox(settings, folder, auth=auth) as mailbox:
        messages = mailbox.fetch(
            criteria,
            limit=limit,
            reverse=True,
            mark_seen=False,
            bulk=True,
            charset="UTF-8",
        )
        return [to_summary(message, folder) for message in messages]


def _fetch_one(mailbox: BaseMailBox, uid: str, *, mark_seen: bool) -> MailMessage:
    messages = list(mailbox.fetch(f"UID {uid}", limit=1, mark_seen=mark_seen, charset="UTF-8"))
    if not messages:
        raise MessageNotFoundError(f"No message with UID {uid} in this folder.")
    return messages[0]


def read(
    settings: Settings,
    uid: str,
    *,
    folder: str = "INBOX",
    max_body_chars: int | None = None,
    mark_seen: bool = False,
    auth: AuthProvider | None = None,
) -> EmailDetail:
    limit = max_body_chars or settings.email_max_body_chars
    with open_mailbox(settings, folder, auth=auth) as mailbox:
        message = _fetch_one(mailbox, uid, mark_seen=mark_seen)
        return to_detail(message, folder, limit)


def set_flags(
    settings: Settings,
    uid: str,
    *,
    folder: str = "INBOX",
    seen: bool | None = None,
    flagged: bool | None = None,
    auth: AuthProvider | None = None,
) -> list[str]:
    """Apply read/unread and flagged/unflagged changes, returning what was done."""
    if seen is None and flagged is None:
        raise ConfigError("Nothing to change: pass seen and/or flagged.")
    done: list[str] = []
    with open_mailbox(settings, folder, auth=auth) as mailbox:
        _assert_exists(mailbox, uid)
        if seen is not None:
            mailbox.flag(uid, MailMessageFlags.SEEN, seen)
            done.append("marked read" if seen else "marked unread")
        if flagged is not None:
            mailbox.flag(uid, MailMessageFlags.FLAGGED, flagged)
            done.append("flagged" if flagged else "unflagged")
    return done


def _assert_exists(mailbox: BaseMailBox, uid: str) -> None:
    if not mailbox.uids(f"UID {uid}"):
        raise MessageNotFoundError(f"No message with UID {uid} in this folder.")


def move(
    settings: Settings,
    uid: str,
    *,
    folder: str,
    destination: str,
    auth: AuthProvider | None = None,
) -> None:
    with open_mailbox(settings, folder, auth=auth) as mailbox:
        _assert_exists(mailbox, uid)
        if not mailbox.folder.exists(destination):
            raise ConfigError(f"Destination folder {destination!r} does not exist.")
        mailbox.move(uid, destination)


def append(
    settings: Settings,
    raw_message: bytes,
    *,
    folder: str,
    flag_set: list[str] | None = None,
    auth: AuthProvider | None = None,
) -> None:
    """Upload a message into a folder, which is how drafts and sent copies are stored."""
    with open_mailbox(settings, auth=auth) as mailbox:
        if not mailbox.folder.exists(folder):
            raise ConfigError(f"Folder {folder!r} does not exist.")
        mailbox.append(raw_message, folder, flag_set=flag_set)


def download_attachment(
    settings: Settings,
    uid: str,
    *,
    folder: str = "INBOX",
    filename: str | None = None,
    auth: AuthProvider | None = None,
) -> DownloadResult:
    """Save one attachment into the configured attachment directory."""
    with open_mailbox(settings, folder, auth=auth) as mailbox:
        message = _fetch_one(mailbox, uid, mark_seen=False)
        attachments = list(message.attachments)
        if not attachments:
            raise MessageNotFoundError(f"Message {uid} has no attachments.")
        if filename:
            match = next((a for a in attachments if a.filename == filename), None)
            if match is None:
                available = ", ".join(a.filename or "(unnamed)" for a in attachments)
                raise MessageNotFoundError(
                    f"No attachment named {filename!r}. Available: {available}"
                )
        elif len(attachments) == 1:
            match = attachments[0]
        else:
            available = ", ".join(a.filename or "(unnamed)" for a in attachments)
            raise ConfigError(
                f"Message {uid} has several attachments; pass filename. Available: {available}"
            )

        target = settings.resolve_attachment_path(
            _safe_filename(match.filename or f"{uid}.bin"), must_exist=False
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(match.payload)
        return DownloadResult(
            path=str(target),
            filename=target.name,
            content_type=match.content_type or "application/octet-stream",
            size=len(match.payload),
        )


def _safe_filename(name: str) -> str:
    """Reduce a sender-controlled filename to a bare name that cannot escape a directory."""
    cleaned = Path(name).name.strip() or "attachment"
    return re.sub(r"[^\w.\-+ ()\[\]]", "_", cleaned)


def latest_uid(
    settings: Settings, *, folder: str = "INBOX", auth: AuthProvider | None = None
) -> int:
    """Highest UID currently in the folder, or 0 when it is empty."""
    with open_mailbox(settings, folder, auth=auth) as mailbox:
        uids = mailbox.uids("ALL")
        return max((int(u) for u in uids if u.isdigit()), default=0)


def fetch_above_uid(
    settings: Settings,
    *,
    folder: str = "INBOX",
    after_uid: int,
    limit: int = 20,
    auth: AuthProvider | None = None,
) -> list[EmailSummary]:
    """Messages whose UID is greater than ``after_uid``, oldest first."""
    with open_mailbox(settings, folder, auth=auth) as mailbox:
        messages = list(
            mailbox.fetch(
                f"UID {after_uid + 1}:*",
                mark_seen=False,
                bulk=True,
                charset="UTF-8",
            )
        )
    # "UID n:*" always returns at least the highest UID even when nothing is
    # above n, so the range has to be re-checked here.
    fresh = [m for m in messages if m.uid and m.uid.isdigit() and int(m.uid) > after_uid]
    fresh.sort(key=lambda m: int(m.uid or 0))
    return [to_summary(m, folder) for m in fresh[:limit]]


def probe(settings: Settings, *, auth: AuthProvider | None = None) -> str:
    """Connect, authenticate, and report the mailbox size without touching anything."""
    with open_mailbox(settings, auth=auth) as mailbox:
        count = len(mailbox.uids("ALL"))
        return (
            f"Authenticated with {settings.imap_host}:{settings.imap_port} "
            f"as {settings.imap_username}; INBOX holds {count} message(s)"
        )


__all__ = [
    "MessageNotFoundError",
    "append",
    "build_criteria",
    "download_attachment",
    "fetch_above_uid",
    "find_special_folder",
    "html_to_text",
    "latest_uid",
    "list_folders",
    "message_body",
    "move",
    "open_mailbox",
    "probe",
    "read",
    "search",
    "set_flags",
    "to_detail",
    "to_summary",
]
