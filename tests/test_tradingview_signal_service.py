"""Deterministic tests for diagnostic-only TradingView observation."""

from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import server  # noqa: E402
import tradingview_signal_service as tradingview  # noqa: E402


FIXED_NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
TOKEN = "local-test-token"


def alert(**overrides):
    payload = {
        "source": "tradingview",
        "symbol": "XAUUSD",
        "timeframe": "15",
        "event": "breakout",
        "direction": "LONG",
        "price": 4400.25,
        "rsi": 58.4,
        "adx": 27.1,
        "atr": 12.5,
        "ema20": 4392.5,
        "ema200": 4350.1,
        "timestamp": 1789052400,
    }
    payload.update(overrides)
    return payload


class TradingViewWebhookTests(unittest.TestCase):
    def ingest(self, payload, path, *, provided_token=TOKEN, now=FIXED_NOW):
        return tradingview.ingest_webhook(
            payload,
            provided_token=provided_token,
            expected_token=TOKEN,
            path=path,
            now=now,
        )

    def test_valid_webhook_is_normalized_and_stored(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            code, result = self.ingest(alert(symbol=" gold ", direction=" long "), path)
            rows = tradingview.read_events(path)
        self.assertEqual(code, 200)
        self.assertEqual(result["status"], "OK")
        self.assertFalse(result["duplicate"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "tradingview")
        self.assertEqual(rows[0]["symbol"], "XAUUSD")
        self.assertEqual(rows[0]["direction"], "LONG")
        self.assertEqual(rows[0]["timeframe"], "15")
        self.assertEqual(rows[0]["score_effect"], 0)

    def test_wrong_token_is_unauthorized_and_not_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            code, result = self.ingest(alert(), path, provided_token="wrong-token")
            rows = tradingview.read_events(path)
        self.assertEqual(code, 401)
        self.assertEqual(result, {"status": "UNAUTHORIZED", "score_effect": 0})
        self.assertEqual(rows, [])

    def test_json_body_token_is_primary_and_never_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            code, result = self.ingest(
                alert(token=TOKEN), path, provided_token="wrong-header-token"
            )
            row = tradingview.read_events(path)[0]
        self.assertEqual(code, 200)
        self.assertEqual(result["status"], "OK")
        self.assertNotIn("token", row)

    def test_token_never_appears_in_response_or_output(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(output), redirect_stderr(output):
            path = Path(temp) / "events.jsonl"
            _, result = self.ingest(alert(token=TOKEN), path, provided_token="")
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn(TOKEN, output.getvalue())

    def test_missing_configured_token_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {tradingview.TOKEN_ENV: ""}, clear=False
        ):
            path = Path(temp) / "events.jsonl"
            code, result = tradingview.ingest_webhook(alert(), path=path)
        self.assertEqual(code, 401)
        self.assertEqual(result["status"], "UNAUTHORIZED")

    def test_invalid_symbol_is_rejected_without_fuzzy_matching(self):
        for symbol in ("EURUSD", "OANDA:XAUUSD", "XAUUSD.a", "GOLD.ASX", "MGC"):
            with self.subTest(symbol=symbol), tempfile.TemporaryDirectory() as temp:
                code, result = self.ingest(alert(symbol=symbol), Path(temp) / "events.jsonl")
                self.assertEqual(code, 400)
                self.assertEqual(result["status"], "INVALID_PAYLOAD")

    def test_only_explicit_gold_aliases_are_accepted(self):
        self.assertEqual(tradingview.normalize_symbol("XAUUSD"), "XAUUSD")
        self.assertEqual(tradingview.normalize_symbol("GOLD"), "XAUUSD")

    def test_invalid_timeframe_is_rejected(self):
        for timeframe in ("1", "30", "H1", 30, 15.0, True):
            with self.subTest(timeframe=timeframe), tempfile.TemporaryDirectory() as temp:
                code, result = self.ingest(
                    alert(timeframe=timeframe), Path(temp) / "events.jsonl"
                )
                self.assertEqual(code, 400)
                self.assertEqual(result["status"], "INVALID_PAYLOAD")

    def test_duplicate_alert_is_not_appended_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            first_code, first = self.ingest(alert(), path)
            second_code, second = self.ingest(
                alert(), path, now=FIXED_NOW + timedelta(seconds=20)
            )
            rows = tradingview.read_events(path)
        self.assertEqual((first_code, second_code), (200, 200))
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(rows), 1)

    def test_large_file_deduplication_uses_bounded_recent_index(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                for index in range(12_000):
                    stream.write(json.dumps({"event_id": f"seed-{index}"}) + "\n")
            duplicate = {"event_id": "seed-11999", "score_effect": 0}
            new_event = {"event_id": "new-event", "score_effect": 0}
            tradingview._RECENT_INDEXES.pop(str(path.resolve()), None)
            with patch.object(
                tradingview,
                "_read_events_unlocked",
                side_effect=AssertionError("full JSONL scan is not allowed during append"),
            ):
                self.assertFalse(tradingview.append_event(duplicate, path))
                self.assertTrue(tradingview.append_event(new_event, path))

    def test_persistence_is_append_only_jsonl(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            self.ingest(alert(), path)
            self.ingest(alert(direction="SHORT", price=4390.0), path)
            lines = path.read_text(encoding="utf-8").splitlines()
            decoded = [json.loads(line) for line in lines]
        self.assertEqual(len(lines), 2)
        self.assertEqual([row["direction"] for row in decoded], ["LONG", "SHORT"])
        self.assertTrue(all(row["score_effect"] == 0 for row in decoded))

    def test_only_whitelisted_fields_are_persisted(self):
        payload = alert(
            token=TOKEN,
            score_effect=99,
            command="OPEN_REAL_TRADE",
            password="must-not-be-persisted",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            code, _ = self.ingest(payload, path, provided_token="")
            row = tradingview.read_events(path)[0]
        self.assertEqual(code, 200)
        self.assertEqual(row["score_effect"], 0)
        self.assertNotIn("token", row)
        self.assertNotIn("command", row)
        self.assertNotIn("password", row)
        self.assertEqual(
            row["indicators"],
            {"rsi": 58.4, "adx": 27.1, "atr": 12.5, "ema20": 4392.5, "ema200": 4350.1},
        )

    def test_indicator_bounds_and_nonfinite_values_are_rejected(self):
        for changes in ({"rsi": 101}, {"adx": -1}, {"atr": float("nan")}, {"price": True}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                code, result = self.ingest(
                    alert(**changes), Path(temp) / "events.jsonl"
                )
                self.assertEqual(code, 400)
                self.assertEqual(result["status"], "INVALID_PAYLOAD")

    def test_event_id_uses_only_the_defined_identity(self):
        base = tradingview.validate_payload(alert(rsi=50))
        changed_indicator = tradingview.validate_payload(alert(rsi=80))
        changed_price = tradingview.validate_payload(alert(price=4400.26))
        self.assertEqual(
            tradingview.deterministic_event_id(base),
            tradingview.deterministic_event_id(changed_indicator),
        )
        self.assertNotEqual(
            tradingview.deterministic_event_id(base),
            tradingview.deterministic_event_id(changed_price),
        )

    def test_snapshot_reports_no_data_ok_and_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            empty = tradingview.observation_snapshot(path, now=FIXED_NOW)
            self.ingest(alert(), path)
            fresh = tradingview.observation_snapshot(path, now=FIXED_NOW)
            stale = tradingview.observation_snapshot(
                path, now=FIXED_NOW + timedelta(seconds=901), stale_seconds=900
            )
        self.assertEqual(empty["api_status"], "NO_DATA")
        self.assertEqual(fresh["api_status"], "OK")
        self.assertEqual(stale["api_status"], "STALE")
        self.assertFalse(stale["available"])
        self.assertEqual(stale["score_effect"], 0)

    def test_replayed_old_event_is_stale_even_when_recently_received(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            old_timestamp = int((FIXED_NOW - timedelta(hours=1)).timestamp())
            self.ingest(alert(timestamp=old_timestamp), path, now=FIXED_NOW)
            snapshot = tradingview.observation_snapshot(
                path, now=FIXED_NOW, stale_seconds=900
            )
        self.assertEqual(snapshot["api_status"], "STALE")
        self.assertGreaterEqual(snapshot["freshness_sec"], 3600)

    def test_invalid_or_unauthorized_attempt_keeps_last_valid_signal(self):
        for invalid_payload, supplied_token, expected_status in (
            (alert(symbol="EURUSD"), TOKEN, "INVALID_PAYLOAD"),
            (alert(), "wrong", "UNAUTHORIZED"),
        ):
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "events.jsonl"
                _, valid = self.ingest(alert(), path)
                code, rejected = self.ingest(
                    invalid_payload, path, provided_token=supplied_token
                )
                snapshot = tradingview.observation_snapshot(path, now=FIXED_NOW)
                rows = tradingview.read_events(path)
            self.assertIn(code, (400, 401))
            self.assertEqual(rejected["status"], expected_status)
            self.assertEqual(snapshot["api_status"], expected_status)
            self.assertEqual(snapshot["data_status"], "OK")
            self.assertEqual(snapshot["last_signal"]["event_id"], valid["event_id"])
            self.assertEqual(len(rows), 1)

    def test_score_effect_is_always_zero_in_response_record_and_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            code, result = self.ingest(alert(score_effect=999), path)
            record = tradingview.read_events(path)[0]
            snapshot = tradingview.observation_snapshot(path, now=FIXED_NOW)
        self.assertEqual(code, 200)
        self.assertEqual(result["score_effect"], 0)
        self.assertEqual(record["score_effect"], 0)
        self.assertEqual(snapshot["score_effect"], 0)


class TradingViewServerTests(unittest.TestCase):
    def test_request_token_supports_header_without_logging_or_persistence(self):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        self.assertEqual(server.request_token(headers), TOKEN)
        self.assertEqual(server.request_token({"X-GoldScout-Token": TOKEN}), TOKEN)

    def test_http_webhook_accepts_valid_json_and_rejects_bad_token(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"

            def ingest(payload, provided_token=""):
                return tradingview.ingest_webhook(
                    payload,
                    provided_token=provided_token,
                    expected_token=TOKEN,
                    path=path,
                    now=FIXED_NOW,
                )

            with patch.object(server, "ingest_tradingview_webhook", ingest):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    body = json.dumps(alert()).encode("utf-8")
                    connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
                    connection.request(
                        "POST",
                        "/api/tradingview/webhook",
                        body=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-GoldScout-Token": TOKEN,
                        },
                    )
                    valid = connection.getresponse()
                    valid_payload = json.loads(valid.read())
                    connection.close()

                    connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
                    connection.request(
                        "POST",
                        "/api/tradingview/webhook",
                        body=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-GoldScout-Token": "wrong",
                        },
                    )
                    invalid = connection.getresponse()
                    invalid_payload = json.loads(invalid.read())
                    connection.close()
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=3)

            rows = tradingview.read_events(path)
        self.assertEqual(valid.status, 200)
        self.assertEqual(valid_payload["status"], "OK")
        self.assertEqual(invalid.status, 401)
        self.assertEqual(invalid_payload["status"], "UNAUTHORIZED")
        self.assertEqual(len(rows), 1)

    def test_rate_limit_returns_429_without_removing_last_signal(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"

            def ingest(payload, provided_token=""):
                return tradingview.ingest_webhook(
                    payload,
                    provided_token=provided_token,
                    expected_token=TOKEN,
                    path=path,
                    now=FIXED_NOW,
                )

            def record(status):
                tradingview.record_service_status(status, path)

            limiter = tradingview.SlidingWindowRateLimiter(limit=1, window_seconds=60)
            with (
                patch.object(server, "ingest_tradingview_webhook", ingest),
                patch.object(server, "record_tradingview_status", record),
                patch.object(server, "TRADINGVIEW_RATE_LIMITER", limiter),
            ):
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                statuses = []
                payloads = []
                try:
                    body = json.dumps(alert(token=TOKEN)).encode("utf-8")
                    for _ in range(2):
                        connection = HTTPConnection(
                            "127.0.0.1", httpd.server_port, timeout=3
                        )
                        connection.request(
                            "POST",
                            "/api/tradingview/webhook",
                            body=body,
                            headers={"Content-Type": "application/json"},
                        )
                        response = connection.getresponse()
                        statuses.append(response.status)
                        payloads.append(json.loads(response.read()))
                        connection.close()
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=3)

            snapshot = tradingview.observation_snapshot(path, now=FIXED_NOW)
            rows = tradingview.read_events(path)
        self.assertEqual(statuses, [200, 429])
        self.assertEqual(payloads[1], {"status": "RATE_LIMITED", "score_effect": 0})
        self.assertEqual(snapshot["api_status"], "RATE_LIMITED")
        self.assertEqual(snapshot["data_status"], "OK")
        self.assertIsNotNone(snapshot["last_signal"])
        self.assertEqual(len(rows), 1)

    def test_dashboard_contract_is_observation_only(self):
        server_source = (DASHBOARD / "server.py").read_text(encoding="utf-8")
        page = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        module = (DASHBOARD / "tradingview_signal_service.py").read_text(encoding="utf-8")
        self.assertIn("'/api/tradingview/webhook'", server_source)
        self.assertIn("TRADINGVIEW — OBSERVACIÓN", page)
        self.assertIn("Solo observación · score_effect=0", page)
        self.assertNotIn("\nimport requests", module)
        self.assertNotIn("\nfrom requests", module)
        self.assertNotIn("MetaTrader", module.replace("never calls MetaTrader", ""))


if __name__ == "__main__":
    unittest.main()
