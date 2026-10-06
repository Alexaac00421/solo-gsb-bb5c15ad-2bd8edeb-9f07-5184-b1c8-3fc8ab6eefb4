"""End-to-end tests for the published-version comparison rehearsal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock as clock_module
from app.main import app, delivery
from app.store import store


client = TestClient(app)

D = (2026, 10, 6)


def iso(h, mi=0, s=0):
    return datetime(D[0], D[1], D[2], h, mi, s, tzinfo=timezone.utc).isoformat()


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def set(self, h, mi=0, s=0):
        self.t = datetime(D[0], D[1], D[2], h, mi, s, tzinfo=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _isolate():
    store._halls.clear()
    store._versions.clear()
    delivery.clear()
    yield
    store._halls.clear()
    store._versions.clear()
    delivery.clear()


@pytest.fixture
def freeze(monkeypatch):
    fc = FakeClock(datetime(D[0], D[1], D[2], 7, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(clock_module.clock, "now", fc.now)
    return fc


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def lamp(lid, default=0):
    return {"id": lid, "default_level": default}


def item(lid, level, fade=0, restore_fade=None):
    d = {"lamp_id": lid, "level": level, "fade_seconds": fade}
    if restore_fade is not None:
        d["restore_fade_seconds"] = restore_fade
    return d


def scene(sid, start, end, items, priority="normal"):
    return {"id": sid, "priority": priority, "start_at": start, "end_at": end, "items": items}


def hall(hid="h", lamps=("L1",), scenes=None):
    return {"id": hid, "lamps": [lamp(c) for c in lamps], "scenes": scenes or []}


def publish(body):
    r = client.put(f"/halls/{body['id']}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def diff(base_v, target_v, hid="h"):
    return client.post(
        "/halls/version-diff",
        json={"base": {"hall_id": hid, "version": base_v},
              "target": {"hall_id": hid, "version": target_v}},
    )


def find_entry(ch_body, at, kind=None):
    for e in ch_body["entries"]:
        if e["at"] == at and (kind is None or e["kind"] == kind):
            return e
    raise AssertionError(f"no {kind} entry at {at}")


def channel(body, cid):
    return next(c for c in body["channels"] if c["channel"] == cid)


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #
def test_version_not_found_is_404(freeze):
    publish(hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 50)])]))
    r = diff(1, 9)
    assert r.status_code == 404
    errs = r.json()["errors"]
    assert errs[0]["code"] == "version_not_found"
    assert errs[0]["which"] == "target" and errs[0]["version"] == 9


def test_both_versions_missing_reported_at_once(freeze):
    r = diff(3, 4)
    assert r.status_code == 404
    which = {e["which"] for e in r.json()["errors"]}
    assert which == {"base", "target"}


def test_hall_mismatch_rejected_even_when_version_absent(freeze):
    # Hall mismatch is a shape error of the request and wins over existence.
    r = client.post(
        "/halls/version-diff",
        json={"base": {"hall_id": "h", "version": 1},
              "target": {"hall_id": "other", "version": 2}},
    )
    assert r.status_code == 422
    err = r.json()["errors"][0]
    assert err["code"] == "hall_mismatch"
    assert err["base_hall_id"] == "h" and err["target_hall_id"] == "other"


def test_same_version_rejected(freeze):
    publish(hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 50)])]))
    r = diff(1, 1)
    assert r.status_code == 422
    assert r.json()["errors"][0]["code"] == "same_version"


def test_same_version_rejected_even_across_hall_names(freeze):
    # Same number, different hall: hall mismatch is checked first.
    r = client.post(
        "/halls/version-diff",
        json={"base": {"hall_id": "h", "version": 1},
              "target": {"hall_id": "g", "version": 1}},
    )
    assert r.status_code == 422
    assert r.json()["errors"][0]["code"] == "hall_mismatch"


def test_diff_on_unknown_hall_is_404(freeze):
    publish(hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 50)])]))
    r = client.post(
        "/halls/version-diff",
        json={"base": {"hall_id": "ghost", "version": 1},
              "target": {"hall_id": "ghost", "version": 2}},
    )
    assert r.status_code == 404
    assert {e["which"] for e in r.json()["errors"]} == {"base", "target"}


def test_invalid_version_number_is_422(freeze):
    r = client.post(
        "/halls/version-diff",
        json={"base": {"hall_id": "h", "version": 0},
              "target": {"hall_id": "h", "version": 1}},
    )
    assert r.status_code == 422  # pydantic field validation


# --------------------------------------------------------------------------- #
# Pairing: added / removed / changed / unchanged
# --------------------------------------------------------------------------- #
def test_added_removed_and_unchanged_pairing(freeze):
    publish(hall(scenes=[
        scene("a", iso(8), iso(12), [item("L1", 40)]),
        scene("b", iso(12), iso(18), [item("L1", 70)]),
    ]))
    freeze.set(7, 30)
    publish(hall(scenes=[
        scene("a", iso(8), iso(12), [item("L1", 40)]),      # unchanged
        scene("c", iso(14), iso(18), [item("L1", 80)]),      # new at 14:00
        # b at 12:00 is removed
    ]))
    r = diff(1, 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    s = body["summary"]
    assert s["added"] == 1 and s["removed"] == 1 and s["changed"] == 0 and s["unchanged"] == 1

    l1 = channel(body, "L1")
    kinds = {e["at"][11:16]: e["kind"] for e in l1["entries"]}
    assert kinds == {"08:00": "unchanged", "12:00": "removed", "14:00": "added"}
    added = find_entry(l1, iso(14), "added")
    assert added["target"]["scene_id"] == "c" and added["base"] is None
    removed = find_entry(l1, iso(12), "removed")
    assert removed["base"]["scene_id"] == "b" and removed["target"] is None


def test_level_fade_and_priority_changes_are_itemised(freeze):
    publish(hall(lamps=("L1", "L2"), scenes=[
        scene("a", iso(8), iso(12), [item("L1", 40, fade=60), item("L2", 30)]),
    ]))
    freeze.set(7, 30)
    publish(hall(lamps=("L1", "L2"), scenes=[
        scene("a", iso(8), iso(12), [item("L1", 55, fade=120), item("L2", 30)]),
        scene("em", iso(9), iso(10), [item("L2", 5)], priority="emergency"),
    ]))
    body = diff(1, 2).json()
    l1 = channel(body, "L1")
    changed = find_entry(l1, iso(8), "changed")
    fields = {c["field"]: (c["from"], c["to"]) for c in changed["changes"]}
    assert fields["to_level"] == (40.0, 55.0)
    assert fields["fade_seconds"] == (60.0, 120.0)

    # Emergency enter/exit on L2 are additions flagged as emergency commands;
    # the preemptions are also summarised at top level.
    l2 = channel(body, "L2")
    enter = find_entry(l2, iso(9), "added")
    assert enter["target"]["type"] == "emergency_enter"
    assert enter["preemption"]["target"]["emergency_command"] is True
    assert enter["delivery_impact"]["preemption_involved"] is True
    assert body["summary"]["preemption_windows"]["added"] == 1
    assert body["preemption_changes"]["added"][0]["emergency_scene_id"] == "em"


def test_pairing_by_scene_id_then_order_at_same_instant(freeze):
    # Two scenes starting at the same instant on different channels; the diff
    # edits scene b's level, so each instant/channel slot pairs by scene id.
    publish(hall(lamps=("L1", "L2"), scenes=[
        scene("a", iso(8), iso(12), [item("L1", 10)]),
        scene("b", iso(8), iso(12), [item("L2", 20)]),
    ]))
    freeze.set(7, 30)
    publish(hall(lamps=("L1", "L2"), scenes=[
        scene("a", iso(8), iso(12), [item("L1", 10)]),
        scene("b", iso(8), iso(12), [item("L2", 25)]),
    ]))
    body = diff(1, 2).json()
    l2 = channel(body, "L2")
    e = find_entry(l2, iso(8))
    assert e["kind"] == "changed"
    assert {c["field"] for c in e["changes"]} == {"to_level"}


def test_type_change_at_same_instant_is_one_paired_change(freeze):
    # A normal fade and an emergency enter sharing the same instant pair up
    # (positionally, after exact matches) as a single changed slot.
    publish(hall(scenes=[
        scene("a", iso(9), iso(12), [item("L1", 50)]),
    ]))
    freeze.set(7, 30)
    publish(hall(scenes=[
        scene("em", iso(9), iso(11), [item("L1", 5)], priority="emergency"),
    ]))
    body = diff(1, 2).json()
    entries = channel(body, "L1")["entries"]
    assert len(entries) == 2  # one paired change at 9 (fade -> enter), exit added at 11
    at9, at11 = entries
    assert at9["at"] == iso(9) and at9["kind"] == "changed"
    assert {c["field"] for c in at9["changes"]} >= {"type", "priority", "scene_id"}
    assert at9["base"]["type"] == "fade" and at9["target"]["type"] == "emergency_enter"
    assert at11["kind"] == "added" and at11["target"]["type"] == "emergency_exit"


def test_added_and_removed_channels(freeze):
    publish(hall(lamps=("L1",), scenes=[scene("a", iso(8), iso(12), [item("L1", 40)])]))
    freeze.set(7, 30)
    publish(hall(lamps=("L1", "L2"), scenes=[
        scene("a", iso(8), iso(12), [item("L1", 40), item("L2", 60)]),
    ]))
    body = diff(1, 2).json()
    assert body["summary"]["channels_added"] == ["L2"]
    assert body["summary"]["channels_removed"] == []


# --------------------------------------------------------------------------- #
# Emergency preemption delivery impact
# --------------------------------------------------------------------------- #
def test_newly_suppressed_normal_activation_is_reported(freeze):
    # v1: normal scene b starts at 10:00 and is delivered.
    publish(hall(scenes=[
        scene("a", iso(8), iso(18), [item("L1", 40)]),
        scene("b", iso(10), iso(12), [item("L1", 80)]),
    ]))
    freeze.set(7, 30)
    # v2: an emergency window covers 10:00 -> b is skipped on L1.
    publish(hall(scenes=[
        scene("a", iso(8), iso(18), [item("L1", 40)]),
        scene("b", iso(10), iso(12), [item("L1", 80)]),
        scene("em", iso(9, 30), iso(10, 30), [item("L1", 5)], priority="emergency"),
    ]))
    body = diff(1, 2).json()
    l1 = channel(body, "L1")
    kinds = {s["kind"] for s in l1["suppression_changes"]}
    assert "suppressed" in kinds
    sup = next(s for s in l1["suppression_changes"] if s["kind"] == "suppressed")
    assert sup["target"]["scene_id"] == "b" and sup["target"]["preempted_by"] == "em"
    assert body["summary"]["suppression_changes"] == 1


def test_restore_target_change_is_reported_as_preemption_change(freeze):
    publish(hall(scenes=[
        scene("a", iso(8), iso(12), [item("L1", 40)]),
        scene("em", iso(9), iso(10), [item("L1", 5, restore_fade=60)], priority="emergency"),
    ]))
    freeze.set(7, 30)
    publish(hall(scenes=[
        scene("a", iso(8), iso(12), [item("L1", 70)]),
        scene("em", iso(9), iso(10), [item("L1", 5, restore_fade=180)], priority="emergency"),
    ]))
    body = diff(1, 2).json()
    changed = body["preemption_changes"]["changed"]
    assert len(changed) == 1
    assert "restored_level" in changed[0]["fields"]
    assert changed[0]["fields"]["restored_level"] == {"from": 40.0, "to": 70.0}


# --------------------------------------------------------------------------- #
# Publish-watermark delivery impact
# --------------------------------------------------------------------------- #
def test_pre_watermark_commands_marked_not_delivered(freeze):
    # v1 published at 07:00; v2 published at 09:00 -> the 08:00 keyframe of
    # every v2 queue is behind the watermark and is never back-filled.
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 40)])]))
    freeze.set(9)
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 40)])]))
    body = diff(1, 2).json()
    e = find_entry(channel(body, "L1"), iso(8))
    td = e["delivery"]["target"]
    assert td["eligible"] is False
    assert td["before_watermark"] is True
    assert td["state"] == "filtered_by_watermark"
    assert "target_command_before_publish_watermark" in e["delivery_impact"]["reasons"]
    assert body["delivery_impact"]["target"]["watermark_filtered_commands"] == 1


def test_outstanding_base_task_marked_void(freeze):
    # v1 published 07:00; the gateway leases the 08:00 command but never acks.
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 40, fade=10)])]))
    freeze.set(8)
    claimed = client.post(
        "/halls/h/commands/claim", json={"max_count": 10, "lease_seconds": 30}
    ).json()["delivered"]
    assert len(claimed) == 1
    freeze.set(8, 30)  # lease now expired; task remains unacknowledged
    # v2 edits that command.
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 60, fade=10)])]))
    body = diff(1, 2).json()
    e = find_entry(channel(body, "L1"), iso(8), "changed")
    bd = e["delivery"]["base"]
    # v1 is superseded: its queue is gone; the outstanding task is void.
    assert bd["state"] == "version_superseded"
    assert e["delivery_impact"]["effect"] == "base_task_already_void"
    assert body["delivery_impact"]["base"]["is_current_version"] is False


def test_current_version_acked_task_is_reflected(freeze):
    # Compare two historical versions after v3 became current, with an acked
    # task in the *currently served* target side when target is current.
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 40)])]))  # v1
    freeze.set(7, 30)
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 60)])]))  # v2 current
    freeze.set(8)
    t = client.post("/halls/h/commands/claim", json={"max_count": 1, "lease_seconds": 60}).json()["delivered"][0]
    client.post("/halls/h/commands/ack", json={"task_id": t["task_id"], "lease_token": t["lease"]["token"]})

    body = diff(1, 2).json()
    e = find_entry(channel(body, "L1"), iso(8), "changed")
    assert e["delivery"]["target"]["state"] == "acked"
    assert e["delivery"]["target"]["task_id"] == t["task_id"]
    assert body["delivery_impact"]["target"]["states"]["acked"] == 1


# --------------------------------------------------------------------------- #
# Read-only guarantees
# --------------------------------------------------------------------------- #
def test_rehearsal_does_not_mutate_store_queue_or_leases(freeze):
    publish(hall(scenes=[
        scene("a", iso(8), iso(12), [item("L1", 40)]),
        scene("b", iso(12), iso(18), [item("L1", 70)]),
    ]))
    freeze.set(7, 30)
    publish(hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 50)])]))  # v2 current

    before_plan = client.get("/halls/h").json()
    before_delivery = client.get("/halls/h/delivery").json()

    for _ in range(3):
        r = diff(1, 2)
        assert r.status_code == 200

    assert client.get("/halls/h").json() == before_plan
    assert client.get("/halls/h/delivery").json() == before_delivery
    assert client.get("/halls/h").json()["version"] == 2
    # Rejected calls mutate nothing either.
    for payload in (
        {"base": {"hall_id": "h", "version": 1}, "target": {"hall_id": "h", "version": 1}},
        {"base": {"hall_id": "h", "version": 1}, "target": {"hall_id": "h", "version": 9}},
        {"base": {"hall_id": "h", "version": 1}, "target": {"hall_id": "x", "version": 2}},
    ):
        client.post("/halls/version-diff", json=payload)
    assert client.get("/halls/h").json() == before_plan
    assert client.get("/halls/h/delivery").json() == before_delivery


def test_diff_does_not_create_a_queue_for_superseded_version(freeze):
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 40)])]))
    freeze.set(7, 30)
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 41)])]))
    diff(1, 2)
    # Only the hall's current (v2) queue exists; v1 was not materialised.
    assert set(delivery._queues["h"].channels) == {"L1"}
    assert delivery._queues["h"].version == 2
    assert delivery.version_view(store.get_version("h", 1)) is None


def test_deleted_hall_loses_its_history(freeze):
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 40)])]))
    freeze.set(7, 30)
    publish(hall(scenes=[scene("a", iso(8), iso(18), [item("L1", 50)])]))
    assert client.delete("/halls/h").status_code == 204
    r = diff(1, 2)
    assert r.status_code == 404
    assert store.get_version("h", 1) is None
    assert client.get("/halls/h/versions").status_code == 404


# --------------------------------------------------------------------------- #
# Versions listing and batch-publish history
# --------------------------------------------------------------------------- #
def test_versions_endpoint_lists_history(freeze):
    publish(hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 40)])]))
    freeze.set(7, 30)
    publish(hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 50)])]))
    r = client.get("/halls/h/versions")
    assert r.status_code == 200
    body = r.json()
    assert body["current_version"] == 2
    assert [v["version"] for v in body["versions"]] == [1, 2]
    assert body["versions"][0]["current"] is False and body["versions"][1]["current"] is True


def test_batch_publish_versions_are_comparable(freeze):
    body_v1 = hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 40)])])
    publish(body_v1)
    freeze.set(7, 30)
    v2 = hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 65)])])
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [{"hall_id": "h", "expected_version": 1, "orchestration": v2}]},
    )
    assert r.status_code == 200, r.text
    body = diff(1, 2).json()
    changed = find_entry(channel(body, "L1"), iso(8), "changed")
    assert {c["field"] for c in changed["changes"]} == {"to_level"}


def test_reverse_direction_swaps_base_and_target(freeze):
    publish(hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 40)])]))
    freeze.set(7, 30)
    publish(hall(scenes=[scene("a", iso(8), iso(12), [item("L1", 60)])]))
    fwd = diff(1, 2).json()
    rev = diff(2, 1).json()
    assert fwd["base"]["version"] == 1 and rev["base"]["version"] == 2
    fwd_entry = channel(fwd, "L1")["entries"][0]
    rev_entry = channel(rev, "L1")["entries"][0]
    assert fwd_entry["changes"][0]["from"] == rev_entry["changes"][0]["to"]
