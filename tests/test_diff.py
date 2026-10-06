"""End-to-end tests for the published-version comparison rehearsal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock as clock_module
from app.main import app, delivery
from app.store import store


client = TestClient(app)

D = (2026, 10, 7)


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
    store.clear_history()
    delivery.clear()
    yield
    store._halls.clear()
    store.clear_history()
    delivery.clear()


@pytest.fixture
def freeze(monkeypatch):
    fc = FakeClock(datetime(D[0], D[1], D[2], 7, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(clock_module.clock, "now", fc.now)
    return fc


def lamp(lid, default=0):
    return {"id": lid, "default_level": default}


def scene(sid, start, end, items, priority="normal", **kw):
    return {"id": sid, "priority": priority, "start_at": start, "end_at": end, "items": items, **kw}


def item(lid, level, fade=0, restore_fade=None):
    d = {"lamp_id": lid, "level": level, "fade_seconds": fade}
    if restore_fade is not None:
        d["restore_fade_seconds"] = restore_fade
    return d


def v1_body():
    return {
        "id": "h",
        "lamps": [lamp("L1"), lamp("L2", default=10)],
        "scenes": [
            scene("morning", iso(8), iso(12),
                  [item("L1", 60, fade=300), item("L2", 40)]),
            scene("extra", iso(9), iso(10), [item("L1", 70)]),
            scene("noon", iso(12), iso(18),
                  [item("L1", 90, fade=120), item("L2", 90, fade=120)]),
            scene("E1", iso(10), iso(11),
                  [item("L1", 5, restore_fade=60)], "emergency"),
        ],
    }


def v2_body():
    # morning changes brightness/fade on L1 and level on L2; "extra" is removed;
    # E1 moves earlier so "extra"-like traffic is gone; a new afternoon scene,
    # a new emergency E2 and a new channel L3 are added.
    return {
        "id": "h",
        "lamps": [lamp("L1"), lamp("L2", default=20), lamp("L3")],
        "scenes": [
            scene("morning", iso(8), iso(12),
                  [item("L1", 55, fade=600), item("L2", 45)]),
            scene("noon", iso(12), iso(18),
                  [item("L1", 90, fade=120), item("L2", 90, fade=120), item("L3", 80)]),
            scene("afternoon", iso(15), iso(17), [item("L1", 75)]),
            scene("E1", iso(9, 30), iso(9, 45),
                  [item("L1", 5, restore_fade=60)], "emergency"),
            scene("E2", iso(16), iso(16, 30),
                  [item("L3", 2)], "emergency"),
        ],
    }


def publish_versions(freeze):
    r1 = client.put("/halls/h", json=v1_body())
    assert r1.status_code == 200, r1.text
    freeze.advance(5 * 3600)  # 12:00 republish watermark
    r2 = client.put("/halls/h", json=v2_body())
    assert r2.status_code == 200, r2.text
    assert r2.json()["version"] == 2
    return r1.json(), r2.json()


def diff(base=1, target=2):
    return client.post("/halls/h/versions/diff", json={"base_version": base, "target_version": target})


# --------------------------------------------------------------------------- #
def test_versions_endpoint_lists_retained_published_versions(freeze):
    publish_versions(freeze)
    r = client.get("/halls/h/versions")
    assert r.status_code == 200
    assert r.json()["versions"] == [1, 2]


def test_diff_pairs_added_removed_and_changed_by_channel_and_time(freeze):
    publish_versions(freeze)
    r = diff()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["read_only"] is True
    assert body["before"]["version"] == 1 and body["after"]["version"] == 2

    changes = body["command_changes"]

    # "extra" fade on L1 at 09:00 exists only in v1 -> removed.
    removed_extra = next(
        e for e in changes if e["kind"] == "removed" and e["channel"] == "L1" and e["at"] == iso(9)
    )
    assert removed_extra["before"]["command"]["scene_id"] == "extra"

    # "afternoon" fade on L1 at 15:00 exists only in v2 -> added (deliverable).
    added_afternoon = next(
        e for e in changes if e["kind"] == "added" and e["channel"] == "L1" and e["at"] == iso(15)
    )
    assert added_afternoon["after"]["command"]["scene_id"] == "afternoon"
    assert added_afternoon["after"]["deliverable"] is True

    # morning fade at 08:00 is paired on L1: brightness 60 -> 55, fade 300 -> 600.
    morning = next(
        e for e in changes
        if e["kind"] == "changed" and e["channel"] == "L1" and e["at"] == iso(8)
    )
    fields = {c["field"]: c for c in morning["changes"]}
    assert fields["brightness"]["before"] == 60 and fields["brightness"]["after"] == 55
    assert fields["brightness"]["delta"] == -5
    assert fields["fade_seconds"]["before"] == 300 and fields["fade_seconds"]["after"] == 600
    assert "priority" not in fields


def test_new_channel_is_reported_and_its_commands_are_added(freeze):
    publish_versions(freeze)
    body = diff().json()
    assert body["channels"]["added"] == ["L3"]
    assert body["channels"]["removed"] == []
    l3_added = [e for e in body["command_changes"] if e["channel"] == "L3" and e["kind"] == "added"]
    # noon fade, E2 enter, E2 exit on the brand-new channel
    assert len(l3_added) == 3
    assert body["summary"]["channels_added"] == 1


def test_removed_default_level_change_is_reported(freeze):
    publish_versions(freeze)
    body = diff().json()
    defaults = body["channels"]["default_level_changes"]
    assert defaults == [{"channel": "L2", "before": 10, "after": 20, "delta": 10}]


def test_emergency_preemption_impacts_are_annotated_on_changed_enter_exit(freeze):
    publish_versions(freeze)
    body = diff().json()
    # E1 moved from 10:00 (v1) to 09:30 (v2): both instants are paired per
    # channel+time only within the same emergency type, so they appear as
    # removed/added entries and each carries an emergency_preemption impact.
    old_enter = next(
        e for e in body["command_changes"]
        if e["kind"] == "removed" and e["channel"] == "L1" and e["at"] == iso(10)
        and e["before"]["command"]["type"] == "emergency_enter"
    )
    codes = {i["code"] for i in old_enter["delivery_impacts"]}
    assert "emergency_preemption" in codes

    new_enter = next(
        e for e in body["command_changes"]
        if e["kind"] == "added" and e["channel"] == "L1" and e["at"] == iso(9, 30)
    )
    assert new_enter["after"]["command"]["type"] == "emergency_enter"
    impact = next(i for i in new_enter["delivery_impacts"] if i["code"] == "emergency_preemption")
    assert impact["emergency_scene_id"] == "E1"

    # the brand-new E2 on L3 is preemptive traffic too
    e2_entries = [
        e for e in body["command_changes"]
        if e["channel"] == "L3"
        and any(i.get("emergency_scene_id") == "E2" for i in e["delivery_impacts"])
    ]
    assert len(e2_entries) == 2  # enter + exit


def test_skipped_activation_due_inside_emergency_is_reported_as_preemption(freeze):
    # In v1, scene "extra" at 09:00 is a normal command; rebuild a scenario
    # where a normal scene is due INSIDE the emergency window in v2 and skipped.
    r1 = client.put("/halls/skip", json={
        "id": "skip",
        "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(8), iso(12), [item("L1", 80)]),
            scene("B", iso(9, 30), iso(10, 30), [item("L1", 40)]),
        ],
    })
    assert r1.status_code == 200
    freeze.advance(3600)  # 08:00
    r2 = client.put("/halls/skip", json={
        "id": "skip",
        "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(8), iso(12), [item("L1", 80)]),
            scene("B", iso(9, 30), iso(10, 30), [item("L1", 40)]),
            scene("E", iso(9), iso(10), [item("L1", 5)], "emergency"),
        ],
    })
    assert r2.status_code == 200
    r = client.post("/halls/skip/versions/diff", json={"base_version": 1, "target_version": 2})
    body = r.json()
    added_skips = [e for e in body["activation_changes"] if e["kind"] == "added"]
    assert len(added_skips) == 1
    entry = added_skips[0]
    assert entry["scene_id"] == "B" and entry["after"]["preempted_by"] == "E"
    assert {i["code"] for i in entry["delivery_impacts"]} == {"emergency_preemption_skipped"}


def test_publish_watermark_marks_historical_commands_of_each_version(freeze):
    publish_versions(freeze)  # v1 watermark 07:00, v2 watermark 12:00
    body = diff().json()
    # v2 noon command is due exactly at the 12:00 watermark -> deliverable.
    noon = next(
        e for e in body["command_changes"]
        if e["kind"] == "changed" and e["channel"] == "L1" and e["at"] == iso(12)
    )
    assert noon["after"]["deliverable"] is True
    assert noon["after"]["watermark_filtered"] is False

    # every v1-only/paired v1 command here is after 07:00, so none filtered;
    # delivery_preview exposes the per-version watermark accounting.
    assert body["delivery_preview"]["before"]["published_at"] == iso(7)
    assert body["delivery_preview"]["after"]["published_at"] == iso(12)
    # v2 is published at 12:00: its morning fade (L1/L2) and the 09:30 E1
    # enter/exit are historical and never queued; the noon command is eligible.
    assert body["delivery_preview"]["after"]["commands_watermark_filtered"] == 4
    # v1's retained snapshot is judged against v1's own (07:00) watermark.
    assert body["delivery_preview"]["before"]["commands_watermark_filtered"] == 0


def test_watermark_filtered_impact_on_a_version_published_after_its_commands(freeze):
    # Publish a hall whose timeline starts in the past relative to publish time.
    body_def = {
        "id": "late",
        "lamps": [lamp("L1")],
        "scenes": [scene("s1", iso(6), iso(8), [item("L1", 40)])],  # 06:00 < 07:00 publish
    }
    assert client.put("/halls/late", json=body_def).status_code == 200
    freeze.advance(3600)  # 08:00
    assert client.put("/halls/late", json={
        "id": "late",
        "lamps": [lamp("L1")],
        "scenes": [
            scene("s1", iso(6), iso(8), [item("L1", 40)]),
            scene("s2", iso(9), iso(11), [item("L1", 70)]),
        ],
    }).status_code == 200
    body = client.post("/halls/late/versions/diff", json={"base_version": 1, "target_version": 2}).json()
    # s1 at 06:00 is paired and filtered on BOTH versions; s2 at 09:00 is a
    # deliverable addition for v2.
    s1 = next(e for e in body["command_changes"] if e["at"] == iso(6))
    assert s1["kind"] == "changed"
    by_side = {i["side"]: i for i in s1["delivery_impacts"] if i["code"] == "publish_watermark_filtered"}
    assert set(by_side) == {"before", "after"}
    s2 = next(e for e in body["command_changes"] if e["kind"] == "added" and e["at"] == iso(9))
    assert s2["after"]["deliverable"] is True
    assert body["delivery_preview"]["after"]["commands_watermark_filtered"] == 1


def test_rejects_same_version(freeze):
    publish_versions(freeze)
    r = client.post("/halls/h/versions/diff", json={"base_version": 2, "target_version": 2})
    assert r.status_code == 422
    assert r.json()["code"] == "same_version"


def test_rejects_missing_version_with_available_list(freeze):
    publish_versions(freeze)
    r = diff(1, 9)
    assert r.status_code == 404
    err = r.json()
    assert err["code"] == "version_not_found"
    assert err["requested_version"] == 9
    assert err["available_versions"] == [1, 2]
    assert err["current_version"] == 2


def test_hall_missing_is_404():
    r = client.post("/halls/ghost/versions/diff", json={"base_version": 1, "target_version": 2})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "hall_not_found"


def test_cross_reference_endpoint_rejects_different_halls(freeze):
    publish_versions(freeze)
    r = client.post("/version-diff", json={
        "before": {"hall_id": "h", "version": 1},
        "after": {"hall_id": "other", "version": 2},
    })
    assert r.status_code == 422
    assert r.json()["code"] == "hall_mismatch"


def test_cross_reference_endpoint_compares_same_hall(freeze):
    publish_versions(freeze)
    r = client.post("/version-diff", json={
        "before": {"hall_id": "h", "version": 1},
        "after": {"hall_id": "h", "version": 2},
    })
    assert r.status_code == 200, r.text
    assert r.json()["hall_id"] == "h"


def test_same_instant_normal_vs_emergency_pairs_as_priority_change(freeze):
    # v1 at 10:00 has an ordinary fade; v2 replaces that instant with an
    # emergency cut-in. Same channel+execution time => paired (not add+remove)
    # and the entry reports a command-type + priority change.
    def body(with_emergency):
        scenes = [scene("A", iso(8), iso(18), [item("L1", 80)])]
        if with_emergency:
            scenes.append(scene("E", iso(10), iso(11), [item("L1", 5)], "emergency"))
        else:
            scenes.append(scene("B", iso(10), iso(11), [item("L1", 30)]))
        return {"id": "pe", "lamps": [lamp("L1")], "scenes": scenes}

    assert client.put("/halls/pe", json=body(False)).status_code == 200
    freeze.advance(3600)
    assert client.put("/halls/pe", json=body(True)).status_code == 200
    r = client.post("/halls/pe/versions/diff", json={"base_version": 1, "target_version": 2})
    body = r.json()
    entry = next(
        e for e in body["command_changes"]
        if e["channel"] == "L1" and e["at"] == iso(10) and e["kind"] == "changed"
    )
    fields = {c["field"]: c for c in entry["changes"]}
    assert fields["command_type"]["before"] == "fade"
    assert fields["command_type"]["after"] == "emergency_enter"
    assert fields["priority"]["before"] == "normal" and fields["priority"]["after"] == "emergency"
    assert body["summary"]["priority_changes"] == 1
    codes = {i["code"] for i in entry["delivery_impacts"]}
    assert codes == {"emergency_preemption"}  # v1 fade at 08:00 publish is eligible, E side preempts


def test_diff_direction_is_before_to_after_and_reversible(freeze):
    publish_versions(freeze)
    forward = diff(1, 2).json()
    reverse = diff(2, 1).json()
    assert forward["before"]["version"] == 1 and forward["after"]["version"] == 2
    assert reverse["before"]["version"] == 2 and reverse["after"]["version"] == 1
    assert forward["summary"]["commands_added"] == reverse["summary"]["commands_removed"]
    assert forward["summary"]["commands_removed"] == reverse["summary"]["commands_added"]


def test_rehearsal_does_not_touch_current_plan_delivery_or_leases(freeze):
    publish_versions(freeze)
    freeze.advance(4 * 3600)  # 16:00 — commands are due on the current v2 queue
    claimed = client.post("/halls/h/commands/claim", json={"max_count": 10, "lease_seconds": 300}).json()
    assert claimed["count"] > 0
    token = claimed["delivered"][0]["lease"]["token"]
    task_id = claimed["delivered"][0]["task_id"]

    before_plan = client.get("/halls/h").json()
    before_delivery = client.get("/halls/h/delivery").json()

    for _ in range(3):
        r = diff(1, 2)
        assert r.status_code == 200
        r_rev = diff(2, 1)
        assert r_rev.status_code == 200

    after_plan = client.get("/halls/h").json()
    after_delivery = client.get("/halls/h/delivery").json()
    assert after_plan["version"] == before_plan["version"]
    assert after_plan["timeline"] == before_plan["timeline"]
    assert after_delivery == before_delivery

    # the live lease still confirms exactly once — rehearsals issued no tokens
    ack = client.post("/halls/h/commands/ack", json={"task_id": task_id, "lease_token": token})
    assert ack.status_code == 200 and ack.json()["idempotent"] is False


def test_deleted_hall_loses_its_version_history():
    client.put("/halls/h", json=v1_body())
    assert client.get("/halls/h/versions").json()["versions"] == [1]
    assert client.delete("/halls/h").status_code == 204
    assert client.get("/halls/h/versions").status_code == 404
    r = client.post("/halls/h/versions/diff", json={"base_version": 1, "target_version": 2})
    assert r.status_code == 404
