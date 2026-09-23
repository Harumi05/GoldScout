"""Materialize causal M5 and technical/broker D1 from persisted closed M1 only.

The historical tick CSV is never opened. Outputs and metadata live under the
Git-ignored research/output directory. Existing incompatible outputs fail
closed instead of being silently replaced.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable, Sequence

from research.broker_clock_analysis import validate_clock_report
from research.build_data_quality_manifest import percentile_linear, validate_manifest
from research.multi_strategy_causal_bars import (
    ClosedM1, ClosedM1Aggregator, CausalBar, MINUTE_MS, require_causal_feature,
)


SCHEMA_VERSION = 1
BUILDER_VERSION = "causal-m5-d1-v1"
BAR_FILES = {"M5": "XAUUSD_M5.jsonl", "D1": "XAUUSD_D1.jsonl"}
INDEX_NAME = "causal_bars_index.json"
VALIDATION_NAME = "mt5_bar_validation.json"
TIMESTAMP_UNIT = "epoch_ms_mt5_server_wall_time"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".causal-index-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_source(root: Path, manifest: dict) -> Iterable[ClosedM1]:
    source = root / "historical_bars" / "XAUUSD_M1.jsonl"
    inputs = json.loads((root / "processed_inputs.json").read_text(encoding="utf-8")).get("inputs", [])
    expected_fingerprints = {item["fingerprint"] for item in manifest["source_files"]}
    successful = [item for item in inputs if item.get("status") == "SUCCESS"]
    if ({item.get("fingerprint") for item in successful} != expected_fingerprints or
            not source.is_file() or not any(
                item.get("verified_output_files", {}).get("historical_bars/XAUUSD_M1.jsonl") == source.stat().st_size
                for item in successful
            )):
        raise ValueError("M1 source or adopted fingerprint differs from frozen evidence")
    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if (row.get("source") != "HISTORICAL_MT5_TICKS" or row.get("symbol") != "XAUUSD" or
                        row.get("timestamp_unit") != TIMESTAMP_UNIT):
                    raise ValueError("unexpected symbol/source/timestamp unit")
                bar = ClosedM1.from_replay_record(row)
                bar.validate()
                yield bar
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"XAUUSD_M1.jsonl:{line_number}: invalid closed M1: {error}") from error


def _clock_policy(clock_report: dict, dataset_id: str) -> tuple[str, str]:
    validate_clock_report(clock_report)
    if clock_report["source_manifest_id"] != dataset_id:
        raise ValueError("broker-clock report belongs to a different dataset")
    if clock_report["broker_clock_status"] == "VERIFIED":
        boundary = clock_report["broker_day_rollover"]
        # This report type is not produced by the phase-2 analyzer without
        # independent rollover proof. Validate before any future promotion.
        from research.multi_strategy_causal_bars import parse_broker_day_boundary
        parse_broker_day_boundary(boundary)
        return boundary, "VERIFIED"
    return "00:00", "UNVERIFIED"


def _record(bar: CausalBar, *, manifest_id: str, generated_at: str, rollover_status: str) -> dict:
    require_causal_feature(bar, bar.available_at, "structure")
    record = {
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "source_manifest_id": manifest_id,
        "generated_at": generated_at,
        "bar_id": f"HISTORICAL_MT5_TICKS-XAUUSD-{bar.timeframe}-{bar.start}",
        "source": "HISTORICAL_MT5_TICKS",
        "symbol": "XAUUSD",
        "timeframe": bar.timeframe,
        "timestamp_unit": TIMESTAMP_UNIT,
        "start": bar.start,
        "end": bar.end,
        "available_at": bar.available_at,
        "available_at_basis": "NEXT_CLOSED_M1_CONFIRMATION",
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "tick_count": bar.tick_count,
        "avg_spread": bar.avg_spread,
        "min_spread": bar.min_spread,
        "max_spread": bar.max_spread,
        "observed_minutes": bar.observed_minutes,
        "expected_minutes": bar.expected_minutes,
        "coverage": bar.coverage,
        "complete": bar.complete,
        "provenance": bar.provenance,
    }
    if bar.timeframe == "D1":
        record.update({
            "rollover_status": rollover_status,
            "d1_session_status": rollover_status,
            "bucket_type": "BROKER_DAY" if rollover_status == "VERIFIED" else "TECHNICAL_DATE_BUCKET",
        })
    return record


class _Statistics:
    def __init__(self, timeframe: str) -> None:
        self.timeframe = timeframe
        self.total = self.complete = self.sunday = 0
        self.coverages: list[float] = []
        self.missing_minutes: Counter[int] = Counter()
        self.examples: dict[str, int] = {}
        self.lowest_tick_count: int | None = None

    def add(self, bar: CausalBar) -> None:
        self.total += 1
        self.complete += int(bar.complete)
        self.coverages.append(bar.coverage)
        missing = bar.expected_minutes - bar.observed_minutes
        if missing:
            self.missing_minutes[missing] += 1
            self.examples.setdefault("incomplete", bar.start)
        else:
            self.examples.setdefault("complete", bar.start)
        # Decode only the MT5 server-wall calendar; this is NOT a UTC/session claim.
        weekday = datetime.fromtimestamp(bar.start / 1000, timezone.utc).weekday()
        if self.timeframe == "D1" and weekday == 6:
            self.sunday += 1
            self.examples.setdefault("technical_sunday", bar.start)
        if self.lowest_tick_count is None or bar.tick_count < self.lowest_tick_count:
            self.lowest_tick_count = bar.tick_count
            self.examples["lowest_quote_activity"] = bar.start

    def result(self) -> dict:
        values = self.coverages[:]
        return {
            "total_buckets": self.total,
            "complete_buckets": self.complete,
            "incomplete_buckets": self.total - self.complete,
            "coverage_percentiles": {
                "p50": percentile_linear(values, 0.50) if values else None,
                "p95": percentile_linear(values, 0.95) if values else None,
                "p99": percentile_linear(values, 0.99) if values else None,
                "minimum": min(values) if values else None,
            },
            "missing_minutes_distribution": dict(sorted(self.missing_minutes.items())),
            "technical_sunday_buckets": self.sunday if self.timeframe == "D1" else None,
            "sample_starts": self.examples,
        }


def _existing_index(output_dir: Path, *, dataset_id: str, boundary: str,
                    rollover_status: str, clock_status: str) -> dict | None:
    targets = [output_dir / filename for filename in BAR_FILES.values()]
    index_path = output_dir / INDEX_NAME
    if not any(path.exists() for path in (*targets, index_path)):
        return None
    if not all(path.is_file() for path in (*targets, index_path)):
        raise FileExistsError("partial causal-bar outputs exist; manual reconciliation required")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if (index.get("schema_version") != SCHEMA_VERSION or index.get("builder_version") != BUILDER_VERSION or
            index.get("source_manifest_id") != dataset_id or index.get("broker_day_boundary") != boundary or
            index.get("rollover_status") != rollover_status or
            index.get("broker_clock_status") != clock_status):
        raise FileExistsError("incompatible causal-bar outputs exist; no silent overwrite")
    for timeframe, path in zip(BAR_FILES, targets):
        entry = index.get("outputs", {}).get(timeframe, {})
        if entry.get("sha256") != _sha256_file(path) or entry.get("size_bytes") != path.stat().st_size:
            raise FileExistsError(f"existing {timeframe} output changed; no silent overwrite")
    return index


def materialize(replay_root: Path, output_dir: Path, source_manifest: dict, clock_report: dict) -> dict:
    """Build both files once; fail closed on incompatible/partial prior outputs."""
    validate_manifest(source_manifest)
    replay_root, output_dir = Path(replay_root), Path(output_dir)
    dataset_id = source_manifest["dataset_id"]
    boundary, rollover_status = _clock_policy(clock_report, dataset_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = _existing_index(output_dir, dataset_id=dataset_id, boundary=boundary,
                               rollover_status=rollover_status,
                               clock_status=clock_report["broker_clock_status"])
    if previous is not None:
        return {**previous, "skipped_identical": True}

    generated_at = _utc_now()
    builders = {"M5": ClosedM1Aggregator("M5"),
                "D1": ClosedM1Aggregator("D1", broker_day_boundary=boundary)}
    stats = {name: _Statistics(name) for name in BAR_FILES}
    streams = {}
    temporary_paths: dict[str, Path] = {}
    hashes = {name: hashlib.sha256() for name in BAR_FILES}
    gap_distribution: Counter[str] = Counter()
    last_start: int | None = None
    source_count = 0
    try:
        for timeframe in BAR_FILES:
            descriptor, temporary = tempfile.mkstemp(prefix=f".{timeframe.lower()}-", dir=output_dir)
            temporary_paths[timeframe] = Path(temporary)
            streams[timeframe] = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        for source in _read_source(replay_root, source_manifest):
            source_count += 1
            if last_start is not None:
                elapsed = (source.start - last_start) // MINUTE_MS
                if elapsed <= 0:
                    raise ValueError("M1 source is not strictly chronological")
                if elapsed > 1:
                    label = "2-5" if elapsed <= 5 else "6-60" if elapsed <= 60 else "61-1440" if elapsed <= 1440 else ">1440"
                    gap_distribution[label] += 1
            last_start = source.start
            for timeframe, builder in builders.items():
                bar = builder.push(source)
                if bar is None:
                    continue
                record = _record(bar, manifest_id=dataset_id, generated_at=generated_at,
                                 rollover_status=rollover_status)
                serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                streams[timeframe].write(serialized)
                hashes[timeframe].update(serialized.encode("utf-8"))
                stats[timeframe].add(bar)
        if source_count != source_manifest["bars_existing"]["M1"]:
            raise ValueError("M1 source count differs from frozen manifest")
        for stream in streams.values():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        streams.clear()
        outputs = {}
        for timeframe, filename in BAR_FILES.items():
            path = temporary_paths[timeframe]
            outputs[timeframe] = {
                "filename": filename,
                "sha256": "sha256:" + hashes[timeframe].hexdigest(),
                "size_bytes": path.stat().st_size,
                "count": stats[timeframe].total,
            }
        index = {
            "schema_version": SCHEMA_VERSION,
            "builder_version": BUILDER_VERSION,
            "source_manifest_id": dataset_id,
            "generated_at": generated_at,
            "broker_clock_status": clock_report["broker_clock_status"],
            "broker_day_boundary": boundary,
            "rollover_status": rollover_status,
            "source_closed_m1_count": source_count,
            "source_gap_intervals_by_elapsed_minutes": dict(sorted(gap_distribution.items())),
            "outputs": outputs,
            "quality": {timeframe: item.result() for timeframe, item in stats.items()},
            "provenance": {"source": "persisted closed M1 replay", "tick_csv_reread": False,
                           "available_at_basis": "NEXT_CLOSED_M1_CONFIRMATION"},
        }
        # The index is published last. A crash after a file replace leaves a
        # partial state that next run refuses to overwrite silently.
        for timeframe, filename in BAR_FILES.items():
            os.replace(temporary_paths[timeframe], output_dir / filename)
        _atomic_json(output_dir / INDEX_NAME, index)
        return {**index, "skipped_identical": False}
    finally:
        for stream in streams.values():
            stream.close()
        for path in temporary_paths.values():
            if path.exists():
                path.unlink()


def compare_mt5_reference(output_path: Path, reference_path: Path | None, sample_starts: Iterable[int],
                          *, tolerance: float = 0.01) -> dict:
    """Compare a small server-wall-aligned MT5 JSONL export; never force equality."""
    starts = set(sample_starts)
    if reference_path is None:
        return {"status": "NO_MT5_REFERENCE", "sample_starts": sorted(starts), "compared": 0,
                "matches": 0, "differences": [], "missing_reference": sorted(starts)}
    if tolerance < 0:
        raise ValueError("negative OHLC tolerance")
    reference = {}
    with Path(reference_path).open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            start = int(row["start"])
            if start in starts:
                if start in reference:
                    raise ValueError("duplicate MT5 reference timestamp")
                reference[start] = row
    differences, matched, compared = [], 0, 0
    with Path(output_path).open("r", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            start = row["start"]
            if start not in starts or start not in reference:
                continue
            compared += 1
            deviations = {field: row[field] - float(reference[start][field])
                          for field in ("open", "high", "low", "close")}
            if all(abs(value) <= tolerance for value in deviations.values()):
                matched += 1
            else:
                differences.append({"start": start, "ohlc_delta": deviations})
    return {"status": "COMPARED" if compared else "NO_OVERLAPPING_REFERENCE", "sample_starts": sorted(starts),
            "compared": compared, "matches": matched, "differences": differences,
            "missing_reference": sorted(starts - reference.keys())}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--replay-root", type=Path, default=base / "output")
    parser.add_argument("--output-dir", type=Path, default=base / "output" / "causal_bars")
    parser.add_argument("--source-manifest", type=Path, default=base / "data_quality_manifest.json")
    parser.add_argument("--broker-clock-report", type=Path, default=base / "output" / "broker_clock_analysis.json")
    parser.add_argument("--mt5-m5-reference", type=Path, help="Optional MT5 M5 JSONL export with server-wall start and OHLC")
    parser.add_argument("--mt5-d1-reference", type=Path, help="Optional MT5 D1 JSONL export; differing rollover is reported, not normalized")
    parser.add_argument("--ohlc-tolerance", type=float, default=0.01)
    args = parser.parse_args(argv)
    manifest = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    clock = json.loads(args.broker_clock_report.read_text(encoding="utf-8"))
    index = materialize(args.replay_root, args.output_dir, manifest, clock)
    validation = {"source_manifest_id": index["source_manifest_id"], "generated_at": _utc_now(), "timeframe_results": {}}
    for timeframe, reference in (("M5", args.mt5_m5_reference), ("D1", args.mt5_d1_reference)):
        samples = index["quality"][timeframe]["sample_starts"].values()
        validation["timeframe_results"][timeframe] = compare_mt5_reference(
            args.output_dir / BAR_FILES[timeframe], reference, samples, tolerance=args.ohlc_tolerance)
    validation["limitations"] = (
        "Sample categories use server-wall calendar, not verified exchange sessions; "
        "no MT5 reference export was supplied unless status=COMPARED."
    )
    _atomic_json(args.output_dir / VALIDATION_NAME, validation)
    print(f"[CAUSAL_BARS] source_m1={index['source_closed_m1_count']:,} "
          f"M5={index['outputs']['M5']['count']:,} D1={index['outputs']['D1']['count']:,} "
          f"rollover={index['rollover_status']} skipped={index['skipped_identical']}")
    print(f"[MT5_VALIDATION] M5={validation['timeframe_results']['M5']['status']} "
          f"D1={validation['timeframe_results']['D1']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
