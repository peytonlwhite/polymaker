import asyncio
import json
import time
import unittest

from sports_market_stream import KalshiSportsStream


class SportsMarketStreamTests(unittest.TestCase):
    def setUp(self):
        self.stream = KalshiSportsStream(api_key_id="test")

    def test_current_ticker_dollar_fields_become_cents(self):
        self.stream._handle_message(
            """{
              "type": "ticker",
              "msg": {
                "market_ticker": "TEST-MARKET",
                "price_dollars": "0.480",
                "yes_bid_dollars": "0.450",
                "yes_ask_dollars": "0.530",
                "volume_fp": "33896.00",
                "open_interest_fp": "20422.00",
                "yes_bid_size_fp": "300.00",
                "yes_ask_size_fp": "150.00",
                "last_trade_size_fp": "25.00",
                "dollar_volume": 16948,
                "dollar_open_interest": 10211,
                "ts_ms": 1669149841000
              }
            }"""
        )
        snapshot = self.stream.snapshot("TEST-MARKET")
        self.assertEqual(snapshot["yes_bid"], 45.0)
        self.assertEqual(snapshot["yes_ask"], 53.0)
        self.assertEqual(snapshot["no_bid"], 47.0)
        self.assertEqual(snapshot["no_ask"], 55.0)
        self.assertEqual(snapshot["volume"], 33896.0)
        self.assertEqual(snapshot["last_trade_size"], 25.0)
        self.assertEqual(snapshot["dollar_volume"], 16948.0)
        self.assertTrue(snapshot["fresh"])

    def test_current_orderbook_fixed_point_fields_build_top_of_book(self):
        self.stream._handle_message(
            """{
              "type": "orderbook_snapshot",
              "msg": {
                "market_ticker": "TEST-BOOK",
                "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
                "no_dollars_fp": [["0.4400", "146.00"], ["0.4600", "20.00"]]
              }
            }"""
        )
        snapshot = self.stream.snapshot("TEST-BOOK")
        self.assertEqual(snapshot["yes_bid"], 22.0)
        self.assertEqual(snapshot["yes_ask"], 44.0)
        self.assertEqual(snapshot["yes_bid_size"], 333.0)
        self.assertEqual(snapshot["yes_ask_size"], 146.0)
        self.assertEqual(snapshot["yes_bid_levels"][0], {"price": 22.0, "size": 333.0})
        self.assertEqual(snapshot["yes_ask_levels"][0], {"price": 44.0, "size": 146.0})

        self.stream._handle_message(
            """{
              "type": "orderbook_delta",
              "msg": {
                "market_ticker": "TEST-BOOK",
                "price_dollars": "0.4400",
                "delta_fp": "-146.00",
                "side": "no"
              }
            }"""
        )
        snapshot = self.stream.snapshot("TEST-BOOK")
        self.assertEqual(snapshot["yes_ask"], 46.0)

    def test_orderbook_delta_removes_floating_point_residue(self):
        self.stream._handle_message(
            '{"type":"orderbook_snapshot","msg":{"market_ticker":"TEST-RESIDUE",'
            '"yes":[[35,0.3],[30,5]],"no":[[40,0.3],[45,4]]}}'
        )
        self.stream._handle_message(
            '{"type":"orderbook_delta","msg":{"market_ticker":"TEST-RESIDUE",'
            '"price":35,"delta":-0.29999999999999954,"side":"yes"}}'
        )
        self.stream._handle_message(
            '{"type":"orderbook_delta","msg":{"market_ticker":"TEST-RESIDUE",'
            '"price":40,"delta":-0.29999999999999954,"side":"no"}}'
        )
        snapshot = self.stream.snapshot("TEST-RESIDUE")
        self.assertEqual(snapshot["yes_bid"], 30.0)
        self.assertEqual(snapshot["yes_ask"], 45.0)
        self.assertEqual(snapshot["yes_bid_size"], 5.0)
        self.assertEqual(snapshot["yes_ask_size"], 4.0)

    def test_orderbook_snapshot_ignores_negligible_levels(self):
        self.stream._handle_message(
            '{"type":"orderbook_snapshot","msg":{"market_ticker":"TEST-TINY",'
            '"yes":[[35,0.0000000000004],[30,5]],'
            '"no":[[40,0.0000000000004],[45,4]]}}'
        )
        snapshot = self.stream.snapshot("TEST-TINY")
        self.assertEqual(snapshot["yes_bid"], 30.0)
        self.assertEqual(snapshot["yes_ask"], 45.0)

    def test_trade_does_not_make_an_old_quote_fresh(self):
        self.stream._handle_message(
            '{"type":"ticker","msg":{"market_ticker":"TEST-AGE","yes_bid_dollars":"0.40","yes_ask_dollars":"0.42"}}'
        )
        with self.stream._lock:
            self.stream._snapshots["TEST-AGE"]["quote_received_unix"] = time.time() - 30
        self.stream._handle_message(
            '{"type":"trade","msg":{"market_ticker":"TEST-AGE","yes_price_dollars":"0.41","count_fp":"2.00"}}'
        )
        self.assertFalse(self.stream.snapshot("TEST-AGE", max_age_seconds=8)["fresh"])

    def test_removing_last_level_clears_price_size_and_complement(self):
        for side in ("yes", "no"):
            with self.subTest(side=side):
                self.stream._handle_message(json.dumps({"type": "orderbook_snapshot", "msg": {
                    "market_ticker": "EMPTY-SIDE", "yes": [[40, 10]], "no": [[45, 12]],
                }}))
                self.stream._handle_message(json.dumps({"type": "orderbook_delta", "msg": {
                    "market_ticker": "EMPTY-SIDE", "side": side,
                    "price": 40 if side == "yes" else 45, "delta": -10 if side == "yes" else -12,
                }}))
                snapshot = self.stream.snapshot("EMPTY-SIDE")
                price_field, complement = ("yes_bid", "no_ask") if side == "yes" else ("yes_ask", "no_bid")
                self.assertIsNone(snapshot.get(price_field))
                self.assertIsNone(snapshot.get(complement))
                self.assertIsNone(snapshot.get(price_field + "_size"))
                self.assertEqual([], snapshot[price_field + "_levels"])

    def test_empty_snapshot_replaces_old_executable_quote(self):
        self.stream._handle_message(json.dumps({"type": "orderbook_snapshot", "msg": {
            "market_ticker": "EMPTY", "yes": [[40, 10]], "no": [[45, 12]],
        }}))
        self.stream._handle_message(json.dumps({"type": "orderbook_snapshot", "msg": {
            "market_ticker": "EMPTY", "yes": [], "no": [],
        }}))
        snapshot = self.stream.snapshot("EMPTY")
        for field in ("yes_bid", "yes_ask", "no_bid", "no_ask", "yes_bid_size", "yes_ask_size"):
            self.assertIsNone(snapshot.get(field), field)

    def test_trade_without_quote_does_not_satisfy_quote_warmup(self):
        self.stream._handle_message(
            '{"type":"trade","msg":{"market_ticker":"ONLY-TRADE","yes_price":41,"count":2}}'
        )
        self.assertFalse(self.stream.snapshot("ONLY-TRADE")["fresh"])
        self.assertEqual(0, self.stream.wait_for_fresh(["ONLY-TRADE"], timeout_seconds=0)["fresh_tickers"])

    def test_empty_stream_side_clears_cached_rest_quote(self):
        from unittest.mock import patch
        import sports_paper_bettor as sports

        self.stream._handle_message(json.dumps({"type": "orderbook_snapshot", "msg": {
            "market_ticker": "EMPTY-ASK", "yes": [[40, 10]], "no": [],
        }}))
        with patch.object(sports, "sports_stream", return_value=self.stream):
            market, _snapshot = sports.market_with_stream_snapshot({
                "ticker": "EMPTY-ASK", "yes_bid": 40, "yes_ask": 45, "no_bid": 55, "no_ask": 60,
                "yes_ask_dollars": "0.45", "no_bid_dollars": "0.55",
            })
            guard = sports.trusted_capper_order_pricing_guard({"kalshi_ticker": "EMPTY-ASK", "order_side": "yes"})
        self.assertIsNone(market.get("yes_ask"))
        self.assertIsNone(market.get("no_bid"))
        self.assertEqual(0.0, sports.market_prices(market)["yes_ask"])
        self.assertEqual(0.0, sports.market_prices(market)["no_bid"])
        self.assertFalse(guard["ok"])
        self.assertEqual("invalid_executable_quote_at_order", guard["error"])

    def test_websocket_error_is_visible_in_status(self):
        self.stream._handle_message(
            '{"type":"error","msg":{"code":6,"msg":"Already subscribed"}}'
        )
        self.assertIn("Already subscribed", self.stream.status()["last_error"])

    def test_subscription_is_not_active_until_acknowledged(self):
        class WebSocket:
            def __init__(self):
                self.messages = []

            async def send(self, payload):
                self.messages.append(json.loads(payload))

        websocket = WebSocket()
        self.stream.subscribe(["ACK-ME"])
        asyncio.run(self.stream._send_pending_subscriptions(websocket))
        self.assertEqual(0, self.stream.status()["subscribed_tickers"])
        message_id = websocket.messages[0]["id"]
        self.assertIs(websocket.messages[0]["params"]["use_yes_price"], True)
        self.stream._handle_message(json.dumps({"id": message_id, "type": "subscribed", "msg": {}}))
        self.assertEqual(1, self.stream.status()["subscribed_tickers"])

    def test_orderbook_sequence_gap_invalidates_depth_and_requests_reconnect(self):
        self.stream._handle_message(
            '{"type":"orderbook_snapshot","seq":10,"msg":{"market_ticker":"SEQ",'
            '"yes":[[40,10]],"no":[[59,12]]}}'
        )
        self.stream._handle_message(
            '{"type":"orderbook_delta","seq":12,"msg":{"market_ticker":"SEQ",'
            '"price":59,"delta":-1,"side":"no"}}'
        )
        snapshot = self.stream.snapshot("SEQ")
        self.assertFalse(snapshot["orderbook_valid"])
        self.assertNotIn("yes_ask_size", snapshot)
        self.assertTrue(self.stream._reconnect_requested.is_set())
        self.assertEqual(1, self.stream.status()["sequence_gap_count"])

    def test_subscription_sequence_allows_interleaved_market_updates(self):
        self.stream._handle_message(
            '{"type":"orderbook_snapshot","sid":7,"seq":10,"msg":{"market_ticker":"SEQ-A",'
            '"yes":[[40,10]],"no":[[42,12]]}}'
        )
        self.stream._handle_message(
            '{"type":"orderbook_snapshot","sid":7,"seq":11,"msg":{"market_ticker":"SEQ-B",'
            '"yes":[[35,8]],"no":[[45,9]]}}'
        )
        self.stream._handle_message(
            '{"type":"orderbook_delta","sid":7,"seq":12,"msg":{"market_ticker":"SEQ-A",'
            '"price":42,"delta":1,"side":"no"}}'
        )
        snapshot = self.stream.snapshot("SEQ-A")
        self.assertTrue(snapshot["orderbook_valid"])
        self.assertFalse(self.stream._reconnect_requested.is_set())
        self.assertEqual(0, self.stream.status()["sequence_gap_count"])

    def test_replacement_drops_obsolete_tickers_and_respects_cap(self):
        self.stream.max_tickers = 2
        self.stream.subscribe(["OLD-1", "OLD-2"])
        self.stream._snapshots["OLD-1"] = {"yes_bid": 40, "received_unix": time.time()}
        self.stream._quote_history["OLD-1"].append({"yes_bid": 40})
        self.stream._trade_history["OLD-1"].append({"price": 41})
        self.stream.replace_subscriptions(["NEW-1", "NEW-2", "NEW-3"])
        with self.stream._lock:
            desired = set(self.stream._desired)
        self.assertEqual(desired, {"NEW-1", "NEW-2"})
        self.assertEqual({}, self.stream.snapshot("OLD-1"))
        self.assertNotIn("OLD-1", self.stream._quote_history)
        self.assertNotIn("OLD-1", self.stream._trade_history)

    def test_wait_for_fresh_reports_coverage(self):
        self.stream._handle_message(
            '{"type":"ticker","msg":{"market_ticker":"FRESH","yes_bid":40,"yes_ask":42}}'
        )
        coverage = self.stream.wait_for_fresh(
            ["FRESH", "MISSING"],
            max_age_seconds=8,
            timeout_seconds=0,
        )
        self.assertEqual(coverage["requested_tickers"], 2)
        self.assertEqual(coverage["fresh_tickers"], 1)
        self.assertEqual(coverage["missing_or_stale_tickers"], 1)


if __name__ == "__main__":
    unittest.main()
