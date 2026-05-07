"""User-facing time formatting in CDT.

All timestamps inside the engine are stored as UTC unix integers (or naive
UTC ISO strings for the watchlist table). User-facing output (logs, replay
emit lines, telemetry) goes through ``fmt_cdt`` so the human sees America/
Chicago wall-clock time. The zone name is "America/Chicago" — that auto-
follows DST, so it's CDT in summer (UTC-5) and CST in winter (UTC-6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
    _CDT = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover — only on systems without tzdata
    _CDT = None


def to_cdt(value: int | float | datetime | str) -> datetime:
    """Coerce a timestamp-like value to a tz-aware datetime in CDT.

    Accepts:
      - unix seconds (int/float)
      - datetime (naive treated as UTC, aware re-zoned)
      - ISO-8601 string (with or without 'Z' suffix)
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    elif isinstance(value, (int, float)):
        value = datetime.fromtimestamp(int(value), tz=timezone.utc)
    elif isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        value = datetime.fromisoformat(s)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    else:
        raise TypeError(f"unsupported time type: {type(value).__name__}")

    if _CDT is None:
        # tzdata unavailable — fall back to a fixed UTC-5 offset (best effort).
        return value.astimezone(timezone(timedelta(hours=-5)))
    return value.astimezone(_CDT)


def fmt_cdt(
    value: int | float | datetime | str,
    fmt: str = "%Y-%m-%d %H:%M %Z",
) -> str:
    """Format a timestamp as a CDT/CST wall-clock string."""
    return to_cdt(value).strftime(fmt)
