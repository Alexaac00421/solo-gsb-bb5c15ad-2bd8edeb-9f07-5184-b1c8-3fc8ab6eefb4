from datetime import datetime, timezone
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app, delivery
from app.store import store


client = TestClient(app)


def iso(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _clear():
    store._halls.clear()
    delivery.clear()
    yield
    store._halls.clear()
    delivery.clear()


def lamp(lid, default=0):
    return {"id": lid, "default_level": default}


def scene(sid, start, end, items, priority="normal", **kw):
    return {"id": sid, "priority": priority, "start_at": start, "end_at": end, "items": items, **kw}


def item(lid, level, fade=0, restore_fade=None):
    d = {"lamp_id": lid, "level": level, "fade_seconds": fade}
    if restore_fade is not None:
        d["restore_fade_seconds"] = restore_fade
    return d


VALID_HALL = {
    "id": "hall-1",
    "name": "Bronze Gallery",
    "lamps": [lamp("L1", default=0), lamp("L2", default=10)],
    "scenes": [
        scene("morning", iso(2026, 10, 5, 8), iso(2026, 10, 5, 12),
              [item("L1", 60, fade=300), item("L2", 40, fade=600)]),
        scene("noon", iso(2026, 10, 5, 12), iso(2026, 10, 5, 18),
              [item("L1", 90), item("L2", 90)]),
    ],
}


# --------------------------------------------------------------------------- #
def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_valid_hall_compiles_and_persists():
    r = client.put("/halls/hall-1", json=VALID_HALL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["lamps"] == 2
    assert body["summary"]["normal_scenes"] == 2
    assert len(body["timeline"]["L1"]) == 2
    # first command starts from the lamp default
    assert body["timeline"]["L1"][0]["from_level"] == 0
    assert body["timeline"]["L1"][0]["to_level"] == 60
    assert body["timeline"]["L1"][0]["fade_seconds"] == 300
    # noon command on L2 starts from morning's 40
    l2_noon = body["timeline"]["L2"][1]
    assert l2_noon["from_level"] == 40 and l2_noon["to_level"] == 90

    assert client.get("/halls/hall-1").status_code == 200
    assert client.get("/halls").json()["count"] == 1


def test_touching_fade_intervals_are_allowed():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("L1", 50, fade=600)]),
            scene("b", iso(2026, 10, 5, 8, 10), iso(2026, 10, 5, 20), [item("L1", 70, fade=0)]),
        ],
    }
    r = client.post("/validate", json=hall)
    assert r.status_code == 200, r.text


def test_overlapping_fade_intervals_rejected_with_details():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("L1", 50, fade=600)]),
            scene("b", iso(2026, 10, 5, 8, 9, 59), iso(2026, 10, 5, 20), [item("L1", 70, fade=10)]),
        ],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422
    err = r.json()["errors"][0]
    assert err["code"] == "fade_interval_overlap"
    assert err["channel"] == "L1"
    assert err["between_scenes"] == ["a", "b"]
    assert err["interval"][0] == iso(2026, 10, 5, 8, 9, 59)
    assert "overlap" in err["message"]
    # not persisted
    assert client.get("/halls/h").status_code == 404


def test_unknown_lamp_rejected():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("GHOST", 50)])],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422
    codes = {e["code"] for e in r.json()["errors"]}
    assert "unknown_lamp" in codes
    assert client.get("/halls/h").status_code == 404


def test_level_out_of_range_rejected():
    # 101 fails pydantic shape validation, 150 via JSON accepted by client but rejected
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("L1", 150)])],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422


def test_invalid_time_window_rejected():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [scene("a", iso(2026, 10, 5, 9), iso(2026, 10, 5, 8), [item("L1", 50)])],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422
    assert any(e["code"] == "invalid_time_window" for e in r.json()["errors"])


def test_emergency_scenes_may_not_overlap():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("e1", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 5)], "emergency"),
            scene("e2", iso(2026, 10, 5, 11, 15), iso(2026, 10, 5, 11, 45), [item("L1", 5)], "emergency"),
        ],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422
    assert any(e["code"] == "emergency_overlap" for e in r.json()["errors"])


# --------------------------------------------------------------------------- #
def test_emergency_preempts_and_restores_still_valid_scene():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 12), [item("L1", 80, fade=600)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30),
                  [item("L1", 10, fade=0, restore_fade=120)], "emergency"),
        ],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 200, r.text
    cmds = r.json()["timeline"]["L1"]
    types = [(c["type"], c["scene_id"]) for c in cmds]
    assert ("fade", "A") in types
    assert ("emergency_enter", "E") in types
    assert ("emergency_exit", "E") in types
    enter = next(c for c in cmds if c["type"] == "emergency_enter")
    exit_ = next(c for c in cmds if c["type"] == "emergency_exit")
    assert enter["from_level"] == 80 and enter["to_level"] == 10
    # A started before E and is valid until 12:00 -> restored to 80
    assert exit_["restored_scene_id"] == "A"
    assert exit_["to_level"] == 80
    assert exit_["fade_seconds"] == 120

    pre = r.json()["preemptions"][0]
    assert pre["emergency_scene_id"] == "E" and pre["restored_scene_id"] == "A"

    # State during emergency = emergency level; after release = restored level
    s_during = client.get("/halls/h/state", params={"at": iso(2026, 10, 5, 11, 10)}).json()
    assert s_during["channels"]["L1"]["level"] == 10
    assert s_during["active_emergency_scene_ids"] == ["E"]
    s_after = client.get("/halls/h/state", params={"at": iso(2026, 10, 5, 11, 35)}).json()
    assert s_after["channels"]["L1"]["level"] == 80
    assert s_after["active_emergency_scene_ids"] == []


def test_scene_due_during_emergency_is_skipped_and_not_resumed():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 12), [item("L1", 80)]),
            scene("B", iso(2026, 10, 5, 11, 15), iso(2026, 10, 5, 11, 45), [item("L1", 40)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 10)], "emergency"),
        ],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 200, r.text
    body = r.json()
    skipped_ids = [s["scene_id"] for s in body["skipped_activations"]]
    assert skipped_ids == ["B"]
    # B has no executable command on the channel
    assert all(c["scene_id"] != "B" for c in body["timeline"]["L1"])
    # restore goes back to A, not B
    exit_ = next(c for c in body["timeline"]["L1"] if c["type"] == "emergency_exit")
    assert exit_["restored_scene_id"] == "A"


def test_no_restore_when_no_prior_scene_remains_valid():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 11, 15), [item("L1", 80)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 10)], "emergency"),
        ],
    }
    r = client.put("/halls/h", json=hall)
    body = r.json()
    exit_ = next(c for c in body["timeline"]["L1"] if c["type"] == "emergency_exit")
    # A ended (11:15) before E released (11:30) -> cannot restore
    assert exit_["restored_scene_id"] is None
    assert exit_["to_level"] == 10
    assert "note" in exit_


def test_emergency_does_not_touch_unrelated_channels():
    hall = {
        "id": "h", "lamps": [lamp("L1"), lamp("L2")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 12),
                  [item("L1", 80), item("L2", 70)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 5)], "emergency"),
        ],
    }
    body = client.put("/halls/h", json=hall).json()
    assert all(c["priority"] == "normal" for c in body["timeline"]["L2"])
    state = client.get("/halls/h/state", params={"at": iso(2026, 10, 5, 11, 10)}).json()
    assert state["channels"]["L1"]["level"] == 5
    assert state["channels"]["L2"]["level"] == 70


def test_mid_fade_preemption_restores_interpolated_level():
    # A fades 0 -> 80 over 2 hours; E cuts in exactly 1 hour in -> held level 40
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 14), [item("L1", 80, fade=7200)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30),
                  [item("L1", 10, fade=0)], "emergency"),
        ],
    }
    body = client.put("/halls/h", json=hall).json()
    exit_ = next(c for c in body["timeline"]["L1"] if c["type"] == "emergency_exit")
    assert exit_["restored_scene_id"] == "A"
    assert exit_["to_level"] == 40


def test_scene_starting_exactly_at_release_runs_after_exit():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 12), [item("L1", 80)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 10)], "emergency"),
            scene("C", iso(2026, 10, 5, 11, 30), iso(2026, 10, 5, 12), [item("L1", 50)]),
        ],
    }
    body = client.put("/halls/h", json=hall).json()
    cmds = body["timeline"]["L1"]
    times = [(c["at"], c["type"], c["scene_id"]) for c in cmds]
    exit_idx = next(i for i, c in enumerate(times) if c[1] == "emergency_exit")
    c_idx = next(i for i, c in enumerate(times) if c[2] == "C")
    assert exit_idx < c_idx
    c_cmd = cmds[c_idx]
    assert c_cmd["from_level"] == 10 and c_cmd["to_level"] == 50
    # C is not marked skipped (release instant is outside the half-open window)
    assert all(s["scene_id"] != "C" for s in body["skipped_activations"])


def test_delete_and_404():
    client.put("/halls/hall-1", json=VALID_HALL)
    assert client.delete("/halls/hall-1").status_code == 204
    assert client.get("/halls/hall-1").status_code == 404
    assert client.delete("/halls/hall-1").status_code == 404


def test_id_mismatch_is_400():
    r = client.put("/halls/other", json=VALID_HALL)
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Batch publish: POST /halls/batch-publish
# --------------------------------------------------------------------------- #
def valid_hall_body(hid, name=None, level=50):
    return {
        "id": hid,
        "name": name or hid,
        "lamps": [lamp("L1")],
        "scenes": [
            scene("s1", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("L1", level)]),
        ],
    }


def batch_item(hid, expected, body):
    return {"hall_id": hid, "expected_version": expected, "orchestration": body}


def test_batch_publish_creates_new_halls_at_version_1():
    r = client.post(
        "/halls/batch-publish",
        json={
            "halls": [
                batch_item("h-a", None, valid_hall_body("h-a", level=40)),
                batch_item("h-b", None, valid_hall_body("h-b", level=60)),
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["count"] == 2
    pub = body["published"]
    assert [p["hall_id"] for p in pub] == ["h-a", "h-b"]  # request order preserved
    assert all(p["version"] == 1 for p in pub)
    # each returns its freshly synthesised timeline
    assert pub[0]["timeline"]["L1"][0]["to_level"] == 40
    assert pub[1]["timeline"]["L1"][0]["to_level"] == 60
    # and both are readable via the existing single-hall API
    assert client.get("/halls/h-a").json()["version"] == 1
    assert client.get("/halls/h-b").json()["version"] == 1
    assert client.get("/halls").json()["count"] == 2


def test_batch_publish_updates_existing_and_increments_once():
    client.put("/halls/h", json=valid_hall_body("h", level=10))
    client.put("/halls/h", json=valid_hall_body("h", level=20))  # now version 2
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [batch_item("h", 2, valid_hall_body("h", level=90))]},
    )
    assert r.status_code == 200, r.text
    p = r.json()["published"][0]
    assert p["version"] == 3
    assert p["timeline"]["L1"][0]["to_level"] == 90
    # created_at survives, version went up by exactly one
    got = client.get("/halls/h").json()
    assert got["version"] == 3


def test_batch_mix_create_and_update():
    client.put("/halls/old", json=valid_hall_body("old"))  # version 1
    r = client.post(
        "/halls/batch-publish",
        json={
            "halls": [
                batch_item("old", 1, valid_hall_body("old", level=70)),
                batch_item("new", None, valid_hall_body("new")),
            ]
        },
    )
    assert r.status_code == 200, r.text
    versions = {p["hall_id"]: p["version"] for p in r.json()["published"]}
    assert versions == {"old": 2, "new": 1}


def test_batch_empty_list_is_422():
    r = client.post("/halls/batch-publish", json={"halls": []})
    assert r.status_code == 422


def test_batch_missing_halls_field_is_422():
    r = client.post("/halls/batch-publish", json={})
    assert r.status_code == 422


def test_batch_duplicate_ids_rejected_and_nothing_written():
    body = valid_hall_body("dup")
    r = client.post(
        "/halls/batch-publish",
        json={
            "halls": [
                batch_item("dup", None, body),
                batch_item("other", None, valid_hall_body("other")),
                batch_item("dup", None, body),
            ]
        },
    )
    assert r.status_code == 422
    failures = r.json()["failures"]
    assert len(failures) == 2
    assert {f["hall_id"] for f in failures} == {"dup"}
    assert all(f["reason"]["code"] == "duplicate_in_batch" for f in failures)
    assert {f["index"] for f in failures} == {0, 2}
    # whole batch rejected, including the valid 'other'
    assert client.get("/halls/dup").status_code == 404
    assert client.get("/halls/other").status_code == 404


def test_batch_id_mismatch_rejected_with_reason():
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [batch_item("outer", None, valid_hall_body("inner"))]},
    )
    assert r.status_code == 422
    f = r.json()["failures"][0]
    assert f["hall_id"] == "outer" and f["index"] == 0
    assert f["reason"]["code"] == "id_mismatch"
    assert "inner" in f["reason"]["message"]
    assert client.get("/halls/outer").status_code == 404
    assert client.get("/halls/inner").status_code == 404


def test_batch_invalid_orchestration_returns_engine_errors():
    bad = {
        "id": "bad",
        "lamps": [lamp("L1")],
        "scenes": [
            scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("L1", 50, fade=600)]),
            scene("b", iso(2026, 10, 5, 8, 9, 59), iso(2026, 10, 5, 20), [item("L1", 70, fade=10)]),
        ],
    }
    r = client.post(
        "/halls/batch-publish",
        json={
            "halls": [
                batch_item("good", None, valid_hall_body("good")),
                batch_item("bad", None, bad),
            ]
        },
    )
    assert r.status_code == 422
    failures = r.json()["failures"]
    assert len(failures) == 1
    f = failures[0]
    assert f["hall_id"] == "bad" and f["index"] == 1
    assert f["reason"]["code"] == "orchestration_invalid"
    codes = {e["code"] for e in f["reason"]["errors"]}
    assert "fade_interval_overlap" in codes
    # atomic: the good hall must not have been written either
    assert client.get("/halls/good").status_code == 404
    assert client.get("/halls").json()["count"] == 0


def test_batch_shape_validation_error_is_422_and_not_written():
    # level 150 fails Pydantic field validation for the nested orchestration.
    bad = {
        "id": "bad",
        "lamps": [lamp("L1")],
        "scenes": [scene("s", iso(2026, 10, 5, 8), iso(2026, 10, 5, 9), [item("L1", 150)])],
    }
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [batch_item("bad", None, bad)]},
    )
    assert r.status_code == 422  # FastAPI request-body validation error
    assert client.get("/halls/bad").status_code == 404


def test_batch_stale_version_on_update_is_409_and_all_unchanged():
    # both halls exist at version 1
    client.put("/halls/h1", json=valid_hall_body("h1"))
    client.put("/halls/h2", json=valid_hall_body("h2"))
    # a successful batch bumps both to version 2
    ok = client.post(
        "/halls/batch-publish",
        json={
            "halls": [
                batch_item("h1", 1, valid_hall_body("h1", level=88)),
                batch_item("h2", 1, valid_hall_body("h2", level=66)),
            ]
        },
    )
    assert ok.status_code == 200, ok.text

    # now retry with stale expectations: h1 still says 1 (current 2), and h2
    # uses null although the hall already exists.
    r = client.post(
        "/halls/batch-publish",
        json={
            "halls": [
                batch_item("h1", 1, valid_hall_body("h1", level=1)),
                batch_item("h2", None, valid_hall_body("h2")),
            ]
        },
    )
    assert r.status_code == 409
    failures = {f["hall_id"]: f["reason"] for f in r.json()["failures"]}
    assert set(failures) == {"h1", "h2"}
    assert failures["h1"]["code"] == "version_mismatch"
    assert failures["h1"]["expected_version"] == 1
    assert failures["h1"]["current_version"] == 2
    assert failures["h2"]["expected_version"] is None
    assert failures["h2"]["current_version"] == 2
    # nothing changed: still version 2 with the levels from the winning batch
    assert client.get("/halls/h1").json()["version"] == 2
    assert client.get("/halls/h2").json()["version"] == 2
    assert client.get("/halls/h1").json()["timeline"]["L1"][0]["to_level"] == 88
    assert client.get("/halls/h2").json()["timeline"]["L1"][0]["to_level"] == 66


def test_batch_expected_version_for_nonexistent_hall_is_409():
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [batch_item("ghost", 1, valid_hall_body("ghost"))]},
    )
    assert r.status_code == 409
    f = r.json()["failures"][0]
    assert f["reason"]["code"] == "version_mismatch"
    assert f["reason"]["expected_version"] == 1
    assert f["reason"]["current_version"] is None
    assert client.get("/halls/ghost").status_code == 404


def test_batch_null_expected_for_existing_hall_is_409():
    client.put("/halls/h", json=valid_hall_body("h"))
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [batch_item("h", None, valid_hall_body("h"))]},
    )
    assert r.status_code == 409
    f = r.json()["failures"][0]
    assert f["reason"]["current_version"] == 1
    assert client.get("/halls/h").json()["version"] == 1


def test_batch_reports_invalid_content_and_version_conflict_together():
    # hall 'a' exists at v1; caller sends an invalid orchestration for it AND a
    # stale expected version for hall 'b' — both must come back in one response.
    client.put("/halls/a", json=valid_hall_body("a"))
    client.put("/halls/b", json=valid_hall_body("b"))
    bad_a = {
        "id": "a",
        "lamps": [lamp("L1")],
        "scenes": [
            scene("a1", iso(2026, 10, 5, 8), iso(2026, 10, 5, 9), [item("GHOST", 50)]),
        ],
    }
    r = client.post(
        "/halls/batch-publish",
        json={
            "halls": [
                batch_item("a", 1, bad_a),
                batch_item("b", 99, valid_hall_body("b")),
            ]
        },
    )
    assert r.status_code == 409  # contains a version conflict
    by = {f["hall_id"]: f["reason"]["code"] for f in r.json()["failures"]}
    assert by == {"a": "orchestration_invalid", "b": "version_mismatch"}
    assert client.get("/halls/a").json()["version"] == 1
    assert client.get("/halls/b").json()["version"] == 1


def test_batch_same_item_content_error_takes_precedence_over_version():
    # Hall exists at v1; the item is BOTH orchestration-invalid AND stale
    # (expected 99). It must surface exactly once as a content error, and the
    # overall status must be 422 (no version_mismatch anywhere).
    client.put("/halls/h", json=valid_hall_body("h"))
    bad = {
        "id": "h",
        "lamps": [lamp("L1")],
        "scenes": [
            scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("GHOST", 50)]),
        ],
    }
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [batch_item("h", 99, bad)]},
    )
    assert r.status_code == 422
    failures = r.json()["failures"]
    assert len(failures) == 1
    assert failures[0]["hall_id"] == "h"
    assert failures[0]["reason"]["code"] == "orchestration_invalid"
    assert client.get("/halls/h").json()["version"] == 1


def test_batch_concurrent_same_expected_version_only_one_wins():
    client.put("/halls/h", json=valid_hall_body("h", level=1))  # version 1
    payload = {"halls": [batch_item("h", 1, valid_hall_body("h", level=77))]}
    barrier = threading.Barrier(8)
    results: list[int] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        resp = client.post("/halls/batch-publish", json=payload)
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results).count(200) == 1
    assert sorted(results).count(409) == 7
    assert client.get("/halls/h").json()["version"] == 2


def test_batch_concurrent_create_same_null_version_only_one_wins():
    payload = {"halls": [batch_item("brand-new", None, valid_hall_body("brand-new"))]}
    barrier = threading.Barrier(6)
    results: list[int] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        resp = client.post("/halls/batch-publish", json=payload)
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(409) == 5
    got = client.get("/halls/brand-new")
    assert got.status_code == 200 and got.json()["version"] == 1


def test_batch_failure_is_atomic_against_unrelated_existing_halls():
    client.put("/halls/keep", json=valid_hall_body("keep", level=20))
    r = client.post(
        "/halls/batch-publish",
        json={
            "halls": [
                batch_item("keep", 1, valid_hall_body("keep", level=99)),
                batch_item("bad", None, {
                    "id": "bad",
                    "lamps": [lamp("L1")],
                    "scenes": [
                        scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("L1", 50, fade=600)]),
                        scene("b", iso(2026, 10, 5, 8, 9, 59), iso(2026, 10, 5, 20), [item("L1", 70, fade=10)]),
                    ],
                }),
            ]
        },
    )
    assert r.status_code == 422
    # existing hall is untouched (still v1, old level)
    got = client.get("/halls/keep").json()
    assert got["version"] == 1
    assert got["timeline"]["L1"][0]["to_level"] == 20


def test_single_hall_api_still_works_after_batch_publish():
    client.post(
        "/halls/batch-publish",
        json={"halls": [batch_item("x", None, valid_hall_body("x", level=33))]},
    )
    # GET /timeline and GET /state keep working on a batch-created hall
    tl = client.get("/halls/x/timeline").json()
    assert tl["timeline"]["L1"][0]["to_level"] == 33
    st = client.get("/halls/x/state", params={"at": iso(2026, 10, 5, 10)}).json()
    assert st["channels"]["L1"]["level"] == 33
    # single PUT still bumps versions independently
    r = client.put("/halls/x", json=valid_hall_body("x", level=44))
    assert r.status_code == 200 and r.json()["version"] == 2
    assert client.delete("/halls/x").status_code == 204
