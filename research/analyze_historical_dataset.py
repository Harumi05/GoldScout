"""Offline statistical analysis for GoldScout historical observations/outcomes.

The module is deliberately read-only with respect to the replay dataset. It joins
append-only records by ``event_id``, excludes terminal gaps/incomplete outcomes,
and writes derived reports without changing any live trading component.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import statistics
import tempfile
from typing import Callable, Iterable, Sequence


HORIZONS = ("15m", "1h", "4h")
PERIODS = ("TRAIN", "VALIDATION", "OOS")
SCORE_BUCKETS = (
    ("<50", None, 50.0),
    ("50-57", 50.0, 58.0),
    ("58-64", 58.0, 65.0),
    ("65-69", 65.0, 70.0),
    ("70-73", 70.0, 74.0),
    ("74-79", 74.0, 80.0),
    ("80-89", 80.0, 90.0),
    ("90+", 90.0, None),
)
OUTCOME_FIELDS = {
    horizon: (f"future_return_{horizon}", f"mfe_{horizon}", f"mae_{horizon}")
    for horizon in HORIZONS
}
FEATURE_FIELDS = ("momentum", "breakout", "pullback", "recovery")
NULL_STATES = {"", "NONE", "NEUTRAL", "INSUFFICIENT", "UNKNOWN", "FALSE", "0", "NULL"}


class AnalysisError(RuntimeError):
    """Raised when the input dataset cannot be analyzed safely."""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _timestamp_ms(value: object) -> int | None:
    if _is_number(value):
        number = float(value)
        return int(number * 1000) if abs(number) < 100_000_000_000 else int(number)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _atomic_write(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
    try:
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write(path, buffer.getvalue())


def _remove_stale_optional_output(path: Path) -> None:
    path = Path(path)
    if path.is_file():
        path.unlink()


def _load_observations(path: Path) -> tuple[list[dict], dict]:
    observations: dict[str, dict] = {}
    stats = {"rows_total": 0, "malformed": 0, "invalid": 0, "duplicate_event_ids": 0}
    try:
        stream = Path(path).open("r", encoding="utf-8-sig")
    except OSError as error:
        raise AnalysisError(f"cannot open observations: {path}: {error}") from error
    with stream:
        for line in stream:
            if not line.strip():
                continue
            stats["rows_total"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                stats["malformed"] += 1
                continue
            event_id = record.get("event_id") if isinstance(record, dict) else None
            timestamp = _timestamp_ms(record.get("timestamp")) if isinstance(record, dict) else None
            observed_at = _timestamp_ms(record.get("observed_at")) if isinstance(record, dict) else None
            if not isinstance(event_id, str) or not event_id or timestamp is None:
                stats["invalid"] += 1
                continue
            if event_id in observations:
                stats["duplicate_event_ids"] += 1
                if observations[event_id] != record:
                    raise AnalysisError(f"conflicting observation for event_id={event_id}")
                continue
            value = dict(record)
            # Indicators and decisions exist only after the source bar closes.
            # Legacy rows without observed_at retain their original timestamp.
            value["_timestamp_ms"] = observed_at if observed_at is not None else timestamp
            observations[event_id] = value
    ordered = sorted(observations.values(), key=lambda item: (item["_timestamp_ms"], item["event_id"]))
    return ordered, stats


def _merge_decision_enrichment(observations: list[dict], path: Path) -> dict:
    stats = {"available": False, "rows_total": 0, "matched": 0, "orphan": 0, "duplicate_event_ids": 0}
    path = Path(path)
    if not path.is_file():
        return stats
    stats["available"] = True
    records: dict[str, dict] = {}
    for record in _read_json_objects(path):
        stats["rows_total"] += 1
        event_id = record.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise AnalysisError(f"invalid decision enrichment event_id in {path}")
        if record.get("observer_only") is not True or record.get("score_effect") != 0:
            raise AnalysisError(f"decision enrichment is not observation-only: event_id={event_id}")
        if event_id in records:
            stats["duplicate_event_ids"] += 1
            if records[event_id] != record:
                raise AnalysisError(f"conflicting decision enrichment for event_id={event_id}")
            continue
        records[event_id] = record
    for observation in observations:
        enrichment = records.pop(observation["event_id"], None)
        if enrichment is None:
            continue
        if enrichment.get("timestamp") != observation.get("timestamp"):
            raise AnalysisError(f"decision enrichment timestamp mismatch for event_id={observation['event_id']}")
        for key, value in enrichment.items():
            if key not in {"event_id", "timestamp", "observed_at", "source", "observer_only", "score_effect"}:
                observation[key] = value
        stats["matched"] += 1
    stats["orphan"] = len(records)
    return stats


def _read_json_objects(path: Path) -> Iterable[dict]:
    try:
        stream = Path(path).open("r", encoding="utf-8-sig")
    except OSError as error:
        raise AnalysisError(f"cannot open JSONL: {path}: {error}") from error
    with stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise AnalysisError(f"invalid JSON at {path}:{line_number}: {error}") from error
            if not isinstance(record, dict):
                raise AnalysisError(f"non-object JSON at {path}:{line_number}")
            yield record


def _normalize_horizon(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace(" ", "")
    aliases = {"15m": "15m", "15min": "15m", "1h": "1h", "60m": "1h", "4h": "4h", "240m": "4h"}
    return aliases.get(text)


def _load_outcomes(path: Path) -> tuple[dict[str, dict[str, dict]], dict[tuple[str, str], str], dict]:
    valid: dict[str, dict[str, dict]] = defaultdict(dict)
    excluded: dict[tuple[str, str], str] = {}
    stats = {
        "rows_total": 0,
        "malformed": 0,
        "invalid": 0,
        "duplicate_terminal_rows": 0,
        "conflicting_terminal_rows": 0,
    }
    try:
        stream = Path(path).open("r", encoding="utf-8-sig")
    except OSError as error:
        raise AnalysisError(f"cannot open outcomes: {path}: {error}") from error
    with stream:
        for line in stream:
            if not line.strip():
                continue
            stats["rows_total"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                stats["malformed"] += 1
                continue
            if not isinstance(record, dict) or not isinstance(record.get("event_id"), str):
                stats["invalid"] += 1
                continue
            event_id = record["event_id"]
            horizon = _normalize_horizon(record.get("completed_horizon") or record.get("horizon"))
            if horizon is None:
                stats["invalid"] += 1
                continue
            key = (event_id, horizon)
            status = str(record.get("status", "COMPLETED")).strip().upper()
            if status == "UNRESOLVABLE_GAP":
                if horizon in valid.get(event_id, {}):
                    valid[event_id].pop(horizon, None)
                    stats["conflicting_terminal_rows"] += 1
                    excluded[key] = "CONFLICTING_TERMINAL_STATE"
                else:
                    excluded[key] = "UNRESOLVABLE_GAP"
                continue
            fields = OUTCOME_FIELDS[horizon]
            values = [record.get(field) for field in fields]
            if all(_is_number(value) for value in values):
                normalized = {
                    "future_return": float(values[0]),
                    "mfe": float(values[1]),
                    "mae": float(values[2]),
                    "evaluated_at": _timestamp_ms(record.get("evaluated_at")),
                    "entry_spread": float(record["entry_spread"]) if _is_number(record.get("entry_spread")) else None,
                    "max_spread": (
                        float(record[f"max_spread_{horizon}"])
                        if _is_number(record.get(f"max_spread_{horizon}"))
                        else None
                    ),
                }
                previous = valid[event_id].get(horizon)
                if previous is not None:
                    if previous == normalized:
                        stats["duplicate_terminal_rows"] += 1
                    else:
                        valid[event_id].pop(horizon, None)
                        excluded[key] = "CONFLICTING_TERMINAL_STATE"
                        stats["conflicting_terminal_rows"] += 1
                elif excluded.get(key) in {"UNRESOLVABLE_GAP", "CONFLICTING_TERMINAL_STATE"}:
                    excluded[key] = "CONFLICTING_TERMINAL_STATE"
                    stats["conflicting_terminal_rows"] += 1
                else:
                    valid[event_id][horizon] = normalized
                    excluded.pop(key, None)
            elif all(value is None for value in values):
                excluded.setdefault(key, "NULL")
            else:
                excluded.setdefault(key, "INCOMPLETE")
    return dict(valid), excluded, stats


def temporal_split(observations: Sequence[dict]) -> tuple[dict[str, str], dict[str, int]]:
    ordered = sorted(observations, key=lambda item: (item["_timestamp_ms"], item["event_id"]))
    train_end = int(len(ordered) * 0.60)
    validation_end = train_end + int(len(ordered) * 0.20)
    train_cutoff = ordered[train_end]["_timestamp_ms"] if train_end < len(ordered) else math.inf
    validation_cutoff = ordered[validation_end]["_timestamp_ms"] if validation_end < len(ordered) else math.inf
    assignment: dict[str, str] = {}
    for observation in ordered:
        timestamp = observation["_timestamp_ms"]
        period = "TRAIN" if timestamp < train_cutoff else "VALIDATION" if timestamp < validation_cutoff else "OOS"
        assignment[observation["event_id"]] = period
    counts = Counter(assignment.values())
    return assignment, {period: counts.get(period, 0) for period in PERIODS}


def _direction(observation: dict) -> str | None:
    value = str(observation.get("direction") or observation.get("armed_direction") or "").strip().upper()
    return value if value in {"LONG", "SHORT", "NEUTRAL"} else None


def _bias(observation: dict) -> tuple[str | None, str | None]:
    direction = _direction(observation)
    if direction in {"LONG", "SHORT"}:
        return direction, "direction"
    structure = str(observation.get("structure", "")).strip().upper()
    if structure == "BULLISH":
        return "LONG", "structure"
    if structure == "BEARISH":
        return "SHORT", "structure"
    return None, None


def _directed_values(outcome: dict, direction: str | None) -> tuple[float, float, float]:
    future_return = outcome["future_return"]
    mfe = outcome["mfe"]
    mae = outcome["mae"]
    if direction == "SHORT":
        return -future_return, -mae, -mfe
    return future_return, mfe, mae


def summarize_values(values: Sequence[tuple[float, float, float]]) -> dict:
    if not values:
        return {
            "cases": 0,
            "mean_future_return": None,
            "median_future_return": None,
            "win_rate_pct": None,
            "loss_rate_pct": None,
            "mean_mfe": None,
            "mean_mae": None,
            "median_mfe": None,
            "median_mae": None,
        }
    returns = [item[0] for item in values]
    mfe = [item[1] for item in values]
    mae = [item[2] for item in values]
    count = len(values)
    return {
        "cases": count,
        "mean_future_return": statistics.fmean(returns),
        "median_future_return": statistics.median(returns),
        "win_rate_pct": sum(value > 0.0 for value in returns) * 100.0 / count,
        "loss_rate_pct": sum(value < 0.0 for value in returns) * 100.0 / count,
        "mean_mfe": statistics.fmean(mfe),
        "mean_mae": statistics.fmean(mae),
        "median_mfe": statistics.median(mfe),
        "median_mae": statistics.median(mae),
    }


def _metrics(
    observations: Iterable[dict], outcomes: dict[str, dict[str, dict]], horizon: str, direction: str | None = None
) -> dict:
    values: list[tuple[float, float, float]] = []
    for observation in observations:
        outcome = outcomes.get(observation["event_id"], {}).get(horizon)
        if outcome is None:
            continue
        actual_direction = _direction(observation)
        if direction is not None and actual_direction != direction:
            continue
        values.append(_directed_values(outcome, actual_direction if direction is not None else None))
    return summarize_values(values)


def _period_observations(observations: Sequence[dict], assignments: dict[str, str], period: str) -> list[dict]:
    if period == "ALL":
        return list(observations)
    return [item for item in observations if assignments[item["event_id"]] == period]


def _horizon_analysis(observations: Sequence[dict], outcomes: dict, assignments: dict[str, str]) -> dict:
    result: dict = {}
    directions = sorted({value for item in observations if (value := _direction(item)) is not None})
    for period in ("ALL",) + PERIODS:
        selected = _period_observations(observations, assignments, period)
        result[period] = {}
        for horizon in HORIZONS:
            metrics = {"ALL_LONG_CONVENTION": _metrics(selected, outcomes, horizon)}
            for direction in directions:
                metrics[direction] = _metrics(selected, outcomes, horizon, direction)
            result[period][horizon] = metrics
    return result


def score_bucket(value: float) -> str:
    for label, lower, upper in SCORE_BUCKETS:
        if (lower is None or value >= lower) and (upper is None or value < upper):
            return label
    raise AssertionError("unreachable score bucket")


def _score_entries(observation: dict) -> list[tuple[str, float]]:
    entries: list[tuple[str, float]] = []
    if _is_number(observation.get("long_score")):
        entries.append(("LONG", float(observation["long_score"])))
    if _is_number(observation.get("short_score")):
        entries.append(("SHORT", float(observation["short_score"])))
    if entries:
        return entries
    if _is_number(observation.get("score")) and _direction(observation) in {"LONG", "SHORT"}:
        return [(_direction(observation), float(observation["score"]))]  # type: ignore[arg-type]
    return []


def _score_rows(observations: Sequence[dict], outcomes: dict, assignments: dict[str, str]) -> list[dict]:
    grouped: dict[tuple[str, str, str, str, str, str], list[tuple[float, float, float]]] = defaultdict(list)
    for observation in observations:
        reproduction = observation.get("score_reproduction")
        reproduction_status = (
            str(reproduction.get("status", "UNSPECIFIED"))
            if isinstance(reproduction, dict)
            else "SOURCE_OBSERVATION"
        )
        for direction, score in _score_entries(observation):
            for horizon in HORIZONS:
                outcome = outcomes.get(observation["event_id"], {}).get(horizon)
                if outcome is None:
                    continue
                values = _directed_values(outcome, direction)
                for period in ("ALL", assignments[observation["event_id"]]):
                    grouped[
                        (
                            period,
                            str(observation.get("timeframe", "UNKNOWN")),
                            score_bucket(score),
                            direction,
                            horizon,
                            reproduction_status,
                        )
                    ].append(values)
    rows: list[dict] = []
    order = {label: index for index, (label, _, _) in enumerate(SCORE_BUCKETS)}
    for key, values in sorted(
        grouped.items(),
        key=lambda item: (item[0][0], item[0][1], order[item[0][2]], item[0][3], item[0][4], item[0][5]),
    ):
        period, observation_timeframe, bucket, direction, horizon, reproduction_status = key
        rows.append(
            {
                "period": period,
                "observation_timeframe": observation_timeframe,
                "bucket": bucket,
                "direction": direction,
                "horizon": horizon,
                "score_reproduction_status": reproduction_status,
                **summarize_values(values),
            }
        )
    return rows


def _active(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value if value is not None else "").strip().upper() not in NULL_STATES


def _group_analysis(
    observations: Sequence[dict],
    outcomes: dict,
    assignments: dict[str, str],
    group: Callable[[dict], str | None],
    min_group_size: int,
) -> dict:
    result: dict = {}
    for period in ("ALL",) + PERIODS:
        selected = _period_observations(observations, assignments, period)
        groups: dict[str, list[dict]] = defaultdict(list)
        for observation in selected:
            label = group(observation)
            if label is not None:
                groups[label].append(observation)
        result[period] = {}
        for label, members in sorted(groups.items()):
            if len(members) < min_group_size:
                continue
            result[period][label] = {
                "observations": len(members),
                "horizons": {horizon: _metrics(members, outcomes, horizon) for horizon in HORIZONS},
            }
    return result


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires values")
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _spread_analysis(observations: Sequence[dict], outcomes: dict, assignments: dict[str, str], min_group_size: int) -> dict:
    train = sorted(
        float(item["spread"])
        for item in observations
        if assignments[item["event_id"]] == "TRAIN" and _is_number(item.get("spread"))
    )
    if not train:
        return {"available": False, "reason": "spread unavailable in TRAIN"}
    thresholds = [_percentile(train, fraction) for fraction in (0.25, 0.50, 0.75)]

    def quartile(item: dict) -> str | None:
        if not _is_number(item.get("spread")):
            return None
        value = float(item["spread"])
        if value <= thresholds[0]:
            return "Q1"
        if value <= thresholds[1]:
            return "Q2"
        if value <= thresholds[2]:
            return "Q3"
        return "Q4"

    return {
        "available": True,
        "thresholds_from_train": {"q25": thresholds[0], "q50": thresholds[1], "q75": thresholds[2]},
        "groups": _group_analysis(observations, outcomes, assignments, quartile, min_group_size),
    }


def _source_hour(observation: dict) -> str:
    return f"{datetime.fromtimestamp(observation['_timestamp_ms'] / 1000, timezone.utc).hour:02d}:00"


def _missed_opportunities(
    observations: Sequence[dict], outcomes: dict, assignments: dict[str, str], threshold_atr: float
) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    eligible = 0
    no_trade_count = 0
    for observation in observations:
        if str(observation.get("decision", "")).strip().upper() != "NO_TRADE":
            continue
        no_trade_count += 1
        outcome = outcomes.get(observation["event_id"], {}).get("1h")
        if outcome is None or not _is_number(observation.get("atr")) or not _is_number(observation.get("close")):
            continue
        atr = float(observation["atr"])
        close = float(observation["close"])
        if atr <= 0.0 or close <= 0.0:
            continue
        eligible += 1
        move = outcome["future_return"] * close
        atr_multiple = abs(move) / atr
        if atr_multiple < threshold_atr:
            continue
        rows.append(
            {
                "event_id": observation["event_id"],
                "period": assignments[observation["event_id"]],
                "timestamp": observation.get("timestamp"),
                "timeframe": observation.get("timeframe"),
                "opportunity_direction": "LONG" if move > 0 else "SHORT",
                "future_return_1h": outcome["future_return"],
                "price_move_1h": move,
                "atr": atr,
                "atr_multiple": atr_multiple,
                "structure": observation.get("structure"),
                "momentum": observation.get("momentum"),
                "breakout": observation.get("breakout"),
                "source_clock_hour": _source_hour(observation),
                "session": observation.get("session"),
                "spread": observation.get("spread"),
                "score_reproduction_status": observation.get("score_reproduction", {}).get("status")
                if isinstance(observation.get("score_reproduction"), dict)
                else None,
            }
        )
    by_direction = Counter(row["opportunity_direction"] for row in rows)
    return rows, {
        "available": no_trade_count > 0,
        "no_trade_observations": no_trade_count,
        "eligible_with_atr_and_1h_outcome": eligible,
        "cases": len(rows),
        "percentage_of_eligible": len(rows) * 100.0 / eligible if eligible else None,
        "by_direction": dict(by_direction),
        "threshold_atr": threshold_atr,
        "caveat": (
            "diagnostic movement only; execution, spread and slippage are not modeled as trade profitability; "
            "PARTIAL_EXACT_CLOSED_BAR_CORE decisions omit patterns, historical news and intrabar state"
        ),
    }


def _stale_bias(
    observations: Sequence[dict], outcomes: dict, assignments: dict[str, str], threshold_atr: float
) -> tuple[list[dict], dict]:
    by_timeframe: dict[str, list[dict]] = defaultdict(list)
    has_bias = False
    for observation in observations:
        by_timeframe[str(observation.get("timeframe", "UNKNOWN"))].append(observation)
        has_bias = has_bias or _bias(observation)[0] is not None
    rows: list[dict] = []
    for timeframe, sequence in by_timeframe.items():
        sequence.sort(key=lambda item: (item["_timestamp_ms"], item["event_id"]))
        start = 0
        while start < len(sequence):
            bias, source = _bias(sequence[start])
            if bias is None:
                start += 1
                continue
            end = start + 1
            while end < len(sequence) and _bias(sequence[end])[0] == bias:
                end += 1
            if end >= len(sequence):
                break
            trigger_index = None
            for index in range(start, end):
                observation = sequence[index]
                outcome = outcomes.get(observation["event_id"], {}).get("1h")
                if outcome is None or not _is_number(observation.get("atr")) or not _is_number(observation.get("close")):
                    continue
                atr = float(observation["atr"])
                close = float(observation["close"])
                if atr <= 0.0 or close <= 0.0:
                    continue
                directed_return, _, _ = _directed_values(outcome, bias)
                if directed_return * close <= -threshold_atr * atr:
                    trigger_index = index
                    break
            if trigger_index is not None:
                trigger = sequence[trigger_index]
                change = sequence[end]
                start_price = float(trigger["close"])
                closes = [float(item["close"]) for item in sequence[trigger_index : end + 1] if _is_number(item.get("close"))]
                adverse_move = start_price - min(closes) if bias == "LONG" else max(closes) - start_price
                atr = float(trigger["atr"])
                rows.append(
                    {
                        "event_id": trigger["event_id"],
                        "period": assignments[trigger["event_id"]],
                        "timeframe": timeframe,
                        "bias": bias,
                        "bias_source": source,
                        "started_at": trigger.get("timestamp"),
                        "changed_at": change.get("timestamp"),
                        "time_to_change_minutes": (change["_timestamp_ms"] - trigger["_timestamp_ms"]) / 60_000,
                        "adverse_price_move_before_change": adverse_move,
                        "adverse_return_before_change": adverse_move / start_price if start_price else None,
                        "adverse_atr": adverse_move / atr,
                        "structure": trigger.get("structure"),
                        "momentum": trigger.get("momentum"),
                        "breakout": trigger.get("breakout"),
                    }
                )
            start = end
    times = [float(row["time_to_change_minutes"]) for row in rows]
    adverse_atr = [float(row["adverse_atr"]) for row in rows]
    return rows, {
        "available": has_bias,
        "cases": len(rows),
        "mean_time_to_change_minutes": statistics.fmean(times) if times else None,
        "mean_adverse_atr": statistics.fmean(adverse_atr) if adverse_atr else None,
        "by_bias": dict(Counter(row["bias"] for row in rows)),
        "bias_sources": dict(Counter(row["bias_source"] for row in rows)),
        "threshold_atr": threshold_atr,
    }


def _field_availability(observations: Sequence[dict]) -> dict:
    fields = (
        "long_score",
        "short_score",
        "score",
        "direction",
        "decision",
        "decision_reason",
        "session",
        "atr",
        "spread",
    )
    availability = {field: sum(item.get(field) is not None for item in observations) for field in fields}
    availability["direction"] = sum(_direction(item) is not None for item in observations)
    return availability


def _feature_analyses(observations: Sequence[dict], outcomes: dict, assignments: dict, min_group_size: int) -> dict:
    singles = {
        field: _group_analysis(
            observations,
            outcomes,
            assignments,
            lambda item, name=field: "ACTIVE" if _active(item.get(name)) else "INACTIVE",
            min_group_size,
        )
        for field in FEATURE_FIELDS
    }

    def structure_momentum(item: dict) -> str:
        structure = str(item.get("structure", "")).upper()
        momentum = str(item.get("momentum", "")).upper()
        aligned = (structure == "BULLISH" and momentum == "LONG") or (structure == "BEARISH" and momentum == "SHORT")
        return "ALIGNED" if aligned else "NOT_ALIGNED"

    def structure_breakout(item: dict) -> str:
        structure = str(item.get("structure", "")).upper()
        breakout = str(item.get("breakout", "")).upper()
        aligned = (structure == "BULLISH" and breakout == "LONG") or (structure == "BEARISH" and breakout == "SHORT")
        return "ALIGNED" if aligned else "NOT_ALIGNED"

    def pullback_recovery(item: dict) -> str:
        pullback = str(item.get("pullback", "")).upper()
        recovery = str(item.get("recovery", "")).upper()
        return "SAME_DIRECTION" if pullback in {"LONG", "SHORT"} and pullback == recovery else "OTHER"

    pairs = {
        "structure_momentum": _group_analysis(observations, outcomes, assignments, structure_momentum, min_group_size),
        "structure_breakout": _group_analysis(observations, outcomes, assignments, structure_breakout, min_group_size),
        "pullback_recovery": _group_analysis(observations, outcomes, assignments, pullback_recovery, min_group_size),
    }
    return {"single_features": singles, "two_feature_combinations": pairs}


def _nested_number(observation: dict, path: Sequence[str]) -> float | None:
    value: object = observation
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return float(value) if _is_number(value) else None


def _regime_analysis(observations: Sequence[dict], outcomes: dict, assignments: dict, min_group_size: int) -> dict:
    available = [
        item
        for item in observations
        if isinstance(item.get("h1_regime"), dict) and item["h1_regime"].get("available") is True
    ]
    if not available:
        return {"available": False, "reason": "h1_regime enrichment unavailable"}
    behavior = _group_analysis(
        available,
        outcomes,
        assignments,
        lambda item: str(item["h1_regime"].get("behavior_shift", "UNKNOWN")),
        min_group_size,
    )
    numeric_features = {
        "h1_30_slope_atr_per_bar": ("h1_regime", "window_30", "slope_atr_per_bar"),
        "h1_10_slope_atr_per_bar": ("h1_regime", "window_10", "slope_atr_per_bar"),
        "h1_3_slope_atr_per_bar": ("h1_regime", "window_3", "slope_atr_per_bar"),
        "h1_recent_acceleration": ("h1_regime", "recent_acceleration"),
    }
    features: dict = {}
    for name, path in numeric_features.items():
        train_values = sorted(
            value
            for item in available
            if assignments[item["event_id"]] == "TRAIN"
            and (value := _nested_number(item, path)) is not None
        )
        if not train_values:
            features[name] = {"available": False}
            continue
        thresholds = [_percentile(train_values, fraction) for fraction in (0.25, 0.50, 0.75)]

        def quartile(item: dict, nested_path=path, cuts=thresholds) -> str | None:
            value = _nested_number(item, nested_path)
            if value is None:
                return None
            if value <= cuts[0]:
                return "Q1"
            if value <= cuts[1]:
                return "Q2"
            if value <= cuts[2]:
                return "Q3"
            return "Q4"

        features[name] = {
            "available": True,
            "thresholds_from_train": {"q25": thresholds[0], "q50": thresholds[1], "q75": thresholds[2]},
            "groups": _group_analysis(available, outcomes, assignments, quartile, min_group_size),
        }
    return {"available": True, "enriched_observations": len(available), "behavior_shift": behavior, "numeric_features": features}


def _consistent_sign(values: Sequence[float | None]) -> bool:
    usable = [value for value in values if value is not None]
    return len(usable) == len(values) and (all(value > 0 for value in usable) or all(value < 0 for value in usable))


def _conclusions(summary: dict, min_group_size: int) -> dict:
    solid: list[str] = []
    weak: list[str] = []
    insufficient: list[str] = []
    hypotheses: list[str] = []
    for horizon in HORIZONS:
        metrics = [summary["horizon_results"][period][horizon]["ALL_LONG_CONVENTION"] for period in PERIODS]
        means = [value["mean_future_return"] for value in metrics]
        enough = all(value["cases"] >= min_group_size for value in metrics)
        text = f"El retorno {horizon} en convención LONG mantiene el mismo signo en TRAIN/VALIDATION/OOS."
        if enough and _consistent_sign(means):
            solid.append(text)
        else:
            weak.append(f"El retorno {horizon} no muestra un signo estable en las tres particiones.")
    availability = summary["field_availability"]
    if availability["direction"] == 0:
        insufficient.append("No existe direction explícita; no se atribuyen resultados a setups LONG/SHORT.")
    if availability["long_score"] + availability["short_score"] + availability["score"] == 0:
        insufficient.append("No existen scores históricos; by_score.csv no se genera.")
    if availability["decision"] == 0:
        insufficient.append("No existe decision/NO_TRADE; missed_opportunities.csv no se genera.")
    if availability["session"] == 0:
        insufficient.append("No existe session; by_session.csv no se genera y la hora conserva el reloj fuente MT5.")
    reproduction = summary.get("decision_enrichment", {}).get("score_reproduction_statuses", {})
    if reproduction.get("PARTIAL_EXACT_CLOSED_BAR_CORE", 0):
        insufficient.append(
            "Los scores son replay parcial del núcleo cerrado; patrones, noticias e intrabar no están reconstruidos y no deben interpretarse como paridad total del EA."
        )
    hypotheses.extend(
        [
            "Validar en una fase posterior si las diferencias por estructura sobreviven a costes y a una definición explícita de dirección.",
            "Contrastar cuartiles de spread fuera de muestra antes de considerar cualquier cambio operativo.",
            "Usar los episodios de stale bias solo como diagnóstico hasta disponer de score/decision históricos.",
        ]
    )
    return {"solid": solid, "weak": weak, "insufficient": insufficient, "hypotheses": hypotheses}


def _markdown(summary: dict) -> str:
    general = summary["general"]
    quality = summary["outcome_quality"]
    lines = [
        "# GoldScout — análisis histórico fase 1",
        "",
        "> Análisis offline. No representa PnL ejecutable ni modifica el EA, scoring o riesgo.",
        "",
        "## Cobertura",
        "",
        f"- Observaciones: {general['observations_total']:,}",
        f"- Outcomes válidos (event_id+horizonte): {quality['valid_event_horizons']:,}",
        f"- Outcomes excluidos: {quality['excluded_event_horizons']:,}",
        f"- Rango: {general['first_timestamp']} → {general['last_timestamp']}",
        f"- TRAIN / VALIDATION / OOS: {general['split_counts']['TRAIN']:,} / {general['split_counts']['VALIDATION']:,} / {general['split_counts']['OOS']:,}",
        "",
        "## Retornos por horizonte",
        "",
        "Los retornos siguientes conservan la convención LONG del dataset; no se infiere dirección de trade.",
        "",
        "| Periodo | Horizonte | Casos | Media | Mediana | Win rate | MFE media | MAE media |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for period in PERIODS:
        for horizon in HORIZONS:
            metrics = summary["horizon_results"][period][horizon]["ALL_LONG_CONVENTION"]
            lines.append(
                f"| {period} | {horizon} | {metrics['cases']:,} | {_fmt(metrics['mean_future_return'])} | "
                f"{_fmt(metrics['median_future_return'])} | {_fmt(metrics['win_rate_pct'], 2)}% | "
                f"{_fmt(metrics['mean_mfe'])} | {_fmt(metrics['mean_mae'])} |"
            )
    lines.extend(
        [
            "",
            "## Estructura y eventos",
            "",
            "| Periodo | Estructura | Casos 1h | Retorno medio 1h | Win rate 1h |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for period in PERIODS:
        for structure, payload in summary["structure_analysis"][period].items():
            metrics = payload["horizons"]["1h"]
            lines.append(
                f"| {period} | {structure} | {metrics['cases']:,} | {_fmt(metrics['mean_future_return'])} | "
                f"{_fmt(metrics['win_rate_pct'], 2)}% |"
            )
    distributions = general["distributions"]
    lines.extend(
        [
            "",
            "Eventos observados: "
            + "; ".join(
                f"{field} activo={sum(count for value, count in distributions[field].items() if value.upper() not in NULL_STATES):,}"
                for field in FEATURE_FIELDS
            )
            + ".",
            "",
            "## Spread y stale bias",
            "",
        ]
    )
    spread = summary["spread_analysis"]
    if spread.get("available"):
        thresholds = spread["thresholds_from_train"]
        lines.append(
            "Los cortes de spread se fijaron solo con TRAIN: "
            f"Q25={_fmt(thresholds['q25'], 4)}, Q50={_fmt(thresholds['q50'], 4)}, Q75={_fmt(thresholds['q75'], 4)}."
        )
    stale = summary["stale_bias"]
    if stale.get("available"):
        lines.append(
            f"Se detectaron {stale['cases']:,} episodios diagnósticos de stale bias; "
            f"tiempo medio al cambio={_fmt(stale['mean_time_to_change_minutes'], 2)} min y "
            f"movimiento adverso medio={_fmt(stale['mean_adverse_atr'], 2)} ATR."
        )
    lines.append("")
    lines.extend(["", "## Conclusiones", ""])
    labels = (("A. Hallazgos sólidos", "solid"), ("B. Hallazgos débiles", "weak"), ("C. Datos insuficientes", "insufficient"), ("D. Posibles hipótesis para probar después", "hypotheses"))
    for heading, key in labels:
        lines.extend([f"### {heading}", ""])
        values = summary["conclusions"][key]
        lines.extend([f"- {value}" for value in values] or ["- Ninguno con la evidencia disponible."])
        lines.append("")
    lines.extend(
        [
            "## Limitaciones",
            "",
            "- El timestamp se agrupa por hora del reloj fuente MT5; no se convierte a una zona horaria inferida.",
            "- Future return/MFE/MAE son movimientos de mercado, no resultados netos de spread, slippage o comisión.",
            "- Solo se comparan variables individuales y tres combinaciones predefinidas de dos variables.",
            "- No se recomiendan cambios de pesos ni thresholds en esta fase.",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(value: object, decimals: int = 6) -> str:
    return "N/A" if not _is_number(value) else f"{float(value):.{decimals}f}"


def analyze_dataset(
    input_dir: Path,
    output_dir: Path,
    *,
    min_group_size: int = 30,
    missed_threshold_atr: float = 0.75,
) -> dict:
    if min_group_size <= 0:
        raise ValueError("min_group_size must be positive")
    if missed_threshold_atr <= 0.0:
        raise ValueError("missed_threshold_atr must be positive")
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    observations_path = input_dir / "historical_observations.jsonl"
    outcomes_path = input_dir / "historical_outcomes.jsonl"
    observations, observation_stats = _load_observations(observations_path)
    if not observations:
        raise AnalysisError("no valid historical observations")
    enrichment_stats = _merge_decision_enrichment(observations, input_dir / "historical_decisions.jsonl")
    outcomes, explicit_excluded, outcome_stats = _load_outcomes(outcomes_path)
    assignments, split_counts = temporal_split(observations)
    known_events = {item["event_id"] for item in observations}
    orphan_valid = sum(
        1 for event_id, values in outcomes.items() if event_id not in known_events for _ in values
    )
    valid_event_horizons = sum(
        1 for event_id, values in outcomes.items() if event_id in known_events for _ in values
    )
    explicit_reasons = Counter(
        reason for (event_id, _), reason in explicit_excluded.items() if event_id in known_events
    )
    expected = len(observations) * len(HORIZONS)
    explicit_count = sum(explicit_reasons.values())
    missing = max(0, expected - valid_event_horizons - explicit_count)
    exclusion_reasons = dict(explicit_reasons)
    if missing:
        exclusion_reasons["MISSING"] = missing
    field_availability = _field_availability(observations)
    reproduction_statuses = Counter(
        str(item.get("score_reproduction", {}).get("status", "UNAVAILABLE"))
        for item in observations
        if isinstance(item.get("score_reproduction"), dict)
    )
    distributions = {
        "direction": dict(Counter(_direction(item) or "UNKNOWN" for item in observations)),
        "timeframe": dict(Counter(str(item.get("timeframe", "UNKNOWN")) for item in observations)),
        "structure": dict(Counter(str(item.get("structure", "UNKNOWN")) for item in observations)),
        **{
            field: dict(Counter(str(item.get(field, "UNKNOWN")) for item in observations))
            for field in FEATURE_FIELDS
        },
    }
    horizon_results = _horizon_analysis(observations, outcomes, assignments)
    score_rows = _score_rows(observations, outcomes, assignments)
    session_available = field_availability["session"] > 0
    session_analysis = (
        _group_analysis(observations, outcomes, assignments, lambda item: str(item.get("session")) if item.get("session") else None, min_group_size)
        if session_available
        else {}
    )
    hour_analysis = _group_analysis(observations, outcomes, assignments, _source_hour, min_group_size)
    missed_rows, missed_summary = _missed_opportunities(observations, outcomes, assignments, missed_threshold_atr)
    stale_rows, stale_summary = _stale_bias(observations, outcomes, assignments, missed_threshold_atr)
    summary = {
        "schema_version": 1,
        "analysis_only": True,
        "inputs": {
            "observations": str(observations_path),
            "outcomes": str(outcomes_path),
            "decision_enrichment": str(input_dir / "historical_decisions.jsonl"),
        },
        "general": {
            "observations_total": len(observations),
            "first_timestamp": datetime.fromtimestamp(observations[0]["_timestamp_ms"] / 1000, timezone.utc).isoformat(),
            "last_timestamp": datetime.fromtimestamp(observations[-1]["_timestamp_ms"] / 1000, timezone.utc).isoformat(),
            "split_counts": split_counts,
            "distributions": distributions,
        },
        "observation_quality": observation_stats,
        "decision_enrichment": {**enrichment_stats, "score_reproduction_statuses": dict(reproduction_statuses)},
        "outcome_quality": {
            **outcome_stats,
            "expected_event_horizons": expected,
            "valid_event_horizons": valid_event_horizons,
            "excluded_event_horizons": expected - valid_event_horizons,
            "exclusion_reasons": exclusion_reasons,
            "orphan_valid_event_horizons": orphan_valid,
        },
        "field_availability": field_availability,
        "horizon_results": horizon_results,
        "structure_analysis": _group_analysis(
            observations, outcomes, assignments, lambda item: str(item.get("structure", "UNKNOWN")), min_group_size
        ),
        "feature_analysis": _feature_analyses(observations, outcomes, assignments, min_group_size),
        "hour_analysis_source_clock": hour_analysis,
        "session_analysis": session_analysis,
        "spread_analysis": _spread_analysis(observations, outcomes, assignments, min_group_size),
        "h1_regime_analysis": _regime_analysis(observations, outcomes, assignments, min_group_size),
        "missed_opportunities": missed_summary,
        "stale_bias": stale_summary,
        "score_analysis_available": bool(score_rows),
        "minimum_group_size": min_group_size,
    }
    summary["conclusions"] = _conclusions(summary, min_group_size)
    generated = ["summary.json", "summary.md"]
    skipped: dict[str, str] = {}
    if score_rows:
        _write_csv(output_dir / "by_score.csv", list(score_rows[0].keys()), score_rows)
        generated.append("by_score.csv")
    else:
        _remove_stale_optional_output(output_dir / "by_score.csv")
        skipped["by_score.csv"] = "score fields unavailable"
    if session_available:
        session_rows: list[dict] = []
        for period, groups in session_analysis.items():
            for session, payload in groups.items():
                for horizon, metrics in payload["horizons"].items():
                    session_rows.append({"period": period, "session": session, "horizon": horizon, **metrics})
        if session_rows:
            _write_csv(output_dir / "by_session.csv", list(session_rows[0].keys()), session_rows)
            generated.append("by_session.csv")
        else:
            _remove_stale_optional_output(output_dir / "by_session.csv")
            skipped["by_session.csv"] = f"no session group reaches minimum sample {min_group_size}"
    else:
        _remove_stale_optional_output(output_dir / "by_session.csv")
        skipped["by_session.csv"] = "session field unavailable"
    if missed_summary["available"]:
        _write_csv(output_dir / "missed_opportunities.csv", list(missed_rows[0].keys()) if missed_rows else ["event_id"], missed_rows)
        generated.append("missed_opportunities.csv")
    else:
        _remove_stale_optional_output(output_dir / "missed_opportunities.csv")
        skipped["missed_opportunities.csv"] = "decision=NO_TRADE unavailable"
    if stale_summary["available"]:
        _write_csv(output_dir / "stale_bias.csv", list(stale_rows[0].keys()) if stale_rows else ["event_id"], stale_rows)
        generated.append("stale_bias.csv")
    else:
        _remove_stale_optional_output(output_dir / "stale_bias.csv")
        skipped["stale_bias.csv"] = "direction/structure bias unavailable"
    summary["generated_files"] = generated
    summary["skipped_files"] = skipped
    _atomic_write(output_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _atomic_write(output_dir / "summary.md", _markdown(summary))
    return summary


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("--input-dir", type=Path, default=root / "output")
    parser.add_argument("--output-dir", type=Path, default=root / "analysis")
    parser.add_argument("--min-group-size", type=int, default=30)
    parser.add_argument("--missed-threshold-atr", type=float, default=0.75)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    try:
        summary = analyze_dataset(
            arguments.input_dir,
            arguments.output_dir,
            min_group_size=arguments.min_group_size,
            missed_threshold_atr=arguments.missed_threshold_atr,
        )
    except (AnalysisError, OSError, ValueError) as error:
        print(f"[ANALYSIS][ERROR] {error}")
        return 2
    print(f"[ANALYSIS] observations={summary['general']['observations_total']}")
    print(f"[ANALYSIS] valid_outcomes={summary['outcome_quality']['valid_event_horizons']}")
    print(f"[ANALYSIS] excluded_outcomes={summary['outcome_quality']['excluded_event_horizons']}")
    print(f"[ANALYSIS] output_dir={Path(arguments.output_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
