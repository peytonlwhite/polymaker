import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import crypto_paper_bettor as crypto


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class CryptoGrokCostControlTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state_file = Path(self.tempdir.name) / "grok-state.json"
        self.state_patch = patch.object(crypto, "GROK_USAGE_STATE_FILE", self.state_file)
        self.log_patch = patch.object(crypto, "log_line")
        self.event_patch = patch.object(crypto, "event_line")
        self.state_patch.start()
        self.log_patch.start()
        self.event_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.log_patch.stop)
        self.addCleanup(self.event_patch.stop)

        self.settings = dict(crypto.DEFAULT_SETTINGS)
        self.settings.update({
            "CRYPTO_GROK_ENABLED": "true",
            "CRYPTO_GROK_API_KEY": "test-key",
            "CRYPTO_GROK_WEB_SEARCH_ENABLED": "false",
            "CRYPTO_GROK_X_SEARCH_ENABLED": "false",
            "CRYPTO_GROK_MIN_EDGE": "15",
            "CRYPTO_GROK_MIN_CONFIDENCE": "80",
            "CRYPTO_GROK_MAX_CALLS_PER_SCAN": "1",
            "CRYPTO_GROK_CACHE_MINUTES": "60",
            "CRYPTO_GROK_DAILY_BUDGET_USD": "1.00",
            "CRYPTO_GROK_CALL_BUDGET_RESERVE_USD": "0.10",
            "CRYPTO_GROK_MAX_OUTPUT_TOKENS": "150",
        })

    def candidate(self, ticker="TEST-1", market_gap=5):
        return {
            "ticker": ticker,
            "event_ticker": "EVENT-1",
            "side": "yes",
            "entry_price": 50,
            "edge": 18,
            "confidence": 85,
            "model_prob_yes": 68,
            "model": {
                "fakeout_risk": 0.1,
                "news": {"score": 0.0, "headlines": ["Routine market update"]},
            },
            "probability": {"market_gap": market_gap},
        }

    def chat_payload(self, cost_ticks=500_000_000):
        return {
            "choices": [{"message": {"content": '{"risk_level":"low","veto":false,"reasons":[]}'}}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "cost_in_usd_ticks": cost_ticks,
            },
        }

    def test_string_false_is_not_a_veto(self):
        review = crypto.parse_grok_review('{"risk_level":"low","veto":"false","reasons":[]}')
        self.assertFalse(review["veto"])

    @patch.object(crypto, "live_order_review")
    def test_shared_bankroll_preflight_runs_before_review_stage(self, live_review):
        live_review.return_value = {
            "ok": False,
            "error": "shared_exposure_cap_hit",
            "approved_stake": 0.0,
        }
        candidate = self.candidate()
        candidate.update({"asset": "BTC", "selective_edge": {"tier": "elite_edge", "base_stake": 5.0}})
        review = crypto.preflight_live_kalshi_order(self.settings, candidate, 7.5)

        self.assertFalse(review["ok"])
        self.assertEqual(review["error"], "shared_exposure_cap_hit")
        kwargs = live_review.call_args.kwargs
        self.assertEqual(kwargs["price_cents"], 50)
        self.assertEqual(kwargs["metadata"]["ticker"], "TEST-1")
        self.assertEqual(kwargs["metadata"]["base_stake"], 5.0)

    @patch.object(crypto.requests, "post")
    def test_below_quality_gate_does_not_call_api(self, post):
        candidate = self.candidate()
        candidate["confidence"] = 79
        review = crypto.maybe_grok_review(self.settings, candidate, {"api_calls": 0})
        self.assertEqual(review["reason"], "confidence_below_grok_min")
        post.assert_not_called()

    @patch.object(crypto.requests, "post")
    def test_exact_cost_is_recorded_and_cache_avoids_second_call(self, post):
        post.return_value = FakeResponse(self.chat_payload())
        first = crypto.maybe_grok_review(self.settings, self.candidate(), {"api_calls": 0})
        second = crypto.maybe_grok_review(self.settings, self.candidate(), {"api_calls": 0})

        self.assertEqual(first["source"], "api")
        self.assertEqual(first["cost_usd"], 0.05)
        self.assertEqual(second["source"], "cache")
        self.assertEqual(second["cost_usd"], 0.0)
        self.assertEqual(post.call_count, 1)
        state = crypto.read_json(self.state_file, {})
        self.assertEqual(state["daily_calls"], 1)
        self.assertEqual(state["daily_cost_usd"], 0.05)

    @patch.object(crypto.requests, "post")
    def test_missing_cost_reserves_conservative_call_amount(self, post):
        payload = self.chat_payload()
        payload.pop("usage")
        post.return_value = FakeResponse(payload)
        review = crypto.maybe_grok_review(self.settings, self.candidate(), {"api_calls": 0})

        self.assertEqual(review["source"], "api")
        state = crypto.read_json(self.state_file, {})
        self.assertEqual(state["daily_calls"], 1)
        self.assertEqual(state["daily_unpriced_reserve_usd"], 0.1)
        self.assertEqual(review["daily_accounted_cost_usd"], 0.1)

    @patch.object(crypto.requests, "post")
    def test_only_one_new_api_review_is_allowed_per_scan(self, post):
        post.return_value = FakeResponse(self.chat_payload())
        scan = {"api_calls": 0}
        first = crypto.maybe_grok_review(self.settings, self.candidate("TEST-1"), scan)
        second = crypto.maybe_grok_review(self.settings, self.candidate("TEST-2"), scan)

        self.assertEqual(first["source"], "api")
        self.assertEqual(second["reason"], "scan_call_limit")
        self.assertEqual(post.call_count, 1)

    @patch.object(crypto.requests, "post")
    def test_daily_budget_stops_before_reserved_call(self, post):
        crypto.write_json(self.state_file, {
            "date": crypto.grok_usage_day(),
            "daily_calls": 9,
            "daily_cost_usd": 0.95,
            "daily_unpriced_reserve_usd": 0.0,
            "cache": {},
        })
        review = crypto.maybe_grok_review(self.settings, self.candidate(), {"api_calls": 0})
        self.assertEqual(review["reason"], "daily_budget_exhausted")
        post.assert_not_called()

    @patch.object(crypto.requests, "post")
    def test_conditional_search_uses_web_without_x_and_caps_output(self, post):
        self.settings["CRYPTO_GROK_WEB_SEARCH_ENABLED"] = "true"
        post.return_value = FakeResponse({
            "output_text": '{"risk_level":"medium","veto":false,"reasons":[]}',
            "usage": {
                "input_tokens": 100,
                "output_tokens": 25,
                "cost_in_usd_ticks": 600_000_000,
                "server_side_tool_usage": {"web_search": 1},
            },
        })
        review = crypto.maybe_grok_review(
            self.settings,
            self.candidate(market_gap=20),
            {"api_calls": 0},
        )

        body = post.call_args.kwargs["json"]
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(body["max_output_tokens"], 150)
        self.assertTrue(review["web_search"])
        self.assertFalse(review["x_search"])


if __name__ == "__main__":
    unittest.main()
