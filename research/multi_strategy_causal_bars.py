"""Closed-M1 to M5/D1 research prototype; never imported by the live EA.

Timestamps are integer milliseconds in the MT5 server *wall-clock* encoding.
No UTC offset, session, missing minute, or tick is inferred here.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Iterator, Mapping


MINUTE_MS = 60_000
DAY_MS = 24 * 60 * MINUTE_MS
CONFIRMATION_FEATURES = frozenset({"pivot", "structure", "regime", "setup", "entry_quality"})


class LookAheadError(ValueError):
    """A feature is not known at the proposed decision time."""


@dataclass(frozen=True)
class ClosedM1:
    start: int
    end: int
    available_at: int
    open: float
    high: float
    low: float
    close: float
    tick_count: int
    avg_spread: float
    min_spread: float
    max_spread: float
    bar_id: str
    closed: bool = True

    @classmethod
    def from_replay_record(cls, record: Mapping[str, object]) -> "ClosedM1":
        if record.get("timeframe") != "M1":
            raise ValueError("expected a persisted closed M1 replay bar")
        start, end = int(record["timestamp"]), int(record["close_timestamp"])
        return cls(
            start=start, end=end, available_at=int(record.get("available_at", end)),
            open=float(record["open"]), high=float(record["high"]),
            low=float(record["low"]), close=float(record["close"]),
            tick_count=int(record["tick_count"]), avg_spread=float(record["avg_spread"]),
            min_spread=float(record["min_spread"]), max_spread=float(record["max_spread"]),
            bar_id=str(record["bar_id"]), closed=record.get("closed", True),
        )

    def validate(self) -> None:
        if self.start % MINUTE_MS or self.end != self.start + MINUTE_MS:
            raise ValueError("M1 boundaries must be aligned and exactly one minute apart")
        if self.closed is not True or self.available_at < self.end:
            raise LookAheadError("source M1 is open or was marked available before close")
        values = (self.open, self.high, self.low, self.close,
                  self.avg_spread, self.min_spread, self.max_spread)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("non-finite M1 value")
        if (self.tick_count <= 0 or self.high < max(self.open, self.close, self.low)
                or self.low > min(self.open, self.close, self.high)
                or self.min_spread < 0 or self.min_spread > self.avg_spread
                or self.avg_spread > self.max_spread):
            raise ValueError("inconsistent M1 OHLC, tick count, or spread")


@dataclass(frozen=True)
class CausalBar:
    timeframe: str
    start: int
    end: int
    available_at: int
    open: float
    high: float
    low: float
    close: float
    tick_count: int
    avg_spread: float
    min_spread: float
    max_spread: float
    observed_minutes: int
    expected_minutes: int
    coverage: float
    complete: bool
    provenance: dict[str, object]
    closed: bool = True
    d1_session_status: str | None = None


def require_causal_feature(bar: ClosedM1 | CausalBar, decision_time: int, feature: str) -> None:
    """Make the closed-bar and availability rule explicit for confirmation features."""
    if feature not in CONFIRMATION_FEATURES:
        raise ValueError(f"unsupported confirmation feature: {feature}")
    if (bar.closed is not True or bar.available_at < bar.end or
            bar.end > decision_time or bar.available_at > decision_time):
        raise LookAheadError(f"{feature} cannot use an open or unavailable {bar.timeframe if isinstance(bar, CausalBar) else 'M1'} bar")


def parse_broker_day_boundary(value: str) -> int:
    """Minutes after midnight in MT5 server wall time, not UTC."""
    if len(value) != 5 or value[2] != ":" or not (value[:2] + value[3:]).isdigit():
        raise ValueError("broker_day_boundary must be HH:MM in server wall time")
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute > 59:
        raise ValueError("broker_day_boundary must be a valid HH:MM")
    return hour * 60 + minute


class ClosedM1Aggregator:
    """Streaming aggregation; only a later closed M1 can finalize a bucket."""

    def __init__(self, timeframe: str, *, broker_day_boundary: str | None = None) -> None:
        if timeframe not in {"M5", "D1"}:
            raise ValueError("only M5 and D1 are supported")
        if timeframe == "D1" and broker_day_boundary is None:
            raise ValueError("D1 requires an explicit broker_day_boundary")
        if timeframe == "M5" and broker_day_boundary is not None:
            raise ValueError("M5 does not use broker_day_boundary")
        self.timeframe = timeframe
        self.duration = 5 * MINUTE_MS if timeframe == "M5" else DAY_MS
        self.boundary = (parse_broker_day_boundary(broker_day_boundary) * MINUTE_MS
                         if broker_day_boundary is not None else 0)
        self._members: list[ClosedM1] = []
        self._bucket_start: int | None = None
        self._last_start: int | None = None

    @property
    def pending_minutes(self) -> int:
        return len(self._members)

    def _start(self, minute: int) -> int:
        return (minute - self.boundary) // self.duration * self.duration + self.boundary

    def _emit(self, closing_evidence_at: int) -> CausalBar:
        members = self._members
        start = self._bucket_start
        assert members and start is not None
        end = start + self.duration
        ticks = sum(item.tick_count for item in members)
        expected = self.duration // MINUTE_MS
        observed = len(members)
        return CausalBar(
            timeframe=self.timeframe, start=start, end=end,
            available_at=max(end, closing_evidence_at, *(item.available_at for item in members)),
            open=members[0].open, high=max(item.high for item in members),
            low=min(item.low for item in members), close=members[-1].close,
            tick_count=ticks,
            avg_spread=sum(item.avg_spread * item.tick_count for item in members) / ticks,
            min_spread=min(item.min_spread for item in members),
            max_spread=max(item.max_spread for item in members),
            observed_minutes=observed, expected_minutes=expected,
            coverage=observed / expected, complete=observed == expected,
            provenance={"source_timeframe": "M1", "first_bar_id": members[0].bar_id,
                        "last_bar_id": members[-1].bar_id,
                        "source_bar_ids": [item.bar_id for item in members],
                        "aggregation": "CLOSED_M1_ONLY"},
            d1_session_status="UNVERIFIED" if self.timeframe == "D1" else None,
        )

    def push(self, bar: ClosedM1) -> CausalBar | None:
        bar.validate()
        if self._last_start is not None and bar.start <= self._last_start:
            raise ValueError("source M1 bars must be strictly chronological and unique")
        start = self._start(bar.start)
        emitted = None
        if self._bucket_start is not None and start != self._bucket_start:
            emitted = self._emit(bar.available_at)
            self._members = []
        self._bucket_start = start
        self._members.append(bar)
        self._last_start = bar.start
        return emitted


def aggregate_closed_m1(
    bars: Iterable[ClosedM1], timeframe: str, *, broker_day_boundary: str | None = None
) -> Iterator[CausalBar]:
    aggregator = ClosedM1Aggregator(timeframe, broker_day_boundary=broker_day_boundary)
    for bar in bars:
        completed = aggregator.push(bar)
        if completed is not None:
            yield completed
    # No flush: without a subsequent closed M1, the last bucket is unproven.
