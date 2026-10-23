"""Deterministic synthetic tests for research.quant.stats."""

from __future__ import annotations

import math
import random
import unittest

from research.quant.stats import (BOOTSTRAP_METHOD, Fold, benjamini_hochberg,
                                  bootstrap_mean_ci, bootstrap_pvalue_mean_gt_zero,
                                  cscv_pbo, deflated_sharpe_ratio, expectancy,
                                  expected_max_sharpe, normal_cdf, normal_ppf,
                                  probabilistic_sharpe_ratio, profit_factor,
                                  purged_walk_forward_folds, sharpe_ratio, win_rate)


def gaussian(n: int, mean: float, sd: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def ar1(n: int, mean: float, phi: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    out, x = [], 0.0
    for _ in range(n):
        x = phi * x + rng.gauss(0, 1)
        out.append(mean + x)
    return out


class BasicMetricTests(unittest.TestCase):
    def test_expectancy_and_win_rate(self) -> None:
        self.assertIsNone(expectancy([]))
        self.assertIsNone(win_rate([]))
        self.assertAlmostEqual(expectancy([1, -1, 2, 0]), 0.5)
        self.assertAlmostEqual(win_rate([1, -1, 2, 0]), 0.5)

    def test_profit_factor_edge_cases(self) -> None:
        self.assertIsNone(profit_factor([]))
        self.assertIsNone(profit_factor([0.0, 0.0]))
        self.assertEqual(profit_factor([1.0, 2.0]), float("inf"))
        self.assertEqual(profit_factor([-1.0, -2.0]), 0.0)
        self.assertAlmostEqual(profit_factor([3.0, -1.0, -0.5, 0.0]), 2.0)


class NormalTests(unittest.TestCase):
    def test_cdf_known_values(self) -> None:
        self.assertAlmostEqual(normal_cdf(0.0), 0.5, places=15)
        self.assertAlmostEqual(normal_cdf(1.959963984540054), 0.975, places=12)
        self.assertAlmostEqual(normal_cdf(-1.0), 0.15865525393145707, places=12)
        self.assertAlmostEqual(normal_cdf(-6.0), 9.865876450376946e-10, delta=1e-20)

    def test_ppf_known_values(self) -> None:
        self.assertAlmostEqual(normal_ppf(0.5), 0.0, places=12)
        self.assertAlmostEqual(normal_ppf(0.975), 1.959963984540054, places=10)
        self.assertAlmostEqual(normal_ppf(0.01), -2.3263478740408408, places=10)
        self.assertAlmostEqual(normal_ppf(1e-9), -5.997807015007686, places=8)
        self.assertEqual(normal_ppf(0.0), float("-inf"))
        with self.assertRaises(ValueError):
            normal_ppf(1.5)

    def test_ppf_inverts_cdf(self) -> None:
        for p in (1e-6, 0.02, 0.0243, 0.3, 0.7, 0.9757, 0.999):
            self.assertAlmostEqual(normal_cdf(normal_ppf(p)), p, delta=1e-12 + p * 1e-10)


class BootstrapTests(unittest.TestCase):
    def test_ci_contains_true_mean_and_shape(self) -> None:
        data = gaussian(400, 0.3, 1.0, seed=1)
        ci = bootstrap_mean_ci(data, n_boot=1000, seed=7)
        self.assertEqual(set(ci), {"mean", "lower", "upper", "n", "block_size", "method"})
        self.assertEqual(ci["method"], BOOTSTRAP_METHOD)
        self.assertEqual(ci["n"], 400)
        self.assertLess(ci["lower"], 0.3)
        self.assertGreater(ci["upper"], 0.3)
        self.assertLess(ci["lower"], ci["mean"])
        self.assertGreater(ci["upper"], ci["mean"])

    def test_ci_deterministic_with_seed(self) -> None:
        data = gaussian(100, 0.1, 1.0, seed=2)
        a = bootstrap_mean_ci(data, n_boot=300, block_size=5, seed=11)
        b = bootstrap_mean_ci(data, n_boot=300, block_size=5, seed=11)
        c = bootstrap_mean_ci(data, n_boot=300, block_size=5, seed=12)
        self.assertEqual(a, b)
        self.assertNotEqual(a["lower"], c["lower"])

    def test_ci_small_samples(self) -> None:
        self.assertIsNone(bootstrap_mean_ci([])["mean"])
        one = bootstrap_mean_ci([1.5])
        self.assertEqual(one["mean"], 1.5)
        self.assertIsNone(one["lower"])
        self.assertIsNone(one["upper"])

    def test_block_bootstrap_widens_ci_for_autocorrelated_series(self) -> None:
        data = ar1(600, 0.0, 0.8, seed=3)
        iid = bootstrap_mean_ci(data, n_boot=800, block_size=1, seed=4)
        block = bootstrap_mean_ci(data, n_boot=800, block_size=20, seed=4)
        self.assertGreater(block["upper"] - block["lower"], 1.5 * (iid["upper"] - iid["lower"]))

    def test_pvalue_small_for_positive_large_for_zero_mean(self) -> None:
        positive = gaussian(300, 0.4, 1.0, seed=5)
        noise = [v - sum(gaussian(300, 0, 1, 6)) / 300 for v in gaussian(300, 0, 1, 6)]
        p_pos = bootstrap_pvalue_mean_gt_zero(positive, n_boot=1000, seed=1)
        p_zero = bootstrap_pvalue_mean_gt_zero(noise, n_boot=1000, seed=1)
        self.assertLess(p_pos, 0.01)
        self.assertGreater(p_zero, 0.3)
        negative = [-v for v in positive]
        self.assertGreater(bootstrap_pvalue_mean_gt_zero(negative, n_boot=1000, seed=1), 0.99)
        self.assertIsNone(bootstrap_pvalue_mean_gt_zero([1.0]))

    def test_pvalue_deterministic(self) -> None:
        data = gaussian(80, 0.1, 1.0, seed=8)
        self.assertEqual(bootstrap_pvalue_mean_gt_zero(data, n_boot=200, block_size=4, seed=3),
                         bootstrap_pvalue_mean_gt_zero(data, n_boot=200, block_size=4, seed=3))


class SharpeTests(unittest.TestCase):
    def test_sharpe_basic(self) -> None:
        self.assertIsNone(sharpe_ratio([1.0]))
        self.assertIsNone(sharpe_ratio([2.0, 2.0, 2.0]))
        self.assertAlmostEqual(sharpe_ratio([1.0, 2.0, 3.0]), 2.0)

    def test_psr_monotonic_in_sr(self) -> None:
        base = gaussian(250, 0.0, 1.0, seed=9)
        psrs = [probabilistic_sharpe_ratio([v + shift for v in base])
                for shift in (-0.1, 0.0, 0.05, 0.1, 0.2)]
        self.assertTrue(all(a < b for a, b in zip(psrs, psrs[1:])))
        self.assertTrue(all(0 <= p <= 1 for p in psrs))

    def test_psr_matches_normal_formula_and_benchmark(self) -> None:
        data = gaussian(500, 0.25, 1.0, seed=10)
        sr = sharpe_ratio(data)
        self.assertGreater(probabilistic_sharpe_ratio(data), 0.99)
        # Hand-computed formula on a small skewed sample.
        small = [1.0, -0.5, 2.0, 0.3, -1.0, 0.8]
        n = len(small)
        mu = sum(small) / n
        m2 = sum((v - mu) ** 2 for v in small) / n
        g3 = sum((v - mu) ** 3 for v in small) / n / m2 ** 1.5
        g4 = sum((v - mu) ** 4 for v in small) / n / m2 ** 2
        s = mu / math.sqrt(m2 * n / (n - 1))
        expected = normal_cdf((s - 0.1) * math.sqrt(n - 1)
                              / math.sqrt(1 - g3 * s + (g4 - 1) / 4 * s * s))
        self.assertAlmostEqual(probabilistic_sharpe_ratio(small, 0.1), expected, places=12)
        self.assertAlmostEqual(probabilistic_sharpe_ratio(data, sr_benchmark=sr), 0.5)
        self.assertLess(probabilistic_sharpe_ratio(data, sr_benchmark=sr + 0.1), 0.5)
        self.assertIsNone(probabilistic_sharpe_ratio([1.0, 2.0]))

    def test_expected_max_sharpe(self) -> None:
        self.assertEqual(expected_max_sharpe(1, 0.01), 0.0)
        e10 = expected_max_sharpe(10, 0.01)
        e100 = expected_max_sharpe(100, 0.01)
        self.assertGreater(e100, e10)
        self.assertGreater(e10, 0)
        # Known value: sqrt(1)*((1-g)*Phi^-1(0.9) + g*Phi^-1(1-1/(10e))) ~= 1.5745
        self.assertAlmostEqual(expected_max_sharpe(10, 1.0), 1.5745, places=3)

    def test_dsr_below_psr_with_trials(self) -> None:
        data = gaussian(400, 0.12, 1.0, seed=11)
        psr = probabilistic_sharpe_ratio(data)
        self.assertEqual(deflated_sharpe_ratio(data, n_trials=1, sr_variance=0.01), psr)
        dsr = deflated_sharpe_ratio(data, n_trials=50, sr_variance=0.0025)
        self.assertLess(dsr, psr)
        more = deflated_sharpe_ratio(data, n_trials=500, sr_variance=0.0025)
        self.assertLess(more, dsr)


class BenjaminiHochbergTests(unittest.TestCase):
    def test_textbook_example(self) -> None:
        # Benjamini & Hochberg (1995) example: 4 rejections at q=0.05.
        p = [0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344,
             0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.0000]
        self.assertEqual(benjamini_hochberg(p, q=0.05), [True] * 4 + [False] * 11)

    def test_order_preserved_none_and_step_up(self) -> None:
        # Step-up: 0.04 at rank 4 of 4 passes (0.04 <= 0.04*... q=0.1 -> 0.1), so all pass.
        self.assertEqual(benjamini_hochberg([0.04, 0.03, 0.02, 0.01], q=0.1), [True] * 4)
        result = benjamini_hochberg([0.5, None, 0.001, 0.2], q=0.10)
        self.assertEqual(result, [False, False, True, False])
        self.assertEqual(benjamini_hochberg([]), [])


class WalkForwardTests(unittest.TestCase):
    def make_events(self, n: int = 600, horizon: int = 7_200_000, seed: int = 12):
        rng = random.Random(seed)
        times = [i * 900_000 for i in range(n)]
        order = list(range(n))
        rng.shuffle(order)  # unsorted input to prove index mapping
        events = [times[i] for i in order]
        ends = [t + rng.randint(0, horizon) for t in events]
        return events, ends

    def test_folds_no_train_label_overlaps_test(self) -> None:
        events, ends = self.make_events()
        folds = purged_walk_forward_folds(events, ends, n_folds=5, min_train=10)
        self.assertEqual(len(folds), 5)
        for fold in folds:
            self.assertIsInstance(fold, Fold)
            self.assertFalse(set(fold.train) & set(fold.test))
            self.assertEqual(fold.test_start_ms, min(events[i] for i in fold.test))
            self.assertEqual(fold.test_end_ms, max(ends[i] for i in fold.test))
            for i in fold.train:
                self.assertLess(ends[i], fold.test_start_ms)
                self.assertLess(events[i], fold.test_start_ms)

    def test_expanding_window_and_contiguous_tests(self) -> None:
        events, ends = self.make_events()
        folds = purged_walk_forward_folds(events, ends, n_folds=5, min_train=10)
        sizes = [len(f.train) for f in folds]
        self.assertEqual(sizes, sorted(sizes))
        all_test = [i for f in folds for i in f.test]
        self.assertEqual(len(all_test), len(set(all_test)))
        self.assertEqual(len(all_test), 500)  # blocks 1..5 of 6 equal blocks
        for prev, nxt in zip(folds, folds[1:]):
            self.assertLess(max(events[i] for i in prev.test), nxt.test_start_ms)

    def test_purge_actually_removes_overlapping_labels(self) -> None:
        events, ends = self.make_events()
        folds = purged_walk_forward_folds(events, ends, n_folds=5, min_train=10)
        order = sorted(range(len(events)), key=lambda i: events[i])
        first = folds[0]
        candidates = order[:100]
        self.assertLess(len(first.train), len(candidates))
        purged = set(candidates) - set(first.train)
        self.assertTrue(all(ends[i] >= first.test_start_ms for i in purged))

    def test_embargo_removes_more(self) -> None:
        events, ends = self.make_events()
        embargo = 3_600_000
        plain = purged_walk_forward_folds(events, ends, n_folds=5, min_train=10)
        emb = purged_walk_forward_folds(events, ends, n_folds=5, embargo_ms=embargo,
                                        min_train=10)
        for a, b in zip(plain, emb):
            self.assertTrue(set(b.train) <= set(a.train))
            self.assertLess(len(b.train), len(a.train))
            for i in b.train:
                self.assertLess(ends[i], b.test_start_ms - embargo)

    def test_min_train_skips_folds_without_raising(self) -> None:
        events, ends = self.make_events()
        folds = purged_walk_forward_folds(events, ends, n_folds=5, min_train=250)
        self.assertTrue(folds)
        self.assertTrue(all(len(f.train) >= 250 for f in folds))
        self.assertNotIn(0, [f.index for f in folds])
        self.assertEqual(purged_walk_forward_folds([1, 2], [1, 2], n_folds=5), [])

    def test_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            purged_walk_forward_folds([1, 2], [1], n_folds=1)
        with self.assertRaises(ValueError):
            purged_walk_forward_folds([5, 6], [4, 6], n_folds=1, min_train=0)


class PboTests(unittest.TestCase):
    def matrix(self, rows: int, configs: int, seed: int, edge: float = 0.0):
        rng = random.Random(seed)
        return [[rng.gauss(edge if c == 0 else 0.0, 1.0) for c in range(configs)]
                for _ in range(rows)]

    def test_superior_config_low_pbo(self) -> None:
        result = cscv_pbo(self.matrix(800, 10, seed=13, edge=0.4), n_splits=16)
        self.assertLess(result["pbo"], 0.05)
        self.assertGreater(result["logits_median"], 0)
        self.assertEqual(result["metric"], "sharpe")
        self.assertEqual(result["n_configs"], 10)
        self.assertEqual(result["n_combinations"], 500)

    def test_noise_configs_pbo_near_half(self) -> None:
        result = cscv_pbo(self.matrix(640, 30, seed=14), n_splits=16)
        self.assertGreater(result["pbo"], 0.2)
        self.assertLess(result["pbo"], 0.8)

    def test_deterministic_and_full_enumeration(self) -> None:
        m = self.matrix(120, 5, seed=15)
        a = cscv_pbo(m, n_splits=8)
        self.assertEqual(a["n_combinations"], math.comb(8, 4))
        self.assertEqual(a, cscv_pbo(m, n_splits=8))
        big = self.matrix(320, 5, seed=16)
        self.assertEqual(cscv_pbo(big, n_splits=16, seed=3), cscv_pbo(big, n_splits=16, seed=3))

    def test_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            cscv_pbo(self.matrix(100, 4, seed=1), n_splits=7)
        with self.assertRaises(ValueError):
            cscv_pbo(self.matrix(10, 4, seed=1), n_splits=16)
        with self.assertRaises(ValueError):
            cscv_pbo(self.matrix(100, 1, seed=1), n_splits=4)


if __name__ == "__main__":
    unittest.main()
