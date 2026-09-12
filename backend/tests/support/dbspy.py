"""Catch a read path that writes.

`GET /workshops/{id}` used to reconcile state on read, so fetching a workshop could change
it. Asserting on the response is not enough to prove that is gone — the fix has to be that
no session flushes at all while the request is in flight.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

__all__ = ["no_writes"]


@contextmanager
def no_writes() -> Iterator[list[str]]:
    """Collect a description of every session flush that happens inside the block."""
    flushed: list[str] = []

    def spy(session: Session, _context: Any) -> None:
        flushed.append(
            ", ".join(
                sorted(
                    type(obj).__name__
                    for obj in [*session.new, *session.dirty, *session.deleted]
                )
            )
            or "unknown"
        )

    event.listen(Session, "after_flush", spy)
    try:
        yield flushed
    finally:
        event.remove(Session, "after_flush", spy)
