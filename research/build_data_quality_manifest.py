"""Freeze evidence from adopted replay outputs without reopening the tick CSV.

The committed JSON is sanitized: source filenames and hashes, not local paths.
`research/output/` remains an ignored place for machine-specific regenerated copies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Sequence

from research.multi_strategy_causal_bars import ClosedM1, ClosedM1Aggregator, MINUTE_MS


SCHEMA_VERSION = 1
TIMEFRAMES = ("M1", "M15", "H1", "H4")
LOW_ACTIVITY_MAX_TICKS = 5


def percentile_linear(values: list[float], fraction: float) -> float:
    """Unweighted linear interpolation at index (n-1)*p, after sorting."""
    if not values or not 0.0 <= fraction <= 1.0:
        raise ValueError("percentile requires nonempty values and 0<=p<=1")
    values.sort()
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _records(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except (ValueError, TypeError) as error:
                    raise ValueError(f"{path.name}:{line_number}: invalid JSON") from error


def _source_metadata(replay_root: Path) -> tuple[dict, list[dict]]:
    index = json.loads((replay_root / "processed_inputs.json").read_text(encoding="utf-8"))
    inputs = index.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("processed-input manifest has no inputs")
    if any(item.get("status") != "SUCCESS" or not str(item.get("fingerprint", "")).startswith("sha256:")
           for item in inputs):
        raise ValueError("only verified SUCCESS inputs may be frozen")
    for item in inputs:
        verified = item.get("verified_output_files", {})
        if not isinstance(verified, dict):
            raise ValueError("verified output size map missing")
        for timeframe in TIMEFRAMES:
            relative = f"historical_bars/XAUUSD_{timeframe}.jsonl"
            path = replay_root / relative
            if not path.is_file() or verified.get(relative) != path.stat().st_size:
                raise ValueError(f"missing or changed verified output: {relative}")
    return index, inputs


def build_manifest(replay_root: Path) -> dict:
    """Stream the four persisted bar files; never read or hash raw tick bytes."""
    replay_root = Path(replay_root)
    index, inputs = _source_metadata(replay_root)
    counts: dict[str, int] = {}
    for timeframe in TIMEFRAMES[1:]:
        counts[timeframe] = sum(1 for _ in _records(replay_root / "historical_bars" / f"XAUUSD_{timeframe}.jsonl"))

    spread_values: list[float] = []
    activity_values: list[float] = []
    maximum_tick_spread = 0.0
    low_activity = 0
    gap_gt_1m = gap_gt_1h = gap_gt_1d = max_gap_minutes = 0
    last_start: int | None = None
    m5 = ClosedM1Aggregator("M5")
    # 00:00 is a technical server-wall date bucket only, not verified rollover/UTC.
    d1 = ClosedM1Aggregator("D1", broker_day_boundary="00:00")
    derived = {"M5": {"closed_buckets": 0, "complete_buckets": 0, "sparse_buckets": 0},
               "D1": {"closed_buckets": 0, "complete_buckets": 0, "sparse_buckets": 0}}
    for record in _records(replay_root / "historical_bars" / "XAUUSD_M1.jsonl"):
        bar = ClosedM1.from_replay_record(record)
        bar.validate()
        if last_start is not None:
            elapsed = bar.start - last_start
            if elapsed <= 0:
                raise ValueError("M1 output is not strictly chronological")
            if elapsed > MINUTE_MS:
                gap_gt_1m += 1
                gap_gt_1h += elapsed > 60 * MINUTE_MS
                gap_gt_1d += elapsed > 24 * 60 * MINUTE_MS
                max_gap_minutes = max(max_gap_minutes, elapsed // MINUTE_MS)
        last_start = bar.start
        spread_values.append(bar.avg_spread)
        activity_values.append(float(bar.tick_count))
        maximum_tick_spread = max(maximum_tick_spread, bar.max_spread)
        low_activity += bar.tick_count <= LOW_ACTIVITY_MAX_TICKS
        for timeframe, builder in (("M5", m5), ("D1", d1)):
            emitted = builder.push(bar)
            if emitted is not None:
                derived[timeframe]["closed_buckets"] += 1
                derived[timeframe]["complete_buckets" if emitted.complete else "sparse_buckets"] += 1
    counts["M1"] = len(spread_values)
    if not counts["M1"]:
        raise ValueError("M1 output is empty")
    for timeframe, builder in (("M5", m5), ("D1", d1)):
        derived[timeframe]["unclosed_final_bucket"] = int(builder.pending_minutes > 0)
        derived[timeframe]["materialized"] = False

    source_files = [
        {"filename": item["filename"], "size_bytes": item["size_bytes"],
         "modified_time": item["modified_time"], "fingerprint": item["fingerprint"],
         "status": item["status"], "adopted_existing_output": bool(item.get("adopted_existing_output", False))}
        for item in sorted(inputs, key=lambda value: value["first_timestamp"])
    ]
    fingerprints = [item["fingerprint"] for item in source_files]
    fingerprint = fingerprints[0] if len(fingerprints) == 1 else "sha256:" + hashlib.sha256(
        "|".join(fingerprints).encode("ascii")
    ).hexdigest()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "XAUUSD_MT5_TICKS_" + fingerprint.split(":", 1)[1][:16],
        "fingerprint": fingerprint,
        "source": "HISTORICAL_MT5_TICKS",
        "source_files": source_files,
        "date_start": min(item["first_timestamp"] for item in inputs),
        "date_end": max(item["last_timestamp"] for item in inputs),
        "ticks": {
            "valid": sum(int(item["ticks_valid"]) for item in inputs),
            "read": None if any(item.get("ticks_read") is None for item in inputs)
                    else sum(int(item["ticks_read"]) for item in inputs),
            "discarded": None if any(item.get("ticks_discarded") is None for item in inputs)
                         else sum(int(item["ticks_discarded"]) for item in inputs),
            "duplicates": None if any(item.get("duplicates") is None for item in inputs)
                        else sum(int(item["duplicates"]) for item in inputs),
        },
        "bars_existing": counts,
        "derived_capability_estimates": derived,
        "gaps_m1": {"intervals_gt_1_minute": gap_gt_1m, "intervals_gt_1_hour": gap_gt_1h,
                    "intervals_gt_1_day": gap_gt_1d, "maximum_elapsed_minutes": max_gap_minutes,
                    "reason": "UNKNOWN_REASON", "market_closure_confirmed": False},
        "spread_m1_average_price_units": {
            "median": percentile_linear(spread_values, 0.50),
            "p95": percentile_linear(spread_values, 0.95),
            "p99": percentile_linear(spread_values, 0.99),
            "maximum_observed_tick_spread_from_m1": maximum_tick_spread,
            "methodology": "unweighted percentiles of per-M1 avg_spread; linear interpolation at (n-1)*p; maximum of per-M1 max_spread",
        },
        "tick_activity_m1": {
            "median_tick_count": percentile_linear(activity_values, 0.50),
            "p95_tick_count": percentile_linear(activity_values, 0.95),
            "low_activity_threshold_ticks": LOW_ACTIVITY_MAX_TICKS,
            "low_activity_bars": low_activity,
            "methodology": "one observation per persisted M1 bar; tick_count is quote activity, not traded volume",
        },
        "timezone": {"broker_server_utc_offset": None, "status": "UNKNOWN"},
        "session_attribution_allowed": False,
        "d1_session_status": "UNVERIFIED",
        "provenance": {"processed_inputs_schema_version": index.get("schema_version"),
                       "replay_output_version": index.get("output_version"),
                       "input_path_policy": "filename_and_hash_only; local normalized_path excluded",
                       "source_bar_timeframe": "M1", "no_tick_csv_reread": True},
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict) -> None:
    required = {"schema_version", "dataset_id", "fingerprint", "source_files", "date_start", "date_end",
                "ticks", "bars_existing", "derived_capability_estimates", "gaps_m1",
                "spread_m1_average_price_units", "tick_activity_m1", "timezone",
                "session_attribution_allowed", "d1_session_status", "provenance"}
    if not required <= manifest.keys() or manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported or incomplete data quality manifest")
    if not str(manifest["fingerprint"]).startswith("sha256:") or not manifest["source_files"]:
        raise ValueError("source fingerprint/files missing")
    if manifest["timezone"]["status"] not in {"VERIFIED", "UNKNOWN"}:
        raise ValueError("invalid timezone verification status")
    if manifest["timezone"]["status"] == "UNKNOWN" and (
        manifest["timezone"]["broker_server_utc_offset"] is not None
        or manifest["session_attribution_allowed"]
        or manifest["d1_session_status"] != "UNVERIFIED"
    ):
        raise ValueError("unverified broker timezone cannot authorize sessions or D1 session attribution")
    if manifest["ticks"]["valid"] < 0 or any(manifest["bars_existing"][key] < 0 for key in TIMEFRAMES):
        raise ValueError("negative data count")
    if manifest["date_start"] > manifest["date_end"]:
        raise ValueError("invalid date range")
    if any("normalized_path" in item for item in manifest["source_files"]):
        raise ValueError("local paths must not be committed in frozen evidence")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-root", type=Path, default=Path(__file__).resolve().parent / "output")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "output" / "data_quality_manifest.json")
    args = parser.parse_args(argv)
    manifest = build_manifest(args.replay_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".data-quality-", suffix=".tmp", dir=args.output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"[DATA_QUALITY] dataset={manifest['dataset_id']} M1={manifest['bars_existing']['M1']:,} "
          f"M5_closed={manifest['derived_capability_estimates']['M5']['closed_buckets']:,} "
          f"D1_closed={manifest['derived_capability_estimates']['D1']['closed_buckets']:,} "
          f"timezone={manifest['timezone']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
