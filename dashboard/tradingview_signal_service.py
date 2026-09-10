"""Diagnostic-only TradingView webhook ingestion for GoldScout.

This module never calls MetaTrader or changes trading scores. Incoming alerts are
strictly normalized and stored as append-only JSON Lines with ``score_effect=0``.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping


SOURCE = "tradingview"
NORMALIZED_SYMBOL = "XAUUSD"
TOKEN_ENV = "GOLDSCOUT_TRADINGVIEW_WEBHOOK_TOKEN"
ROOT = Path(__file__).resolve().parent
EVENTS_PATH = ROOT / "data" / "tradingview_events.jsonl"
DEFAULT_STALE_SECONDS = 900
RECENT_EVENT_ID_LIMIT = 4096
RECENT_INDEX_MAX_BYTES = 4 * 1024 * 1024

SYMBOL_ALIASES = {
    "XAUUSD": NORMALIZED_SYMBOL,
    "GOLD": NORMALIZED_SYMBOL,
}
ALLOWED_TIMEFRAMES = {"15", "60", "240"}
ALLOWED_DIRECTIONS = {"LONG", "SHORT", "NEUTRAL"}
ALLOWED_EVENTS = {
    "alert",
    "breakout",
    "momentum",
    "pattern",
    "pullback",
    "recovery",
    "structure",
}
INDICATOR_LIMITS = {
    "rsi": (0.0, 100.0),
    "adx": (0.0, 100.0),
    "atr": (0.0, None),
    "ema20": (0.0, None),
    "ema200": (0.0, None),
}
ALLOWED_FIELDS = {
    "source",
    "symbol",
    "timeframe",
    "event",
    "direction",
    "price",
    "timestamp",
    "token",
    *INDICATOR_LIMITS,
}
MAX_EVENT_NAME_LENGTH = 32
_FILE_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()
_RECENT_INDEXES: dict[str, "_RecentEventIndex"] = {}
_SERVICE_STATUS: dict[str, str] = {}
DIAGNOSTIC_STATUSES = {
    "NO_DATA",
    "OK",
    "STALE",
    "UNAUTHORIZED",
    "RATE_LIMITED",
    "INVALID_PAYLOAD",
}


class PayloadValidationError(ValueError):
    """A sanitized validation error safe to return to the webhook caller."""


class _RecentEventIndex:
    def __init__(self, event_ids: list[str], file_size: int):
        self.event_ids = deque(event_ids[-RECENT_EVENT_ID_LIMIT:])
        self.event_id_set = set(self.event_ids)
        self.file_size = file_size
        self.event_count: int | None = 0 if file_size == 0 else None
        self.last_event: dict[str, Any] | None = None

    def add(self, event_id: str, event: Mapping[str, Any], file_size: int) -> None:
        if event_id not in self.event_id_set:
            self.event_ids.append(event_id)
            self.event_id_set.add(event_id)
        while len(self.event_ids) > RECENT_EVENT_ID_LIMIT:
            removed = self.event_ids.popleft()
            self.event_id_set.discard(removed)
        if self.event_count is not None:
            self.event_count += 1
        self.last_event = dict(event)
        self.file_size = file_size


class SlidingWindowRateLimiter:
    """Small in-memory limiter intended only for the TradingView POST route."""

    def __init__(self, limit: int = 120, window_seconds: float = 60.0, clock=None):
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._requests: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = float(self.clock())
        cutoff = now - self.window_seconds
        with self._lock:
            requests = self._requests.setdefault(str(key), deque())
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self.limit:
                return False
            requests.append(now)
            return True


def _utc_now(now: datetime | None = None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _iso_utc(stamp: datetime) -> str:
    return stamp.isoformat().replace("+00:00", "Z")


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadValidationError(f"{field}_INVALID")
    number = float(value)
    if not math.isfinite(number):
        raise PayloadValidationError(f"{field}_INVALID")
    return number


def _compact_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def normalize_symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise PayloadValidationError("SYMBOL_INVALID")
    normalized = SYMBOL_ALIASES.get(value.strip().upper())
    if not normalized:
        raise PayloadValidationError("SYMBOL_INVALID")
    return normalized


def normalize_timeframe(value: Any) -> str:
    if isinstance(value, bool):
        raise PayloadValidationError("TIMEFRAME_INVALID")
    if isinstance(value, int):
        normalized = str(value)
    elif isinstance(value, str):
        normalized = value.strip()
    else:
        raise PayloadValidationError("TIMEFRAME_INVALID")
    if normalized not in ALLOWED_TIMEFRAMES:
        raise PayloadValidationError("TIMEFRAME_INVALID")
    return normalized


def validate_payload(payload: Any) -> dict[str, Any]:
    """Return only trusted fields from a strictly validated alert payload."""

    if not isinstance(payload, Mapping):
        raise PayloadValidationError("PAYLOAD_INVALID")
    payload = {key: payload[key] for key in ALLOWED_FIELDS if key in payload}

    source = payload.get("source")
    if not isinstance(source, str) or source.strip().lower() != SOURCE:
        raise PayloadValidationError("SOURCE_INVALID")

    direction = payload.get("direction")
    if not isinstance(direction, str):
        raise PayloadValidationError("DIRECTION_INVALID")
    direction = direction.strip().upper()
    if direction not in ALLOWED_DIRECTIONS:
        raise PayloadValidationError("DIRECTION_INVALID")

    event = payload.get("event")
    if not isinstance(event, str):
        raise PayloadValidationError("EVENT_INVALID")
    event = event.strip().lower()
    if len(event) > MAX_EVENT_NAME_LENGTH or event not in ALLOWED_EVENTS:
        raise PayloadValidationError("EVENT_INVALID")

    price = _number(payload.get("price"), "PRICE")
    if price <= 0:
        raise PayloadValidationError("PRICE_INVALID")

    event_timestamp = _number(payload.get("timestamp"), "TIMESTAMP")
    if event_timestamp < 0:
        raise PayloadValidationError("TIMESTAMP_INVALID")

    normalized: dict[str, Any] = {
        "source": SOURCE,
        "symbol": normalize_symbol(payload.get("symbol")),
        "timeframe": normalize_timeframe(payload.get("timeframe")),
        "event": event,
        "direction": direction,
        "price": _compact_number(price),
        "event_timestamp": _compact_number(event_timestamp),
    }
    indicators: dict[str, int | float] = {}
    for name, (minimum, maximum) in INDICATOR_LIMITS.items():
        if name not in payload or payload[name] is None:
            continue
        value = _number(payload[name], name.upper())
        if value < minimum or (maximum is not None and value > maximum):
            raise PayloadValidationError(f"{name.upper()}_INVALID")
        indicators[name] = _compact_number(value)
    normalized["indicators"] = indicators
    return normalized


def deterministic_event_id(event: Mapping[str, Any]) -> str:
    identity = {
        "symbol": event["symbol"],
        "timeframe": event["timeframe"],
        "event": event["event"],
        "direction": event["direction"],
        "event_timestamp": event["event_timestamp"],
        "price": event["price"],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_events_unlocked(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(row, dict) or not row.get("event_id"):
                    continue
                row["score_effect"] = 0
                events.append(row)
    except OSError:
        return []
    return events


def read_events(path: Path = EVENTS_PATH) -> list[dict[str, Any]]:
    with _FILE_LOCK:
        return _read_events_unlocked(Path(path))


def _recent_event_ids(path: Path) -> tuple[list[str], int]:
    if not path.exists():
        return [], 0
    file_size = path.stat().st_size
    if file_size <= 0:
        return [], file_size
    with path.open("rb") as stream:
        read_size = min(file_size, RECENT_INDEX_MAX_BYTES)
        stream.seek(file_size - read_size)
        data = stream.read(read_size)
    if read_size < file_size:
        first_newline = data.find(b"\n")
        data = data[first_newline + 1:] if first_newline >= 0 else b""
    ids: list[str] = []
    for line in data.splitlines()[-RECENT_EVENT_ID_LIMIT:]:
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        event_id = row.get("event_id") if isinstance(row, dict) else None
        if isinstance(event_id, str) and event_id:
            ids.append(event_id)
    return ids, file_size


def _recent_index(path: Path) -> _RecentEventIndex:
    key = str(path.resolve())
    current_size = path.stat().st_size if path.exists() else 0
    index = _RECENT_INDEXES.get(key)
    if index is None or index.file_size != current_size:
        event_ids, current_size = _recent_event_ids(path)
        index = _RecentEventIndex(event_ids, current_size)
        _RECENT_INDEXES[key] = index
    return index


def _event_summary(path: Path) -> tuple[int, dict[str, Any] | None]:
    """Return exact count/latest event, scanning once per file version when needed."""

    with _FILE_LOCK:
        index = _recent_index(path)
        if index.event_count is None:
            count = 0
            last_event = None
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8", errors="strict") as stream:
                        for line in stream:
                            try:
                                row = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                            if isinstance(row, dict) and row.get("event_id"):
                                count += 1
                                last_event = row
                except OSError:
                    return 0, None
            index.event_count = count
            index.last_event = last_event
        last = dict(index.last_event) if index.last_event is not None else None
        return index.event_count, last


def append_event(event: Mapping[str, Any], path: Path = EVENTS_PATH) -> bool:
    """Append one complete JSONL record; return False when it is a duplicate."""

    target = Path(path)
    event_id = str(event["event_id"])
    encoded = (json.dumps(dict(event), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _FILE_LOCK:
        index = _recent_index(target)
        if event_id in index.event_id_set:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        descriptor = os.open(target, flags, 0o600)
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise OSError("incomplete TradingView event write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        index.add(event_id, event, index.file_size + len(encoded))
    return True


def configured_token() -> str:
    return os.getenv(TOKEN_ENV, "").strip()


def _authorized(provided: Any, expected: str) -> bool:
    if not expected or not isinstance(provided, str) or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def record_service_status(status: str, path: Path = EVENTS_PATH) -> None:
    normalized = str(status).strip().upper()
    if normalized not in DIAGNOSTIC_STATUSES:
        return
    with _STATUS_LOCK:
        _SERVICE_STATUS[str(Path(path).resolve())] = normalized


def _last_service_status(path: Path) -> str | None:
    with _STATUS_LOCK:
        return _SERVICE_STATUS.get(str(Path(path).resolve()))


def ingest_webhook(
    payload: Any,
    *,
    provided_token: str = "",
    expected_token: str | None = None,
    path: Path = EVENTS_PATH,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Authenticate, normalize and persist one diagnostic webhook event."""

    expected = configured_token() if expected_token is None else expected_token
    body_token = payload.get("token", "") if isinstance(payload, Mapping) else ""
    candidate = body_token or provided_token
    if not _authorized(candidate, expected):
        record_service_status("UNAUTHORIZED", path)
        return 401, {"status": "UNAUTHORIZED", "score_effect": 0}

    try:
        normalized = validate_payload(payload)
    except PayloadValidationError as exc:
        record_service_status("INVALID_PAYLOAD", path)
        return 400, {
            "status": "INVALID_PAYLOAD",
            "reason": str(exc),
            "score_effect": 0,
        }

    event_id = deterministic_event_id(normalized)
    record = {
        "received_at": _iso_utc(_utc_now(now)),
        **normalized,
        "event_id": event_id,
        "score_effect": 0,
    }
    try:
        stored = append_event(record, path)
    except OSError:
        record_service_status("NO_DATA", path)
        return 503, {"status": "NO_DATA", "score_effect": 0}
    record_service_status("OK", path)
    return 200, {
        "status": "OK",
        "event_id": event_id,
        "duplicate": not stored,
        "score_effect": 0,
    }


def _parse_received_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _parse_event_timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    # Pine timestamps are commonly milliseconds; conventional webhooks may use seconds.
    if timestamp >= 10_000_000_000:
        timestamp /= 1000.0
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def observation_snapshot(
    path: Path = EVENTS_PATH,
    *,
    now: datetime | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> dict[str, Any]:
    path = Path(path)
    event_count, latest_event = _event_summary(path)
    base = {
        "source": SOURCE,
        "symbol": NORMALIZED_SYMBOL,
        "api_status": "NO_DATA",
        "available": False,
        "last_signal": None,
        "freshness_sec": None,
        "event_count": event_count,
        "score_effect": 0,
        "read_only": True,
        "observation_only": True,
    }
    service_status = _last_service_status(path)
    if latest_event is None:
        if service_status in {"UNAUTHORIZED", "RATE_LIMITED", "INVALID_PAYLOAD"}:
            base["api_status"] = service_status
        return base

    last = dict(latest_event)
    last.pop("token", None)
    last["score_effect"] = 0
    observed_at = _parse_event_timestamp(last.get("event_timestamp"))
    if observed_at is None:
        observed_at = _parse_received_at(last.get("received_at"))
    if observed_at is None:
        return {**base, "api_status": "INVALID_PAYLOAD"}
    freshness = max(0.0, (_utc_now(now) - observed_at).total_seconds())
    stale = freshness > max(1, int(stale_seconds))
    data_status = "STALE" if stale else "OK"
    display_status = (
        service_status
        if service_status in {"UNAUTHORIZED", "RATE_LIMITED", "INVALID_PAYLOAD"}
        else data_status
    )
    return {
        **base,
        "api_status": display_status,
        "data_status": data_status,
        "last_ingest_status": service_status or data_status,
        "available": not stale,
        "last_signal": last,
        "freshness_sec": round(freshness, 3),
        "score_effect": 0,
    }
