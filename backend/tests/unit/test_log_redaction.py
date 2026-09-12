"""SSE credentials must not survive into the log file.

uvicorn's access logger prints the request line with its query string intact, and the SSE
stream is the one place a credential travels in a URL. The existing `server.log` in this
repository already contains real `?token=` values; this filter is what stops the next one.
"""

from __future__ import annotations

import logging

from app.core.logging import (
    RedactCredentialsFilter,
    install_credential_redaction,
    redact_credentials,
)

ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


def access_record(path: str) -> logging.LogRecord:
    """The record uvicorn's access logger actually emits."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=ACCESS_FORMAT,
        args=("127.0.0.1:51234", "GET", path, "1.1", 200),
        exc_info=None,
    )


def filtered(path: str) -> str:
    record = access_record(path)
    assert RedactCredentialsFilter().filter(record) is True
    return record.getMessage()


def test_redacts_a_ticket_from_the_access_line():
    message = filtered("/api/runs/2b1d/events?ticket=8f3c1d2e9a4b")

    assert "8f3c1d2e9a4b" not in message
    assert "ticket=REDACTED" in message
    assert "/api/runs/2b1d/events" in message


def test_redacts_a_token_and_keeps_the_other_parameters():
    message = filtered("/api/runs/2b1d/events?token=s3cret&after_seq=42")

    assert "s3cret" not in message
    assert "token=REDACTED" in message
    assert "after_seq=42" in message


def test_redacts_both_when_both_are_present():
    message = filtered("/api/runs/2b1d/events?ticket=aaa&token=bbb")

    assert "aaa" not in message and "bbb" not in message


def test_leaves_alone_a_parameter_that_merely_ends_in_token():
    assert redact_credentials("?csrf_token=keepme") == "?csrf_token=keepme"


def test_redacts_a_url_logged_as_the_message_itself():
    record = logging.LogRecord(
        name="app",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="connecting to /api/runs/x/events?token=s3cret",
        args=None,
        exc_info=None,
    )
    RedactCredentialsFilter().filter(record)

    assert "s3cret" not in record.getMessage()


def test_installing_twice_does_not_stack_filters():
    logger = logging.getLogger("uvicorn.access")
    install_credential_redaction()
    install_credential_redaction()

    installed = [f for f in logger.filters if isinstance(f, RedactCredentialsFilter)]
    assert len(installed) == 1
