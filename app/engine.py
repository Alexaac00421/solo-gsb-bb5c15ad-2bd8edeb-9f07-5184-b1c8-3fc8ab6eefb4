"""Timeline compiler.

Pure functions: a :class:`HallIn` is validated and compiled into a per-channel
executable timeline. Nothing here touches the store, so a rejected orchestration
is never persisted.

Rules implemented
-----------------
* Every scene item must reference a declared lamp; level must be within 0..100.
* A scene may target a lamp at most once; its time window must be valid.
* For any one channel, the *fade intervals* of normal scenes must not overlap
  (half-open intervals ``[start_at, start_at + fade)``; touching at an endpoint
  is legal).
* Emergency scenes must not overlap each other in their active windows.
* The compiled timeline schedules normal fades; an emergency scene cuts in at
  its ``start_at`` (fading for the item's ``fade_seconds``), holds its level for
  the whole emergency window, and at ``end_at`` hands the channel back only to a
  normal scene that had already started before the emergency and is still in its
  window at release. A normal scene that first becomes due *during* the emergency
  is skipped on the preempted channel and is never resumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .models import HallIn, LampIn, Priority, SceneIn


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #
@dataclass
class Issue:
    code: str
    message: str
    scene_id: str | None = None
    channel: str | None = None
    interval: tuple[datetime, datetime] | None = None
    between: tuple[str, str] | None = None

    def to_dict(self) -> dict:
        d: dict = {"code": self.code, "message": self.message}
        if self.scene_id is not None:
            d["scene_id"] = self.scene_id
        if self.channel is not None:
            d["channel"] = self.channel
        if self.interval is not None:
            d["interval"] = [self.interval[0].isoformat(), self.interval[1].isoformat()]
        if self.between is not None:
            d["between_scenes"] = list(self.between)
        return d


def _iso(t: datetime) -> str:
    return t.isoformat()


def _q2(v: float) -> float:
    """Round to 2 decimals (percent) so output stays deterministic/DMX-friendly."""
    return round(float(v) + 0.0, 2)


def _find_duplicates(ids: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    dup: set[str] = set()
    for i in ids:
        (dup if i in seen else seen).add(i)
    return dup


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_hall(hall: HallIn) -> tuple[list[Issue], list[Issue], dict[str, LampIn]]:
    """Return ``(errors, warnings, lamps_by_id)``. Errors block compilation."""
    errors: list[Issue] = []
    warnings: list[Issue] = []

    for dup in sorted(_find_duplicates(l.id for l in hall.lamps)):
        errors.append(Issue("duplicate_lamp", f"Lamp id '{dup}' is declared more than once.", channel=dup))
    lamps = {l.id: l for l in hall.lamps}

    for dup in sorted(_find_duplicates(s.id for s in hall.scenes)):
        errors.append(Issue("duplicate_scene", f"Scene id '{dup}' is declared more than once.", scene_id=dup))

    for s in hall.scenes:
        if s.end_at <= s.start_at:
            errors.append(
                Issue(
                    "invalid_time_window",
                    f"end_at ({_iso(s.end_at)}) must be later than start_at ({_iso(s.start_at)}).",
                    scene_id=s.id,
                    interval=(s.start_at, s.end_at),
                )
            )
        for dup in sorted(_find_duplicates(it.lamp_id for it in s.items)):
            errors.append(
                Issue(
                    "duplicate_channel_in_scene",
                    f"Channel '{dup}' is targeted more than once inside scene '{s.id}'.",
                    scene_id=s.id,
                    channel=dup,
                )
            )
        for it in s.items:
            if it.lamp_id not in lamps:
                errors.append(
                    Issue(
                        "unknown_lamp",
                        f"Scene '{s.id}' references lamp '{it.lamp_id}' which is not declared in hall '{hall.id}'.",
                        scene_id=s.id,
                        channel=it.lamp_id,
                    )
                )
            if not (0 <= it.level <= 100):
                errors.append(
                    Issue(
                        "level_out_of_range",
                        f"Level {it.level} for channel '{it.lamp_id}' in scene '{s.id}' is outside 0..100.",
                        scene_id=s.id,
                        channel=it.lamp_id,
                    )
                )
            fade_end = s.start_at + timedelta(seconds=it.fade_seconds)
            if fade_end > s.end_at:
                warnings.append(
                    Issue(
                        "fade_exceeds_window",
                        f"Fade of {it.fade_seconds}s on '{it.lamp_id}' extends past the scene window; "
                        "the target level is held once reached.",
                        scene_id=s.id,
                        channel=it.lamp_id,
                        interval=(s.start_at, fade_end),
                    )
                )

    if errors:
        # Cross-scene checks only add noise when references/windows are broken.
        return errors, warnings, lamps

    # Per-channel overlap of normal fade intervals (half-open, touching is fine).
    for ch in sorted(lamps):
        entries = []
        for s in hall.scenes:
            if s.priority is Priority.NORMAL:
                it = next((x for x in s.items if x.lamp_id == ch), None)
                if it is not None:
                    entries.append((s, it))
        entries.sort(key=lambda e: e[0].start_at)
        for i, (s1, it1) in enumerate(entries):
            a1 = s1.start_at + timedelta(seconds=it1.fade_seconds)
            for s2, it2 in entries[i + 1 :]:
                if s2.start_at >= a1:
                    break  # start-sorted: nothing later can overlap s1
                b1 = s2.start_at + timedelta(seconds=it2.fade_seconds)
                if b1 > s1.start_at:
                    lo = max(s1.start_at, s2.start_at)
                    hi = min(a1, b1)
                    errors.append(
                        Issue(
                            "fade_interval_overlap",
                            f"Fade intervals on channel '{ch}' overlap for scenes '{s1.id}' and '{s2.id}' "
                            f"between {_iso(lo)} and {_iso(hi)}.",
                            scene_id=s2.id,
                            channel=ch,
                            interval=(lo, hi),
                            between=(s1.id, s2.id),
                        )
                    )

    # Emergency windows must not overlap one another.
    emergencies = sorted(
        (s for s in hall.scenes if s.priority is Priority.EMERGENCY),
        key=lambda s: s.start_at,
    )
    for e1, e2 in zip(emergencies, emergencies[1:]):
        if e2.start_at < e1.end_at and e2.end_at > e1.start_at:
            lo = max(e1.start_at, e2.start_at)
            hi = min(e1.end_at, e2.end_at)
            shared = sorted({it.lamp_id for it in e1.items} & {it.lamp_id for it in e2.items})
            errors.append(
                Issue(
                    "emergency_overlap",
                    f"Emergency scenes '{e1.id}' and '{e2.id}' overlap between {_iso(lo)} and {_iso(hi)}"
                    + (f" on channels {shared}." if shared else "."),
                    scene_id=e2.id,
                    channel=shared[0] if shared else None,
                    interval=(lo, hi),
                    between=(e1.id, e2.id),
                )
            )

    return errors, warnings, lamps


# --------------------------------------------------------------------------- #
# Timeline synthesis
# --------------------------------------------------------------------------- #
def _fade_end(scene: SceneIn, fade_seconds: float) -> datetime:
    return scene.start_at + timedelta(seconds=fade_seconds)


def _level_at(t: datetime, commands: list[dict], default: float) -> float:
    """Brightness of the (already merged) command list at time ``t``."""
    level = default
    for c in commands:
        if c["at"] > t:
            break
        start, end = c["at"], c["fade_end_at"]
        if t >= end or end <= start:
            level = c["to_level"]
        else:
            frac = (t - start).total_seconds() / (end - start).total_seconds()
            level = c["from_level"] + (c["to_level"] - c["from_level"]) * min(1.0, max(0.0, frac))
    return level


def _level_just_before(t: datetime, commands: list[dict], default: float) -> float:
    """Brightness an instant before ``t``: commands starting exactly at ``t``
    have not taken effect yet (prevents simultaneous restore->scene flashes)."""
    level = default
    for c in commands:
        if c["at"] >= t:
            break
        start, end = c["at"], c["fade_end_at"]
        if t >= end or end <= start:
            level = c["to_level"]
        else:
            frac = (t - start).total_seconds() / (end - start).total_seconds()
            level = c["from_level"] + (c["to_level"] - c["from_level"]) * min(1.0, max(0.0, frac))
    return level


def _normal_start_level(normal_cmds: list[dict], scene: SceneIn, default: float) -> float:
    """Uninterrupted normal-schedule level at the start of ``scene``."""
    level = default
    for c in normal_cmds:
        if c["at"] >= scene.start_at:
            break
        level = c["to_level"]
    return level


def build_channel_timeline(
    channel: str,
    hall: HallIn,
    lamps: dict[str, LampIn],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Compile one channel -> ``(commands, preemptions, skipped)``."""
    default_level = float(lamps[channel].default_level)

    normals: list[tuple[SceneIn, object]] = []
    emergencies: list[tuple[SceneIn, object]] = []
    for s in hall.scenes:
        it = next((x for x in s.items if x.lamp_id == channel), None)
        if it is None:
            continue
        (emergencies if s.priority is Priority.EMERGENCY else normals).append((s, it))
    normals.sort(key=lambda e: e[0].start_at)
    emergencies.sort(key=lambda e: e[0].start_at)
    # Validation guarantees emergency windows are pairwise disjoint.
    windows = [(s.start_at, s.end_at, s, it) for s, it in emergencies]

    def covering(t: datetime):
        for lo, hi, s, it in windows:
            if lo <= t < hi:
                return lo, hi, s, it
        return None

    # --- Normal keyframes --------------------------------------------------- #
    # A normal scene is suppressed on this channel only when its start instant
    # falls inside an emergency window. A scene already fading when the emergency
    # cuts in is kept (it is the preempted owner candidate), its command simply
    # gets superseded by emergency_enter.
    normal_cmds: list[dict] = []
    skipped: list[dict] = []
    skipped_ids: set[str] = set()
    for s, it in normals:
        hit = covering(s.start_at)
        if hit is not None:
            skipped_ids.add(s.id)
            skipped.append(
                {
                    "scene_id": s.id,
                    "channel": channel,
                    "scheduled_at": _iso(s.start_at),
                    "preempted_by": hit[2].id,
                    "reason": f"Scene is due while emergency scene '{hit[2].id}' owns channel '{channel}'; "
                    "it is not activated and not resumed afterwards.",
                }
            )
            continue
        normal_cmds.append(
            {
                "type": "fade",
                "at": s.start_at,
                "from_level": default_level,  # recomputed after the merge
                "to_level": _q2(it.level),
                "fade_end_at": _fade_end(s, it.fade_seconds),
                "scene_id": s.id,
                "priority": "normal",
            }
        )
    normal_cmds.sort(key=lambda c: c["at"])

    # --- Merge emergency segments ------------------------------------------ #
    commands: list[dict] = []
    preemptions: list[dict] = []
    used = 0
    for lo, hi, em, em_it in windows:
        while used < len(normal_cmds) and normal_cmds[used]["at"] < lo:
            commands.append(normal_cmds[used])
            used += 1

        entry_level = _level_at(lo, commands, default_level)
        entry_fade = max(0.0, float(em_it.fade_seconds))
        entry_end = min(lo + timedelta(seconds=entry_fade), hi)
        commands.append(
            {
                "type": "emergency_enter",
                "at": lo,
                "from_level": _q2(entry_level),
                "to_level": _q2(em_it.level),
                "fade_end_at": entry_end,
                "scene_id": em.id,
                "priority": "emergency",
            }
        )

        # Owner = normal scene that had started strictly before the emergency AND
        # is still inside its window when the emergency releases. Anything else is
        # unrecoverable by contract.
        owner: tuple[SceneIn, object] | None = None
        for s, it in normals:
            if s.id in skipped_ids:
                continue
            if s.start_at < lo < s.end_at and s.end_at > hi:
                owner = (s, it)
                break

        held = None
        if owner is not None:
            os, oit = owner
            start_level = _normal_start_level(normal_cmds, os, default_level)
            owner_fade_end = _fade_end(os, oit.fade_seconds)
            if owner_fade_end <= lo:
                held = _q2(oit.level)
            else:
                span = (owner_fade_end - os.start_at).total_seconds()
                frac = min(1.0, (lo - os.start_at).total_seconds() / span) if span > 0 else 1.0
                held = _q2(start_level + (oit.level - start_level) * frac)

        restore_fade = max(
            0.0,
            float(
                em_it.restore_fade_seconds
                if em_it.restore_fade_seconds is not None
                else em_it.fade_seconds
            ),
        )
        commands.append(
            {
                "type": "emergency_exit",
                "at": hi,
                "from_level": _q2(em_it.level),
                "to_level": held if held is not None else _q2(em_it.level),
                "fade_end_at": hi + timedelta(seconds=restore_fade) if held is not None else hi,
                "scene_id": em.id,
                "priority": "emergency",
                "restored_scene_id": owner[0].id if owner is not None else None,
                "note": None
                if owner is not None
                else "No normal scene active before preemption remains in window; "
                "the emergency level is held until the next scheduled scene.",
            }
        )
        preemptions.append(
            {
                "emergency_scene_id": em.id,
                "channel": channel,
                "from": _iso(lo),
                "to": _iso(hi),
                "restored_scene_id": owner[0].id if owner is not None else None,
                "restored_level": held,
                "skipped_scene_ids": sorted(
                    s.id for s, _ in normals if s.id in skipped_ids and lo <= s.start_at < hi
                ),
            }
        )

    commands.extend(normal_cmds[used:])
    # Emergency events order before a normal keyframe sharing the same instant:
    # exit-at-hi then normal-at-hi means the scheduled scene takes over cleanly.
    commands.sort(key=lambda c: (c["at"], 0 if c["priority"] == "emergency" else 1))

    # If a normal scene is scheduled exactly at the release instant, it takes
    # over directly: the emergency exit must not emit a (potentially visible)
    # restore jump that is immediately overwritten.
    normal_starts = {c["at"] for c in commands if c["priority"] == "normal"}
    for c in commands:
        if c["type"] == "emergency_exit" and c["at"] in normal_starts:
            takeover = next(n["scene_id"] for n in normal_cmds if n["at"] == c["at"])
            # Hold the emergency value for zero time; the simultaneous scene
            # becomes the new envelope. from_level is recomputed in the next pass.
            c["to_level"] = float(c["from_level"])
            c["fade_end_at"] = c["at"]
            c["taken_over_by"] = takeover
            c["note"] = f"A scheduled scene '{takeover}' starts at release; it takes over directly."

    # Recompute every from_level against the real, preempted envelope so the
    # emitted instructions are executable as-is.
    for i, c in enumerate(commands):
        c["from_level"] = _q2(_level_just_before(c["at"], commands[:i], default_level))

    out = []
    for c in commands:
        out.append(
            {
                "type": c["type"],
                "at": _iso(c["at"]),
                "from_level": c["from_level"],
                "to_level": c["to_level"],
                "fade_end_at": _iso(c["fade_end_at"]),
                "fade_seconds": _q2((c["fade_end_at"] - c["at"]).total_seconds()),
                "scene_id": c["scene_id"],
                "priority": c["priority"],
                **({"restored_scene_id": c["restored_scene_id"]} if "restored_scene_id" in c else {}),
                **({"taken_over_by": c["taken_over_by"]} if c.get("taken_over_by") else {}),
                **({"note": c["note"]} if c.get("note") else {}),
            }
        )
    return out, preemptions, skipped


# --------------------------------------------------------------------------- #
# Hall compilation
# --------------------------------------------------------------------------- #
def compile_hall(hall: HallIn) -> dict:
    errors, warnings, lamps = validate_hall(hall)
    if errors:
        return {"ok": False, "errors": [e.to_dict() for e in errors], "warnings": [w.to_dict() for w in warnings]}

    timeline: dict[str, list[dict]] = {}
    preemptions: list[dict] = []
    skipped: list[dict] = []
    for lamp in hall.lamps:
        cmds, pre, skp = build_channel_timeline(lamp.id, hall, lamps)
        timeline[lamp.id] = cmds
        preemptions.extend(pre)
        skipped.extend(skp)

    normal_count = sum(1 for s in hall.scenes if s.priority is Priority.NORMAL)
    return {
        "ok": True,
        "errors": [],
        "warnings": [w.to_dict() for w in warnings],
        "hall_id": hall.id,
        "lamp_defaults": {l.id: _q2(l.default_level) for l in hall.lamps},
        "scene_windows": {
            s.id: {
                "priority": s.priority.value,
                "start_at": _iso(s.start_at),
                "end_at": _iso(s.end_at),
                "channels": sorted(it.lamp_id for it in s.items),
            }
            for s in hall.scenes
        },
        "timeline": timeline,
        "summary": {
            "lamps": len(hall.lamps),
            "scenes": len(hall.scenes),
            "normal_scenes": normal_count,
            "emergency_scenes": len(hall.scenes) - normal_count,
            "commands": sum(len(v) for v in timeline.values()),
            "preemptions": len(preemptions),
            "skipped_scene_activations": len(skipped),
        },
        "preemptions": preemptions,
        "skipped_activations": skipped,
    }
