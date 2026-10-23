"""Synthetic tests for the Quant Research V1 edge-study orchestration helpers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research.quant import edge_study as es
from research.quant.features import Bar, CausalBarSeries

HOUR = es.HOUR_MS


def cand(t: int, direction: int, r: float, *, exit_after: int = HOUR, score: int = 80,
         feat: float | None = 1.0) -> es.Candidate:
    c = es.Candidate(f"e{t}", t, direction, score, {"h1_tsmom_6": feat, "h1_rsi14": 60.0}, 1.0)
    for cost in ("ZERO", "BASE", "STRESS"):
        c.outcomes[(cost, 1.25)] = {"status": "OK", "r_multiple": r, "entry_time_ms": t,
                                    "exit_time_ms": t + exit_after, "exit_reason": "TIME"}
    return c


class CandidateLoadingTests(unittest.TestCase):
    def test_first_armed_per_hour_and_direction_above_score(self) -> None:
        rows = [
            {"event_id": "a", "decision": "ARMED", "armed_direction": "LONG", "observed_at": HOUR,
             "long_score": 80, "short_score": 0},
            {"event_id": "b", "decision": "ARMED", "armed_direction": "LONG",
             "observed_at": HOUR + 900_000, "long_score": 90, "short_score": 0},
            {"event_id": "c", "decision": "ARMED", "armed_direction": "SHORT",
             "observed_at": HOUR + 900_000, "long_score": 0, "short_score": 70},
            {"event_id": "d", "decision": "NO_TRADE", "armed_direction": None,
             "observed_at": 2 * HOUR, "long_score": 99, "short_score": 0},
            {"event_id": "e", "decision": "ARMED", "armed_direction": "SHORT",
             "observed_at": 2 * HOUR, "long_score": 0, "short_score": 74},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n", encoding="utf-8")
            got = list(es.iter_candidates(path, min_score=74))
        self.assertEqual([g["event_id"] for g in got], ["a", "e"])
        self.assertEqual(got[1]["direction"], -1)


class SequencingTests(unittest.TestCase):
    def test_single_slot_skips_overlapping_signals(self) -> None:
        cs = [cand(0, 1, 1.0, exit_after=3 * HOUR), cand(HOUR, 1, -1.0), cand(3 * HOUR, -1, 0.5)]
        self.assertEqual([c.event_id for c in es.non_overlapping(cs, 1.25)], ["e0", f"e{3 * HOUR}"])

    def test_single_slot_uses_exit_times_of_evaluated_cost(self) -> None:
        a, b = cand(0, 1, 1.0, exit_after=HOUR), cand(2 * HOUR, 1, 1.0)
        a.outcomes[("STRESS", 1.25)]["exit_time_ms"] = 3 * HOUR
        self.assertEqual(len(es.non_overlapping([a, b], 1.25, "BASE")), 2)
        self.assertEqual(len(es.non_overlapping([a, b], 1.25, "STRESS")), 1)

    def test_intrabar_exit_keeps_slot_until_bar_close(self) -> None:
        # R1: TP touched inside the M1 bar opening at t=60s is only known by 120s.
        a, b = cand(0, 1, 1.0, exit_after=60_000), cand(60_000, 1, 1.0)
        for cost in ("ZERO", "BASE", "STRESS"):
            a.outcomes[(cost, 1.25)]["exit_reason"] = "TP"
        self.assertEqual([c.event_id for c in es.non_overlapping([a, b], 1.25, "BASE")], ["e0"])

    def test_sequencing_uses_actual_entry_time(self) -> None:
        a, b = cand(0, 1, 1.0, exit_after=2 * HOUR), cand(HOUR, 1, 1.0)
        for cost in ("ZERO", "BASE", "STRESS"):
            b.outcomes[(cost, 1.25)]["entry_time_ms"] = 3 * HOUR  # delayed first bar
        self.assertEqual(len(es.non_overlapping([a, b], 1.25, "BASE")), 2)

    def test_censored_trade_reserves_slot(self) -> None:
        # R4: a censored first trade must not free the slot for the next signal.
        a, b = cand(0, 1, 0.0), cand(60_000, 1, 1.0)
        for cost in ("ZERO", "BASE", "STRESS"):
            a.outcomes[(cost, 1.25)] = {"status": "INSUFFICIENT_PATH", "r_multiple": None,
                                        "entry_time_ms": 0, "exit_time_ms": None,
                                        "exit_reason": None}
        seq = es.non_overlapping([a, b], 1.25, "BASE", max_hold_ms=es.DAY_MS)
        self.assertEqual([c.event_id for c in seq], ["e0"])
        self.assertIsNone(seq[0].r("BASE", 1.25))

    def test_max_drawdown(self) -> None:
        self.assertEqual(es.max_drawdown([1.0, -2.0, 0.5, -1.0, 3.0]), -2.5)
        self.assertIsNone(es.max_drawdown([]))

    def test_model_vector_aligns_directional_features(self) -> None:
        names = ["h1_tsmom_6", "h1_rsi14"]
        self.assertEqual(es.model_vector(cand(0, -1, 0.0), names), [-1.0, -10.0, 80.0, -1.0])
        self.assertEqual(es.model_vector(cand(0, 1, 0.0, feat=None), names,
                                         include_direction=False), [None, 10.0, 80.0])

    def test_rule_matrix_buckets_by_week_and_filters(self) -> None:
        cs = [cand(0, 1, 1.0), cand(es.WEEK_MS + 1, -1, -0.5, score=90)]
        matrix, names = es.rule_matrix(cs, [("all", 1.25, 74, 0), ("short86", 1.25, 86, -1)],
                                       es.WEEK_MS)
        self.assertEqual(names, ["all", "short86"])
        self.assertEqual(matrix, [[1.0, 0.0], [-0.5, -0.5]])


class VerdictTests(unittest.TestCase):
    @staticmethod
    def slot(e: float) -> dict:
        return {"single_slot": {"n": 150, "expectancy_r": e, "ci_lower": 0.05, "dsr": 0.99,
                                "profit_factor": 1.5}}

    def test_rule_must_beat_direction_only_benchmarks(self) -> None:
        oos = {"tp2.0_BASELINE_STRESS": self.slot(0.10), "tp2.0_LONG_ONLY_STRESS": self.slot(0.30),
               "tp2.0_SHORT_ONLY_STRESS": self.slot(-0.1), "tp2.0_META_TOP50_STRESS": self.slot(0.25),
               "tp2.0_META_TOP30_STRESS": self.slot(0.35)}
        v = es.verdict({"walk_forward_oos": oos, "cscv_pbo_simple_rules": None})
        self.assertEqual(v["candidates_for_paper"], ["tp2.0_META_TOP30_STRESS"])
        self.assertEqual({r["rule"] for r in v["rules"]},
                         {"tp2.0_META_TOP50_STRESS", "tp2.0_META_TOP30_STRESS"})


class VerdictIncompleteBenchmarkTests(unittest.TestCase):
    def test_missing_benchmark_blocks_nomination(self) -> None:
        # R7: a rule cannot pass by beating only the benchmarks that exist.
        oos = {"tp2.0_BASELINE_STRESS": VerdictTests.slot(0.10),
               "tp2.0_META_TOP50_STRESS": VerdictTests.slot(0.50)}
        v = es.verdict({"walk_forward_oos": oos, "cscv_pbo_simple_rules": None})
        self.assertEqual(v["candidates_for_paper"], [])
        self.assertFalse(v["rules"][0]["benchmarks_complete"])


class DiscoveryVarianceTests(unittest.TestCase):
    def test_uses_only_pre_cutoff_candidates(self) -> None:
        early = [cand(i * 2 * HOUR, 1, (1.0 if i % 3 else -1.0), score=90) for i in range(30)]
        late = [cand(10_000 * HOUR + i * 2 * HOUR, 1, 50.0 * (i % 2), score=90) for i in range(30)]
        rules = [("a", 1.25, 74, 0), ("b", 1.25, 86, 1), ("c", 1.25, 74, -1)]
        base = es.discovery_sr_variance(early, rules, 10_000 * HOUR, es.DAY_MS)
        self.assertEqual(base, es.discovery_sr_variance(early + late, rules, 10_000 * HOUR,
                                                        es.DAY_MS))


class StructureDistanceTests(unittest.TestCase):
    def test_uses_only_closed_bars(self) -> None:
        bars = [Bar("H1", i * HOUR, (i + 1) * HOUR, 100.0, 101.0 + i, 99.0 - i, 100.0, 0.3)
                for i in range(25)]
        h1 = CausalBarSeries(bars)
        # As of the close of bar 20 the forming bars 21+ (wider ranges) are invisible.
        self.assertEqual(es.structure_distance(h1, 21 * HOUR, 1), 100.0 - (99.0 - 20))
        self.assertEqual(es.structure_distance(h1, 21 * HOUR, -1), (101.0 + 20) - 100.0)
        self.assertIsNone(es.structure_distance(h1, 5 * HOUR, 1))


class ConditionalEdgeTests(unittest.TestCase):
    def test_cuts_fixed_on_discovery_and_bh_applied(self) -> None:
        cs = []
        for i in range(400):
            feat = float(i % 10)
            cs.append(cand(i * HOUR, 1, 1.0 if feat >= 8 else -0.2, feat=feat))
        rows = es.conditional_edge(cs, ["h1_tsmom_6"], tp_r=1.25, discovery_fraction=0.6,
                                   q=0.10, seed=0)
        top = [r for r in rows if r["feature"] == "h1_tsmom_6" and r["quintile"] == 5][0]
        self.assertTrue(top["bh_discovery"])
        self.assertTrue(top["sign_persists_later"])
        self.assertEqual(top["discovery_n"] + top["later_n"], 80)


if __name__ == "__main__":
    unittest.main()
