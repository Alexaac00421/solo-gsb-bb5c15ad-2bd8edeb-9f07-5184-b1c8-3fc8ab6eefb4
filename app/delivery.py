"""Per-channel command delivery queue with leases and version invalidation.

A published orchestration compiles into an ordered command timeline per
channel (lamp). A DMX/lighting *gateway* claims the commands that are due for a
hall and acknowledges each after sending it to the bus. Delivery is a strict
per-channel FIFO with a leasing/visibility-timeout layer:

* Each channel hands out its head command in timeline order; the next command
  on a channel is never claimable while the previous one is unacknowledged
  (head-of-line blocking).
* A claim returns a ``lease_token``. The command is not handed out again while
  its lease is live. When the lease expires the same *stable* task may be
  re-claimed; a fresh token is issued and the old token stops confirming it.
* Confirmation must present the current token. Re-confirming the same token is
  idempotent. An old (superseded/expired) token gets a conflict and changes
  nothing.
* Republishing or deleting a hall invalidates every unacknowledged task of the
  previous version: its tokens report ``version_expired`` on confirm. The new
  version is watermarked at its publish time and never back-fills commands
  whose execution time is before that watermark.

All state lives behind the store's single re-entrant lock, so a concurrent
claim / confirm / publish serialises per hall and a confirmation can never
cross a version boundary.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .clock import utc_now

# Lease token lifecycle states.
LEASED = "leased"          # currently held; the only token that can confirm
ACKED = "acked"            # command confirmed (re-confirm is idempotent)
SUPERSEDED = "superseded"  # lease timed out and a replacement token was issued
VERSION_EXPIRED = "version_expired"  # hall republished/deleted after issuance

_SEQ_WIDTH = 6


def task_ident(hall_id: str, version: int, channel: str, seq: int) -> str:
    """Stable identifier for one (hall version, channel, timeline position)."""
    return f"{hall_id}:v{version}:{channel}:{seq:0{_SEQ_WIDTH}d}"


@dataclass
class LeaseRecord:
    token: str
    task_id: str
    hall_id: str
    version: int
    channel: str
    seq: int
    issued_at: datetime
    expires_at: datetime

    # Mutable lifecycle marker; the registry is the authority on validity.
    state: str = LEASED

    def is_live(self, now: datetime) -> bool:
        return self.state == LEASED and now < self.expires_at


@dataclass
class ChannelQueue:
    channel: str
    # Timeline commands at/after the publish watermark, already in execution
    # order; ``head`` is the sequence of the first unacknowledged command.
    entries: list[dict]
    head: int = 0
    lease: LeaseRecord | None = None  # non-None while the head is handed out

    @property
    def pending(self) -> int:
        return len(self.entries) - self.head


@dataclass
class DeliveryQueue:
    hall_id: str
    version: int
    published_at: datetime
    channels: dict[str, ChannelQueue]
    # Stable channel iteration order for deterministic, fair selection when a
    # claim's max_count is smaller than the number of ready channels.
    order: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, stored, published_at: datetime) -> "DeliveryQueue":
        timeline: dict[str, list[dict]] = stored.plan["timeline"]
        channels: dict[str, ChannelQueue] = {}
        for channel in sorted(timeline):
            # Watermark is half-open at the lower bound: a command due exactly
            # at publish time belongs to the new version.
            eligible = [
                cmd
                for cmd in timeline[channel]
                if datetime.fromisoformat(cmd["at"]) >= published_at
            ]
            channels[channel] = ChannelQueue(channel=channel, entries=eligible)
        return cls(
            hall_id=stored.spec.id,
            version=stored.version,
            published_at=published_at,
            channels=channels,
            order=sorted(channels),
        )


class DeliveryManager:
    def __init__(self, lock: threading.RLock) -> None:
        self._lock = lock
        # hall -> current published version (key absent/None if deleted).
        self._current: dict[str, int | None] = {}
        self._queues: dict[str, DeliveryQueue] = {}
        # token -> lease, and hall -> set of tokens ever issued under it (kept
        # as tombstones so an old token still reports a precise expiry/conflict
        # instead of an opaque 404).
        self._tokens: dict[str, LeaseRecord] = {}
        self._hall_tokens: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ #
    # Store hooks (invoked while the store already holds ``self._lock``).
    # ------------------------------------------------------------------ #
    def on_persist(self, stored, published_at: datetime) -> None:
        with self._lock:
            self._expire_hall(stored.spec.id)
            self._queues[stored.spec.id] = DeliveryQueue.build(stored, published_at)
            self._current[stored.spec.id] = stored.version

    def on_delete(self, hall_id: str) -> None:
        with self._lock:
            self._expire_hall(hall_id)
            self._queues.pop(hall_id, None)
            self._current[hall_id] = None

    def _expire_hall(self, hall_id: str) -> None:
        for tok in self._hall_tokens.get(hall_id, ()):  # acked leases included
            self._tokens[tok].state = VERSION_EXPIRED
        self._hall_tokens[hall_id] = set()

    def clear(self) -> None:
        """Reset all delivery state (tests only)."""
        with self._lock:
            self._current.clear()
            self._queues.clear()
            self._tokens.clear()
            self._hall_tokens.clear()

    # ------------------------------------------------------------------ #
    # Claim
    # ------------------------------------------------------------------ #
    def claim(self, stored, max_count: int, lease_seconds: int) -> dict:
        now = utc_now()
        with self._lock:
            queue = self._queues.get(stored.spec.id)
            if queue is None or queue.version != stored.version:
                # Defensive: always keep a queue in step with the stored plan.
                queue = DeliveryQueue.build(stored, stored.updated_at)
                self._queues[stored.spec.id] = queue
                self._current[stored.spec.id] = stored.version

            picked: list[tuple[datetime, str, dict, LeaseRecord]] = []
            for channel in queue.order:
                if len(picked) >= max_count:
                    break
                cq = queue.channels[channel]
                if cq.head >= len(cq.entries):
                    continue  # every eligible command on this channel is acked
                cmd = cq.entries[cq.head]
                due = datetime.fromisoformat(cmd["at"])
                if due > now:
                    continue  # not due yet; later commands on the channel aren't either
                lease = cq.lease
                if lease is not None and lease.is_live(now):
                    continue  # within its lease: do not hand the task out again
                if lease is not None:
                    # Previous lease expired (or was otherwise left behind): the
                    # same stable task is re-leased under a brand-new token.
                    lease.state = SUPERSEDED

                token = secrets.token_urlsafe(24)
                rec = LeaseRecord(
                    token=token,
                    task_id=task_ident(stored.spec.id, queue.version, channel, cq.head),
                    hall_id=stored.spec.id,
                    version=queue.version,
                    channel=channel,
                    seq=cq.head,
                    issued_at=now,
                    expires_at=self._expiry(now, lease_seconds),
                )
                self._tokens[token] = rec
                self._hall_tokens.setdefault(stored.spec.id, set()).add(token)
                cq.lease = rec
                picked.append((due, channel, cmd, rec))

            # Deliver the batch in execution-time order (channel tie-breaks),
            # while each individual channel still respects its own FIFO.
            picked.sort(key=lambda p: (p[0], p[1]))
            delivered = [
                self._task_payload(queue, cmd, rec, lease_seconds)
                for _due, _ch, cmd, rec in picked
            ]
            return {
                "hall_id": stored.spec.id,
                "version": queue.version,
                "published_at": queue.published_at.isoformat(),
                "server_time": now.isoformat(),
                "max_count": max_count,
                "lease_seconds": lease_seconds,
                "count": len(delivered),
                "delivered": delivered,
            }

    @staticmethod
    def _expiry(now: datetime, lease_seconds: int) -> datetime:
        return now + timedelta(seconds=lease_seconds)

    def _task_payload(self, queue: DeliveryQueue, cmd: dict, rec: LeaseRecord, lease_seconds: int) -> dict:
        return {
            "task_id": rec.task_id,
            "hall_id": rec.hall_id,
            "version": rec.version,
            "channel": rec.channel,
            "sequence": rec.seq,
            "at": cmd["at"],
            "command": cmd,
            "lease": {
                "token": rec.token,
                "lease_seconds": lease_seconds,
                "issued_at": rec.issued_at.isoformat(),
                "expires_at": rec.expires_at.isoformat(),
            },
        }

    # ------------------------------------------------------------------ #
    # Confirm
    # ------------------------------------------------------------------ #
    def confirm(self, hall_id: str, task_id: str, token: str) -> tuple[int, dict]:
        now = utc_now()
        with self._lock:
            rec = self._tokens.get(token)
            if rec is None:
                return 404, {
                    "code": "lease_not_found",
                    "message": "Unknown lease token; it was never issued or the service restarted.",
                    "hall_id": hall_id,
                    "task_id": task_id,
                }
            if rec.hall_id != hall_id or rec.task_id != task_id:
                return 409, {
                    "code": "ack_target_mismatch",
                    "message": "The lease token was issued for a different hall/task than the one being confirmed.",
                    "hall_id": hall_id,
                    "task_id": task_id,
                    "token_hall_id": rec.hall_id,
                    "token_task_id": rec.task_id,
                }

            if rec.state == VERSION_EXPIRED:
                return 410, {
                    "code": "version_expired",
                    "message": (
                        f"Hall '{hall_id}' was republished or deleted after this task was leased "
                        f"(task version {rec.version}); unacknowledged tasks of that version are void."
                    ),
                    "hall_id": hall_id,
                    "task_id": task_id,
                    "task_version": rec.version,
                    "current_version": self._current.get(hall_id),
                }
            if rec.state == SUPERSEDED:
                return 409, {
                    "code": "lease_token_conflict",
                    "message": "This lease timed out and a newer lease token was issued for the task; confirm with the current token.",
                    "hall_id": hall_id,
                    "task_id": task_id,
                }
            if rec.state == ACKED:
                # Duplicate confirmation of the same token: idempotent success.
                return 200, self._ack_body(rec, idempotent=True, confirmed_at=now)
            if now >= rec.expires_at:
                # LEASED but past its deadline: old token is no longer authoritative.
                return 409, {
                    "code": "lease_expired",
                    "message": "The lease has expired; re-claim the task and confirm with the freshly issued token.",
                    "hall_id": hall_id,
                    "task_id": task_id,
                    "expires_at": rec.expires_at.isoformat(),
                }

            # Current, live token: commit the acknowledgment and advance the
            # channel's head, releasing the next command for a future claim.
            queue = self._queues.get(hall_id)
            cq = queue.channels.get(rec.channel) if queue else None
            if queue is None or queue.version != rec.version or cq is None or cq.lease is not rec:
                # Defensive: registry/queue desync — treat the version as stale.
                return 410, {
                    "code": "version_expired",
                    "message": "The task is no longer the current head of its channel.",
                    "hall_id": hall_id,
                    "task_id": task_id,
                    "task_version": rec.version,
                    "current_version": self._current.get(hall_id),
                }

            rec.state = ACKED
            cq.lease = None
            cq.head = rec.seq + 1
            return 200, self._ack_body(rec, idempotent=False, confirmed_at=now)

    @staticmethod
    def _ack_body(rec: LeaseRecord, idempotent: bool, confirmed_at: datetime) -> dict:
        return {
            "ok": True,
            "idempotent": idempotent,
            "hall_id": rec.hall_id,
            "version": rec.version,
            "task_id": rec.task_id,
            "channel": rec.channel,
            "sequence": rec.seq,
            "confirmed_at": confirmed_at.isoformat(),
        }

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def status(self, stored) -> dict:
        with self._lock:
            queue = self._queues.get(stored.spec.id)
            if queue is None or queue.version != stored.version:
                queue = DeliveryQueue.build(stored, stored.updated_at)
            now = utc_now()
            channels = {}
            for channel in queue.order:
                cq = queue.channels[channel]
                lease = cq.lease
                channels[channel] = {
                    "eligible_commands": len(cq.entries),
                    "head_sequence": cq.head,
                    "pending": cq.pending,
                    "leased": None
                    if lease is None
                    else {
                        "task_id": lease.task_id,
                        "state": "live" if lease.is_live(now) else "expired",
                        "issued_at": lease.issued_at.isoformat(),
                        "expires_at": lease.expires_at.isoformat(),
                    },
                }
            return {
                "hall_id": queue.hall_id,
                "version": queue.version,
                "published_at": queue.published_at.isoformat(),
                "server_time": now.isoformat(),
                "channels": channels,
            }
