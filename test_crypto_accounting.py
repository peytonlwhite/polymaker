import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import crypto_paper_bettor as crypto


class CryptoAccountingRegressionTests(unittest.TestCase):
    def directional_breaker_settings(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_DIRECTIONAL_RECOVERY_BREAKER_ENABLED": "true",
            "CRYPTO_15M_DIRECTIONAL_RECOVERY_BREAKER_LOSS_COUNT": "3",
            "CRYPTO_15M_DIRECTIONAL_RECOVERY_BREAKER_LOOKBACK_MINUTES": "90",
            "CRYPTO_15M_DIRECTIONAL_RECOVERY_BREAKER_MIN_BOTS": "2",
        })
        return settings

    def directional_loss(self, settled_at, bot_number, side="no"):
        return {
            "status": "settled",
            "result": "LOSS",
            "side": side,
            "strategy_owner": crypto.CRYPTO_15M_CAMPAIGN_OWNER,
            "settled_at": settled_at.isoformat(),
            "bot_number": bot_number,
            "asset": "BTC",
            "crypto_live_campaign": {"role": "primary", "bot_number": bot_number},
        }

    def test_directional_recovery_breaker_detects_cross_bot_loss_cluster(self):
        now = datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc)
        portfolio = {"bets": [
            self.directional_loss(now - timedelta(minutes=70), 1),
            self.directional_loss(now - timedelta(minutes=40), 2),
            self.directional_loss(now - timedelta(minutes=10), 1),
        ]}

        review = crypto.crypto_directional_recovery_breaker(
            self.directional_breaker_settings(),
            portfolio,
            "no",
            now=now,
        )

        self.assertTrue(review["active"])
        self.assertEqual(review["loss_count"], 3)
        self.assertEqual(review["distinct_bot_count"], 2)

    def test_directional_recovery_breaker_expires_and_is_side_specific(self):
        now = datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc)
        portfolio = {"bets": [
            self.directional_loss(now - timedelta(minutes=100), 1),
            self.directional_loss(now - timedelta(minutes=40), 2),
            self.directional_loss(now - timedelta(minutes=10), 3),
        ]}

        no_review = crypto.crypto_directional_recovery_breaker(
            self.directional_breaker_settings(), portfolio, "no", now=now
        )
        yes_review = crypto.crypto_directional_recovery_breaker(
            self.directional_breaker_settings(), portfolio, "yes", now=now
        )

        self.assertFalse(no_review["active"])
        self.assertEqual(no_review["loss_count"], 2)
        self.assertFalse(yes_review["active"])
        self.assertEqual(yes_review["loss_count"], 0)

    def test_directional_breaker_is_diagnostic_while_cooldown_controls_sizing(self):
        settings = self.directional_breaker_settings()
        candidate = {
            "ticker": "KXBTC15M-TEST",
            "asset": "BTC",
            "side": "no",
            "entry_price": 50,
            "edge": 10,
            "confidence": 75,
        }
        active_breaker = {"enabled": True, "active": True, "reason": "test"}

        with patch.object(
            crypto,
            "crypto_directional_recovery_breaker",
            return_value=active_breaker,
        ), patch.object(
            crypto,
            "review_crypto_15m_campaign_candidate",
            return_value={
                "eligible": True,
                "role": "primary",
                "consecutive_primary_losses": 1,
                "bot_number": 1,
            },
        ):
            recovery = crypto.crypto_15m_campaign_lane_review(settings, {"bets": []}, candidate, 1)

        self.assertTrue(recovery["eligible"])
        self.assertTrue(recovery["directional_recovery_breaker"]["active"])

        with patch.object(
            crypto,
            "crypto_directional_recovery_breaker",
            return_value=active_breaker,
        ), patch.object(
            crypto,
            "review_crypto_15m_campaign_candidate",
            return_value={
                "eligible": True,
                "role": "primary",
                "consecutive_primary_losses": 0,
                "bot_number": 1,
            },
        ):
            base = crypto.crypto_15m_campaign_lane_review(settings, {"bets": []}, candidate, 1)

        self.assertTrue(base["eligible"])
    def test_market_price_distinguishes_legacy_cents_from_dollars(self):
        self.assertEqual(crypto.market_price({"yes_ask": 1}, "yes_ask"), 1.0)
        self.assertEqual(
            crypto.market_price({"yes_ask_dollars": "0.0100"}, "yes_ask_dollars"),
            1.0,
        )
        self.assertEqual(
            crypto.market_price(
                {"yes_ask": 0, "yes_ask_dollars": "0.5100"},
                "yes_ask",
                "yes_ask_dollars",
            ),
            51.0,
        )

    def test_no_ask_order_uses_complement_of_average_yes_fill(self):
        order = {
            "side": "ask",
            "fill_count_fp": "10.00",
            "average_fill_price": "0.4300",
            "average_fee_paid": "0.0200",
        }
        self.assertEqual(crypto.order_fill_count(order), 10.0)
        self.assertEqual(crypto.order_entry_price_cents(order), 57.0)
        self.assertEqual(crypto.order_fill_cost(order), 5.7)
        self.assertEqual(crypto.order_fill_fee(order), 0.2)

    def test_live_submission_accepts_fixed_point_fill_and_records_execution(self):
        settings = dict(crypto.DEFAULT_SETTINGS)
        settings.update({
            "CRYPTO_15M_CAMPAIGN_ENABLED": "false",
            "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL": "false",
            "CRYPTO_LIVE_MAX_STAKE": "10",
            "CRYPTO_LIVE_DRY_RUN": "false",
        })
        candidate = {
            "ticker": "KXXRP15M-TEST",
            "asset": "XRP",
            "market_kind": "above",
            "side": "no",
            "entry_price": 58,
            "model_prob_yes": 0.35,
            "edge": 7.0,
            "confidence": 75,
            "crypto_live_campaign": {"enabled": False},
        }
        response = {
            "order": {
                "order_id": "order-1",
                "side": "ask",
                "fill_count_fp": "10.00",
                "average_fill_price": "0.4300",
                "average_fee_paid": "0.0200",
            }
        }

        with patch.object(crypto, "crypto_live_readiness", return_value={"ok": True}), patch.object(
            crypto,
            "kalshi_private_request",
            return_value=(response, {}),
        ):
            live_order = crypto.place_live_kalshi_order(settings, {"bets": []}, candidate, 5.8)

        self.assertTrue(live_order["ok"], live_order)
        self.assertEqual(live_order["contracts"], 10.0)
        self.assertEqual(live_order["price"], 58)
        self.assertEqual(live_order["executed_price"], 57.0)
        self.assertEqual(live_order["actual_stake"], 5.7)

        portfolio = {"bets": []}
        bet = crypto.record_live_bet(portfolio, candidate, live_order, persist=False)
        self.assertEqual(bet["quoted_entry_price"], 58)
        self.assertEqual(bet["entry_price"], 57.0)
        self.assertEqual(bet["fee"], 0.2)

    def test_live_order_sync_corrects_executed_price_cost_and_fee(self):
        portfolio = {
            "bets": [{
                "mode": "live",
                "status": "open",
                "side": "no",
                "ticker": "KXXRP15M-TEST",
                "entry_price": 58,
                "stake": 5.8,
                "contracts": 10,
                "kalshi_order_id": "order-1",
                "live_order": {"price": 58, "response": {"order_id": "order-1"}},
            }]
        }
        orders = [{
            "order_id": "order-1",
            "side": "ask",
            "fill_count_fp": "10.00",
            "average_fill_price": "0.4300",
            "average_fee_paid": "0.0200",
        }]

        changed = crypto.sync_live_order_costs(portfolio, {}, orders=orders)

        self.assertTrue(changed)
        bet = portfolio["bets"][0]
        self.assertEqual(bet["stake"], 5.7)
        self.assertEqual(bet["quoted_entry_price"], 58)
        self.assertEqual(bet["entry_price"], 57.0)
        self.assertEqual(bet["fee"], 0.2)
        self.assertEqual(bet["live_order"]["executed_price"], 57.0)

    def test_live_candidate_is_counted_as_placed(self):
        groups = crypto.candidate_group_rows(
            [{"asset": "BTC", "decision": "placed_live", "edge": 5, "confidence": 70}],
            lambda row: row["asset"],
        )
        self.assertEqual(groups[0]["placed"], 1)
        self.assertEqual(groups[0]["eligible"], 0)

    def test_historical_analytics_use_saved_exchange_fill_price(self):
        row = {
            "side": "no",
            "entry_price": 47,
            "live_order": {
                "response": {
                    "side": "ask",
                    "average_fill_price": "0.5500",
                }
            },
        }
        self.assertEqual(crypto.analytics_entry_price(row), 45.0)

    def test_historical_close_band_uses_entry_time_not_current_time(self):
        row = {
            "placed_at": "2026-07-18T15:00:00Z",
            "close_time": "2026-07-18T15:15:00Z",
        }
        self.assertEqual(crypto.hours_to_close_band(row), "<1h")

    def test_campaign_recovery_analytics_are_not_labeled_normal(self):
        self.assertEqual(
            crypto.recovery_tier({"crypto_live_campaign": {"consecutive_primary_losses": 1}}),
            "campaign_recovery_after_1_loss",
        )
        self.assertEqual(
            crypto.recovery_tier({"crypto_live_campaign": {"consecutive_primary_losses": 0}}),
            "campaign_base",
        )


if __name__ == "__main__":
    unittest.main()
