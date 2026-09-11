"""Minimal schedules module for local deploy (no cron/root — just stores JSON)."""
from __future__ import annotations
import json, os, uuid
from datetime import datetime, timezone
from typing import Optional

SCHEDULES_FILE = os.environ.get(
    "BOB_SCHEDULES_FILE",
    os.path.join(os.path.dirname(__file__), "schedules.json")
)


class ScheduleError(Exception):
    pass


def load() -> list[dict]:
    try:
        with open(SCHEDULES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(data: list[dict]) -> None:
    with open(SCHEDULES_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get(schedule_id: str) -> Optional[dict]:
    for s in load():
        if s["id"] == schedule_id:
            return s
    return None


def add(name: str, cron: str, mode: str, prompt: str, **kwargs) -> dict:
    sched = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "cron": cron,
        "mode": mode,
        "prompt": prompt,
        "created": datetime.now(timezone.utc).isoformat(),
        "last_run": None,
        "last_run_id": None,
        "last_status": None,
        **kwargs,
    }
    data = load()
    data.append(sched)
    _save(data)
    return sched


def remove(schedule_id: str) -> bool:
    data = load()
    new = [s for s in data if s["id"] != schedule_id]
    if len(new) == len(data):
        return False
    _save(new)
    return True


def mark_run(schedule_id: str, run_id: str, status: str) -> None:
    data = load()
    for s in data:
        if s["id"] == schedule_id:
            s["last_run"] = datetime.now(timezone.utc).isoformat()
            s["last_run_id"] = run_id
            s["last_status"] = status
    _save(data)


def sync() -> None:
    """On local deploy: no-op (no cron daemon). On Linux: would sync crontab."""
    pass
