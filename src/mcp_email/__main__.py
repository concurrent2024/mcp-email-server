"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError, get_settings
from .server import mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-email-server",
        description="An MCP server that sends and receives email over SMTP and IMAP.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="stdio (default) for a local client such as Cursor; streamable-http to serve over a port.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for streamable-http.")
    parser.add_argument("--port", type=int, default=8000, help="Port for streamable-http.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the configuration and credentials, print the result, and exit.",
    )
    return parser


def _run_check() -> int:
    """Probe both services and report, for diagnosing setup outside a client."""
    from .server import check_connection

    status = check_connection()
    print(f"account: {status.account or '(not configured)'}")
    print(f"IMAP: {'ok' if status.imap.ok else 'FAILED'} - {status.imap.detail}")
    print(f"SMTP: {'ok' if status.smtp.ok else 'FAILED'} - {status.smtp.detail}")
    print(f"sending enabled: {status.sending_enabled}")
    print(f"deleting enabled: {status.deleting_enabled}")
    print(f"recipient allowlist: {', '.join(status.recipient_allowlist) or '(everyone)'}")
    return 0 if status.imap.ok and status.smtp.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        return _run_check()

    try:
        # Fail loudly at startup rather than on the first tool call, so a
        # misconfigured server is obvious in the client's connection log.
        get_settings().require_imap()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
