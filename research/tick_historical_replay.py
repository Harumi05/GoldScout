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
from typing import Callable, Iterator, Sequence


SOURCE = "HISTORICAL_MT5_TICKS"
TIMEFRAMES = {"M1": 60, "M15": 15 * 60, "H1": 60 * 60, "H4": 4 * 60 * 60}
OBSERVATION_TIMEFRAMES = ("M15", "H1", "H4")
HORIZONS = (("15m", 15 * 60 * 1000), ("1h", 60 * 60 * 1000), ("4h", 4 * 60 * 60 * 1000))
EPOCH_ORDINAL = datetime(1970, 1, 1).toordinal()
MANIFEST_SCHEMA_VERSION = 1
OUTPUT_SCHEMA_VERSION = "historical-replay-v1"
MANIFEST_FILENAME = "processed_inputs.json"
INPUT_STATUSES = {
    "NEW",
    "PROCESSING",
    "SUCCESS",
    "FAILED",
    "CHANGED_REQUIRES_REBUILD",
    "WAITING_FOR_STABLE_FILE",
}


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
    file_stats: dict[str, dict[str, int]] = field(default_factory=dict)

    def _file(self, path: Path | str) -> dict[str, int]:
        key = str(path)
        return self.file_stats.setdefault(
            key,
            {"ticks_read": 0, "ticks_valid": 0, "ticks_discarded": 0, "duplicates": 0},
        )

    def read(self, path: Path) -> None:
        self.ticks_read += 1
        self._file(path)["ticks_read"] += 1

    def discard(self, reason: str, path: Path | None = None) -> None:
        self.ticks_discarded += 1
        self.discard_reasons[reason] = self.discard_reasons.get(reason, 0) + 1
        if path is not None:
            key = str(path)
            reasons = self.file_discard_reasons.setdefault(key, {})
            reasons[reason] = reasons.get(reason, 0) + 1
            self._file(path)["ticks_discarded"] += 1

    def optional_error(self, field_name: str) -> None:
        self.optional_field_errors[field_name] = self.optional_field_errors.get(field_name, 0) + 1

    def accept(self, tick: Tick) -> None:
        self.ticks_valid += 1
        self._file(tick.source_file)["ticks_valid"] += 1
        if self.first_timestamp_ms is None:
            self.first_timestamp_ms = tick.timestamp_ms
        self.last_timestamp_ms = tick.timestamp_ms
        if tick.flags:
            self.flags_nonempty += 1
            if tick.flags not in self.flag_samples and len(self.flag_samples) < 10:
                self.flag_samples.append(tick.flags)

    def duplicate(self, tick: Tick) -> None:
        self.duplicates += 1
        self._file(tick.source_file)["duplicates"] += 1


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


def _find_overlaps(metadata: Sequence[CsvFileMetadata]) -> list[dict]:
    ordered = sorted(
        metadata,
        key=lambda item: (item.first_timestamp_ms, item.last_timestamp_ms, str(item.path).lower()),
    )
    overlaps: list[dict] = []
    if not ordered:
        return overlaps
    covering = ordered[0]
    farthest_end = covering.last_timestamp_ms
    for item in ordered[1:]:
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
    return overlaps


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

    overlaps = _find_overlaps(metadata)
    return InputDiscovery(input_dir, len(unique_candidates), metadata, issues, overlaps)


def format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{size_bytes} B"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalized_input_path(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _modified_time(stat_result: os.stat_result) -> str:
    return datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def fingerprint_input(
    path: Path,
    block_size: int = 4 * 1024 * 1024,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """Return a full-file SHA-256 without loading the CSV into memory."""
    digest = hashlib.sha256()
    total_size = Path(path).stat().st_size
    bytes_read = 0
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(block_size)
            if not block:
                break
            digest.update(block)
            bytes_read += len(block)
            if progress is not None:
                progress(bytes_read, total_size)
    return f"sha256:{digest.hexdigest()}"


class ProcessedInputManifest:
    """Crash-safe identity and processing state for historical CSV inputs."""

    def __init__(self, path: Path, records: dict[str, dict] | None = None):
        self.path = Path(path)
        self.records = records or {}

    @classmethod
    def load(cls, path: Path) -> "ProcessedInputManifest":
        path = Path(path)
        if not path.exists():
            return cls(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReplayError(f"corrupt processed input manifest: {path}: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ReplayError(f"unsupported processed input manifest schema: {path}")
        inputs = payload.get("inputs")
        if not isinstance(inputs, list):
            raise ReplayError(f"corrupt processed input manifest inputs: {path}")
        records: dict[str, dict] = {}
        for value in inputs:
            if not isinstance(value, dict):
                raise ReplayError(f"corrupt processed input manifest record: {path}")
            normalized = value.get("normalized_path")
            status = value.get("status")
            if not isinstance(normalized, str) or not normalized or status not in INPUT_STATUSES:
                raise ReplayError(f"invalid processed input manifest record: {path}")
            records[os.path.normcase(normalized)] = dict(value)
        return cls(path, records)

    def get(self, path: Path) -> dict | None:
        return self.records.get(normalized_input_path(path))

    def set(self, record: dict) -> None:
        normalized = record.get("normalized_path")
        if not isinstance(normalized, str) or not normalized:
            raise ReplayError("manifest record has no normalized_path")
        if record.get("status") not in INPUT_STATUSES:
            raise ReplayError(f"invalid manifest status: {record.get('status')}")
        self.records[os.path.normcase(normalized)] = dict(record)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "output_version": OUTPUT_SCHEMA_VERSION,
            "updated_at": _utc_now(),
            "inputs": sorted(self.records.values(), key=lambda item: item["normalized_path"]),
        }
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def recover_interrupted(self) -> int:
        recovered = 0
        for record in self.records.values():
            if record.get("status") == "PROCESSING":
                record["status"] = "FAILED"
                record["error"] = "interrupted while PROCESSING; safe retry required"
                record["failed_at"] = _utc_now()
                recovered += 1
        if recovered:
            self.save()
        return recovered

    def success_records(self) -> list[dict]:
        return [record for record in self.records.values() if record.get("status") == "SUCCESS"]


class FileStabilityTracker:
    """Require the same size and mtime in consecutive directory observations."""

    def __init__(self, required_observations: int = 2):
        if required_observations < 2:
            raise ValueError("required_observations must be at least two")
        self.required_observations = required_observations
        self._seen: dict[str, tuple[tuple[int, int], int]] = {}

    def observe(self, path: Path) -> bool:
        stat_result = Path(path).stat()
        signature = (stat_result.st_size, stat_result.st_mtime_ns)
        key = normalized_input_path(path)
        previous = self._seen.get(key)
        count = previous[1] + 1 if previous is not None and previous[0] == signature else 1
        self._seen[key] = (signature, count)
        return count >= self.required_observations

    def forget(self, path: Path) -> None:
        self._seen.pop(normalized_input_path(path), None)

    def retain(self, paths: Sequence[Path]) -> None:
        current = {normalized_input_path(path) for path in paths}
        self._seen = {key: value for key, value in self._seen.items() if key in current}


def wait_for_stable_file(
    path: Path,
    *,
    checks: int = 2,
    interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> os.stat_result:
    if checks < 2:
        raise ValueError("checks must be at least two")
    if interval < 0.0:
        raise ValueError("stability interval cannot be negative")
    previous: tuple[int, int] | None = None
    stable_observations = 0
    latest: os.stat_result | None = None
    while stable_observations < checks:
        latest = Path(path).stat()
        signature = (latest.st_size, latest.st_mtime_ns)
        stable_observations = stable_observations + 1 if signature == previous else 1
        previous = signature
        if stable_observations < checks:
            sleep(interval)
    return latest


def _base_manifest_record(path: Path, stat_result: os.stat_result, status: str) -> dict:
    return {
        "normalized_path": normalized_input_path(path),
        "filename": Path(path).name,
        "size_bytes": stat_result.st_size,
        "modified_time": _modified_time(stat_result),
        "modified_time_ns": stat_result.st_mtime_ns,
        "fingerprint": None,
        "first_timestamp": None,
        "last_timestamp": None,
        "first_timestamp_ms": None,
        "last_timestamp_ms": None,
        "processed_at": None,
        "status": status,
        "ticks_read": 0,
        "ticks_valid": 0,
        "ticks_discarded": 0,
        "duplicates": 0,
        "output_version": OUTPUT_SCHEMA_VERSION,
        "schema_version": MANIFEST_SCHEMA_VERSION,
    }


def _record_with_metadata(record: dict, metadata: CsvFileMetadata, fingerprint: str) -> dict:
    value = dict(record)
    stat_result = metadata.path.stat()
    value.update(
        {
            "normalized_path": normalized_input_path(metadata.path),
            "filename": metadata.path.name,
            "size_bytes": stat_result.st_size,
            "modified_time": _modified_time(stat_result),
            "modified_time_ns": stat_result.st_mtime_ns,
            "fingerprint": fingerprint,
            "first_timestamp": format_timestamp(metadata.first_timestamp_ms),
            "last_timestamp": format_timestamp(metadata.last_timestamp_ms),
            "first_timestamp_ms": metadata.first_timestamp_ms,
            "last_timestamp_ms": metadata.last_timestamp_ms,
            "output_version": OUTPUT_SCHEMA_VERSION,
            "schema_version": MANIFEST_SCHEMA_VERSION,
        }
    )
    return value


def parse_expected_timestamp(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "T" not in text:
        raise ReplayError(f"expected ISO timestamp with T separator: {value}")
    date_value, time_value = text.split("T", 1)
    try:
        return parse_mt5_timestamp(date_value, time_value)
    except (TypeError, ValueError) as error:
        raise ReplayError(f"invalid expected timestamp: {value}") from error


def _jsonl_edges(path: Path) -> tuple[dict, dict]:
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ReplayError(f"required historical output is missing or empty: {path}")
    first_line = None
    with path.open("r", encoding="utf-8-sig", errors="strict") as stream:
        for line in stream:
            if line.strip():
                first_line = line
                break
    last_line = next(_reverse_lines(path), None)
    if first_line is None or last_line is None:
        raise ReplayError(f"required historical output has no records: {path}")
    try:
        first = json.loads(first_line)
        last = json.loads(last_line)
    except json.JSONDecodeError as error:
        raise ReplayError(f"invalid JSONL edge record in historical output {path}: {error}") from error
    if not isinstance(first, dict) or not isinstance(last, dict):
        raise ReplayError(f"invalid JSONL object in historical output: {path}")
    return first, last


def verify_existing_historical_outputs(
    output_dir: Path,
    *,
    symbol: str,
    expected_first_ms: int,
    expected_last_ms: int,
) -> dict[str, int]:
    output_dir = Path(output_dir)
    required = {
        f"historical_bars/{symbol}_M1.jsonl": ("bar", "M1"),
        f"historical_bars/{symbol}_M15.jsonl": ("bar", "M15"),
        f"historical_bars/{symbol}_H1.jsonl": ("bar", "H1"),
        f"historical_bars/{symbol}_H4.jsonl": ("bar", "H4"),
        "historical_observations.jsonl": ("observation", None),
        "historical_outcomes.jsonl": ("outcome", None),
    }
    verified: dict[str, int] = {}
    m1_first: dict | None = None
    m1_last: dict | None = None
    for relative, (kind, timeframe) in required.items():
        path = output_dir / relative
        first, last = _jsonl_edges(path)
        for record in (first, last):
            if record.get("source") != SOURCE:
                raise ReplayError(f"unexpected source in historical output: {path}")
            if kind == "bar":
                if record.get("symbol") != symbol or record.get("timeframe") != timeframe:
                    raise ReplayError(f"unexpected bar contract in historical output: {path}")
            elif record.get("observer_only") is not True or record.get("score_effect") != 0:
                raise ReplayError(f"historical output is not observation-only: {path}")
        if timeframe == "M1":
            m1_first, m1_last = first, last
        verified[relative] = path.stat().st_size

    assert m1_first is not None and m1_last is not None
    expected_first_bucket = expected_first_ms // 60_000 * 60_000
    expected_last_closed_at = expected_last_ms // 60_000 * 60_000
    actual_first = m1_first.get("timestamp")
    actual_last_closed = m1_last.get("close_timestamp")
    if not isinstance(actual_first, int) or not isinstance(actual_last_closed, int):
        raise ReplayError("historical M1 output has invalid timestamp fields")
    if actual_first != expected_first_bucket:
        raise ReplayError(
            "historical M1 output starts outside the adopted input range: "
            f"expected {format_timestamp(expected_first_bucket)}, "
            f"found {format_timestamp(actual_first)}"
        )
    if actual_last_closed != expected_last_closed_at:
        raise ReplayError(
            "historical M1 output ends outside the adopted input range: "
            f"expected closed through {format_timestamp(expected_last_closed_at)}, "
            f"found {format_timestamp(actual_last_closed)}"
        )
    return verified


def adopt_existing_input(
    *,
    input_dir: Path,
    output_dir: Path,
    expected_first_timestamp: str,
    expected_last_timestamp: str,
    expected_size_bytes: int,
    expected_ticks_valid: int,
    symbol: str = "XAUUSD",
    adopt_file: Path | None = None,
    expected_fingerprint: str | None = None,
    stability_checks: int = 2,
    stability_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    emit_logs: bool = True,
) -> dict:
    """Adopt a validated legacy full-run without replaying any tick."""
    if expected_size_bytes <= 0 or expected_ticks_valid <= 0:
        raise ReplayError("expected size and ticks_valid must be positive")
    expected_first_ms = parse_expected_timestamp(expected_first_timestamp)
    expected_last_ms = parse_expected_timestamp(expected_last_timestamp)
    if expected_last_ms < expected_first_ms:
        raise ReplayError("expected last timestamp precedes first timestamp")

    manifest = ProcessedInputManifest.load(Path(output_dir) / MANIFEST_FILENAME)
    if adopt_file is not None:
        candidates = [Path(adopt_file)]
    else:
        candidates = _candidate_paths((), Path(input_dir))
    accepted: list[CsvFileMetadata] = []
    rejected: list[str] = []
    for path in candidates:
        try:
            accepted.append(inspect_tick_csv(path, symbol))
        except (OSError, ReplayError) as error:
            rejected.append(f"{path}: {error}")
    if len(accepted) != 1:
        detail = "; ".join(rejected) if rejected else "no unique candidate"
        raise ReplayError(f"adoption requires exactly one valid {symbol} tick CSV; found {len(accepted)}: {detail}")
    metadata = accepted[0]
    path = metadata.path
    stat_result = path.stat()
    existing = manifest.get(path)
    already_adopted = False
    if existing is not None:
        if (
            existing.get("status") == "SUCCESS"
            and existing.get("adopted_existing_output") is True
            and existing.get("size_bytes") == stat_result.st_size
            and existing.get("modified_time_ns") == stat_result.st_mtime_ns
            and existing.get("first_timestamp_ms") == expected_first_ms
            and existing.get("last_timestamp_ms") == expected_last_ms
            and existing.get("ticks_valid") == expected_ticks_valid
        ):
            already_adopted = True
        else:
            raise ReplayError(f"manifest already contains a non-matching record for adopted input: {path}")

    if metadata.size_bytes != expected_size_bytes:
        raise ReplayError(
            f"input size mismatch: expected {expected_size_bytes}, found {metadata.size_bytes}"
        )
    if metadata.first_timestamp_ms != expected_first_ms:
        raise ReplayError(
            "input first timestamp mismatch: "
            f"expected {format_timestamp(expected_first_ms)}, found {format_timestamp(metadata.first_timestamp_ms)}"
        )
    if metadata.last_timestamp_ms != expected_last_ms:
        raise ReplayError(
            "input last timestamp mismatch: "
            f"expected {format_timestamp(expected_last_ms)}, found {format_timestamp(metadata.last_timestamp_ms)}"
        )
    verified_outputs = verify_existing_historical_outputs(
        output_dir,
        symbol=symbol,
        expected_first_ms=expected_first_ms,
        expected_last_ms=expected_last_ms,
    )
    if already_adopted:
        if emit_logs:
            print(f"[HISTORICAL] already_adopted: {path.name}")
        return {
            "adopted": False,
            "already_adopted": True,
            "manifest": str(manifest.path),
            "input": str(path),
            "fingerprint": existing.get("fingerprint"),
            "outputs_verified": verified_outputs,
        }
    stable_stat = wait_for_stable_file(
        path,
        checks=stability_checks,
        interval=stability_interval,
        sleep=sleep,
    )
    if stable_stat.st_size != expected_size_bytes:
        raise ReplayError("input size changed while preparing adoption")

    last_reported = -1

    def report_fingerprint(bytes_read: int, total_size: int) -> None:
        nonlocal last_reported
        if not emit_logs or total_size <= 0:
            return
        percent = min(100, int(bytes_read * 100 / total_size))
        step = percent // 10 * 10
        if step > last_reported:
            last_reported = step
            print(f"[HISTORICAL] fingerprint_progress={step}%")

    actual_fingerprint = fingerprint_input(path, progress=report_fingerprint)
    verified_stat = path.stat()
    if (stable_stat.st_size, stable_stat.st_mtime_ns) != (
        verified_stat.st_size,
        verified_stat.st_mtime_ns,
    ):
        raise ReplayError("input changed while calculating adoption fingerprint")
    if expected_fingerprint is not None and actual_fingerprint.lower() != expected_fingerprint.lower():
        raise ReplayError(
            f"input fingerprint mismatch: expected {expected_fingerprint}, found {actual_fingerprint}"
        )

    record = _record_with_metadata(
        _base_manifest_record(path, verified_stat, "SUCCESS"),
        metadata,
        actual_fingerprint,
    )
    record.update(
        {
            "status": "SUCCESS",
            "processed_at": _utc_now(),
            "ticks_read": None,
            "ticks_valid": expected_ticks_valid,
            "ticks_discarded": None,
            "duplicates": None,
            "adopted_existing_output": True,
            "verified_output_files": verified_outputs,
        }
    )
    manifest.set(record)
    manifest.save()
    if emit_logs:
        print(f"[HISTORICAL] adopted_existing_input={path}")
        print(f"[HISTORICAL] fingerprint={actual_fingerprint}")
        print(f"[HISTORICAL] status=SUCCESS | ticks_valid={expected_ticks_valid}")
    return {
        "adopted": True,
        "already_adopted": False,
        "manifest": str(manifest.path),
        "input": str(path),
        "fingerprint": actual_fingerprint,
        "first_timestamp": format_timestamp(metadata.first_timestamp_ms),
        "last_timestamp": format_timestamp(metadata.last_timestamp_ms),
        "size_bytes": metadata.size_bytes,
        "ticks_valid": expected_ticks_valid,
        "outputs_verified": verified_outputs,
    }


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
                self.stats.read(self.path)
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
            self.stats.read(self.path)
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
                    stats.duplicate(tick)
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
        "file_stats": stats.file_stats,
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


def _candidate_paths(explicit_files: Sequence[Path], input_dir: Path | None) -> list[Path]:
    candidates = [Path(path) for path in explicit_files]
    if input_dir is not None:
        directory = Path(input_dir)
        if not directory.is_dir():
            raise ReplayError(f"input directory does not exist: {directory}")
        candidates.extend(
            path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".csv"
        )
    unique: dict[str, Path] = {}
    for path in candidates:
        unique.setdefault(normalized_input_path(path), path)
    return sorted(unique.values(), key=lambda path: normalized_input_path(path))


def _metadata_from_record(record: dict) -> CsvFileMetadata | None:
    first = record.get("first_timestamp_ms")
    last = record.get("last_timestamp_ms")
    size = record.get("size_bytes")
    normalized = record.get("normalized_path")
    if not isinstance(first, int) or not isinstance(last, int) or not isinstance(size, int):
        return None
    if not isinstance(normalized, str) or not normalized:
        return None
    return CsvFileMetadata(Path(normalized), size, first, last)


def _ranges_overlap(first: CsvFileMetadata, second: CsvFileMetadata) -> bool:
    return first.first_timestamp_ms <= second.last_timestamp_ms and second.first_timestamp_ms <= first.last_timestamp_ms


def _affected_range(previous: dict, current: CsvFileMetadata) -> dict:
    previous_first = previous.get("first_timestamp_ms")
    previous_last = previous.get("last_timestamp_ms")
    starts = [current.first_timestamp_ms]
    ends = [current.last_timestamp_ms]
    if isinstance(previous_first, int):
        starts.append(previous_first)
    if isinstance(previous_last, int):
        ends.append(previous_last)
    return {"start": format_timestamp(min(starts)), "end": format_timestamp(max(ends))}


def _mark_waiting(
    manifest: ProcessedInputManifest,
    path: Path,
    stat_result: os.stat_result,
    previous: dict | None,
) -> None:
    if previous is None:
        record = _base_manifest_record(path, stat_result, "WAITING_FOR_STABLE_FILE")
        record["waiting_previous_status"] = "NEW"
    else:
        record = dict(previous)
        record["status"] = "WAITING_FOR_STABLE_FILE"
        record["waiting_previous_status"] = previous.get("waiting_previous_status", previous.get("status"))
        record["waiting_size_bytes"] = stat_result.st_size
        record["waiting_modified_time"] = _modified_time(stat_result)
        record["waiting_modified_time_ns"] = stat_result.st_mtime_ns
    manifest.set(record)
    manifest.save()


def _is_stable(
    path: Path,
    *,
    stability_tracker: FileStabilityTracker | None,
    stability_checks: int,
    stability_interval: float,
    sleep: Callable[[float], None],
) -> bool:
    if stability_tracker is not None:
        return stability_tracker.observe(path)
    wait_for_stable_file(
        path,
        checks=stability_checks,
        interval=stability_interval,
        sleep=sleep,
    )
    return True


def _print_incremental_before(counts: dict[str, int]) -> None:
    for name in ("total_csv", "new_files", "unchanged_files", "changed_files", "failed_files"):
        print(f"[HISTORICAL] {name}={counts[name]}")


def _print_incremental_after(summary: dict) -> None:
    print(f"[HISTORICAL] processed_now={summary['processed_now']}")
    print(f"[HISTORICAL] skipped={summary['skipped']}")
    print(f"[HISTORICAL] failed_now={summary['failed_now']}")
    print(f"[HISTORICAL] new_range_added={summary['new_range_added']}")
    print(f"[HISTORICAL] overlap_detected={summary['overlap_detected']}")
    print(f"[HISTORICAL] total_success_files={summary['total_success_files']}")


def incremental_replay_once(
    *,
    input_dir: Path | None,
    output_dir: Path,
    explicit_files: Sequence[Path] = (),
    symbol: str = "XAUUSD",
    chunk_size: int = 50_000,
    max_ticks: int | None = None,
    stability_checks: int = 2,
    stability_interval: float = 1.0,
    stability_tracker: FileStabilityTracker | None = None,
    sleep: Callable[[float], None] = time.sleep,
    emit_logs: bool = True,
) -> dict:
    """Process only stable NEW/FAILED inputs and atomically record file state."""
    output_dir = Path(output_dir)
    manifest = ProcessedInputManifest.load(output_dir / MANIFEST_FILENAME)
    interrupted = manifest.recover_interrupted()
    candidates = _candidate_paths(explicit_files, input_dir)
    if stability_tracker is not None:
        stability_tracker.retain(candidates)
    counts = {
        "total_csv": len(candidates),
        "new_files": 0,
        "unchanged_files": 0,
        "changed_files": 0,
        "failed_files": 0,
    }
    processable: list[tuple[CsvFileMetadata, str, dict]] = []
    accepted_metadata: list[CsvFileMetadata] = []
    issues: list[dict] = []
    overlap_rows: list[dict] = []
    skipped = 0
    failed_now = 0

    successful_metadata = [
        metadata
        for record in manifest.success_records()
        if (metadata := _metadata_from_record(record)) is not None
    ]

    for path in candidates:
        normalized = normalized_input_path(path)
        previous = manifest.get(path)
        try:
            stat_result = path.stat()
        except OSError as error:
            counts["failed_files"] += 1
            failed_now += 1
            if emit_logs:
                print(f"[FAILED] {path} | {error}")
            continue

        previous_status = previous.get("status") if previous is not None else None
        if previous_status == "WAITING_FOR_STABLE_FILE":
            previous_status = previous.get("waiting_previous_status", "NEW")
        same_stat = bool(
            previous is not None
            and previous.get("size_bytes") == stat_result.st_size
            and previous.get("modified_time_ns") == stat_result.st_mtime_ns
        )
        if previous_status == "CHANGED_REQUIRES_REBUILD" and previous is not None and same_stat:
            counts["changed_files"] += 1
            metadata = _metadata_from_record(previous)
            if metadata is not None:
                accepted_metadata.append(metadata)
            if emit_logs:
                print(f"[CHANGED] rebuild_required={path}")
            continue
        if previous_status == "SUCCESS" and previous is not None:
            if same_stat:
                counts["unchanged_files"] += 1
                skipped += 1
                metadata = _metadata_from_record(previous)
                if metadata is not None:
                    accepted_metadata.append(metadata)
                if emit_logs:
                    print(f"[UNCHANGED] {path}")
                    print(f"[HISTORICAL] SKIP already_processed: {path.name}")
                continue

        classification = "FAILED" if previous_status in {"FAILED", "PROCESSING"} else "NEW"
        if previous_status in {"SUCCESS", "CHANGED_REQUIRES_REBUILD"}:
            classification = "CHANGED"
        counts[
            "failed_files" if classification == "FAILED" else "changed_files" if classification == "CHANGED" else "new_files"
        ] += 1
        if emit_logs:
            print(f"[{classification}] {path}")
        if previous is None:
            previous = _base_manifest_record(path, stat_result, "NEW")
            manifest.set(previous)
            manifest.save()

        try:
            stable = _is_stable(
                path,
                stability_tracker=stability_tracker,
                stability_checks=stability_checks,
                stability_interval=stability_interval,
                sleep=sleep,
            )
        except OSError as error:
            record = dict(previous) if previous is not None else _base_manifest_record(path, stat_result, "FAILED")
            record.update({"status": "FAILED", "error": str(error), "failed_at": _utc_now()})
            manifest.set(record)
            manifest.save()
            failed_now += 1
            issues.append({"file": str(path), "cause": str(error)})
            continue
        if not stable:
            _mark_waiting(manifest, path, stat_result, previous)
            if emit_logs:
                print(f"[HISTORICAL] WAITING_FOR_STABLE_FILE: {path.name}")
            continue

        try:
            stable_stat = path.stat()
            current_fingerprint = fingerprint_input(path)
            metadata = inspect_tick_csv(path, symbol)
            verified_stat = path.stat()
        except (OSError, ReplayError) as error:
            record = dict(previous) if previous is not None else _base_manifest_record(path, stat_result, "FAILED")
            record.update(
                {
                    "normalized_path": normalized,
                    "filename": path.name,
                    "size_bytes": stat_result.st_size,
                    "modified_time": _modified_time(stat_result),
                    "modified_time_ns": stat_result.st_mtime_ns,
                    "status": "FAILED",
                    "error": str(error),
                    "failed_at": _utc_now(),
                }
            )
            manifest.set(record)
            manifest.save()
            failed_now += 1
            issues.append({"file": str(path), "cause": str(error)})
            if emit_logs:
                print(f"[HISTORICAL][WARNING] file={path} | cause={error}")
            continue
        if (stable_stat.st_size, stable_stat.st_mtime_ns) != (
            verified_stat.st_size,
            verified_stat.st_mtime_ns,
        ):
            _mark_waiting(manifest, path, verified_stat, previous)
            if emit_logs:
                print(f"[HISTORICAL] WAITING_FOR_STABLE_FILE changed_during_fingerprint: {path.name}")
            continue

        if previous_status == "SUCCESS" and previous is not None:
            if previous.get("fingerprint") == current_fingerprint:
                record = _record_with_metadata(previous, metadata, current_fingerprint)
                record["status"] = "SUCCESS"
                record.pop("waiting_previous_status", None)
                record.pop("error", None)
                manifest.set(record)
                manifest.save()
                counts["changed_files"] -= 1
                counts["unchanged_files"] += 1
                skipped += 1
                accepted_metadata.append(metadata)
                if emit_logs:
                    print(f"[UNCHANGED] fingerprint match: {path}")
                    print(f"[HISTORICAL] SKIP already_processed: {path.name}")
                continue
            record = _record_with_metadata(previous, metadata, current_fingerprint)
            record["status"] = "CHANGED_REQUIRES_REBUILD"
            record["previous_fingerprint"] = previous.get("fingerprint")
            record["affected_range"] = _affected_range(previous, metadata)
            record["error"] = "processed input changed; append-only outputs require rebuild"
            manifest.set(record)
            manifest.save()
            accepted_metadata.append(metadata)
            if emit_logs:
                affected = record["affected_range"]
                print(f"[CHANGED] rebuild_required={path} | range={affected['start']}..{affected['end']}")
            continue
        if previous_status == "CHANGED_REQUIRES_REBUILD":
            record = _record_with_metadata(previous or {}, metadata, current_fingerprint)
            record["status"] = "CHANGED_REQUIRES_REBUILD"
            record["affected_range"] = _affected_range(previous or {}, metadata)
            record["error"] = "processed input changed; append-only outputs require rebuild"
            manifest.set(record)
            manifest.save()
            accepted_metadata.append(metadata)
            continue

        record = _record_with_metadata(
            previous or _base_manifest_record(path, stat_result, classification),
            metadata,
            current_fingerprint,
        )
        record["status"] = classification
        record.pop("waiting_previous_status", None)
        record.pop("error", None)

        duplicate_of = next(
            (
                existing
                for existing in manifest.success_records()
                if existing.get("fingerprint") == current_fingerprint
                and existing.get("first_timestamp_ms") == metadata.first_timestamp_ms
                and existing.get("last_timestamp_ms") == metadata.last_timestamp_ms
            ),
            None,
        )
        if duplicate_of is not None:
            record.update(
                {
                    "status": "SUCCESS",
                    "processed_at": _utc_now(),
                    "duplicate_of": duplicate_of["normalized_path"],
                    "ticks_read": 0,
                    "ticks_valid": 0,
                    "ticks_discarded": 0,
                    "duplicates": 0,
                }
            )
            manifest.set(record)
            manifest.save()
            skipped += 1
            accepted_metadata.append(metadata)
            overlap_rows.extend(_find_overlaps([_metadata_from_record(duplicate_of), metadata]))
            if emit_logs:
                print(f"[HISTORICAL] SKIP duplicate_input: {path.name}")
            continue

        prior_overlap = next((existing for existing in successful_metadata if _ranges_overlap(existing, metadata)), None)
        if prior_overlap is not None:
            overlap = _find_overlaps([prior_overlap, metadata])[0]
            overlap_rows.append(overlap)
            record["status"] = "CHANGED_REQUIRES_REBUILD"
            record["affected_range"] = {
                "start": overlap["overlap_start"],
                "end": overlap["overlap_end"],
            }
            record["error"] = "new input overlaps finalized append-only history; rebuild required"
            manifest.set(record)
            manifest.save()
            accepted_metadata.append(metadata)
            counts["new_files"] -= 1
            counts["changed_files"] += 1
            if emit_logs:
                print(
                    f"[CHANGED] overlap_requires_rebuild={path} | "
                    f"range={overlap['overlap_start']}..{overlap['overlap_end']}"
                )
            continue

        processable.append((metadata, classification, record))
        accepted_metadata.append(metadata)

    new_overlaps = _find_overlaps([item[0] for item in processable])
    overlap_rows.extend(new_overlaps)
    discovery = InputDiscovery(
        Path(input_dir) if input_dir is not None else None,
        len(candidates),
        sorted(
            accepted_metadata,
            key=lambda item: (item.first_timestamp_ms, item.last_timestamp_ms, str(item.path).lower()),
        ),
        issues,
        overlap_rows,
    )
    if emit_logs:
        _print_incremental_before(counts)
        print_discovery(discovery)

    replay_report: dict | None = None
    processed_now = 0
    successful_now: list[CsvFileMetadata] = []
    if processable:
        for metadata, _, record in processable:
            value = dict(record)
            value.update(
                {
                    "status": "PROCESSING",
                    "processing_started_at": _utc_now(),
                    "error": None,
                }
            )
            manifest.set(value)
        manifest.save()
        try:
            ordered = sorted(
                (item[0] for item in processable),
                key=lambda item: (item.first_timestamp_ms, item.last_timestamp_ms, str(item.path).lower()),
            )
            replay_report = replay_files(
                [metadata.path for metadata in ordered],
                output_dir,
                symbol=symbol,
                chunk_size=chunk_size,
                max_ticks=max_ticks,
            )
            runtime_errors = {
                normalized_input_path(Path(issue["file"])): issue["cause"]
                for issue in replay_report["runtime_file_issues"]
            }
            partial_error = "max_ticks stopped before complete input; safe retry required" if max_ticks is not None else None
            for metadata, _, _ in processable:
                current = manifest.get(metadata.path) or {}
                file_stats = replay_report["file_stats"].get(
                    str(metadata.path),
                    {"ticks_read": 0, "ticks_valid": 0, "ticks_discarded": 0, "duplicates": 0},
                )
                error = runtime_errors.get(normalized_input_path(metadata.path)) or partial_error
                changed_during_replay = False
                if error is None:
                    try:
                        final_stat = metadata.path.stat()
                        changed_during_replay = (
                            current.get("size_bytes") != final_stat.st_size
                            or current.get("modified_time_ns") != final_stat.st_mtime_ns
                        )
                    except OSError as stat_error:
                        error = str(stat_error)
                if changed_during_replay:
                    error = "input changed during replay; append-only outputs require rebuild"
                current.update(file_stats)
                current["processed_at"] = _utc_now()
                current.pop("processing_started_at", None)
                if error is None:
                    current["status"] = "SUCCESS"
                    current.pop("error", None)
                    processed_now += 1
                    successful_now.append(metadata)
                else:
                    current["status"] = (
                        "CHANGED_REQUIRES_REBUILD" if changed_during_replay else "FAILED"
                    )
                    current["error"] = error
                    current["failed_at"] = _utc_now()
                    failed_now += 1
                manifest.set(current)
            manifest.save()
        except Exception as error:
            for metadata, _, _ in processable:
                current = manifest.get(metadata.path) or {}
                current.update({"status": "FAILED", "error": str(error), "failed_at": _utc_now()})
                current.pop("processing_started_at", None)
                manifest.set(current)
            manifest.save()
            failed_now += len(processable)
            issues.append({"file": "<replay>", "cause": str(error)})
            if emit_logs:
                print(f"[HISTORICAL][ERROR] replay failed safely: {error}")

    if successful_now:
        new_range_added = (
            f"{format_timestamp(min(item.first_timestamp_ms for item in successful_now))}.."
            f"{format_timestamp(max(item.last_timestamp_ms for item in successful_now))}"
        )
    else:
        new_range_added = "NONE"
    summary = {
        "manifest": str(manifest.path),
        "interrupted_recovered": interrupted,
        "counts_before": counts,
        "processed_now": processed_now,
        "skipped": skipped,
        "failed_now": failed_now,
        "new_range_added": new_range_added,
        "overlap_detected": bool(overlap_rows),
        "overlaps": overlap_rows,
        "total_success_files": len(manifest.success_records()),
        "issues": issues,
        "replay_report": replay_report,
    }
    if emit_logs:
        _print_incremental_after(summary)
        if replay_report is not None:
            print_final_summary(replay_report)
    return summary


def watch_input_directory(
    *,
    input_dir: Path,
    output_dir: Path,
    symbol: str = "XAUUSD",
    chunk_size: int = 50_000,
    watch_interval: float = 60.0,
    max_cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    emit_logs: bool = True,
) -> list[dict]:
    if watch_interval <= 0.0:
        raise ValueError("watch_interval must be positive")
    tracker = FileStabilityTracker(required_observations=2)
    reports: list[dict] = []
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        reports.append(
            incremental_replay_once(
                input_dir=input_dir,
                output_dir=output_dir,
                symbol=symbol,
                chunk_size=chunk_size,
                stability_tracker=tracker,
                stability_interval=0.0,
                sleep=sleep,
                emit_logs=emit_logs,
            )
        )
        cycles += 1
        if max_cycles is None or cycles < max_cycles:
            sleep(watch_interval)
    return reports


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
    parser.add_argument(
        "--adopt-existing-input",
        action="store_true",
        help="register one verified legacy full-run input as SUCCESS without replaying ticks",
    )
    parser.add_argument("--adopt-file", type=Path, help="explicit CSV to adopt when the directory is ambiguous")
    parser.add_argument("--expected-first-timestamp")
    parser.add_argument("--expected-last-timestamp")
    parser.add_argument("--expected-size-bytes", type=int)
    parser.add_argument("--expected-ticks-valid", type=int)
    parser.add_argument("--expected-fingerprint", help="optional sha256:... value to verify during adoption")
    parser.add_argument("--watch", action="store_true", help="keep watching --input-dir for stable new CSV files")
    parser.add_argument("--watch-interval", type=float, default=60.0, help="seconds between watch scans")
    parser.add_argument(
        "--stability-check-interval",
        type=float,
        default=1.0,
        help="seconds between the two stable size/mtime checks in one-shot mode",
    )
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
    if arguments.adopt_existing_input:
        if arguments.input_dir is None:
            parser.error("--adopt-existing-input requires --input-dir")
        if arguments.watch:
            parser.error("--adopt-existing-input cannot be combined with --watch")
        required = {
            "--expected-first-timestamp": arguments.expected_first_timestamp,
            "--expected-last-timestamp": arguments.expected_last_timestamp,
            "--expected-size-bytes": arguments.expected_size_bytes,
            "--expected-ticks-valid": arguments.expected_ticks_valid,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser.error(f"--adopt-existing-input requires {', '.join(missing)}")
        try:
            result = adopt_existing_input(
                input_dir=arguments.input_dir,
                output_dir=arguments.output_dir,
                adopt_file=arguments.adopt_file,
                symbol=arguments.symbol,
                expected_first_timestamp=arguments.expected_first_timestamp,
                expected_last_timestamp=arguments.expected_last_timestamp,
                expected_size_bytes=arguments.expected_size_bytes,
                expected_ticks_valid=arguments.expected_ticks_valid,
                expected_fingerprint=arguments.expected_fingerprint,
                stability_interval=arguments.stability_check_interval,
            )
        except (OSError, ReplayError, ValueError) as error:
            print(f"[HISTORICAL][ERROR] adoption aborted: {error}")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if arguments.watch and arguments.input_dir is None:
        parser.error("--watch requires --input-dir")
    if arguments.watch:
        try:
            watch_input_directory(
                input_dir=arguments.input_dir,
                output_dir=arguments.output_dir,
                symbol=arguments.symbol,
                chunk_size=arguments.chunk_size,
                watch_interval=arguments.watch_interval,
            )
        except KeyboardInterrupt:
            print("[HISTORICAL] watch stopped")
        except (OSError, ReplayError, ValueError) as error:
            print(f"[HISTORICAL][ERROR] {error}")
            return 2
        return 0
    if arguments.input_dir is not None:
        try:
            summary = incremental_replay_once(
                input_dir=arguments.input_dir,
                explicit_files=arguments.inputs,
                output_dir=arguments.output_dir,
                symbol=arguments.symbol,
                chunk_size=arguments.chunk_size,
                max_ticks=arguments.max_ticks,
                stability_interval=arguments.stability_check_interval,
            )
        except (OSError, ReplayError, ValueError) as error:
            print(f"[HISTORICAL][ERROR] {error}")
            return 2
        if arguments.validate_m1:
            validation = validate_m1(
                arguments.output_dir / "historical_bars" / f"{arguments.symbol}_M1.jsonl",
                arguments.validate_m1,
                arguments.tolerance,
            )
            print(json.dumps({"incremental": summary, "m1_validation": validation}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
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
