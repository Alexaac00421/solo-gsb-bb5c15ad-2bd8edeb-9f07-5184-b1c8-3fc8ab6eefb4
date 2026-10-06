"""Point-in-time state simulation over a compiled plan."""
from __future__ import annotations

from datetime import datetime

from .models import as_utc


def _level_from_commands(t: datetime, commands: list[dict], default: float) -> tuple[float, str | None, str | None]:
    """Return ``(level, effective_scene_id, effective_priority)`` at ``t``."""
    level = default
    scene_id: str | None = None
    priority: str | None = None
    for c in commands:
        at = datetime.fromisoformat(c["at"])
        if at > t:
            break
        end = datetime.fromisoformat(c["fade_end_at"])
        level = float(c["to_level"]) if t >= end or end <= at else float(c["from_level"]) + (
            float(c["to_level"]) - float(c["from_level"])
        ) * min(1.0, max(0.0, (t - at).total_seconds() / (end - at).total_seconds()))
        if c["type"] == "emergency_enter":
            scene_id, priority = c["scene_id"], "emergency"
        elif c["type"] == "emergency_exit":
            # After release the channel belongs to the restored normal scene;
            # if nothing was restored the emergency level is merely being held.
            scene_id = c.get("restored_scene_id")
            priority = "normal" if scene_id else None
        else:
            scene_id, priority = c["scene_id"], "normal"
    return round(level, 2), scene_id, priority


def simulate(plan: dict, at: datetime) -> dict:
    t = as_utc(at)
    windows = plan.get("scene_windows", {})
    defaults = plan.get("lamp_defaults", {})

    active_scenes: list[str] = []
    active_emergencies: list[str] = []
    for sid, w in windows.items():
        start = datetime.fromisoformat(w["start_at"])
        end = datetime.fromisoformat(w["end_at"])
        if start <= t < end:
            active_scenes.append(sid)
            if w["priority"] == "emergency":
                active_emergencies.append(sid)

    channels = {}
    for ch, commands in plan["timeline"].items():
        level, scene_id, priority = _level_from_commands(t, commands, float(defaults.get(ch, 0.0)))
        channels[ch] = {
            "level": level,
            "active_scene_id": scene_id,
            "priority": priority,
            "in_emergency": priority == "emergency"
            and any(
                datetime.fromisoformat(w["start_at"]) <= t < datetime.fromisoformat(w["end_at"])
                for sid, w in windows.items()
                if sid == scene_id and w["priority"] == "emergency"
            ),
        }

    return {
        "hall_id": plan["hall_id"],
        "at": t.isoformat(),
        "active_scene_ids": sorted(active_scenes),
        "active_emergency_scene_ids": sorted(active_emergencies),
        "channels": channels,
    }
