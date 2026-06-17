"""structlog configuration that keeps stdout a clean MCP JSON-RPC channel.

The MCP stdio transport multiplexes the protocol over stdin/stdout; any stray bytes on
stdout (structlog defaults to it) corrupt the stream and the host fails to parse messages.
Routing all log output to stderr keeps diagnostics flowing while leaving stdout untouched.

Both transports import this so the configuration lives in exactly one place. Log events
reference account *names* only — never API keys (NFR-S4, CS-009).
"""

from __future__ import annotations

import sys

import structlog


def configure_stderr_logging() -> None:
    """Send all structlog output to stderr so stdout stays a clean JSON-RPC channel.

    Called at the very start of each transport's entry point, before any log line is
    written. Idempotent: re-invoking simply re-applies the same configuration.
    """
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
