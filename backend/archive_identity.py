from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


PROVISIONAL_ID_PATTERN = re.compile(r"500-周.\d{3}")


def normalize_team(value: Any) -> str:
    return re.sub(r"[\s·.\-（）()\[\]]+", "", str(value or "")).lower()


def normalize_kickoff(value: Any) -> str:
    text = str(value or "").strip().replace(" ", "T")
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    return parsed.strftime("%Y-%m-%dT%H:%M")


def archive_match_aliases(item: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    canonical_key = str(item.get("canonicalMatchKey") or "").strip()
    fixture_id = str(item.get("fixtureId") or item.get("fixture_id") or "").strip()
    item_id = str(item.get("id") or "").strip()
    if canonical_key:
        aliases.add(f"canonical:{canonical_key}")
    if fixture_id:
        aliases.add(f"fixture:{fixture_id}")
    if item_id and not PROVISIONAL_ID_PATTERN.fullmatch(item_id):
        aliases.add(f"id:{item_id}")

    home = normalize_team(item.get("home"))
    away = normalize_team(item.get("away"))
    kickoff = normalize_kickoff(item.get("date"))
    if home and away and kickoff:
        aliases.add(f"kickoff:{kickoff}:{home}:{away}")
    return aliases


def archive_records_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(archive_match_aliases(left) & archive_match_aliases(right))


def unique_archive_rows(rows: list[Any]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        matches = [index for index, existing in enumerate(unique) if archive_records_match(existing, item)]
        if not matches:
            unique.append(item)
            continue
        primary = matches[0]
        merged = dict(unique[primary])
        merged.update({key: value for key, value in item.items() if value not in (None, "", [], {})})
        for duplicate in reversed(matches[1:]):
            merged.update({key: value for key, value in unique[duplicate].items() if value not in (None, "", [], {})})
            unique.pop(duplicate)
        unique[primary] = merged
    return unique


def archive_covers_previous(previous: list[Any], incoming: list[Any]) -> bool:
    previous_rows = unique_archive_rows(previous)
    incoming_rows = unique_archive_rows(incoming)
    return all(any(archive_records_match(old, new) for new in incoming_rows) for old in previous_rows)
