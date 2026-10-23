import json
import math
import os
import random
import tempfile
import unittest

from research.quant.features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    Bar,
    CausalBarSeries,
    LookAheadError,
    build_features,
    load_bars,
)

H = 3_600_000
BASE = 1_747_000_800_000  # arbitrary epoch ms aligned to the hour
STEP = {"M15": H // 4, "H1": H, "H4": 4 * H}
H4_START = BASE - 250 * 4 * H  # H4 history long enough for EMA200 warmup


def make_bars(tf, closes, start=BASE, spread=0.3, wiggle=None):
    step = STEP[tf]
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        w = wiggle[i] if wiggle else 0.5
        hi = max(o, c) + w
        lo = min(o, c) - w
        ts = start + i * step
        bars.append(Bar(tf, ts, ts + step, o, hi, lo, c, spread))
        prev = c
    return bars


def random_walk(n, seed, start=2000.0, scale=0.004):
    rng = random.Random(seed)
    out = [start]
    for _ in range(n - 1):
        out.append(out[-1] * math.exp(rng.gauss(0.0, scale)))
    return out


def universe(n_h1=400, seed=7):
    h1 = make_bars("H1", random_walk(n_h1, seed))
    h4 = make_bars("H4", random_walk(250 + n_h1 // 4, seed + 1), start=H4_START)
    m15 = make_bars("M15", random_walk(n_h1 * 4, seed + 2))
    return h1, h4, m15


def series(h1, h4, m15):
    return CausalBarSeries(h1), CausalBarSeries(h4), CausalBarSeries(m15)


class LeakageTests(unittest.TestCase):
    def test_future_and_forming_bars_do_not_change_features(self):
        h1, h4, m15 = universe()
        # as_of in the middle of a forming H1/H4/M15 bar
        as_of = h1[300].close_timestamp + 20 * 60_000
        base = build_features(*series(h1, h4, m15), as_of)
        self.assertTrue(all(base["features"][k] is not None for k in FEATURE_NAMES))

        def mutate(bars):
            out = []
            for b in bars:
                if b.close_timestamp > as_of:
                    f = 1.5
                    out.append(Bar(b.timeframe, b.timestamp, b.close_timestamp, b.open * f,
                                   b.high * f + 50, b.low * f - 50, b.close * f, 9.9))
                else:
                    out.append(b)
            last = out[-1]
            step = last.close_timestamp - last.timestamp
            out.append(Bar(last.timeframe, last.close_timestamp, last.close_timestamp + step,
                           1.0, 99999.0, 0.5, 5000.0, 1.0))
            return out

        # confirm a forming bar exists in each timeframe (open <= as_of < close)
        for bars in (h1, h4, m15):
            self.assertTrue(any(b.timestamp <= as_of < b.close_timestamp for b in bars))
        mutated = build_features(*series(mutate(h1), mutate(h4), mutate(m15)), as_of)
        self.assertEqual(base, mutated)

        truncated = build_features(*series(
            [b for b in h1 if b.close_timestamp <= as_of],
            [b for b in h4 if b.close_timestamp <= as_of],
            [b for b in m15 if b.close_timestamp <= as_of]), as_of)
        self.assertEqual(base, truncated)

    def test_window_never_returns_unclosed_bar(self):
        h1, _, _ = universe(100)
        s = CausalBarSeries(h1)
        as_of = h1[50].timestamp + 1  # bar 50 forming
        w = s.window(as_of, 10)
        self.assertEqual(w[-1], h1[49])
        self.assertTrue(all(b.close_timestamp <= as_of for b in w))
        self.assertIsNone(s.window(as_of, 51))

    def test_lookahead_assertion_on_corrupted_index(self):
        h1, _, _ = universe(50)
        s = CausalBarSeries(h1)
        s._close_ts = tuple(t - 10 * H for t in s._close_ts)  # corrupt internals deliberately
        with self.assertRaises(LookAheadError):
            s.window(h1[10].close_timestamp, 3)


class WarmupTests(unittest.TestCase):
    def test_no_bars_all_none(self):
        h1, _, m15 = universe(400)
        h4 = make_bars("H4", random_walk(100, 1))  # starts at BASE, like H1/M15
        r = build_features(*series(h1, h4, m15), BASE - 1)
        self.assertTrue(all(v is None for v in r["features"].values()))
        self.assertEqual(r["feature_version"], FEATURE_VERSION)
        self.assertIs(r["observer_only"], True)
        self.assertEqual(r["score_effect"], 0)
        self.assertEqual(tuple(r["features"]), FEATURE_NAMES)

    def test_partial_warmup(self):
        h1, _, m15 = universe(400)
        h4 = make_bars("H4", random_walk(100, 1))  # only ~8 H4 bars closed at H1 bar 29
        s = series(h1, h4, m15)
        as_of = h1[29].close_timestamp  # 30 closed H1 bars
        f = build_features(*s, as_of)["features"]
        for k in ("h1_atr14", "h1_rv24", "h1_rsi14", "h1_dist_ema20_atr", "h1_efficiency_20",
                  "h1_tsmom_24", "server_hour", "m15_rsi14", "m15_ret4_atr"):
            self.assertIsNotNone(f[k], k)
        for k in ("h1_rv120", "h1_vol_ratio", "h1_tsmom_120", "h1_dist_ema50_atr",
                  "h1_ema50_slope_atr", "h1_variance_ratio_4", "h1_range_pos_50",
                  "h4_trend_sign", "h4_dist_ema50_atr"):
            self.assertIsNone(f[k], k)
        # exact thresholds
        self.assertIsNone(build_features(*s, h1[13].close_timestamp)["features"]["h1_atr14"])
        self.assertIsNotNone(build_features(*s, h1[14].close_timestamp)["features"]["h1_atr14"])
        self.assertIsNone(build_features(*s, h1[23].close_timestamp)["features"]["h1_rv24"])
        self.assertIsNotNone(build_features(*s, h1[24].close_timestamp)["features"]["h1_rv24"])
        self.assertIsNone(build_features(*s, h1[48].close_timestamp)["features"]["h1_range_pos_50"])
        self.assertIsNotNone(build_features(*s, h1[49].close_timestamp)["features"]["h1_range_pos_50"])


class KnownValueTests(unittest.TestCase):
    def test_constant_series(self):
        n = 300
        h1 = make_bars("H1", [2000.0] * n)
        h4 = make_bars("H4", [2000.0] * 330, start=H4_START)
        m15 = make_bars("M15", [2000.0] * (n * 4))
        f = build_features(*series(h1, h4, m15), h1[-1].close_timestamp)["features"]
        self.assertEqual(f["h1_rv24"], 0.0)
        self.assertEqual(f["h1_rv120"], 0.0)
        self.assertIsNone(f["h1_vol_ratio"])
        self.assertIsNone(f["h1_tsmom_24"])
        self.assertIsNone(f["h1_variance_ratio_4"])
        self.assertIsNone(f["h1_efficiency_20"])
        self.assertIsNone(f["h1_rsi14"])
        self.assertAlmostEqual(f["h1_atr14"], 1.0)  # high-low = 2*0.5
        self.assertAlmostEqual(f["h1_dist_ema20_atr"], 0.0)
        self.assertAlmostEqual(f["h1_ema50_slope_atr"], 0.0)
        self.assertAlmostEqual(f["h1_range_pos_50"], 0.5)
        self.assertEqual(f["h4_trend_sign"], 0.0)
        self.assertAlmostEqual(f["h1_spread_atr"], 0.3)
        self.assertEqual(f["m15_ret4_atr"], 0.0)

    def test_linear_trend(self):
        n = 300
        closes = [2000.0 + i for i in range(n)]
        h1 = make_bars("H1", closes, wiggle=[0.0] * n)  # high == close on up bars
        h4 = make_bars("H4", [2000.0 + 4 * i for i in range(330)], start=H4_START)
        m15 = make_bars("M15", [2000.0 + 0.25 * i for i in range(n * 4)])
        f = build_features(*series(h1, h4, m15), h1[-1].close_timestamp)["features"]
        self.assertAlmostEqual(f["h1_efficiency_20"], 1.0)
        self.assertAlmostEqual(f["h1_range_pos_50"], 1.0)
        self.assertEqual(f["h1_rsi14"], 100.0)
        self.assertAlmostEqual(f["h1_atr14"], 1.0)
        self.assertEqual(f["h4_trend_sign"], 1.0)
        self.assertGreater(f["h1_dist_ema20_atr"], 0.0)
        # linear EMA lag: close - EMA_p -> (p-1)/2 for slope 1/bar
        self.assertAlmostEqual(f["h1_dist_ema20_atr"], 9.5, places=6)
        self.assertAlmostEqual(f["h1_ema50_slope_atr"], 1.0, places=6)
        self.assertEqual(f["m15_rsi14"], 100.0)

    def test_calendar_and_spread(self):
        # 2025-05-12 is a Monday; start at 13:00 wall time
        start = 1_747_054_800_000  # 2025-05-12T13:00:00Z
        h1 = make_bars("H1", random_walk(40, 3), start=start, spread=None)
        h4 = make_bars("H4", random_walk(5, 4), start=start)
        m15 = make_bars("M15", random_walk(40, 5), start=start)
        f = build_features(*series(h1, h4, m15), h1[2].close_timestamp)["features"]
        self.assertEqual(f["server_hour"], 15.0)  # last closed bar opened 15:00
        self.assertEqual(f["server_weekday"], 0.0)
        self.assertIsNone(f["h1_spread_atr"])

    def test_variance_ratio_direction(self):
        rng = random.Random(11)
        # strongly mean-reverting alternating returns -> VR << 1
        closes = [2000.0]
        for i in range(200):
            closes.append(closes[-1] * math.exp((0.004 if i % 2 else -0.004) + rng.gauss(0, 1e-4)))
        h1 = make_bars("H1", closes)
        f = build_features(*series(h1, make_bars("H4", [1.0] * 3), make_bars("M15", [1.0] * 3)),
                           h1[-1].close_timestamp)["features"]
        self.assertLess(f["h1_variance_ratio_4"], 0.3)


class LoadBarsTests(unittest.TestCase):
    def _write(self, lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write("\n".join(lines) + "\n")
        self.addCleanup(os.remove, path)
        return path

    @staticmethod
    def _rec(ts, tf="H1", **kw):
        r = {"bar_id": f"x-{ts}", "timeframe": tf, "timestamp": ts, "close_timestamp": ts + H,
             "timestamp_unit": "epoch_ms_mt5_server_wall_time", "open": 10.0, "high": 11.0,
             "low": 9.0, "close": 10.5, "tick_count": 5, "avg_spread": 0.3,
             "min_spread": 0.3, "max_spread": 0.3}
        r.update(kw)
        return json.dumps(r)

    def test_skips_blank_lines(self):
        path = self._write([self._rec(BASE), "", "   ", self._rec(BASE + H), ""])
        bars = load_bars(path, timeframe="H1")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[1].timestamp, BASE + H)
        self.assertEqual(bars[0].avg_spread, 0.3)

    def test_missing_spread_is_none(self):
        bars = load_bars(self._write([self._rec(BASE, avg_spread=None)]))
        self.assertIsNone(bars[0].avg_spread)

    def test_rejects_duplicate(self):
        path = self._write([self._rec(BASE), self._rec(BASE)])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_bars(path)

    def test_rejects_non_monotonic(self):
        path = self._write([self._rec(BASE + H), self._rec(BASE)])
        with self.assertRaisesRegex(ValueError, "non-monotonic"):
            load_bars(path)

    def test_rejects_timeframe_mismatch(self):
        with self.assertRaises(ValueError):
            load_bars(self._write([self._rec(BASE)]), timeframe="H4")
        with self.assertRaises(ValueError):
            load_bars(self._write([self._rec(BASE), self._rec(BASE + H, tf="H4")]))

    def test_series_rejects_bad_order(self):
        bars = make_bars("H1", [1.0, 2.0, 3.0])
        with self.assertRaises(ValueError):
            CausalBarSeries([bars[1], bars[0]])
        with self.assertRaises(ValueError):
            CausalBarSeries([bars[0], bars[0]])


def naive_ema(closes, p):
    if len(closes) < p:
        return None
    v = sum(closes[:p]) / p
    a = 2 / (p + 1)
    for c in closes[p:]:
        v = a * c + (1 - a) * v
    return v


def naive_atr(bars, p=14):
    if len(bars) <= p:
        return None
    trs = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
           for a, b in zip(bars, bars[1:])]
    v = sum(trs[:p]) / p
    for t in trs[p:]:
        v = (v * (p - 1) + t) / p
    return v


def naive_rsi(closes, p=14):
    if len(closes) <= p:
        return None
    d = [b - a for a, b in zip(closes, closes[1:])]
    g = sum(max(x, 0) for x in d[:p]) / p
    lo = sum(max(-x, 0) for x in d[:p]) / p
    for x in d[p:]:
        g = (g * (p - 1) + max(x, 0)) / p
        lo = (lo * (p - 1) + max(-x, 0)) / p
    return 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)


class CacheConsistencyTests(unittest.TestCase):
    def test_cached_arrays_match_naive_recompute(self):
        rng = random.Random(42)
        h1 = make_bars("H1", random_walk(320, 42), wiggle=[rng.uniform(0.1, 3.0) for _ in range(320)])
        s = CausalBarSeries(h1)
        cache = s.indicator_cache
        for i in (0, 13, 14, 15, 19, 20, 49, 50, 120, 199, 200, 319):
            prefix = h1[: i + 1]
            closes = [b.close for b in prefix]
            for p in (20, 50, 200):
                exp, got = naive_ema(closes, p), cache[f"ema{p}"][i]
                if exp is None:
                    self.assertIsNone(got)
                else:
                    self.assertAlmostEqual(got, exp, places=8)
            exp, got = naive_atr(prefix), cache["atr14"][i]
            self.assertIsNone(got) if exp is None else self.assertAlmostEqual(got, exp, places=9)
            exp, got = naive_rsi(closes), cache["rsi14"][i]
            self.assertIsNone(got) if exp is None else self.assertAlmostEqual(got, exp, places=9)
            # value_at through the causal choke point equals the array value
            self.assertEqual(s.value_at("atr14", h1[i].close_timestamp), cache["atr14"][i])

    def test_rv_and_tsmom_match_naive(self):
        h1, h4, m15 = universe(300, seed=9)
        j = 250
        f = build_features(*series(h1, h4, m15), h1[j].close_timestamp)["features"]
        closes = [b.close for b in h1[: j + 1]]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        def sd(x):
            m = sum(x) / len(x)
            return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))
        rv24 = sd(rets[-24:])
        self.assertAlmostEqual(f["h1_rv24"], rv24, places=12)
        self.assertAlmostEqual(f["h1_rv120"], sd(rets[-120:]), places=12)
        self.assertAlmostEqual(f["h1_tsmom_6"], math.log(closes[-1] / closes[-7]) / (rv24 * math.sqrt(6)),
                               places=9)


class ClosedUptoTests(unittest.TestCase):
    def test_boundary_inclusive(self):
        bars = make_bars("H1", [1.0, 2.0, 3.0, 4.0])
        s = CausalBarSeries(bars)
        self.assertEqual(s.closed_upto(bars[1].close_timestamp), 2)
        self.assertEqual(s.closed_upto(bars[1].close_timestamp - 1), 1)
        self.assertEqual(s.closed_upto(bars[0].timestamp), 0)
        self.assertEqual(s.closed_upto(bars[-1].close_timestamp + 10 * H), 4)
        self.assertEqual(s.window(bars[1].close_timestamp, 2), bars[:2])


if __name__ == "__main__":
    unittest.main()
