import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import crypto_paper_bettor as crypto
import multi_market


class MultiMarketClassificationTests(unittest.TestCase):
    def test_supported_series_are_assigned_to_the_expected_lanes(self):
        cases = {
            "KXBTC15M": "crypto_15m",
            "KXBTCD": "crypto_hourly",
            "KXBTC": "crypto_hourly",
            "KXGOLD15M": "commodity_15m",
            "KXSILVERH": "commodity_hourly",
            "KXWTIH": "commodity_hourly",
        }
        for series, expected in cases.items():
            with self.subTest(series=series):
                self.assertEqual(
                    multi_market.market_lane({"series_ticker": series}),
                    expected,
                )

    def test_assets_and_exposure_groups_are_settlement_aware(self):
        self.assertEqual(
            multi_market.infer_market_asset({"series_ticker": "KXGOLDH"}),
            "GOLD",
        )
        self.assertEqual(multi_market.exposure_group("GOLD"), "metals")
        self.assertEqual(multi_market.exposure_group("SILVER"), "metals")
        self.assertEqual(multi_market.exposure_group("WTI"), "energy")
        self.assertEqual(multi_market.exposure_group("BTC"), "crypto")

    def test_custom_strike_markets_are_normalized(self):
        market = {
            "series_ticker": "KXDOGED",
            "custom_strike": {
                "floor_strike": "0.2549999",
                "cap_strike": "",
                "strike_type": "greater",
            },
        }
        self.assertEqual(crypto.market_kind(market), "above")
        self.assertEqual(crypto.market_strikes(market), (0.2549999, None))


class MultiMarketModelTests(unittest.TestCase):
    def settings(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_EXECUTION_MODE": "live",
            "MULTI_MARKET_ENABLED": "true",
            "MULTI_MARKET_LIVE_ENABLED": "true",
            "MULTI_MARKET_LIVE_MIN_EDGE": "10",
            "MULTI_MARKET_LIVE_MIN_CONFIDENCE": "75",
            "MULTI_MARKET_LIVE_MIN_PRICE_CENTS": "45",
            "MULTI_MARKET_LIVE_MAX_PRICE_CENTS": "70",
            "CRYPTO_MAX_OPEN_BETS": "3",
            "CRYPTO_MAX_OPEN_PER_ASSET": "1",
            "CRYPTO_MAX_OPEN_PER_EVENT": "1",
        })
        return settings

    def candidate(self, **overrides):
        row = {
            "asset": "GOLD",
            "ticker": "KXGOLDH-TEST-2500",
            "event_ticker": "KXGOLDH-TEST",
            "series_ticker": "KXGOLDH",
            "market_lane": "commodity_hourly",
            "market_kind": "above",
            "side": "yes",
            "entry_price": 55.0,
            "model_prob_yes": 72.0,
            "edge": 16.0,
            "confidence": 80.0,
            "minutes_to_close": 30.0,
            "close_time": (crypto.utc_now() + timedelta(minutes=30)).isoformat(),
            "position_cluster": "GOLD|test-close",
            "exposure_group": "metals",
            "model": {
                "data_source": "pyth",
                "settlement_source": "Pyth",
                "history_points": 720,
            },
            "skip_reasons": [],
        }
        row.update(overrides)
        return row

    def test_price_momentum_is_one_bounded_provider_vote(self):
        rising = [100.0 + index * 0.1 for index in range(90)]
        falling = list(reversed(rising))
        self.assertGreater(multi_market.price_momentum_score(rising), 0.05)
        self.assertLess(multi_market.price_momentum_score(falling), -0.05)
        self.assertLessEqual(abs(multi_market.price_momentum_score(rising)), 1.0)

    @patch("multi_market.fetch_twelve_gold_confirmation")
    @patch("multi_market.fetch_pyth_history")
    @patch("multi_market.fetch_pyth_latest")
    @patch("multi_market.resolve_pyth_proxy_feed")
    def test_proxy_history_adds_exactly_one_pyth_direction_vote(
        self,
        resolve_feed,
        latest,
        history,
        twelve,
    ):
        resolve_feed.return_value = {
            "symbol": "Metal.XAU/USD",
            "lazer_id": 1,
            "hermes_id": "feed",
        }
        latest.return_value = {"spot": 109.0, "age_seconds": 1.0}
        history.return_value = {
            "times": list(range(90)),
            "closes": [100.0 + index * 0.1 for index in range(90)],
        }
        twelve.return_value = None

        row = multi_market.fetch_commodity_proxy_snapshot(
            "GOLD",
            api_key="test",
            twelve_api_key="",
        )

        scores = row["microstructure"]["source_flow_scores"]
        self.assertEqual(set(scores), {"pyth_price_momentum"})
        self.assertGreater(scores["pyth_price_momentum"], 0.05)
        self.assertEqual(row["microstructure"]["settlement_proxy_source_count"], 1)

    def test_intraday_probability_is_monotonic_in_strike(self):
        model = {
            "spot": 2500.0,
            "minute_vol": 0.0005,
            "drift_per_minute": 0.0,
            "trend_drift_per_minute": 0.0,
            "reversion_drift_per_minute": 0.0,
            "fakeout_risk": 0.0,
            "settlement_source": "Pyth",
        }
        close = (crypto.utc_now() + timedelta(minutes=30)).isoformat()
        lower = crypto.probability_for_intraday_threshold_market(
            {"floor_strike": 2490.0, "close_time": close}, model, self.settings()
        )
        higher = crypto.probability_for_intraday_threshold_market(
            {"floor_strike": 2510.0, "close_time": close}, model, self.settings()
        )
        self.assertGreater(lower["prob"], higher["prob"])
        self.assertEqual(lower["settlement_price_source"], "Pyth")

    def test_far_future_hourly_contracts_are_not_modeled_as_intraday(self):
        settings = self.settings()
        market = {
            "series_ticker": "KXBTCD",
            "ticker": "KXBTCD-FUTURE-T2500",
            "event_ticker": "KXBTCD-FUTURE",
            "floor_strike": 2500.0,
            "yes_ask": 50,
            "no_ask": 50,
            "close_time": (crypto.utc_now() + timedelta(hours=4)).isoformat(),
        }
        asset = {
            "spot": 2500.0,
            "source": "coinbase",
            "closes": [2490.0 + index * 0.1 for index in range(120)],
            "microstructure": {},
        }
        rows = crypto.build_candidates(
            settings,
            [market],
            {"BTC": asset},
            {"value": 50},
        )
        self.assertEqual(rows, [])

    def test_commodity_candidate_passes_then_respects_correlation_caps(self):
        settings = self.settings()
        candidate = self.candidate()
        self.assertTrue(
            crypto.multi_market_candidate_passes(settings, {"bets": []}, candidate)
        )
        open_row = {
            **candidate,
            "id": "existing",
            "status": "open",
            "strategy_owner": "commodity_hourly_pilot",
            "multi_market": {"lane": "commodity_hourly"},
        }
        blocked = self.candidate(ticker="KXGOLDH-TEST-2510")
        self.assertFalse(
            crypto.multi_market_candidate_passes(
                settings,
                {"bets": [open_row]},
                blocked,
            )
        )
        self.assertIn("asset_cap", blocked["skip_reasons"])
        self.assertIn("multi_market_lane_cap", blocked["skip_reasons"])
        self.assertIn("multi_market_cluster_cap", blocked["skip_reasons"])

    def test_live_edge_floor_accepts_three_percent_but_rejects_below_it(self):
        settings = self.settings()
        settings["MULTI_MARKET_LIVE_MIN_EDGE"] = "3"
        at_floor = self.candidate(edge=3.0)
        below_floor = self.candidate(edge=2.99)

        self.assertTrue(
            crypto.multi_market_candidate_passes(settings, {"bets": []}, at_floor)
        )
        self.assertFalse(
            crypto.multi_market_candidate_passes(settings, {"bets": []}, below_floor)
        )
        self.assertIn("multi_market_edge_too_low", below_floor["skip_reasons"])

    def test_live_confidence_floor_accepts_seventy_but_rejects_below_it(self):
        settings = self.settings()
        settings["MULTI_MARKET_LIVE_MIN_EDGE"] = "3"
        settings["MULTI_MARKET_LIVE_MIN_CONFIDENCE"] = "70"
        at_floor = self.candidate(edge=3.0, confidence=70.0)
        below_floor = self.candidate(edge=3.0, confidence=69.9)

        self.assertTrue(
            crypto.multi_market_candidate_passes(settings, {"bets": []}, at_floor)
        )
        self.assertFalse(
            crypto.multi_market_candidate_passes(settings, {"bets": []}, below_floor)
        )
        self.assertIn("multi_market_confidence_too_low", below_floor["skip_reasons"])

    def test_tiered_price_quality_expands_range_without_flattening_risk(self):
        settings = self.settings()
        settings.update({
            "MULTI_MARKET_LIVE_MIN_EDGE": "3",
            "MULTI_MARKET_LIVE_MIN_CONFIDENCE": "70",
            "MULTI_MARKET_LIVE_MIN_PRICE_CENTS": "35",
            "MULTI_MARKET_LIVE_MAX_PRICE_CENTS": "75",
            "MULTI_MARKET_TIERED_PRICE_QUALITY_ENABLED": "true",
            "MULTI_MARKET_LOW_PRICE_MAX_CENTS": "44",
            "MULTI_MARKET_LOW_PRICE_MIN_EDGE": "4",
            "MULTI_MARKET_LOW_PRICE_MIN_CONFIDENCE": "70",
            "MULTI_MARKET_HIGH_PRICE_MIN_CENTS": "71",
            "MULTI_MARKET_HIGH_PRICE_MIN_EDGE": "5",
            "MULTI_MARKET_HIGH_PRICE_MIN_CONFIDENCE": "74",
        })

        low_good = self.candidate(entry_price=40, edge=4, confidence=70)
        low_weak = self.candidate(entry_price=40, edge=3.99, confidence=80)
        standard = self.candidate(entry_price=55, edge=3, confidence=70)
        high_good = self.candidate(entry_price=72, edge=5, confidence=74)
        high_weak = self.candidate(entry_price=72, edge=4.99, confidence=80)

        self.assertTrue(crypto.multi_market_candidate_passes(settings, {"bets": []}, low_good))
        self.assertFalse(crypto.multi_market_candidate_passes(settings, {"bets": []}, low_weak))
        self.assertIn("multi_market_low_price_edge_too_low", low_weak["skip_reasons"])
        self.assertTrue(crypto.multi_market_candidate_passes(settings, {"bets": []}, standard))
        self.assertTrue(crypto.multi_market_candidate_passes(settings, {"bets": []}, high_good))
        self.assertFalse(crypto.multi_market_candidate_passes(settings, {"bets": []}, high_weak))
        self.assertIn("multi_market_high_price_edge_too_low", high_weak["skip_reasons"])

    def test_expanded_lane_stake_is_capped_and_never_uses_recovery(self):
        settings = self.settings()
        settings["MULTI_MARKET_LIVE_MAX_STAKE"] = "3"
        stake = crypto.candidate_stake(
            settings,
            self.candidate(model_prob_yes=95.0, entry_price=45.0),
            balance=1000.0,
            portfolio={"bets": []},
        )
        self.assertGreaterEqual(stake, 1.0)
        self.assertLessEqual(stake, 3.0)

    @patch("crypto_paper_bettor.http_json")
    def test_live_quote_refresh_revalidates_expanded_lane(self, http_json):
        settings = self.settings()
        settings.update({
            "CRYPTO_LIVE_QUOTE_REFRESH_ENABLED": "true",
            "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "3",
        })
        candidate = self.candidate(
            entry_price=50.0,
            model_prob_yes=75.0,
            edge=24.0,
        )
        http_json.return_value = {
            "orderbook_fp": {
                "no_dollars": [["0.4800", "100.00"]],
            }
        }
        review = crypto.refresh_live_campaign_candidate_quote(
            settings,
            {"bets": []},
            candidate,
        )
        self.assertTrue(review["ok"])
        self.assertEqual(review["refreshed_price_cents"], 52.0)
        self.assertTrue(candidate["multi_market"]["eligible"])
        self.assertEqual(candidate["strategy_owner"], "commodity_hourly_pilot")

    @patch("crypto_paper_bettor.fetch_commodity_snapshot")
    def test_entitlement_failure_uses_retry_cooldown(self, fetch_snapshot):
        settings = self.settings()
        previous = dict(crypto.COMMODITY_DATA_HEALTH)
        try:
            crypto.COMMODITY_DATA_HEALTH["GOLD"] = {
                "ok": False,
                "reason": "pyth_not_entitled",
                "checked_at": crypto.iso_now(),
            }
            rows = crypto.fetch_asset_data(settings, ["GOLD"])
            self.assertEqual(rows, {})
            fetch_snapshot.assert_not_called()
        finally:
            crypto.COMMODITY_DATA_HEALTH.clear()
            crypto.COMMODITY_DATA_HEALTH.update(previous)

    @patch("multi_market.requests.post")
    def test_pyth_latest_price_applies_exponent_and_age(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "parsed": {
                "priceFeeds": [{
                    "price": "250012345",
                    "confidence": "125",
                    "exponent": -5,
                    "feedUpdateTimestamp": int(multi_market.time.time() * 1_000_000),
                    "marketSession": "regular",
                }]
            }
        }
        post.return_value = response
        row = multi_market.fetch_pyth_latest(
            {"lazer_id": 3153},
            api_key="test-key",
            timeout=1,
        )
        self.assertAlmostEqual(row["spot"], 2500.12345, places=5)
        self.assertAlmostEqual(row["pyth_confidence_interval"], 0.00125, places=5)
        self.assertLess(row["age_seconds"], 2)

    @patch("crypto_paper_bettor.fetch_pyth_price_at")
    def test_core_proxy_is_return_anchored_to_exact_kalshi_open(self, price_at):
        price_at.return_value = {"price": 100.0, "publish_time": 1}
        settings = self.settings()
        settings["MULTI_MARKET_PYTH_TIMEOUT_SECONDS"] = "1"
        row = {
            "source": "pyth_core_proxy",
            "settlement_source": "Pyth",
            "spot": 101.0,
            "closes": [99.0, 100.0, 101.0],
            "requires_kalshi_anchor": True,
            "pyth_proxy": {"hermes_id": "test", "symbol": "Metal.XAU/USD"},
            "microstructure": {},
        }
        market = {
            "ticker": "KXGOLD15M-TEST",
            "open_time": (crypto.utc_now() - timedelta(minutes=5)).isoformat(),
            "floor_strike": 200.0,
        }
        anchored = crypto.align_commodity_proxy_to_kalshi_anchor(settings, row, market)
        self.assertAlmostEqual(anchored["spot"], 202.0)
        self.assertEqual(anchored["source"], "kalshi_anchored_pyth_core")
        self.assertFalse(anchored["requires_kalshi_anchor"])
        self.assertEqual(anchored["kalshi_anchor"]["settlement_open"], 200.0)

    @patch("crypto_paper_bettor.fetch_pyth_price_at")
    def test_stale_kalshi_anchor_fails_closed(self, price_at):
        settings = self.settings()
        settings["MULTI_MARKET_PROXY_MAX_ANCHOR_AGE_MINUTES"] = "20"
        row = {
            "spot": 101.0,
            "closes": [100.0] * 60,
            "requires_kalshi_anchor": True,
            "pyth_proxy": {"hermes_id": "test"},
        }
        market = {
            "open_time": (crypto.utc_now() - timedelta(minutes=21)).isoformat(),
            "floor_strike": 200.0,
        }
        with self.assertRaisesRegex(ValueError, "anchor_stale"):
            crypto.align_commodity_proxy_to_kalshi_anchor(settings, row, market)
        price_at.assert_not_called()

    @patch("crypto_paper_bettor.http_json")
    def test_hourly_discovery_is_staggered_but_15m_refreshes_every_scan(self, http_json):
        http_json.return_value = {"markets": []}
        settings = self.settings()
        settings.update({
            "CRYPTO_SERIES": "KXBTC15M,KXBTCD,KXETHD",
            "MULTI_MARKET_HOURLY_DISCOVERY_STAGGER_ENABLED": "true",
            "MULTI_MARKET_HOURLY_DISCOVERY_CACHE_SECONDS": "90",
            "CRYPTO_FETCH_MAX_WORKERS": "1",
        })
        old_cache = dict(crypto.KALSHI_SERIES_MARKET_CACHE)
        old_sequence = crypto.KALSHI_MARKET_SCAN_SEQUENCE
        try:
            crypto.KALSHI_SERIES_MARKET_CACHE.clear()
            crypto.KALSHI_MARKET_SCAN_SEQUENCE = 0
            crypto.fetch_kalshi_markets(settings)
            self.assertEqual(http_json.call_count, 3)
            crypto.fetch_kalshi_markets(settings)
            # The 15-minute series and one of two hourly groups refresh.
            self.assertEqual(http_json.call_count, 5)
            self.assertEqual(
                crypto.LAST_KALSHI_MARKET_FETCH_HEALTH["cached_series"],
                1,
            )
        finally:
            crypto.KALSHI_SERIES_MARKET_CACHE.clear()
            crypto.KALSHI_SERIES_MARKET_CACHE.update(old_cache)
            crypto.KALSHI_MARKET_SCAN_SEQUENCE = old_sequence


if __name__ == "__main__":
    unittest.main()
