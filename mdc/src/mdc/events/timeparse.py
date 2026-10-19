"""Reading "as of" times typed in chat.

    2026-10-19T10:30:00Z   an exact time (no zone = UTC)
    2026-10-19             the END of that day, UTC - "as of the 19th" means
                           everything that happened on it
    15 minutes ago         relative to now (seconds, minutes, hours, days, weeks)
    now
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_RELATIVE = re.compile(r"^(\d+)\s*(second|sec|minute|min|hour|hr|day|week)s?\s+ago$", re.IGNORECASE)
_UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

HELP = "Use a time like 2026-10-19T10:30:00Z, a date like 2026-10-19 (end of that day, UTC), or '15 minutes ago'."


def parse_time(text: str, now: datetime | None = None) -> datetime:
    """The moment `text` names, as an aware UTC datetime; ValueError if unreadable."""
    now = now or datetime.now(timezone.utc)
    cleaned = text.strip().strip("\"'").rstrip(".")
    if cleaned.lower() == "now":
        return now
    relative = _RELATIVE.match(cleaned)
    if relative:
        return now - timedelta(seconds=int(relative.group(1)) * _UNITS[relative.group(2).lower()])
    try:
        moment = datetime.fromisoformat(cleaned.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise ValueError(f"I couldn't read {text.strip()!r} as a time. {HELP}") from None
    if _DATE_ONLY.match(cleaned):
        moment = moment + timedelta(days=1) - timedelta(microseconds=1)
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
