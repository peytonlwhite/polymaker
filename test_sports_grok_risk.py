import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import sports_paper_bettor as sports


class SportsGrokRiskTests(unittest.TestCase):
    def test_contradictory_live_state_vetoes_without_ai_call(self):
        candidate = {
            "game_started": True,
            "minutes_since_start": 82,
            "game_state_features": {"authoritative_progress": False},
            "live_score_context": {"status": "pre"},
        }
        with patch.object(sports, "openai_vote") as openai_mock:
            review = sports.sports_ai_risk_check(candidate)
        self.assertTrue(review["veto"])
        self.assertTrue(review["ai_bypassed"])
        self.assertEqual("deterministic_live_state_guard", review["api"])
        openai_mock.assert_not_called()

    def test_missing_live_state_without_explicit_contradiction_is_not_auto_vetoed(self):
        candidate = {
            "game_started": True,
            "minutes_since_start": 82,
            "game_state_features": {"authoritative_progress": False},
            "live_score_context": {},
        }
        self.assertIsNone(sports.deterministic_live_state_contradiction(candidate))

    def test_deterministic_clearance_bypasses_ai_for_complete_two_book_candidate(self):
        candidate = {
            "kalshi_ticker": "TEST-CLEAR",
            "market_type": "moneyline",
            "game_started": False,
            "game_completed": False,
            "edge": 0.5,
            "kalshi_spread": 1,
            "independent_book_family_count": 2,
            "pricing_v2": {"ok": True, "consensus": {"ok": True}},
        }
        with patch.object(sports, "SPORTS_AI_DETERMINISTIC_BYPASS_ENABLED", True):
            review = sports.deterministic_ai_clearance(candidate)
        self.assertIsNotNone(review)
        self.assertTrue(review["ai_bypassed"])
        self.assertEqual("deterministic", review["effective_provider"])

    def test_missing_live_tennis_context_becomes_one_unit_unresolved_not_search(self):
        candidate = {
            "sport_key": "tennis_atp",
            "market_type": "moneyline",
            "game_started": True,
            "live_odds_age_minutes": 0.3,
            "independent_book_family_count": 2,
            "pricing_v2": {"ok": True},
            "game_state_features": {},
        }
        review = {
            "enabled": True,
            "ok": True,
            "veto": True,
            "risk_level": "high",
            "needs_live_search": True,
            "reasons": ["live_score_context is None and state_quality=unavailable"],
        }
        normalized = sports.normalize_ai_timing_review(candidate, review)
        self.assertFalse(normalized["veto"])
        self.assertFalse(normalized["needs_live_search"])
        self.assertTrue(normalized["unresolved_context_accepted_1u"])

    def test_ai_cannot_veto_valid_moneyline_for_harmless_missing_fields(self):
        candidate = {
            "market_type": "moneyline",
            "book_line": None,
            "commence_time": "2026-08-12T00:00:00Z",
            "kalshi_volume": 5000,
            "kalshi_spread": 1,
            "pricing_v2": {"ok": True},
        }
        review = {
            "enabled": True,
            "ok": True,
            "veto": True,
            "risk_level": "high",
            "reasons": [
                "kalshi_liquidity is 0",
                "book_line is null for this moneyline",
                "kalshi_market_start is null",
                "favorite_rebound.ok is false",
            ],
        }
        normalized = sports.normalize_ai_risk_review(candidate, review)
        self.assertFalse(normalized["veto"])
        self.assertEqual("invalid_nonrisk_field_interpretation", normalized["veto_ignored_reason"])

    def test_ai_real_injury_veto_survives_harmless_field_noise(self):
        candidate = {
            "market_type": "moneyline",
            "kalshi_volume": 5000,
            "kalshi_spread": 1,
        }
        review = {
            "enabled": True,
            "ok": True,
            "veto": True,
            "risk_level": "high",
            "reasons": ["kalshi_liquidity is 0", "starting pitcher was scratched"],
        }
        normalized = sports.normalize_ai_risk_review(candidate, review)
        self.assertTrue(normalized["veto"])

    def test_parse_grok_veto_handles_string_false(self):
        self.assertFalse(sports.parse_grok_veto('{"veto": "false"}'))
        self.assertTrue(sports.parse_grok_veto('{"veto": "true"}'))

    def test_high_risk_level_is_an_effective_veto(self):
        review = {
            "veto": False,
            "summary": {"risk_level": "high", "reasons": ["postponement risk"]},
        }
        self.assertTrue(sports.grok_review_requires_veto(review))

    def test_recent_veto_is_cached_for_same_ticker(self):
        portfolio = {}
        candidate = {"kalshi_ticker": "TEST-TICKER"}
        review = {
            "veto": True,
            "summary": {"risk_level": "high", "reasons": ["weather delay"]},
        }
        with patch.object(sports, "SPORTS_GROK_VETO_COOLDOWN_MINUTES", 60):
            sports.remember_grok_veto(portfolio, candidate, review)
            cached = sports.grok_veto_cache_entry(portfolio, candidate)
        self.assertIsNotNone(cached)
        self.assertIn("weather delay", cached["reason"])

    def test_expired_veto_is_pruned(self):
        candidate = {"kalshi_ticker": "TEST-TICKER"}
        portfolio = {
            "grok_veto_cache": {
                "TEST-TICKER": {
                    "vetoed_at": (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat(),
                    "reason": "old risk",
                }
            }
        }
        with patch.object(sports, "SPORTS_GROK_VETO_COOLDOWN_MINUTES", 60):
            cached = sports.grok_veto_cache_entry(portfolio, candidate)
        self.assertIsNone(cached)
        self.assertNotIn("TEST-TICKER", portfolio["grok_veto_cache"])

    def test_wall_clock_cannot_be_used_as_wnba_game_clock(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "minutes_since_start": 96.5,
            "game_state_features": {"score_available": True},
        }
        review = {
            "enabled": True,
            "ok": True,
            "veto": True,
            "risk_level": "high",
            "reasons": [
                "96.5 minutes since start is deep into or beyond regulation for a 40-min WNBA game"
            ],
        }
        normalized = sports.normalize_ai_timing_review(candidate, review)
        self.assertFalse(normalized["veto"])
        self.assertEqual(normalized["risk_level"], "medium")
        self.assertEqual(
            normalized["veto_ignored_reason"],
            "invalid_wall_clock_as_game_clock",
        )

    def test_authoritative_period_and_clock_are_not_overridden(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "minutes_since_start": 96.5,
            "game_state_features": {
                "period": 4,
                "clock_minutes": 0.2,
                "regulation_minutes_remaining": 0.2,
            },
        }
        review = {
            "enabled": True,
            "ok": True,
            "veto": True,
            "risk_level": "high",
            "reasons": ["late-game risk with 0:12 on the authoritative game clock"],
        }
        normalized = sports.normalize_ai_timing_review(candidate, review)
        self.assertTrue(normalized["veto"])
        self.assertNotIn("veto_ignored_reason", normalized)

    def test_missing_game_clock_requests_search_without_veto(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "minutes_since_start": 96.5,
            "game_state_features": {"score_available": True},
        }
        review = {
            "enabled": True,
            "ok": True,
            "veto": True,
            "risk_level": "high",
            "needs_live_search": True,
            "reasons": [
                "Timing uncertainty: no authoritative game clock or time remaining fields"
            ],
        }
        normalized = sports.normalize_ai_timing_review(candidate, review)
        self.assertFalse(normalized["veto"])
        self.assertEqual(normalized["risk_level"], "medium")
        self.assertTrue(normalized["needs_live_search"])
        self.assertEqual(
            normalized["veto_ignored_reason"],
            "missing_game_progress_requires_search_not_veto",
        )

    def test_tennis_does_not_require_clock_or_inning_context(self):
        candidate = {
            "sport_key": "tennis_atp_cincinnati",
            "minutes_since_start": 112.5,
            "game_started": True,
            "game_state_features": {},
        }
        review = {
            "enabled": True,
            "ok": True,
            "veto": True,
            "risk_level": "high",
            "reasons": [
                "Live match is 112.5 minutes elapsed and lacks live score/clock/inning context"
            ],
        }
        normalized = sports.normalize_ai_timing_review(candidate, review)
        self.assertFalse(normalized["veto"])
        self.assertTrue(normalized["needs_live_search"])
        self.assertEqual(normalized["risk_level"], "medium")
        self.assertIn(
            normalized["veto_ignored_reason"],
            {
                "invalid_wall_clock_as_game_clock",
                "tennis_requires_sport_specific_progress",
            },
        )
        payload = sports.ai_candidate_payload(candidate)
        self.assertIn("tennis has no game clock or innings", payload["timing_semantics"]["rule"].lower())
        self.assertIn("selected_sets_diff", payload["timing_semantics"]["authoritative_fields"])

    def test_every_supported_sport_has_correct_progress_semantics(self):
        cases = (
            ("americanfootball_nfl", "football", "quarter", "game clock", "inning"),
            ("basketball_nba", "basketball", "period", "game clock", "inning"),
            ("baseball_mlb", "baseball", "inning", "outs", "quarter"),
            ("icehockey_nhl", "hockey", "period", "game clock", "inning"),
            ("soccer_epl", "soccer", "match_minute", "stoppage", "inning"),
            ("tennis_atp_canadian_open", "tennis", "current_set", "server", "inning"),
            ("mma_mixed_martial_arts", "combat", "round", "round clock", "inning"),
            ("cricket_ipl", "cricket", "overs", "wickets", "quarter"),
        )
        for sport_key, family, field, context_term, irrelevant_term in cases:
            with self.subTest(sport_key=sport_key):
                payload = sports.ai_candidate_payload({"sport_key": sport_key, "game_state_features": {}})
                semantics = payload["timing_semantics"]
                self.assertEqual(semantics["sport_family"], family)
                self.assertIn(field, semantics["authoritative_fields"])
                self.assertIn(context_term, semantics["required_context"])
                self.assertIn(irrelevant_term, semantics["rule"].lower())

    def test_irrelevant_progress_requirements_do_not_veto_other_sports(self):
        cases = (
            ("americanfootball_nfl", "No live inning context was provided", "football_requires_sport_specific_progress"),
            ("baseball_mlb", "Missing game clock context", "baseball_requires_sport_specific_progress"),
            ("basketball_wnba", "Missing inning context", "basketball_requires_sport_specific_progress"),
            ("icehockey_nhl", "No quarter context is available", "hockey_requires_sport_specific_progress"),
            ("soccer_epl", "Missing inning context", "soccer_requires_sport_specific_progress"),
            ("mma_mixed_martial_arts", "Missing quarter context", "combat_requires_sport_specific_progress"),
        )
        for sport_key, reason, ignored_reason in cases:
            with self.subTest(sport_key=sport_key):
                normalized = sports.normalize_ai_timing_review(
                    {"sport_key": sport_key, "game_state_features": {}},
                    {
                        "enabled": True,
                        "ok": True,
                        "veto": True,
                        "risk_level": "high",
                        "reasons": [reason],
                    },
                )
                self.assertFalse(normalized["veto"])
                self.assertTrue(normalized["needs_live_search"])
                self.assertEqual(normalized["veto_ignored_reason"], ignored_reason)

    def test_authoritative_progress_fields_are_sport_specific(self):
        cases = (
            ("americanfootball_nfl", {"quarter": 4, "clock_minutes": 2.5}),
            ("basketball_nba", {"period": 4, "clock_minutes": 2.5}),
            ("baseball_mlb", {"inning": 8, "inning_half": "top"}),
            ("icehockey_nhl", {"period": 3, "clock_minutes": 2.5}),
            ("soccer_epl", {"half": 2, "match_minute": 83}),
            ("tennis_wta", {"current_set": 2, "game_score": "4-3", "server": "Player A"}),
            ("mma_mixed_martial_arts", {"round": 3, "round_clock_minutes": 1.5}),
            ("cricket_ipl", {"innings": 2, "overs": 16.4}),
        )
        for sport_key, features in cases:
            with self.subTest(sport_key=sport_key):
                self.assertTrue(
                    sports.candidate_has_authoritative_game_progress(
                        {"sport_key": sport_key, "game_state_features": features}
                    )
                )

    def test_tennis_hard_risk_veto_is_preserved(self):
        candidate = {
            "sport_key": "tennis_wta_montreal",
            "game_started": True,
            "game_state_features": {},
        }
        review = {
            "enabled": True,
            "ok": True,
            "veto": True,
            "risk_level": "high",
            "reasons": ["Player retired from the match; market odds are stale"],
        }
        normalized = sports.normalize_ai_timing_review(candidate, review)
        self.assertTrue(normalized["veto"])
        self.assertNotIn("veto_ignored_reason", normalized)

    def test_openai_clear_vote_does_not_call_local_or_grok(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "minutes_since_start": 96.5,
            "game_state_features": {},
        }
        openai_vote = {
            "enabled": True,
            "ok": True,
            "provider": "openai",
            "risk_level": "low",
            "veto": False,
            "needs_live_search": False,
            "reasons": [],
        }
        with patch.object(sports, "SPORTS_AI_CALLS", {"openai": 0, "local": 0}), patch.object(
            sports, "openai_vote", return_value=openai_vote
        ), patch.object(sports, "ollama_vote") as local, patch.object(
            sports, "grok_risk_check"
        ) as grok, patch.object(sports, "log_line"):
            review = sports.sports_ai_risk_check(candidate)
        self.assertEqual(review["effective_provider"], "openai")
        self.assertFalse(review["veto"])
        local.assert_not_called()
        grok.assert_not_called()

    def test_openai_high_risk_vote_is_primary_without_local_or_grok(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "game_state_features": {"inning": 8, "score_available": True},
        }
        openai_vote = {
            "enabled": True,
            "ok": True,
            "provider": "openai",
            "risk_level": "high",
            "veto": True,
            "needs_live_search": False,
            "reasons": ["authoritative live state contradicts the position"],
        }
        with patch.object(sports, "SPORTS_AI_CALLS", {"openai": 0, "local": 0}), patch.object(
            sports, "openai_vote", return_value=openai_vote
        ), patch.object(sports, "ollama_vote") as local, patch.object(
            sports, "grok_risk_check"
        ) as grok, patch.object(sports, "log_line"):
            review = sports.sports_ai_risk_check(candidate)
        self.assertEqual(review["effective_provider"], "openai")
        self.assertTrue(review["veto"])
        self.assertFalse(review["grok_escalated"])
        local.assert_not_called()
        grok.assert_not_called()

    def test_openai_live_search_request_uses_grok_without_local(self):
        candidate = {
            "sport_key": "baseball_mlb",
            "game_state_features": {"score_available": False},
        }
        openai_vote = {
            "enabled": True,
            "ok": True,
            "provider": "openai",
            "risk_level": "medium",
            "veto": False,
            "needs_live_search": True,
            "reasons": ["authoritative live state is unavailable"],
        }
        grok_vote = {
            "enabled": True,
            "ok": True,
            "provider": "grok",
            "risk_level": "low",
            "veto": False,
            "needs_live_search": False,
            "reasons": [],
        }
        with patch.object(sports, "SPORTS_AI_CALLS", {"openai": 0, "local": 0}), patch.object(
            sports, "openai_vote", return_value=openai_vote
        ), patch.object(sports, "ollama_vote") as local, patch.object(
            sports, "grok_risk_check", return_value=grok_vote
        ) as grok, patch.object(sports, "log_line"):
            review = sports.sports_ai_risk_check(candidate)
        self.assertEqual(review["effective_provider"], "grok")
        self.assertEqual(review["grok_escalation_reason"], "openai_requested_live_search")
        local.assert_not_called()
        grok.assert_called_once()

    def test_openai_failure_uses_local_before_grok(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "minutes_since_start": 96.5,
            "game_state_features": {},
        }
        local_vote = {
            "enabled": True,
            "ok": True,
            "provider": "ollama",
            "risk_level": "low",
            "veto": False,
            "needs_live_search": False,
            "reasons": [],
        }
        with patch.object(sports, "SPORTS_AI_CALLS", {"openai": 0, "local": 0}), patch.object(
            sports,
            "openai_vote",
            return_value={"enabled": True, "ok": False, "provider": "openai", "error": "timeout"},
        ), patch.object(sports, "ollama_vote", return_value=local_vote), patch.object(
            sports, "grok_risk_check"
        ) as grok, patch.object(sports, "log_line"):
            review = sports.sports_ai_risk_check(candidate)
        self.assertEqual(review["effective_provider"], "ollama")
        self.assertFalse(review["veto"])
        grok.assert_not_called()

    def test_openai_and_ollama_failure_use_grok_fallback(self):
        candidate = {
            "sport_key": "basketball_wnba",
            "game_state_features": {},
        }
        grok_vote = {
            "enabled": True,
            "ok": True,
            "provider": "grok",
            "risk_level": "low",
            "veto": False,
            "needs_live_search": False,
            "reasons": [],
        }
        with patch.object(sports, "SPORTS_AI_CALLS", {"openai": 0, "local": 0}), patch.object(
            sports,
            "openai_vote",
            return_value={"enabled": True, "ok": False, "provider": "openai", "error": "timeout"},
        ), patch.object(
            sports,
            "ollama_vote",
            return_value={"enabled": True, "ok": False, "provider": "ollama", "error": "timeout"},
        ), patch.object(sports, "grok_risk_check", return_value=grok_vote), patch.object(
            sports, "log_line"
        ):
            review = sports.sports_ai_risk_check(candidate)
        self.assertEqual(review["effective_provider"], "grok")
        self.assertEqual(review["grok_escalation_reason"], "openai_and_ollama_unresolved")


if __name__ == "__main__":
    unittest.main()
