"""Keep SSE credentials out of the log file.

uvicorn's access log prints the request line verbatim, query string included — the
previous server.log has real `?token=` values sitting in it. The live stream has to carry
its credential in the URL (an `EventSource` cannot send headers), so the log line is where
that credential would otherwise come to rest: a plain file, often pasted into a bug report.

This is a filter rather than a formatter because a filter reaches the record before any
handler formats it, so a second handler added later cannot leak what the first one hides.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# Matches `token=` / `ticket=` as a whole word, so `csrf_token=` and the like are left
# alone; the value runs to the next parameter, quote or space.
CREDENTIAL_PATTERN = re.compile(r"(?i)(?<![\w-])(token|ticket)=([^&\s\"']*)")
REDACTED = "REDACTED"

_TARGET_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn")


def redact_credentials(text: str) -> str:
    return CREDENTIAL_PATTERN.sub(rf"\1={REDACTED}", text)


class RedactCredentialsFilter(logging.Filter):
    """Rewrite `token=`/`ticket=` out of a record before anything formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_credentials(record.msg)
        record.args = _redact_args(record.args)
        return True


def _redact_args(args: Any) -> Any:
    if isinstance(args, tuple):
        return tuple(redact_credentials(a) if isinstance(a, str) else a for a in args)
    if isinstance(args, dict):
        return {
            key: redact_credentials(value) if isinstance(value, str) else value
            for key, value in args.items()
        }
    if isinstance(args, str):
        return redact_credentials(args)
    return args


def install_credential_redaction() -> None:
    """Attach the filter to uvicorn's loggers and to whatever the root is handling.

    Safe to call more than once: creating a second app in the same process (which the
    tests do constantly) must not stack duplicate filters.
    """
    targets: list[logging.Logger | logging.Handler] = [
        logging.getLogger(name) for name in _TARGET_LOGGERS
    ]
    targets.extend(logging.getLogger().handlers)
    for target in targets:
        if not any(isinstance(existing, RedactCredentialsFilter) for existing in target.filters):
            target.addFilter(RedactCredentialsFilter())
