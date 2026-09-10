"""Read-only eToro context for the GoldScout dashboard.

This module is deliberately disconnected from the EA and from GoldScout's
trading score.  It only calls documented eToro GET endpoints and always emits
``score_effect: 0``.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote
from uuid import uuid4

import requests


SOURCE = "etoro"
NORMALIZED_SYMBOL = "XAUUSD"
BASE_URL = "https://public-api.etoro.com"
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "external_signal_etoro.json"
READ_ONLY_ENDPOINTS = {
    "users_info": "/api/v1/user-info/people",
    "user_live_portfolio": "/api/v1/user-info/people/{username}/portfolio/live",
    "rankings": "/api/v2/portfolios/rankings",
    "user_gain": "/api/v2/portfolios/{username}/gain/daily",
    "user_copiers": "/api/v2/portfolios/{username}/copiers",
    "pi_data": "/api/v1/pi-data/copiers",
    "social_feed": "/api/v1/feeds/users/{user_id}",
    "instrument_search": "/api/v1/market-data/search",
}


@dataclass
class ExternalTrader:
    source: str
    trader_id: str
    username: str
    display_name: str
    risk_score: float | None
    copiers: int
    return_period: str
    return_pct: float | None
    relevant_to_gold: bool
    data_timestamp: str
    data_quality: int
    reliability_score: int
    global_accuracy: float | None = None
    london_accuracy: float | None = None
    new_york_accuracy: float | None = None
    regime_accuracy: dict[str, float] | None = None
    recency_score: float | None = None


@dataclass
class ExternalSignal:
    source: str
    trader_id: str
    instrument: str
    normalized_symbol: str
    direction: str
    action: str
    timestamp: str
    price: float | None
    confidence: int
    data_quality: int
    session_context: str
    raw_reference_id: str
    allocation_weight: float = 1.0
    read_only_copy_data: bool = False


class EtoroAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class EtoroUnavailable(EtoroAPIError):
    """A public trader/resource is private, missing, or unavailable."""


class EtoroInvalidResponse(EtoroAPIError):
    """The API returned a successful HTTP status but invalid JSON."""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any, default: int = 0) -> int:
    number = _number(value)
    return default if number is None else int(number)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value).astimezone(timezone.utc)
            return max(0.0, (parsed - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class EtoroClient:
    """Minimal eToro client that exposes GET only."""

    def __init__(
        self,
        api_key: str | None = None,
        user_key: str | None = None,
        *,
        timeout: float = 8.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 10.0,
        session: Any = None,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("ETORO_API_KEY", "")).strip()
        self.user_key = (user_key if user_key is not None else os.getenv("ETORO_USER_KEY", "")).strip()
        self.timeout = max(0.1, float(timeout))
        self.max_retries = max(0, min(4, int(max_retries)))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.max_backoff_seconds = max(0.0, float(max_backoff_seconds))
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.now = now

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.user_key)

    def get(self, path: str, params: Any = None) -> Any:
        if not self.has_credentials:
            raise EtoroAPIError("missing eToro credentials")
        if not path.startswith("/api/") or "://" in path:
            raise ValueError("eToro requests must use a relative official API path")
        if path.startswith("/api/v1/trading/") or path.startswith("/api/v1/copy-trading/"):
            raise ValueError("eToro trading and copy-trading endpoints are disabled")

        url = BASE_URL + path
        for attempt in range(self.max_retries + 1):
            headers = {
                "Accept": "application/json",
                "User-Agent": "GoldScout-external-observer/1.0",
                "x-api-key": self.api_key,
                "x-user-key": self.user_key,
                "x-request-id": str(uuid4()),
            }
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise EtoroAPIError(type(exc).__name__) from exc
                self.sleeper(min(self.max_backoff_seconds, self.backoff_seconds * (2**attempt)))
                continue

            if 200 <= response.status_code < 300:
                try:
                    payload = response.json()
                except (TypeError, ValueError) as exc:
                    raise EtoroInvalidResponse("invalid JSON response", response.status_code) from exc
                if not isinstance(payload, (dict, list)):
                    raise EtoroInvalidResponse("unexpected JSON response type", response.status_code)
                return payload

            if response.status_code in (403, 404):
                raise EtoroUnavailable("resource private or unavailable", response.status_code)
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt >= self.max_retries:
                    raise EtoroAPIError(f"HTTP {response.status_code}", response.status_code)
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"), self.now())
                delay = retry_after if retry_after is not None else self.backoff_seconds * (2**attempt)
                self.sleeper(min(self.max_backoff_seconds, delay))
                continue
            raise EtoroAPIError(f"HTTP {response.status_code}", response.status_code)

        raise EtoroAPIError("retry budget exhausted")


def normalize_gold_symbol(*identifiers: Any) -> str | None:
    """Map only explicit spot-gold identifiers; never infer related assets."""

    for identifier in identifiers:
        text = str(identifier or "").strip().upper()
        token = re.sub(r"[\s/_-]+", "", text)
        if token in {"GOLD", "XAUUSD"}:
            return NORMALIZED_SYMBOL
    return None


def normalize_gold_instruments(payloads: Iterable[Any]) -> dict[int, str]:
    instruments: dict[int, str] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        rows = payload.get("items")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized = normalize_gold_symbol(
                row.get("internalSymbolFull"),
                row.get("internalInstrumentDisplayName"),
                row.get("displayname"),
            )
            instrument_id = _integer(row.get("instrumentId"), -1)
            if normalized and instrument_id >= 0:
                instruments[instrument_id] = str(
                    row.get("internalSymbolFull") or row.get("displayname") or "GOLD"
                )
    return instruments


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> int:
    first_weekday = datetime(year, month, 1, tzinfo=timezone.utc).weekday()
    return 1 + (weekday - first_weekday) % 7 + (occurrence - 1) * 7


def _last_weekday(year: int, month: int, weekday: int) -> int:
    last_day = monthrange(year, month)[1]
    last_weekday = datetime(year, month, last_day, tzinfo=timezone.utc).weekday()
    return last_day - (last_weekday - weekday) % 7


def _london_dst_at(stamp: datetime) -> bool:
    year = stamp.year
    start = datetime(year, 3, _last_weekday(year, 3, 6), 1, tzinfo=timezone.utc)
    end = datetime(year, 10, _last_weekday(year, 10, 6), 1, tzinfo=timezone.utc)
    return start <= stamp < end


def _new_york_dst_at(stamp: datetime) -> bool:
    year = stamp.year
    start = datetime(year, 3, _nth_weekday(year, 3, 6, 2), 7, tzinfo=timezone.utc)
    end = datetime(year, 11, _nth_weekday(year, 11, 6, 1), 6, tzinfo=timezone.utc)
    return start <= stamp < end


def signal_session_context(timestamp: Any) -> str:
    stamp = parse_timestamp(timestamp)
    if stamp is None:
        return "UNKNOWN"
    if stamp.weekday() >= 5:
        return "CERRADO"
    utc_minute = stamp.hour * 60 + stamp.minute
    london_offset = 60 if _london_dst_at(stamp) else 0
    new_york_offset = -4 * 60 if _new_york_dst_at(stamp) else -5 * 60
    london_minute = (utc_minute + london_offset) % (24 * 60)
    ny_minute = (utc_minute + new_york_offset) % (24 * 60)
    london_active = 8 * 60 <= london_minute < 17 * 60
    ny_active = 8 * 60 + 20 <= ny_minute < 16 * 60
    if london_active and ny_active:
        return "LONDRES+NUEVA YORK/COMEX"
    if london_active:
        return "LONDRES"
    if ny_active:
        return "NUEVA YORK/COMEX"
    if 0 <= stamp.hour * 60 + stamp.minute < 9 * 60:
        return "ASIA"
    return "TRANSICION"


def normalize_portfolio_signals(
    trader_id: str,
    portfolio: Any,
    gold_instruments: Mapping[int, str],
    fetched_at: str,
) -> list[ExternalSignal]:
    if not isinstance(portfolio, dict):
        return []
    candidates: list[tuple[dict[str, Any], bool]] = []
    for position in portfolio.get("positions") or []:
        if isinstance(position, dict):
            candidates.append((position, False))
    for social_trade in portfolio.get("socialTrades") or []:
        if not isinstance(social_trade, dict):
            continue
        for position in social_trade.get("positions") or []:
            if isinstance(position, dict):
                candidates.append((position, True))

    signals: list[ExternalSignal] = []
    seen: set[str] = set()
    for position, copy_data in candidates:
        instrument_id = _integer(position.get("instrumentId"), -1)
        if instrument_id not in gold_instruments or not isinstance(position.get("isBuy"), bool):
            continue
        reference = str(position.get("positionId") or "").strip()
        if not reference:
            reference = f"{trader_id}:{instrument_id}:{position.get('openTimestamp', fetched_at)}"
        if reference in seen:
            continue
        seen.add(reference)
        timestamp = str(position.get("openTimestamp") or fetched_at)
        price = _number(position.get("openRate"))
        allocation = _number(position.get("investmentPct"))
        signals.append(
            ExternalSignal(
                source=SOURCE,
                trader_id=str(trader_id),
                instrument=gold_instruments[instrument_id],
                normalized_symbol=NORMALIZED_SYMBOL,
                direction="LONG" if position["isBuy"] else "SHORT",
                action="OPEN",
                timestamp=timestamp,
                price=price,
                confidence=0,
                data_quality=100,
                session_context=signal_session_context(timestamp),
                raw_reference_id=reference,
                allocation_weight=max(0.01, abs(allocation)) if allocation is not None else 1.0,
                read_only_copy_data=copy_data,
            )
        )
    return signals


def _gain_values(payload: Any) -> list[float]:
    if not isinstance(payload, dict) or not isinstance(payload.get("gains"), list):
        return []
    values = [_number(row.get("gain")) for row in payload["gains"] if isinstance(row, dict)]
    return [value for value in values if value is not None]


def reliability_score(ranking: Mapping[str, Any], gain_payload: Any = None) -> int:
    """Diagnostic reliability, intentionally dominated by risk/stability/activity."""

    risk = _number(ranking.get("riskScore"))
    risk_points = 0.0 if risk is None else 30.0 * _clamp(1.0 - abs(risk - 4.0) / 6.0, 0.0, 1.0)

    profitable_months = _number(ranking.get("profitableMonthsPct"))
    drawdown = _number(ranking.get("peakToValley"))
    stability_points = 0.0
    if profitable_months is not None:
        stability_points += 15.0 * _clamp(profitable_months / 100.0, 0.0, 1.0)
    if drawdown is not None:
        stability_points += 10.0 * (1.0 - _clamp(abs(drawdown) / 50.0, 0.0, 1.0))

    active_weeks = _number(ranking.get("activeWeeksPct"))
    activity_points = 0.0 if active_weeks is None else 12.0 * _clamp(active_weeks / 100.0, 0.0, 1.0)
    last_activity = parse_timestamp(ranking.get("lastActivity"))
    if last_activity is not None:
        days = max(0.0, (_utc_now() - last_activity).total_seconds() / 86400.0)
        activity_points += 8.0 * _clamp(1.0 - days / 30.0, 0.0, 1.0)

    copiers = max(0, _integer(ranking.get("copiers"), 0))
    adoption_points = 15.0 * _clamp(math.log10(1.0 + copiers) / 4.0, 0.0, 1.0)

    gains = _gain_values(gain_payload)
    return_points = 0.0
    if gains:
        positive_ratio = sum(1 for gain in gains if gain > 0) / len(gains)
        total_gain = sum(gains)
        return_points = 6.0 * positive_ratio + 4.0 * _clamp((total_gain + 10.0) / 40.0, 0.0, 1.0)
    elif _number(ranking.get("annualizedReturn")) is not None:
        annualized = _number(ranking.get("annualizedReturn")) or 0.0
        return_points = 5.0 + 5.0 * _clamp(annualized / 30.0, -1.0, 1.0)

    return int(round(_clamp(
        risk_points + stability_points + activity_points + adoption_points + return_points,
        0.0,
        100.0,
    )))


def normalize_trader(
    ranking: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None,
    portfolio_available: bool,
    gain_payload: Any,
    copiers_payload: Any,
    feed_payload: Any,
    signals: Sequence[ExternalSignal],
    fetched_at: str,
) -> ExternalTrader:
    username = str(ranking.get("username") or "").strip()
    trader_id = str(ranking.get("cid") or (profile or {}).get("gcid") or username)
    display_name = str(ranking.get("fullName") or username)
    copiers = _integer((copiers_payload or {}).get("copiers") if isinstance(copiers_payload, dict) else None, -1)
    if copiers < 0:
        copiers = max(0, _integer(ranking.get("copiers"), 0))
    return_pct = _number(ranking.get("annualizedReturn"))
    if return_pct is None and isinstance(gain_payload, dict):
        return_pct = _number(gain_payload.get("totalGain"))

    checks = [
        bool(username),
        _number(ranking.get("riskScore")) is not None,
        _number(ranking.get("copiers")) is not None,
        return_pct is not None,
        parse_timestamp(ranking.get("lastActivity")) is not None,
        profile is not None,
        portfolio_available,
        isinstance(gain_payload, dict),
        isinstance(copiers_payload, dict),
        isinstance(feed_payload, dict),
    ]
    data_quality = int(round(100.0 * sum(checks) / len(checks)))
    score = reliability_score(ranking, gain_payload)
    for signal in signals:
        signal.confidence = score
        signal.data_quality = min(signal.data_quality, data_quality)
    return ExternalTrader(
        source=SOURCE,
        trader_id=trader_id,
        username=username,
        display_name=display_name,
        risk_score=_number(ranking.get("riskScore")),
        copiers=copiers,
        return_period="OneYearAgo",
        return_pct=return_pct,
        relevant_to_gold=bool(signals),
        data_timestamp=fetched_at,
        data_quality=data_quality,
        reliability_score=score,
        recency_score=_number(ranking.get("activeWeeksPct")),
    )


def calculate_consensus(
    traders: Sequence[ExternalTrader],
    signals: Sequence[ExternalSignal],
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    trader_by_id = {trader.trader_id: trader for trader in traders if trader.relevant_to_gold}
    positions: dict[str, dict[str, float]] = {
        trader_id: {"LONG": 0.0, "SHORT": 0.0} for trader_id in trader_by_id
    }
    for signal in signals:
        if signal.normalized_symbol != NORMALIZED_SYMBOL or signal.trader_id not in positions:
            continue
        if signal.direction in ("LONG", "SHORT"):
            positions[signal.trader_id][signal.direction] += max(0.01, signal.allocation_weight)

    directional_weights = {"LONG": 0.0, "SHORT": 0.0, "NEUTRAL": 0.0}
    counts = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}
    qualities: list[int] = []
    for trader_id, sides in positions.items():
        if sides["LONG"] > sides["SHORT"]:
            direction = "LONG"
        elif sides["SHORT"] > sides["LONG"]:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"
        trader = trader_by_id[trader_id]
        weight = max(0.05, trader.reliability_score / 100.0)
        directional_weights[direction] += weight
        counts[direction] += 1
        qualities.append(trader.data_quality)

    total = sum(directional_weights.values())
    weights = {
        key: (directional_weights[key] / total if total > 0.0 else 0.0)
        for key in directional_weights
    }
    return {
        "source": SOURCE,
        "symbol": NORMALIZED_SYMBOL,
        "long_weight": round(weights["LONG"], 4),
        "short_weight": round(weights["SHORT"], 4),
        "neutral_weight": round(weights["NEUTRAL"], 4),
        "long_traders": counts["LONG"],
        "short_traders": counts["SHORT"],
        "neutral_traders": counts["NEUTRAL"],
        "valid_traders": sum(counts.values()),
        "freshness_sec": 0,
        "quality": int(round(sum(qualities) / len(qualities))) if qualities else 0,
        "updated_at": updated_at or _iso(_utc_now()),
        "score_effect": 0,
    }


def empty_snapshot(status: str, message: str, *, now: datetime | None = None) -> dict[str, Any]:
    stamp = now or _utc_now()
    consensus = calculate_consensus([], [], updated_at=_iso(stamp))
    return {
        **consensus,
        "updated_epoch": int(stamp.timestamp()),
        "stale_after_seconds": 900,
        "available": False,
        "fresh": True,
        "api_status": status,
        "data_quality": "LOW",
        "message": message,
        "read_only": True,
        "copy_trading_mode": "READ_ONLY_PORTFOLIO_DATA",
        "traders": [],
        "signals": [],
        "endpoint_status": {},
        "future_metrics": [
            "accuracy_global",
            "accuracy_london",
            "accuracy_new_york",
            "accuracy_by_regime",
            "recency",
        ],
        "future_sources": ["etoro", "zulutrade"],
        "future_max_score_effect": 5,
        "score_effect": 0,
    }


def mark_snapshot_freshness(snapshot: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    result = dict(snapshot)
    current = now or _utc_now()
    updated = parse_timestamp(result.get("updated_at"))
    freshness = max(0, int((current - updated).total_seconds())) if updated else 2**31 - 1
    stale_after = max(1, _integer(result.get("stale_after_seconds"), 900))
    result["freshness_sec"] = freshness
    result["fresh"] = freshness <= stale_after
    if not result["fresh"]:
        result["available"] = False
        result["api_status"] = "STALE"
        result["data_quality"] = "LOW"
        result["quality"] = min(25, _integer(result.get("quality"), 0))
    result["score_effect"] = 0
    return result


class EtoroExternalSignalService:
    def __init__(
        self,
        client: EtoroClient,
        *,
        max_traders: int = 5,
        stale_after_seconds: int = 900,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.client = client
        self.max_traders = max(1, min(10, int(max_traders)))
        self.stale_after_seconds = max(60, int(stale_after_seconds))
        self.now = now
        self.endpoint_status: dict[str, str] = {}

    def _get(self, name: str, path: str, params: Any = None) -> Any:
        try:
            payload = self.client.get(path, params=params)
            self.endpoint_status[name] = "OK"
            return payload
        except EtoroUnavailable:
            self.endpoint_status[name] = "UNAVAILABLE"
        except EtoroAPIError as exc:
            if exc.status_code == 429:
                self.endpoint_status[name] = "RATE_LIMITED"
            else:
                self.endpoint_status[name] = "ERROR"
        return None

    def _gold_instruments(self) -> dict[int, str]:
        payloads = []
        fields = "instrumentId,displayname,internalInstrumentDisplayName,internalSymbolFull"
        for symbol in ("GOLD", "XAUUSD"):
            payload = self._get(
                f"instrument_search_{symbol.lower()}",
                READ_ONLY_ENDPOINTS["instrument_search"],
                {
                    "fields": fields,
                    "internalSymbolFull": symbol,
                    "pageNumber": 1,
                    "pageSize": 20,
                },
            )
            if payload is not None:
                payloads.append(payload)
        return normalize_gold_instruments(payloads)

    def collect(self) -> dict[str, Any]:
        stamp = self.now()
        fetched_at = _iso(stamp)
        if not self.client.has_credentials:
            snapshot = empty_snapshot("NO_CREDENTIALS", "Faltan credenciales de eToro.", now=stamp)
            snapshot["stale_after_seconds"] = self.stale_after_seconds
            return snapshot

        instruments = self._gold_instruments()
        ranking_payload = self._get(
            "rankings",
            READ_ONLY_ENDPOINTS["rankings"],
            {"period": "OneYearAgo", "sort": "-copiers", "page": 1, "pageSize": self.max_traders},
        )
        self._get("pi_data", READ_ONLY_ENDPOINTS["pi_data"])
        ranking_rows = ranking_payload.get("results") if isinstance(ranking_payload, dict) else None
        if not isinstance(ranking_rows, list):
            snapshot = empty_snapshot("API_ERROR", "Rankings de eToro no disponibles.", now=stamp)
            snapshot["endpoint_status"] = self.endpoint_status
            snapshot["stale_after_seconds"] = self.stale_after_seconds
            return snapshot

        traders: list[ExternalTrader] = []
        signals: list[ExternalSignal] = []
        for row in ranking_rows[: self.max_traders]:
            if not isinstance(row, dict):
                continue
            username = str(row.get("username") or "").strip()
            if not username:
                continue
            safe_username = quote(username, safe="")
            profile_payload = self._get(
                f"profile:{username}", READ_ONLY_ENDPOINTS["users_info"], {"usernames": username}
            )
            profiles = profile_payload.get("users") if isinstance(profile_payload, dict) else None
            profile = profiles[0] if isinstance(profiles, list) and profiles and isinstance(profiles[0], dict) else None
            portfolio = self._get(
                f"portfolio:{username}",
                READ_ONLY_ENDPOINTS["user_live_portfolio"].format(username=safe_username),
            )
            gain = self._get(
                f"gain:{username}", READ_ONLY_ENDPOINTS["user_gain"].format(username=safe_username)
            )
            copiers = self._get(
                f"copiers:{username}", READ_ONLY_ENDPOINTS["user_copiers"].format(username=safe_username)
            )
            user_id = str(row.get("cid") or (profile or {}).get("gcid") or "").strip()
            feed = self._get(
                f"feed:{username}",
                READ_ONLY_ENDPOINTS["social_feed"].format(user_id=quote(user_id, safe="")),
                {"take": 10, "offset": 0},
            ) if user_id else None
            trader_id = str(row.get("cid") or (profile or {}).get("gcid") or username)
            trader_signals = normalize_portfolio_signals(
                trader_id, portfolio, instruments, fetched_at
            )
            trader = normalize_trader(
                row,
                profile=profile,
                portfolio_available=isinstance(portfolio, dict),
                gain_payload=gain,
                copiers_payload=copiers,
                feed_payload=feed,
                signals=trader_signals,
                fetched_at=fetched_at,
            )
            traders.append(trader)
            signals.extend(trader_signals)

        consensus = calculate_consensus(traders, signals, updated_at=fetched_at)
        ok = sum(1 for status in self.endpoint_status.values() if status == "OK")
        total = len(self.endpoint_status)
        status = "OK" if total and ok == total else ("PARTIAL" if ok else "API_ERROR")
        available = bool(consensus["valid_traders"])
        data_quality = "HIGH" if consensus["quality"] >= 75 else (
            "MEDIUM" if consensus["quality"] >= 50 else "LOW"
        )
        return {
            **consensus,
            "updated_epoch": int(stamp.timestamp()),
            "stale_after_seconds": self.stale_after_seconds,
            "available": available,
            "fresh": True,
            "api_status": status if instruments else "NO_GOLD_INSTRUMENT",
            "data_quality": data_quality,
            "message": "Consenso diagnóstico; no altera ni ejecuta operaciones.",
            "read_only": True,
            "copy_trading_mode": "READ_ONLY_PORTFOLIO_DATA",
            "traders": [asdict(trader) for trader in traders],
            "signals": [asdict(signal) for signal in signals],
            "endpoint_status": self.endpoint_status,
            "future_metrics": [
                "accuracy_global",
                "accuracy_london",
                "accuracy_new_york",
                "accuracy_by_regime",
                "recency",
            ],
            "future_sources": ["etoro", "zulutrade"],
            "future_max_score_effect": 5,
            "score_effect": 0,
        }


def write_snapshot(snapshot: Mapping[str, Any], output_path: Path = OUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(snapshot)
    payload["score_effect"] = 0
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output_path)


def run_once(
    *,
    output_path: Path = OUT,
    client: EtoroClient | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    timeout = float(os.getenv("GOLDSCOUT_ETORO_TIMEOUT", "8"))
    retries = int(os.getenv("GOLDSCOUT_ETORO_RETRIES", "2"))
    max_traders = int(os.getenv("GOLDSCOUT_ETORO_MAX_TRADERS", "5"))
    stale_after = int(os.getenv("GOLDSCOUT_ETORO_STALE_SECONDS", "900"))
    active_client = client or EtoroClient(timeout=timeout, max_retries=retries, now=now)
    service = EtoroExternalSignalService(
        active_client,
        max_traders=max_traders,
        stale_after_seconds=stale_after,
        now=now,
    )
    snapshot = service.collect()
    snapshot["score_effect"] = 0
    write_snapshot(snapshot, output_path)
    return snapshot


def main() -> None:
    interval = max(60, int(os.getenv("GOLDSCOUT_ETORO_INTERVAL", "300")))
    print(f"GoldScout eToro observer: {OUT}")
    while True:
        try:
            snapshot = run_once()
            print(
                "[etoro] refresh "
                f"{snapshot.get('api_status')} | traders={snapshot.get('valid_traders', 0)} "
                f"quality={snapshot.get('quality', 0)} | score_effect=0"
            )
        except Exception as exc:
            fallback = empty_snapshot("ERROR", f"Observador eToro degradado: {type(exc).__name__}")
            write_snapshot(fallback)
            print(f"[etoro] refresh ERROR: {type(exc).__name__}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
