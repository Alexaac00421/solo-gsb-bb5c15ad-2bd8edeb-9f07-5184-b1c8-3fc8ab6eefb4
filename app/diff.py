"""Published-version comparison rehearsal (pure functions).

Given two *already compiled* plans of two published versions of the same hall,
produce an operations-facing diff that:

* pairs commands **per channel by execution time** (commands at the same
  instant are paired by ``(type, scene_id)`` first, then in timeline order);
* reports each pair as ``added`` / ``removed`` / ``changed`` / ``unchanged``,
  where a change is broken down into ``level``, ``fade``, ``priority``,
  ``restore`` and command-``type`` deltas;
* annotates the **delivery impact** of every entry relative to each side's
  *publish watermark* (commands before a version's watermark are never
  delivered) and to **emergency preemption** (normal activations suppressed
  while an emergency owns the channel, and release/restore behaviour).

Nothing here mutates the store or the delivery layer. The two compiled plans
are augmented with two small read-only snapshots:

* ``view``  — a live delivery snapshot for that version (``None`` when the
  version has been superseded/deleted), see
  :meth:`app.delivery.DeliveryManager.version_view`;
* ``now``   — the current server time, used to interpret due/undue tasks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

# Floats are stored at 2-decimal percent precision (see engine._q2); treat
# anything below this as identical so 59.999999 does not read as a change.
_EPS = 1e-6

# Command payload fields compared verbatim (floats use the epsilon above).
_FIELDS = ("to_level", "fade_seconds", "priority", "type", "restored_scene_id", "taken_over_by", "scene_id")


@dataclass(frozen=True)
class Side:
    """One side of the comparison."""

    hall_id: str
    version: int
    plan: dict
    published_at: datetime
    # Read-only delivery snapshot (None => version is no longer served).
    view: dict | None
    now: datetime
    created_at: datetime | None = None
    updated_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= _EPS
        except (TypeError, ValueError):
            return False
    return a == b


def _pair_instant(items_a: list[dict], items_b: list[dict]) -> list[tuple[dict | None, dict | None]]:
    """Pair the commands that share one execution instant.

    Phase 1 prefers identical ``(type, scene_id)`` pairs (a fade that stays a
    fade for the same scene is the most useful match for operators). Phase 2
    pairs whatever remains positionally, so a normal fade that becomes an
    emergency enter at the same instant reads as one *changed* slot (with
    ``type``/``priority``/``scene_id`` deltas) rather than a remove plus an add.
    True leftovers are unpaired removals/additions.
    """
    pairs: list[tuple[dict | None, dict | None]] = []
    rest_a: list[dict | None] = list(items_a)
    rest_b: list[dict | None] = list(items_b)
    used_a: set[int] = set()
    used_b: set[int] = set()
    for i, a in enumerate(items_a):
        j = next(
            (
                j
                for j, b in enumerate(items_b)
                if j not in used_b and b["type"] == a["type"] and b.get("scene_id") == a.get("scene_id")
            ),
            None,
        )
        if j is not None:
            used_a.add(i)
            used_b.add(j)
            pairs.append((a, items_b[j]))
    leftover_a = [a for i, a in enumerate(rest_a) if i not in used_a]
    leftover_b = [b for j, b in enumerate(rest_b) if j not in used_b]
    n = min(len(leftover_a), len(leftover_b))
    for k in range(n):
        pairs.append((leftover_a[k], leftover_b[k]))
    for a in leftover_a[n:]:
        pairs.append((a, None))
    for b in leftover_b[n:]:
        pairs.append((None, b))
    return pairs


def _pair_times(cmds_a: list[dict], cmds_b: list[dict]) -> list[tuple[str, list[tuple[dict | None, dict | None]]]]:
    """Group both sides by execution instant and pair within each instant."""
    by_a: dict[str, list[dict]] = {}
    by_b: dict[str, list[dict]] = {}
    for c in cmds_a:
        by_a.setdefault(c["at"], []).append(c)
    for c in cmds_b:
        by_b.setdefault(c["at"], []).append(c)
    out = []
    for at in sorted(set(by_a) | set(by_b)):
        out.append((at, _pair_instant(by_a.get(at, []), by_b.get(at, []))))
    return out


def _field_changes(a: dict, b: dict) -> list[dict]:
    changes: list[dict] = []
    for field in _FIELDS:
        va = a.get(field)
        vb = b.get(field)
        if not _same(va, vb):
            changes.append({"field": field, "from": va, "to": vb})

    # from_level is derived from the merged envelope: an upstream keyframe
    # moving is usually the real cause, but report it explicitly anyway.
    if not _same(a.get("from_level"), b.get("from_level")):
        changes.append({"field": "from_level", "from": a.get("from_level"), "to": b.get("from_level")})

    # Semantic fade deltas are easier to read than one raw-seconds field.
    if not _same(a.get("fade_end_at"), b.get("fade_end_at")):
        changes.append({"field": "fade_end_at", "from": a.get("fade_end_at"), "to": b.get("fade_end_at")})
    return changes


def _skip_index(plan: dict) -> dict[tuple[str, str], dict]:
    """Skipped activations keyed by (channel, scheduled_at)."""
    return {(s["channel"], s["scheduled_at"]): s for s in plan.get("skipped_activations", [])}


def _pre_index(plan: dict) -> dict[tuple[str, str, str], dict]:
    """Preemptions keyed by (channel, emergency scene id, window-from)."""
    return {(p["channel"], p["emergency_scene_id"], p["from"]): p for p in plan.get("preemptions", [])}


# --------------------------------------------------------------------------- #
# Delivery annotation
# --------------------------------------------------------------------------- #
def _view_index(view: dict | None) -> dict[tuple[str, int], dict]:
    if view is None:
        return {}
    idx: dict[tuple[str, int], dict] = {}
    for channel, entries in (view or {}).get("channels", {}).items():
        for e in entries:
            idx[(channel, e["seq"])] = e
    return idx


def _eligible(side: Side, channel: str, cmd: dict) -> tuple[bool, bool, int, dict | None]:
    """Return ``(eligible, due_now, sequence, queue_entry)`` for a command.

    A command belongs to a version's delivery queue iff its execution instant
    is at/after the publish watermark; ``sequence`` is its position within that
    post-watermark queue (matching the gateway ``task_id`` scheme).
    """
    timeline = side.plan["timeline"].get(channel, [])
    seq = -1
    eligible = False
    post = 0
    for c in timeline:
        if _parse(c["at"]) >= side.published_at:
            if c is cmd:  # identity: diff entries reference these exact dicts
                seq = post
                eligible = True
                break
            post += 1
    due = eligible and _parse(cmd["at"]) <= side.now
    entry = _view_index(side.view).get((channel, seq)) if eligible else None
    return eligible, due, seq, entry


def _delivery(side: Side | None, channel: str, cmd: dict) -> dict:
    """Watermark + live-queue delivery annotation for one command of one side."""
    if side is None:
        return {"eligible": False, "deliverable": False, "before_watermark": False}
    eligible, due, seq, entry = _eligible(side, channel, cmd)
    info: dict[str, Any] = {
        "version": side.version,
        "published_at": side.published_at.isoformat(),
        "eligible": eligible,
        "before_watermark": not eligible,
        "deliverable": False,
        "due": due,
        "sequence": seq if eligible else None,
        "task_id": None,
        "state": None,
    }
    if not eligible:
        # Historical command relative to this version's own watermark: the
        # publish rules never back-fill it.
        info["state"] = "filtered_by_watermark"
        info["impact"] = "at_before_published_at"
        return info
    if side.view is None:
        # Version exists in history but is no longer the served version.
        info["state"] = "version_superseded"
        info["deliverable"] = due
        return info
    if entry is None:
        # Current version but not visible in the live queue snapshot.
        info["state"] = "not_in_queue"
        info["deliverable"] = due
        return info
    info["task_id"] = entry["task_id"]
    info["state"] = entry["state"]
    # acked already reached the gateway; leased/waiting entries are the ones a
    # republish (or an edited timeline) would actually disrupt.
    info["deliverable"] = due and entry["state"] != "acked"
    return info


def _emergency_flags(plan: dict, channel: str, cmd: dict, skips: dict) -> dict:
    """Preemption-related markers for one command."""
    flags: dict[str, Any] = {
        "emergency_command": cmd["priority"] == "emergency",
        "suppressed_by_emergency": (channel, cmd["at"]) in skips,
        "preempts_normal": False,
        "release": False,
        "preemption": None,
    }
    if cmd["type"] == "emergency_enter":
        p = next(
            (
                p
                for p in plan.get("preemptions", [])
                if p["channel"] == channel
                and p["emergency_scene_id"] == cmd.get("scene_id")
                and p["from"] == cmd["at"]
            ),
            None,
        )
        flags["preempts_normal"] = True
        flags["preemption"] = p
    elif cmd["type"] == "emergency_exit":
        p = next(
            (
                p
                for p in plan.get("preemptions", [])
                if p["channel"] == channel
                and p["emergency_scene_id"] == cmd.get("scene_id")
                and p["to"] == cmd["at"]
            ),
            None,
        )
        flags["release"] = True
        flags["preemption"] = p
    return flags


# --------------------------------------------------------------------------- #
# Entry construction
# --------------------------------------------------------------------------- #
def _entry(kind: str, at: str, base: dict | None, target: dict | None, changes: list[dict]) -> dict:
    return {"kind": kind, "at": at, "base": base, "target": target, "changes": changes}


def _annotate(entry: dict, base_side: Side, target_side: Side,
              base_skips: dict, target_skips: dict) -> None:
    """Attach preemption markers and delivery info to one diff entry."""
    channel = entry["channel"]
    b, t = entry["base"], entry["target"]

    base_em = _emergency_flags(base_side.plan, channel, b, base_skips) if b else None
    target_em = _emergency_flags(target_side.plan, channel, t, target_skips) if t else None
    entry["preemption"] = {
        "base": base_em,
        "target": target_em,
    }

    base_delivery = _delivery(base_side if b else None, channel, b) if b else None
    target_delivery = _delivery(target_side if t else None, channel, t) if t else None
    entry["delivery"] = {"base": base_delivery, "target": target_delivery}

    # Cross-references: a command at this slot that is skipped (emergency-
    # suppressed) on exactly one side explains the visible add/remove.
    base_suppressed = bool(base_em and base_em["suppressed_by_emergency"])
    target_suppressed = bool(target_em and target_em["suppressed_by_emergency"])
    entry["delivery_impact"] = _impact(entry, base_delivery, target_delivery,
                                       base_suppressed, target_suppressed)


def _impact(entry: dict, bd: dict | None, td: dict | None,
            base_suppressed: bool, target_suppressed: bool) -> dict:
    """Human/machine readable delivery-impact summary for one entry."""
    reasons: list[str] = []
    outstanding_base = bool(bd and bd.get("deliverable") and bd.get("state") in ("leased_live", "leased_expired", "waiting"))

    if bd and bd.get("before_watermark"):
        reasons.append("base_command_before_publish_watermark")
    if td and td.get("before_watermark"):
        reasons.append("target_command_before_publish_watermark")
    if base_suppressed:
        reasons.append("base_suppressed_by_emergency_preemption")
    if target_suppressed:
        reasons.append("target_suppressed_by_emergency_preemption")
    if bd and bd.get("state") == "version_superseded":
        reasons.append("base_version_already_superseded")
    if td and td.get("state") == "version_superseded":
        reasons.append("target_version_already_superseded")

    kind = entry["kind"]
    if kind == "unchanged":
        effect = "no_change"
    elif kind == "added":
        if td and not td.get("eligible"):
            effect = "not_delivered_watermark_filtered"
        elif td and td.get("state") == "version_superseded":
            effect = "would_be_delivered_if_served"
        else:
            effect = "new_delivery"
    elif kind == "removed":
        if not bd or not bd.get("eligible"):
            effect = "no_delivery_change_watermark_filtered"
        elif bd.get("state") == "acked":
            effect = "already_delivered_ack_remains"
        elif outstanding_base:
            effect = "outstanding_base_task_void_on_republish"
        elif bd.get("state") == "version_superseded":
            effect = "base_task_already_void"
        else:
            effect = "delivery_removed"
    else:  # changed
        if outstanding_base:
            effect = "outstanding_base_task_replaced_on_republish"
        elif bd and bd.get("state") == "version_superseded":
            effect = "base_task_already_void"
        elif bd and bd.get("state") == "acked" and td and td.get("eligible"):
            effect = "already_delivered_change_only_affects_future"
        else:
            effect = "future_command_changes"

    pre = entry.get("preemption") or {}
    base_pre = pre.get("base") or {}
    target_pre = pre.get("target") or {}
    return {
        "effect": effect,
        "reasons": reasons,
        "outstanding_base_task": outstanding_base,
        "preemption_involved": base_suppressed or target_suppressed
        or bool(base_pre.get("emergency_command"))
        or bool(target_pre.get("emergency_command")),
        "watermark_filtered": bool(
            (bd and bd.get("before_watermark")) or (td and td.get("before_watermark"))
        ),
    }


# --------------------------------------------------------------------------- #
# Channel diff
# --------------------------------------------------------------------------- #
def _diff_channel(channel: str, base_cmds: list[dict], target_cmds: list[dict],
                  base_side: Side, target_side: Side,
                  base_skips: dict, target_skips: dict) -> dict:
    entries: list[dict] = []
    counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    field_counts: dict[str, int] = {}

    for at, pairs in _pair_times(base_cmds, target_cmds):
        for b, t in pairs:
            if b is not None and t is None:
                entry = _entry("removed", at, b, None, [])
            elif b is None and t is not None:
                entry = _entry("added", at, None, t, [])
            else:
                changes = _field_changes(b, t)
                kind = "changed" if changes else "unchanged"
                entry = _entry(kind, at, b, t, changes)
            entry["channel"] = channel
            _annotate(entry, base_side, target_side, base_skips, target_skips)
            entries.append(entry)
            counts[entry["kind"]] += 1
            for c in entry["changes"]:
                field_counts[c["field"]] = field_counts.get(c["field"], 0) + 1

    # Emergency suppressions that only exist on one side (a normal activation
    # is newly blocked, or no longer blocked, even though its command slot may
    # have been absent from the timeline on the other side).
    suppression_changes = _suppression_changes(channel, base_skips, target_skips)
    return {
        "channel": channel,
        "present_in": {
            "base": bool(base_cmds) or channel in base_side.plan["lamp_defaults"],
            "target": bool(target_cmds) or channel in target_side.plan["lamp_defaults"],
        },
        "default_level": {
            "base": base_side.plan["lamp_defaults"].get(channel),
            "target": target_side.plan["lamp_defaults"].get(channel),
        },
        "summary": {
            "base_commands": len(base_cmds),
            "target_commands": len(target_cmds),
            **counts,
            "fields_changed": field_counts,
            "suppression_changes": len(suppression_changes),
        },
        "entries": entries,
        "suppression_changes": suppression_changes,
    }


def _suppression_changes(channel: str, base_skips: dict, target_skips: dict) -> list[dict]:
    out: list[dict] = []
    b_keys = {k: v for k, v in base_skips.items() if k[0] == channel}
    t_keys = {k: v for k, v in target_skips.items() if k[0] == channel}
    for k in sorted(set(b_keys) | set(t_keys), key=lambda k: k[1]):
        b, t = b_keys.get(k), t_keys.get(k)
        if b and not t:
            out.append({"kind": "released", "at": k[1], "base": b, "target": None,
                        "message": f"Normal activation is no longer suppressed on '{channel}'."})
        elif t and not b:
            out.append({"kind": "suppressed", "at": k[1], "base": None, "target": t,
                        "message": f"Normal activation on '{channel}' becomes suppressed by emergency preemption."})
        elif b["preempted_by"] != t["preempted_by"]:
            out.append({"kind": "preemptor_changed", "at": k[1], "base": b, "target": t,
                        "message": f"Suppressing emergency on '{channel}' changes from "
                                   f"'{b['preempted_by']}' to '{t['preempted_by']}'."})
    return out


# --------------------------------------------------------------------------- #
# Preemption-window changes
# --------------------------------------------------------------------------- #
def _preemption_changes(base_plan: dict, target_plan: dict) -> dict:
    bp = _pre_index(base_plan)
    tp = _pre_index(target_plan)
    added, removed, changed = [], [], []
    for key in sorted(set(bp) | set(tp), key=lambda k: (k[0], k[2])):
        b, t = bp.get(key), tp.get(key)
        if b and not t:
            removed.append(b)
        elif t and not b:
            added.append(t)
        else:
            fields = {}
            for f in ("restored_scene_id", "restored_level"):
                if not _same(b.get(f), t.get(f)):
                    fields[f] = {"from": b.get(f), "to": t.get(f)}
            if not _same(sorted(b.get("skipped_scene_ids", [])), sorted(t.get("skipped_scene_ids", []))):
                fields["skipped_scene_ids"] = {
                    "from": b.get("skipped_scene_ids", []),
                    "to": t.get("skipped_scene_ids", []),
                }
            if not _same(b.get("to"), t.get("to")):
                fields["window_to"] = {"from": b.get("to"), "to": t.get("to")}
            if fields:
                changed.append({"key": {"channel": key[0], "emergency_scene_id": key[1], "from": key[2]},
                                "base": b, "target": t, "fields": fields})
    return {"added": added, "removed": removed, "changed": changed}


# --------------------------------------------------------------------------- #
# Top-level delivery roll-up
# --------------------------------------------------------------------------- #
def _side_delivery_totals(side: Side) -> dict:
    """Post-watermark command counts for one version's delivery queue."""
    view = side.view
    total_eligible = 0
    states = {"acked": 0, "leased_live": 0, "leased_expired": 0, "waiting": 0, "undue": 0}
    if view is not None:
        for entries in view["channels"].values():
            total_eligible += len(entries)
            for e in entries:
                if _parse(e["at"]) > side.now and e["state"] == "waiting":
                    states["undue"] += 1
                else:
                    states[e["state"]] = states.get(e["state"], 0) + 1
    return {
        "version": side.version,
        "is_current_version": view is not None,
        "published_at": side.published_at.isoformat(),
        "eligible_commands": total_eligible,
        "watermark_filtered_commands": sum(
            1
            for ch, cmds in side.plan["timeline"].items()
            for c in cmds
            if _parse(c["at"]) < side.published_at
        ),
        "states": states,
        "outstanding_tasks": states["leased_live"] + states["leased_expired"] + states["waiting"],
    }


def diff_plans(base: Side, target: Side) -> dict[str, Any]:
    """Compile the full comparison rehearsal for two sides of one hall."""
    base_skips = _skip_index(base.plan)
    target_skips = _skip_index(target.plan)

    channels = sorted(set(base.plan["timeline"]) | set(target.plan["timeline"]))
    channel_diffs: list[dict] = []
    totals = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    field_totals: dict[str, int] = {}
    channels_added: list[str] = []
    channels_removed: list[str] = []

    for ch in channels:
        cd = _diff_channel(
            ch,
            base.plan["timeline"].get(ch, []),
            target.plan["timeline"].get(ch, []),
            base, target, base_skips, target_skips,
        )
        channel_diffs.append(cd)
        for k in ("added", "removed", "changed", "unchanged"):
            totals[k] += cd["summary"][k]
        for f, n in cd["summary"]["fields_changed"].items():
            field_totals[f] = field_totals.get(f, 0) + n
        if ch not in base.plan["timeline"]:
            channels_added.append(ch)
        if ch not in target.plan["timeline"]:
            channels_removed.append(ch)

    preemptions = _preemption_changes(base.plan, target.plan)

    def _side_info(side: Side) -> dict:
        return {
            "hall_id": side.hall_id,
            "version": side.version,
            "published_at": side.published_at.isoformat(),
            "created_at": side.created_at.isoformat() if side.created_at else None,
            "updated_at": side.updated_at.isoformat() if side.updated_at else None,
            "summary": side.plan["summary"],
        }

    # Roll the per-entry effects up so operators can spot disruptive switches.
    outstanding_void = 0
    watermark_entries = 0
    preemption_entries = 0
    for cd in channel_diffs:
        for e in cd["entries"]:
            imp = e["delivery_impact"]
            if imp["outstanding_base_task"]:
                outstanding_void += 1
            if imp["watermark_filtered"]:
                watermark_entries += 1
            if imp["preemption_involved"]:
                preemption_entries += 1

    return {
        "dry_run": True,
        "mutates": "nothing: stored orchestration, delivery queues and leases are untouched",
        "hall_id": base.hall_id,
        "base": _side_info(base),
        "target": _side_info(target),
        "server_time": max(base.now, target.now).isoformat(),
        "summary": {
            "channels_compared": len(channels),
            "channels_added": channels_added,
            "channels_removed": channels_removed,
            **totals,
            "fields_changed": field_totals,
            "preemption_windows": {
                "added": len(preemptions["added"]),
                "removed": len(preemptions["removed"]),
                "changed": len(preemptions["changed"]),
            },
            "suppression_changes": sum(cd["summary"]["suppression_changes"] for cd in channel_diffs),
        },
        "preemption_changes": preemptions,
        "delivery_impact": {
            "base": _side_delivery_totals(base),
            "target": _side_delivery_totals(target),
            "entries_with_outstanding_base_task": outstanding_void,
            "entries_touching_watermark_filter": watermark_entries,
            "entries_involving_emergency_preemption": preemption_entries,
            "note": (
                "Outstanding (leased/unacked/waiting) base commands are voided on republish; "
                "commands before a version's publish watermark are never delivered."
            ),
        },
        "channels": channel_diffs,
    }
