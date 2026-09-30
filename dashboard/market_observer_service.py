"""Read-only projection of the append-only MT5 market-observation dataset."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = ROOT / "data" / "market_observations.jsonl"
ALLOWED_TIMEFRAMES = frozenset({"M15", "H1", "H4"})
STALE_AFTER_SECONDS = 20 * 60

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}


def _empty_snapshot(status: str = "NO_DATA") -> dict:
    return {
        "source": "MT5",
        "observer_only": True,
        "score_effect": 0,
        "status": status,
        "observation_count": 0,
        "latest_by_timeframe": {"M15": None, "H1": None, "H4": None},
        "last_decision": None,
        "freshness_sec": None,
    }


def _valid_record(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("source") == "MT5"
        and value.get("observer_only") is True
        and value.get("score_effect") == 0
        and value.get("timeframe") in ALLOWED_TIMEFRAMES
        and isinstance(value.get("event_id"), str)
        and bool(value["event_id"])
    )


def _new_cache() -> dict:
    return {
        "offset": 0,
        "partial": b"",
        "observation_count": 0,
        "latest_by_timeframe": {"M15": None, "H1": None, "H4": None},
        "last_record": None,
        "closed_bars": {"M15": {}, "H1": {}, "H4": {}},
    }


def _record_order(record: dict) -> tuple[int, int]:
    captured = record.get("captured_at")
    timestamp = record.get("timestamp")
    return (
        int(captured) if isinstance(captured, (int, float)) else 0,
        int(timestamp) if isinstance(timestamp, (int, float)) else 0,
    )


def _update_cache(path: Path) -> dict:
    key = str(path.resolve())
    size = path.stat().st_size
    cache = _CACHE.setdefault(key, _new_cache())
    if size < cache["offset"]:
        cache = _CACHE[key] = _new_cache()

    if size == cache["offset"]:
        return cache

    with path.open("rb") as stream:
        stream.seek(cache["offset"])
        chunk = stream.read()
    cache["offset"] += len(chunk)
    complete = cache["partial"] + chunk
    lines = complete.split(b"\n")
    cache["partial"] = lines.pop() if complete and not complete.endswith(b"\n") else b""

    for raw in lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not _valid_record(record):
            continue
        cache["observation_count"] += 1
        timeframe = record["timeframe"]
        previous = cache["latest_by_timeframe"][timeframe]
        if previous is None or _record_order(record) >= _record_order(previous):
            cache["latest_by_timeframe"][timeframe] = record
        if cache["last_record"] is None or _record_order(record) >= _record_order(cache["last_record"]):
            cache["last_record"] = record

        if record.get("snapshot_type") == "BAR_CLOSE" and record.get("bar_closed") is True:
            ts = record.get("timestamp")
            if isinstance(ts, (int, float)):
                bars = cache["closed_bars"][timeframe]
                bars[int(ts)] = {
                    key: record.get(key)
                    for key in ("timestamp", "open", "high", "low", "close", "ema20", "ema200", "rsi", "adx")
                }
                if len(bars) > 600:
                    for old_ts in sorted(bars)[:-500]:
                        bars.pop(old_ts, None)
    return cache


def _choose_path(paths: Iterable[Path] | None) -> Path | None:
    candidates = [Path(path) for path in paths] if paths is not None else [DEFAULT_PATH]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def observation_snapshot(
    paths: Iterable[Path] | None = None,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_AFTER_SECONDS,
) -> dict:
    """Return an incremental dashboard projection without rewriting the JSONL."""
    path = _choose_path(paths)
    if path is None:
        return _empty_snapshot()

    try:
        with _LOCK:
            cache = _update_cache(path)
            count = int(cache["observation_count"])
            latest = dict(cache["latest_by_timeframe"])
            last = cache["last_record"]
    except OSError:
        return _empty_snapshot("PERSISTENCE_ERROR")

    if count == 0 or last is None:
        return _empty_snapshot()

    current = now or datetime.now(timezone.utc)
    captured = last.get("captured_at")
    freshness = None
    if isinstance(captured, (int, float)):
        freshness = max(0, int(current.timestamp() - float(captured)))
    status = "STALE" if freshness is not None and freshness > stale_after_seconds else "OK"
    decision = last.get("decision")
    reason = last.get("decision_reason")
    if decision and reason:
        decision = f"{decision} · {reason}"
    return {
        "source": "MT5",
        "observer_only": True,
        "score_effect": 0,
        "status": status,
        "observation_count": count,
        "latest_by_timeframe": latest,
        "last_decision": decision,
        "freshness_sec": freshness,
    }


def reset_cache() -> None:
    """Test helper; production code never truncates or deletes observations."""
    with _LOCK:
        _CACHE.clear()

def recent_bar_series(
    paths: Iterable[Path] | None = None,
    *,
    timeframe: str = "H1",
    limit: int = 120,
) -> dict:
    """Return recent confirmed OHLC bars for the read-only dashboard chart."""
    tf = str(timeframe or "H1").upper()
    if tf not in ALLOWED_TIMEFRAMES:
        tf = "H1"
    limit = max(20, min(int(limit or 120), 300))
    path = _choose_path(paths)
    if path is None:
        return {"source": "MT5", "timeframe": tf, "bars": [], "count": 0}

    try:
        with _LOCK:
            cache = _update_cache(path)
            by_time = dict(cache["closed_bars"].get(tf, {}))
    except OSError:
        return {"source": "MT5", "timeframe": tf, "bars": [], "count": 0}

    ordered = [by_time[key] for key in sorted(by_time)]
    # Keep only the newest contiguous closed-bar window. This prevents a stale
    # historical observation from drawing a long diagonal line across months
    # when the observer file contains gaps from restarts or older experiments.
    max_gap_seconds = {"M15": 90 * 60, "H1": 6 * 60 * 60, "H4": 24 * 60 * 60}[tf]
    contiguous: list[dict] = []
    for row in reversed(ordered):
        ts = row.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        if contiguous:
            newer_ts = contiguous[-1].get("timestamp")
            if isinstance(newer_ts, (int, float)) and int(newer_ts) - int(ts) > max_gap_seconds:
                break
        contiguous.append(row)
        if len(contiguous) >= limit:
            break
    rows = list(reversed(contiguous))
    return {
        "source": "MT5",
        "timeframe": tf,
        "bars": rows,
        "count": len(rows),
        "observer_only": True,
        "score_effect": 0,
    }
