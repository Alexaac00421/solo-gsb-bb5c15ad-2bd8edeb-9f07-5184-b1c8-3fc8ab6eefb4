"""HTTP API for scheduled museum lighting scene orchestration."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from .delivery import DeliveryManager
from .diff import Side, diff_plans
from .models import AckIn, BatchPublishIn, ClaimIn, HallIn, VersionDiffIn, as_utc
from .simulator import simulate
from .clock import utc_now
from .store import StoredHall, store

app = FastAPI(
    title="Museum Lighting Orchestration API",
    version="1.2.0",
    description=(
        "Compile scheduled light scenes into an executable per-channel timeline. "
        "Validates lamp references, brightness ranges and non-overlapping fade intervals; "
        "emergency scenes preempt normal scenes and release only to scenes still in window. "
        "Gateways claim due commands per channel under a lease and confirm them with the "
        "current lease token; republishing or deleting a hall invalidates the old version. "
        "Operations can rehearse a read-only diff (adds/removes/level/fade/priority changes "
        "plus preemption and publish-watermark delivery impact) between any two retained "
        "published versions of one hall via POST /halls/version-diff."
    ),
)

# Publish/delete and gateway claim/confirm share the store's single lock, so a
# confirmation can never slip across a version boundary.
delivery = DeliveryManager(store.lock)
store.set_listener(delivery)


def _stored_or_404(hall_id: str) -> StoredHall:
    stored = store.get(hall_id)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "hall_not_found", "message": f"Hall '{hall_id}' does not exist."},
        )
    return stored


def _plan_response(stored: StoredHall) -> dict[str, Any]:
    return {
        "hall_id": stored.spec.id,
        "name": stored.spec.name,
        "version": stored.version,
        "created_at": stored.created_at.isoformat(),
        "updated_at": stored.updated_at.isoformat(),
        **{k: stored.plan[k] for k in ("summary", "warnings", "timeline", "preemptions", "skipped_activations")},
    }


def _reject_invalid(plan: dict, hall_id: str) -> JSONResponse:
    # 422 with concrete channel / interval / reason; nothing was written.
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "hall_id": hall_id,
            "message": "Orchestration rejected; no timeline was saved.",
            "errors": plan["errors"],
            "warnings": plan["warnings"],
        },
    )


def _batch_failure_reason(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"code": reason, "message": message, **extra}
    return out


def _batch_rejected(failures: list[dict[str, Any]]) -> JSONResponse:
    # Any optimistic-lock conflict is a 409; pure content errors are 422.
    # Nothing in the batch was written and every stored version is unchanged.
    status_code = 409 if any(f["reason"]["code"] == "version_mismatch" for f in failures) else 422
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "message": "Batch publish rejected; no hall was written and all versions are unchanged.",
            "failures": failures,
        },
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "halls": len(store.list())}


@app.put(
    "/halls/{hall_id}",
    status_code=status.HTTP_200_OK,
    tags=["halls"],
    summary="Validate and persist a hall orchestration (full replace)",
)
def put_hall(hall_id: str, hall: HallIn) -> Any:
    if hall.id != hall_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "id_mismatch",
                "message": f"Path hall id '{hall_id}' does not match body hall id '{hall.id}'.",
            },
        )
    stored, plan = store.put(hall)
    if stored is None:
        return _reject_invalid(plan, hall_id)
    return _plan_response(stored)


@app.post(
    "/halls/batch-publish",
    status_code=status.HTTP_200_OK,
    tags=["halls"],
    summary="Atomically validate and publish multiple full hall orchestrations",
)
def batch_publish(batch: BatchPublishIn) -> Any:
    """Publish a whole batch of halls or nothing.

    Each item carries the target ``hall_id``, the ``expected_version`` it is
    replacing (``null`` for a hall that has never been saved) and the full
    ``orchestration``. Items reuse the exact single-hall validation and
    timeline synthesis. Duplicate ids inside one request, an id/orchestration
    mismatch, an invalid orchestration, or an expected-version mismatch causes
    the whole batch to be rejected with per-hall reasons; no version changes.
    On success every hall is atomically replaced and its version increments by 1.
    """
    items = batch.halls

    # Phase 1 (no lock, pure functions): content checks that never depend on
    # current store state — duplicate ids, id/orchestration mismatch, compile.
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        (duplicates if item.hall_id in seen else seen).add(item.hall_id)

    failures: list[dict[str, Any]] = []
    content_failed: set[int] = set()
    # Every non-duplicate item still goes through the read-only version check,
    # so a batch where hall A is invalid and hall B is stale reports both at
    # once. Each item contributes at most one reason (a content error takes
    # precedence); only items that compiled successfully are ever committed.
    version_checks: list[tuple[str, int | None]] = []
    committable: list[tuple[str, int | None, HallIn, dict]] = []
    for index, item in enumerate(items):
        entry_base = {"index": index, "hall_id": item.hall_id}
        if item.hall_id in duplicates:
            failures.append(
                {
                    **entry_base,
                    "reason": _batch_failure_reason(
                        "duplicate_in_batch",
                        f"Hall id '{item.hall_id}' appears more than once in the same batch.",
                    ),
                }
            )
            content_failed.add(index)
            continue
        version_checks.append((item.hall_id, item.expected_version))
        if item.orchestration.id != item.hall_id:
            failures.append(
                {
                    **entry_base,
                    "reason": _batch_failure_reason(
                        "id_mismatch",
                        f"Item hall id '{item.hall_id}' does not match orchestration id "
                        f"'{item.orchestration.id}'.",
                    ),
                }
            )
            content_failed.add(index)
            continue
        plan = store.compile(item.orchestration)
        if not plan["ok"]:
            failures.append(
                {
                    **entry_base,
                    "reason": _batch_failure_reason(
                        "orchestration_invalid",
                        "Orchestration rejected; no timeline was saved.",
                        errors=plan["errors"],
                        warnings=plan["warnings"],
                    ),
                }
            )
            content_failed.add(index)
            continue
        committable.append((item.hall_id, item.expected_version, item.orchestration, plan))

    # Read-only optimistic-lock snapshot for every addressable item. A version
    # conflict is reported only when the item has no content failure already.
    conflicts = store.batch_check_versions(version_checks)
    for c in conflicts:
        hall_id = c["hall_id"]
        index = next(i for i, it in enumerate(items) if it.hall_id == hall_id)
        if index in content_failed:
            continue
        failures.append({"index": index, "hall_id": hall_id, "reason": c})

    if failures:
        failures.sort(key=lambda f: f["index"])
        return _batch_rejected(failures)

    # All content checks and versions match — replace the whole batch atomically.
    # batch_commit re-checks versions under the write lock, closing the race with
    # a concurrent publish between the pre-check above and this commit.
    stored_list, commit_conflicts = store.batch_commit(committable)
    if commit_conflicts:
        # Lost an optimistic-lock race (same expected version) to another writer.
        for c in commit_conflicts:
            hall_id = c["hall_id"]
            failures.append(
                {
                    "index": next(i for i, it in enumerate(items) if it.hall_id == hall_id),
                    "hall_id": hall_id,
                    "reason": c,
                }
            )
        failures.sort(key=lambda f: f["index"])
        return _batch_rejected(failures)

    # Preserve the order the caller submitted; each hall bumped its version once.
    assert stored_list is not None
    by_id = {s.spec.id: s for s in stored_list}
    return {
        "ok": True,
        "count": len(stored_list),
        "published": [_plan_response(by_id[it.hall_id]) for it in items],
    }


@app.post(
    "/validate",
    status_code=status.HTTP_200_OK,
    tags=["halls"],
    summary="Validate/compile without persisting (dry run)",
)
def validate(hall: HallIn) -> Any:
    plan = store.compile(hall)
    if not plan["ok"]:
        return _reject_invalid(plan, hall.id)
    return {"ok": True, "hall_id": hall.id, **{k: plan[k] for k in ("summary", "warnings", "timeline", "preemptions", "skipped_activations")}}


@app.get("/halls", tags=["halls"], summary="List persisted halls")
def list_halls() -> dict:
    items = [
        {
            "hall_id": s.spec.id,
            "name": s.spec.name,
            "version": s.version,
            "updated_at": s.updated_at.isoformat(),
            **s.plan["summary"],
        }
        for s in store.list()
    ]
    return {"halls": items, "count": len(items)}


@app.get("/halls/{hall_id}", tags=["halls"], summary="Get the compiled plan/timeline")
def get_hall(hall_id: str) -> dict:
    return _plan_response(_stored_or_404(hall_id))


@app.get(
    "/halls/{hall_id}/timeline",
    tags=["halls"],
    summary="Get just the executable per-channel command timeline",
)
def get_timeline(hall_id: str) -> dict:
    stored = _stored_or_404(hall_id)
    return {"hall_id": hall_id, "timeline": stored.plan["timeline"], "summary": stored.plan["summary"]}


@app.get(
    "/halls/{hall_id}/state",
    tags=["halls"],
    summary="Simulate effective channel levels at a given time",
)
def get_state(
    hall_id: str,
    at: datetime | None = Query(default=None, description="ISO 8601 time; defaults to now (UTC)."),
) -> dict:
    stored = _stored_or_404(hall_id)
    t = as_utc(at) if at is not None else datetime.now(timezone.utc)
    return simulate(stored.plan, t)


@app.get(
    "/halls/{hall_id}/versions",
    tags=["halls"],
    summary="List retained published versions (for comparison rehearsals)",
)
def list_versions(hall_id: str) -> dict:
    """Every published version still retained for a hall, oldest first.

    Version numbers are the same values returned by the publish endpoints; the
    current (served) version is flagged. Use two of these numbers as input to
    ``POST /halls/version-diff``.
    """
    stored = _stored_or_404(hall_id)
    current = stored.version
    versions = [
        {
            "version": s.version,
            "current": s.version == current,
            "published_at": s.updated_at.isoformat(),
            "created_at": s.created_at.isoformat(),
            **s.plan["summary"],
        }
        for s in store.versions(hall_id)
    ]
    return {"hall_id": hall_id, "current_version": current, "versions": versions, "count": len(versions)}


@app.post(
    "/halls/version-diff",
    status_code=status.HTTP_200_OK,
    tags=["halls"],
    summary="Rehearse the diff between two published versions (read-only)",
)
def version_diff(body: VersionDiffIn) -> Any:
    """Compare two *existing published* versions of the **same** hall.

    Returns per-channel command pairs (paired by execution instant; same
    ``(type, scene_id)`` preferred within an instant) classified as added,
    removed or changed — level / fade / priority / restore / type deltas are
    itemised — together with the delivery impact caused by emergency-scene
    preemption and by each version's publish watermark (commands due before a
    version was published are never delivered), and whether outstanding
    (leased/unacked) base tasks would be voided by switching versions.

    The rehearsal is rejected when either referenced version does not exist
    (404), when the two references name different halls (422 ``hall_mismatch``)
    or when both reference the same version (422 ``same_version``). It is a
    pure read: the current orchestration, delivery queues, heads and leases are
    never modified.
    """
    refs = (body.base, body.target)

    # 1) The two references must describe one hall.
    if body.base.hall_id != body.target.hall_id:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "message": "Version comparison rejected; the two versions must belong to the same hall.",
                "errors": [
                    {
                        "code": "hall_mismatch",
                        "message": (
                            f"base references hall '{body.base.hall_id}' but target references "
                            f"hall '{body.target.hall_id}'."
                        ),
                        "base_hall_id": body.base.hall_id,
                        "target_hall_id": body.target.hall_id,
                    }
                ],
            },
        )

    # 2) Comparing a version with itself is meaningless.
    if body.base.version == body.target.version:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "message": "Version comparison rejected; base and target must be different versions.",
                "errors": [
                    {
                        "code": "same_version",
                        "hall_id": body.base.hall_id,
                        "version": body.base.version,
                        "message": f"Both references point at version {body.base.version} of '{body.base.hall_id}'.",
                    }
                ],
            },
        )

    # 3) Both versions must exist (both problems are reported at once).
    missing = [
        {"which": which, "hall_id": ref.hall_id, "version": ref.version,
         "code": "version_not_found",
         "message": f"Hall '{ref.hall_id}' has no retained published version {ref.version}."}
        for which, ref in (("base", refs[0]), ("target", refs[1]))
        if store.get_version(ref.hall_id, ref.version) is None
    ]
    if missing:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "message": "Version comparison rejected; one or both versions do not exist.",
                "errors": missing,
            },
        )

    base_stored = store.get_version(body.base.hall_id, body.base.version)
    target_stored = store.get_version(body.target.hall_id, body.target.version)
    assert base_stored is not None and target_stored is not None
    now = utc_now()

    # Read-only snapshots of the live queues (None for superseded versions);
    # nothing in delivery is created, advanced or invalidated.
    base_side = Side(
        hall_id=base_stored.spec.id,
        version=base_stored.version,
        plan=base_stored.plan,
        published_at=base_stored.updated_at,
        view=delivery.version_view(base_stored),
        now=now,
        created_at=base_stored.created_at,
        updated_at=base_stored.updated_at,
    )
    target_side = Side(
        hall_id=target_stored.spec.id,
        version=target_stored.version,
        plan=target_stored.plan,
        published_at=target_stored.updated_at,
        view=delivery.version_view(target_stored),
        now=now,
        created_at=target_stored.created_at,
        updated_at=target_stored.updated_at,
    )
    return diff_plans(base_side, target_side)


@app.post(
    "/halls/{hall_id}/commands/claim",
    status_code=status.HTTP_200_OK,
    tags=["gateway"],
    summary="Claim due per-channel commands under a lease",
)
def claim_commands(hall_id: str, body: ClaimIn) -> Any:
    """Hand a gateway up to ``max_count`` due commands for one hall.

    Delivery is per channel in execution-time order: a channel contributes at
    most its *head* command, and while that command is held by a live lease the
    channel contributes nothing (the next command is blocked until the head is
    confirmed). Each task carries a stable ``task_id``, the published
    ``version`` it belongs to, the executable ``command`` and a ``lease`` token.
    A live lease is never issued twice; once it expires the same task can be
    re-claimed with a new token. An empty ``delivered`` list is a normal poll
    (nothing due / every head still leased).
    """
    stored = _stored_or_404(hall_id)
    return delivery.claim(stored, body.max_count, body.lease_seconds)


@app.post(
    "/halls/{hall_id}/commands/ack",
    status_code=status.HTTP_200_OK,
    tags=["gateway"],
    summary="Confirm a delivered command with its current lease token",
)
def acknowledge_command(hall_id: str, body: AckIn) -> Any:
    """Confirm one task. The token must be the task's current lease token.

    Re-confirming the same token returns the same success (idempotent). A token
    that timed out or was replaced by a re-claim is a 409 conflict and changes
    nothing. A token issued under a hall version that has since been
    republished or deleted reports 410 ``version_expired``.
    """
    code, payload = delivery.confirm(hall_id, body.task_id, body.lease_token)
    if code != 200:
        return JSONResponse(status_code=code, content=payload)
    return payload


@app.get(
    "/halls/{hall_id}/delivery",
    tags=["gateway"],
    summary="Inspect per-channel delivery/lease state (operational view)",
)
def delivery_status(hall_id: str) -> dict:
    stored = _stored_or_404(hall_id)
    return delivery.status(stored)


@app.delete(
    "/halls/{hall_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["halls"],
    summary="Delete a hall orchestration",
)
def delete_hall(hall_id: str) -> Response:
    if not store.delete(hall_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "hall_not_found", "message": f"Hall '{hall_id}' does not exist."},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
