import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import crypto_paper_bettor as crypto
from crypto_live_campaign import review_candidate


def settings():
    values = dict(crypto.DEFAULT_SETTINGS)
    values["CRYPTO_SERIES"] = "KXBTC15M,KXXRP15M"
    values["CRYPTO_ANALYTICS_MIN_ENTRY_PRICE_CENTS"] = "2"
    return values


def manual_order(
    order_id="user-order-1",
    ticker="KXBTC15M-26JUL191145-45",
    side="yes",
    count="9.47",
    price="0.5100",
    cost="4.829700",
    fee="0.165800",
):
    opposite = f"{1.0 - float(price):.4f}"
    return {
        "action": "buy",
        "client_order_id": "",
        "created_time": "2026-07-19T15:32:27.161754Z",
        "fill_count_fp": count,
        "maker_fees_dollars": "0.000000",
        "maker_fill_cost_dollars": "0.000000",
        "no_price_dollars": price if side == "no" else opposite,
        "order_id": order_id,
        "outcome_side": side,
        "side": side,
        "status": "executed",
        "taker_fees_dollars": fee,
        "taker_fill_cost_dollars": cost,
        "ticker": ticker,
        "yes_price_dollars": price if side == "yes" else opposite,
    }


class CryptoUserBetImportTests(unittest.TestCase):
    def test_live_bot_ledger_rebuild_prefers_settled_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = root / "events.jsonl"
            ledger = root / "ledger.json"
            open_row = {
                "id": "bot-1",
                "status": "open",
                "mode": "live",
                "ticker": "KXBTC15M-TEST",
                "strategy_owner": "crypto_15m_campaign",
            }
            settled_row = {
                **open_row,
                "status": "settled",
                "result": "WIN",
                "profit": 2.0,
            }
            events.write_text(
                "\n".join([
                    json.dumps({"type": "live_bet", "bet": open_row}),
                    json.dumps({"type": "live_settled_bet", "bet": settled_row}),
                ]) + "\n",
                encoding="utf-8",
            )
            with patch.object(crypto, "LIVE_BOT_LEDGER_FILE", ledger):
                result = crypto.rebuild_live_bot_ledger_from_events(events)
                portfolio = {"bets": []}
                recovered = crypto.recover_live_bot_rows_from_ledger(portfolio)

            self.assertEqual(result["recovered_rows"], 1)
            self.assertEqual(recovered, 1)
            self.assertEqual(portfolio["bets"][0]["status"], "settled")
            self.assertEqual(portfolio["bets"][0]["profit"], 2.0)

    def test_live_portfolio_save_blocks_row_loss_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            portfolio_path = root / "live.json"
            backup_path = root / "live.backup.json"
            original = {"bets": [{"id": "one"}, {"id": "two"}]}
            portfolio_path.write_text(json.dumps(original), encoding="utf-8")
            with patch.object(crypto, "LIVE_PORTFOLIO_FILE", portfolio_path), patch.object(
                crypto, "LIVE_PORTFOLIO_BACKUP_FILE", backup_path
            ), patch.object(crypto, "PORTFOLIO_FILE", portfolio_path):
                with self.assertRaises(crypto.LivePortfolioError):
                    crypto.save_portfolio({"bets": [{"id": "one"}]})

            self.assertEqual(
                json.loads(portfolio_path.read_text(encoding="utf-8")),
                original,
            )

    def test_stale_local_position_recovers_from_finalized_market(self):
        bet = {
            "id": "stale-bot",
            "status": "open",
            "mode": "live",
            "ticker": "KXBTC15M-OLD",
            "kalshi_ticker": "KXBTC15M-OLD",
            "side": "yes",
            "order_side": "yes",
            "stake": 3.1,
            "contracts": 12.4,
            "strategy_owner": "crypto_15m_campaign",
            "live_order": {"response": {"average_fee_paid": "0.0131"}},
        }
        portfolio = {"bets": [bet]}
        with patch.object(crypto, "update_live_bot_ledger", return_value=1), patch.object(
            crypto, "event_line"
        ), patch.object(crypto, "log_line"):
            recovered = crypto.settle_stale_live_bets_from_markets(
                portfolio,
                settings(),
                {"KXBTC15M-OLD"},
                markets={
                    "KXBTC15M-OLD": {
                        "status": "finalized",
                        "result": "yes",
                        "settlement_ts": "2026-07-18T21:02:12Z",
                    }
                },
            )

        self.assertEqual(recovered, 1)
        self.assertEqual(bet["status"], "settled")
        self.assertEqual(bet["result"], "WIN")
        self.assertEqual(bet["fee"], 0.1624)
        self.assertEqual(bet["profit"], 9.14)

    def test_delayed_remote_settlement_alert_does_not_assume_outcome(self):
        bet = {
            "id": "delayed-bot",
            "status": "open",
            "mode": "live",
            "ticker": "KXBTC15M-DELAYED",
            "side": "yes",
            "asset": "BTC",
            "close_time": "2026-07-18T10:00:00Z",
            "strategy_owner": "crypto_15m_campaign",
            "bot_number": 2,
            "crypto_live_campaign": {"bot_number": 2},
        }
        portfolio = {"bets": [bet]}
        now = crypto.parse_time("2026-07-18T11:00:00Z")
        values = settings()
        values["CRYPTO_LIVE_DELAYED_SETTLEMENT_ALERT_MINUTES"] = "30"
        values["CRYPTO_LIVE_DELAYED_SETTLEMENT_ALERT_REPEAT_MINUTES"] = "60"

        with patch.object(crypto, "event_line") as event_mock, patch.object(
            crypto,
            "log_line",
        ), patch.object(
            crypto,
            "iso_now",
            return_value="2026-07-18T11:00:00Z",
        ):
            alerts = crypto.delayed_live_settlement_alerts(
                portfolio,
                values,
                {"KXBTC15M-DELAYED"},
                now=now,
            )

        self.assertEqual(len(alerts), 1)
        self.assertFalse(alerts[0]["outcome_assumed"])
        self.assertEqual(bet["status"], "open")
        event_mock.assert_called_once()

    def test_only_unmatched_non_bot_fills_are_imported(self):
        user = manual_order()
        bot = {
            **manual_order(order_id="bot-order"),
            "client_order_id": "crypto-generated-id",
        }
        canceled = {
            **manual_order(order_id="unfilled-order"),
            "fill_count_fp": "0.00",
            "status": "canceled",
        }
        rows = crypto.user_crypto_order_candidates(
            {"bets": []},
            settings(),
            [user, bot, canceled],
        )
        self.assertEqual([row["order_id"] for row in rows], ["user-order-1"])

    def test_import_is_idempotent_and_marks_user_ownership(self):
        portfolio = {"bets": []}
        order = manual_order()
        imported = crypto.import_user_crypto_orders(
            portfolio,
            settings(),
            orders=[order],
        )
        repeated = crypto.import_user_crypto_orders(
            portfolio,
            settings(),
            orders=[order],
        )
        self.assertEqual(len(imported), 1)
        self.assertEqual(repeated, [])
        bet = portfolio["bets"][0]
        self.assertEqual(bet["strategy_owner"], "user_bet")
        self.assertTrue(bet["user_bet"])
        self.assertTrue(bet["excluded_from_bot_campaign"])
        self.assertEqual(bet["asset"], "BTC")
        self.assertEqual(bet["side"], "yes")
        self.assertEqual(bet["contracts"], 9.47)
        self.assertEqual(bet["stake"], 4.8297)
        self.assertEqual(bet["fee"], 0.1658)

    def test_settlement_uses_each_orders_own_cost_contracts_and_fee(self):
        portfolio = {"bets": []}
        crypto.import_user_crypto_orders(
            portfolio,
            settings(),
            orders=[manual_order()],
        )
        portfolio["bets"].append({
            "id": "bot-bet",
            "status": "open",
            "mode": "live",
            "ticker": "KXBTC15M-26JUL191145-45",
            "kalshi_ticker": "KXBTC15M-26JUL191145-45",
            "side": "yes",
            "order_side": "yes",
            "contracts": 5.0,
            "stake": 2.55,
            "fee": 0.09,
            "entry_price": 51,
            "strategy_owner": "crypto_15m_campaign",
        })
        settlement = {
            "ticker": "KXBTC15M-26JUL191145-45",
            "market_result": "yes",
            "yes_count_fp": "14.47",
            "no_count_fp": "0.00",
            "yes_total_cost_dollars": "7.379700",
            "no_total_cost_dollars": "0.000000",
            "revenue": 1447,
            "fee_cost": "0.255800",
            "settled_time": "2026-07-19T15:45:12.627061Z",
        }
        changed = crypto.settle_live_bets_from_kalshi(
            portfolio,
            settings(),
            settlements=[settlement],
        )
        self.assertTrue(changed)
        user_bet, bot_bet = portfolio["bets"]
        self.assertEqual(user_bet["payout"], 9.47)
        self.assertEqual(user_bet["profit"], 4.47)
        self.assertEqual(bot_bet["payout"], 5.0)
        self.assertEqual(bot_bet["profit"], 2.36)

    def test_user_bets_do_not_consume_bot_slot_or_duplicate_filter(self):
        user_bet = {
            "status": "open",
            "mode": "live",
            "strategy_owner": "user_bet",
            "user_bet": True,
            "ticker": "KXBTC15M-WINDOW",
            "event_ticker": "KXBTC15M-WINDOW",
            "asset": "BTC",
            "close_time": "2026-07-19T15:15:00Z",
            "side": "yes",
            "stake": 5,
        }
        candidate = {
            "ticker": "KXBTC15M-WINDOW",
            "event_ticker": "KXBTC15M-WINDOW",
            "series_ticker": "KXBTC15M",
            "asset": "BTC",
            "market_kind": "above",
            "is_15m_market": True,
            "side": "yes",
            "minutes_to_close": 7,
            "close_time": "2026-07-19T15:15:00Z",
            "entry_price": 50,
            "edge": 23,
            "confidence": 84,
        }
        portfolio = {"bets": [user_bet], "balance": 700}
        review = review_candidate(
            portfolio,
            candidate,
            "2026-07-19",
            lambda _value=None: "2026-07-19",
            max_open=1,
        )
        self.assertTrue(review["eligible"])
        self.assertFalse(
            crypto.duplicate_open(portfolio, candidate["ticker"], candidate["side"])
        )
        self.assertEqual(crypto.bot_open_bets(portfolio), [])

    def test_user_bets_are_excluded_from_bot_analytics(self):
        bet = {
            "strategy_owner": "user_bet",
            "user_bet": True,
            "entry_price": 51,
        }
        self.assertTrue(crypto.is_user_crypto_bet(bet))
        self.assertFalse(crypto.crypto_analytics_included(bet, settings()))


if __name__ == "__main__":
    unittest.main()
