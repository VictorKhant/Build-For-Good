"""Shared feasibility rules: opening windows, storage, rejection reporting."""

from __future__ import annotations

from datetime import datetime, timedelta

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

STORAGE_FOR = {
    "hot": "hot_holding",
    "refrigerated": "refrigerated",
    "frozen": "frozen",
    "ambient": None,          # dry goods need no special storage
}
COLD_CONDITIONS = {"refrigerated", "frozen"}


def hhmm(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def week_of_month(d: datetime) -> int:
    return (d.day - 1) // 7 + 1


def is_last_dow(d: datetime) -> bool:
    return (d + timedelta(days=7)).month != d.month


def window_matches(when: datetime, w: dict) -> bool:
    """Weekly window, optionally restricted to certain weeks of the month.

    Many pantry sites distribute on a monthly cadence -- "1st and 3rd Thursday"
    -- not weekly. Flattening that would have the engine confidently propose
    runs to sites closed three weeks in four. -1 means "last".
    """
    if w["dow"] != when.weekday():
        return False
    minutes = when.hour * 60 + when.minute
    if not (hhmm(w["start"]) <= minutes <= hhmm(w["end"])):
        return False
    weeks = w.get("weeks_of_month")
    if not weeks:
        return True
    return week_of_month(when) in weeks or (-1 in weeks and is_last_dow(when))


def in_windows(when: datetime, windows: list[dict]) -> bool:
    return any(window_matches(when, w) for w in windows)


def next_open(when: datetime, windows: list[dict]) -> str:
    if not windows:
        return "no published hours"
    for offset in range(40):          # monthly cadences can be weeks out
        day = when + timedelta(days=offset)
        for w in sorted([x for x in windows if x["dow"] == day.weekday()],
                        key=lambda x: x["start"]):
            start = day.replace(hour=hhmm(w["start"]) // 60,
                                minute=hhmm(w["start"]) % 60,
                                second=0, microsecond=0)
            if start >= when and window_matches(start, w):
                return ("today " if offset == 0 else day.strftime("%a %-d %b ")) + w["start"]
    return "no published hours"


def next_window_start(when: datetime, windows: list[dict]) -> datetime | None:
    """First moment at or after `when` that falls inside one of these windows.

    Returns `when` itself if it is already inside one. None if nothing in the
    next 40 days matches -- long enough to catch a monthly cadence.
    """
    if not windows:
        return None
    if in_windows(when, windows):
        return when
    for offset in range(40):
        day = when + timedelta(days=offset)
        for w in sorted([x for x in windows if x["dow"] == day.weekday()],
                        key=lambda x: x["start"]):
            start = day.replace(hour=hhmm(w["start"]) // 60,
                                minute=hhmm(w["start"]) % 60,
                                second=0, microsecond=0)
            if start >= when and window_matches(start, w):
                return start
    return None


# --------------------------------------------------------------------------
# rejection reporting
# --------------------------------------------------------------------------

def reject(actor_id: str, actor_name: str, code: str, msg: str, **extra) -> dict:
    return {"actor_id": actor_id, "actor_name": actor_name,
            "reason_code": code, "reason": msg, **extra}


# How much a reason tells the person holding the food, most useful first.
# Deliberately NOT frequency order: the commonest reason is usually the least
# informative one.
INFORMATIVENESS = [
    "LIMIT_REACHED", "INTAKE_SATURATED", "NO_WALK_IN_DEMAND", "NET_NEGATIVE", "INEFFICIENT",
    "SPOILS_BEFORE_REACHED", "EXPIRES_IN_TRANSIT", "COLD_CHAIN", "AGENCY_CLOSED",
    "NO_STORAGE",
    "NO_COLD_VEHICLE", "OVER_CAPACITY", "DEMAND_TOO_SMALL", "QTY_TOO_SMALL",
    "TYPE_NOT_ACCEPTED", "NOT_COLLECTING", "NO_MOBILE_PANTRY",
]


def summarise(rejections: list[dict], priority: set[str] | None = None) -> list[dict]:
    """Grouped by reason code, ordered by what the reason explains."""
    priority = priority or set()
    by_code: dict[str, list[dict]] = {}
    for r in rejections:
        by_code.setdefault(r["reason_code"], []).append(r)

    rank = {c: i for i, c in enumerate(INFORMATIVENESS)}
    out = []
    for k, v in by_code.items():
        hot = [x for x in v if x["actor_id"] in priority]
        out.append({"reason_code": k, "count": len(v),
                    "priority_count": len(hot),
                    "example": (hot or v)[0]["reason"]})
    return sorted(out, key=lambda x: (rank.get(x["reason_code"], 99), -x["count"]))
