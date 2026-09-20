import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from dashboard import (
    CENTRAL_TZ,
    CRYPTO_CANDIDATE_HISTORY_CACHE,
    HTML,
    SPORTS_CANDIDATE_HISTORY_CACHE,
    build_combined_performance,
    build_daily_performance,
    compact_dashboard_state,
    is_regular_sports_bot_row,
    parse_central_datetime,
    crypto_candidate_history,
    reset_sports_campaign_bot,
    sports_source_analytics,
    sports_candidate_history,
)


class DashboardPerformanceTests(unittest.TestCase):
    def test_sports_source_analytics_reconciles_mutually_exclusive_ledgers(self):
        now = datetime(2026, 9, 4, 9, 0, tzinfo=CENTRAL_TZ)
        rows = [
            {"result": "WIN", "profit": 10, "stake": 20, "settled_at": "2026-09-04T08:00:00-05:00", "strategy_owner": "live_campaign"},
            {"result": "LOSS", "profit": -6, "stake": 5, "settled_at": "2026-09-03T08:00:00-05:00", "source": "trusted_capper"},
            {"result": "WIN", "profit": 12, "stake": 10, "settled_at": "2026-09-02T08:00:00-05:00", "source": "user_manual"},
            {"result": "LOSS", "profit": -0.06, "stake": 0.06, "settled_at": "2026-09-01T08:00:00-05:00", "source": "manual_live_test"},
        ]
        open_rows = [
            {"status": "open", "stake": 7, "source": "trusted_capper"},
            {"status": "open", "stake": 9, "strategy_owner": "user_bet"},
        ]

        result = sports_source_analytics(open_rows, rows, [rows[0]], now=now)

        self.assertEqual(10.0, result["regular_bot"]["profit"])
        self.assertEqual(-6.0, result["influenced_bets"]["profit"])
        self.assertEqual(12.0, result["user_live"]["profit"])
        self.assertEqual(-0.06, result["system_test"]["profit"])
        self.assertEqual(15.94, result["all_sports"]["profit"])
        self.assertEqual(16.0, result["all_sports"]["open_exposure"])
        self.assertEqual(15.94, result["all_sports"]["month_to_date"]["profit"])
        self.assertEqual(10.0, result["regular_bot"]["today"]["profit"])
        self.assertTrue(result["reconciliation"]["settled_count_matches"])
        self.assertTrue(result["reconciliation"]["open_count_matches"])
        self.assertEqual(0.0, result["reconciliation"]["profit_delta"])
        self.assertEqual(0.0, result["reconciliation"]["open_exposure_delta"])

    def test_regular_sports_bot_classifier_excludes_user_and_influenced_rows(self):
        self.assertTrue(is_regular_sports_bot_row({"strategy_owner": "live_campaign"}))
        self.assertFalse(is_regular_sports_bot_row({"source": "trusted_capper"}))
        self.assertFalse(is_regular_sports_bot_row({"strategy_owner": "manual_live_import"}))
        self.assertFalse(is_regular_sports_bot_row({"source": "manual_live_test"}))

    def test_sports_overview_labels_and_renders_separate_source_ledgers(self):
        self.assertIn("Regular Bot All-Time P&amp;L", HTML)
        self.assertIn("Current Account Equity &amp; Automated Bot Performance", HTML)
        self.assertIn("Regular Sports Bot P&amp;L", HTML)
        self.assertIn('id="overview-sports-account-pnl"', HTML)
        self.assertIn('id="overview-sports-aibetpicks-pnl"', HTML)
        self.assertIn('id="overview-sports-user-pnl"', HTML)
        self.assertIn('id="overview-sports-test-pnl"', HTML)
        self.assertIn("sports.source_analytics", HTML)
        self.assertIn("Regular Sports Bot Performance", HTML)

    def test_sports_dashboard_replaces_retired_capper_with_aibetpicks(self):
        self.assertIn('data-testid="aibetpicks-panel"', HTML)
        self.assertIn('id="aibetpicks-rows"', HTML)
        self.assertIn('id="SPORTS_AIBETPICKS_ENABLED"', HTML)
        self.assertIn("renderAIBetPicks(sports.aibetpicks", HTML)
        self.assertNotIn('/api/sports/capper/', HTML)
        self.assertNotIn('id="sports-capper-paste"', HTML)
        self.assertNotIn('id="SPORTS_CAPPER_ENABLED"', HTML)
        self.assertNotIn('capper-counterfactual-reviewed', HTML)

    def test_crypto_strategy_summary_uses_probability_edge_kelly_gate(self):
        self.assertIn("Probability/Edge Unit Gate", HTML)
        self.assertIn('id="CRYPTO_15M_UNIT_SIZE_PCT"', HTML)
        self.assertIn('id="CRYPTO_15M_UNIT_5_MIN_EDGE_LOW"', HTML)
        self.assertIn('id="CRYPTO_15M_UNIT_RECOVERY_ENABLED"', HTML)
        self.assertIn('id="CRYPTO_15M_POOLED_RECOVERY_ENABLED"', HTML)
        self.assertIn('id="CRYPTO_15M_RECOVERY_MAX_OVERLAYS_PER_EXPIRY"', HTML)
        self.assertIn('id="CRYPTO_15M_UNIT_RECOVERY_MIN_PROBABILITY_UNITS"', HTML)
        self.assertIn('id="CRYPTO_15M_UNIT_RECOVERY_MAX_TOTAL_UNITS"', HTML)
        self.assertIn("Bounded Recovery", HTML)
        self.assertIn("one durable pooled campaign high-water mark", HTML)
        self.assertIn("open-profit reservation", HTML)
        self.assertIn("no loss-chasing size escalation", HTML)
        self.assertIn("all ledgers continue across midnight", HTML)
        self.assertIn("CRYPTO_15M_UNIT_SIZE_PCT || 0.75", HTML)
        self.assertIn("fractional Kelly", HTML)
        self.assertIn("win probability", HTML)
        self.assertIn("90% Prob Interval", HTML)
        self.assertIn('id="CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS"', HTML)
        self.assertIn('id="CRYPTO_LIVE_SLIPPAGE_RECONFIRM_ENABLED"', HTML)
        self.assertIn('id="CRYPTO_LIVE_SLIPPAGE_RECONFIRM_SECONDS"', HTML)
        self.assertIn('id="CRYPTO_LIVE_SLIPPAGE_RECONFIRM_MAX_MOVE_CENTS"', HTML)
        self.assertIn("larger moves wait", HTML)
        self.assertIn("base size only", HTML)
        self.assertIn("GOLD, SILVER &amp; WTI", HTML)
        self.assertIn('class="strategy-requirements-grid"', HTML)
        self.assertIn("crypto_15m_action_gate", HTML)
        self.assertNotIn("ordinary same-expiry position", HTML)
        self.assertNotIn("low-edge pilot off", HTML)

    def test_crypto_dashboard_has_recovery_counterfactual_analytics(self):
        self.assertIn('data-testid="crypto-recovery-counterfactual"', HTML)
        self.assertIn('id="crypto-recovery-cf-incremental-profit"', HTML)
        self.assertIn('id="crypto-recovery-cf-bots"', HTML)
        self.assertIn('id="crypto-recovery-cf-recent"', HTML)
        self.assertIn("analytics.recovery_counterfactual", HTML)
        self.assertIn("same fills at base size only", HTML)

    def test_crypto_dashboard_has_isolated_cycle_shadow_panel(self):
        self.assertIn('data-testid="crypto-cycle-shadow-panel"', HTML)
        self.assertIn('id="crypto-cycle-shadow-bots"', HTML)
        self.assertIn('id="crypto-cycle-shadow-recent"', HTML)
        self.assertIn("function renderCryptoCycleShadow(data)", HTML)
        self.assertIn(
            "renderCryptoCycleShadow(cryptoReport.cycle_shadow || {})",
            HTML,
        )
        self.assertIn("This panel cannot place orders", HTML)
        self.assertIn("Crypto V6 Strategy Ledger", HTML)
        self.assertIn("early 10–13 minute flow fade", HTML)
        self.assertIn("late 1.75–2.5 minute convex setup", HTML)
        self.assertIn("four attempts, three contracts, $1.50 per attempt", HTML)
        self.assertIn("Flat one-contract and maker/taker results", HTML)
        self.assertIn("no automatic promotion", HTML)
        self.assertIn('id="crypto-cycle-shadow-risk-capped"', HTML)
        self.assertIn('id="crypto-cycle-shadow-correlation"', HTML)
        self.assertIn('id="crypto-cycle-shadow-flat-profit"', HTML)
        self.assertIn('id="crypto-cycle-shadow-recovery-incremental"', HTML)
        self.assertIn('id="crypto-cycle-shadow-attempt-performance"', HTML)
        self.assertIn('id="crypto-cycle-shadow-price-performance"', HTML)
        self.assertIn('data-testid="crypto-cycle-cap-counterfactual"', HTML)
        self.assertIn('id="crypto-cycle-cap-counterfactual-status"', HTML)
        self.assertIn('data-testid="crypto-positive-edge-discovery"', HTML)
        self.assertIn('id="crypto-edge-discovery-funnel"', HTML)
        self.assertIn('id="crypto-edge-discovery-raw-edge"', HTML)
        self.assertIn('id="crypto-edge-discovery-recent"', HTML)
        self.assertIn("positive_edge_discovery", HTML)
        self.assertIn("market_guardrail_adjustment_pp", HTML)
        self.assertIn("This research cannot place orders", HTML)
        self.assertIn("Bounded-Residual Shadow", HTML)
        self.assertIn("Pre-V3 records", HTML)
        self.assertIn("maker_taker_counterfactual", HTML)
        self.assertIn("flat_stake_counterfactual", HTML)
        self.assertIn("queue position not modeled", HTML)
        self.assertIn("one global position per expiry", HTML)
        self.assertIn("max_attempt_cost_dollars", HTML)

    def test_crypto_dashboard_has_locked_btc_15m_sprint_panel(self):
        self.assertIn('data-testid="crypto-btc-15m-sprint"', HTML)
        self.assertIn('id="crypto-sprint-status"', HTML)
        self.assertIn('id="crypto-sprint-freeze"', HTML)
        self.assertIn('id="crypto-sprint-lanes"', HTML)
        self.assertIn('id="crypto-sprint-controls"', HTML)
        self.assertIn('id="crypto-sprint-recent"', HTML)
        self.assertIn("function renderCryptoBtc15mSprint(data)", HTML)
        self.assertIn(
            "renderCryptoBtc15mSprint(cryptoReport.btc_15m_sprint || {})",
            HTML,
        )
        self.assertIn("one-cent reserve", HTML)
        self.assertIn("no automatic promotion", HTML)

    def test_crypto_dashboard_has_exact_brti_settlement_lag_shadow(self):
        self.assertIn('data-testid="crypto-settlement-lag-shadow"', HTML)
        self.assertIn('id="crypto-settlement-lag-current"', HTML)
        self.assertIn('id="crypto-settlement-lag-recent"', HTML)
        self.assertIn('id="crypto-settlement-lag-invalid"', HTML)
        self.assertIn('data-testid="crypto-directional-opposition-v2"', HTML)
        self.assertIn('id="crypto-directional-v2-recent"', HTML)
        self.assertIn('id="crypto-directional-pair-sample"', HTML)
        self.assertIn('id="crypto-directional-pair-recent"', HTML)
        self.assertIn('id="crypto-settlement-lag-fok"', HTML)
        self.assertIn('id="crypto-settlement-lag-days"', HTML)
        self.assertIn('id="crypto-directional-pair-days"', HTML)
        self.assertIn('id="crypto-directional-pair-integer"', HTML)
        self.assertIn('id="crypto-directional-doge-sample"', HTML)
        self.assertIn('id="crypto-directional-eth-sample"', HTML)
        self.assertIn('id="crypto-directional-assets-recent"', HTML)
        self.assertIn('data-testid="crypto-signal-tournament-shadow"', HTML)
        self.assertIn('id="crypto-signal-tournament-lanes"', HTML)
        self.assertIn('id="crypto-signal-tournament-recent"', HTML)
        self.assertIn('data-testid="crypto-complement-arb-shadow"', HTML)
        self.assertIn('id="crypto-complement-arb-parity"', HTML)
        self.assertIn('id="crypto-complement-arb-recent"', HTML)
        self.assertIn("function renderCryptoSettlementLagShadow(data)", HTML)
        self.assertIn("function renderCryptoSignalTournament(data)", HTML)
        self.assertIn("function renderCryptoExecutionLab(data)", HTML)
        self.assertIn('data-testid="crypto-execution-lab-shadow"', HTML)
        self.assertIn("function renderCryptoAssetSpecialist(data)", HTML)
        self.assertIn('data-testid="crypto-asset-specialist-shadow"', HTML)
        self.assertIn('id="crypto-asset-specialist-lanes"', HTML)
        self.assertIn('id="crypto-asset-specialist-recent"', HTML)
        self.assertIn("renderCryptoAssetSpecialist", HTML)
        self.assertIn("function renderCryptoComplementArb(data)", HTML)
        self.assertIn("crypto.settlement_lag_shadow", HTML)
        self.assertIn("Finalized official market results alone settle records", HTML)
        self.assertIn("It cannot place orders", HTML)

    def test_dashboard_poll_payload_bounds_large_table_rows(self):
        bulky = {
            "id": "row",
            "scalar": 1,
            "nested": {"visible": 2, "deep": {"discarded": "x" * 1000}},
        }
        state = {
            "sports": {
                "history": [dict(bulky) for _ in range(100)],
                "report": {"top_candidates": [dict(bulky) for _ in range(100)]},
                "trusted_capper": {"tickets": [dict(bulky) for _ in range(100)]},
            },
            "crypto": {
                "history": [dict(bulky) for _ in range(100)],
                "candidate_log": [dict(bulky) for _ in range(100)],
                "report": {"shadow": {"recent_records": [dict(bulky) for _ in range(100)]}},
                "perps_shadow": {},
            },
        }

        compact = compact_dashboard_state(state)

        self.assertEqual(len(compact["sports"]["history"]), 40)
        self.assertEqual(len(compact["sports"]["report"]["top_candidates"]), 40)
        self.assertEqual(len(compact["crypto"]["candidate_log"]), 50)
        self.assertEqual(len(compact["crypto"]["report"]["shadow"]["recent_records"]), 40)
        self.assertNotIn("deep", compact["sports"]["history"][0]["nested"])
        self.assertEqual(compact["payload_metadata"]["view"], "bounded_dashboard_summary")

    def test_crypto_dashboard_separates_current_strategy_performance(self):
        self.assertIn('data-testid="crypto-current-strategy-performance"', HTML)
        for element_id in (
            "overview-crypto-current-pnl",
            "overview-crypto-current-record",
            "overview-crypto-current-roi",
            "crypto-current-pnl",
            "crypto-current-record",
            "crypto-current-roi",
        ):
            self.assertIn(f'id="{element_id}"', HTML)
        self.assertIn("current_strategy_performance", HTML)
        self.assertIn("unit_capacity_limited", HTML)
        self.assertIn("minimum_unit_capacity", HTML)

    def test_crypto_candidate_log_has_price_and_time_filters(self):
        self.assertIn('id="crypto-log-time-range"', HTML)
        self.assertIn('<option value="15">Last 15 minutes</option>', HTML)
        self.assertIn('<option value="360">Last 6 hours</option>', HTML)
        self.assertIn('id="crypto-log-min-price"', HTML)
        self.assertIn('id="crypto-log-max-price"', HTML)
        for element_id in (
            "crypto-log-min-edge", "crypto-log-max-edge",
            "crypto-log-min-confidence", "crypto-log-max-confidence",
            "crypto-log-asset", "crypto-log-side", "crypto-log-decision",
            "crypto-log-search",
        ):
            self.assertIn(f'id="{element_id}"', HTML)
        self.assertIn("function cryptoSkipReasonText(row)", HTML)
        self.assertIn("c.probability?.p_yes == null", HTML)
        self.assertIn("c.probability?.p_low == null", HTML)
        self.assertIn("live_quote_slippage_reconfirm_wait", HTML)

    def test_crypto_candidate_history_incrementally_loads_time_windows(self):
        now = datetime.now(timezone.utc)

        def event(minutes_ago, ticker):
            return {
                "type": "scan_analytics",
                "ts": (now - timedelta(minutes=minutes_ago)).isoformat(),
                "top_candidates": [{
                    "asset": "BTC",
                    "ticker": ticker,
                    "side": "yes",
                    "entry_price": 50,
                    "edge": 3,
                    "confidence": 72,
                    "decision": "skipped",
                    "skip_reasons": ["campaign_entry_window"],
                }],
            }

        with TemporaryDirectory() as temp_dir:
            events_path = Path(temp_dir) / "crypto_events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(row) for row in (
                    event(30, "OLDER"),
                    event(5, "RECENT"),
                )) + "\n",
                encoding="utf-8",
            )
            empty_cache = {
                "path": "",
                "identity": None,
                "offset": 0,
                "fragment": b"",
                "rows": [],
            }
            with patch.dict(CRYPTO_CANDIDATE_HISTORY_CACHE, empty_cache, clear=True):
                hour = crypto_candidate_history(events_path, minutes=60)
                fifteen = crypto_candidate_history(events_path, minutes=15)
                self.assertEqual([row["ticker"] for row in hour["rows"]], ["RECENT", "OLDER"])
                self.assertEqual([row["ticker"] for row in fifteen["rows"]], ["RECENT"])

                with events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event(1, "NEWEST")) + "\n")
                updated = crypto_candidate_history(events_path, minutes=15)
                unchanged = crypto_candidate_history(events_path, minutes=15)
                self.assertEqual(
                    [row["ticker"] for row in updated["rows"]],
                    ["NEWEST", "RECENT"],
                )
                self.assertEqual(len(unchanged["rows"]), 2)

    def test_sports_log_dashboard_has_structured_filters(self):
        for element_id in (
            "sports-log-time-range", "sports-log-min-price", "sports-log-max-price",
            "sports-log-min-edge", "sports-log-max-edge", "sports-log-decision",
            "sports-log-sport", "sports-log-market", "sports-log-search",
            "logs-sports-candidates",
        ):
            self.assertIn(f'id="{element_id}"', HTML)
        self.assertIn("/api/sports/candidate-log", HTML)
        self.assertIn("Raw Sports Log", HTML)
        self.assertIn("Books F/R/S", HTML)

    def test_sports_candidate_history_links_placed_bet_to_candidate_metrics(self):
        stamp = datetime.now(CENTRAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        ticker = "KXMLBGAME-26AUG11CLEDET-DET"
        candidate = (
            f"[{stamp}] Top sports candidate: edge=4.25% skips=[] moneyline Detroit Tigers / {ticker} "
            "edge_basis=guarded_nominal_net raw_books=6 families=3 stale=2 conf=91.0 pro=96.0 final=93.0 entry=44.0c vol=12345.0 units=2u/target=2u"
        )
        placed = (
            f"[{stamp}] LIVE SPORTS BET: owner=live_campaign moneyline Detroit Tigers Yes {ticker} "
            "stake=$30.00 edge=4.25% entry=43.0c conf=92.0 pro=97.0 final=94.0 families=4 "
            "live_campaign=on bot=1 quality=qualified units=2u"
        )
        empty_cache = {
            "path": "", "identity": None, "offset": 0, "fragment": b"",
            "rows": [], "latest_by_ticker": {},
        }
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "sports.log"
            log_path.write_text(candidate + "\n" + placed + "\n", encoding="utf-8")
            with patch.dict(SPORTS_CANDIDATE_HISTORY_CACHE, empty_cache, clear=True):
                result = sports_candidate_history(log_path, minutes=60)
        self.assertEqual(["placed", "eligible"], [row["decision"] for row in result["rows"]])
        bet = result["rows"][0]
        self.assertEqual(43.0, bet["entry_price"])
        self.assertEqual(92.0, bet["confidence"])
        self.assertEqual(4, bet["families"])
        self.assertEqual(6, bet["raw_books"])
        self.assertEqual(2, bet["stale_books"])
        self.assertEqual(30.0, bet["stake"])
        self.assertEqual(2.0, bet["units"])

    def test_sports_dashboard_supports_dynamic_campaign_cards_and_bot_columns(self):
        self.assertIn('id="SPORTS_LIVE_CAMPAIGN_BOT_COUNT"', HTML)
        self.assertIn('id="SPORTS_UNIT_SIZE_PCT"', HTML)
        self.assertIn('id="SPORTS_UNIT_INCREMENT"', HTML)
        self.assertIn('id="sports-unit-size-preview"', HTML)
        self.assertNotIn('id="SPORTS_UNIT_SIZE"', HTML)
        self.assertIn('id="SPORTS_UNIT_5_MIN_BOOK_FAMILIES"', HTML)
        self.assertIn("1.5u, 2.5u, 3.5u, and 4.5u", HTML)
        self.assertIn('id="SPORTS_LIVE_CAMPAIGN_BOT_COUNT" inputmode="numeric" min="1" max="5"', HTML)
        self.assertIn('id="sports-campaign-cards"', HTML)
        self.assertIn('id="overview-sports-campaign-cards"', HTML)
        self.assertIn("function renderSportsCampaignCards", HTML)
        self.assertIn('data-testid="sports-unit-requirements"', HTML)
        self.assertIn('id="sports-unit-requirements-rows"', HTML)
        self.assertIn("function renderSportsUnitRequirements", HTML)
        self.assertIn("[0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5]", HTML)
        self.assertIn("Live risk reduction · relaxed entry shadow-tested", HTML)
        self.assertIn("campaignBotNumber(b)", HTML)
        self.assertIn("always-on units", HTML)
        self.assertIn("p.commence_time ? esc(shortTime(p.commence_time))", HTML)
        self.assertNotIn("ticket.market_line === null || ticket.market_line === undefined ? '' : ' ' + esc(ticket.market_line)", HTML)

    def test_sports_dashboard_has_no_goal_or_cycle_controls(self):
        self.assertNotIn('id="overview-sports-goal"', HTML)
        self.assertNotIn('id="sports-recovery-next"', HTML)
        self.assertNotIn('id="SPORTS_LIVE_CAMPAIGN_BOT_1_CYCLE_PROFIT"', HTML)
        self.assertNotIn("Reset Bot to Cycle 1", HTML.split('function renderCryptoCampaignCards', 1)[0])

    def test_sports_campaign_reset_rotates_ledger_id_without_deleting_history(self):
        with TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "bot_settings.json"
            portfolio_path = Path(temp_dir) / "sports_paper_portfolio.json"
            settings_path.write_text(
                '{"SPORTS_LIVE_CAMPAIGN_BOT_COUNT":"2"}',
                encoding="utf-8",
            )
            portfolio_path.write_text(
                '{"bets":[],"history":[{"status":"settled","profit":2}]}',
                encoding="utf-8",
            )
            with patch("dashboard.SETTINGS_FILE", settings_path), patch(
                "dashboard.SPORTS_PORTFOLIO_FILE",
                portfolio_path,
            ):
                result = reset_sports_campaign_bot(2)

            saved_settings = settings_path.read_text(encoding="utf-8")
            saved_portfolio = portfolio_path.read_text(encoding="utf-8")
            self.assertIn("SPORTS_LIVE_CAMPAIGN_BOT_2_CAMPAIGN_ID", saved_settings)
            self.assertEqual(result["bot_number"], 2)
            self.assertIn('"history":[{"status":"settled","profit":2}]', saved_portfolio)

    def test_sports_campaign_reset_is_blocked_while_position_is_open(self):
        with TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "bot_settings.json"
            portfolio_path = Path(temp_dir) / "sports_paper_portfolio.json"
            settings_path.write_text(
                '{"SPORTS_LIVE_CAMPAIGN_BOT_COUNT":"1"}',
                encoding="utf-8",
            )
            portfolio_path.write_text(
                '{"bets":[{"status":"open","strategy_owner":"live_campaign","bot_number":1}]}',
                encoding="utf-8",
            )
            with patch("dashboard.SETTINGS_FILE", settings_path), patch(
                "dashboard.SPORTS_PORTFOLIO_FILE",
                portfolio_path,
            ):
                with self.assertRaisesRegex(ValueError, "has_an_open_position"):
                    reset_sports_campaign_bot(1)

    def test_crypto_dashboard_uses_continuous_high_water_workflow(self):
        self.assertNotIn('id="crypto-campaign-reset-status"', HTML)
        self.assertNotIn("function resetCryptoBotCycles(botNumber)", HTML)
        self.assertNotIn("Reset Bot to Cycle 1", HTML)
        self.assertIn("Continuous opportunity mode has no profit target or cycle stop", HTML)
        self.assertIn("High water", HTML)
        self.assertIn("Ledger P/L", HTML)

    def test_crypto_capacity_reset_preserves_history_and_writes_marker(self):
        with TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "crypto_settings.json"
            portfolio_path = Path(temp_dir) / "crypto_live_portfolio.json"
            settings_path.write_text(
                '{"CRYPTO_EXECUTION_MODE":"live","CRYPTO_15M_CAMPAIGN_BOT_COUNT":"3"}',
                encoding="utf-8",
            )
            portfolio_path.write_text(
                '{"bets":[{"status":"settled","strategy_owner":"crypto_15m_campaign",'
                '"bot_number":3,"profit":-396.02}],'
                '"crypto_15m_high_water_ledgers":{"3":{"outstanding_drawdown":396.02}}}',
                encoding="utf-8",
            )
            with patch("dashboard.CRYPTO_SETTINGS_FILE", settings_path), patch(
                "dashboard.CRYPTO_LIVE_PORTFOLIO_FILE",
                portfolio_path,
            ):
                from dashboard import reset_crypto_campaign_bot

                result = reset_crypto_campaign_bot(3)

            saved = json.loads(portfolio_path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(saved["bets"]))
            self.assertEqual(
                396.02,
                saved["crypto_15m_high_water_ledgers"]["3"]["outstanding_drawdown"],
            )
            self.assertTrue(saved["crypto_15m_daily_capacity_resets"]["3"]["reset_at"])
            self.assertTrue(result["bet_history_preserved"])
            self.assertTrue(Path(result["backup_file"]).exists())

    def test_crypto_dashboard_has_shadow_market_regime_panel(self):
        self.assertIn('data-testid="crypto-market-regime-lab"', HTML)
        self.assertIn('id="crypto-regime-assets"', HTML)
        self.assertIn('id="crypto-regime-1h-pill"', HTML)
        self.assertIn('id="crypto-regime-4h-pill"', HTML)
        self.assertIn('id="crypto-regime-24h-pill"', HTML)
        self.assertIn('data-testid="crypto-market-trend-card"', HTML)
        self.assertIn('id="crypto-overview-regime-1h-pill"', HTML)
        self.assertIn('id="crypto-overview-regime-4h-pill"', HTML)
        self.assertIn('id="crypto-overview-regime-24h-pill"', HTML)
        self.assertIn('class="trend-pill neutral"', HTML)
        self.assertIn("function setTrendSignal(pillId, cardId, value)", HTML)
        self.assertIn("trendPillHtml(row.direction_24h || 'neutral')", HTML)
        self.assertIn("P(up)", HTML)
        self.assertIn("forward accuracy", HTML)
        self.assertIn("LIVE BOUNDED SIGNAL", HTML)
        self.assertIn("maximum_live_adjustment_pp", HTML)
        self.assertIn("function renderCryptoMarketRegime(data)", HTML)
        self.assertIn("renderCryptoMarketRegime(report.market_regime || {})", HTML)

    def test_central_time_parsing_converts_utc_and_preserves_naive_wall_time(self):
        utc_value = parse_central_datetime("2026-07-21T04:30:00Z")
        naive_value = parse_central_datetime("2026-07-21T11:30:00")

        self.assertEqual(utc_value.isoformat(), "2026-07-20T23:30:00-05:00")
        self.assertEqual(naive_value.isoformat(), "2026-07-21T11:30:00-05:00")

    def test_daily_performance_groups_utc_settlements_by_central_day(self):
        rows = [
            {"settled_at": "2026-07-21T04:30:00Z", "result": "WIN", "profit": 2, "stake": 1},
            {"settled_at": "2026-07-21T05:30:00Z", "result": "LOSS", "profit": -1, "stake": 1},
        ]

        daily = build_daily_performance(rows, today=date(2026, 7, 21))

        self.assertEqual(daily[0]["date"], "2026-07-20")
        self.assertEqual(daily[0]["profit"], 2.0)
        self.assertEqual(daily[1]["date"], "2026-07-21")
        self.assertEqual(daily[1]["profit"], -1.0)

    def test_daily_performance_is_gap_free_and_excludes_manual_rows(self):
        rows = [
            {"settled_at": "2026-07-18T10:00:00", "result": "WIN", "profit": 8, "stake": 4},
            {"settled_at": "2026-07-18T12:00:00", "result": "LOSS", "profit": -3, "stake": 3},
            {"settled_at": "2026-07-20T09:00:00", "result": "WIN", "profit": 2, "stake": 2},
            {
                "settled_at": "2026-07-20T10:00:00",
                "result": "WIN",
                "profit": 100,
                "stake": 1,
                "strategy_owner": "user_bet",
            },
        ]

        daily = build_daily_performance(rows, today=date(2026, 7, 20))

        self.assertEqual([row["date"] for row in daily], ["2026-07-18", "2026-07-19", "2026-07-20"])
        self.assertEqual(daily[0]["profit"], 5.0)
        self.assertEqual(daily[0]["wins"], 1)
        self.assertEqual(daily[0]["losses"], 1)
        self.assertEqual(daily[1]["profit"], 0.0)
        self.assertEqual(daily[2]["profit"], 2.0)
        self.assertEqual(daily[2]["settled"], 1)

    def test_combined_performance_tracks_contributions_and_bankroll(self):
        sports = [
            {"date": "2026-07-19", "profit": 5, "stake": 10, "wins": 1, "losses": 0},
            {"date": "2026-07-20", "profit": -2, "stake": 4, "wins": 0, "losses": 1},
        ]
        crypto = [
            {"date": "2026-07-19", "profit": -1, "stake": 2, "wins": 0, "losses": 1},
            {"date": "2026-07-20", "profit": 3, "stake": 3, "wins": 1, "losses": 0},
        ]

        performance = build_combined_performance(sports, crypto, current_bankroll=105)

        self.assertEqual(performance["starting_bankroll"], 100.0)
        self.assertEqual(performance["total_bot_profit"], 5.0)
        self.assertEqual(performance["daily"][0]["profit"], 4.0)
        self.assertEqual(performance["daily"][0]["sports_profit"], 5.0)
        self.assertEqual(performance["daily"][0]["crypto_profit"], -1.0)
        self.assertEqual([row["bankroll"] for row in performance["bankroll"]], [104.0, 105.0])


if __name__ == "__main__":
    unittest.main()
