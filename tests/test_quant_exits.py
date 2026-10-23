"""Synthetic exit fills, chronology and offline-study validation."""

from pathlib import Path
import random
import unittest
from unittest.mock import patch

from research.quant import barriers, edge_study, features
from research.quant.exits import HOUR_MS, MAX_HOLD_MS, POLICIES, ExitPolicy, simulate_exit
from research.quant import exits_study as study

M = barriers.BAR_MS
ZERO = barriers.COST_SCENARIOS["ZERO"]
P = {p.name: p for p in POLICIES}


def path(rows, times=None):
    return barriers.M1Path(dict(timestamp=t, open=o, high=h, low=l, close=c)
                           for t, (o, h, l, c) in zip(times if times is not None else range(0, len(rows) * M, M), rows))


def simulate(rows, policy="FIXED_2", **kwargs):
    args = dict(signal_time_ms=0, direction=1, stop_dist=1.0, atr=1.0,
                policy=P[policy] if isinstance(policy, str) else policy,
                cost=ZERO, max_hold_ms=len(rows) * M)
    args.update(kwargs)
    return simulate_exit(path(rows), **args)


def event(t, direction=1, r=1.0, end=None, status="OK"):
    c = edge_study.Candidate(str(t), t, direction, 80, {}, 1.0)
    e = study.Event(c)
    for p in POLICIES:
        for cost in study.COSTS:
            e.outcomes[(p.name, cost)] = dict(
                status=status, entry_time_ms=t, exit_time_ms=end if end is not None else t + M,
                r_multiple=r if status == "OK" else None, horizon_gap_ms=None,
                legs=[dict(exit_time_ms=t + M, r_multiple=r)])
    return e


class ExitFillTests(unittest.TestCase):
    def test_fixed_tp_long_and_short(self):
        for d, row in ((1, (100, 102.5, 99.5, 102)), (-1, (100, 100.5, 97.5, 98))):
            with self.subTest(d=d):
                out = simulate([row], direction=d)
                self.assertEqual(out["r_multiple"], 2)
                self.assertEqual(out["exit_time_ms"], M)

    def test_same_bar_stop_first(self):
        for policy in ("FIXED_2", "PARTIAL_1_TO_2", "BE_1_TO_2"):
            out = simulate([(100, 103, 98, 100)], policy)
            self.assertEqual(out["r_multiple"], -1)
            self.assertTrue(out["ambiguous_bar"])
            self.assertFalse(out["partial_taken"])

    def test_gap_stop_is_adverse(self):
        out = simulate([(100, 100.1, 99.9, 100), (98, 98.5, 97, 98)])
        self.assertEqual(out["r_multiple"], -2)
        self.assertEqual(out["exit_time_ms"], M)
        self.assertEqual(out["exit_reason"], "SL_GAP")

    def test_positive_gap_only_credits_tp_level(self):
        out = simulate([(100, 100.1, 99.9, 100), (104, 105, 103, 104)])
        self.assertEqual(out["r_multiple"], 2)
        self.assertEqual(out["exit_time_ms"], M)

    def test_costs_partial_proportional(self):
        cost = barriers.CostScenario("test", .4, .1, .2)
        # Entry 100.3; TP bid levels 101.3/102.3 -> mid 101.5/102.5.
        out = simulate([(100, 100.1, 99.9, 100), (101.7, 103, 101.6, 102.8)],
                       "PARTIAL_1_TO_2", cost=cost)
        self.assertAlmostEqual(out["r_multiple"], 1.5 - .1 - .2)
        self.assertEqual([leg["weight"] for leg in out["legs"]], [.5, .5])

    def test_short_partial_mirrors_long(self):
        rows = [(100, 101.2, 99.8, 101), (101, 102.2, 100.8, 102)]
        mirrored = [(200-o, 200-l, 200-h, 200-c) for o, h, l, c in rows]
        a = simulate(rows, "PARTIAL_1_TO_2")
        b = simulate(mirrored, "PARTIAL_1_TO_2", direction=-1)
        self.assertEqual(a["r_multiple"], b["r_multiple"])
        self.assertEqual(a["r_multiple"], 1.5)

    def test_partial_then_initial_stop(self):
        out = simulate([(100, 101.2, 99.8, 101), (100, 100.2, 98.5, 99)], "PARTIAL_1_TO_3")
        self.assertEqual(out["r_multiple"], 0)
        self.assertTrue(out["partial_taken"])

    def test_known_open_partial_before_intrabar_stop(self):
        out = simulate([(100, 100.2, 99.8, 100), (101.2, 101.5, 98, 99)], "PARTIAL_1_TO_3")
        self.assertEqual(out["r_multiple"], 0)
        self.assertTrue(out["partial_taken"])

    def test_be_does_not_apply_inside_activation_bar(self):
        out = simulate([(100, 101.2, 99.5, 100.5), (100.4, 100.6, 99.8, 100.1)], "BE_1_TO_2")
        self.assertEqual(out["exit_time_ms"], 2 * M)
        self.assertEqual(out["r_multiple"], 0)
        self.assertEqual(out["exit_reason"], "SL")

    def test_be_gap_can_lose(self):
        out = simulate([(100, 101.2, 99.8, 101), (99.6, 100, 99, 99.5)], "BE_1_TO_2")
        self.assertAlmostEqual(out["r_multiple"], -.4)

    def test_be_is_not_net_zero(self):
        cost = barriers.CostScenario("test", .2, .1, .05)
        out = simulate([(100, 101.6, 99.8, 101), (100.8, 100.9, 99.8, 100.2)],
                       "BE_1_TO_2", cost=cost)
        self.assertAlmostEqual(out["r_multiple"], -.15)

    def test_trailing_stop_uses_prior_closed_high(self):
        out = simulate([(100, 103, 99.8, 102), (102, 102.2, 101, 101.5)], "CHANDELIER_1.5")
        self.assertEqual(out["exit_time_ms"], 2 * M)
        self.assertAlmostEqual(out["r_multiple"], 1.5)

    def test_trailing_stop_never_widens(self):
        out = simulate([(100, 102, 99.8, 101), (101, 101.1, 100.6, 100.8),
                        (100.8, 101, 100.4, 100.5)], "CHANDELIER_1.5")
        self.assertEqual(out["r_multiple"], .5)

    def test_trailing_short(self):
        out = simulate([(100, 100.2, 97, 98), (98, 99, 97.8, 98.5)],
                       "CHANDELIER_1.5", direction=-1)
        self.assertEqual(out["r_multiple"], 1.5)

    def test_chandelier_does_not_count_entry_ask_as_executable_high(self):
        out = simulate([(100, 100, 100, 100), (99, 99.2, 98.6, 99)],
                       "CHANDELIER_1.5", stop_dist=2,
                       cost=barriers.CostScenario("test", .4, 0, 0))
        self.assertEqual(out["exit_reason"], "TIME")
        self.assertAlmostEqual(out["r_multiple"], -.7)

    def test_time_stop_exactly_eight_hours_no_future_close(self):
        rows = [(100, 100.1, 99.9, 100)] * 481
        rows[-1] = (100.1, 103, 99.5, 102)
        out = simulate(rows, "TIME_8H_TO_2")
        self.assertEqual(out["exit_reason"], "TIME_8H")
        self.assertEqual(out["exit_time_ms"], 8 * HOUR_MS)
        self.assertAlmostEqual(out["r_multiple"], .1)

    def test_time_stop_at_threshold_keeps_trade(self):
        rows = [(100, 100.1, 99.9, 100)] * 480 + [(100.25, 102.1, 100.2, 102)]
        self.assertEqual(simulate(rows, "TIME_8H_TO_2")["exit_reason"], "TP")

    def test_time_barrier_close(self):
        out = simulate([(100, 100.5, 99.5, 100.25)])
        self.assertEqual(out["exit_reason"], "TIME")
        self.assertEqual(out["r_multiple"], .25)
        self.assertEqual(out["horizon_gap_ms"], 0)

    def test_censored_partial_has_no_completed_return(self):
        out = simulate([(100, 101.1, 99.8, 101)], "PARTIAL_1_TO_3", max_hold_ms=MAX_HOLD_MS)
        self.assertEqual(out["status"], "INSUFFICIENT_PATH")
        self.assertTrue(out["partial_taken"])
        self.assertIsNone(out["r_multiple"])
        self.assertIsNone(out["exit_time_ms"])

    def test_no_entry(self):
        self.assertEqual(simulate([(100, 100, 100, 100)], signal_time_ms=2*M)["status"], "NO_ENTRY_BAR")

    def test_later_entry_uses_first_available_open(self):
        out = simulate([(100, 100, 100, 100), (101, 101.2, 100.8, 101)], signal_time_ms=1)
        self.assertEqual(out["entry_time_ms"], M)
        self.assertEqual(out["entry_price"], 101)

    def test_gap_horizon_is_explicit(self):
        data = path([(100, 100.1, 99.9, 100)] * 2, times=[0, 3 * HOUR_MS])
        out = simulate_exit(data, signal_time_ms=0, direction=1, stop_dist=1, atr=1,
                            policy=P["FIXED_2"], cost=ZERO, max_hold_ms=HOUR_MS)
        self.assertEqual(out["horizon_gap_ms"], HOUR_MS - M)

    def test_offline_flags(self):
        out = simulate([(100, 100, 100, 100)])
        self.assertTrue(out["observer_only"])
        self.assertEqual(out["score_effect"], 0)
        self.assertFalse(out["strategy_validated"])

    def test_validation(self):
        for kwargs in (dict(direction=0), dict(stop_dist=0), dict(atr=float("nan")),
                       dict(policy=ExitPolicy("posthoc", 7)), dict(max_hold_ms=0),
                       dict(cost=barriers.CostScenario("bad", -1, 0, 0))):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                simulate([(100, 100, 100, 100)], **kwargs)

    def test_fixed_fills_match_original_barriers_randomized(self):
        rng = random.Random(81)
        for direction in (-1, 1):
            for cost in barriers.COST_SCENARIOS.values():
                for tp, name in ((1.25, "FIXED_1.25"), (2, "FIXED_2")):
                    for _ in range(30):
                        rows, previous = [], 100.0
                        for _ in range(15):
                            o = previous + rng.uniform(-.3, .3)
                            c = o + rng.uniform(-.7, .7)
                            rows.append((o, max(o, c) + rng.random(), min(o, c) - rng.random(), c))
                            previous = c
                        data = path(rows)
                        original = barriers.triple_barrier(data, signal_time_ms=0, direction=direction,
                            stop_dist=1, tp_r=tp, max_hold_ms=15*M, cost=cost)
                        out = simulate(rows, name, direction=direction, cost=cost)
                        self.assertEqual(out["status"], original["status"])
                        self.assertAlmostEqual(out["r_multiple"], original["r_multiple"])


class StudyTests(unittest.TestCase):
    def test_frozen_grid_and_trial_count(self):
        self.assertEqual(len(POLICIES), 9)
        self.assertEqual(study.N_TRIALS, 142)

    def test_intrabar_slot_not_reused_at_same_open(self):
        a, b, c = event(0, end=M+M), event(M), event(2*M)
        self.assertEqual(study.select([a, b, c], "FIXED_2", "BASE", single_slot=True), [a, c])

    def test_slot_uses_actual_entry_and_same_cost(self):
        a, b = event(0, end=3*M), event(M)
        b.outcomes[("FIXED_2", "BASE")]["entry_time_ms"] = 4*M
        b.outcomes[("FIXED_2", "BASE")]["exit_time_ms"] = 5*M
        self.assertEqual(len(study.select([a,b], "FIXED_2", "BASE", single_slot=True)), 2)
        self.assertEqual(len(study.select([a,b], "FIXED_2", "STRESS", single_slot=True)), 1)

    def test_censoring_does_not_free_slot_retroactively(self):
        a, b, c = event(0, status="INSUFFICIENT_PATH"), event(HOUR_MS), event(MAX_HOLD_MS)
        selected = study.select([a,b,c], "FIXED_2", "BASE", single_slot=True)
        self.assertEqual(selected, [a,c])
        self.assertEqual(study.values(selected, "FIXED_2", "BASE"), [1])

    def test_direction_only_is_independent_portfolio(self):
        a, b = event(0, 1, end=5*M), event(M, -1)
        self.assertEqual(study.select([a,b], "FIXED_2", "BASE", -1, single_slot=True), [b])

    def test_sr_variance_uses_only_input_discovery(self):
        es = [event(i*2*HOUR_MS, r=(-1 if i%3 else 2)) for i in range(80)]
        first = study.discovery_sr_variance(es)
        # Outside objects cannot affect an explicitly passed train ledger.
        other = event(9999999999, r=1e9)
        self.assertEqual(study.discovery_sr_variance(es), first)
        self.assertGreater(first, 0)
        self.assertIsNone(study.discovery_sr_variance([other]))

    def test_summary_preserves_registered_block_size(self):
        self.assertEqual(study.summary([1, -1, 2], block=20, sr_variance=.1, seed=0)
                         ["bootstrap_block_size"], 20)

    def test_pbo_includes_zero_weeks_and_directional_columns(self):
        es = [event(0), event(2 * edge_study.WEEK_MS)]
        matrix, names = study.pbo_matrix(es)
        self.assertEqual(len(names), 27)
        self.assertEqual(matrix[1], [0] * 27)
        self.assertEqual(matrix[0][0], 1)

    def test_gate_requires_beating_both_fixed_tp_families(self):
        def row(policy, direction, expectancy):
            return dict(policy=policy, direction=direction, cost="STRESS", mode="single_slot",
                        expectancy_r=expectancy, n=150, ci_lower=.01, dsr=.99, profit_factor=1.5)
        rows = [row(p, d, .5 if p == "FIXED_1.25" and d == "LONG_ONLY" else .1)
                for p in ("FIXED_1.25", "FIXED_2") for d, _ in study.DIRECTIONS]
        rows += [row("BE_1_TO_2", "BOTH", .4), row("CHANDELIER_2", "BOTH", .6)]
        self.assertEqual([r["passes_gate"] for r in study.gate(rows)], [False, True])

    def test_gate_cannot_pass_without_all_benchmarks(self):
        rows = [dict(policy="FIXED_2", direction="BOTH", cost="STRESS", mode="single_slot",
                     expectancy_r=.1),
                dict(policy="BE_1_TO_2", direction="BOTH", cost="STRESS", mode="single_slot",
                     expectancy_r=.5, n=150, ci_lower=.1, dsr=.99, profit_factor=2)]
        self.assertFalse(study.gate(rows)[0]["passes_gate"])
        self.assertFalse(study.gate(rows)[0]["benchmarks_complete"])

    def test_evaluate_purges_horizon_and_keeps_slot_across_folds(self):
        es = [event(i*2*HOUR_MS, direction=1 if i%2 else -1,
                    r=2 if i%3 else -1, end=i*2*HOUR_MS+10*HOUR_MS) for i in range(1200)]
        def fast(vals, **kwargs):
            return dict(n=len(vals), expectancy_r=study.stats.expectancy(vals),
                        ci_lower=-1, ci_upper=1, profit_factor=1, dsr=.1,
                        p_value_mean_gt_0=.5, bootstrap_block_size=kwargs["block"],
                        max_drawdown_r=-1)
        with patch.object(study, "summary", side_effect=fast):
            report = study.evaluate(es)
        self.assertEqual(len(report["rows"]), 108)
        self.assertEqual(len(report["folds"]), 5)
        first = report["folds"][0]
        latest_train_t = (first["train_n"] - 1)*2*HOUR_MS
        self.assertLess(latest_train_t+MAX_HOLD_MS+M, first["test_start_ms"]-24*HOUR_MS)
        row = next(r for r in report["rows"] if r["policy"] == "FIXED_2" and
                   r["direction"] == "BOTH" and r["cost"] == "BASE" and r["mode"] == "single_slot")
        self.assertEqual(row["n"], 200)
        self.assertFalse(any(r["passes_gate"] for r in report["gate"]))
        self.assertIn("NINGUNA", study.render_markdown(report))

    def test_candidate_builder_reuses_edge_helpers_and_causal_atr(self):
        h = HOUR_MS
        def series(tf, step):
            return [features.Bar(tf, i*step, (i+1)*step, 100, 101, 99, 100, .3) for i in range(250)]
        raw = dict(event_id="e", observed_at=220*h, direction=1, score=74)
        with patch.object(features, "load_bars", side_effect=[series("H1", h), series("H4", 4*h),
                                                             series("M15", h//4)]), \
             patch.object(barriers.M1Path, "from_jsonl", return_value=path([(100,103,99.9,102)], [220*h])), \
             patch.object(edge_study, "iter_candidates", return_value=iter([raw])) as candidates:
            got = study.build_events(Path("synthetic"))
        candidates.assert_called_once_with(Path("synthetic/historical_decisions.jsonl"), min_score=74)
        self.assertEqual(got[0].candidate.stop_dist, 2.4)
        self.assertEqual(len(got[0].outcomes), 18)


if __name__ == "__main__":
    unittest.main()
