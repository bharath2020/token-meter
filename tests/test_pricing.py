import unittest

from token_meter import pricing
from token_meter.usage import Tokens

TEN_K = Tokens(input=10_000, output=10_000, write_5m=10_000, write_1h=10_000, cache_read=10_000)


class PricingTest(unittest.TestCase):
    def assertCost(self, got, expected):
        self.assertIsNotNone(got)
        self.assertEqual(set(got), {"input", "output", "cache_write", "cache_read"})
        for k, v in expected.items():
            self.assertAlmostEqual(got[k], v, places=12, msg=k)

    def test_opus(self):
        self.assertCost(pricing.cost("claude-opus-5-5", TEN_K),
                        {"input": 0.04, "output": 0.2, "cache_write": 0.13, "cache_read": 0.002})

    def test_sonnet(self):
        self.assertCost(pricing.cost("claude-sonnet-5-5", TEN_K),
                        {"input": 0.02, "output": 0.1, "cache_write": 0.065, "cache_read": 0.002})

    def test_haiku(self):
        self.assertCost(pricing.cost("claude-haiku-5-5", TEN_K),
                        {"input": 0.001, "output": 0.005, "cache_write": 0.00325, "cache_read": 0.0001})

    def test_write_multipliers(self):
        for model, rates in pricing.PRICES.items():
            with self.subTest(model=model):
                self.assertAlmostEqual(rates.write_5m, rates.input * 1.25)
                self.assertAlmostEqual(rates.write_1h, rates.input * 2.0)
                c5 = pricing.cost(model, Tokens(write_5m=1_000))["cache_write"]
                c1 = pricing.cost(model, Tokens(write_1h=1_000))["cache_write"]
                ci = pricing.cost(model, Tokens(input=1_000))["input"]
                self.assertAlmostEqual(c5, ci * 1.25, places=15)
                self.assertAlmostEqual(c1, ci * 2.0, places=15)

    def test_one_million_opus(self):
        c = pricing.cost("claude-opus-5-5", Tokens(1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000))
        self.assertCost(c, {"input": 4.0, "output": 20.0, "cache_write": 5.0 + 8.0, "cache_read": 0.2})

    def test_haiku_long_prompt_over_threshold(self):
        c = pricing.cost("claude-haiku-5-5", Tokens(input=100_001, output=1_000_000))
        self.assertCost(c, {"input": 100_001 * 0.5 / 1e6, "output": 2.5, "cache_write": 0.0, "cache_read": 0.0})

    def test_haiku_long_prompt_counts_cache_read_and_writes(self):
        t = Tokens(input=1, output=0, write_5m=30_000, write_1h=20_000, cache_read=50_000)
        self.assertEqual(t.prompt, 100_001)
        c = pricing.cost("claude-haiku-5-5", t)
        self.assertCost(c, {
            "input": 0.5e-6,
            "cache_write": (30_000 * 0.625 + 20_000 * 1.0) / 1e6,
            "cache_read": 50_000 * 0.05 / 1e6,
        })

    def test_haiku_exactly_at_threshold_uses_base_rate(self):
        c = pricing.cost("claude-haiku-5-5", Tokens(input=100_000, output=1_000_000))
        self.assertCost(c, {"input": 0.01, "output": 0.5})

    def test_long_rate_is_haiku_only(self):
        c = pricing.cost("claude-sonnet-5-5", Tokens(input=1_000_000))
        self.assertAlmostEqual(c["input"], 2.0)

    def test_model_suffixes(self):
        self.assertEqual(pricing.base_model("claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertEqual(pricing.base_model("claude-sonnet-5-5-20261001"), "claude-sonnet-5-5")
        self.assertEqual(pricing.base_model("claude-haiku-5-5"), "claude-haiku-5-5")
        self.assertCost(pricing.cost("claude-opus-5-5[1m]", TEN_K), pricing.cost("claude-opus-5-5", TEN_K))

    def test_unknown_models(self):
        for m in ("", None, "gpt-5", "claude-opus-5-50", "claude-opus-5", "claude-opus-4-1"):
            with self.subTest(model=m):
                self.assertIsNone(pricing.base_model(m))
                self.assertIsNone(pricing.cost(m, TEN_K))


if __name__ == "__main__":
    unittest.main()
