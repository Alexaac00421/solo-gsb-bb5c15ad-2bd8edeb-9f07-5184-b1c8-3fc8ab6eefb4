"""Thread-safe in-memory store of validated hall orchestrations."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from .clock import utc_now
from .models import HallIn
from .engine import compile_hall


@dataclass
class StoredHall:
    spec: HallIn
    plan: dict
    created_at: datetime
    updated_at: datetime
    version: int


class HallStore:
    def __init__(self, lock: threading.RLock | None = None) -> None:
        self._halls: dict[str, StoredHall] = {}
        # Shared with the DeliveryManager so publish/delete and claim/confirm
        # serialise in one critical section (never two locks => no deadlock).
        self._lock = lock if lock is not None else threading.RLock()
        # Optional listener notified on every successful persist/delete while
        # the lock is held; set by the delivery layer during wiring.
        self._listener: object = None

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def set_listener(self, listener: object) -> None:
        self._listener = listener

    @staticmethod
    def compile(spec: HallIn) -> dict:
        return compile_hall(spec)

    def put(self, spec: HallIn) -> tuple[StoredHall | None, dict]:
        """Validate and persist. Returns ``(stored, error_body)`` — exactly one is set."""
        plan = compile_hall(spec)
        if not plan["ok"]:
            return None, plan
        with self._lock:
            prev = self._halls.get(spec.id)
            now = utc_now()
            stored = StoredHall(
                spec=spec,
                plan=plan,
                created_at=prev.created_at if prev else now,
                updated_at=now,
                version=(prev.version + 1) if prev else 1,
            )
            self._halls[spec.id] = stored
            if self._listener is not None:
                self._listener.on_persist(stored, now)
        return stored, None

    @staticmethod
    def version_conflict(hall_id: str, expected: int | None, current: int | None) -> dict:
        if expected is None:
            message = (
                f"Expected hall '{hall_id}' to be unsaved (expected_version=null), "
                f"but it currently exists at version {current}."
            )
        elif current is None:
            message = (
                f"Expected hall '{hall_id}' at version {expected}, but it does not exist."
            )
        else:
            message = (
                f"Expected hall '{hall_id}' at version {expected}, but current version is {current}."
            )
        return {
            "code": "version_mismatch",
            "hall_id": hall_id,
            "message": message,
            "expected_version": expected,
            "current_version": current,
        }

    def batch_check_versions(self, items: list[tuple[str, int | None]]) -> list[dict]:
        """Read-only optimistic-lock check for ``(hall_id, expected_version)``.

        Returns one ``version_mismatch`` conflict per item whose expected
        version differs from the current store state. Takes the lock so the
        snapshot is consistent; it never mutates anything.
        """
        conflicts: list[dict] = []
        with self._lock:
            for hall_id, expected in items:
                prev = self._halls.get(hall_id)
                current = prev.version if prev else None
                if expected != current:
                    conflicts.append(self.version_conflict(hall_id, expected, current))
        return conflicts

    def batch_commit(
        self, items: list[tuple[str, int | None, HallIn, dict]]
    ) -> tuple[list[StoredHall] | None, list[dict]]:
        """Atomically replace a batch of already-compiled halls.

        Each item is ``(hall_id, expected_version, spec, compiled_plan)`` and the
        caller guarantees every orchestration compiled successfully.

        Version comparison and every write happen in one critical section, so
        either all halls are replaced (each version incremented once) or nothing
        is written. Concurrent batches that expect the same version serialise
        here and at most the first one wins; later callers see the bumped version
        and are rejected.

        Returns ``(stored_halls, [])`` on success or ``(None, conflicts)`` with a
        ``version_mismatch`` per offending item when nothing was committed.
        """
        with self._lock:
            conflicts: list[dict] = []
            for hall_id, expected, _spec, _plan in items:
                prev = self._halls.get(hall_id)
                current = prev.version if prev else None
                if expected != current:
                    conflicts.append(self.version_conflict(hall_id, expected, current))

            # Re-check here (after the read-only pre-check) closes the race with
            # a concurrent single/batch publish between check and commit.
            if conflicts or not items:
                return None, conflicts

            now = utc_now()
            stored_list: list[StoredHall] = []
            for hall_id, _expected, spec, plan in items:
                prev = self._halls.get(hall_id)
                stored = StoredHall(
                    spec=spec,
                    plan=plan,
                    created_at=prev.created_at if prev else now,
                    updated_at=now,
                    version=(prev.version + 1) if prev else 1,
                )
                self._halls[hall_id] = stored
                stored_list.append(stored)
                if self._listener is not None:
                    # Each hall in a batch gets its own version watermark at the
                    # commit instant; old-version leases are invalidated first.
                    self._listener.on_persist(stored, now)
        return stored_list, []

    def get(self, hall_id: str) -> StoredHall | None:
        with self._lock:
            return self._halls.get(hall_id)

    def list(self) -> list[StoredHall]:
        with self._lock:
            return list(self._halls.values())

    def delete(self, hall_id: str) -> bool:
        with self._lock:
            existed = self._halls.pop(hall_id, None) is not None
            if existed and self._listener is not None:
                self._listener.on_delete(hall_id)
            return existed


store = HallStore()
