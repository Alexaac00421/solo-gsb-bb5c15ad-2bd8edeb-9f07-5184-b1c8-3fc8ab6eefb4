"""Read-only diff/rehearsal between two already-published hall versions.

Pure functions over immutable :class:`~app.store.StoredHall` snapshots: nothing
here touches the store, the delivery queues or any lease. Commands are paired
**per channel and execution time**; unpaired commands are additions/removals and
paired commands are compared for brightness / fade / priority (and the related
command type / scene) changes.

Delivery impact is annotated from data already fixed at publish time:

* ``publish_watermark_filtered`` — a command whose ``at`` precedes that
  version's publish watermark was never put into that version's delivery queue
  (the gateway never back-fills historical commands).
* ``emergency_preemption`` / ``emergency_preemption_skipped`` — emergency
  enter/exit traffic, or a normal activation skipped because it fell inside an
  emergency window, is not delivered as an ordinary scene command.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .clock import utc_now
from .store import StoredHall

# Diff entry kinds.
ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"

# Delivery-impact codes.
WATERMARK_FILTERED = "publish_watermark_filtered"
EMERGENCY_PREEMPTION = "emergency_preemption"
EMERGENCY_SKIPPED = "emergency_preemption_skipped"


# --------------------------------------------------------------------------- #
# Version metadata / command projection
# --------------------------------------------------------------------------- #
def _iso(t: datetime) -> str:
    return t.isoformat()


def version_meta(stored: StoredHall, side: str) -> dict[str, Any]:
    return {
        "side": side,
        "hall_id": stored.spec.id,
        "name": stored.spec.name,
        "version": stored.version,
        "published_at": _iso(stored.updated_at),
        "created_at": _iso(stored.created_at),
    }


def _watermark(stored: StoredHall) -> datetime:
    # DeliveryQueue.build uses the same half-open boundary (at >= published_at).
    return stored.updated_at


def _command_view(cmd: dict, side: str, version: int, watermark: datetime) -> dict[str, Any]:
    """Project one compiled command for diffing (never mutates the plan)."""
    at = datetime.fromisoformat(cmd["at"])
    filtered = at < watermark
    return {
        "side": side,
        "version": version,
        "at": cmd["at"],
        "deliverable": not filtered,
        "watermark_filtered": filtered,
        "command": cmd,
    }


def _channel_views(stored: StoredHall, side: str) -> dict[str, list[dict[str, Any]]]:
    wm = _watermark(stored)
    return {
        channel: [_command_view(cmd, side, stored.version, wm) for cmd in commands]
        for channel, commands in stored.plan["timeline"].items()
    }


# --------------------------------------------------------------------------- #
# Delivery impacts
# --------------------------------------------------------------------------- #
def _emergency_message(view: dict[str, Any]) -> str:
    cmd = view["command"]
    if cmd["type"] == "emergency_enter":
        return (
            f"Emergency scene '{cmd['scene_id']}' cuts in at {cmd['at']} and preempts the "
            "normal per-channel delivery."
        )
    return (
        f"Emergency scene '{cmd['scene_id']}' releases the channel at {cmd['at']}; delivery "
        f"restores scene {cmd.get('restored_scene_id')!r} (or holds the emergency level)."
    )


def _command_impacts(view: dict[str, Any], watermark: datetime) -> list[dict[str, Any]]:
    impacts: list[dict[str, Any]] = []
    cmd = view["command"]
    at = datetime.fromisoformat(cmd["at"])
    if at < watermark:
        impacts.append(
            {
                "code": WATERMARK_FILTERED,
                "side": view["side"],
                "version": view["version"],
                "at": cmd["at"],
                "deliverable": False,
                "message": (
                    f"Command due at {cmd['at']} precedes version {view['version']}'s publish "
                    f"watermark {_iso(watermark)}; that version's delivery queue never contained "
                    "it (historical commands are not back-filled)."
                ),
            }
        )
    if cmd["type"] in ("emergency_enter", "emergency_exit"):
        impacts.append(
            {
                "code": EMERGENCY_PREEMPTION,
                "side": view["side"],
                "version": view["version"],
                "at": cmd["at"],
                "deliverable": not view["watermark_filtered"],
                "emergency_scene_id": cmd["scene_id"],
                "event": cmd["type"],
                "restored_scene_id": cmd.get("restored_scene_id"),
                "message": _emergency_message(view),
            }
        )
    return impacts


# --------------------------------------------------------------------------- #
# Pairing
# --------------------------------------------------------------------------- #
def _pair_channel(
    before_views: list[dict[str, Any]],
    after_views: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair one channel's commands by execution time.

    Commands sharing an instant are grouped together; within each instant the
    nth command of a kind (``emergency_enter`` / ``emergency_exit`` / normal
    ``fade``) on each side is paired first. Remaining commands at the same
    instant (e.g. a normal scene replaced by an emergency cut-in) are paired
    across kinds, which surfaces the command-type/priority change; anything
    left unpaired is an addition/removal.
    """
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    removed: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []

    instants = sorted({v["command"]["at"] for v in [*before_views, *after_views]})
    for at in instants:
        a_group = [v for v in before_views if v["command"]["at"] == at]
        b_group = [v for v in after_views if v["command"]["at"] == at]

        def kind(v: dict[str, Any]) -> str:
            t = v["command"]["type"]
            return t if t != "fade" else "normal"

        a_by_kind: dict[str, list[dict[str, Any]]] = {}
        b_by_kind: dict[str, list[dict[str, Any]]] = {}
        for v in a_group:
            a_by_kind.setdefault(kind(v), []).append(v)
        for v in b_group:
            b_by_kind.setdefault(kind(v), []).append(v)

        a_left: list[dict[str, Any]] = []
        b_left: list[dict[str, Any]] = []
        for k in sorted(set(a_by_kind) | set(b_by_kind)):
            ak, bk = a_by_kind.get(k, []), b_by_kind.get(k, [])
            for av, bv in zip(ak, bk):
                pairs.append((av, bv))
            a_left.extend(ak[len(bk):])
            b_left.extend(bk[len(ak):])
        # Cross-kind pairing should match like with like before falling back:
        # prefer an emergency-to-emergency pairing (enter vs exit at the same
        # instant is itself a meaningful change) over an emergency-to-normal one.
        def cross_rank(v: dict[str, Any]) -> int:
            return 0 if v["command"]["priority"] == "emergency" else 1

        a_left.sort(key=cross_rank)
        b_left.sort(key=cross_rank)

        # Same instant, different kind: pair cross-kind (a normal scene being
        # preempted by an emergency event, or vice-versa), then leave the rest.
        for av, bv in zip(a_left, b_left):
            pairs.append((av, bv))
        removed.extend(a_left[len(b_left):])
        added.extend(b_left[len(a_left):])

    return pairs, removed, added


# --------------------------------------------------------------------------- #
# Field comparison
# --------------------------------------------------------------------------- #
def _change(field: str, before: Any, after: Any, *, delta: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"field": field, "before": before, "after": after}
    if delta is not None:
        out["delta"] = round(float(delta), 2)
    return out


def _field_changes(before_cmd: dict, after_cmd: dict) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []

    if before_cmd["type"] != after_cmd["type"]:
        changes.append(_change("command_type", before_cmd["type"], after_cmd["type"]))
    if before_cmd["priority"] != after_cmd["priority"]:
        changes.append(_change("priority", before_cmd["priority"], after_cmd["priority"]))

    b_level, a_level = float(before_cmd["to_level"]), float(after_cmd["to_level"])
    if b_level != a_level:
        changes.append(_change("brightness", before_cmd["to_level"], after_cmd["to_level"], delta=a_level - b_level))

    b_fade, a_fade = float(before_cmd["fade_seconds"]), float(after_cmd["fade_seconds"])
    if b_fade != a_fade:
        changes.append(_change("fade_seconds", before_cmd["fade_seconds"], after_cmd["fade_seconds"], delta=a_fade - b_fade))

    if before_cmd.get("scene_id") != after_cmd.get("scene_id"):
        changes.append(_change("scene_id", before_cmd.get("scene_id"), after_cmd.get("scene_id")))
    if before_cmd.get("restored_scene_id") != after_cmd.get("restored_scene_id"):
        changes.append(
            _change("restored_scene_id", before_cmd.get("restored_scene_id"), after_cmd.get("restored_scene_id"))
        )
    return changes


def _entry_base(channel: str, at: str) -> dict[str, Any]:
    return {"channel": channel, "at": at}


# --------------------------------------------------------------------------- #
# Per-channel command diff
# --------------------------------------------------------------------------- #
def _command_entries_for_channel(
    channel: str,
    before_views: list[dict[str, Any]],
    after_views: list[dict[str, Any]],
    before: StoredHall,
    after: StoredHall,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    paired, left_a, left_b = _pair_channel(before_views, after_views)

    for bv, av in paired:
        bc, ac = bv["command"], av["command"]
        changes = _field_changes(bc, ac)
        entry = {
            **_entry_base(channel, ac["at"]),
            "kind": CHANGED,
            "before": bv,
            "after": av,
            "changes": changes,
            "delivery_impacts": [
                *_command_impacts(bv, _watermark(before)),
                *_command_impacts(av, _watermark(after)),
            ],
        }
        entries.append(entry)

    for view in left_a:
        entries.append(
            {
                **_entry_base(channel, view["command"]["at"]),
                "kind": REMOVED,
                "before": view,
                "delivery_impacts": _command_impacts(view, _watermark(before)),
            }
        )
    for view in left_b:
        entries.append(
            {
                **_entry_base(channel, view["command"]["at"]),
                "kind": ADDED,
                "after": view,
                "delivery_impacts": _command_impacts(view, _watermark(after)),
            }
        )
    return entries


# --------------------------------------------------------------------------- #
# Skipped activations (normal scenes suppressed by an emergency)
# --------------------------------------------------------------------------- #
def _skipped_index(stored: StoredHall) -> dict[tuple[str, str, str], dict]:
    return {
        (s["channel"], s["scheduled_at"], s["scene_id"]): s
        for s in stored.plan.get("skipped_activations", [])
    }


def _activation_impacts(view: str, stored: StoredHall, record: dict) -> list[dict[str, Any]]:
    impacts: list[dict[str, Any]] = []
    scheduled = datetime.fromisoformat(record["scheduled_at"])
    if scheduled < _watermark(stored):
        impacts.append(
            {
                "code": WATERMARK_FILTERED,
                "side": view,
                "version": stored.version,
                "at": record["scheduled_at"],
                "deliverable": False,
                "message": (
                    f"Skipped activation of '{record['scene_id']}' at {record['scheduled_at']} also "
                    f"precedes version {stored.version}'s publish watermark {_iso(_watermark(stored))}; "
                    "no delivery queue entry existed for it."
                ),
            }
        )
    impacts.append(
        {
            "code": EMERGENCY_SKIPPED,
            "side": view,
            "version": stored.version,
            "at": record["scheduled_at"],
            "deliverable": False,
            "emergency_scene_id": record["preempted_by"],
            "message": (
                f"Normal scene '{record['scene_id']}' on channel '{record['channel']}' is due at "
                f"{record['scheduled_at']} inside emergency scene '{record['preempted_by']}'; it is "
                "never delivered and is not resumed afterwards."
            ),
        }
    )
    return impacts


def _activation_changes(before: StoredHall, after: StoredHall) -> list[dict[str, Any]]:
    bi, ai = _skipped_index(before), _skipped_index(after)
    entries: list[dict[str, Any]] = []
    for key in sorted(bi.keys() - ai.keys()):
        rec = bi[key]
        entries.append(
            {
                "kind": REMOVED,
                "channel": rec["channel"],
                "at": rec["scheduled_at"],
                "scene_id": rec["scene_id"],
                "before": rec,
                "delivery_impacts": _activation_impacts("before", before, rec),
            }
        )
    for key in sorted(ai.keys() - bi.keys()):
        rec = ai[key]
        entries.append(
            {
                "kind": ADDED,
                "channel": rec["channel"],
                "at": rec["scheduled_at"],
                "scene_id": rec["scene_id"],
                "after": rec,
                "delivery_impacts": _activation_impacts("after", after, rec),
            }
        )
    for key in sorted(bi.keys() & ai.keys()):
        b_rec, a_rec = bi[key], ai[key]
        changes = []
        if b_rec.get("preempted_by") != a_rec.get("preempted_by"):
            changes.append(_change("preempted_by", b_rec.get("preempted_by"), a_rec.get("preempted_by")))
        if changes:
            entries.append(
                {
                    "kind": CHANGED,
                    "channel": b_rec["channel"],
                    "at": b_rec["scheduled_at"],
                    "scene_id": b_rec["scene_id"],
                    "before": b_rec,
                    "after": a_rec,
                    "changes": changes,
                    "delivery_impacts": [
                        *_activation_impacts("before", before, b_rec),
                        *_activation_impacts("after", after, a_rec),
                    ],
                }
            )
    return entries


# --------------------------------------------------------------------------- #
# Preemption window metadata changes
# --------------------------------------------------------------------------- #
def _preemption_index(stored: StoredHall) -> dict[tuple[str, str, str, str], dict]:
    return {
        (p["channel"], p["emergency_scene_id"], p["from"], p["to"]): p
        for p in stored.plan.get("preemptions", [])
    }


def _preemption_impacts(view: str, stored: StoredHall, pre: dict) -> list[dict[str, Any]]:
    impacts: list[dict[str, Any]] = []
    start = datetime.fromisoformat(pre["from"])
    if start < _watermark(stored):
        impacts.append(
            {
                "code": WATERMARK_FILTERED,
                "side": view,
                "version": stored.version,
                "at": pre["from"],
                "deliverable": False,
                "message": (
                    f"Emergency '{pre['emergency_scene_id']}' starts at {pre['from']} before version "
                    f"{stored.version}'s publish watermark {_iso(_watermark(stored))}; its enter command "
                    "was never queued for delivery."
                ),
            }
        )
    impacts.append(
        {
            "code": EMERGENCY_PREEMPTION,
            "side": view,
            "version": stored.version,
            "at": pre["from"],
            "deliverable": start >= _watermark(stored),
            "emergency_scene_id": pre["emergency_scene_id"],
            "from": pre["from"],
            "to": pre["to"],
            "skipped_scene_ids": pre.get("skipped_scene_ids", []),
            "message": (
                f"Emergency scene '{pre['emergency_scene_id']}' owns channel '{pre['channel']}' from "
                f"{pre['from']} until {pre['to']}; normal delivery is preempted"
                + (
                    f"; skipped scenes: {', '.join(pre['skipped_scene_ids'])}."
                    if pre.get("skipped_scene_ids")
                    else "."
                )
            ),
        }
    )
    return impacts


def _preemption_changes(before: StoredHall, after: StoredHall) -> list[dict[str, Any]]:
    bi, ai = _preemption_index(before), _preemption_index(after)
    entries: list[dict[str, Any]] = []

    for key in sorted(bi.keys() - ai.keys()):
        pre = bi[key]
        entries.append(
            {
                "kind": REMOVED,
                "channel": pre["channel"],
                "at": pre["from"],
                "emergency_scene_id": pre["emergency_scene_id"],
                "before": {"version": before.version, **pre},
                "delivery_impacts": _preemption_impacts("before", before, pre),
            }
        )
    for key in sorted(ai.keys() - bi.keys()):
        pre = ai[key]
        entries.append(
            {
                "kind": ADDED,
                "channel": pre["channel"],
                "at": pre["from"],
                "emergency_scene_id": pre["emergency_scene_id"],
                "after": {"version": after.version, **pre},
                "delivery_impacts": _preemption_impacts("after", after, pre),
            }
        )
    for key in sorted(bi.keys() & ai.keys()):
        b_pre, a_pre = bi[key], ai[key]
        changes: list[dict[str, Any]] = []
        if b_pre.get("restored_scene_id") != a_pre.get("restored_scene_id"):
            changes.append(
                _change("restored_scene_id", b_pre.get("restored_scene_id"), a_pre.get("restored_scene_id"))
            )
        if b_pre.get("restored_level") != a_pre.get("restored_level"):
            changes.append(
                _change("restored_level", b_pre.get("restored_level"), a_pre.get("restored_level"))
            )
        if b_pre.get("skipped_scene_ids", []) != a_pre.get("skipped_scene_ids", []):
            changes.append(
                _change("skipped_scene_ids", b_pre.get("skipped_scene_ids", []), a_pre.get("skipped_scene_ids", []))
            )
        if changes:
            entries.append(
                {
                    "kind": CHANGED,
                    "channel": b_pre["channel"],
                    "at": b_pre["from"],
                    "emergency_scene_id": b_pre["emergency_scene_id"],
                    "before": {"version": before.version, **b_pre},
                    "after": {"version": after.version, **a_pre},
                    "changes": changes,
                    "delivery_impacts": [
                        *_preemption_impacts("before", before, b_pre),
                        *_preemption_impacts("after", after, a_pre),
                    ],
                }
            )
    return entries


# --------------------------------------------------------------------------- #
# Channels / delivery stats
# --------------------------------------------------------------------------- #
def _channel_changes(before: StoredHall, after: StoredHall) -> dict[str, Any]:
    b_defaults = before.plan["lamp_defaults"]
    a_defaults = after.plan["lamp_defaults"]
    b_channels = set(b_defaults)
    a_channels = set(a_defaults)
    default_changes = []
    for ch in sorted(b_channels & a_channels):
        if float(b_defaults[ch]) != float(a_defaults[ch]):
            default_changes.append(
                {
                    "channel": ch,
                    "before": b_defaults[ch],
                    "after": a_defaults[ch],
                    "delta": round(float(a_defaults[ch]) - float(b_defaults[ch]), 2),
                }
            )
    return {
        "added": sorted(a_channels - b_channels),
        "removed": sorted(b_channels - a_channels),
        "default_level_changes": default_changes,
    }


def _delivery_stats(stored: StoredHall, views: Iterable[dict[str, Any]]) -> dict[str, Any]:
    views = list(views)
    all_commands = [v for channel_views in views for v in channel_views]
    emergency = [v for v in all_commands if v["command"]["type"] in ("emergency_enter", "emergency_exit")]
    filtered = [v for v in all_commands if v["watermark_filtered"]]
    return {
        "version": stored.version,
        "published_at": _iso(stored.updated_at),
        "commands_total": len(all_commands),
        "commands_deliverable": len(all_commands) - len(filtered),
        "commands_watermark_filtered": len(filtered),
        "emergency_commands": len(emergency),
        "preemptions": stored.plan["summary"]["preemptions"],
        "skipped_scene_activations": stored.plan["summary"]["skipped_scene_activations"],
    }


# --------------------------------------------------------------------------- #
# Top-level diff
# --------------------------------------------------------------------------- #
def diff_versions(before: StoredHall, after: StoredHall, *, generated_at: datetime | None = None) -> dict[str, Any]:
    """Rehearse going from published version ``before`` to published version ``after``.

    Read-only: callers pass immutable snapshots; no orchestration, delivery
    queue or lease state is consulted or modified.
    """
    if before.spec.id != after.spec.id:
        raise ValueError("diff_versions requires two versions of the same hall")
    if before.version == after.version:
        raise ValueError("diff_versions requires two distinct versions")

    hall_id = before.spec.id
    before_views = _channel_views(before, "before")
    after_views = _channel_views(after, "after")
    channels = sorted(set(before_views) | set(after_views))

    command_changes: list[dict[str, Any]] = []
    for channel in channels:
        command_changes.extend(
            _command_entries_for_channel(
                channel,
                before_views.get(channel, []),
                after_views.get(channel, []),
                before,
                after,
            )
        )
    command_changes.sort(key=lambda e: (e["channel"], e["at"], {"removed": 0, "changed": 1, "added": 2}[e["kind"]]))

    activation_changes = _activation_changes(before, after)
    preemption_changes = _preemption_changes(before, after)

    change_lists = [e["changes"] for e in command_changes if e["kind"] == CHANGED]
    fields = [c["field"] for changes in change_lists for c in changes]

    summary = {
        "commands_added": sum(e["kind"] == ADDED for e in command_changes),
        "commands_removed": sum(e["kind"] == REMOVED for e in command_changes),
        "commands_changed": sum(e["kind"] == CHANGED for e in command_changes),
        "brightness_changes": fields.count("brightness"),
        "fade_changes": fields.count("fade_seconds"),
        "priority_changes": fields.count("priority"),
        "command_type_changes": fields.count("command_type"),
        "scene_changes": fields.count("scene_id"),
        "restored_scene_changes": fields.count("restored_scene_id"),
        "channels_added": 0,
        "channels_removed": 0,
        "default_level_changes": 0,
        "skipped_activations_added": sum(e["kind"] == ADDED for e in activation_changes),
        "skipped_activations_removed": sum(e["kind"] == REMOVED for e in activation_changes),
        "preemptions_added": sum(e["kind"] == ADDED for e in preemption_changes),
        "preemptions_removed": sum(e["kind"] == REMOVED for e in preemption_changes),
    }

    channel_changes = _channel_changes(before, after)
    summary["channels_added"] = len(channel_changes["added"])
    summary["channels_removed"] = len(channel_changes["removed"])
    summary["default_level_changes"] = len(channel_changes["default_level_changes"])

    impacts = [
        impact
        for entry in [*command_changes, *activation_changes, *preemption_changes]
        for impact in entry.get("delivery_impacts", [])
    ]
    summary["delivery_impacts_total"] = len(impacts)
    summary["delivery_impacts_watermark_filtered"] = sum(i["code"] == WATERMARK_FILTERED for i in impacts)
    summary["delivery_impacts_emergency_preemption"] = sum(
        i["code"] in (EMERGENCY_PREEMPTION, EMERGENCY_SKIPPED) for i in impacts
    )

    return {
        "ok": True,
        "read_only": True,
        "hall_id": hall_id,
        "direction": "before_to_after",
        "generated_at": _iso(generated_at or utc_now()),
        "before": version_meta(before, "before"),
        "after": version_meta(after, "after"),
        "summary": summary,
        "channels": channel_changes,
        "delivery_preview": {
            "before": _delivery_stats(before, list(before_views.values())),
            "after": _delivery_stats(after, list(after_views.values())),
            "note": "Counts reflect each version's own publish-time watermark and emergency compilation; "
            "the rehearsal creates no queue and touches no lease.",
        },
        "command_changes": command_changes,
        "activation_changes": activation_changes,
        "preemption_changes": preemption_changes,
    }
