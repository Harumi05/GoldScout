"""Deterministic tests for the read-only eToro diagnostic observer."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import external_signal_service as external  # noqa: E402


FIXED_NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, json_error=False):
        self.status_code = status_code
        self.payload = {} if payload is None else payload
        self.headers = headers or {}
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("invalid json")
        return self.payload


class FakeSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class AvailableClient:
    has_credentials = True

    def __init__(self, *, private_portfolio=False, gold_instrument=True):
        self.private_portfolio = private_portfolio
        self.gold_instrument = gold_instrument
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == external.READ_ONLY_ENDPOINTS["instrument_search"]:
            symbol = (params or {}).get("internalSymbolFull")
            if symbol == "GOLD" and self.gold_instrument:
                return {"items": [{
                    "instrumentId": 559,
                    "displayname": "GOLD 24/7",
                    "internalInstrumentDisplayName": "GOLD 24/7",
                    "internalSymbolFull": "GOLD.24-7",
                    "internalAssetClassName": "Commodity",
                }]}
            return {"items": []}
        if path == external.READ_ONLY_ENDPOINTS["rankings"]:
            return {"results": [{
                "cid": 77,
                "username": "goldobserver",
                "fullName": "Gold Observer",
                "riskScore": 4,
                "copiers": 250,
                "annualizedReturn": 12,
                "profitableMonthsPct": 70,
                "peakToValley": 10,
                "activeWeeksPct": 80,
                "lastActivity": "2026-09-09T15:00:00Z",
            }]}
        if path == external.READ_ONLY_ENDPOINTS["users_info"]:
            return {"users": [{"gcid": 77, "username": "goldobserver", "optOut": False}]}
        if path.endswith("/portfolio/live"):
            if self.private_portfolio:
                raise external.EtoroUnavailable("private", 403)
            return {"positions": [{
                "positionId": 9001,
                "openTimestamp": "2026-09-10T13:30:00Z",
                "openRate": 2500.5,
                "instrumentId": 559,
                "isBuy": True,
                "investmentPct": 4.0,
            }], "socialTrades": []}
        if path.endswith("/gain/daily"):
            return {"gains": [{"date": "2026-09-08", "gain": 0.2}, {"date": "2026-09-09", "gain": 0.3}], "totalGain": 0.5}
        if path.endswith("/copiers") and "/portfolios/" in path:
            return {"copiers": 250, "aumTier": 2, "aumTierDesc": "Silver"}
        if path.startswith("/api/v1/feeds/users/"):
            return {"discussions": [], "paging": {}}
        if path == external.READ_ONLY_ENDPOINTS["pi_data"]:
            return {"copiers": []}
        raise AssertionError(f"unexpected endpoint: {path}")


def trader(trader_id, reliability, quality=80, relevant=True):
    return external.ExternalTrader(
        source="etoro",
        trader_id=str(trader_id),
        username=f"u{trader_id}",
        display_name=f"Trader {trader_id}",
        risk_score=4,
        copiers=100,
        return_period="OneYearAgo",
        return_pct=10,
        relevant_to_gold=relevant,
        data_timestamp="2026-09-10T15:00:00Z",
        data_quality=quality,
        reliability_score=reliability,
    )


def signal(trader_id, direction, allocation=1.0):
    return external.ExternalSignal(
        source="etoro",
        trader_id=str(trader_id),
        instrument="GOLD",
        normalized_symbol="XAUUSD",
        direction=direction,
        action="OPEN",
        timestamp="2026-09-10T13:30:00Z",
        price=2500.0,
        confidence=70,
        data_quality=80,
        session_context="NUEVA YORK/COMEX",
        raw_reference_id=f"{trader_id}-{direction}-{allocation}",
        allocation_weight=allocation,
    )


class EtoroTransportTests(unittest.TestCase):
    def test_api_available_uses_official_headers_and_get(self):
        session = FakeSession([FakeResponse(payload={"ok": True})])
        client = external.EtoroClient("api", "user", session=session, max_retries=0)
        self.assertEqual(client.get("/api/v1/test"), {"ok": True})
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://public-api.etoro.com/api/v1/test")
        self.assertEqual(kwargs["headers"]["x-api-key"], "api")
        self.assertEqual(kwargs["headers"]["x-user-key"], "user")
        self.assertIn("x-request-id", kwargs["headers"])

    def test_429_honors_retry_after_with_limited_retry(self):
        delays = []
        session = FakeSession([
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(200, {"ok": True}),
        ])
        client = external.EtoroClient(
            "api", "user", session=session, max_retries=1, sleeper=delays.append
        )
        self.assertEqual(client.get("/api/v1/test"), {"ok": True})
        self.assertEqual(delays, [2.0])
        self.assertEqual(len(session.calls), 2)

    def test_timeout_exhausts_retry_budget_safely(self):
        session = FakeSession([requests.Timeout(), requests.Timeout()])
        client = external.EtoroClient(
            "api", "user", session=session, max_retries=1, sleeper=lambda _: None
        )
        with self.assertRaises(external.EtoroAPIError):
            client.get("/api/v1/test")
        self.assertEqual(len(session.calls), 2)

    def test_5xx_retries_with_exponential_backoff(self):
        delays = []
        session = FakeSession([
            FakeResponse(503),
            FakeResponse(502),
            FakeResponse(200, {"ok": True}),
        ])
        client = external.EtoroClient(
            "api", "user", session=session, max_retries=2,
            backoff_seconds=0.5, sleeper=delays.append,
        )
        self.assertEqual(client.get("/api/v1/test"), {"ok": True})
        self.assertEqual(delays, [0.5, 1.0])

    def test_invalid_response_fails_safely(self):
        client = external.EtoroClient(
            "api", "user", session=FakeSession([FakeResponse(json_error=True)]), max_retries=0
        )
        with self.assertRaises(external.EtoroInvalidResponse):
            client.get("/api/v1/test")

    def test_private_trader_is_unavailable_not_fatal(self):
        client = external.EtoroClient(
            "api", "user", session=FakeSession([FakeResponse(403)]), max_retries=0
        )
        service = external.EtoroExternalSignalService(client)
        self.assertIsNone(service._get("private", "/api/v1/private"))
        self.assertEqual(service.endpoint_status["private"], "FORBIDDEN")
        self.assertEqual(service.endpoint_http_status["private"], 403)

    def test_http_failures_are_kept_distinct(self):
        expected = {
            401: "AUTH_ERROR",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            429: "RATE_LIMITED",
            503: "PROVIDER_ERROR",
        }
        for code, status in expected.items():
            with self.subTest(code=code):
                client = external.EtoroClient(
                    "api", "user", session=FakeSession([FakeResponse(code)]), max_retries=0
                )
                service = external.EtoroExternalSignalService(client)
                self.assertIsNone(service._get("endpoint", "/api/v1/test"))
                self.assertEqual(service.endpoint_status["endpoint"], status)
                self.assertEqual(service.endpoint_http_status["endpoint"], code)

    def test_only_relative_api_paths_are_accepted(self):
        client = external.EtoroClient("api", "user", session=FakeSession([]))
        with self.assertRaises(ValueError):
            client.get("https://example.com/api/v1/test")

    def test_trading_and_copy_trading_paths_are_disabled(self):
        client = external.EtoroClient("api", "user", session=FakeSession([]))
        for path in ("/api/v1/trading/orders", "/api/v1/copy-trading/copy"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.get(path)


class EtoroNormalizationTests(unittest.TestCase):
    def test_only_explicit_gold_symbols_are_normalized(self):
        for value in ("Gold", "GOLD", "XAUUSD", "GOLD.24-7"):
            self.assertEqual(external.normalize_gold_symbol(value), "XAUUSD")

    def test_non_gold_or_ambiguous_instrument_is_rejected(self):
        for value in (
            "Gold Miners ETF", "Barrick Gold", "XAU/EUR", "XAU/USD", "XAU-USD",
            "GOLD.APR27", "GOLD.AUG27", "GOLD.DEC27", "MGC.DEC27", "GOLD.ASX", "Silver", "",
        ):
            self.assertIsNone(external.normalize_gold_symbol(value))

    def test_instrument_mapping_requires_exact_gold_24_7_id_and_symbol(self):
        payload = {"items": [
            {"instrumentId": 559, "internalSymbolFull": "GOLD.24-7", "displayname": "GOLD 24/7"},
            {"instrumentId": 560, "internalSymbolFull": "MGC.DEC27", "displayname": "Micro Gold"},
            {"instrumentId": 561, "internalSymbolFull": "GOLD.ASX", "displayname": "Gold ETF"},
            {"instrumentId": 562, "internalSymbolFull": "GOLD.DEC27", "displayname": "Gold Future"},
            {"instrumentId": 558, "internalSymbolFull": "GOLD.24-7", "displayname": "Wrong ID"},
            {"instrumentId": 559, "internalSymbolFull": "GOLD", "displayname": "Gold"},
        ]}
        self.assertEqual(
            external.normalize_gold_instruments([payload]),
            {559: "GOLD.24-7"},
        )

    def test_search_rows_are_sanitized_and_never_keep_unknown_fields(self):
        rows = external.sanitize_instrument_rows({"items": [{
            "instrumentId": 100,
            "internalSymbolFull": "GOLD",
            "displayname": "Gold",
            "internalAssetClassName": "Commodities",
            "instrumentTypeId": 7,
            "apiKey": "must-not-survive",
        }]})
        self.assertEqual(rows, [{
            "instrumentId": 100,
            "internalSymbolFull": "GOLD",
            "displayName": "Gold",
            "assetClass": "Commodities",
            "instrumentType": "7",
        }])
        self.assertNotIn("must-not-survive", json.dumps(rows))

    def test_configured_twenty_traders_is_not_silently_reduced_to_ten(self):
        self.assertEqual(external.resolve_max_traders(20), (20, 20))
        self.assertEqual(external.resolve_max_traders(101), (101, 100))

    def test_long_portfolio_position_normalizes_to_external_signal(self):
        rows = external.normalize_portfolio_signals(
            "77",
            {"positions": [{"positionId": 1, "instrumentId": 100, "isBuy": True,
                            "openTimestamp": "2026-09-10T13:30:00Z", "openRate": 2500}]},
            {100: "GOLD"},
            "2026-09-10T15:00:00Z",
        )
        self.assertEqual(rows[0].direction, "LONG")
        self.assertEqual(rows[0].normalized_symbol, "XAUUSD")

    def test_short_portfolio_position_normalizes_to_external_signal(self):
        rows = external.normalize_portfolio_signals(
            "77",
            {"positions": [{"positionId": 2, "instrumentId": 100, "isBuy": False}]},
            {100: "GOLD"},
            "2026-09-10T15:00:00Z",
        )
        self.assertEqual(rows[0].direction, "SHORT")

    def test_unrelated_position_is_not_a_signal(self):
        rows = external.normalize_portfolio_signals(
            "77",
            {"positions": [{"positionId": 3, "instrumentId": 999, "isBuy": True}]},
            {100: "GOLD"},
            "2026-09-10T15:00:00Z",
        )
        self.assertEqual(rows, [])

    def test_read_only_copy_position_is_labeled_and_deduplicated(self):
        position = {"positionId": 4, "instrumentId": 100, "isBuy": True}
        rows = external.normalize_portfolio_signals(
            "77",
            {"positions": [], "socialTrades": [{"positions": [position, position]}]},
            {100: "GOLD"},
            "2026-09-10T15:00:00Z",
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].read_only_copy_data)

    def test_signal_session_context_uses_timezone_rules(self):
        self.assertEqual(external.signal_session_context("2026-07-01T07:30:00Z"), "LONDRES")
        self.assertEqual(external.signal_session_context("2026-07-01T13:00:00Z"), "LONDRES+NUEVA YORK/COMEX")
        self.assertEqual(external.signal_session_context("2026-01-05T02:00:00Z"), "ASIA")


class EtoroConsensusTests(unittest.TestCase):
    def test_consensus_with_multiple_traders_is_reliability_weighted(self):
        traders = [trader(1, 80), trader(2, 40), trader(3, 60)]
        signals = [signal(1, "LONG"), signal(2, "SHORT"), signal(3, "LONG")]
        result = external.calculate_consensus(traders, signals, updated_at="2026-09-10T15:00:00Z")
        self.assertEqual(result["valid_traders"], 3)
        self.assertGreater(result["long_weight"], result["short_weight"])
        self.assertAlmostEqual(result["long_weight"] + result["short_weight"] + result["neutral_weight"], 1.0, places=3)

    def test_balanced_trader_is_neutral(self):
        result = external.calculate_consensus(
            [trader(1, 70)], [signal(1, "LONG", 2), signal(1, "SHORT", 2)]
        )
        self.assertEqual(result["neutral_traders"], 1)
        self.assertEqual(result["neutral_weight"], 1.0)

    def test_empty_consensus_is_safe(self):
        result = external.calculate_consensus([], [])
        self.assertEqual(result["valid_traders"], 0)
        self.assertEqual(result["long_weight"], 0.0)
        self.assertEqual(result["score_effect"], 0)

    def test_stale_snapshot_is_unavailable_and_keeps_zero_effect(self):
        snapshot = external.empty_snapshot("OK", "test", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        snapshot["available"] = True
        freshened = external.mark_snapshot_freshness(snapshot, FIXED_NOW)
        self.assertFalse(freshened["available"])
        self.assertEqual(freshened["api_status"], "STALE")
        self.assertEqual(freshened["score_effect"], 0)

    def test_reliability_does_not_equate_high_return_with_best_trader(self):
        disciplined = {"riskScore": 4, "copiers": 100, "annualizedReturn": 10,
                       "profitableMonthsPct": 80, "peakToValley": 8, "activeWeeksPct": 90}
        reckless = {"riskScore": 10, "copiers": 100, "annualizedReturn": 100,
                    "profitableMonthsPct": 40, "peakToValley": 50, "activeWeeksPct": 90}
        self.assertGreater(external.reliability_score(disciplined), external.reliability_score(reckless))


class EtoroIntegrationTests(unittest.TestCase):
    def test_missing_credentials_degrades_and_writes_no_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            client = external.EtoroClient("", "")
            result = external.run_once(output_path=output, client=client, now=lambda: FIXED_NOW)
            self.assertEqual(result["api_status"], "NO_CREDENTIALS")
            self.assertFalse(result["available"])
            self.assertEqual(result["score_effect"], 0)
            self.assertNotIn("api_key", output.read_text(encoding="utf-8").lower())

    def test_available_api_builds_read_only_gold_consensus(self):
        service = external.EtoroExternalSignalService(
            AvailableClient(), max_traders=1, now=lambda: FIXED_NOW
        )
        result = service.collect()
        self.assertTrue(result["available"])
        self.assertEqual(result["long_weight"], 1.0)
        self.assertEqual(result["valid_traders"], 1)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["score_effect"], 0)

    def test_private_portfolio_yields_empty_consensus_without_failure(self):
        service = external.EtoroExternalSignalService(
            AvailableClient(private_portfolio=True), max_traders=1, now=lambda: FIXED_NOW
        )
        result = service.collect()
        self.assertFalse(result["available"])
        self.assertEqual(result["valid_traders"], 0)
        self.assertEqual(result["api_status"], "GOLD_FOUND_PORTFOLIO_UNAVAILABLE")
        self.assertEqual(result["portfolio_summary"], {
            "reviewed_traders": 1,
            "portfolio_ok": 0,
            "portfolio_403": 1,
            "portfolio_404": 0,
            "portfolio_other_errors": 0,
        })
        self.assertEqual(result["portfolio_diagnostics"], [{
            "http_status": 403,
            "portfolio_available": False,
            "positions": 0,
        }])
        self.assertEqual(result["score_effect"], 0)

    def test_twenty_forbidden_portfolios_report_gold_portfolio_unavailable(self):
        class TwentyPrivateClient(AvailableClient):
            def __init__(self):
                super().__init__(private_portfolio=True)

            def get(self, path, params=None):
                if path == external.READ_ONLY_ENDPOINTS["rankings"]:
                    return {"results": [
                        {
                            "cid": index,
                            "username": f"public_trader_{index}",
                            "riskScore": 4,
                            "copiers": 100,
                            "annualizedReturn": 5,
                        }
                        for index in range(1, 21)
                    ]}
                return super().get(path, params)

        result = external.EtoroExternalSignalService(
            TwentyPrivateClient(), max_traders=20, now=lambda: FIXED_NOW
        ).collect()
        self.assertEqual(result["api_status"], "GOLD_FOUND_PORTFOLIO_UNAVAILABLE")
        self.assertEqual(result["portfolio_summary"]["reviewed_traders"], 20)
        self.assertEqual(result["portfolio_summary"]["portfolio_ok"], 0)
        self.assertEqual(result["portfolio_summary"]["portfolio_403"], 20)
        self.assertEqual(result["score_effect"], 0)

    def test_no_explicit_gold_instrument_yields_safe_empty_consensus(self):
        service = external.EtoroExternalSignalService(
            AvailableClient(gold_instrument=False), max_traders=1, now=lambda: FIXED_NOW
        )
        result = service.collect()
        self.assertFalse(result["available"])
        self.assertEqual(result["api_status"], "SEARCH_EMPTY")
        self.assertEqual(result["valid_traders"], 0)
        self.assertEqual(result["score_effect"], 0)

    def test_search_rows_without_exact_gold_report_alias_not_matched(self):
        class AliasClient(AvailableClient):
            def get(self, path, params=None):
                if path == external.READ_ONLY_ENDPOINTS["instrument_search"]:
                    return {"items": [{
                        "instrumentId": 555,
                        "internalSymbolFull": "GOLD.FUT",
                        "displayname": "Gold",
                    }]}
                return super().get(path, params)

        result = external.EtoroExternalSignalService(
            AliasClient(), max_traders=1, now=lambda: FIXED_NOW
        ).collect()
        self.assertEqual(result["api_status"], "GOLD_ALIAS_NOT_MATCHED")
        self.assertIsNone(result["gold_instrument_id"])
        self.assertEqual(result["instrument_search"][0]["rows"][0]["instrumentId"], 555)

    def test_exact_gold_search_keeps_official_instrument_id(self):
        result = external.EtoroExternalSignalService(
            AvailableClient(), max_traders=20, now=lambda: FIXED_NOW
        ).collect()
        self.assertEqual(result["gold_instrument_id"], 559)
        self.assertEqual(result["configured_max_traders"], 20)
        self.assertEqual(result["effective_max_traders"], 20)

    def test_gold_found_without_ranked_traders_is_distinct(self):
        class NoTraderClient(AvailableClient):
            def get(self, path, params=None):
                if path == external.READ_ONLY_ENDPOINTS["rankings"]:
                    return {"results": []}
                return super().get(path, params)

        result = external.EtoroExternalSignalService(
            NoTraderClient(), max_traders=20, now=lambda: FIXED_NOW
        ).collect()
        self.assertEqual(result["api_status"], "GOLD_FOUND_NO_TRADERS")

    def test_gold_found_without_active_position_is_distinct(self):
        class EmptyPortfolioClient(AvailableClient):
            def get(self, path, params=None):
                if path.endswith("/portfolio/live"):
                    return {"positions": [], "socialTrades": []}
                return super().get(path, params)

        result = external.EtoroExternalSignalService(
            EmptyPortfolioClient(), max_traders=1, now=lambda: FIXED_NOW
        ).collect()
        self.assertEqual(result["api_status"], "GOLD_FOUND_NO_ACTIVE_POSITION")
        self.assertEqual(result["portfolio_summary"]["portfolio_ok"], 1)

    def test_portfolio_not_found_is_counted_separately(self):
        class MissingPortfolioClient(AvailableClient):
            def get(self, path, params=None):
                if path.endswith("/portfolio/live"):
                    raise external.EtoroUnavailable("missing", 404)
                return super().get(path, params)

        result = external.EtoroExternalSignalService(
            MissingPortfolioClient(), max_traders=1, now=lambda: FIXED_NOW
        ).collect()
        self.assertEqual(result["portfolio_summary"]["portfolio_403"], 0)
        self.assertEqual(result["portfolio_summary"]["portfolio_404"], 1)
        self.assertEqual(result["portfolio_diagnostics"][0]["http_status"], 404)

    def test_search_transport_failures_define_exact_final_state(self):
        expected = {
            401: "AUTH_ERROR",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            429: "RATE_LIMITED",
            503: "PROVIDER_ERROR",
        }

        class SearchFailureClient(AvailableClient):
            def __init__(self, status_code):
                super().__init__()
                self.status_code = status_code

            def get(self, path, params=None):
                if path == external.READ_ONLY_ENDPOINTS["instrument_search"]:
                    if self.status_code in (403, 404):
                        raise external.EtoroUnavailable("search failed", self.status_code)
                    raise external.EtoroAPIError("search failed", self.status_code)
                return super().get(path, params)

        for code, status in expected.items():
            with self.subTest(code=code):
                result = external.EtoroExternalSignalService(
                    SearchFailureClient(code), max_traders=1, now=lambda: FIXED_NOW
                ).collect()
                self.assertEqual(result["api_status"], status)
                self.assertEqual(
                    [row["http_status"] for row in result["instrument_search"]],
                    [code, code],
                )

    def test_diagnostic_logs_contain_only_sanitized_runtime_fields(self):
        result = external.EtoroExternalSignalService(
            AvailableClient(), max_traders=20, now=lambda: FIXED_NOW
        ).collect()
        log = "\n".join(external.diagnostic_log_lines(result, include_limits=True))
        self.assertIn("[ETORO] configured_max_traders=20", log)
        self.assertIn("[ETORO] effective_max_traders=20", log)
        self.assertIn("instrumentId=559", log)
        self.assertIn("portfolio_available=true", log)
        self.assertIn(
            "path=/api/v1/user-info/people/{username}/portfolio/live",
            log,
        )
        self.assertNotIn("goldobserver", log)
        self.assertNotIn("api_key", log.lower())

    def test_fallback_design_is_get_only_and_never_claims_live_positions(self):
        result = external.EtoroExternalSignalService(
            AvailableClient(private_portfolio=True), max_traders=1, now=lambda: FIXED_NOW
        ).collect()
        fallback = result["portfolio_fallback"]
        self.assertEqual(fallback["method"], "GET")
        self.assertEqual(
            fallback["path"],
            "/api/v2/portfolios/{username}/exposure/history",
        )
        self.assertEqual(fallback["direction_field"], "netExposurePct")
        self.assertFalse(fallback["live_positions"])
        self.assertEqual(fallback["status"], "NOT_EXECUTED")

    def test_score_effect_is_forced_to_zero_when_written(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            external.write_snapshot({"source": "etoro", "score_effect": 99}, output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["score_effect"], 0)

    def test_source_contract_contains_only_read_paths_and_no_ea_hook(self):
        source = (DASHBOARD / "external_signal_service.py").read_text(encoding="utf-8")
        server = (DASHBOARD / "server.py").read_text(encoding="utf-8")
        page = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        ea = (ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8")
        self.assertIn("self.session.get", source)
        for mutation in ("self.session.post", "self.session.put", "self.session.patch", "self.session.delete"):
            self.assertNotIn(mutation, source)
        self.assertNotIn("etoro", ea.lower())
        self.assertIn("external_signal", server)
        self.assertIn("CONSENSO EXTERNO — ETORO", page)
        self.assertIn("score_effect=0", page)

    def test_protected_trading_defaults_are_untouched(self):
        ea = (ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8")
        self.assertIn("input bool   EnableLiveTrading      = false;", ea)
        self.assertIn("input double RiskPercent             = 5.0;", ea)
        self.assertIn("input int    ArmScoreThreshold       = 58;", ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", ea)


if __name__ == "__main__":
    unittest.main()
