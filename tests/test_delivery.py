"""End-to-end tests for the gateway command-claim / lease / confirm queue."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock as clock_module
from app.main import app, delivery
from app.store import store


client = TestClient(app)

D = (2026, 10, 6)


def iso(h, mi=0):
    return datetime(D[0], D[1], D[2], h, mi, tzinfo=timezone.utc).isoformat()


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _isolate():
    store._halls.clear()
    delivery.clear()
    yield
    store._halls.clear()
    delivery.clear()


@pytest.fixture
def freeze(monkeypatch):
    fc = FakeClock(datetime(D[0], D[1], D[2], 7, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(clock_module.clock, "now", fc.now)
    return fc


# --------------------------------------------------------------------------- #
# Hall builders (instant fades; consecutive hourly windows touch at endpoints).
# --------------------------------------------------------------------------- #
def multiscene_hall(hid="h", channels=("L1",), hours=(8, 9, 10), levels=None):
    lamps = [{"id": c, "default_level": 0} for c in channels]
    levels = levels or [40 + i * 10 for i in range(len(hours))]
    scenes = [
        {
            "id": f"s{i}",
            "start_at": iso(h),
            "end_at": iso(h + 1),
            "items": [{"lamp_id": c, "level": levels[i], "fade_seconds": 0} for c in channels],
        }
        for i, h in enumerate(hours)
    ]
    return {"id": hid, "lamps": lamps, "scenes": scenes}


def publish(hid="h", **kw):
    body = multiscene_hall(hid, **kw)
    r = client.put(f"/halls/{hid}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def claim(hid="h", max_count=10, lease=30):
    return client.post(
        f"/halls/{hid}/commands/claim",
        json={"max_count": max_count, "lease_seconds": lease},
    )


def ack(hid, task_id, token):
    return client.post(f"/halls/{hid}/commands/ack", json={"task_id": task_id, "lease_token": token})


def first_task(r):
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    return body["delivered"][0]


# --------------------------------------------------------------------------- #
def test_claim_returns_stable_task_id_version_command_and_lease(freeze):
    publish()
    freeze.advance(3600)  # 08:00 — first command is due
    r = claim(lease=60)
    t = first_task(r)
    body = r.json()
    assert body["version"] == 1
    assert body["lease_seconds"] == 60
    assert t["task_id"] == "h:v1:L1:000000"
    assert t["channel"] == "L1" and t["sequence"] == 0
    assert t["version"] == 1 and t["hall_id"] == "h"
    assert t["at"] == iso(8)
    assert t["command"]["type"] == "fade"
    assert t["command"]["to_level"] == 40
    assert t["lease"]["token"]
    assert t["lease"]["expires_at"] == iso(8, 1)
    # payload carries the publish watermark / server time too
    assert body["published_at"] == iso(7)
    assert body["server_time"] == iso(8)


def test_empty_poll_when_nothing_due_is_still_200(freeze):
    publish()
    freeze.advance(1800)  # 07:30
    r = claim()
    assert r.status_code == 200
    assert r.json()["count"] == 0 and r.json()["delivered"] == []


def test_head_of_line_blocks_later_commands_on_same_channel(freeze):
    publish(hours=(8, 9))
    freeze.advance(3 * 3600)  # 10:00 — both commands are due
    r = claim(max_count=10)
    # only the channel head (08:00) is handed out, never the 09:00 follow-up
    assert r.json()["count"] == 1
    assert r.json()["delivered"][0]["at"] == iso(8)
    r2 = claim(max_count=10)
    assert r2.json()["count"] == 0  # head still unacked -> follow-up stays blocked


def test_channel_advances_only_after_confirm(freeze):
    publish(hours=(8, 9))
    freeze.advance(3600)
    t0 = first_task(claim())
    assert ack("h", t0["task_id"], t0["lease"]["token"]).status_code == 200
    freeze.advance(3600)  # 09:00
    t1 = first_task(claim())
    assert t1["sequence"] == 1 and t1["at"] == iso(9)
    assert t1["task_id"] == "h:v1:L1:000001"


def test_live_lease_is_not_reissued_but_expired_lease_reclaims_same_task(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    t = first_task(claim(lease=30))
    # within the lease the same task cannot be claimed again
    assert claim().json()["count"] == 0
    freeze.advance(31)
    t2 = first_task(claim(lease=30))
    assert t2["task_id"] == t["task_id"]          # stable identity across re-claim
    assert t2["lease"]["token"] != t["lease"]["token"]  # but a fresh token


def test_repeated_timeouts_each_issue_a_fresh_token_only_newest_confirms(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    tokens = [first_task(claim(lease=30))["lease"]["token"]]
    for _ in range(2):  # expire and re-claim twice more
        freeze.advance(31)
        tokens.append(first_task(claim(lease=30))["lease"]["token"])
    assert len(set(tokens)) == 3  # every re-claim rotates the token
    # every older token is a conflict; only the newest lease confirms
    for old in tokens[:-1]:
        assert ack("h", "h:v1:L1:000000", old).status_code == 409
    assert ack("h", "h:v1:L1:000000", tokens[-1]).status_code == 200


def test_confirm_with_current_token_succeeds_and_repeat_is_idempotent(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    t = first_task(claim())
    r1 = ack("h", t["task_id"], t["lease"]["token"])
    assert r1.status_code == 200 and r1.json()["idempotent"] is False
    r2 = ack("h", t["task_id"], t["lease"]["token"])
    assert r2.status_code == 200 and r2.json()["idempotent"] is True


def test_old_token_after_timeout_is_conflict_and_changes_nothing(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    old = first_task(claim(lease=30))
    freeze.advance(31)
    new = first_task(claim(lease=30))  # re-claim swaps the token
    r = ack("h", old["task_id"], old["lease"]["token"])
    assert r.status_code == 409
    assert r.json()["code"] == "lease_token_conflict"
    # state untouched: the new token still confirms
    assert ack("h", new["task_id"], new["lease"]["token"]).status_code == 200


def test_confirm_with_expired_but_not_reclaimed_token_is_409(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    t = first_task(claim(lease=30))
    freeze.advance(31)
    # do not re-claim; the stale token is rejected outright
    r = ack("h", t["task_id"], t["lease"]["token"])
    assert r.status_code == 409 and r.json()["code"] == "lease_expired"


def test_unknown_token_is_404(freeze):
    publish(hours=(8,))
    r = ack("h", "h:v1:L1:000000", "never-issued")
    assert r.status_code == 404 and r.json()["code"] == "lease_not_found"


def test_token_for_other_task_is_conflict_and_does_not_consume(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    t = first_task(claim())
    r = ack("h", "h:v1:L1:000099", t["lease"]["token"])
    assert r.status_code == 409 and r.json()["code"] == "ack_target_mismatch"
    # the genuine head is still leaseable; the correct task id confirms fine
    assert ack("h", t["task_id"], t["lease"]["token"]).status_code == 200


def test_channels_are_independent_and_sorted_by_execution_time(freeze):
    publish(channels=("L1", "L2"), hours=(8, 9))
    freeze.advance(3 * 3600)  # both channels have 08:00 and 09:00 due
    r = claim(max_count=10)
    delivered = r.json()["delivered"]
    # exactly one per channel (the head), ordered by (at, channel)
    assert [(d["channel"], d["at"]) for d in delivered] == [("L1", iso(8)), ("L2", iso(8))]


def test_max_count_caps_and_live_heads_block_while_other_channels_drain(freeze):
    publish(channels=("L1", "L2", "L3"), hours=(8,))
    freeze.advance(3600)
    first = claim(max_count=2).json()["delivered"]
    assert [d["channel"] for d in first] == ["L1", "L2"]
    # L1/L2 are within lease; the next claim can only pick up L3
    second = claim(max_count=10).json()["delivered"]
    assert [d["channel"] for d in second] == ["L3"]


def test_one_channel_does_not_block_another(freeze):
    publish(channels=("L1", "L2"), hours=(8, 9))
    freeze.advance(3600)
    # A long lease keeps L1's head held past the next hour.
    l1 = first_task(claim(max_count=1, lease=7200))
    assert l1["channel"] == "L1"
    freeze.advance(3600)  # 09:00
    # L1's 09:00 follow-up is blocked, but L2's 08:00 head is claimable
    got = claim(max_count=10).json()["delivered"]
    assert [(d["channel"], d["at"]) for d in got] == [("L2", iso(8))]
    ack("h", l1["task_id"], l1["lease"]["token"])
    nxt = claim(max_count=10).json()["delivered"]
    assert [(d["channel"], d["at"]) for d in nxt] == [("L1", iso(9))]


# --------------------------------------------------------------------------- #
# Version invalidation: republish / delete
# --------------------------------------------------------------------------- #
def test_republish_expires_old_unacked_and_acked_leases(freeze):
    publish(hours=(8, 9))
    freeze.advance(3600)
    t1 = first_task(claim())
    ack("h", t1["task_id"], t1["lease"]["token"])  # already confirmed
    freeze.advance(3600)  # 09:00
    t2 = first_task(claim())  # left unconfirmed

    freeze.advance(3 * 3600)  # 12:00 republish
    r = client.put("/halls/h", json=multiscene_hall("h", hours=(8, 9, 10, 13)))
    assert r.status_code == 200 and r.json()["version"] == 2

    # even the already-confirmed token now reports version expiry; state unchanged
    res1 = ack("h", t1["task_id"], t1["lease"]["token"])
    assert res1.status_code == 410 and res1.json()["code"] == "version_expired"
    res2 = ack("h", t2["task_id"], t2["lease"]["token"])
    assert res2.status_code == 410 and res2.json()["current_version"] == 2


def test_new_version_does_not_backfill_commands_before_publish_time(freeze):
    publish(hours=(8, 9, 10))
    freeze.advance(5 * 3600)  # 12:00 republish
    r = client.put("/halls/h", json=multiscene_hall("h", hours=(8, 9, 10, 13)))
    assert r.json()["version"] == 2
    freeze.advance(3600)  # 13:00
    delivered = claim(max_count=10).json()["delivered"]
    # historical 08/09/10 commands are not re-issued; only the post-watermark one
    assert [d["at"] for d in delivered] == [iso(13)]
    assert delivered[0]["task_id"] == "h:v2:L1:000000"
    assert delivered[0]["version"] == 2


def test_command_due_exactly_at_publish_time_is_delivered(freeze):
    publish(hours=(8,))
    freeze.advance(5 * 3600)  # republish at exactly 12:00
    client.put("/halls/h", json=multiscene_hall("h", hours=(12,)))
    t = first_task(claim())  # now == published_at == at
    assert t["at"] == iso(12) and t["version"] == 2


def test_initial_version_is_watermarked_at_publish_time_too(freeze):
    freeze.advance(2 * 3600 + 1800)  # publish v1 at 09:30
    client.put("/halls/h", json=multiscene_hall("h", hours=(8, 10)))
    freeze.advance(1800)  # 10:00
    delivered = claim().json()["delivered"]
    assert [d["at"] for d in delivered] == [iso(10)]  # 08:00 is pre-publish


def test_delete_expires_outstanding_leases(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    t = first_task(claim())
    assert client.delete("/halls/h").status_code == 204
    r = ack("h", t["task_id"], t["lease"]["token"])
    assert r.status_code == 410 and r.json()["code"] == "version_expired"
    assert r.json()["current_version"] is None
    assert claim().status_code == 404


def test_delete_then_recreate_issues_new_watermark_and_voids_old_tokens(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    t = first_task(claim())
    client.delete("/halls/h")
    assert ack("h", t["task_id"], t["lease"]["token"]).status_code == 410
    # recreate the same id: version restarts at 1 but the watermark is fresh,
    # so the pre-delete 08:00 command is not back-filled and old tokens stay void.
    freeze.advance(3600)  # 09:00
    client.put("/halls/h", json=multiscene_hall("h", hours=(8, 10)))
    assert client.get("/halls/h").json()["version"] == 1
    assert claim().json()["count"] == 0  # 08:00 is before the new publish time
    assert ack("h", t["task_id"], t["lease"]["token"]).status_code == 410
    freeze.advance(3600)  # 10:00
    t2 = first_task(claim())
    assert t2["task_id"] == "h:v1:L1:000000" and t2["at"] == iso(10)


def test_batch_publish_invalidates_old_version_and_no_backfill(freeze):
    publish(hours=(8, 9, 10))  # v1 at 07:00
    freeze.advance(3600)
    old = first_task(claim())  # unacked v1 lease
    freeze.advance(4 * 3600)   # 12:00 batch publish v2
    batch = {
        "halls": [
            {"hall_id": "h", "expected_version": 1,
             "orchestration": multiscene_hall("h", hours=(8, 9, 10, 13))}
        ]
    }
    r = client.post("/halls/batch-publish", json=batch)
    assert r.status_code == 200 and r.json()["published"][0]["version"] == 2

    assert ack("h", old["task_id"], old["lease"]["token"]).status_code == 410
    freeze.advance(3600)  # 13:00
    delivered = claim().json()["delivered"]
    assert [d["at"] for d in delivered] == [iso(13)]


def test_failed_single_or_batch_publish_leaves_queue_intact(freeze):
    publish(hours=(8, 9))
    freeze.advance(3600)
    t = first_task(claim(lease=300))  # live v1 lease

    # invalid single-hall publish (fade overlap) -> 422, no write
    bad = {
        "id": "h",
        "lamps": [{"id": "L1"}],
        "scenes": [
            {"id": "a", "start_at": "2026-10-06T08:00:00+00:00", "end_at": iso(20),
             "items": [{"lamp_id": "L1", "level": 50, "fade_seconds": 600}]},
            {"id": "b", "start_at": "2026-10-06T08:09:59+00:00", "end_at": iso(20),
             "items": [{"lamp_id": "L1", "level": 70, "fade_seconds": 10}]},
        ],
    }
    assert client.put("/halls/h", json=bad).status_code == 422

    # stale batch publish -> 409, no write
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [{"hall_id": "h", "expected_version": 99,
                         "orchestration": multiscene_hall("h", hours=(8, 9))}]},
    )
    assert r.status_code == 409

    # queue/version untouched: the original lease still confirms
    assert client.get("/halls/h").json()["version"] == 1
    assert ack("h", t["task_id"], t["lease"]["token"]).status_code == 200


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_claims_within_lease_hand_task_out_once(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    barrier = threading.Barrier(8)
    counts = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        body = claim().json()
        with lock:
            counts.append(body["count"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counts.count(1) == 1 and counts.count(0) == 7


def test_concurrent_confirm_and_republish_never_crosses_version(freeze):
    publish(hours=(8,))
    freeze.advance(3600)
    t = first_task(claim(lease=300))
    results = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def confirmer():
        start.wait()
        code = ack("h", t["task_id"], t["lease"]["token"]).status_code
        with lock:
            results.append(code)

    def publisher():
        start.wait()
        client.put("/halls/h", json=multiscene_hall("h", hours=(8, 9, 10)))

    threads = [threading.Thread(target=confirmer) for _ in range(7)]
    threads.append(threading.Thread(target=publisher))
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # every confirmation either won the race (200) or lost it to the republish
    # (410) — never a 5xx, and after republish the old token is universally void
    assert set(results) <= {200, 410}
    assert client.get("/halls/h").json()["version"] == 2
    assert ack("h", t["task_id"], t["lease"]["token"]).status_code == 410


# --------------------------------------------------------------------------- #
# Operational view
# --------------------------------------------------------------------------- #
def test_delivery_status_reflects_lease_lifecycle(freeze):
    publish(channels=("L1", "L2"), hours=(8, 9))
    freeze.advance(3600)
    t = first_task(claim(max_count=1, lease=30))  # only L1's head
    st = client.get("/halls/h/delivery").json()
    assert st["version"] == 1
    assert st["channels"]["L1"]["leased"]["task_id"] == t["task_id"]
    assert st["channels"]["L1"]["leased"]["state"] == "live"
    assert st["channels"]["L2"]["leased"] is None
    assert st["channels"]["L1"]["pending"] == 2

    freeze.advance(31)
    st = client.get("/halls/h/delivery").json()
    assert st["channels"]["L1"]["leased"]["state"] == "expired"

    # expired token cannot confirm; re-claim then confirm to advance the head
    t2 = first_task(claim(max_count=1, lease=30))
    assert ack("h", t2["task_id"], t2["lease"]["token"]).status_code == 200
    st = client.get("/halls/h/delivery").json()
    assert st["channels"]["L1"]["head_sequence"] == 1
    assert st["channels"]["L1"]["leased"] is None
