"""Injectable UTC clock.

Every time-dependent decision in the service — the publish watermark, command
due filtering and lease expiry — goes through :data:`clock`, a single
:class:`Clock` instance. Production callers read the real wall clock; tests can
freeze or advance time deterministically (see ``FakeClock`` in the test suite)
so lease timeouts and version watermarks are reproducible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable


class Clock:
    """Returns the current time as a timezone-aware UTC ``datetime``."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# Tests replace ``clock.now`` (or the whole object) to control time; production
# code must never read ``datetime.now`` directly.
clock = Clock()


def utc_now() -> datetime:
    return clock.now()


# Convenience for tests: swap the source function temporarily.
def set_source(fn: Callable[[], datetime]) -> None:
    clock.now = fn  # type: ignore[method-assign]
