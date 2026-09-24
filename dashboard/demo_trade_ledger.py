"""Read-only projection of MT5's append-only DEMO execution ledger.

This module never sends orders and never repairs the ledger. An invalid line is
reported, not silently interpreted as a zero-valued trade.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable
import json
import math


ALIASES = {"ORDER_REQUEST": "ORDER_REQUESTED", "POSITION_PARTIAL_CLOSE": "PARTIALLY_CLOSED", "POSITION_CLOSE": "POSITION_CLOSED"}
TERMINAL_EVENTS = {"POSITION_CLOSED", "RECONCILIATION_ERROR"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _ticket(value: Any) -> int | None:
    # MT5 identifiers are 64-bit integers; a float conversion can merge two
    # distinct tickets above 2**53 and corrupt deal/position provenance.
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lo = math.floor(index)
    hi = math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def _trade_key(row: dict[str, Any]) -> str:
    trade_id = row.get("trade_id")
    if trade_id:
        return str(trade_id)
    position = row.get("position_identifier", row.get("position_id"))
    return f"legacy-position:{position}" if position else ""


def _close_day(row: dict[str, Any]) -> str | None:
    value = row.get("close_time")
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    if (epoch := _number(value)) is not None and epoch > 0:
        return datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()
    return None


def _stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    closed = list(rows)
    pnl = [_number(row.get("net_pnl")) for row in closed]
    known = [value for value in pnl if value is not None]
    r_values = [value for row in closed if (value := _number(row.get("realized_r_net", row.get("realized_r")))) is not None]
    wins = [value for value in known if value > 0]
    losses = [value for value in known if value < 0]
    breakeven = sum(value == 0 for value in known)
    commissions = [value for row in closed if (value := _number(row.get("commission"))) is not None]
    swaps = [value for row in closed if (value := _number(row.get("swap"))) is not None]
    gross = [value for row in closed if (value := _number(row.get("gross_pnl"))) is not None]
    slip = [value for row in closed if (value := _number(row.get("entry_slippage_price"))) is not None]
    return {
        "trades": len(closed), "trades_closed": len(closed),
        "wins": len(wins), "losses": len(losses), "breakeven": breakeven,
        "win_rate": 100.0 * len(wins) / len(known) if known else None,
        "gross_pnl": sum(gross) if len(gross) == len(closed) else None,
        "net_pnl": sum(known) if len(known) == len(closed) else None,
        "average_realized_r": sum(r_values) / len(r_values) if r_values else None,
        "median_realized_r": median(r_values) if r_values else None,
        "expectancy_r": sum(r_values) / len(r_values) if r_values else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "average_win": sum(wins) / len(wins) if wins else None,
        "average_loss": sum(losses) / len(losses) if losses else None,
        "max_win": max(wins) if wins else None,
        "max_loss": min(losses) if losses else None,
        "commission_total": sum(commissions) if len(commissions) == len(closed) else None,
        "swap_total": sum(swaps) if len(swaps) == len(closed) else None,
        "entry_slippage_average": sum(slip) / len(slip) if slip else None,
        "entry_slippage_p95": _percentile(slip, .95) if len(slip) >= 20 else None,
    }


def project_events(events: Iterable[dict[str, Any]], *, day: str | None = None,
                   account_login: int | None = None) -> dict[str, Any]:
    """Idempotent projection: one final broker-reconciled row per trade."""
    seen_events: set[str] = set()
    closed: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    impossible_lifecycle: set[str] = set()
    states: dict[str, str] = {}
    orders: dict[str, set[int]] = defaultdict(set)
    deals: dict[str, set[int]] = defaultdict(set)
    count = 0
    for raw in events:
        if not isinstance(raw, dict):
            continue
        record_account = raw.get("account_login")
        if account_login is not None and record_account != account_login:
            continue
        event_id = raw.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in seen_events:
            continue
        seen_events.add(event_id)
        count += 1
        event = ALIASES.get(raw.get("event"), raw.get("event"))
        trade_id = _trade_key(raw)
        if not trade_id:
            continue
        order_ticket = _ticket(raw.get("order_ticket", raw.get("order_id")))
        deal_ticket = _ticket(raw.get("deal_ticket", raw.get("deal_id")))
        if order_ticket is not None:
            orders[trade_id].add(order_ticket)
        if deal_ticket is not None:
            deals[trade_id].add(deal_ticket)
        if event == "RECONCILIATION_ERROR":
            conflicts.add(trade_id)
        elif event == "ORDER_REJECTED":
            states[trade_id] = "REJECTED"
        elif event == "POSITION_OPEN":
            if states.get(trade_id) == "REJECTED":
                conflicts.add(trade_id)
                impossible_lifecycle.add(trade_id)
            states[trade_id] = "OPEN"
        if event != "POSITION_CLOSED":
            continue
        if raw.get("schema_version") != 2:
            # Legacy markers may have no deal/cost proof and are not a reliable
            # source for realized statistics.
            continue
        if states.get(trade_id) != "REJECTED" and trade_id not in impossible_lifecycle:
            conflicts.discard(trade_id)
        else:
            conflicts.add(trade_id)
        states[trade_id] = "CLOSED"
        gross = _number(raw.get("gross_pnl"))
        commission = _number(raw.get("commission"))
        swap = _number(raw.get("swap"))
        fees = _number(raw.get("fees"))
        net = _number(raw.get("net_pnl"))
        if (None in (gross, commission, swap, fees, net) or
                _ticket(raw.get("position_identifier")) is None or
                _ticket(raw.get("deal_ticket")) is None):
            conflicts.add(trade_id)
        if None not in (gross, commission, swap, fees, net) and abs(gross + commission + swap + fees - net) > .011:
            conflicts.add(trade_id)
        risk = _number(raw.get("initial_risk_account_currency"))
        realized = _number(raw.get("realized_r_net"))
        if risk is not None and risk > 0 and realized is not None and net is not None:
            if abs(net / risk - realized) > .001:
                conflicts.add(trade_id)
        if trade_id in closed:
            # Two distinct final records are ambiguous even if their figures match.
            conflicts.add(trade_id)
            continue
        row = dict(raw)
        row["status"] = "POSITION_CLOSED"
        row["trade_id"] = trade_id
        row["realized_r"] = row.get("realized_r_net", row.get("realized_r"))
        closed[trade_id] = row
    for trade_id, row in closed.items():
        row["order_tickets"] = sorted(orders[trade_id])
        row["deal_tickets"] = sorted(deals[trade_id])
    rows = [row for key, row in closed.items() if key not in conflicts]
    rows.sort(key=lambda row: (_number(row.get("close_time")) or
                               _number(row.get("timestamp")) or 0, row["trade_id"]), reverse=True)
    session = [row for row in rows if day is None or _close_day(row) == day]
    segments: dict[str, dict[str, dict[str, Any]]] = {}
    for field in ("setup", "direction", "class"):
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            buckets[str(row.get(field) or "UNKNOWN")].append(row)
        segments[field] = {key: _stats(value) for key, value in buckets.items()}
    return {
        "status": "CONFLICT" if conflicts else "OK", "event_count": count,
        "closed_trades": rows, "session_stats": _stats(session),
        "trade_stats": _stats(rows), "segments": segments,
        "conflicting_trade_ids": sorted(conflicts),
    }


def read_ledger(paths: Iterable[Path], *, day: str | None = None,
                account_login: int | None = None) -> dict[str, Any]:
    """Stream a single selected JSONL file; flag corrupt/partial records."""
    path = next((Path(path) for path in paths if Path(path).is_file()), None)
    if path is None:
        return {**project_events((), day=day, account_login=account_login), "status": "NO_DATA"}
    errors: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            def records():
                for number, line in enumerate(handle, 1):
                    if "\ufffd" in line:
                        errors.append({"line": number, "reason": "INVALID_UTF8"})
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("record is not an object")
                        yield row
                    except ValueError as exc:
                        errors.append({"line": number, "reason": type(exc).__name__})
            result = project_events(records(), day=day, account_login=account_login)
    except OSError as exc:
        return {"status": "READ_ERROR", "error": type(exc).__name__, "closed_trades": [], "session_stats": _stats(())}
    if errors:
        result["status"] = "CORRUPT_LEDGER"
        result["errors"] = errors[:20]
    result["path"] = str(path)
    return result
