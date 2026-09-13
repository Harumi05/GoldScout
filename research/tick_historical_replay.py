"""Causal, streaming replay of MT5 XAUUSD tick exports.

The engine is research-only. It never imports the live EA, returns a trading
decision, or changes a GoldScout score. MT5 timestamps are preserved as broker
wall-clock values encoded on an epoch-millisecond axis; no timezone conversion
is guessed.
"""

from __future__ import annotations

import argparse
import csv
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Iterator, Sequence


SOURCE = "HISTORICAL_MT5_TICKS"
TIMEFRAMES = {"M1": 60, "M15": 15 * 60, "H1": 60 * 60, "H4": 4 * 60 * 60}
OBSERVATION_TIMEFRAMES = ("M15", "H1", "H4")
HORIZONS = (("15m", 15 * 60 * 1000), ("1h", 60 * 60 * 1000), ("4h", 4 * 60 * 60 * 1000))
EPOCH_ORDINAL = datetime(1970, 1, 1).toordinal()


class ReplayError(RuntimeError):
    """Base error for deterministic replay failures."""


class ChronologyError(ReplayError):
    """Raised when one source file is not internally chronological."""


@dataclass(frozen=True)
class Tick:
    timestamp_ms: int
    bid: float
    ask: float
    last: float | None = None
    volume: float | None = None
    flags: str = ""
    source_file: str = ""
    line_number: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def exact_key(self) -> tuple:
        return (
            self.timestamp_ms,
            self.bid,
            self.ask,
            self.last is not None,
            0.0 if self.last is None else self.last,
            self.volume is not None,
            0.0 if self.volume is None else self.volume,
            self.flags,
        )


@dataclass
class IngestionStats:
    ticks_read: int = 0
    ticks_valid: int = 0
    ticks_discarded: int = 0
    duplicates: int = 0
    first_timestamp_ms: int | None = None
    last_timestamp_ms: int | None = None
    flags_nonempty: int = 0
    flag_samples: list[str] = field(default_factory=list)
    discard_reasons: dict[str, int] = field(default_factory=dict)
    file_discard_reasons: dict[str, dict[str, int]] = field(default_factory=dict)
    optional_field_errors: dict[str, int] = field(default_factory=dict)

    def discard(self, reason: str, path: Path | None = None) -> None:
        self.ticks_discarded += 1
        self.discard_reasons[reason] = self.discard_reasons.get(reason, 0) + 1
        if path is not None:
            key = str(path)
            reasons = self.file_discard_reasons.setdefault(key, {})
            reasons[reason] = reasons.get(reason, 0) + 1

    def optional_error(self, field_name: str) -> None:
        self.optional_field_errors[field_name] = self.optional_field_errors.get(field_name, 0) + 1

    def accept(self, tick: Tick) -> None:
        self.ticks_valid += 1
        if self.first_timestamp_ms is None:
            self.first_timestamp_ms = tick.timestamp_ms
        self.last_timestamp_ms = tick.timestamp_ms
        if tick.flags:
            self.flags_nonempty += 1
            if tick.flags not in self.flag_samples and len(self.flag_samples) < 10:
                self.flag_samples.append(tick.flags)


@dataclass(frozen=True)
class CsvFileMetadata:
    path: Path
    size_bytes: int
    first_timestamp_ms: int
    last_timestamp_ms: int


@dataclass
class InputDiscovery:
    input_dir: Path | None
    csv_files_found: int
    files: list[CsvFileMetadata]
    issues: list[dict]
    overlaps: list[dict]

    @property
    def first_timestamp_ms(self) -> int | None:
        return min((item.first_timestamp_ms for item in self.files), default=None)

    @property
    def last_timestamp_ms(self) -> int | None:
        return max((item.last_timestamp_ms for item in self.files), default=None)

    @property
    def estimated_total_size(self) -> int:
        return sum(item.size_bytes for item in self.files)


def _canonical_header(value: str) -> str:
    return value.lstrip("\ufeff").strip().strip("<>").upper()


def _delimiter(line: str) -> str:
    candidates = ("\t", ",", ";")
    delimiter = max(candidates, key=line.count)
    if line.count(delimiter) == 0:
        raise ReplayError("CSV header has no supported delimiter")
    return delimiter


def _header_details(line: str) -> tuple[str, dict[str, int]]:
    delimiter = _delimiter(line)
    headers = [_canonical_header(item) for item in next(csv.reader([line], delimiter=delimiter))]
    return delimiter, {name: index for index, name in enumerate(headers)}


def parse_mt5_timestamp(date_value: str, time_value: str) -> int:
    """Parse MT5 DATE+TIME without assuming the broker's timezone."""
    date_parts = date_value.strip().replace("-", ".").replace("/", ".").split(".")
    time_parts = time_value.strip().split(":")
    if len(date_parts) != 3 or len(time_parts) != 3:
        raise ValueError("invalid DATE/TIME")
    year, month, day = (int(value) for value in date_parts)
    second_part = time_parts[2]
    if "." in second_part:
        seconds_text, fraction = second_part.split(".", 1)
        if not fraction.isdigit():
            raise ValueError("invalid milliseconds")
        milliseconds = int((fraction + "000")[:3])
    else:
        seconds_text = second_part
        milliseconds = 0
    hour, minute, second = int(time_parts[0]), int(time_parts[1]), int(seconds_text)
    value = datetime(year, month, day, hour, minute, second)
    seconds = (value.toordinal() - EPOCH_ORDINAL) * 86400 + hour * 3600 + minute * 60 + second
    return seconds * 1000 + milliseconds


def format_timestamp(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _optional_float(value: str, stats: IngestionStats, field_name: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        stats.optional_error(field_name)
        return None
    if not math.isfinite(number):
        stats.optional_error(field_name)
        return None
    return number


def _row_field(row: list[str], columns: dict[str, int], name: str) -> str:
    index = columns.get(name)
    return row[index] if index is not None and index < len(row) else ""


def _valid_row_timestamp(line: str, delimiter: str, columns: dict[str, int]) -> tuple[int, list[str]] | None:
    try:
        row = next(csv.reader([line], delimiter=delimiter))
        timestamp_ms = parse_mt5_timestamp(
            _row_field(row, columns, "DATE"), _row_field(row, columns, "TIME")
        )
        bid = float(_row_field(row, columns, "BID").strip())
        ask = float(_row_field(row, columns, "ASK").strip())
    except (ValueError, csv.Error):
        return None
    if (
        not math.isfinite(bid)
        or not math.isfinite(ask)
        or bid <= 0.0
        or ask <= 0.0
        or ask < bid
    ):
        return None
    return timestamp_ms, row


def _reverse_lines(path: Path, block_size: int = 64 * 1024) -> Iterator[str]:
    """Yield decoded non-empty lines from the end without loading the CSV."""
    with Path(path).open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        remainder = b""
        while position > 0:
            amount = min(block_size, position)
            position -= amount
            stream.seek(position)
            remainder = stream.read(amount) + remainder
            lines = remainder.split(b"\n")
            remainder = lines[0]
            for raw in reversed(lines[1:]):
                if not raw.strip():
                    continue
                try:
                    yield raw.decode("utf-8-sig").strip("\r")
                except UnicodeDecodeError:
                    continue
        if remainder.strip():
            try:
                yield remainder.decode("utf-8-sig").strip("\r")
            except UnicodeDecodeError:
                return


def _filename_looks_like_symbol(path: Path, symbol: str) -> bool:
    expected = symbol.upper()
    return re.search(rf"(^|[^A-Z0-9]){re.escape(expected)}([^A-Z0-9]|$)", path.stem.upper()) is not None


def inspect_tick_csv(path: Path, symbol: str = "XAUUSD") -> CsvFileMetadata:
    """Inspect header plus file edges; the large body remains unread."""
    path = Path(path)
    with path.open("rb") as stream:
        header = None
        while header is None:
            raw = stream.readline()
            if not raw:
                raise ReplayError("empty CSV")
            try:
                candidate = raw.decode("utf-8-sig").strip("\r\n")
            except UnicodeDecodeError as error:
                raise ReplayError(f"invalid header encoding: {error}") from error
            if candidate.strip():
                header = candidate
        delimiter, columns = _header_details(header)
        missing = [name for name in TickCsvReader.REQUIRED if name not in columns]
        if missing:
            raise ReplayError(f"not a tick CSV; missing columns {missing!r}")

        first_timestamp_ms = None
        first_row = None
        for raw in stream:
            try:
                line = raw.decode("utf-8-sig").strip("\r\n")
            except UnicodeDecodeError:
                continue
            parsed = _valid_row_timestamp(line, delimiter, columns)
            if parsed is not None:
                first_timestamp_ms, first_row = parsed
                break
        if first_timestamp_ms is None or first_row is None:
            raise ReplayError("no valid BID/ASK tick rows")

    if "SYMBOL" in columns:
        actual_symbol = _row_field(first_row, columns, "SYMBOL").strip().upper()
        looks_like_symbol = actual_symbol == symbol.upper()
    else:
        looks_like_symbol = _filename_looks_like_symbol(path, symbol)
    if not looks_like_symbol:
        raise ReplayError(f"SKIPPED_NON_XAUUSD: file does not identify {symbol}")

    last_timestamp_ms = None
    for line in _reverse_lines(path):
        parsed = _valid_row_timestamp(line, delimiter, columns)
        if parsed is not None:
            last_timestamp_ms = parsed[0]
            break
    if last_timestamp_ms is None:
        raise ReplayError("no valid tick at end of CSV")
    if last_timestamp_ms < first_timestamp_ms:
        raise ReplayError("last timestamp precedes first timestamp")
    return CsvFileMetadata(path, path.stat().st_size, first_timestamp_ms, last_timestamp_ms)


def discover_inputs(
    explicit_files: Sequence[Path] = (),
    input_dir: Path | None = None,
    *,
    symbol: str = "XAUUSD",
) -> InputDiscovery:
    """Discover, validate and content-sort tick files before replay."""
    candidates: list[Path] = [Path(path) for path in explicit_files]
    if input_dir is not None:
        input_dir = Path(input_dir)
        if not input_dir.is_dir():
            raise ReplayError(f"input directory does not exist: {input_dir}")
        candidates.extend(
            path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() == ".csv"
        )
    unique_candidates: list[Path] = []
    seen_paths: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()).lower()
        if key not in seen_paths:
            seen_paths.add(key)
            unique_candidates.append(candidate)

    issues: list[dict] = []
    metadata: list[CsvFileMetadata] = []
    for path in unique_candidates:
        try:
            metadata.append(inspect_tick_csv(path, symbol))
        except (OSError, ReplayError) as error:
            issues.append({"file": str(path), "cause": str(error)})
    metadata.sort(key=lambda item: (item.first_timestamp_ms, item.last_timestamp_ms, str(item.path).lower()))

    overlaps: list[dict] = []
    if metadata:
        covering = metadata[0]
        farthest_end = covering.last_timestamp_ms
        for item in metadata[1:]:
            if item.first_timestamp_ms <= farthest_end:
                overlaps.append(
                    {
                        "earlier_file": str(covering.path),
                        "later_file": str(item.path),
                        "overlap_start": format_timestamp(item.first_timestamp_ms),
                        "overlap_end": format_timestamp(min(farthest_end, item.last_timestamp_ms)),
                    }
                )
            if item.last_timestamp_ms > farthest_end:
                covering = item
                farthest_end = item.last_timestamp_ms
    return InputDiscovery(input_dir, len(unique_candidates), metadata, issues, overlaps)


def format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{size_bytes} B"


class TickCsvReader:
    """Line-streaming reader for one internally ordered MT5 tick CSV."""

    REQUIRED = ("DATE", "TIME", "BID", "ASK")

    def __init__(self, path: Path, stats: IngestionStats):
        self.path = Path(path)
        self.stats = stats
        self.stream = self.path.open("rb")
        self.line_number = 0
        self.last_timestamp_ms: int | None = None
        header_line = self._read_nonempty_line()
        if header_line is None:
            self.close()
            raise ReplayError(f"empty tick CSV: {self.path}")
        self.delimiter = _delimiter(header_line)
        headers = [_canonical_header(item) for item in next(csv.reader([header_line], delimiter=self.delimiter))]
        self.columns = {name: index for index, name in enumerate(headers)}
        missing = [name for name in self.REQUIRED if name not in self.columns]
        if missing:
            self.close()
            raise ReplayError(f"missing columns {missing!r} in {self.path}")

    def _read_nonempty_line(self) -> str | None:
        while True:
            raw = self.stream.readline()
            if not raw:
                return None
            self.line_number += 1
            try:
                line = raw.decode("utf-8-sig").strip("\r\n")
            except UnicodeDecodeError:
                self.stats.ticks_read += 1
                self.stats.discard("encoding", self.path)
                continue
            if line.strip():
                return line

    def _field(self, row: list[str], name: str) -> str:
        index = self.columns.get(name)
        return row[index] if index is not None and index < len(row) else ""

    def next_tick(self) -> Tick | None:
        while True:
            line = self._read_nonempty_line()
            if line is None:
                return None
            self.stats.ticks_read += 1
            try:
                row = next(csv.reader([line], delimiter=self.delimiter))
                timestamp_ms = parse_mt5_timestamp(self._field(row, "DATE"), self._field(row, "TIME"))
            except (ValueError, csv.Error):
                self.stats.discard("timestamp_or_csv", self.path)
                continue
            try:
                bid = float(self._field(row, "BID").strip())
                ask = float(self._field(row, "ASK").strip())
            except ValueError:
                self.stats.discard("bid_or_ask", self.path)
                continue
            if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0.0 or ask <= 0.0:
                self.stats.discard("bid_or_ask", self.path)
                continue
            if ask < bid:
                self.stats.discard("ask_below_bid", self.path)
                continue
            if self.last_timestamp_ms is not None and timestamp_ms < self.last_timestamp_ms:
                raise ChronologyError(
                    f"non-chronological tick at {self.path}:{self.line_number}"
                )
            self.last_timestamp_ms = timestamp_ms
            return Tick(
                timestamp_ms=timestamp_ms,
                bid=bid,
                ask=ask,
                last=_optional_float(self._field(row, "LAST"), self.stats, "LAST"),
                volume=_optional_float(self._field(row, "VOLUME"), self.stats, "VOLUME"),
                flags=self._field(row, "FLAGS").strip(),
                source_file=str(self.path),
                line_number=self.line_number,
            )

    def close(self) -> None:
        if not self.stream.closed:
            self.stream.close()


def iter_ticks(
    paths: Sequence[Path],
    stats: IngestionStats,
    issues: list[dict] | None = None,
    processed_files: list[str] | None = None,
) -> Iterator[Tick]:
    """K-way chronological merge with exact deterministic deduplication."""
    if not paths:
        raise ReplayError("at least one input CSV is required")
    readers: list[TickCsvReader | None] = []
    heap: list[tuple[int, int, int, Tick]] = []

    def file_error(path: Path, error: Exception) -> None:
        if issues is None:
            raise error
        issues.append({"file": str(path), "cause": str(error)})

    try:
        for index, path in enumerate(paths):
            path = Path(path)
            reader = None
            try:
                reader = TickCsvReader(path, stats)
                tick = reader.next_tick()
            except (OSError, ReplayError) as error:
                if reader is not None:
                    reader.close()
                readers.append(None)
                file_error(path, error)
                continue
            readers.append(reader)
            if processed_files is not None:
                processed_files.append(str(path))
            if tick is not None:
                heapq.heappush(heap, (tick.timestamp_ms, index, tick.line_number, tick))

        while heap:
            timestamp_ms = heap[0][0]
            group: list[Tick] = []
            while heap and heap[0][0] == timestamp_ms:
                _, index, _, tick = heapq.heappop(heap)
                group.append(tick)
                reader = readers[index]
                if reader is None:
                    continue
                try:
                    following = reader.next_tick()
                except (OSError, ReplayError) as error:
                    file_error(Path(reader.path), error)
                    reader.close()
                    readers[index] = None
                    continue
                if following is not None:
                    heapq.heappush(
                        heap,
                        (following.timestamp_ms, index, following.line_number, following),
                    )
            group.sort(key=Tick.exact_key)
            previous_key: tuple | None = None
            for tick in group:
                key = tick.exact_key()
                if key == previous_key:
                    stats.duplicates += 1
                    continue
                previous_key = key
                stats.accept(tick)
                yield tick
    finally:
        for reader in readers:
            if reader is not None:
                reader.close()


@dataclass
class Bar:
    timeframe: str
    timestamp_ms: int
    close_timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    tick_count: int
    avg_spread: float
    min_spread: float
    max_spread: float
    close_spread: float

    def record(self, symbol: str) -> dict:
        return {
            "bar_id": f"{SOURCE}-{symbol}-{self.timeframe}-{self.timestamp_ms}",
            "source": SOURCE,
            "symbol": symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp_ms,
            "close_timestamp": self.close_timestamp_ms,
            "timestamp_unit": "epoch_ms_mt5_server_wall_time",
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "tick_count": self.tick_count,
            "avg_spread": self.avg_spread,
            "min_spread": self.min_spread,
            "max_spread": self.max_spread,
        }


@dataclass
class _OpenBar:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    tick_count: int
    spread_sum: float
    min_spread: float
    max_spread: float
    close_spread: float


class BarBuilder:
    def __init__(self, timeframe: str, seconds: int):
        self.timeframe = timeframe
        self.duration_ms = seconds * 1000
        self.current: _OpenBar | None = None

    def add(self, tick: Tick) -> Bar | None:
        bucket = tick.timestamp_ms // self.duration_ms * self.duration_ms
        if self.current is None:
            self.current = _OpenBar(
                bucket, tick.mid, tick.mid, tick.mid, tick.mid, 1,
                tick.spread, tick.spread, tick.spread, tick.spread,
            )
            return None
        if bucket < self.current.timestamp_ms:
            raise ChronologyError("bar builder received an older tick")
        if bucket == self.current.timestamp_ms:
            current = self.current
            current.high = max(current.high, tick.mid)
            current.low = min(current.low, tick.mid)
            current.close = tick.mid
            current.tick_count += 1
            current.spread_sum += tick.spread
            current.min_spread = min(current.min_spread, tick.spread)
            current.max_spread = max(current.max_spread, tick.spread)
            current.close_spread = tick.spread
            return None
        closed = self._closed_bar()
        self.current = _OpenBar(
            bucket, tick.mid, tick.mid, tick.mid, tick.mid, 1,
            tick.spread, tick.spread, tick.spread, tick.spread,
        )
        return closed

    def _closed_bar(self) -> Bar:
        current = self.current
        assert current is not None
        return Bar(
            timeframe=self.timeframe,
            timestamp_ms=current.timestamp_ms,
            close_timestamp_ms=current.timestamp_ms + self.duration_ms,
            open=current.open,
            high=current.high,
            low=current.low,
            close=current.close,
            tick_count=current.tick_count,
            avg_spread=current.spread_sum / current.tick_count,
            min_spread=current.min_spread,
            max_spread=current.max_spread,
            close_spread=current.close_spread,
        )


class MultiTimeframeBars:
    def __init__(self):
        self.builders = {name: BarBuilder(name, seconds) for name, seconds in TIMEFRAMES.items()}

    def add(self, tick: Tick) -> list[Bar]:
        closed: list[Bar] = []
        for name in ("M1", "M15", "H1", "H4"):
            bar = self.builders[name].add(tick)
            if bar is not None:
                closed.append(bar)
        return closed


class IndicatorState:
    """Incremental Wilder/EMA indicators using only closed bars received so far."""

    def __init__(self, period: int = 14):
        self.period = period
        self.ema20: float | None = None
        self.ema200: float | None = None
        self.ema20_seed: deque[float] = deque(maxlen=20)
        self.ema200_seed: deque[float] = deque(maxlen=200)
        self.previous_close: float | None = None
        self.previous_high: float | None = None
        self.previous_low: float | None = None
        self.average_gain: float | None = None
        self.average_loss: float | None = None
        self.rsi_gains: deque[float] = deque(maxlen=period)
        self.rsi_losses: deque[float] = deque(maxlen=period)
        self.tr_values: deque[float] = deque(maxlen=period)
        self.plus_dm_values: deque[float] = deque(maxlen=period)
        self.minus_dm_values: deque[float] = deque(maxlen=period)
        self.smoothed_tr: float | None = None
        self.smoothed_plus_dm: float | None = None
        self.smoothed_minus_dm: float | None = None
        self.dx_values: deque[float] = deque(maxlen=period)
        self.adx: float | None = None

    @staticmethod
    def _ema(previous: float | None, seed: deque[float], value: float, period: int) -> float | None:
        if previous is None:
            seed.append(value)
            return sum(seed) / period if len(seed) == period else None
        alpha = 2.0 / (period + 1.0)
        return previous + alpha * (value - previous)

    def update(self, bar: Bar) -> dict[str, float | None]:
        self.ema20 = self._ema(self.ema20, self.ema20_seed, bar.close, 20)
        self.ema200 = self._ema(self.ema200, self.ema200_seed, bar.close, 200)

        rsi: float | None = None
        if self.previous_close is not None:
            change = bar.close - self.previous_close
            gain, loss = max(change, 0.0), max(-change, 0.0)
            if self.average_gain is None:
                self.rsi_gains.append(gain)
                self.rsi_losses.append(loss)
                if len(self.rsi_gains) == self.period:
                    self.average_gain = sum(self.rsi_gains) / self.period
                    self.average_loss = sum(self.rsi_losses) / self.period
            else:
                self.average_gain = (self.average_gain * (self.period - 1) + gain) / self.period
                self.average_loss = (self.average_loss * (self.period - 1) + loss) / self.period
        if self.average_gain is not None and self.average_loss is not None:
            if self.average_loss == 0.0:
                rsi = 100.0 if self.average_gain > 0.0 else 50.0
            else:
                ratio = self.average_gain / self.average_loss
                rsi = 100.0 - 100.0 / (1.0 + ratio)

        if self.previous_close is None:
            true_range = bar.high - bar.low
            plus_dm = minus_dm = 0.0
        else:
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - self.previous_close),
                abs(bar.low - self.previous_close),
            )
            up_move = bar.high - (self.previous_high if self.previous_high is not None else bar.high)
            down_move = (self.previous_low if self.previous_low is not None else bar.low) - bar.low
            plus_dm = up_move if up_move > down_move and up_move > 0.0 else 0.0
            minus_dm = down_move if down_move > up_move and down_move > 0.0 else 0.0

        if self.smoothed_tr is None:
            self.tr_values.append(true_range)
            self.plus_dm_values.append(plus_dm)
            self.minus_dm_values.append(minus_dm)
            if len(self.tr_values) == self.period:
                self.smoothed_tr = sum(self.tr_values)
                self.smoothed_plus_dm = sum(self.plus_dm_values)
                self.smoothed_minus_dm = sum(self.minus_dm_values)
        else:
            self.smoothed_tr = self.smoothed_tr - self.smoothed_tr / self.period + true_range
            self.smoothed_plus_dm = (
                self.smoothed_plus_dm - self.smoothed_plus_dm / self.period + plus_dm
            )
            self.smoothed_minus_dm = (
                self.smoothed_minus_dm - self.smoothed_minus_dm / self.period + minus_dm
            )

        atr = plus_di = minus_di = None
        if self.smoothed_tr is not None:
            atr = self.smoothed_tr / self.period
            if self.smoothed_tr > 0.0:
                plus_di = 100.0 * self.smoothed_plus_dm / self.smoothed_tr
                minus_di = 100.0 * self.smoothed_minus_dm / self.smoothed_tr
                denominator = plus_di + minus_di
                dx = 0.0 if denominator == 0.0 else 100.0 * abs(plus_di - minus_di) / denominator
                if self.adx is None:
                    self.dx_values.append(dx)
                    if len(self.dx_values) == self.period:
                        self.adx = sum(self.dx_values) / self.period
                else:
                    self.adx = (self.adx * (self.period - 1) + dx) / self.period

        self.previous_close = bar.close
        self.previous_high = bar.high
        self.previous_low = bar.low
        return {
            "rsi": rsi,
            "adx": self.adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "atr": atr,
            "ema20": self.ema20,
            "ema200": self.ema200,
        }


@dataclass(frozen=True)
class Pivot:
    kind: str
    price: float
    index: int
    timestamp_ms: int


class ConfirmedPivotState:
    """Small causal pivot classifier; confirmation needs right-side closed bars."""

    def __init__(self, left: int = 2, right: int = 2, min_bars: int = 2):
        self.left = left
        self.right = right
        self.min_bars = min_bars
        self.bars: deque[tuple[int, Bar]] = deque(maxlen=512)
        self.pivots: list[Pivot] = []

    def update(self, index: int, bar: Bar) -> None:
        self.bars.append((index, bar))
        required = self.left + self.right + 1
        if len(self.bars) < required:
            return
        window = list(self.bars)[-required:]
        candidate_index, candidate = window[self.left]
        neighbours = [item[1] for position, item in enumerate(window) if position != self.left]
        is_high = all(candidate.high > other.high for other in neighbours)
        is_low = all(candidate.low < other.low for other in neighbours)
        if is_high:
            self._append(Pivot("HIGH", candidate.high, candidate_index, candidate.timestamp_ms))
        if is_low:
            self._append(Pivot("LOW", candidate.low, candidate_index, candidate.timestamp_ms))

    def _append(self, pivot: Pivot) -> None:
        if not self.pivots:
            self.pivots.append(pivot)
            return
        previous = self.pivots[-1]
        if previous.kind == pivot.kind:
            more_extreme = pivot.price > previous.price if pivot.kind == "HIGH" else pivot.price < previous.price
            if more_extreme:
                self.pivots[-1] = pivot
            return
        if pivot.index - previous.index < self.min_bars:
            return
        self.pivots.append(pivot)
        if len(self.pivots) > 128:
            del self.pivots[:-128]

    def classify(self, atr: float | None) -> tuple[str, float | None, float | None]:
        highs = [pivot for pivot in self.pivots if pivot.kind == "HIGH"]
        lows = [pivot for pivot in self.pivots if pivot.kind == "LOW"]
        last_high = highs[-1].price if highs else None
        last_low = lows[-1].price if lows else None
        if len(highs) < 2 or len(lows) < 2 or atr is None or atr <= 0.0:
            return "INSUFFICIENT", last_high, last_low
        tolerance = atr * 0.20
        high_delta = highs[-1].price - highs[-2].price
        low_delta = lows[-1].price - lows[-2].price
        if high_delta > tolerance and low_delta > tolerance:
            return "BULLISH", last_high, last_low
        if high_delta < -tolerance and low_delta < -tolerance:
            return "BEARISH", last_high, last_low
        return "NEUTRAL", last_high, last_low


class HistoricalMarketState:
    def __init__(self):
        self.indicators = IndicatorState()
        self.pivots = ConfirmedPivotState()
        self.index = 0
        self.previous_close: float | None = None
        self.previous_ema20: float | None = None

    def update(self, bar: Bar) -> dict:
        values = self.indicators.update(bar)
        self.pivots.update(self.index, bar)
        structure, swing_high, swing_low = self.pivots.classify(values["atr"])
        atr = values["atr"]
        buffer = atr * 0.05 if atr is not None else 0.0
        breakout = "NONE"
        if self.previous_close is not None and swing_high is not None:
            if self.previous_close <= swing_high + buffer and bar.close > swing_high + buffer:
                breakout = "LONG"
        if self.previous_close is not None and swing_low is not None and breakout == "NONE":
            if self.previous_close >= swing_low - buffer and bar.close < swing_low - buffer:
                breakout = "SHORT"

        momentum = "NONE"
        if all(values[name] is not None for name in ("adx", "plus_di", "minus_di", "rsi")):
            if values["adx"] >= 20.0 and values["plus_di"] > values["minus_di"] and values["rsi"] >= 55.0:
                momentum = "LONG"
            elif values["adx"] >= 20.0 and values["minus_di"] > values["plus_di"] and values["rsi"] <= 45.0:
                momentum = "SHORT"

        pullback = "NONE"
        recovery = "NONE"
        ema20, ema200 = values["ema20"], values["ema200"]
        if atr is not None and ema20 is not None and ema200 is not None:
            if structure == "BULLISH" and bar.close >= ema20 and bar.low <= ema20 + atr * 0.20 and bar.close > ema200:
                pullback = "LONG"
            elif structure == "BEARISH" and bar.close <= ema20 and bar.high >= ema20 - atr * 0.20 and bar.close < ema200:
                pullback = "SHORT"
            if self.previous_close is not None and self.previous_ema20 is not None:
                if structure == "BULLISH" and self.previous_close <= self.previous_ema20 and bar.close > ema20:
                    recovery = "LONG"
                elif structure == "BEARISH" and self.previous_close >= self.previous_ema20 and bar.close < ema20:
                    recovery = "SHORT"

        self.previous_close = bar.close
        self.previous_ema20 = ema20
        self.index += 1
        return {
            **values,
            "structure": structure,
            "breakout": breakout,
            "momentum": momentum,
            "pullback": pullback,
            "recovery": recovery,
        }


class DeduplicatingJsonlWriter:
    """Append-only JSONL writer; existing deterministic IDs are never rewritten."""

    def __init__(self, path: Path, id_field: str):
        self.path = Path(path)
        self.id_field = id_field
        self.ids: set[str] = set()
        self.fingerprints: dict[str, str] = {}
        self.written = 0
        self.skipped = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with self.path.open("r", encoding="utf-8-sig", errors="replace") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    identifier = value.get(self.id_field)
                    if isinstance(identifier, str) and identifier:
                        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                        if identifier in self.fingerprints and self.fingerprints[identifier] != fingerprint:
                            raise ReplayError(f"conflicting existing append-only record: {identifier}")
                        self.ids.add(identifier)
                        self.fingerprints[identifier] = fingerprint
        needs_separator = self.path.exists() and self.path.stat().st_size > 0
        if needs_separator:
            with self.path.open("rb") as stream:
                stream.seek(-1, os.SEEK_END)
                needs_separator = stream.read(1) != b"\n"
        self.stream = self.path.open("a", encoding="utf-8", newline="\n")
        if needs_separator:
            self.stream.write("\n")

    def append(self, record: dict) -> bool:
        identifier = record.get(self.id_field)
        if not isinstance(identifier, str) or not identifier:
            raise ReplayError(f"record has no {self.id_field}")
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if identifier in self.ids:
            if self.fingerprints.get(identifier) != fingerprint:
                raise ReplayError(f"conflicting append-only record: {identifier}")
            self.skipped += 1
            return False
        self.stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.ids.add(identifier)
        self.fingerprints[identifier] = fingerprint
        self.written += 1
        if self.written % 1000 == 0:
            self.stream.flush()
        return True

    def close(self) -> None:
        self.stream.flush()
        self.stream.close()


@dataclass
class _HorizonAccumulator:
    last_mid: float | None = None
    maximum: float | None = None
    minimum: float | None = None
    max_spread: float | None = None
    tick_count: int = 0

    def add(self, tick: Tick) -> None:
        mid = tick.mid
        self.last_mid = mid
        self.maximum = mid if self.maximum is None else max(self.maximum, mid)
        self.minimum = mid if self.minimum is None else min(self.minimum, mid)
        self.max_spread = tick.spread if self.max_spread is None else max(self.max_spread, tick.spread)
        self.tick_count += 1


@dataclass
class _PendingOutcome:
    event_id: str
    anchor_ms: int
    initial_price: float
    entry_spread: float
    accumulators: dict[str, _HorizonAccumulator] = field(
        default_factory=lambda: {name: _HorizonAccumulator() for name, _ in HORIZONS}
    )
    values: dict[str, dict] = field(default_factory=dict)


def directional_outcome(values: dict, direction: str) -> dict:
    """Convert the canonical LONG view to LONG or SHORT semantics."""
    direction = direction.upper()
    if direction == "LONG":
        return dict(values)
    if direction != "SHORT":
        raise ValueError("direction must be LONG or SHORT")
    result = dict(values)
    future = values.get("future_return")
    mfe = values.get("mfe")
    mae = values.get("mae")
    result["future_return"] = None if future is None else -future
    result["mfe"] = None if mae is None else -mae
    result["mae"] = None if mfe is None else -mfe
    return result


class OutcomeTracker:
    def __init__(self, writer: DeduplicatingJsonlWriter):
        self.writer = writer
        self.pending: list[_PendingOutcome] = []
        self.rows_generated = 0

    def add_observation(self, observation: dict) -> None:
        self.pending.append(
            _PendingOutcome(
                event_id=observation["event_id"],
                anchor_ms=observation["observed_at"],
                initial_price=observation["close"],
                entry_spread=observation["spread"],
            )
        )

    def on_tick(self, tick: Tick) -> None:
        retained: list[_PendingOutcome] = []
        for pending in self.pending:
            for name, horizon_ms in HORIZONS:
                if name in pending.values:
                    continue
                if pending.anchor_ms < tick.timestamp_ms <= pending.anchor_ms + horizon_ms:
                    pending.accumulators[name].add(tick)
                if tick.timestamp_ms > pending.anchor_ms + horizon_ms:
                    self._finalize(pending, name)
            if len(pending.values) < len(HORIZONS):
                retained.append(pending)
        self.pending = retained

    def finish(self, available_through_ms: int | None) -> None:
        if available_through_ms is None:
            return
        retained: list[_PendingOutcome] = []
        for pending in self.pending:
            for name, horizon_ms in HORIZONS:
                if name not in pending.values and available_through_ms >= pending.anchor_ms + horizon_ms:
                    self._finalize(pending, name)
            if len(pending.values) < len(HORIZONS):
                retained.append(pending)
        self.pending = retained

    def _finalize(self, pending: _PendingOutcome, name: str) -> None:
        accumulator = pending.accumulators[name]
        if accumulator.last_mid is None:
            values = {
                "future_return": None,
                "mfe": None,
                "mae": None,
                "max_spread": None,
                "tick_count": 0,
            }
        else:
            initial = pending.initial_price
            values = {
                "future_return": (accumulator.last_mid - initial) / initial,
                "mfe": max(0.0, (accumulator.maximum - initial) / initial),
                "mae": min(0.0, (accumulator.minimum - initial) / initial),
                "max_spread": accumulator.max_spread,
                "tick_count": accumulator.tick_count,
            }
        pending.values[name] = values
        record = {
            "outcome_id": f"{pending.event_id}|{name}",
            "event_id": pending.event_id,
            "evaluated_at": pending.anchor_ms + dict(HORIZONS)[name],
            "source": SOURCE,
            "observer_only": True,
            "score_effect": 0,
            "return_convention": "LONG_XAUUSD_DECIMAL",
            "short_transform": "return=-return;mfe=-mae;mae=-mfe",
            "completed_horizon": name,
            "entry_spread": pending.entry_spread,
        }
        for horizon_name, _ in HORIZONS:
            completed = pending.values.get(horizon_name)
            record[f"future_return_{horizon_name}"] = None if completed is None else completed["future_return"]
            record[f"mfe_{horizon_name}"] = None if completed is None else completed["mfe"]
            record[f"mae_{horizon_name}"] = None if completed is None else completed["mae"]
            record[f"max_spread_{horizon_name}"] = None if completed is None else completed["max_spread"]
        self.writer.append(record)
        self.rows_generated += 1


def observation_from_bar(symbol: str, bar: Bar, state: dict) -> dict:
    event_id = f"{SOURCE}-{symbol}-{bar.timeframe}-{bar.timestamp_ms}"
    return {
        "event_id": event_id,
        "timestamp": bar.timestamp_ms,
        "observed_at": bar.close_timestamp_ms,
        "timestamp_unit": "epoch_ms_mt5_server_wall_time",
        "symbol": symbol,
        "timeframe": bar.timeframe,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "rsi": state["rsi"],
        "adx": state["adx"],
        "plus_di": state["plus_di"],
        "minus_di": state["minus_di"],
        "atr": state["atr"],
        "ema20": state["ema20"],
        "ema200": state["ema200"],
        "structure": state["structure"],
        "breakout": state["breakout"],
        "momentum": state["momentum"],
        "pullback": state["pullback"],
        "recovery": state["recovery"],
        "spread": bar.close_spread,
        "tick_count": bar.tick_count,
        "bar_closed": True,
        "source": SOURCE,
        "observer_only": True,
        "score_effect": 0,
    }


def peak_working_set_mb() -> float | None:
    if os.name != "nt":
        try:
            import resource

            divisor = 1024.0 if sys.platform != "darwin" else 1024.0 * 1024.0
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor
        except (ImportError, OSError):
            return None
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        )
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / (1024.0 * 1024.0)
    except (AttributeError, OSError):
        return None
    return None


def replay_files(
    paths: Sequence[Path],
    output_dir: Path,
    *,
    symbol: str = "XAUUSD",
    chunk_size: int = 50_000,
    max_ticks: int | None = None,
) -> dict:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    started = time.perf_counter()
    stats = IngestionStats()
    output_dir = Path(output_dir)
    bar_writers = {
        timeframe: DeduplicatingJsonlWriter(
            output_dir / "historical_bars" / f"{symbol}_{timeframe}.jsonl", "bar_id"
        )
        for timeframe in TIMEFRAMES
    }
    observation_writer = DeduplicatingJsonlWriter(
        output_dir / "historical_observations.jsonl", "event_id"
    )
    outcome_writer = DeduplicatingJsonlWriter(
        output_dir / "historical_outcomes.jsonl", "outcome_id"
    )
    outcomes = OutcomeTracker(outcome_writer)
    bars = MultiTimeframeBars()
    states = {timeframe: HistoricalMarketState() for timeframe in OBSERVATION_TIMEFRAMES}
    bars_generated = {timeframe: 0 for timeframe in TIMEFRAMES}
    observations_generated = 0
    processed = 0
    runtime_issues: list[dict] = []
    processed_files: list[str] = []

    try:
        iterator = iter_ticks(
            [Path(path) for path in paths],
            stats,
            issues=runtime_issues,
            processed_files=processed_files,
        )
        exhausted = False
        while not exhausted:
            chunk: list[Tick] = []
            while len(chunk) < chunk_size:
                if max_ticks is not None and processed + len(chunk) >= max_ticks:
                    exhausted = True
                    break
                try:
                    chunk.append(next(iterator))
                except StopIteration:
                    exhausted = True
                    break
            for tick in chunk:
                for bar in bars.add(tick):
                    bars_generated[bar.timeframe] += 1
                    bar_writers[bar.timeframe].append(bar.record(symbol))
                    if bar.timeframe in states:
                        state = states[bar.timeframe].update(bar)
                        observation = observation_from_bar(symbol, bar, state)
                        observation_writer.append(observation)
                        observations_generated += 1
                        outcomes.add_observation(observation)
                outcomes.on_tick(tick)
                processed += 1
            if not chunk:
                break
        outcomes.finish(stats.last_timestamp_ms)
    finally:
        if "iterator" in locals():
            iterator.close()
        for writer in bar_writers.values():
            writer.close()
        observation_writer.close()
        outcome_writer.close()

    elapsed = max(time.perf_counter() - started, 1e-9)
    return {
        "source": SOURCE,
        "observer_only": True,
        "score_effect": 0,
        "symbol": symbol,
        "input_files": [str(Path(path)) for path in paths],
        "files_processed": processed_files,
        "runtime_file_issues": runtime_issues,
        "chunk_size": chunk_size,
        "ticks_read": stats.ticks_read,
        "ticks_valid": stats.ticks_valid,
        "ticks_discarded": stats.ticks_discarded,
        "duplicates": stats.duplicates,
        "first_timestamp": format_timestamp(stats.first_timestamp_ms),
        "last_timestamp": format_timestamp(stats.last_timestamp_ms),
        "discard_reasons": stats.discard_reasons,
        "file_discard_reasons": stats.file_discard_reasons,
        "flags_nonempty": stats.flags_nonempty,
        "flag_samples": stats.flag_samples,
        "optional_field_errors": stats.optional_field_errors,
        "bars_generated": bars_generated,
        "bars_written": {name: writer.written for name, writer in bar_writers.items()},
        "observations_generated": observations_generated,
        "observations_written": observation_writer.written,
        "outcomes_generated": outcomes.rows_generated,
        "outcomes_written": outcome_writer.written,
        "pending_outcomes": len(outcomes.pending),
        "resume_skipped": {
            "bars": sum(writer.skipped for writer in bar_writers.values()),
            "observations": observation_writer.skipped,
            "outcomes": outcome_writer.skipped,
        },
        "elapsed_seconds": elapsed,
        "ticks_per_second": stats.ticks_valid / elapsed,
        "peak_working_set_mb": peak_working_set_mb(),
    }


def _reference_bars(path: Path) -> Iterator[dict]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        first = stream.readline()
        delimiter = _delimiter(first)
        headers = [_canonical_header(item) for item in next(csv.reader([first], delimiter=delimiter))]
        columns = {name: index for index, name in enumerate(headers)}
        for required in ("DATE", "TIME", "OPEN", "HIGH", "LOW", "CLOSE"):
            if required not in columns:
                raise ReplayError(f"reference M1 CSV has no {required}")
        for row in csv.reader(stream, delimiter=delimiter):
            try:
                yield {
                    "timestamp": parse_mt5_timestamp(row[columns["DATE"]], row[columns["TIME"]]),
                    "open": float(row[columns["OPEN"]]),
                    "high": float(row[columns["HIGH"]]),
                    "low": float(row[columns["LOW"]]),
                    "close": float(row[columns["CLOSE"]]),
                }
            except (ValueError, IndexError):
                continue


def _jsonl_records(path: Path) -> Iterator[dict]:
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def validate_m1(reconstructed_path: Path, reference_path: Path, tolerance: float = 1e-5) -> dict:
    if tolerance < 0.0:
        raise ValueError("tolerance cannot be negative")
    reconstructed = iter(_jsonl_records(Path(reconstructed_path)))
    reference = iter(_reference_bars(Path(reference_path)))
    current_reconstructed = next(reconstructed, None)
    current_reference = next(reference, None)
    report = {
        "bars_compared": 0,
        "matches": 0,
        "ohlc_differences": {name: {"count": 0, "max_abs": 0.0} for name in ("open", "high", "low", "close")},
        "missing_reconstructed": 0,
        "missing_reference": 0,
        "missing_reconstructed_samples": [],
        "missing_reference_samples": [],
        "tolerance": tolerance,
    }
    while current_reconstructed is not None or current_reference is not None:
        reconstructed_time = current_reconstructed.get("timestamp") if current_reconstructed else None
        reference_time = current_reference.get("timestamp") if current_reference else None
        if current_reconstructed is not None and current_reference is not None and reconstructed_time == reference_time:
            report["bars_compared"] += 1
            match = True
            for field_name in ("open", "high", "low", "close"):
                difference = abs(float(current_reconstructed[field_name]) - float(current_reference[field_name]))
                if difference > tolerance:
                    match = False
                    values = report["ohlc_differences"][field_name]
                    values["count"] += 1
                    values["max_abs"] = max(values["max_abs"], difference)
            if match:
                report["matches"] += 1
            current_reconstructed = next(reconstructed, None)
            current_reference = next(reference, None)
        elif current_reference is None or (
            current_reconstructed is not None and reconstructed_time < reference_time
        ):
            report["missing_reference"] += 1
            if len(report["missing_reference_samples"]) < 10:
                report["missing_reference_samples"].append(format_timestamp(reconstructed_time))
            current_reconstructed = next(reconstructed, None)
        else:
            report["missing_reconstructed"] += 1
            if len(report["missing_reconstructed_samples"]) < 10:
                report["missing_reconstructed_samples"].append(format_timestamp(reference_time))
            current_reference = next(reference, None)
    return report


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path, help="optional explicit MT5 tick CSV file(s)")
    parser.add_argument("--input-dir", type=Path, help="folder containing MT5 tick CSV files")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "output")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--chunk-size", type=int, default=50_000)
    parser.add_argument("--max-ticks", type=int)
    parser.add_argument("--validate-m1", type=Path)
    parser.add_argument("--tolerance", type=float, default=1e-5)
    return parser


def print_discovery(discovery: InputDiscovery) -> None:
    input_description = str(discovery.input_dir) if discovery.input_dir is not None else "<explicit-files>"
    print(f"[HISTORICAL] input_dir={input_description}")
    print(f"[HISTORICAL] csv_files={discovery.csv_files_found}")
    print(f"[HISTORICAL] first_timestamp={format_timestamp(discovery.first_timestamp_ms)}")
    print(f"[HISTORICAL] last_timestamp={format_timestamp(discovery.last_timestamp_ms)}")
    print(
        "[HISTORICAL] estimated_total_size="
        f"{format_size(discovery.estimated_total_size)} ({discovery.estimated_total_size} bytes)"
    )
    for issue in discovery.issues:
        print(f"[HISTORICAL][WARNING] file={issue['file']} | cause={issue['cause']}")
    for overlap in discovery.overlaps:
        print(
            "[HISTORICAL] overlap="
            f"{overlap['earlier_file']} <> {overlap['later_file']} | "
            f"{overlap['overlap_start']}..{overlap['overlap_end']}"
        )


def print_final_summary(report: dict) -> None:
    print(f"[HISTORICAL] files_processed={len(report['files_processed'])}")
    for path in report["files_processed"]:
        print(f"[HISTORICAL] processed_file={path}")
    for issue in report["runtime_file_issues"]:
        print(f"[HISTORICAL][WARNING] file={issue['file']} | cause={issue['cause']}")
    for path, reasons in report["file_discard_reasons"].items():
        detail = ",".join(f"{reason}:{count}" for reason, count in sorted(reasons.items()))
        print(f"[HISTORICAL][WARNING] file={path} | discarded_rows={detail}")
    print(f"[HISTORICAL] ticks_read={report['ticks_read']}")
    print(f"[HISTORICAL] ticks_valid={report['ticks_valid']}")
    print(f"[HISTORICAL] duplicates={report['duplicates']}")
    print(f"[HISTORICAL] first_timestamp={report['first_timestamp']}")
    print(f"[HISTORICAL] last_timestamp={report['last_timestamp']}")
    for timeframe in ("M1", "M15", "H1", "H4"):
        print(f"[HISTORICAL] bars_{timeframe}={report['bars_generated'][timeframe]}")
    print(f"[HISTORICAL] observations={report['observations_generated']}")
    print(f"[HISTORICAL] outcomes={report['outcomes_generated']}")
    print(f"[HISTORICAL] elapsed_seconds={report['elapsed_seconds']:.3f}")
    print(f"[HISTORICAL] ticks_per_second={report['ticks_per_second']:.2f}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    if not arguments.inputs and arguments.input_dir is None:
        parser.error("provide explicit CSV files or --input-dir")
    discovery = discover_inputs(arguments.inputs, arguments.input_dir, symbol=arguments.symbol)
    print_discovery(discovery)
    if not discovery.files:
        print("[HISTORICAL][ERROR] no valid XAUUSD tick CSV files to process")
        return 2
    report = replay_files(
        [item.path for item in discovery.files],
        arguments.output_dir,
        symbol=arguments.symbol,
        chunk_size=arguments.chunk_size,
        max_ticks=arguments.max_ticks,
    )
    if arguments.validate_m1:
        report["m1_validation"] = validate_m1(
            arguments.output_dir / "historical_bars" / f"{arguments.symbol}_M1.jsonl",
            arguments.validate_m1,
            arguments.tolerance,
        )
    report["input_discovery"] = {
        "csv_files_found": discovery.csv_files_found,
        "files_accepted": len(discovery.files),
        "issues": discovery.issues,
        "overlaps": discovery.overlaps,
        "estimated_total_size": discovery.estimated_total_size,
    }
    print_final_summary(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
