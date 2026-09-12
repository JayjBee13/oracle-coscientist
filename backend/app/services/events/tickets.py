"""Short-lived, single-use tickets so an `EventSource` can prove who it is.

The browser's `EventSource` cannot send headers. If the live stream is to be watchable at
all with `APP_AUTH_TOKEN` set, some credential has to travel in the URL — where it lands
in the access log, in browser history and in any referrer a page sends onward. So the
frontend spends its real token once, over an ordinary fetch it *can* put a header on, and
gets back a ticket that is good for sixty seconds and exactly one connection.

The store is a dict in this process, deliberately. The app is one uvicorn worker on the
scientist's own machine; a ticket that survived a restart would be a persistent credential,
which is the thing this exists to avoid.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

TICKET_TTL_SECONDS = 60


@dataclass(frozen=True)
class _Issued:
    run_id: str
    expires_at: float


class TicketStore:
    """Issue and redeem SSE tickets. Every ticket is good for one run and one use."""

    def __init__(
        self,
        ttl_seconds: float = TICKET_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._issued: dict[str, _Issued] = {}

    def issue(self, run_id: str) -> str:
        self.purge_expired()
        ticket = uuid.uuid4().hex
        self._issued[ticket] = _Issued(str(run_id), self._clock() + self.ttl_seconds)
        return ticket

    def consume(self, ticket: str, run_id: str) -> bool:
        """Redeem a ticket for this run. A ticket is spent whether or not it was valid.

        Spending a mismatched ticket is intentional: presenting one against the wrong run
        is either a bug or an attempt, and neither deserves a second try.
        """
        issued = self._issued.pop(ticket, None)
        if issued is None:
            return False
        if issued.expires_at <= self._clock():
            return False
        return issued.run_id == str(run_id)

    def purge_expired(self) -> None:
        now = self._clock()
        expired = [key for key, issued in self._issued.items() if issued.expires_at <= now]
        for key in expired:
            del self._issued[key]

    def __len__(self) -> int:
        return len(self._issued)


# One store for the process: the auth middleware redeems tickets that the events router
# issued, and they have no other way to reach each other.
TICKETS = TicketStore()
