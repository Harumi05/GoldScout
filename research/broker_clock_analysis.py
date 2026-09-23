"""Conservative broker-clock evidence inventory; never infer UTC from the PC.

The adopted MT5 tick timestamps encode server wall time on a millisecond axis.
Without an independent same-instant UTC reference and broker rollover evidence,
historical pause times cannot establish a timezone or DST rule.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Sequence


CLOCK_SCHEMA_VERSION = 1
ALLOWED_STATUSES = frozenset({"VERIFIED", "PARTIAL", "UNKNOWN"})
TRUSTED_PAIR_SOURCE = "MT5_SAME_INSTANT_SERVER_UTC_CAPTURE"


def _server_wall_label(milliseconds: int) -> str:
    # Decode the wall-time encoding using UTC arithmetic only as a calendar
    # formatter. The returned string deliberately has no UTC suffix.
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def collect_pause_evidence(m1_path: Path, *, largest: int = 8) -> dict:
    """Stream M1 starts; report observed pauses without assigning a cause or timezone."""
    if largest < 1:
        raise ValueError("largest must be positive")
    previous = None
    count = 0
    examples: list[dict] = []
    with Path(m1_path).open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            current = int(json.loads(line)["timestamp"])
            if previous is not None:
                if current <= previous:
                    raise ValueError("M1 pause evidence requires chronological unique bars")
                elapsed = (current - previous) // 60_000
                if elapsed > 60:
                    count += 1
                    examples.append({"last_observed_m1_server_wall": _server_wall_label(previous),
                                     "next_observed_m1_server_wall": _server_wall_label(current),
                                     "elapsed_minutes": elapsed, "cause": "UNKNOWN_REASON"})
                    examples.sort(key=lambda item: item["elapsed_minutes"], reverse=True)
                    del examples[largest:]
            previous = current
    return {"type": "OBSERVED_SERVER_WALL_PAUSES", "source_timeframe": "M1",
            "intervals_gt_1_hour": count, "largest": examples, "utc_anchor": False}


def _parse_pair(row: dict) -> tuple[str, float, str]:
    """Return capture ID, offset hours and UTC date for a trusted paired sample."""
    if row.get("source") != TRUSTED_PAIR_SOURCE or not row.get("capture_id"):
        raise ValueError("clock pair lacks trusted source or capture_id")
    server = datetime.fromisoformat(str(row["server_wall_time"]))
    utc = datetime.fromisoformat(str(row["utc_time"]).replace("Z", "+00:00"))
    if server.tzinfo is not None or utc.tzinfo is None:
        raise ValueError("server_wall_time must be naive and utc_time timezone-aware")
    utc = utc.astimezone(timezone.utc)
    wall_as_utc = server.replace(tzinfo=timezone.utc)
    seconds = (wall_as_utc - utc).total_seconds()
    rounded_minutes = round(seconds / 60)
    if abs(seconds - rounded_minutes * 60) > 5 or abs(rounded_minutes) > 14 * 60:
        raise ValueError("clock pair has implausible offset or capture skew")
    return str(row["capture_id"]), rounded_minutes / 60, utc.date().isoformat()


def analyze_clock(source_manifest: dict, paired_samples: Sequence[dict] = (),
                  pause_evidence: dict | None = None) -> dict:
    """Partial offset evidence is possible; VERIFIED requires unavailable rollover proof."""
    if not source_manifest.get("dataset_id") or not source_manifest.get("fingerprint"):
        raise ValueError("source manifest lacks dataset identity")
    parsed = [_parse_pair(row) for row in paired_samples]
    ids = [capture_id for capture_id, _, _ in parsed]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate clock capture_id")
    dates = {date for _, _, date in parsed}
    offsets = sorted({offset for _, offset, _ in parsed})
    partial = len(parsed) >= 2 and len(dates) >= 2
    status = "PARTIAL" if partial else "UNKNOWN"
    # Two paired instants can constrain an offset, but not prove daily rollover
    # or an annual DST schedule. Never promote to VERIFIED from pauses alone.
    result = {
        "schema_version": CLOCK_SCHEMA_VERSION,
        "source_manifest_id": source_manifest["dataset_id"],
        "broker_clock_status": status,
        "broker_server_utc_offset": offsets[0] if partial and len(offsets) == 1 else None,
        "observed_offsets_hours": offsets if partial else [],
        "broker_day_rollover": None,
        "dst_behavior": "UNKNOWN",
        "confidence": 0.5 if partial else 0.0,
        "session_attribution_allowed": False,
        "evidence": [
            {"type": "ADOPTED_REPLAY_TIMESTAMP_ENCODING",
             "detail": "epoch_ms_mt5_server_wall_time; no UTC conversion", "source_manifest_id": source_manifest["dataset_id"]},
            {"type": "OBSERVED_M1_GAPS", "intervals_gt_1_hour": source_manifest["gaps_m1"]["intervals_gt_1_hour"],
             "reason": source_manifest["gaps_m1"]["reason"], "utc_anchor": False},
            {"type": "TRUSTED_PAIRED_SAMPLES", "capture_ids": ids if partial else [],
             "distinct_utc_dates": len(dates) if partial else 0, "utc_anchor": partial},
        ],
        "limitations": [
            "No verified broker rollover specification or independently paired rollover capture.",
            "Observed market pauses/weekend reopen times cannot by themselves prove UTC offset or DST.",
            "TimeTradeServer/TimeGMT appear in runtime code, but no same-instant pair is persisted in this dataset.",
        ],
    }
    if pause_evidence is not None:
        if pause_evidence.get("utc_anchor") is not False:
            raise ValueError("server-wall pause evidence cannot be treated as a UTC anchor")
        result["evidence"].append(pause_evidence)
    validate_clock_report(result)
    return result


def validate_clock_report(report: dict) -> None:
    if report.get("schema_version") != CLOCK_SCHEMA_VERSION or report.get("broker_clock_status") not in ALLOWED_STATUSES:
        raise ValueError("unsupported broker-clock report")
    if not report.get("source_manifest_id") or not isinstance(report.get("evidence"), list):
        raise ValueError("broker-clock report lacks source/evidence")
    if report["broker_clock_status"] != "VERIFIED" and (
        report.get("broker_day_rollover") is not None or report.get("session_attribution_allowed") is not False
    ):
        raise ValueError("unverified rollover cannot authorize sessions")
    if report["broker_clock_status"] == "UNKNOWN" and report.get("broker_server_utc_offset") is not None:
        raise ValueError("unknown broker clock cannot carry a verified offset")
    if report["broker_clock_status"] == "VERIFIED" and (
        report.get("broker_server_utc_offset") is None or report.get("broker_day_rollover") is None or
        report.get("dst_behavior") in (None, "UNKNOWN") or
        not any(item.get("type") == "BROKER_ROLLOVER_VERIFIED" and item.get("source_id")
                for item in report["evidence"])
    ):
        raise ValueError("verified clock requires offset, rollover, DST policy and independent evidence")


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".broker-clock-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--source-manifest", type=Path, default=base / "data_quality_manifest.json")
    parser.add_argument("--paired-samples", type=Path, help="Optional trusted same-instant MT5 server/UTC JSONL; never PC time")
    parser.add_argument("--m1-bars", type=Path, default=base / "output" / "historical_bars" / "XAUUSD_M1.jsonl",
                        help="Persisted M1 bar file for server-wall pause evidence; no tick CSV read")
    parser.add_argument("--output", type=Path, default=base / "output" / "broker_clock_analysis.json")
    args = parser.parse_args(argv)
    manifest = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    pairs = []
    if args.paired_samples:
        with args.paired_samples.open("r", encoding="utf-8") as stream:
            pairs = [json.loads(line) for line in stream if line.strip()]
    pauses = collect_pause_evidence(args.m1_bars) if args.m1_bars.is_file() else None
    report = analyze_clock(manifest, pairs, pauses)
    _write_atomic(args.output, report)
    print(f"[BROKER_CLOCK] status={report['broker_clock_status']} offset={report['broker_server_utc_offset']} "
          f"rollover={report['broker_day_rollover']} sessions={report['session_attribution_allowed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
