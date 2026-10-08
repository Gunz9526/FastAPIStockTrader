from datetime import UTC, datetime
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str | datetime) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if dt.tzinfo is None:
        raise ValueError("naive timestamp rejected")
    return dt.astimezone(UTC)


def trading_day(now: datetime) -> str:
    return now.astimezone(NEW_YORK).date().isoformat()
