from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


DATA_UPDATE_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_DATA_UPDATE_TIMES = "00:15,02:15,04:15,08:15,14:15,18:15,22:15"
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def parse_update_times(value: str) -> tuple[tuple[int, int], ...]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise ValueError("DATA_UPDATE_TIMES 不能为空")
    invalid = [part for part in parts if not _TIME_PATTERN.fullmatch(part)]
    if invalid:
        raise ValueError(f"DATA_UPDATE_TIMES 包含无效时间：{', '.join(invalid)}")
    return tuple(sorted({tuple(map(int, part.split(":", 1))) for part in parts}))


def format_update_times(times: tuple[tuple[int, int], ...]) -> str:
    return "、".join(f"{hour:02d}:{minute:02d}" for hour, minute in times)


def next_update_at(current: datetime, times: tuple[tuple[int, int], ...]) -> datetime:
    if not times:
        raise ValueError("自动更新时间表不能为空")
    local_now = current.replace(tzinfo=DATA_UPDATE_TIMEZONE) if current.tzinfo is None else current.astimezone(DATA_UPDATE_TIMEZONE)
    for hour, minute in times:
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > local_now:
            return candidate
    next_day = local_now + timedelta(days=1)
    hour, minute = times[0]
    return next_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
