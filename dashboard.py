import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dashboard_controls import expand_controls
from ai_voting import ollama_health
from kalshi_common import KALSHI_BASE_URL, KALSHI_ORDER_PATH, get_json, kalshi_event_order_body, kalshi_private_request, log_line, market_prices
from sports_aibetpicks import dashboard_summary as aibetpicks_summary
from sports_modest_recovery import summary as modest_recovery_summary
from sports_unit_accounting import filled_units
from aibetpicks_accounting import local_analytics as aibetpicks_analytics, is_aibetpicks
from itf_shadow_accounting import dashboard_summary as itf_shadow_summary
from sports_itf_followups import dashboard_summary as itf_followups_summary
from sports_itf_price_quality import dashboard_summary as itf_price_quality_summary
from storage_maintenance import read_storage_health, run_maintenance
from crypto_scan_intelligence import intelligence_history
from crypto_funding import summary as crypto_funding_summary
from shared_cash_pool import display_summary as shared_cash_summary
from process_supervision import native_process, worker_status
from crypto_patient_dashboard import (
    installed as patient_code_installed,
    load_portfolio as load_patient_portfolio,
    set_control as set_patient_control,
    summary as patient_control_summary,
)

try:
    from shared_bankroll import build_shared_bankroll_state
except Exception:
    build_shared_bankroll_state = None

HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8765"))
CRYPTO_SETTINGS_LOCK = threading.RLock()
CRYPTO_CANDIDATE_HISTORY_LOCK = threading.RLock()
CRYPTO_CANDIDATE_HISTORY_CACHE = {
    "path": "",
    "identity": None,
    "offset": 0,
    "fragment": b"",
    "rows": [],
}
SPORTS_CANDIDATE_HISTORY_LOCK = threading.RLock()
SPORTS_CANDIDATE_HISTORY_CACHE = {
    "path": "",
    "identity": None,
    "offset": 0,
    "fragment": b"",
    "rows": [],
    "latest_by_ticker": {},
}


def _first_sunday_on_or_after(value):
    days_to_go = 6 - value.weekday()
    return value if days_to_go == 0 else value + timedelta(days=days_to_go)


class _CentralTimeZone(tzinfo):
    """America/Chicago fallback for Windows Python installs without tzdata."""

    standard_offset = timedelta(hours=-6)

    @staticmethod
    def _dst_range(year):
        # U.S. rules in effect since 2007: second Sunday in March to first Sunday in November.
        start = _first_sunday_on_or_after(datetime(year, 3, 8, 2))
        end = _first_sunday_on_or_after(datetime(year, 11, 1, 2))
        return start, end

    def utcoffset(self, value):
        return self.standard_offset + self.dst(value)

    def dst(self, value):
        if value is None:
            return timedelta(0)
        start, end = self._dst_range(value.year)
        naive = value.replace(tzinfo=None)
        return timedelta(hours=1) if start <= naive < end else timedelta(0)

    def tzname(self, value):
        return "CDT" if self.dst(value) else "CST"

    def fromutc(self, value):
        if value.tzinfo is not self:
            raise ValueError("fromutc requires the Central timezone instance")
        start, end = self._dst_range(value.year)
        standard_time = value.replace(tzinfo=None) + self.standard_offset
        daylight_time = standard_time + timedelta(hours=1)
        if end <= daylight_time < end + timedelta(hours=1):
            return standard_time.replace(tzinfo=self, fold=1)
        if standard_time < start <= daylight_time:
            return daylight_time.replace(tzinfo=self)
        if start <= standard_time < end - timedelta(hours=1):
            return daylight_time.replace(tzinfo=self)
        return standard_time.replace(tzinfo=self)


try:
    CENTRAL_TZ = ZoneInfo("America/Chicago")
except ZoneInfoNotFoundError:
    CENTRAL_TZ = _CentralTimeZone()
SPORTS_PORTFOLIO_FILE = Path("sports_paper_portfolio.json")
SPORTS_REPORT_FILE = Path("sports_paper_report.json")
BOT_PICKS_REPORT_FILE = Path("bot_picks_report.json")
SPORTS_LOG_FILE = Path("sports_paper_log.txt")
SPORTS_ODDS_BUDGET_FILE = Path("sports_odds_budget.json")
SPORTS_WATCHLIST_FILE = Path("sports_watchlist.json")
SPORTS_LIVE_RECONCILIATION_FILE = Path("sports_live_reconciliation.json")
SPORTS_LIVE_AUDIT_FILE = Path("sports_live_audit.json")
SPORTS_PHASE_TWO_STATE_FILE = Path("sports_phase_two_state.json")
CRYPTO_PAPER_PORTFOLIO_FILE = Path("crypto_paper_portfolio.json")
CRYPTO_PAPER_REPORT_FILE = Path("crypto_paper_report.json")
CRYPTO_PAPER_LOG_FILE = Path("crypto_paper_log.txt")
CRYPTO_LIVE_PORTFOLIO_FILE = Path("crypto_live_portfolio.json")
DAILY_RESET_FILE = Path("daily_pnl_reset.json")
CRYPTO_LIVE_REPORT_FILE = Path("crypto_live_report.json")
CRYPTO_LIVE_LOG_FILE = Path("crypto_live_log.txt")
CRYPTO_SETTINGS_FILE = Path("crypto_settings.json")
CRYPTO_PAPER_EVENTS_FILE = Path("crypto_paper_events.jsonl")
CRYPTO_PERPS_SHADOW_PORTFOLIO_FILE = Path("crypto_perps_shadow_portfolio.json")
CRYPTO_PERPS_SHADOW_REPORT_FILE = Path("crypto_perps_shadow_report.json")
CRYPTO_PERPS_SHADOW_LOG_FILE = Path("crypto_perps_shadow_log.txt")
CRYPTO_LIVE_EVENTS_FILE = Path("crypto_live_events.jsonl")
CRYPTO_SCAN_INTELLIGENCE_FILE = Path("crypto_scan_intelligence.jsonl")
CRYPTO_SETTLEMENT_LAG_SHADOW_FILE = Path("crypto_settlement_lag_shadow.json")
CRYPTO_SHADOW_EXPANSION_REPORT_FILE = Path("crypto_shadow_expansion_report.json")
CRYPTO_SHADOW_SIZING_REPORT_FILE = Path("crypto_shadow_sizing_report.json")
CRYPTO_BTC_RANDOM_REPORT_FILE = Path("crypto_btc_random_shadow_report.json")
CRYPTO_BTC_VALUE_REPORT_FILE = Path("crypto_btc_value_shadow_report.json")
ARCHIVE_DIR = Path("archives")
SETTINGS_FILE = Path("bot_settings.json")
PROCESS_FILE = Path("bot_processes.json")

SENSITIVE_KEYS = {
    "ODDS_API_KEY",
    "XAI_API_KEY",
    "SPORTS_OPENAI_API_KEY",
    "SPORTRADAR_API_KEY",
    "SPORTS_GAME_ODDS_API_KEY",
    "KALSHI_API_KEY",
    "KALSHI_API_SECRET",
    "POLY_PRIVATE_KEY",
    "POLY_API_SECRET",
}
CRYPTO_SENSITIVE_KEYS = {
    "CRYPTO_GROK_API_KEY",
    "CRYPTO_OPENAI_API_KEY",
    "CRYPTO_NEWS_API_KEY",
    "CRYPTO_CRYPTOPANIC_API_KEY",
}
DEFAULT_SETTINGS = {
    "SPORTS_EXECUTION_MODE": "paper",
    "ALLOW_LIVE_TRADING": "false",
    "SHARED_BANKROLL_ENABLED": "true",
    "SHARED_BANKROLL_RESERVATIONS_ENABLED": "true",
    "SHARED_RESERVATION_TTL_SECONDS": "120",
    "SHARED_SYSTEM_DAILY_TARGET_PCT": "0.034",
    "SHARED_SYSTEM_DAILY_TARGET_MIN": "20",
    "SHARED_SYSTEM_DAILY_TARGET_MAX": "50",
    "SHARED_SYSTEM_DAILY_LOSS_PCT": "0.08",
    "SHARED_SYSTEM_DAILY_LOSS_CAP": "75",
    "SHARED_SYSTEM_MAX_OPEN_EXPOSURE_PCT": "0",
    "SHARED_SYSTEM_MAX_OPEN_EXPOSURE_CAP": "0",
    "SHARED_SPORTS_MAX_OPEN_EXPOSURE_PCT": "0",
    "SHARED_SPORTS_MAX_OPEN_EXPOSURE_CAP": "0",
    "SHARED_SPORTS_MAX_STAKE_PCT": "0.12",
    "SHARED_SPORTS_MAX_STAKE_CAP": "75",
    "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_PCT": "0.10",
    "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_CAP": "400",
    "SHARED_CRYPTO_MAX_STAKE_PCT": "0.015",
    "SHARED_CRYPTO_MAX_STAKE_CAP": "7.50",
    "SHARED_CRYPTO_ISOLATED_RISK_ENABLED": "true",
    "SHARED_CRYPTO_DAILY_LOSS_PCT": "0.05",
    "SHARED_CRYPTO_DAILY_LOSS_CAP": "0",
    "SHARED_PAUSE_CRYPTO_ON_SYSTEM_TARGET": "true",
    "SHARED_PAUSE_SPORTS_ON_SYSTEM_TARGET": "false",
    "SHARED_CRYPTO_LOSSES_TRIGGER_SPORTS_RECOVERY": "false",
    "SHARED_COUNT_CRYPTO_LIVE_PNL_IN_SYSTEM_TARGET": "true",
    "SPORTS_STARTING_BALANCE": "300",
    "SPORTS_SCAN_INTERVAL_MINUTES": "1",
    "SPORTS_LIVE_CAMPAIGN_BOT_COUNT": "1",
    "SPORTS_LIVE_CAMPAIGN_BOT_1_CYCLE_PROFIT": "2",
    "SPORTS_LIVE_CAMPAIGN_BOT_1_CYCLE_COUNT": "5",
    "SPORTS_LIVE_CAMPAIGN_BOT_2_CYCLE_PROFIT": "2",
    "SPORTS_LIVE_CAMPAIGN_BOT_2_CYCLE_COUNT": "5",
    "SPORTS_LIVE_CAMPAIGN_BOT_3_CYCLE_PROFIT": "2",
    "SPORTS_LIVE_CAMPAIGN_BOT_3_CYCLE_COUNT": "5",
    "SPORTS_LIVE_CAMPAIGN_BOT_4_CYCLE_PROFIT": "2",
    "SPORTS_LIVE_CAMPAIGN_BOT_4_CYCLE_COUNT": "5",
    "SPORTS_LIVE_CAMPAIGN_BOT_5_CYCLE_PROFIT": "2",
    "SPORTS_LIVE_CAMPAIGN_BOT_5_CYCLE_COUNT": "5",
    "SPORTS_LIVE_CAMPAIGN_SAME_GAME_MAX_POSITIONS": "2",
    "SPORTS_LIVE_CAMPAIGN_SAME_DIRECTION_MAX_POSITIONS": "1",
    "SPORTS_LIVE_CAMPAIGN_SAME_GAME_RISK_CAP": "0",
    "SPORTS_LIVE_CAMPAIGN_SAME_GAME_RISK_PCT": "0.10",
    "SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_CONSERVATIVE_EDGE": "3",
    "SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_CONFIDENCE": "85",
    "SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_PRO_SCORE": "95",
    "SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_FINAL_SCORE": "95",
    "SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_BOOK_FAMILIES": "2",
    "SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_SHARP_BOOKS": "1",
    "SPORTS_LIVE_CAMPAIGN_OPPOSITE_HEDGE_ENABLED": "true",
    "SPORTS_LIVE_CAMPAIGN_HEDGE_MIN_WORST_CASE_PROFIT": "0",
    "SPORTS_LIVE_CAMPAIGN_MAX_OPEN": "12",
    "SPORTS_LIVE_CAMPAIGN_DAILY_LOSS_CAP": "0",
    "SPORTS_LIVE_CAMPAIGN_ASSUMED_LOSS_ENABLED": "false",
    "SPORTS_LIVE_CAMPAIGN_PARTIAL_RECOVERY_ENABLED": "false",
    "SPORTS_LIVE_CAMPAIGN_MARKET_TYPES": "moneyline,spread,total",
    "SPORTS_LIVE_CAMPAIGN_SPREAD_MIN_CONSERVATIVE_EDGE": "0",
    "SPORTS_LIVE_CAMPAIGN_MID_LIVE_SPREAD_MIN_CONSERVATIVE_EDGE": "0",
    "SPORTS_LIVE_CAMPAIGN_TOTAL_MIN_TIER": "qualified",
    "SPORTS_LIVE_CAMPAIGN_TOTAL_MIN_CONSERVATIVE_EDGE": "0",
    "SPORTS_LIVE_CAMPAIGN_TOTAL_LINE_TOLERANCE": "0.01",
    "SPORTS_LIVE_CAMPAIGN_TOTAL_MIN_BOOK_FAMILIES": "2",
    "SPORTS_LIVE_CAMPAIGN_TOTAL_MIN_SHARP_BOOKS": "1",
    "SPORTS_MLB_TOTAL_MIN_PRICE_CENTS": "35",
    "SPORTS_MLB_TOTAL_MAX_PRICE_CENTS": "60",
    "SPORTS_MLB_TOTAL_MIN_EDGE": "2.5",
    "SPORTS_MLB_TOTAL_MIN_CONFIDENCE": "85",
    "SPORTS_MLB_TOTAL_MIN_PRO_SCORE": "95",
    "SPORTS_MLB_TOTAL_MIN_FINAL_SCORE": "90",
    "SPORTS_MLB_TOTAL_MIN_BOOK_FAMILIES": "4",
    "SPORTS_MLB_TOTAL_MIN_SHARP_BOOKS": "2",
    "SPORTS_MLB_TOTAL_MAX_UNITS": "2",
    "SPORTS_UNIT_STAKING_ENABLED": "true",
    "SPORTS_UNIT_SIZE_PCT": "1",
    "SPORTS_UNIT_MAX_PER_MARKET": "5",
    "SPORTS_UNIT_INCREMENT": "0.5",
    "SPORTS_AIBETPICKS_ENABLED": "true",
    "SPORTS_MODEST_RECOVERY_ENABLED": "false",
    "SPORTS_GAME_ODDS_ENABLED": "false",
    "SPORTS_GAME_ODDS_API_KEY": "",
    "SPORTS_GAME_ODDS_MAX_OPEN": "12",
    "SPORTS_GAME_ODDS_MAX_UNITS": "5",
    "SPORTS_GAME_ODDS_UNIT_SIZE": "20",
    "SPORTS_GAME_ODDS_PREGAME_MIN_EDGE": "2",
    "SPORTS_GAME_ODDS_LIVE_MIN_EDGE": "3",
    "SPORTS_GAME_ODDS_MIN_CONFIDENCE": "70",
    "SPORTS_GAME_ODDS_MIN_PRO_SCORE": "85",
    "SPORTS_GAME_ODDS_MIN_FINAL_SCORE": "85",
    "SPORTS_GAME_ODDS_MIN_BOOK_FAMILIES": "2",
    "SPORTS_GAME_ODDS_MAIN_LIVE_MIN_EDGE": "0",
    "SPORTS_GAME_ODDS_MAIN_PREGAME_MIN_EDGE": "-0.5",
    "SPORTS_GAME_ODDS_MAIN_MIN_BOOK_FAMILIES": "2",
    "SPORTS_MLB_MONEYLINE_MAX_UNITS": "1",
    "SPORTS_LIVE_MISSING_STATE_MAX_UNITS": "1",
    "SPORTS_AI_MEDIUM_RISK_MAX_UNITS": "2",
    "SPORTS_AI_UNRESOLVED_MAX_UNITS": "1",
    "SPORTS_AI_OVERRIDE_MAX_UNITS": "1",
    "SPORTS_POST_AI_ODDS_REVALIDATION_ENABLED": "true",
    "SPORTS_POST_AI_ODDS_REVALIDATION_MIN_RAW_UNITS": "2",
    "SPORTS_POST_AI_EDGE_STABILITY_SECONDS": "15",
    "SPORTS_POST_AI_REVALIDATION_MAX_CALLS_PER_SCAN": "5",
    "SPORTS_POST_AI_REVALIDATION_FAILURE_MAX_UNITS": "1",
    "SPORTS_POST_AI_LIVE_MIN_RAW_UNITS": "1",
    "SPORTS_POST_AI_EDGE_TOLERANCE_PP": "0.5",
    "SPORTS_POST_AI_MAX_ADVERSE_PRICE_MOVE_CENTS": "3",
    "SPORTS_REQUIRE_AUTHORITATIVE_LIVE_DERIVATIVE_STATE": "true",
    "SPORTS_ALLOW_SCORE_ONLY_LIVE_DERIVATIVES_1U": "true",
    "SPORTS_NEGATIVE_CLV_FLAG_MIN_BETS": "20",
    "SPORTS_NEGATIVE_CLV_FLAG_THRESHOLD_CENTS": "-1",
    "SPORTS_LOW_EDGE_CLV_AUTO_TIGHTEN_ENABLED": "true",
    "SPORTS_LOW_EDGE_CLV_MIN_SAMPLES": "12",
    "SPORTS_LOW_EDGE_CLV_THRESHOLD_CENTS": "-1",
    "SPORTS_LOW_EDGE_CLV_RESTORED_MIN_EDGE": "2",
    "SPORTS_HOT_RECHECK_ENABLED": "true",
    "SPORTS_HOT_RECHECK_INTERVAL_SECONDS": "15",
    "SPORTS_HOT_RECHECK_MAX_BURST_SCANS": "5",
    "SPORTS_HOT_RECHECK_EDGE_WINDOW_PP": "1.5",
    "SPORTS_HOT_RECHECK_MAX_CANDIDATES": "12",
    "SPORTS_AI_DETERMINISTIC_BYPASS_ENABLED": "true",
    "SPORTS_AI_REVIEW_CACHE_LIVE_SECONDS": "30",
    "SPORTS_AI_REVIEW_CACHE_PREGAME_SECONDS": "300",
    "SPORTS_AI_SKIP_SEARCH_WITH_AUTHORITATIVE_STATE": "true",
    "SPORTS_UNIT_1_MIN_EDGE": "0",
    "SPORTS_UNIT_1_MIN_CONFIDENCE": "70",
    "SPORTS_UNIT_1_MIN_PRO_SCORE": "85",
    "SPORTS_UNIT_1_MIN_FINAL_SCORE": "85",
    "SPORTS_UNIT_1_MIN_BOOK_FAMILIES": "2",
    "SPORTS_UNIT_2_MIN_EDGE": "1",
    "SPORTS_UNIT_2_MIN_CONFIDENCE": "76",
    "SPORTS_UNIT_2_MIN_PRO_SCORE": "90",
    "SPORTS_UNIT_2_MIN_FINAL_SCORE": "90",
    "SPORTS_UNIT_2_MIN_BOOK_FAMILIES": "2",
    "SPORTS_UNIT_3_MIN_EDGE": "2",
    "SPORTS_UNIT_3_MIN_CONFIDENCE": "82",
    "SPORTS_UNIT_3_MIN_PRO_SCORE": "95",
    "SPORTS_UNIT_3_MIN_FINAL_SCORE": "94",
    "SPORTS_UNIT_3_MIN_BOOK_FAMILIES": "2",
    "SPORTS_UNIT_4_MIN_EDGE": "4",
    "SPORTS_UNIT_4_MIN_CONFIDENCE": "88",
    "SPORTS_UNIT_4_MIN_PRO_SCORE": "100",
    "SPORTS_UNIT_4_MIN_FINAL_SCORE": "97",
    "SPORTS_UNIT_4_MIN_BOOK_FAMILIES": "2",
    "SPORTS_UNIT_5_MIN_EDGE": "6",
    "SPORTS_UNIT_5_MIN_CONFIDENCE": "92",
    "SPORTS_UNIT_5_MIN_PRO_SCORE": "105",
    "SPORTS_UNIT_5_MIN_FINAL_SCORE": "100",
    "SPORTS_UNIT_5_MIN_BOOK_FAMILIES": "2",
    "SPORTS_PRICING_V2_ENABLED": "true",
    "SPORTS_PRICING_V2_ENFORCEMENT": "active",
    "SPORTS_PRICING_V2_DEVIG_METHOD": "power",
    "SPORTS_PRICING_V2_DEFAULT_BOOK_WEIGHT": "0.25",
    "SPORTS_PRICING_V2_PRIOR_STRENGTH": "50",
    "SPORTS_PRICING_V2_MIN_BOOK_FAMILIES": "2",
    "SPORTS_PRICING_V2_BOOK_HALF_LIFE_MINUTES": "6",
    "SPORTS_PRICING_V2_MAX_BOOK_AGE_MINUTES": "6",
    "SPORTS_PRICING_V2_MIN_UNCERTAINTY_PP": "2",
    "SPORTS_PRICING_V2_REQUIRE_CONSERVATIVE_EDGE": "true",
    "SPORTS_KALSHI_STREAM_ENABLED": "true",
    "SPORTS_KALSHI_STREAM_REQUIRE_FOR_LIVE": "true",
    "SPORTS_KALSHI_STREAM_MAX_AGE_SECONDS": "8",
    "SPORTS_KALSHI_STREAM_WARMUP_SECONDS": "1.5",
    "SPORTS_KALSHI_STREAM_MAX_TICKERS": "500",
    "SPORTS_DECISION_ANALYTICS_ENABLED": "true",
    "SPORTS_LIVE_DATA_PROVIDER": "public_scoreboards",
    "SPORTRADAR_API_KEY": "",
    "SPORTS_SPORTRADAR_ACCESS_LEVEL": "trial",
    "SPORTS_SPORTRADAR_VERSION": "v8",
    "SPORTS_SPORTRADAR_FAILURE_THRESHOLD": "3",
    "SPORTS_SPORTRADAR_CIRCUIT_COOLDOWN_SECONDS": "900",
    "SPORTS_LIVE_DATA_MAX_GAMES_PER_SCAN": "10",
    "SPORTS_PUBLIC_STATE_CACHE_SECONDS": "20",
    "SPORTS_PUBLIC_STATE_FAILURE_THRESHOLD": "3",
    "SPORTS_PUBLIC_STATE_CIRCUIT_COOLDOWN_SECONDS": "300",
    "SPORTS_GAME_STATE_MODEL_ENABLED": "true",
    "SPORTS_NO_MATCH_BACKOFF_ENABLED": "true",
    "SPORTS_NO_MATCH_BACKOFF_MINUTES": "30",
    "SPORTS_QUIET_HOURS_ENABLED": "true",
    "SPORTS_QUIET_START_HOUR": "21",
    "SPORTS_QUIET_END_HOUR": "9",
    "SPORTS_ODDS_CACHE_MAX_HOURS": "0.25",
    "SPORTS_ODDS_PRIMARY_REGION": "us,us2,eu",
    "SPORTS_ODDS_SECONDARY_REGIONS": "uk",
    "SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED": "true",
    "SPORTS_ODDS_SECONDARY_MIN_BOOKMAKERS": "5",
    "SPORTS_ODDS_ACTIONABLE_EVENT_FILTER_ENABLED": "true",
    "SPORTS_ODDS_ALTERNATES_ON_DEMAND_ENABLED": "true",
    "SPORTS_ODDS_ALTERNATES_MAX_EVENTS_PER_SCAN": "20",
    "SPORTS_ODDS_ALTERNATES_LIVE_CACHE_MINUTES": "1",
    "SPORTS_ODDS_ALTERNATES_PREGAME_CACHE_MINUTES": "2",
    "SPORTS_ODDS_UNCHANGED_BACKOFF_MINUTES": "1",
    "SPORTS_ODDS_PROVIDER_RESET_DAY": "1",
    "SPORTS_ODDS_SKIP_INACTIVE": "true",
    "SPORTS_ALL_ACTIVE_ENABLED": "false",
    "SPORTS_ALL_ACTIVE_INCLUDE_OUTRIGHTS": "false",
    "SPORTS_ALL_ACTIVE_EXCLUDED_GROUPS": "Politics",
    "SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED": "true",
    "SPORTS_SCORES_ENABLED": "true",
    "SPORTS_SCORES_CACHE_MINUTES": "2",
    "SPORTS_SCORES_FALLBACK_CACHE_MINUTES": "5",
    "SPORTS_SCORES_DAYS_FROM": "1",
    "SPORTS_EARLY_RESULT_CHECK_ENABLED": "true",
    "SPORTS_EARLY_RESULT_SCORE_CACHE_MINUTES": "3",
    "SPORTS_EARLY_RESULT_MIN_STAKE": "4",
    "SPORTS_EARLY_RESULT_MIN_STAKE_PCT": "0.03",
    "SPORTS_EARLY_RESULT_MARKET_PRICE_CENTS": "3",
    "SPORTS_EARLY_RESULT_MIN_MINUTES": "45",
    "SPORTS_KEYS": "baseball_mlb,basketball_nba,basketball_wnba,basketball_ncaab,americanfootball_ncaaf,americanfootball_nfl_preseason,americanfootball_nfl,icehockey_nhl,tennis_atp,tennis_wta",
    "SPORTS_KALSHI_SERIES_PREFIXES": "KXMLB,KXNBA,KXWNBA,KXNCAAMB,KXNCAAB,KXNCAAF,KXCFB,KXNFL,KXNHL,KXATP,KXWTA",
    "SPORTS_KALSHI_MARKET_SERIES": "KXMLBGAME,KXMLBSPREAD,KXMLBTOTAL,KXNBAGAME,KXNBASPREAD,KXNBATOTAL,KXWNBAGAME,KXWNBASPREAD,KXWNBATOTAL,KXNCAAMBGAME,KXNCAAMBSPREAD,KXNCAAMBTOTAL,KXNCAABGAME,KXNCAABSPREAD,KXNCAABTOTAL,KXNCAAFGAME,KXNCAAFSPREAD,KXNCAAFTOTAL,KXCFBGAME,KXCFBSPREAD,KXCFBTOTAL,KXNFLGAME,KXNFLSPREAD,KXNFLTOTAL,KXNHLGAME,KXNHLSPREAD,KXNHLTOTAL,KXATPMATCH,KXATPGAME,KXATPCHALLENGERMATCH,KXCHALLENGERMATCH,KXATPDOUBLES,KXWTAMATCH,KXWTAGAME,KXWTACHALLENGERMATCH,KXWTADOUBLES",
    "SPORTS_KALSHI_DATE_MAX_DAYS": "0",
    "SPORTS_ODDS_DAILY_CREDIT_LIMIT": "0",
    "SPORTS_ODDS_WEEKLY_CREDIT_LIMIT": "0",
    "SPORTS_ODDS_MONTHLY_CREDIT_LIMIT": "4500000",
    "SPORTS_ODDS_HARD_CREDIT_LIMIT": "5000000",
    "SPORTS_ODDS_PACING_CREDIT_TARGET": "4750000",
    "SPORTS_ODDS_ADAPTIVE_PACING_ENABLED": "true",
    "SPORTS_ODDS_PACING_WINDOW_HOURS": "24",
    "SPORTS_ODDS_PACING_TRIGGER_RATIO": "1.05",
    "SPORTS_ODDS_PACING_MAX_MULTIPLIER": "6",
    "SPORTS_ODDS_PROVIDER_RESERVE_CREDITS": "250000",
    "SPORTS_ODDS_SHADOW_BUDGET_GUARD_ENABLED": "true",
    "SPORTS_OVERNIGHT_ODDS_ROUTING_ENABLED": "true",
    "SPORTS_OVERNIGHT_ODDS_START_HOUR": "0",
    "SPORTS_OVERNIGHT_ODDS_END_HOUR": "9",
    "SPORTS_OVERNIGHT_OVERSEAS_RESEARCH_CACHE_MINUTES": "60",
    "SPORTS_OVERNIGHT_PAUSE_CRICKET_SHADOW_ON_BUDGET_PRESSURE": "true",
    "SPORTS_OVERSEAS_DERIVATIVE_SHADOW_BUDGET_GUARD_ENABLED": "true",
    "SPORTS_LIVE_ODDS_CACHE_MAX_MINUTES": "0.75",
    "SPORTS_LIVE_TENNIS_REFRESH_SECONDS": "45",
    "SPORTS_MIN_ENTRY_PRICE_CENTS": "25",
    "SPORTS_PRICE_DISCIPLINE_ENABLED": "true",
    "SPORTS_PLUS_PRICE_MAX_CENTS": "52.5",
    "SPORTS_FAVORITE_MAX_PRICE_CENTS": "65",
    "SPORTS_HEAVY_FAVORITE_MAX_PRICE_CENTS": "70",
    "SPORTS_LOW_PRICE_GUARD_ENABLED": "true",
    "SPORTS_LOW_PRICE_STRICT_MAX_CENTS": "25",
    "SPORTS_LOW_PRICE_FOCUS_MIN_CENTS": "35",
    "SPORTS_LOW_PRICE_FOCUS_MAX_CENTS": "55",
    "SPORTS_LOW_PRICE_MIN_EDGE": "8",
    "SPORTS_LOW_PRICE_MIN_CONFIDENCE": "82",
    "SPORTS_LOW_PRICE_MIN_PRO_SCORE": "100",
    "SPORTS_LOW_PRICE_MIN_FINAL_SCORE": "95",
    "SPORTS_MOONSHOT_MIN_ENTRY_CENTS": "20",
    "SPORTS_MOONSHOT_ELITE_MIN_EDGE": "18",
    "SPORTS_MOONSHOT_ELITE_MIN_CONFIDENCE": "95",
    "SPORTS_MOONSHOT_ELITE_MIN_PRO_SCORE": "115",
    "SPORTS_MOONSHOT_ELITE_MIN_FINAL_SCORE": "100",
    "SPORTS_FAVORITE_MIN_EDGE": "10",
    "SPORTS_FAVORITE_MIN_CONFIDENCE": "88",
    "SPORTS_FAVORITE_MIN_PRO_SCORE": "90",
    "SPORTS_FAVORITE_MIN_FINAL_SCORE": "90",
    "SPORTS_HEAVY_FAVORITE_MIN_EDGE": "16",
    "SPORTS_HEAVY_FAVORITE_MIN_CONFIDENCE": "95",
    "SPORTS_HEAVY_FAVORITE_MIN_PRO_SCORE": "96",
    "SPORTS_HEAVY_FAVORITE_MIN_FINAL_SCORE": "98",
    "SPORTS_FAVORITE_WATCH_ENABLED": "true",
    "SPORTS_FAVORITE_WATCH_BOOK_ODDS_MAX": "-160",
    "SPORTS_FAVORITE_WATCH_MIN_MODEL_PROB": "60",
    "SPORTS_FAVORITE_WATCH_ENTRY_MIN_CENTS": "40",
    "SPORTS_FAVORITE_WATCH_ENTRY_MAX_CENTS": "50",
    "SPORTS_FAVORITE_WATCH_MIN_CONFIDENCE": "82",
    "SPORTS_FAVORITE_WATCH_MIN_PRO_SCORE": "82",
    "SPORTS_FAVORITE_WATCH_MAX_MINUTES_AFTER_START": "180",
    "SPORTS_FAVORITE_WATCH_STAKE_MULTIPLIER": "2",
    "SPORTS_FAVORITE_WATCH_MAX_STAKE_PCT": "0.08",
    "SPORTS_FAVORITE_WATCH_EXPIRE_HOURS": "36",
    "SPORTS_FAVORITE_WATCH_REQUIRE_LIVE_SCORE": "true",
    "SPORTS_EDGE_THRESHOLD": "8",
    "SPORTS_PRO_MODE_ENABLED": "true",
    "SPORTS_PRO_MIN_EDGE": "3",
    "SPORTS_PRO_MIN_SCORE": "72",
    "SPORTS_PRO_MIN_CONFIDENCE": "68",
    "SPORTS_PRO_GROK_MIN_SCORE": "78",
    "SPORTS_FINAL_SCORE_ENABLED": "true",
    "SPORTS_FINAL_SCORE_MIN": "78",
    "SPORTS_FINAL_SCORE_LOW_EDGE_MIN": "2",
    "SPORTS_FINAL_SCORE_LOW_EDGE_MIN_SCORE": "86",
    "SPORTS_FINAL_SCORE_LOW_EDGE_MIN_CONFIDENCE": "72",
    "SPORTS_FINAL_SCORE_LOW_EDGE_MIN_PRO_SCORE": "84",
    "SPORTS_PHASE_TWO_ENABLED": "true",
    "SPORTS_PHASE_TWO_CONTROL_EDGE_BETS": "false",
    "SPORTS_PHASE_TWO_PAUSE_NORMAL_AFTER_TARGET": "false",
    "SPORTS_PHASE_TWO_DAILY_TARGET_PCT": "0.034",
    "SPORTS_PHASE_TWO_DAILY_TARGET_MIN": "1",
    "SPORTS_PHASE_TWO_DAILY_TARGET_MAX": "50",
    "SPORTS_PHASE_TWO_CARRYOVER_ENABLED": "true",
    "SPORTS_PHASE_TWO_CARRYOVER_SMALL_LOSS_PCT": "0.25",
    "SPORTS_PHASE_TWO_CARRYOVER_BIG_LOSS_PCT": "0.10",
    "SPORTS_PHASE_TWO_CARRYOVER_BIG_LOSS_THRESHOLD_PCT": "0.10",
    "SPORTS_PHASE_TWO_CARRYOVER_MAX_ADD": "10",
    "SPORTS_PHASE_TWO_STAND_DOWN_ON_DAILY_TARGET": "false",
    "SPORTS_PHASE_TWO_MAX_ATTEMPTS": "4",
    "SPORTS_PHASE_TWO_MAX_CYCLES": "4",
    "SPORTS_PHASE_TWO_CYCLE_DECAY": "0.50",
    "SPORTS_PHASE_TWO_MIN_CYCLE_TARGET": "0.25",
    "SPORTS_PHASE_TWO_MIN_EDGE": "2",
    "SPORTS_PHASE_TWO_MIN_CONFIDENCE": "72",
    "SPORTS_PHASE_TWO_MIN_PRO_SCORE": "84",
    "SPORTS_PHASE_TWO_MIN_FINAL_SCORE": "86",
    "SPORTS_PHASE_TWO_TOTAL_MIN_EDGE": "6",
    "SPORTS_PHASE_TWO_TOTAL_MIN_CONFIDENCE": "80",
    "SPORTS_PHASE_TWO_TOTAL_MIN_PRO_SCORE": "100",
    "SPORTS_PHASE_TWO_TOTAL_MIN_FINAL_SCORE": "95",
    "SPORTS_PHASE_TWO_BASE_STAKE_PCT": "0.025",
    "SPORTS_PHASE_TWO_BASE_STAKE_MIN": "0.50",
    "SPORTS_PHASE_TWO_MAX_STAKE_PCT": "0.35",
    "SPORTS_PHASE_TWO_WAIT_FOR_SETTLEMENT": "true",
    "SPORTS_PHASE_TWO_TIME_WINDOW_ENABLED": "true",
    "SPORTS_PHASE_TWO_IMMEDIATE_START_HOURS": "3",
    "SPORTS_PHASE_TWO_ELITE_LATE_EDGE": "999",
    "SPORTS_PHASE_TWO_ELITE_LATE_CONFIDENCE": "999",
    "SPORTS_PHASE_TWO_ELITE_LATE_PRO_SCORE": "999",
    "SPORTS_PHASE_TWO_ELITE_LATE_FINAL_SCORE": "999",
    "SPORTS_PHASE_TWO_MARKET_TYPES": "moneyline,total",
    "SPORTS_LIVE_ELITE_RETRY_ENABLED": "false",
    "SPORTS_LIVE_ELITE_RETRY_MAX_BUMP_CENTS": "2",
    "SPORTS_LIVE_ELITE_RETRY_MIN_EDGE": "8",
    "SPORTS_LIVE_ELITE_RETRY_MIN_CONFIDENCE": "85",
    "SPORTS_LIVE_ELITE_RETRY_MIN_PRO_SCORE": "100",
    "SPORTS_LIVE_ELITE_RETRY_MIN_FINAL_SCORE": "95",
    "SPORTS_LIVE_ELITE_RETRY_MIN_PRICE_CENTS": "25",
    "SPORTS_LIVE_ELITE_RETRY_MAX_PRICE_CENTS": "55",
    "SPORTS_FOK_RECHECK_ENABLED": "true",
    "SPORTS_FOK_RECHECK_ATTEMPTS": "4",
    "SPORTS_FOK_RECHECK_INTERVAL_SECONDS": "5",
    "SPORTS_FOK_RECHECK_MAX_WAIT_SECONDS": "30",
    "SPORTS_CLV_BOOK_IDENTITY_RETRY_MINUTES": "20",
    "SPORTS_CLV_BOOK_IDENTITY_MAX_ATTEMPTS": "12",
    "SPORTS_MIN_CONFIDENCE": "85",
    "SPORTS_EDGE_MARKET_TYPES": "moneyline,total,spread",
    "SPORTS_MAX_PAPER_BETS_PER_SCAN": "2",
    "SPORTS_MAX_STAKE_PCT": "0.025",
    "SPORTS_KELLY_FRACTION": "0.025",
    "SPORTS_SPREAD_STAKE_MULTIPLIER": "0.5",
    "SPORTS_SPREAD_MIN_CONFIDENCE": "75",
    "SPORTS_SPREAD_MIN_PRO_SCORE": "90",
    "SPORTS_SPREAD_MIN_FINAL_SCORE": "88",
    "SPORTS_SELECTIVE_STAKING_ENABLED": "true",
    "SPORTS_SELECTIVE_BASE_STAKE": "12",
    "SPORTS_SELECTIVE_BASE_STAKE_PCT": "0.12",
    "SPORTS_SELECTIVE_GAME_EXPOSURE_PCT": "0.10",
    "SPORTS_SELECTIVE_MIN_EDGE": "6",
    "SPORTS_SELECTIVE_MIN_CONFIDENCE": "75",
    "SPORTS_SELECTIVE_MIN_PRO_SCORE": "85",
    "SPORTS_FINAL_SMALL_EDGE_ENABLED": "true",
    "SPORTS_FINAL_SMALL_EDGE_MIN_EDGE": "2.5",
    "SPORTS_FINAL_SMALL_EDGE_MIN_CONFIDENCE": "72",
    "SPORTS_FINAL_SMALL_EDGE_MIN_PRO_SCORE": "100",
    "SPORTS_FINAL_SMALL_EDGE_MIN_FINAL_SCORE": "95",
    "SPORTS_SMALL_EDGE_SCALING_ENABLED": "true",
    "SPORTS_SMALL_EDGE_STRONG_MIN_EDGE": "5",
    "SPORTS_SMALL_EDGE_STRONG_MIN_CONFIDENCE": "78",
    "SPORTS_SMALL_EDGE_STRONG_MIN_PRO_SCORE": "105",
    "SPORTS_SMALL_EDGE_STRONG_MIN_FINAL_SCORE": "98",
    "SPORTS_SMALL_EDGE_STRONG_STAKE_PCT": "0.0015",
    "SPORTS_SMALL_EDGE_STRONG_MAX_STAKE": "3",
    "SPORTS_SMALL_EDGE_ELITE_MIN_EDGE": "8",
    "SPORTS_SMALL_EDGE_ELITE_MIN_CONFIDENCE": "84",
    "SPORTS_SMALL_EDGE_ELITE_MIN_PRO_SCORE": "112",
    "SPORTS_SMALL_EDGE_ELITE_MIN_FINAL_SCORE": "100",
    "SPORTS_SMALL_EDGE_ELITE_STAKE_PCT": "0.003",
    "SPORTS_SMALL_EDGE_ELITE_MAX_STAKE": "5",
    "SPORTS_SMALL_EDGE_PHASE_TWO_EXPOSURE_PCT": "0.01",
    "SPORTS_SELECTIVE_WAIT_FOR_SETTLEMENT": "true",
    "SPORTS_SELECTIVE_ELITE_BYPASS_CONFIDENCE": "95",
    "SPORTS_SELECTIVE_ELITE_BYPASS_EDGE": "15",
    "SPORTS_SELECTIVE_ELITE_BYPASS_PRO_SCORE": "110",
    "SPORTS_LOSS_STREAK_STAKING_ENABLED": "true",
    "SPORTS_LOSS_STREAK_MULTIPLIER": "2",
    "SPORTS_LOSS_STREAK_MAX_MULTIPLIER": "3",
    "SPORTS_LOSS_STREAK_MAX_STAKE_PCT": "0.35",
    "SPORTS_TOTAL_LIFELINE_ENABLED": "true",
    "SPORTS_TOTAL_LIFELINE_MAX_ADD_PCT": "0.03",
    "SPORTS_TOTAL_LIFELINE_MAX_OPEN_STAKE": "10",
    "SPORTS_TOTAL_LIFELINE_STAKE_MULTIPLIER": "1.00",
    "SPORTS_TOTAL_LIFELINE_MIN_EDGE": "8",
    "SPORTS_TOTAL_LIFELINE_MIN_CONFIDENCE": "84",
    "SPORTS_TOTAL_LIFELINE_MIN_PRO_SCORE": "105",
    "SPORTS_TOTAL_LIFELINE_MIN_FINAL_SCORE": "98",
    "SPORTS_RECOVERY_STAKING_ENABLED": "true",
    "SPORTS_RECOVERY_CARRYOVER_ENABLED": "true",
    "SPORTS_RECOVERY_HIGH_WATER_ENABLED": "false",
    "SPORTS_RECOVERY_ONLY_WHEN_ACTIVE": "true",
    "SPORTS_RECOVERY_DISABLE_WHEN_DAILY_GREEN": "true",
    "SPORTS_RECOVERY_PAUSE_FOR_PHASE_TWO": "true",
    "SPORTS_RECOVERY_PHASE_TWO_OFFSET_ENABLED": "true",
    "SPORTS_RECOVERY_MIN_DAILY_LOSS": "12",
    "SPORTS_RECOVERY_MIN_DAILY_LOSS_PCT": "0.10",
    "SPORTS_RECOVERY_MIN_LOSS_STREAK": "0",
    "SPORTS_RECOVERY_MIN_CONFIDENCE": "85",
    "SPORTS_RECOVERY_MIN_EDGE": "6",
    "SPORTS_RECOVERY_MIN_PRO_SCORE": "100",
    "SPORTS_RECOVERY_REQUIRE_EXTRA_QUALITY": "true",
    "SPORTS_RECOVERY_ALL_AVAILABLE_ENABLED": "false",
    "SPORTS_RECOVERY_TARGET_PROFIT_MULTIPLIER": "0.40",
    "SPORTS_RECOVERY_MAX_STAKE_PCT": "0.10",
    "SPORTS_RECOVERY_MAX_STAKE_TIERS": "500:0.10,1000:0.08,2500:0.06,inf:0.04",
    "SPORTS_RECOVERY_MAX_DAILY_LOSS_PCT": "0.35",
    "SPORTS_RECOVERY_MAX_SPORT_EXPOSURE_PCT": "0.30",
    "SPORTS_RECOVERY_MARKET_TYPES": "moneyline,total",
    "SPORTS_RECOVERY_BASKET_ENABLED": "true",
    "SPORTS_RECOVERY_BASKET_ONLY_WHEN_PHASE_TWO_PENDING": "true",
    "SPORTS_RECOVERY_BASKET_MAX_OPEN": "3",
    "SPORTS_RECOVERY_BASKET_MAX_TOTAL_STAKE_PCT": "0.12",
    "SPORTS_RECOVERY_BASKET_MAX_STAKE_PCT": "0.04",
    "SPORTS_RECOVERY_BASKET_MIN_STAKE": "0.50",
    "SPORTS_RECOVERY_BASKET_TARGET_PROFIT_MULTIPLIER": "0.60",
    "SPORTS_RECOVERY_BASKET_MIN_EDGE": "4",
    "SPORTS_RECOVERY_BASKET_MIN_CONFIDENCE": "80",
    "SPORTS_RECOVERY_BASKET_MIN_PRO_SCORE": "100",
    "SPORTS_RECOVERY_BASKET_MIN_FINAL_SCORE": "95",
    "SPORTS_RECOVERY_BASKET_MAX_SAME_GAME_OPEN": "1",
    "SPORTS_RECOVERY_BASKET_MARKET_TYPES": "moneyline,total",
    "SPORTS_EDGE_BOOST_CONFIDENCE": "97",
    "SPORTS_EDGE_BOOST_MIN_EDGE": "15",
    "SPORTS_EDGE_STAKE_MULTIPLIER": "1.25",
    "SPORTS_DAILY_PROFIT_TARGET_PCT": "0.034",
    "SPORTS_DAILY_PROFIT_TARGET_MIN": "20",
    "SPORTS_DAILY_PROFIT_TARGET_MAX": "50",
    "SPORTS_PROFIT_LOCK_START_MULTIPLE": "1",
    "SPORTS_PROFIT_LOCK_FULL_MULTIPLE": "2",
    "SPORTS_PROFIT_LOCK_STAKE_MULTIPLIER": "0.5",
    "SPORTS_PROFIT_LOCK_ELITE_MULTIPLIER": "0.25",
    "SPORTS_PROFIT_LOCK_MIN_CONFIDENCE": "95",
    "SPORTS_PROFIT_LOCK_MIN_EDGE": "15",
    "SPORTS_PROFIT_LOCK_MIN_PRO_SCORE": "105",
    "SPORTS_PROFIT_LOCK_MIN_STAKE": "5",
    "SPORTS_PROFIT_LOCK_MAX_STAKE": "15",
    "SPORTS_BOT_PICK_STAKE_PCT": "0.02",
    "SPORTS_MAX_GAME_EXPOSURE_PCT": "0.10",
    "SPORTS_MAX_SPORT_EXPOSURE_PCT": "0.60",
    "SPORTS_SMALL_EDGE_SAME_GAME_CAP_ENABLED": "true",
    "SPORTS_SMALL_EDGE_MAX_SAME_GAME_OPEN": "3",
    "SPORTS_SELECTIVE_MAX_SAME_GAME_OPEN": "2",
    "SPORTS_TRACK_MISSED_FILLS": "true",
    "SPORTS_MISSED_FILL_HISTORY_LIMIT": "500",
    "SPORTS_HEDGE_ENABLED": "true",
    "SPORTS_ALLOW_IN_GAME_EDGES": "true",
    "SPORTS_IN_GAME_GRACE_MINUTES": "5",
    "SPORTS_REQUIRE_LIVE_SCORE_FOR_IN_GAME": "true",
    "SPORTS_LIVE_GAME_BETTING_ENABLED": "true",
    "SPORTS_LIVE_GAME_MAX_ODDS_AGE_MINUTES": "8",
    "SPORTS_LIVE_GAME_MAX_MINUTES_AFTER_START": "240",
    "SPORTS_LIVE_GAME_MIN_EDGE": "0",
    "SPORTS_LIVE_GAME_MIN_CONFIDENCE": "72",
    "SPORTS_LIVE_GAME_MIN_PRO_SCORE": "80",
    "SPORTS_LIVE_GAME_NO_SCORE_GRACE_MINUTES": "90",
    "SPORTS_PREGAME_ENABLED": "true",
    "SPORTS_PREGAME_ALL_OPEN_ENABLED": "true",
    "SPORTS_PREGAME_MAX_MINUTES_BEFORE_START": "45",
    "SPORTS_PREGAME_FAST_POLL_MINUTES_BEFORE_START": "15",
    "SPORTS_PREGAME_ODDS_CACHE_MAX_MINUTES": "2",
    "SPORTS_PREGAME_FAST_ODDS_CACHE_MAX_MINUTES": "1",
    "SPORTS_PREGAME_FAR_POLL_MINUTES_BEFORE_START": "180",
    "SPORTS_PREGAME_FAR_ODDS_CACHE_MAX_MINUTES": "5",
    "SPORTS_PREGAME_MAX_OPEN": "12",
    "SPORTS_PROTECTED_CONFIDENCE": "0.75",
    "SPORTS_DIRECT_OPPOSITE_MIN_EDGE_ADVANTAGE": "999",
    "SPORTS_TRUE_HEDGE_MIN_PROFIT": "999",
    "SPORTS_AI_VOTING_ENABLED": "true",
    "SPORTS_LOCAL_AI_ENABLED": "true",
    "SPORTS_LOCAL_AI_URL": "http://127.0.0.1:11434",
    "SPORTS_LOCAL_AI_MODEL": "gpt-oss:20b",
    "SPORTS_OPENAI_ENABLED": "true",
    "SPORTS_OPENAI_API_KEY": "",
    "SPORTS_OPENAI_MODEL": "gpt-5.4-nano",
    "SPORTS_AI_MAX_CALLS_PER_RUN": "5",
    "SPORTS_AI_MAX_OUTPUT_TOKENS": "180",
    "SPORTS_GROK_FALLBACK_ENABLED": "true",
    "SPORTS_ENABLE_GROK": "true",
    "SPORTS_GROK_SEARCH_ENABLED": "true",
    "SPORTS_GROK_WEB_SEARCH_ENABLED": "true",
    "SPORTS_GROK_X_SEARCH_ENABLED": "true",
    "BOT_PICKS_GROK_SEARCH_ENABLED": "true",
    "BOT_PICKS_GROK_WEB_SEARCH_ENABLED": "true",
    "BOT_PICKS_GROK_X_SEARCH_ENABLED": "true",
    "BOT_PICKS_ENABLED": "false",
    "BOT_PICKS_ACTIVE_KEYS": "",
    "BOT_PICKS_PUBLIC_FADE_MIN_PCT": "95",
    "BOT_PICKS_MORNING_HOUR": "10",
    "BOT_PICKS_AFTERNOON_HOUR": "14",
    "BOT_PICKS_EVENING_HOUR": "18",
    "SPORTS_BOT_PICK_MIN_CONFIDENCE": "0.55",
    "SPORTS_BOT_PICK_REQUIRE_ACTIONABLE": "false",
    "SPORTS_BOT_PICK_ALLOW_TRACKING": "true",
    "SPORTS_BOT_PICK_ALWAYS_PAPER": "true",
    "SPORTS_BOT_PICK_MARKETLESS_PAPER": "true",
    "SPORTS_BOT_PICK_MAX_LINE_IMPROVEMENT": "2.5",
    "SPORTS_BOT_PICK_FIXED_STAKE": "0",
    "SPORTS_FADE_PUBLIC_BOT_STAKE": "10",
    "SPORTS_TRACKING_EXPIRE_HOURS": "8",
    "SPORTS_STALE_OPEN_HOURS": "36",
    "SPORTS_LIVE_ORDER_ENABLED": "false",
    "SPORTS_LIVE_TIME_IN_FORCE": "immediate_or_cancel",
    "SPORTS_LIVE_MAX_PRICE_CENTS": "67",
    "SPORTS_BET_MORE_MAX_PRICE_MOVE_CENTS": "20",
    "SPORTS_BET_MORE_ENABLED": "false",
    "SPORTS_LIVE_FALLBACK_TO_PAPER": "false",
    "SPORTS_LIVE_EDGE_ORDER_ENABLED": "false",
    "SPORTS_RECONCILE_LIVE_ON_SCAN": "true",
    "SPORTS_LIVE_AUDIT_ENABLED": "true",
    "SPORTS_LIVE_AUDIT_INTERVAL_MINUTES": "60",
    "SPORTS_LIVE_AUDIT_CASH_TOLERANCE": "0.25",
    "LIVE_MAX_STAKE": "0",
    "LIVE_MAX_STAKE_PCT": "0",
    "LIVE_MAX_STAKE_CAP": "0",
    "LIVE_MAX_DAILY_LOSS": "0",
    "LIVE_MAX_DAILY_LOSS_PCT": "0",
    "LIVE_MAX_DAILY_LOSS_CAP": "0",
    "LIVE_MAX_OPEN_EXPOSURE": "0",
    "LIVE_MAX_OPEN_EXPOSURE_PCT": "0",
    "LIVE_MAX_OPEN_EXPOSURE_CAP": "0",
    "LIVE_REQUIRE_CONFIRMATION": "true",
    "ODDS_API_KEY": "",
    "XAI_API_KEY": "",
    "XAI_MODEL": "grok-4.20-0309-non-reasoning",
    "XAI_SEARCH_MODEL": "grok-4.3",
    "KALSHI_API_KEY": "",
    "KALSHI_API_SECRET": "",
    "KALSHI_PRIVATE_KEY_PATH": "",
    "POLY_PRIVATE_KEY": "",
}
PROCESS_SPECS = {
    "sports": {"label": "Sports Bot Loop", "script": "start_sports_loop.ps1", "process_markers": ["start_sports_loop.ps1", "start_sports_hidden_loop.ps1", "sports_paper_bettor.py"], "env": {"SPORTS_RUN_LOOP": "true"}},
    "crypto": {"label": "Crypto Bot Loop", "script": "start_crypto_paper.ps1", "process_markers": ["start_crypto_paper.ps1", "crypto_paper_bettor.py"], "env": {"CRYPTO_RUN_LOOP": "true"}},
}


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    last_error = None
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise last_error


def load_settings():
    env_settings = {key: os.getenv(key, "") for key in DEFAULT_SETTINGS if os.getenv(key, "")}
    settings = {**DEFAULT_SETTINGS, **env_settings, **read_json(SETTINGS_FILE, {})}
    for key, value in settings.items():
        if value is None:
            settings[key] = ""
        else:
            settings[key] = str(value)
    return settings


def save_settings(new_values):
    current = load_settings()
    old_sports_mode = current.get("SPORTS_EXECUTION_MODE", "paper")
    for key, value in new_values.items():
        if key not in DEFAULT_SETTINGS and key not in SENSITIVE_KEYS:
            continue
        value = "" if value is None else str(value).strip()
        if key in SENSITIVE_KEYS and value in ("", "********"):
            continue
        current[key] = value
    new_sports_mode = current.get("SPORTS_EXECUTION_MODE", old_sports_mode)
    if new_sports_mode != old_sports_mode:
        switch_sports_mode_files(old_sports_mode, new_sports_mode)
    write_json(SETTINGS_FILE, current)
    return current


def archive_active_sports_files(mode):
    ARCHIVE_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for path in (SPORTS_PORTFOLIO_FILE, SPORTS_REPORT_FILE, SPORTS_LOG_FILE, SPORTS_EVENTS_FILE, BOT_PICKS_REPORT_FILE, SPORTS_LIVE_RECONCILIATION_FILE):
        if path.exists():
            shutil.copy2(path, ARCHIVE_DIR / f"{path.name}.{mode}_before_switch_{stamp}")
    return stamp


def restore_latest_sports_archive(mode):
    restored = []
    for path in (SPORTS_PORTFOLIO_FILE, SPORTS_REPORT_FILE, SPORTS_LOG_FILE, SPORTS_EVENTS_FILE, BOT_PICKS_REPORT_FILE, SPORTS_LIVE_RECONCILIATION_FILE):
        candidates = sorted(ARCHIVE_DIR.glob(f"{path.name}.{mode}_before_*"), key=lambda item: item.stat().st_mtime, reverse=True)
        if candidates:
            shutil.copy2(candidates[0], path)
            restored.append(path.name)
    return restored


def switch_sports_mode_files(old_mode, new_mode):
    old_mode = "live" if str(old_mode).lower() == "live" else "paper"
    new_mode = "live" if str(new_mode).lower() == "live" else "paper"
    stamp = archive_active_sports_files(old_mode)
    restored = restore_latest_sports_archive(new_mode)
    log_line(SPORTS_LOG_FILE, f"SPORTS MODE FILE SWITCH: {old_mode} -> {new_mode} archived={stamp} restored={','.join(restored) or 'none'}")


def masked_value(value):
    if not value:
        return ""
    return f"saved ({len(str(value))} chars)"


def public_settings():
    settings = load_settings()
    public = {}
    for key in DEFAULT_SETTINGS.keys() | SENSITIVE_KEYS:
        value = settings.get(key, "")
        public[key] = masked_value(value) if key in SENSITIVE_KEYS else value
    return public


def reveal_setting(key):
    if key not in SENSITIVE_KEYS:
        raise ValueError("not_a_sensitive_setting")
    return load_settings().get(key, "")


def load_processes():
    return read_json(PROCESS_FILE, {})


def save_processes(processes):
    write_json(PROCESS_FILE, processes)


def pid_running(pid):
    return native_process(pid).get("running") is True


def find_running_script_pid(script_names):
    for script_name in script_names if isinstance(script_names, (list, tuple)) else [script_names]:
        try:
            script = Path(script_name).name
            pattern = re.escape(script).replace("'", "''")
            ps = (
                "Get-CimInstance Win32_Process | "
                f"Where-Object {{ $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -match '{pattern}' }} | "
                "Select-Object -First 1 -ExpandProperty ProcessId"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            output = result.stdout.strip().splitlines()
            if output:
                return int(output[0])
        except Exception:
            continue
    return None


def process_status():
    processes = load_processes()
    status = {}
    for name, spec in PROCESS_SPECS.items():
        entry = processes.get(name, {})
        observed = worker_status(name)
        # The registry records launch requests; only the worker guard and OS
        # identity establish that the trading scanner is actually alive.
        if observed.get("reason") == "worker_guard_missing" and entry.get("running"):
            launcher = native_process(entry.get("pid"))
            if launcher.get("running") is not False:
                observed.update(running=None, status="starting", reason="waiting_for_worker_guard")
        status[name] = {
            "name": name,
            "label": spec["label"],
            "script": spec["script"],
            "launcher_pid": entry.get("pid"),
            **observed,
        }
    return status


def merged_process_env():
    env = os.environ.copy()
    env.update(load_settings())
    return env


def start_process(name):
    if name not in PROCESS_SPECS:
        raise ValueError("unknown_process")
    status = process_status().get(name, {})
    if status.get("running"):
        return status
    if status.get("running") is None:
        raise ValueError("Worker status is not verified; refusing a duplicate launch. " + str(status.get("reason") or ""))
    spec = PROCESS_SPECS[name]
    script = str(Path(spec["script"]).resolve())
    env = merged_process_env()
    env.update(spec.get("env", {}))
    control_log = open(f"{name}_control.log", "a", encoding="utf-8")
    control_log.write(f"\n[{datetime.now(CENTRAL_TZ).isoformat(timespec='seconds')}] starting {script}\n")
    control_log.flush()
    command = [sys.executable, script]
    if Path(script).suffix.lower() == ".ps1":
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
    proc = subprocess.Popen(
        command,
        cwd=str(Path.cwd()),
        env=env,
        stdout=control_log,
        stderr=control_log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    processes = load_processes()
    processes[name] = {
        "pid": proc.pid,
        "running": True,
        "started_at": datetime.now(CENTRAL_TZ).isoformat(timespec="seconds"),
        "script": PROCESS_SPECS[name]["script"],
    }
    save_processes(processes)
    return process_status()[name]


def stop_process(name):
    if name not in PROCESS_SPECS:
        raise ValueError("unknown_process")
    processes = load_processes()
    entry = processes.get(name, {})
    observed = worker_status(name)
    if observed.get("running") is None:
        raise ValueError("Worker identity is unverified; refusing to stop an unrelated process.")
    if observed.get("running"):
        # Companion research workers may share a launcher. Stop only this
        # verified scanner; killing its entire parent tree also kills research.
        subprocess.run(["taskkill", "/PID", str(observed["pid"]), "/F"], capture_output=True, text=True, timeout=10, check=True)
    entry["running"] = False
    entry["stopped_at"] = datetime.now(CENTRAL_TZ).isoformat(timespec="seconds")
    processes[name] = entry
    save_processes(processes)
    return process_status()[name]


def rerun_bot_picks_once():
    sports_status = process_status().get("sports", {})
    was_running = bool(sports_status.get("running"))
    if was_running:
        stop_process("sports")

    script = str(Path(PROCESS_SPECS["sports"]["script"]).resolve())
    env = merged_process_env()
    env.update({"SPORTS_RUN_LOOP": "false", "SPORTS_BOT_RERUN_ONCE": "true"})
    started_at = datetime.now(CENTRAL_TZ).isoformat(timespec="seconds")
    returncode = -1

    try:
        with open("sports_control.log", "a", encoding="utf-8") as control_log:
            control_log.write(f"\n[{started_at}] dashboard bot-pick rerun {script}\n")
            control_log.flush()
            try:
                result = subprocess.run(
                    [sys.executable, script],
                    cwd=str(Path.cwd()),
                    env=env,
                    stdout=control_log,
                    stderr=control_log,
                    timeout=240,
                    check=False,
                )
                returncode = result.returncode
            except subprocess.TimeoutExpired:
                control_log.write("Bot-pick rerun timed out after 240 seconds.\n")
                returncode = 124
    finally:
        restarted = start_process("sports") if was_running else process_status().get("sports", {})

    return {
        "started_at": started_at,
        "returncode": returncode,
        "restarted": was_running,
        "process": restarted,
        "logs": read_tail(SPORTS_LOG_FILE, 30),
    }


def reset_odds_state():
    removed = []
    for path in (Path("sports_odds_pause.json"), Path("sports_odds_usage.json")):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def read_tail(path: Path, limit: int = 80):
    return read_lines(path, limit)


def read_lines(path: Path, limit: int = 6000):
    """Read only the tail needed by the dashboard, even for multi-GB logs."""
    try:
        if not limit:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            offset = stream.tell()
            chunks = []
            newlines = 0
            while offset > 0 and newlines <= limit:
                size = min(offset, 65536)
                offset -= size
                stream.seek(offset)
                chunk = stream.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
        return b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()[-limit:]
    except FileNotFoundError:
        return []


def top_counter_items(counter, limit=12):
    return [{"label": key, "count": value} for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def build_log_analytics(path: Path, kind: str):
    lines = read_lines(path, 6000)
    categories = {
        "scan_started": 0,
        "scan_complete": 0,
        "api_ok": 0,
        "api_error": 0,
        "kalshi_fetch_failed": 0,
        "paper_bet": 0,
        "settled_win": 0,
        "settled_loss": 0,
        "no_bets": 0,
        "top_candidate": 0,
        "diagnostics": 0,
        "duplicate": 0,
        "unmatched": 0,
        "expired_tracking": 0,
        "cached_odds": 0,
        "grok_veto": 0,
        "live_order_not_filled": 0,
        "missed_fill_tracked": 0,
        "missed_fill_would_win": 0,
        "missed_fill_would_loss": 0,
        "same_game_small_edge_cap": 0,
    }
    skip_reasons = {}
    diagnostics = {}
    api_sports = {}
    hourly = {}
    recent_notices = []
    latest_usage = {}
    latest_scan = {}
    odds_monthly_projection = {}

    def inc(mapping, key, amount=1):
        if not key:
            return
        mapping[key] = mapping.get(key, 0) + amount

    for line in lines:
        timestamp = re.match(r"\[(\d{4}-\d{2}-\d{2}) (\d{2}):", line)
        if timestamp:
            inc(hourly, f"{timestamp.group(1)} {timestamp.group(2)}:00")
        lower = line.lower()
        if "scan started" in lower or "scan initiated" in lower:
            categories["scan_started"] += 1
        if "scan complete" in lower:
            categories["scan_complete"] += 1
            scan_match = re.search(r"candidates=(\d+).*?placed=(\d+).*?balance=\$?(-?\d+(?:\.\d+)?)", line)
            if scan_match:
                latest_scan = {
                    "candidates": int(scan_match.group(1)),
                    "placed": int(scan_match.group(2)),
                    "balance": float(scan_match.group(3)),
                    "line": line,
                }
        if "api ok" in lower:
            categories["api_ok"] += 1
            sport_match = re.search(r"sport=([a-z0-9_]+)", line)
            if sport_match:
                inc(api_sports, sport_match.group(1))
            usage_match = re.search(r"monthly_used=(\d+)/(\d+).*?quota_remaining=(\d+)", line)
            if usage_match:
                latest_usage = {
                    "monthly_used": int(usage_match.group(1)),
                    "monthly_limit": int(usage_match.group(2)),
                    "quota_remaining": int(usage_match.group(3)),
                    "line": line,
                }
        if "api error" in lower or "http 429" in lower or "too_many_requests" in lower or "failed" in lower:
            categories["api_error"] += 1
        if "kalshi series fetch failed" in lower or "kalshi fetch failed" in lower:
            categories["kalshi_fetch_failed"] += 1
            recent_notices.append(line)
        if "paper sports bet" in lower or "paper bet:" in lower:
            categories["paper_bet"] += 1
        if "settled sports bet: win" in lower:
            categories["settled_win"] += 1
        if "settled sports bet: loss" in lower:
            categories["settled_loss"] += 1
        if "no paper bets" in lower or "no bets" in lower:
            categories["no_bets"] += 1
        if "top sports candidate" in lower or "top candidate" in lower:
            categories["top_candidate"] += 1
            skips_match = re.search(r"skips=\[(.*?)\]", line)
            if skips_match:
                for reason in re.findall(r"'([^']+)'", skips_match.group(1)):
                    inc(skip_reasons, reason)
        if "diagnostics:" in lower:
            categories["diagnostics"] += 1
            for key, value in re.findall(r"([a-z_]+)=(-?\d+(?:\.\d+)?)", line):
                diagnostics[key] = diagnostics.get(key, 0) + float(value)
        if "duplicate" in lower:
            categories["duplicate"] += 1
        if "unmatched" in lower or "no kalshi match" in lower:
            categories["unmatched"] += 1
        if "expired bot tracking" in lower:
            categories["expired_tracking"] += 1
        if "using cached odds" in lower:
            categories["cached_odds"] += 1
        if "grok_veto" in lower or "grok veto" in lower:
            categories["grok_veto"] += 1
        if "live_order_not_filled" in lower:
            categories["live_order_not_filled"] += 1
        if "missed fill tracked" in lower:
            categories["missed_fill_tracked"] += 1
        if "missed fill settled: win" in lower:
            categories["missed_fill_would_win"] += 1
        if "missed fill settled: loss" in lower:
            categories["missed_fill_would_loss"] += 1
        if "small-edge skip" in lower or "small_edge_same_game_cap" in lower:
            categories["same_game_small_edge_cap"] += 1
        if any(token in lower for token in ("error", "failed", "429", "no paper bets", "expired", "unmatched")):
            recent_notices.append(line)

    if kind == "sports":
        budget = read_json(SPORTS_ODDS_BUDGET_FILE, {})
        report = read_json(SPORTS_REPORT_FILE, {})
        report_budget = report.get("odds_budget") or {}
        settings = read_json(Path("bot_settings.json"), {})
        entries = budget.get("entries") if isinstance(budget.get("entries"), list) else []
        local_used = sum(max(0, int(float(entry.get("actual_cost") or 0))) for entry in entries)
        reported_used = report_budget.get("operating_credits_used")
        used = int(float(reported_used if reported_used is not None else local_used))
        usage_source = str(
            report_budget.get("operating_usage_source")
            or "local_calendar_ledger_fallback"
        )
        monthly_limit = int(float(settings.get("SPORTS_ODDS_MONTHLY_CREDIT_LIMIT") or latest_usage.get("monthly_limit") or 4500000))
        pacing_target = int(float(settings.get("SPORTS_ODDS_PACING_CREDIT_TARGET") or 4750000))
        hard_limit = int(float(settings.get("SPORTS_ODDS_HARD_CREDIT_LIMIT") or 5000000))
        now = datetime.now(CENTRAL_TZ)

        def projection_time(value):
            return parse_central_datetime(value)

        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        timed_entries = [
            (projection_time(entry.get("at")), max(0, int(float(entry.get("actual_cost") or 0))))
            for entry in entries
        ]
        timed_entries = [(at, cost) for at, cost in timed_entries if at is not None and at <= now]
        entry_times = [at for at, _cost in timed_entries]
        if start is None and entry_times:
            start = min(entry_times)
        if start is None:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (
            start.replace(year=start.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12
            else start.replace(month=start.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        )
        elapsed_days = max((now - start).total_seconds() / 86400.0, 1.0 / 24.0)
        remaining_days = max(0.0, (next_month - now).total_seconds() / 86400.0)
        since_reset_daily_rate = used / elapsed_days
        pacing_window_hours = max(
            1.0,
            float(
                report_budget.get("pacing_window_hours")
                or settings.get("SPORTS_ODDS_PACING_WINDOW_HOURS")
                or 24
            ),
        )
        recent_cutoff = now - timedelta(hours=pacing_window_hours)
        recent_entries = [(at, cost) for at, cost in timed_entries if at >= recent_cutoff]
        recent_cost = sum(cost for _at, cost in recent_entries)
        recent_start = max(start, recent_cutoff)
        recent_elapsed_days = max(
            (now - recent_start).total_seconds() / 86400.0,
            pacing_window_hours / 24.0,
        )
        daily_rate = recent_cost / recent_elapsed_days if recent_entries else since_reset_daily_rate
        if report_budget.get("pacing_recent_credits_per_hour") is not None:
            daily_rate = float(report_budget["pacing_recent_credits_per_hour"]) * 24
        paid_refresh_multiplier = max(
            1.0,
            float(report_budget.get("paid_refresh_multiplier") or 1),
        )
        unpaced_projected = int(round(float(
            report_budget.get("unpaced_projected_operating_credits")
            or used + daily_rate * remaining_days
        )))
        projected = int(round(float(
            report_budget.get("projected_operating_credits")
            or used + (daily_rate / paid_refresh_multiplier) * remaining_days
        )))
        safe_daily_rate = float(
            report_budget.get("pacing_safe_credits_per_hour") or 0
        ) * 24
        if safe_daily_rate <= 0 and remaining_days > 0:
            safe_daily_rate = max(0.0, pacing_target - used) / remaining_days
        pressure_mode = str(report_budget.get("odds_usage_pressure_mode") or "normal")
        provider_reserve = int(float(report_budget.get("provider_reserve_credits") or 0))
        odds_monthly_projection = {
            "used": used,
            "limit": monthly_limit,
            "monitoring_target": monthly_limit,
            "pacing_target": pacing_target,
            "hard_limit": hard_limit,
            "daily_rate": round(daily_rate, 1),
            "since_reset_daily_rate": round(since_reset_daily_rate, 1),
            "safe_daily_rate": round(safe_daily_rate, 1),
            "remaining_days": round(remaining_days, 1),
            "projected": projected,
            "unpaced_projected": unpaced_projected,
            "projected_over": max(0, projected - pacing_target),
            "status": (
                "hard_limit" if projected >= hard_limit
                else "pacing_active" if paid_refresh_multiplier > 1
                else "reserve_in_use" if projected > monthly_limit
                else "on_track"
            ),
            "projection_basis": f"trailing_{pacing_window_hours:g}_hours" if recent_entries else "since_reset",
            "basis_started_at": start.isoformat(timespec="seconds"),
            "usage_source": usage_source,
            "local_calendar_used": local_used,
            "last_hour_credits": report_budget.get("last_hour_credits"),
            "last_hour_calls": report_budget.get("last_hour_calls"),
            "paid_refresh_multiplier": paid_refresh_multiplier,
            "pressure_mode": pressure_mode,
            "provider_reserve": provider_reserve,
            "provider_spendable_to_reset": int(float(
                report_budget.get("provider_spendable_credits_to_reset") or 0
            )),
            "shadow_paid_calls_allowed": bool(
                report_budget.get("shadow_paid_calls_allowed", True)
            ),
        }

    category_rows = [{"label": key.replace("_", " "), "count": value} for key, value in categories.items()]
    category_rows.sort(key=lambda row: (-row["count"], row["label"]))
    diagnostic_rows = [{"label": key, "count": round(value, 2)} for key, value in diagnostics.items()]
    diagnostic_rows.sort(key=lambda row: (-row["count"], row["label"]))
    return {
        "kind": kind,
        "line_count": len(lines),
        "categories": category_rows,
        "skip_reasons": top_counter_items(skip_reasons),
        "diagnostics": diagnostic_rows[:16],
        "api_sports": top_counter_items(api_sports),
        "hourly": top_counter_items(hourly, 24),
        "recent_notices": recent_notices[-20:],
        "latest_usage": latest_usage,
        "odds_monthly_projection": odds_monthly_projection,
        "latest_scan": latest_scan,
    }


def read_jsonl_events(path: Path, limit: int = 100):
    events = []
    for line in read_tail(path, limit):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def crypto_candidate_log(events, limit=200):
    rows = []
    skip_reasons = {}
    for event in events:
        if event.get("type") != "scan_analytics":
            continue
        scanned_at = event.get("ts") or event.get("at") or ""
        for candidate in event.get("top_candidates") or []:
            reasons = candidate.get("skip_reasons") or []
            for reason in reasons:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            rows.append({
                "scanned_at": scanned_at,
                "asset": candidate.get("asset"),
                "ticker": candidate.get("ticker"),
                "side": candidate.get("side"),
                "entry_price": candidate.get("entry_price"),
                "edge": candidate.get("edge"),
                "confidence": candidate.get("confidence"),
                "expected_edge": candidate.get("expected_edge"),
                "edge_low": candidate.get("edge_low"),
                "probability_net_edge_positive": candidate.get("probability_net_edge_positive"),
                "probability": candidate.get("probability") or {},
                "data_quality": candidate.get("data_quality") or {},
                "decision": candidate.get("decision"),
                "skip_reasons": reasons,
                "campaign_review": candidate.get("campaign_review") or {},
                "disagreement_guard": candidate.get("disagreement_guard") or {},
                "directional_confirmation": candidate.get("directional_confirmation") or {},
            })
    rows.sort(key=lambda row: row.get("scanned_at") or "", reverse=True)
    return {
        "rows": rows[:limit],
        "skip_reasons": top_counter_items(skip_reasons, 20),
    }


def _crypto_candidate_rows_from_event(event):
    if event.get("type") != "scan_analytics":
        return []
    scanned_at = event.get("ts") or event.get("at") or ""
    try:
        scanned_epoch = datetime.fromisoformat(
            str(scanned_at).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        scanned_epoch = 0.0
    return [
        {
            "scanned_at": scanned_at,
            "scanned_epoch": scanned_epoch,
            "asset": candidate.get("asset"),
            "ticker": candidate.get("ticker"),
            "side": candidate.get("side"),
            "entry_price": candidate.get("entry_price"),
            "edge": candidate.get("edge"),
            "confidence": candidate.get("confidence"),
            "expected_edge": candidate.get("expected_edge"),
            "edge_low": candidate.get("edge_low"),
            "probability_net_edge_positive": candidate.get("probability_net_edge_positive"),
            "probability": candidate.get("probability") or {},
            "data_quality": candidate.get("data_quality") or {},
            "decision": candidate.get("decision"),
            "skip_reasons": candidate.get("skip_reasons") or [],
            "campaign_review": candidate.get("campaign_review") or {},
            "disagreement_guard": candidate.get("disagreement_guard") or {},
            "directional_confirmation": candidate.get("directional_confirmation") or {},
        }
        for candidate in event.get("top_candidates") or []
    ]


def refresh_crypto_candidate_history(path: Path, retention_hours=48):
    """Incrementally cache compact candidate rows from the rotating event log."""
    with CRYPTO_CANDIDATE_HISTORY_LOCK:
        cache = CRYPTO_CANDIDATE_HISTORY_CACHE
        try:
            stat = path.stat()
        except FileNotFoundError:
            return []
        identity = (getattr(stat, "st_dev", 0), getattr(stat, "st_ino", 0))
        path_key = str(path.resolve())
        reset_reader = bool(
            cache["path"] != path_key
            or cache["identity"] != identity
            or stat.st_size < int(cache["offset"] or 0)
        )
        if reset_reader:
            if cache["path"] != path_key:
                cache["rows"] = []
            cache.update({
                "path": path_key,
                "identity": identity,
                "offset": 0,
                "fragment": b"",
            })
        if stat.st_size > int(cache["offset"] or 0):
            with path.open("rb") as handle:
                handle.seek(int(cache["offset"] or 0))
                chunk = handle.read()
                cache["offset"] = handle.tell()
            payload = bytes(cache["fragment"] or b"") + chunk
            lines = payload.split(b"\n")
            cache["fragment"] = lines.pop() if lines else b""
            for raw_line in lines:
                if not raw_line.strip():
                    continue
                try:
                    event = json.loads(raw_line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                cache["rows"].extend(_crypto_candidate_rows_from_event(event))
        cutoff = time.time() - max(1.0, float(retention_hours)) * 3600.0
        cache["rows"] = [
            row for row in cache["rows"]
            if float(row.get("scanned_epoch") or 0) >= cutoff
        ][-100000:]
        return list(cache["rows"])


def crypto_candidate_history(path: Path, minutes=60, limit=50000):
    rows = refresh_crypto_candidate_history(path)
    minutes = max(0.0, min(2880.0, float(minutes or 0)))
    cutoff = time.time() - minutes * 60.0 if minutes > 0 else 0.0
    filtered = [
        row for row in rows
        if cutoff <= float(row.get("scanned_epoch") or 0)
    ]
    filtered = list(reversed(filtered[-max(1, int(limit)):]))
    return {
        "rows": filtered,
        "minutes": minutes,
        "available_rows": len(rows),
        "oldest_scanned_at": rows[0].get("scanned_at") if rows else None,
        "newest_scanned_at": rows[-1].get("scanned_at") if rows else None,
    }


def _sports_ticker_sport(ticker):
    value = str(ticker or "").upper()
    prefixes = (
        ("KXMLB", "MLB"), ("KXWNBA", "WNBA"), ("KXNBA", "NBA"),
        ("KXNCAAF", "NCAAF"), ("KXNCAAB", "NCAAB"), ("KXNFL", "NFL"),
        ("KXNHL", "NHL"), ("KXATP", "Tennis ATP"), ("KXWTA", "Tennis WTA"),
        ("KXUFC", "MMA/UFC"), ("KXMLS", "Soccer"),
    )
    return next((label for prefix, label in prefixes if value.startswith(prefix)), "Other")


def _sports_log_number(text, key):
    match = re.search(rf"(?:^|\s){re.escape(key)}=(-?\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def _sports_log_time(line):
    match = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
    if not match:
        return "", 0.0
    scanned_at = match.group(1)
    try:
        epoch = datetime.strptime(scanned_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CENTRAL_TZ).timestamp()
    except ValueError:
        epoch = 0.0
    return scanned_at, epoch


def _sports_candidate_row(line):
    scanned_at, scanned_epoch = _sports_log_time(line)
    match = re.search(
        r"Top sports candidate:\s*edge=(-?\d+(?:\.\d+)?)%\s+skips=\[(.*?)\]\s+"
        r"(moneyline|spread|total)\s+(.+?)\s+/\s+(\S+)\s*(.*)$",
        line,
        flags=re.I,
    )
    if not match:
        return None
    reasons = re.findall(r"['\"]([^'\"]+)['\"]", match.group(2))
    tail = match.group(6)
    families = _sports_log_number(tail, "families")
    raw_books = _sports_log_number(tail, "raw_books")
    if raw_books is None:
        raw_books = _sports_log_number(tail, "exact_line_books")
    stale_books = _sports_log_number(tail, "stale")
    decision = "eligible" if not reasons else ("not_filled" if reasons == ["live_order_not_filled"] else "skipped")
    return {
        "scanned_at": scanned_at,
        "scanned_epoch": scanned_epoch,
        "row_type": "candidate",
        "decision": decision,
        "sport": _sports_ticker_sport(match.group(5)),
        "market_type": match.group(3).lower(),
        "selection": match.group(4).strip(),
        "ticker": match.group(5),
        "entry_price": _sports_log_number(tail, "entry"),
        "edge": float(match.group(1)),
        "confidence": _sports_log_number(tail, "conf"),
        "pro_score": _sports_log_number(tail, "pro"),
        "final_score": _sports_log_number(tail, "final"),
        "families": int(families) if families is not None else None,
        "raw_books": int(raw_books) if raw_books is not None else None,
        "stale_books": int(stale_books) if stale_books is not None else None,
        "volume": _sports_log_number(tail, "vol"),
        "units": _sports_log_number(tail, "units"),
        "stake": None,
        "skip_reasons": reasons,
        "edge_basis": (re.search(r"(?:^|\s)edge_basis=([^\s]+)", tail) or [None, None])[1],
    }


def _sports_placed_row(line, latest_by_ticker):
    scanned_at, scanned_epoch = _sports_log_time(line)
    match = re.search(
        r"LIVE SPORTS BET:\s+owner=(\S+)\s+(moneyline|spread|total)\s+(.+?)\s+(Yes|No)\s+"
        r"(\S+)\s+stake=\$(-?\d+(?:\.\d+)?)\s+edge=(-?\d+(?:\.\d+)?)%\s*(.*)$",
        line,
        flags=re.I,
    )
    if not match:
        return None
    ticker = match.group(5)
    prior_source = latest_by_ticker.get(ticker) or {}
    prior_age = scanned_epoch - float(prior_source.get("scanned_epoch") or 0)
    # Do not present a stale earlier-scan quote as the actual fill price.
    prior = dict(prior_source) if 0 <= prior_age <= 60 else {}
    prior.update({
        "scanned_at": scanned_at,
        "scanned_epoch": scanned_epoch,
        "row_type": "bet",
        "decision": "placed",
        "sport": _sports_ticker_sport(ticker),
        "market_type": match.group(2).lower(),
        "selection": match.group(3).strip(),
        "side": match.group(4),
        "ticker": ticker,
        "edge": float(match.group(7)),
        "stake": float(match.group(6)),
        "units": _sports_log_number(match.group(8), "units"),
        "owner": match.group(1),
        "skip_reasons": [],
    })
    tail = match.group(8)
    prior["requested_units"] = _sports_log_number(tail, "requested_units")
    prior["unit_size"] = _sports_log_number(tail, "unit_size")
    actual_units = filled_units(prior)
    if actual_units is not None:
        prior["units"] = actual_units
        prior["units_basis"] = "filled_stake"
    for field, key in (("entry_price", "entry"), ("confidence", "conf"), ("pro_score", "pro"), ("final_score", "final")):
        value = _sports_log_number(tail, key)
        if value is not None:
            prior[field] = value
    families = _sports_log_number(tail, "families")
    if families is not None:
        prior["families"] = int(families)
    return prior


def refresh_sports_candidate_history(path: Path, retention_hours=168, bootstrap_bytes=64 * 1024 * 1024):
    """Incrementally structure sports candidate and placed-bet log lines."""
    with SPORTS_CANDIDATE_HISTORY_LOCK:
        cache = SPORTS_CANDIDATE_HISTORY_CACHE
        try:
            stat = path.stat()
        except FileNotFoundError:
            return []
        identity = (getattr(stat, "st_dev", 0), getattr(stat, "st_ino", 0))
        path_key = str(path.resolve())
        reset_reader = bool(cache["path"] != path_key or cache["identity"] != identity or stat.st_size < int(cache["offset"] or 0))
        if reset_reader:
            start = max(0, stat.st_size - int(bootstrap_bytes))
            cache.update({
                "path": path_key, "identity": identity, "offset": start,
                "fragment": b"", "rows": [], "latest_by_ticker": {},
            })
        if stat.st_size > int(cache["offset"] or 0):
            with path.open("rb") as handle:
                handle.seek(int(cache["offset"] or 0))
                chunk = handle.read()
                cache["offset"] = handle.tell()
            payload = bytes(cache["fragment"] or b"") + chunk
            lines = payload.split(b"\n")
            cache["fragment"] = lines.pop() if lines else b""
            if reset_reader and int(cache["offset"] or 0) - len(chunk) > 0 and lines:
                lines = lines[1:]
            for raw_line in lines:
                line = raw_line.decode("utf-8", errors="replace").strip()
                row = _sports_candidate_row(line)
                if row:
                    cache["rows"].append(row)
                    cache["latest_by_ticker"][row["ticker"]] = row
                    continue
                row = _sports_placed_row(line, cache["latest_by_ticker"])
                if row:
                    cache["rows"].append(row)
        cutoff = time.time() - max(1.0, float(retention_hours)) * 3600.0
        cache["rows"] = [row for row in cache["rows"] if float(row.get("scanned_epoch") or 0) >= cutoff][-150000:]
        return list(cache["rows"])


def sports_candidate_history(
    path: Path, minutes=360, limit=2000, min_price=None, max_price=None,
    min_edge=None, max_edge=None, decision="", sport="", market="", search="", portfolio=None,
):
    rows = refresh_sports_candidate_history(path)
    minutes = max(0.0, min(10080.0, float(minutes or 0)))
    cutoff = time.time() - minutes * 60.0 if minutes > 0 else 0.0
    time_rows = [row for row in rows if float(row.get("scanned_epoch") or 0) >= cutoff]
    available_sports = sorted({str(row.get("sport")) for row in time_rows if row.get("sport")})
    search = str(search or "").strip().lower()

    def included(row):
        price = row.get("entry_price")
        edge = row.get("edge")
        if price is None:
            if min_price is not None or max_price is not None:
                return False
        elif (min_price is not None and float(price) < float(min_price)) or (max_price is not None and float(price) > float(max_price)):
            return False
        if edge is None or (min_edge is not None and float(edge) < float(min_edge)) or (max_edge is not None and float(edge) > float(max_edge)):
            return False
        if decision and row.get("decision") != decision:
            return False
        if sport and row.get("sport") != sport:
            return False
        if market and row.get("market_type") != market:
            return False
        haystack = f"{row.get('selection') or ''} {row.get('ticker') or ''} {' '.join(row.get('skip_reasons') or [])}".lower()
        return not search or search in haystack

    filtered = [row for row in time_rows if included(row)]
    matched_rows = len(filtered)
    filtered = list(reversed(filtered[-max(1, min(5000, int(limit))):]))
    # Older logs printed the requested tier after depth trimming. Match a
    # unique retained fill, including stake/side/time, never just the ticker
    # (a user can hold a separate manual position in the same market).
    ledger = {}
    for bet in [*((portfolio or {}).get("bets") or []), *((portfolio or {}).get("history") or [])]:
        stamp = parse_central_datetime(bet.get("placed_at"))
        if filled_units(bet) is None or stamp is None:
            continue
        key = (bet.get("kalshi_ticker"), bet.get("strategy_owner"),
               str(bet.get("order_side") or bet.get("kalshi_order_side") or "yes").lower())
        ledger.setdefault(key, []).append((stamp.timestamp(), bet))
    corrected = []
    for row in filtered:
        row = dict(row)
        if row.get("decision") == "placed" and row.get("units_basis") != "filled_stake":
            key = (row.get("ticker"), row.get("owner"), str(row.get("side") or "yes").lower())
            matches = [bet for stamp, bet in ledger.get(key, [])
                       if abs(stamp - float(row.get("scanned_epoch") or 0)) <= 60
                       and abs(float(bet["stake"]) - float(row.get("stake") or 0)) < 0.005]
            if len(matches) == 1:
                bet = matches[0]
                row.update(units=filled_units(bet), units_basis="filled_stake",
                           unit_size=bet.get("unit_size") or (bet.get("sports_units") or {}).get("unit_size"),
                           requested_units=(bet.get("sports_units") or {}).get("additional_units"))
        corrected.append(row)
    return {
        "rows": corrected,
        "minutes": minutes,
        "available_rows": len(rows),
        "time_rows": len(time_rows),
        "matched_rows": matched_rows,
        "sports": available_sports,
        "oldest_scanned_at": rows[0].get("scanned_at") if rows else None,
        "newest_scanned_at": rows[-1].get("scanned_at") if rows else None,
    }


def money(value):
    return round(float(value or 0), 2)


def is_user_bet_row(row):
    return (
        row.get("strategy_owner") in {"user_bet", "manual_live_import"}
        or row.get("source") == "user_manual"
        or bool(row.get("user_bet"))
        or bool(row.get("manual_bet"))
    )


def parse_central_datetime(value):
    """Parse timestamps with offsets and treat naive values as America/Chicago wall time."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CENTRAL_TZ)
    return parsed.astimezone(CENTRAL_TZ)


def performance_timestamp(row):
    """Return a settled row timestamp in local dashboard time."""
    raw = row.get("settled_at") or row.get("settlement_ts") or row.get("closed_at") or row.get("placed_at")
    if not raw:
        return None
    return parse_central_datetime(raw)


def build_daily_performance(rows, today=None, max_days=730):
    """Aggregate automated settled bets into a gap-free daily P&L series."""
    today = today or datetime.now(CENTRAL_TZ).date()
    buckets = {}
    for row in rows or []:
        if is_user_bet_row(row) or row.get("result") not in {"WIN", "LOSS"}:
            continue
        settled = performance_timestamp(row)
        if settled is None:
            continue
        day = settled.date()
        bucket = buckets.setdefault(day, {"profit": 0.0, "stake": 0.0, "wins": 0, "losses": 0})
        bucket["profit"] += parse_float(row.get("profit"), 0)
        bucket["stake"] += parse_float(row.get("stake"), 0)
        bucket["wins" if row.get("result") == "WIN" else "losses"] += 1

    first_day = min(buckets) if buckets else today
    if (today - first_day).days >= max_days:
        first_day = today - timedelta(days=max_days - 1)
    result = []
    day = first_day
    while day <= today:
        bucket = buckets.get(day, {"profit": 0.0, "stake": 0.0, "wins": 0, "losses": 0})
        stake = money(bucket["stake"])
        profit = money(bucket["profit"])
        wins = int(bucket["wins"])
        losses = int(bucket["losses"])
        result.append({
            "date": day.isoformat(),
            "profit": profit,
            "stake": stake,
            "wins": wins,
            "losses": losses,
            "settled": wins + losses,
            "roi": round((profit / stake) * 100, 1) if stake else 0.0,
        })
        day += timedelta(days=1)
    return result


def build_combined_performance(sports_daily, crypto_daily, current_bankroll):
    """Combine bot series and reconstruct strategy bankroll from current equity."""
    sports_by_day = {row["date"]: row for row in sports_daily or []}
    crypto_by_day = {row["date"]: row for row in crypto_daily or []}
    dates = sorted(set(sports_by_day) | set(crypto_by_day))
    combined = []
    for day in dates:
        sports = sports_by_day.get(day, {})
        crypto = crypto_by_day.get(day, {})
        sports_profit = money(sports.get("profit", 0))
        crypto_profit = money(crypto.get("profit", 0))
        stake = money(parse_float(sports.get("stake"), 0) + parse_float(crypto.get("stake"), 0))
        profit = money(sports_profit + crypto_profit)
        wins = int(sports.get("wins", 0)) + int(crypto.get("wins", 0))
        losses = int(sports.get("losses", 0)) + int(crypto.get("losses", 0))
        combined.append({
            "date": day,
            "sports_profit": sports_profit,
            "crypto_profit": crypto_profit,
            "profit": profit,
            "stake": stake,
            "wins": wins,
            "losses": losses,
            "settled": wins + losses,
            "roi": round((profit / stake) * 100, 1) if stake else 0.0,
        })

    total_profit = money(sum(row["profit"] for row in combined))
    baseline = money(parse_float(current_bankroll, 0) - total_profit)
    running = baseline
    bankroll = []
    for row in combined:
        running = money(running + row["profit"])
        bankroll.append({"date": row["date"], "bankroll": running, "daily_profit": row["profit"]})
    return {
        "daily": combined,
        "bankroll": bankroll,
        "current_bankroll": money(current_bankroll),
        "starting_bankroll": baseline,
        "total_bot_profit": total_profit,
    }


def user_bet_analytics(open_bets, history):
    open_rows = [row for row in open_bets if is_user_bet_row(row)]
    history_rows = [row for row in history if is_user_bet_row(row)]
    settled_rows = [row for row in history_rows if row.get("result") in {"WIN", "LOSS"}]
    stake = money(sum(row.get("stake", 0) for row in settled_rows))
    profit = money(sum(row.get("profit", 0) for row in settled_rows))
    wins = sum(1 for row in settled_rows if row.get("result") == "WIN")
    losses = sum(1 for row in settled_rows if row.get("result") == "LOSS")
    voids = sum(1 for row in history_rows if row.get("result") == "VOID")
    open_exposure = money(sum(row.get("stake", 0) for row in open_rows))
    return {
        "tag": "user_bet",
        "open_count": len(open_rows),
        "open_exposure": open_exposure,
        "settled_count": len(settled_rows),
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "stake": stake,
        "profit": profit,
        "roi": round((profit / stake) * 100, 1) if stake else 0.0,
    }


def is_trusted_capper_row(row):
    return bool(
        row.get("source") == "trusted_capper"
        or row.get("strategy_owner") == "trusted_capper"
        or row.get("trusted_capper_ticket_id")
    )


def is_system_test_sports_row(row):
    """Identify explicit validation orders that are neither user nor strategy bets."""
    return bool(
        row.get("source") == "manual_live_test"
        or row.get("strategy_owner") == "manual_live_test"
    )


def is_regular_sports_bot_row(row):
    """Return True only for autonomous sports strategy rows.

    InfluencedBets and positions entered manually by the user share the live
    sports portfolio for cash reconciliation, but neither belongs in the
    regular bot's strategy performance.
    """
    return (
        not is_user_bet_row(row)
        and not is_trusted_capper_row(row)
        and row.get("source") != "aibetpicks"
        and row.get("strategy_owner") != "aibetpicks"
        and not is_system_test_sports_row(row)
    )


def sports_ledger_bucket(open_rows, history_rows, today_rows=None, now=None):
    """Build a compact, full-ledger accounting summary for one source."""
    now = now or datetime.now(CENTRAL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CENTRAL_TZ)
    local_now = now.astimezone(CENTRAL_TZ)
    month_start = local_now.date().replace(day=1)
    yesterday = local_now.date() - timedelta(days=1)
    decisions = [row for row in history_rows or [] if row.get("result") in {"WIN", "LOSS"}]
    voids = [row for row in history_rows or [] if row.get("result") == "VOID"]
    if today_rows is None:
        today_rows = [
            row for row in decisions
            if (performance_timestamp(row) or datetime.min.replace(tzinfo=CENTRAL_TZ)).date()
            == local_now.date()
        ]
    else:
        today_rows = [row for row in today_rows if row.get("result") in {"WIN", "LOSS"}]
    month_rows = [
        row for row in decisions
        if (performance_timestamp(row) or datetime.min.replace(tzinfo=CENTRAL_TZ)).date()
        >= month_start
    ]
    yesterday_settled_rows = [
        row for row in decisions
        if (performance_timestamp(row) or datetime.min.replace(tzinfo=CENTRAL_TZ)).date()
        == yesterday
    ]
    yesterday_decision_rows = []
    for row in decisions:
        placed = parse_central_datetime(row.get("placed_at"))
        if placed is not None and placed.date() == yesterday:
            yesterday_decision_rows.append(row)
    last_24h_cutoff = local_now - timedelta(hours=24)
    last_24h_rows = [
        row for row in decisions
        if (performance_timestamp(row) or datetime.min.replace(tzinfo=CENTRAL_TZ))
        >= last_24h_cutoff
    ]
    timestamps = [performance_timestamp(row) for row in decisions]
    timestamps = [stamp for stamp in timestamps if stamp is not None]

    def period(rows):
        stake = sum(parse_float(row.get("stake"), 0) for row in rows)
        profit = sum(parse_float(row.get("profit"), 0) for row in rows)
        winning_rows = [row for row in rows if row.get("result") == "WIN"]
        losing_rows = [row for row in rows if row.get("result") == "LOSS"]
        wins = len(winning_rows)
        losses = len(losing_rows)
        return {
            "settled_count": wins + losses,
            "wins": wins,
            "losses": losses,
            "stake": money(stake),
            "profit": money(profit),
            "roi": round(profit / stake * 100.0, 1) if stake else 0.0,
            "average_stake": money(stake / len(rows)) if rows else 0.0,
            "average_win": money(
                sum(parse_float(row.get("profit"), 0) for row in winning_rows) / wins
            ) if wins else 0.0,
            "average_loss": money(
                sum(parse_float(row.get("profit"), 0) for row in losing_rows) / losses
            ) if losses else 0.0,
        }

    result = period(decisions)
    result.update({
        "voids": len(voids),
        "open_count": len(open_rows or []),
        "open_exposure": money(sum(parse_float(row.get("stake"), 0) for row in open_rows or [])),
        "ledger_start_date": min(stamp.date() for stamp in timestamps).isoformat() if timestamps else None,
        "today": period(today_rows),
        "yesterday_settled": period(yesterday_settled_rows),
        "yesterday_decisions": period(yesterday_decision_rows),
        "last_24h": period(last_24h_rows),
        "month_to_date": period(month_rows),
    })
    return result


def sports_source_analytics(open_bets, history, today_history=None, now=None):
    """Reconcile the shared sports ledger into mutually exclusive sources."""
    open_bets = list(open_bets or [])
    history = list(history or [])
    today_history_rows = None if today_history is None else list(today_history or [])
    predicates = {
        "system_test": is_system_test_sports_row,
        "aibetpicks": is_aibetpicks,
        "influenced_bets": is_trusted_capper_row,
        "user_live": is_user_bet_row,
        "regular_bot": is_regular_sports_bot_row,
    }
    def source_bucket(row):
        # Explicit sources take precedence over generic ownership backfills.
        return next((name for name, predicate in predicates.items() if predicate(row)), "regular_bot")
    buckets = {}
    for name in predicates:
        buckets[name] = sports_ledger_bucket(
            [row for row in open_bets if source_bucket(row) == name],
            [row for row in history if source_bucket(row) == name],
            None if today_history_rows is None else [row for row in today_history_rows if source_bucket(row) == name],
            now=now,
        )
    all_sports = sports_ledger_bucket(open_bets, history, today_history_rows, now=now)
    classified_settled_count = sum(bucket["settled_count"] for bucket in buckets.values())
    classified_open_count = sum(bucket["open_count"] for bucket in buckets.values())
    classified_profit = money(sum(bucket["profit"] for bucket in buckets.values()))
    classified_open_exposure = money(sum(bucket["open_exposure"] for bucket in buckets.values()))
    buckets["all_sports"] = all_sports
    buckets["reconciliation"] = {
        "settled_count_matches": classified_settled_count == all_sports["settled_count"],
        "profit_delta": money(all_sports["profit"] - classified_profit),
        "open_count_matches": classified_open_count == all_sports["open_count"],
        "open_exposure_delta": money(all_sports["open_exposure"] - classified_open_exposure),
    }
    return buckets


def american_probability_percent(value):
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    if odds == 0:
        return None
    return (100.0 / (odds + 100.0) if odds > 0 else -odds / (-odds + 100.0)) * 100.0


def trusted_capper_row_units(row, default_unit_size):
    unit_size = parse_float(row.get("unit_size"), default_unit_size)
    if unit_size <= 0:
        unit_size = default_unit_size if default_unit_size > 0 else 1.0
    explicit = row.get("unit_count")
    if explicit is None:
        explicit = (row.get("sports_units") or {}).get("placed_units")
    units = parse_float(explicit, -1)
    if units < 0:
        units = parse_float(row.get("stake"), 0) / unit_size
    return round(max(0.0, units), 3), unit_size


def trusted_capper_price_improvement_pp(row):
    posted = american_probability_percent(row.get("capper_posted_odds"))
    contracts = parse_float(row.get("contracts"), 0)
    stake = parse_float(row.get("stake"), 0)
    fee = parse_float(row.get("fee"), 0)
    if posted is None:
        return None
    if contracts > 0:
        effective_entry = ((stake + fee) / contracts) * 100.0
    else:
        effective_entry = parse_float(row.get("entry_price"), -1)
    if effective_entry < 0:
        return None
    return round(posted - effective_entry, 3)


def trusted_capper_clv_5m(row):
    horizon = (row.get("fixed_horizon_clv") or {}).get("5m") or {}
    value = horizon.get("clv_vs_entry_ask_cents")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def trusted_capper_group_summary(rows, label_fn, default_unit_size):
    groups = {}
    for row in rows or []:
        label = str(label_fn(row) or "unknown")
        item = groups.setdefault(label, {
            "label": label, "bets": 0, "wins": 0, "losses": 0, "voids": 0,
            "stake": 0.0, "profit": 0.0, "risked_units": 0.0, "net_units": 0.0,
        })
        units, unit_size = trusted_capper_row_units(row, default_unit_size)
        item["bets"] += 1
        item["wins"] += 1 if row.get("result") == "WIN" else 0
        item["losses"] += 1 if row.get("result") == "LOSS" else 0
        item["voids"] += 1 if row.get("result") == "VOID" else 0
        item["stake"] += parse_float(row.get("stake"), 0)
        item["profit"] += parse_float(row.get("profit"), 0)
        item["risked_units"] += units
        item["net_units"] += parse_float(row.get("profit"), 0) / unit_size
    output = []
    for item in groups.values():
        decisions = item["wins"] + item["losses"]
        item["stake"] = money(item["stake"])
        item["profit"] = money(item["profit"])
        item["risked_units"] = round(item["risked_units"], 2)
        item["net_units"] = round(item["net_units"], 2)
        item["win_rate"] = round(item["wins"] / decisions * 100, 1) if decisions else 0.0
        item["roi"] = round(item["profit"] / item["stake"] * 100, 1) if item["stake"] else 0.0
        output.append(item)
    return sorted(output, key=lambda row: (-row["bets"], -row["profit"], row["label"]))


def trusted_capper_analytics(open_bets, history, tickets, default_unit_size=20.0):
    account_open_rows = [row for row in open_bets or [] if is_trusted_capper_row(row)]
    account_history_rows = [row for row in history or [] if is_trusted_capper_row(row)]
    account_summary = sports_ledger_bucket(account_open_rows, account_history_rows)
    excluded_open_rows = [
        row for row in open_bets or []
        if is_trusted_capper_row(row) and row.get("strategy_analytics_excluded") is True
    ]
    open_rows = [
        row for row in open_bets or []
        if is_trusted_capper_row(row) and row.get("strategy_analytics_excluded") is not True
    ]
    excluded_settled_rows = [
        row for row in history or []
        if is_trusted_capper_row(row)
        and row.get("result") in {"WIN", "LOSS", "VOID"}
        and row.get("strategy_analytics_excluded") is True
    ]
    settled_rows = [
        row for row in history or []
        if is_trusted_capper_row(row) and row.get("result") in {"WIN", "LOSS", "VOID"}
        and row.get("strategy_analytics_excluded") is not True
    ]
    decision_rows = [row for row in settled_rows if row.get("result") in {"WIN", "LOSS"}]
    wins = sum(1 for row in decision_rows if row.get("result") == "WIN")
    losses = sum(1 for row in decision_rows if row.get("result") == "LOSS")
    stake = sum(parse_float(row.get("stake"), 0) for row in settled_rows)
    profit = sum(parse_float(row.get("profit"), 0) for row in settled_rows)
    risked_units = 0.0
    net_units = 0.0
    for row in settled_rows:
        units, unit_size = trusted_capper_row_units(row, default_unit_size)
        risked_units += units
        net_units += parse_float(row.get("profit"), 0) / unit_size
    open_units = sum(trusted_capper_row_units(row, default_unit_size)[0] for row in open_rows)
    open_exposure = sum(parse_float(row.get("stake"), 0) for row in open_rows)
    price_improvements = [
        value for value in (trusted_capper_price_improvement_pp(row) for row in [*open_rows, *settled_rows])
        if value is not None
    ]
    clv_values = [value for value in (trusted_capper_clv_5m(row) for row in settled_rows) if value is not None]
    edges = [parse_float(row.get("edge"), 0) for row in [*open_rows, *settled_rows] if row.get("edge") is not None]
    ticket_rows = list(tickets or [])
    executable_tickets = [row for row in ticket_rows if row.get("executable", True)]
    placed_ticket_ids = {
        str(row.get("trusted_capper_ticket_id"))
        for row in [*open_rows, *settled_rows]
        if row.get("trusted_capper_ticket_id")
    }
    queue_counts = {}
    for ticket in ticket_rows:
        status = str(ticket.get("status") or "unknown")
        queue_counts[status] = queue_counts.get(status, 0) + 1
    capper_unit_values = [parse_float(row.get("capper_units"), 0) for row in executable_tickets if parse_float(row.get("capper_units"), 0) > 0]
    placed_units = [trusted_capper_row_units(row, default_unit_size)[0] for row in [*open_rows, *settled_rows]]
    counterfactual_tickets = [
        row for row in executable_tickets
        if isinstance(row.get("native_counterfactual"), dict)
        and row.get("native_counterfactual")
    ]
    counterfactual_selected = sum(
        1
        for row in counterfactual_tickets
        if (row.get("native_counterfactual") or {}).get("would_native_select") is True
    )
    counterfactual_reason_counts = {}
    for ticket in counterfactual_tickets:
        for reason in (ticket.get("native_counterfactual") or {}).get("reasons") or []:
            counterfactual_reason_counts[reason] = counterfactual_reason_counts.get(reason, 0) + 1
    counterfactual_recent = []
    for ticket in sorted(
        counterfactual_tickets,
        key=lambda row: str((row.get("native_counterfactual") or {}).get("evaluated_at") or ""),
        reverse=True,
    )[:50]:
        review = ticket.get("native_counterfactual") or {}
        components = review.get("components") or []
        component = components[0] if components else {}
        settlement = ticket.get("settlement") or {}
        counterfactual_recent.append({
            "evaluated_at": review.get("evaluated_at"),
            "selection": ticket.get("selection"),
            "sport": ticket.get("sport_key") or component.get("sport_key"),
            "market_type": ticket.get("market_type") or component.get("market_type"),
            "decision": review.get("status"),
            "edge": component.get("edge"),
            "confidence": component.get("confidence"),
            "pro_score": component.get("pro_score"),
            "final_score": component.get("final_score"),
            "book_families": component.get("independent_book_families"),
            "reasons": review.get("reasons") or [],
            "ticket_status": ticket.get("status"),
            "result": settlement.get("result"),
        })

    def timing_label(row):
        bucket = str(row.get("bet_timing_bucket") or "")
        if bucket:
            return bucket.replace("_", " ")
        return "live" if row.get("game_started") else "pregame"

    def sport_label(row):
        return str(row.get("sport_title") or row.get("sport_key") or "unknown").replace("_", " ")

    daily = trusted_capper_group_summary(
        settled_rows,
        lambda row: (performance_timestamp(row).date().isoformat() if performance_timestamp(row) else "unknown"),
        default_unit_size,
    )
    recent = sorted(
        settled_rows,
        key=lambda row: performance_timestamp(row) or datetime.min.replace(tzinfo=CENTRAL_TZ),
        reverse=True,
    )[:30]
    recent_results = []
    for row in recent:
        units, unit_size = trusted_capper_row_units(row, default_unit_size)
        price_improvement = trusted_capper_price_improvement_pp(row)
        recent_results.append({
            "settled_at": row.get("settled_at") or row.get("settlement_ts"),
            "selection": row.get("selected_team") or row.get("selection"),
            "sport": sport_label(row),
            "market_type": row.get("market_type"),
            "timing": timing_label(row),
            "posted_odds": row.get("capper_posted_odds"),
            "entry_odds": row.get("entry_american_odds"),
            "placed_units": units,
            "result": row.get("result"),
            "profit": money(row.get("profit")),
            "net_units": round(parse_float(row.get("profit"), 0) / unit_size, 2),
            "price_improvement_pp": price_improvement,
            "clv_5m_cents": trusted_capper_clv_5m(row),
        })

    normal_settled = [
        row for row in history or []
        if row.get("result") in {"WIN", "LOSS", "VOID"}
        and not is_user_bet_row(row)
        and not is_trusted_capper_row(row)
    ]
    comparison = trusted_capper_group_summary(settled_rows, lambda _row: "InfluencedBets", default_unit_size)
    if not comparison:
        comparison = [{
            "label": "InfluencedBets", "bets": 0, "wins": 0, "losses": 0, "voids": 0,
            "stake": 0.0, "profit": 0.0, "risked_units": 0.0, "net_units": 0.0,
            "win_rate": 0.0, "roi": 0.0,
        }]
    comparison.extend(trusted_capper_group_summary(normal_settled, lambda _row: "Regular sports bot", default_unit_size))
    return {
        "source": "InfluencedBets",
        "account": account_summary,
        "account_profit": account_summary["profit"],
        "account_stake": account_summary["stake"],
        "account_settled_positions": account_summary["settled_count"],
        "account_open_positions": account_summary["open_count"],
        "account_open_exposure": account_summary["open_exposure"],
        "strategy_integrity_excluded_positions": len(excluded_settled_rows),
        "strategy_integrity_excluded_open_positions": len(excluded_open_rows),
        "strategy_integrity_excluded_profit": money(sum(parse_float(row.get("profit"), 0) for row in excluded_settled_rows)),
        "approved_tickets": len(ticket_rows),
        "executable_tickets": len(executable_tickets),
        "track_only_tickets": sum(1 for row in ticket_rows if not row.get("executable", True)),
        "placed_tickets": len(placed_ticket_ids),
        "ticket_fill_rate": round(len(placed_ticket_ids) / len(executable_tickets) * 100, 1) if executable_tickets else 0.0,
        "queue_statuses": [{"label": key.replace("_", " "), "count": value} for key, value in sorted(queue_counts.items())],
        "open_positions": len(open_rows),
        "settled_positions": len(settled_rows),
        "wins": wins,
        "losses": losses,
        "voids": sum(1 for row in settled_rows if row.get("result") == "VOID"),
        "win_rate": round(wins / (wins + losses) * 100, 1) if wins + losses else 0.0,
        "stake": money(stake),
        "profit": money(profit),
        "roi": round(profit / stake * 100, 1) if stake else 0.0,
        "risked_units": round(risked_units, 2),
        "net_units": round(net_units, 2),
        "open_units": round(open_units, 2),
        "open_exposure": money(open_exposure),
        "average_capper_units": round(sum(capper_unit_values) / len(capper_unit_values), 2) if capper_unit_values else 0.0,
        "average_placed_units": round(sum(placed_units) / len(placed_units), 2) if placed_units else 0.0,
        "average_price_improvement_pp": round(sum(price_improvements) / len(price_improvements), 2) if price_improvements else None,
        "better_price_count": sum(1 for value in price_improvements if value > 0.05),
        "worse_price_count": sum(1 for value in price_improvements if value < -0.05),
        "average_clv_5m_cents": round(sum(clv_values) / len(clv_values), 2) if clv_values else None,
        "average_entry_edge": round(sum(edges) / len(edges), 2) if edges else None,
        "native_counterfactual": {
            "version": "capper-off-counterfactual-v1",
            "affects_execution": False,
            "evaluated_tickets": len(counterfactual_tickets),
            "native_selected_tickets": counterfactual_selected,
            "native_rejected_tickets": len(counterfactual_tickets) - counterfactual_selected,
            "native_selection_rate_pct": round(
                counterfactual_selected / len(counterfactual_tickets) * 100.0,
                1,
            ) if counterfactual_tickets else 0.0,
            "reason_counts": [
                {"label": reason.replace("_", " "), "reason": reason, "count": count}
                for reason, count in sorted(
                    counterfactual_reason_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "recent": counterfactual_recent,
        },
        "by_sport": trusted_capper_group_summary(settled_rows, sport_label, default_unit_size),
        "by_market": trusted_capper_group_summary(settled_rows, lambda row: row.get("market_type") or "unknown", default_unit_size),
        "by_timing": trusted_capper_group_summary(settled_rows, timing_label, default_unit_size),
        "by_units": trusted_capper_group_summary(settled_rows, lambda row: f"{trusted_capper_row_units(row, default_unit_size)[0]:g}u", default_unit_size),
        "daily": daily,
        "recent_results": recent_results,
        "comparison": comparison,
    }


def local_live_readiness(settings):
    checks = {
        "mode_live": settings.get("SPORTS_EXECUTION_MODE") == "live",
        "allow_live_trading": settings.get("ALLOW_LIVE_TRADING") == "true",
        "live_order_enabled": settings.get("SPORTS_LIVE_ORDER_ENABLED") == "true",
        "confirmation_disabled": settings.get("LIVE_REQUIRE_CONFIRMATION") == "false",
        "kalshi_api_key_saved": bool(settings.get("KALSHI_API_KEY")),
        "kalshi_private_key_saved": bool(settings.get("KALSHI_PRIVATE_KEY_PATH") or settings.get("KALSHI_API_SECRET")),
    }
    blocking = [key for key, ok in checks.items() if not ok]
    return {"ready": not blocking, "checks": checks, "blocking": blocking}


def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_today_timestamp(value):
    parsed = parse_central_datetime(value)
    return bool(parsed and parsed.date() == datetime.now(CENTRAL_TZ).date())




def active_daily_reset_cutoff(strategy=None):
    reset = read_json(DAILY_RESET_FILE, {})
    if reset.get("local_date") != datetime.now(CENTRAL_TZ).date().isoformat():
        return None
    strategies = {
        str(value).strip().lower()
        for value in (reset.get("strategies") or [])
        if str(value).strip()
    }
    if strategy and strategies and str(strategy).strip().lower() not in strategies:
        return None
    return parse_central_datetime(reset.get("reset_at"))


def sports_unit_session_identity():
    now = datetime.now(CENTRAL_TZ)
    local_date = now.date().isoformat()
    reset = read_json(DAILY_RESET_FILE, {})
    reset_at = None
    if reset.get("local_date") == local_date and reset.get("reset_at"):
        parsed_reset = parse_central_datetime(reset.get("reset_at"))
        if parsed_reset:
            reset_at = parsed_reset.isoformat(timespec="seconds")
    return f"{local_date}|{reset_at or 'daily_open'}", local_date, reset_at


def is_after_daily_reset(value, strategy=None):
    cutoff = active_daily_reset_cutoff(strategy)
    if not cutoff or not value:
        return True
    parsed = parse_central_datetime(value)
    if not parsed:
        return True
    return parsed > cutoff


def tiered_percent_for_value(value, tiers, fallback):
    for raw_tier in str(tiers or "").split(","):
        if ":" not in raw_tier:
            continue
        raw_limit, raw_pct = [part.strip().lower() for part in raw_tier.split(":", 1)]
        try:
            limit = float("inf") if raw_limit in {"inf", "infinity", "*"} else float(raw_limit)
            pct = float(raw_pct)
        except ValueError:
            continue
        if value <= limit:
            return pct
    return fallback


def open_sports_bet_by_ticker(ticker):
    portfolio = read_json(
        SPORTS_PORTFOLIO_FILE,
        {"mode": "paper", "starting_balance": 300, "balance": 300, "bets": [], "history": []},
    )
    for bet in portfolio.get("bets", []):
        if bet.get("status", "open") == "open" and bet.get("kalshi_ticker") == ticker:
            return portfolio, bet
    raise ValueError("open_bet_not_found")


def bet_order_side(bet):
    side = str(bet.get("kalshi_order_side") or bet.get("order_side") or bet.get("side") or "yes").strip().lower()
    return "no" if side in {"no", "n"} else "yes"


def current_kalshi_bet_quote(ticker):
    if not ticker:
        raise ValueError("ticker_required")
    data, _headers = get_json(f"{KALSHI_BASE_URL}/markets/{ticker}")
    market = data.get("market") or data
    prices = market_prices(market)
    return {"market": market, "prices": prices}


def sports_bet_more_quote(payload):
    settings = load_settings()
    if settings.get("SPORTS_BET_MORE_ENABLED", "false") != "true":
        return {"ok": False, "error": "bet_more_disabled", "message": "Bet More is disabled in settings."}
    ticker = str(payload.get("ticker") or "").strip()
    _portfolio, bet = open_sports_bet_by_ticker(ticker)
    quote = current_kalshi_bet_quote(ticker)
    side = bet_order_side(bet)
    prices = quote["prices"]
    current_price = prices["yes_ask"] if side == "yes" else prices["no_ask"]
    original_price = parse_float(bet.get("entry_price"), 0)
    move = abs(float(current_price or 0) - original_price) if current_price and original_price else None
    max_move = parse_float(payload.get("max_move_cents"), parse_float(settings.get("SPORTS_BET_MORE_MAX_PRICE_MOVE_CENTS"), 20))
    return {
        "ok": True,
        "ticker": ticker,
        "side": side,
        "original_price": original_price,
        "current_price": current_price,
        "current_american": american_from_cents(current_price),
        "price_move_cents": round(move, 2) if move is not None else None,
        "max_move_cents": max_move,
        "within_price_guard": bool(move is not None and move <= max_move),
        "market_status": quote["market"].get("status"),
        "yes_bid": prices["yes_bid"],
        "yes_ask": prices["yes_ask"],
        "no_bid": prices["no_bid"],
        "no_ask": prices["no_ask"],
        "live_ready": local_live_readiness(settings),
    }


def american_from_cents(cents):
    price = parse_float(cents, 0)
    if price <= 0 or price >= 100:
        return None
    if price <= 50:
        return int(round(((100 - price) / price) * 100))
    return int(round(-(price / (100 - price)) * 100))


def sports_bet_more_order(payload):
    settings = load_settings()
    if settings.get("SPORTS_BET_MORE_ENABLED", "false") != "true":
        return {"ok": False, "error": "bet_more_disabled", "message": "Bet More is disabled in settings."}
    ticker = str(payload.get("ticker") or "").strip()
    stake = parse_float(payload.get("stake"), 0)
    if stake <= 0:
        raise ValueError("stake_required")
    portfolio, bet = open_sports_bet_by_ticker(ticker)
    quote = sports_bet_more_quote(payload)
    if not quote.get("within_price_guard"):
        return {"ok": False, "error": "price_moved_too_far", **quote}

    price = int(round(parse_float(quote.get("current_price"), 0)))
    if price <= 0 or price >= 100:
        return {"ok": False, "error": "invalid_live_price", **quote}

    wants_paper = settings.get("SPORTS_EXECUTION_MODE", "paper") == "paper" or portfolio.get("mode") == "paper" or bet.get("mode") == "paper"
    if wants_paper:
        balance = parse_float(portfolio.get("balance"), 0)
        actual_stake = round(stake, 2)
        if actual_stake > balance:
            return {"ok": False, "error": "paper_balance_too_low", "balance": balance, "requested_stake": actual_stake, **quote}
        contracts = actual_stake / (price / 100.0)
        existing_contracts = parse_float(bet.get("contracts"), 0)
        if existing_contracts <= 0:
            original_stake = parse_float(bet.get("stake"), 0)
            original_price = parse_float(bet.get("entry_price"), 0)
            existing_contracts = original_stake / (original_price / 100.0) if original_price > 0 else 0.0
        topup = {
            "at": datetime.now(CENTRAL_TZ).isoformat(timespec="seconds"),
            "ticker": ticker,
            "mode": "paper",
            "strategy_owner": "manual_bet_more",
            "side": quote["side"],
            "price": price,
            "contracts": round(contracts, 4),
            "requested_stake": actual_stake,
            "actual_stake": actual_stake,
            "price_move_cents": quote.get("price_move_cents"),
        }
        bet["base_strategy_owner"] = bet.get("base_strategy_owner") or bet.get("strategy_owner") or ""
        bet["strategy_owner"] = "manual_bet_more"
        owners = list(dict.fromkeys([*(bet.get("strategy_owners") or []), bet.get("base_strategy_owner"), "manual_bet_more"]))
        bet["strategy_owners"] = [owner for owner in owners if owner]
        bet.setdefault("topups", []).append(topup)
        bet["stake"] = money(parse_float(bet.get("stake"), 0) + actual_stake)
        bet["contracts"] = round(existing_contracts + contracts, 4)
        bet["avg_entry_price"] = round((parse_float(bet.get("stake"), 0) / bet["contracts"]) * 100, 2) if bet["contracts"] else price
        bet["last_topup_at"] = topup["at"]
        bet["last_topup_price"] = price
        portfolio["balance"] = money(balance - actual_stake)
        write_json(SPORTS_PORTFOLIO_FILE, portfolio)
        log_line(SPORTS_LOG_FILE, f"PAPER BET MORE PLACED: {ticker} side={quote['side']} price={price}c stake=${actual_stake:.2f} contracts={contracts:.4f}")
        return {"ok": True, "mode": "paper", "order": topup, "quote": quote, "balance": portfolio["balance"]}

    readiness = local_live_readiness(settings)
    if not readiness.get("ready"):
        return {"ok": False, "error": "live_not_ready", "readiness": readiness}
    if not ((bet.get("live_order") or {}).get("ok") or portfolio.get("mode") == "live" or bet.get("mode") == "live"):
        return {"ok": False, "error": "open_bet_is_paper_only", "message": "Quote is available, but real add-on orders are only allowed for live-tracked open bets."}

    live_max_price = int(parse_float(settings.get("SPORTS_LIVE_MAX_PRICE_CENTS"), 95))
    if price > live_max_price:
        return {"ok": False, "error": "live_price_above_cap", "live_max_price_cents": live_max_price, **quote}

    live_max_stake = parse_float(settings.get("LIVE_MAX_STAKE"), 500)
    live_max_stake_pct = parse_float(settings.get("LIVE_MAX_STAKE_PCT"), 0.08)
    live_max_stake_cap = parse_float(settings.get("LIVE_MAX_STAKE_CAP"), 500)
    live_account = read_json(SPORTS_LIVE_RECONCILIATION_FILE, {}).get("account", {})
    live_cash = parse_float(live_account.get("cash_balance"), 0)
    effective_live_max_stake = 0.0
    if live_cash > 0 and live_max_stake_pct > 0:
        effective_live_max_stake = live_cash * live_max_stake_pct
        if live_max_stake_cap > 0:
            effective_live_max_stake = min(effective_live_max_stake, live_max_stake_cap)
        if live_max_stake > 0:
            effective_live_max_stake = min(effective_live_max_stake, live_max_stake)
    else:
        configured_caps = [
            value
            for value in (live_max_stake, live_max_stake_cap)
            if value > 0
        ]
        effective_live_max_stake = min(configured_caps) if configured_caps else stake
    requested_stake = min(stake, effective_live_max_stake)
    count = int((requested_stake * 100) // price)
    if count < 1:
        return {"ok": False, "error": "stake_too_small_for_one_contract", "price": price, "requested_stake": requested_stake, "effective_live_max_stake": round(effective_live_max_stake, 2), **quote}
    actual_stake = round(count * price / 100.0, 2)
    side = quote["side"]
    body = kalshi_event_order_body(
        ticker=ticker,
        order_side=side,
        price_cents=price,
        count=count,
        client_order_id=f"dashboard-more-{uuid.uuid4()}",
        time_in_force=settings.get("SPORTS_LIVE_TIME_IN_FORCE", "immediate_or_cancel"),
    )
    data, _headers = kalshi_private_request(
        KALSHI_ORDER_PATH,
        method="POST",
        body=body,
        api_key_id=settings.get("KALSHI_API_KEY", ""),
        private_key_path=settings.get("KALSHI_PRIVATE_KEY_PATH", ""),
        private_key_pem=settings.get("KALSHI_API_SECRET", ""),
    )
    order_response = data.get("order") if isinstance(data, dict) and isinstance(data.get("order"), dict) else data
    try:
        fill_count = float((order_response or {}).get("fill_count") or 0)
    except (TypeError, ValueError):
        fill_count = 0.0
    if fill_count <= 0:
        log_line(SPORTS_LOG_FILE, f"LIVE BET MORE NOT FILLED: {ticker} side={side} price={price}c requested_stake=${requested_stake:.2f} response={order_response}")
        return {
            "ok": False,
            "error": "live_order_not_filled",
            "ticker": ticker,
            "side": side,
            "price": price,
            "requested_stake": round(stake, 2),
            "capped_requested_stake": round(requested_stake, 2),
            "contracts": 0,
            "response": order_response,
            "quote": quote,
        }

    actual_stake = round(fill_count * price / 100.0, 2)
    topup = {
        "at": datetime.now(CENTRAL_TZ).isoformat(timespec="seconds"),
        "ticker": ticker,
        "strategy_owner": "manual_bet_more",
        "side": side,
        "price": price,
        "contracts": fill_count,
        "requested_stake": round(stake, 2),
        "capped_requested_stake": round(requested_stake, 2),
        "actual_stake": actual_stake,
        "price_move_cents": quote.get("price_move_cents"),
        "response": order_response,
    }
    bet["base_strategy_owner"] = bet.get("base_strategy_owner") or bet.get("strategy_owner") or ""
    bet["strategy_owner"] = "manual_bet_more"
    owners = list(dict.fromkeys([*(bet.get("strategy_owners") or []), bet.get("base_strategy_owner"), "manual_bet_more"]))
    bet["strategy_owners"] = [owner for owner in owners if owner]
    bet.setdefault("topups", []).append(topup)
    if (bet.get("live_order") or {}).get("ok"):
        base_stake = parse_float((bet.get("live_order") or {}).get("actual_stake"), 0)
        base_contracts = parse_float((bet.get("live_order") or {}).get("contracts"), 0)
        topup_stake = sum(parse_float(item.get("actual_stake"), 0) for item in bet.get("topups", []))
        topup_contracts = sum(parse_float(item.get("contracts"), 0) for item in bet.get("topups", []))
        bet["stake"] = money(base_stake + topup_stake)
        bet["contracts"] = round(base_contracts + topup_contracts, 4)
    else:
        bet["stake"] = money(parse_float(bet.get("stake"), 0) + actual_stake)
        bet["contracts"] = round(parse_float(bet.get("contracts"), 0) + fill_count, 4)
    bet["last_topup_at"] = topup["at"]
    bet["last_topup_price"] = price
    portfolio["balance"] = money(parse_float(portfolio.get("balance"), 0) - actual_stake)
    write_json(SPORTS_PORTFOLIO_FILE, portfolio)
    log_line(SPORTS_LOG_FILE, f"LIVE BET MORE ORDER FILLED: {ticker} side={side} price={price}c stake=${actual_stake:.2f} contracts={fill_count:.4f}")
    return {"ok": True, "order": topup, "quote": quote, "balance": portfolio["balance"]}


def build_local_ai_state():
    crypto_settings = read_json(CRYPTO_SETTINGS_FILE, {})
    sports_settings = load_settings()
    base_url = crypto_settings.get("CRYPTO_LOCAL_AI_URL") or sports_settings.get("SPORTS_LOCAL_AI_URL") or "http://127.0.0.1:11434"
    model = crypto_settings.get("CRYPTO_LOCAL_AI_MODEL") or sports_settings.get("SPORTS_LOCAL_AI_MODEL") or "gpt-oss:20b"
    status = ollama_health(base_url, model)
    status["label"] = "Local AI (Ollama)"
    status["base_url"] = base_url
    status["crypto_enabled"] = str(crypto_settings.get("CRYPTO_LOCAL_AI_ENABLED", "true")).lower() == "true"
    status["sports_enabled"] = str(sports_settings.get("SPORTS_LOCAL_AI_ENABLED", "true")).lower() == "true"
    status["enforcement"] = "shadow"
    return status


def build_state():
    settings = load_settings()
    shared_bankroll = build_shared_bankroll_state(save=True) if build_shared_bankroll_state else {}
    sports_state = build_sports_state()
    sports_state["aibetpicks"] = aibetpicks_summary()
    sports_state["modest_recovery"] = {
        **modest_recovery_summary(read_json(SPORTS_PORTFOLIO_FILE, {})),
        "enabled": str(settings.get("SPORTS_MODEST_RECOVERY_ENABLED", "false")).lower() == "true",
    }
    sports_state["itf_shadow"] = itf_shadow_summary()
    sports_state["itf_followups"] = itf_followups_summary()
    sports_state["itf_price_quality"] = itf_price_quality_summary()
    crypto_state = build_crypto_state()
    shared_cash = parse_float(shared_bankroll.get("cash"), sports_state.get("balance", 0))
    shared_exposure = parse_float(
        shared_bankroll.get("system_live_open_exposure"),
        parse_float(sports_state.get("open_exposure"), 0) + parse_float(crypto_state.get("open_exposure"), 0),
    )
    performance = build_combined_performance(
        sports_state.get("daily_performance", []),
        crypto_state.get("daily_performance", []),
        money(shared_cash + shared_exposure),
    )
    return {
        "generated_at": datetime.now(CENTRAL_TZ).isoformat(timespec="seconds"),
        "sports": sports_state,
        "crypto": crypto_state,
        "performance": performance,
        "shared_bankroll": shared_bankroll,
        "settings": public_settings(),
        "processes": process_status(),
        "local_ai": build_local_ai_state(),
        "storage": read_storage_health(),
    }


def _compact_dashboard_row(value, depth=0):
    """Keep table-facing evidence while dropping bulky, deeply nested audit copies."""
    if isinstance(value, list):
        return [_compact_dashboard_row(item, depth + 1) for item in value[:6]]
    if not isinstance(value, dict):
        return value
    compact = {}
    for key, item in value.items():
        if not isinstance(item, (dict, list)):
            compact[key] = item
        elif depth < 1:
            compact[key] = _compact_dashboard_row(item, depth + 1)
    return compact


def _compact_named_row_lists(value):
    """Bound research-ledger table rows without changing their aggregate summaries."""
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key in {
                "recent_records",
                "recent_history",
                "top_candidates",
                "open_positions",
                "recent_bets",
            } and isinstance(item, list):
                value[key] = [_compact_dashboard_row(row) for row in item[:40]]
            else:
                _compact_named_row_lists(item)
    elif isinstance(value, list):
        for item in value:
            _compact_named_row_lists(item)


def compact_dashboard_state(state):
    """Return the bounded summary payload used by the frequently-polled dashboard."""
    limits = {
        "history": 40,
        "open_bets": 20,
        "bot_open_bets": 20,
        "events": 40,
        "candidate_log": 50,
        "missed_fill_history": 30,
    }
    for section_name in ("sports", "crypto"):
        section = state.get(section_name) or {}
        for key, limit in limits.items():
            rows = section.get(key)
            if isinstance(rows, list):
                section[key] = [
                    _compact_dashboard_row(row)
                    for row in rows[:limit]
                ]
        report = section.get("report")
        if isinstance(report, dict):
            _compact_named_row_lists(report)
    crypto = state.get("crypto") or {}
    if isinstance(crypto.get("perps_shadow"), dict):
        _compact_named_row_lists(crypto["perps_shadow"])
    trusted = (state.get("sports") or {}).get("trusted_capper") or {}
    if isinstance(trusted.get("tickets"), list):
        trusted["tickets"] = [
            _compact_dashboard_row(row)
            for row in trusted["tickets"][:40]
        ]
    state["payload_metadata"] = {
        "view": "bounded_dashboard_summary",
        "candidate_history_endpoints": [
            "/api/crypto/candidate-log",
            "/api/sports/candidate-log",
        ],
    }
    return state


def public_crypto_settings():
    settings = read_json(CRYPTO_SETTINGS_FILE, {})
    public = {}
    for key, value in settings.items():
        if key.endswith("_KEY") or "SECRET" in key or "PRIVATE" in key:
            public[key] = "saved" if value else ""
        else:
            public[key] = value
    return public


def save_crypto_settings(new_values):
    with CRYPTO_SETTINGS_LOCK:
        return _save_crypto_settings(new_values)


def _save_crypto_settings(new_values):
    current = read_json(CRYPTO_SETTINGS_FILE, {})
    allowed = set(current.keys()) | CRYPTO_SENSITIVE_KEYS | {
        "CRYPTO_GROK_ENABLED",
        "CRYPTO_NEWS_ENABLED",
        "CRYPTO_GROK_MODEL",
    }
    for key, value in new_values.items():
        if key not in allowed and not key.startswith("CRYPTO_"):
            continue
        value = "" if value is None else str(value).strip()
        if key in CRYPTO_SENSITIVE_KEYS and value in ("", "********"):
            continue
        current[key] = value
    write_json(CRYPTO_SETTINGS_FILE, current)
    return current


def reload_patient_crypto_worker(*, needs_reload, open_positions):
    """Called only by the explicit activation POST; never by status rendering."""
    pid_state = read_json(Path(".crypto_bot.pid"), {})
    worker_pid = int(pid_state.get("pid") or 0)
    running = pid_running(worker_pid)
    if running and not needs_reload:
        return {"pid": worker_pid, "running": True, "reloaded": False}
    if running:
        # Do not kill the wrapper's process tree: its shadow companions own
        # independent research cohorts and must continue undisturbed.
        command = (
            f"$worker = Get-CimInstance Win32_Process -Filter 'ProcessId = {worker_pid}'; "
            "if (-not $worker -or $worker.Name -notmatch '^python(?:w|[0-9]+(?:[.][0-9]+)*)?[.]exe$' "
            "-or $worker.CommandLine -notmatch 'crypto_paper_bettor[.]py') "
            "{ throw 'Crypto worker identity could not be verified' }; "
            f"Stop-Process -Id {worker_pid} -ErrorAction Stop"
        )
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                       capture_output=True, text=True, timeout=15, check=True)
    env = merged_process_env()
    env["CRYPTO_RUN_LOOP"] = "true"
    # Settings files remain authoritative for the button-controlled switches.
    for key in ("CRYPTO_ETH_PATIENT_ENABLED", "CRYPTO_LIVE_ORDER_ENABLED", "CRYPTO_ETH_PATIENT_BASE_STAKE_PCT",
                "ALLOW_LIVE_TRADING", "CRYPTO_EXECUTION_MODE", "CRYPTO_LIVE_DRY_RUN",
                "CRYPTO_LIVE_REQUIRE_CONFIRMATION", "CRYPTO_15M_SPRINT_SHADOW_ONLY",
                "CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED", "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL",
                "CRYPTO_RECONCILE_LIVE_ON_SCAN", "CRYPTO_LIVE_DEPTH_PREFLIGHT_ENABLED",
                "CRYPTO_ETH_PATIENT_ONLY_LIVE", "CRYPTO_LIVE_CORE_ENABLED",
                "MULTI_MARKET_LIVE_ENABLED", "MULTI_MARKET_COMMODITY_LIVE_ENABLED"):
        env.pop(key, None)
    with open("crypto_control.log", "a", encoding="utf-8") as output:
        output.write(f"\n[{datetime.now(CENTRAL_TZ).isoformat()}] User activated patient ETH; open positions before reload={open_positions}\n")
        output.flush()
        proc = subprocess.Popen([sys.executable, str(Path("crypto_paper_bettor.py").resolve())],
                                cwd=str(Path.cwd()), env=env, stdout=output, stderr=output,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    processes = load_processes()
    processes["crypto"] = {"pid": proc.pid, "running": True, "script": "crypto_paper_bettor.py",
                           "started_at": datetime.now(CENTRAL_TZ).isoformat(timespec="seconds")}
    save_processes(processes)
    return {"pid": proc.pid, "running": True, "reloaded": running}


def control_patient_crypto(action):
    with CRYPTO_SETTINGS_LOCK:
        settings = read_json(CRYPTO_SETTINGS_FILE, {})
        report = read_json(CRYPTO_LIVE_REPORT_FILE, {})
        return set_patient_control(
            action, settings, load_settings(), report,
            save=save_crypto_settings, reload_worker=reload_patient_crypto_worker,
            portfolio=load_patient_portfolio(CRYPTO_LIVE_PORTFOLIO_FILE) if action in {"activate", "reload", "make_live"} else {},
            code_installed=patient_code_installed(Path.cwd()),
        )


def control_crypto_funding(action):
    if action != "shared_pool":
        raise ValueError("Use shared cash for Sports and Crypto, with no percentage split.")
    with CRYPTO_SETTINGS_LOCK:
        # Reuse the trader's credential precedence without starting a worker.
        from crypto_paper_bettor import kalshi_credentials, load_settings as load_crypto_settings
        credentials = kalshi_credentials(load_crypto_settings())
        def request(path, method="GET", body=None):
            data, _headers = kalshi_private_request(path, method=method, body=body, **credentials)
            return data
        import shared_cash_pool
        return shared_cash_pool.activate(request)


def crypto_campaign_row_bot_number(row):
    campaign = (row or {}).get("crypto_live_campaign") or {}
    value = campaign.get("bot_number", (row or {}).get("bot_number", 1))
    try:
        return max(1, min(5, int(float(value or 1))))
    except (TypeError, ValueError):
        return 1


def sports_campaign_row_bot_number(row):
    campaign = (row or {}).get("live_campaign") or {}
    value = campaign.get("bot_number", (row or {}).get("bot_number", 1))
    try:
        return max(1, min(5, int(float(value or 1))))
    except (TypeError, ValueError):
        return 1


def reset_sports_campaign_bot(bot_number):
    try:
        bot_number = int(float(bot_number))
    except (TypeError, ValueError):
        raise ValueError("invalid_sports_bot_number")
    if bot_number < 1 or bot_number > 5:
        raise ValueError("sports_bot_number_must_be_1_through_5")

    settings = read_json(SETTINGS_FILE, {})
    configured_count = max(
        1,
        min(5, int(float(settings.get("SPORTS_LIVE_CAMPAIGN_BOT_COUNT") or 1))),
    )
    if bot_number > configured_count:
        raise ValueError(f"sports_bot_{bot_number}_is_not_enabled")

    portfolio = read_json(SPORTS_PORTFOLIO_FILE, {"bets": []})
    open_rows = [
        row
        for row in portfolio.get("bets") or []
        if str(row.get("strategy_owner") or "") == "live_campaign"
        and sports_campaign_row_bot_number(row) == bot_number
        and str(row.get("status") or "").lower() in {"open", "pending", "resting"}
    ]
    if open_rows:
        raise ValueError(
            f"sports_bot_{bot_number}_has_an_open_position; wait_for_it_to_settle_before_resetting"
        )

    now = datetime.now(CENTRAL_TZ)
    campaign_id = (
        f"reset-{now.date().isoformat()}-bot{bot_number}-"
        f"{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    setting_key = f"SPORTS_LIVE_CAMPAIGN_BOT_{bot_number}_CAMPAIGN_ID"
    settings[setting_key] = campaign_id
    write_json(SETTINGS_FILE, settings)
    return {
        "bot_number": bot_number,
        "campaign_id": campaign_id,
        "setting_key": setting_key,
        "reset_at": now.isoformat(timespec="seconds"),
    }


def reset_crypto_campaign_bot(bot_number):
    try:
        bot_number = int(float(bot_number))
    except (TypeError, ValueError):
        raise ValueError("invalid_crypto_bot_number")
    if bot_number < 1 or bot_number > 5:
        raise ValueError("crypto_bot_number_must_be_1_through_5")

    settings = read_json(CRYPTO_SETTINGS_FILE, {})
    configured_count = max(
        1,
        min(5, int(float(settings.get("CRYPTO_15M_CAMPAIGN_BOT_COUNT") or 1))),
    )
    if bot_number > configured_count:
        raise ValueError(f"crypto_bot_{bot_number}_is_not_enabled")

    mode = str(settings.get("CRYPTO_EXECUTION_MODE") or "paper").lower()
    portfolio_file = (
        CRYPTO_LIVE_PORTFOLIO_FILE
        if mode == "live"
        else CRYPTO_PAPER_PORTFOLIO_FILE
    )
    portfolio = read_json(portfolio_file, {"bets": []})
    open_rows = [
        row
        for row in portfolio.get("bets") or []
        if str(row.get("strategy_owner") or "") == "crypto_15m_campaign"
        and crypto_campaign_row_bot_number(row) == bot_number
        and str(row.get("status") or "").lower() in {"open", "pending", "resting"}
    ]
    if open_rows:
        raise ValueError(
            f"crypto_bot_{bot_number}_has_an_open_position; wait_for_it_to_settle_before_resetting"
        )

    now = datetime.now(CENTRAL_TZ)
    reset_at = now.isoformat(timespec="seconds")
    backup_file = portfolio_file.with_name(
        f"{portfolio_file.stem}.pre_bot{bot_number}_capacity_reset_"
        f"{now.strftime('%Y%m%d_%H%M%S')}{portfolio_file.suffix}"
    )
    if portfolio_file.exists():
        shutil.copy2(portfolio_file, backup_file)
    resets = portfolio.setdefault("crypto_15m_daily_capacity_resets", {})
    previous = resets.get(str(bot_number))
    resets[str(bot_number)] = {
        "reset_at": reset_at,
        "reason": "manual_fresh_start",
        "preserves_bet_history": True,
        "preserves_high_water_recovery": True,
        "previous_reset": previous,
    }
    write_json(portfolio_file, portfolio)
    return {
        "bot_number": bot_number,
        "reset_at": reset_at,
        "portfolio_file": str(portfolio_file),
        "backup_file": str(backup_file) if portfolio_file.exists() else "",
        "bet_history_preserved": True,
        "high_water_recovery_preserved": True,
    }


def reveal_crypto_setting(key):
    if key not in CRYPTO_SENSITIVE_KEYS:
        raise ValueError("not_a_crypto_sensitive_setting")
    return read_json(CRYPTO_SETTINGS_FILE, {}).get(key, "")


def build_crypto_state():
    crypto_settings = public_crypto_settings()
    crypto_mode = str(crypto_settings.get("CRYPTO_EXECUTION_MODE") or "paper").lower()
    if crypto_mode == "live":
        portfolio_file = CRYPTO_LIVE_PORTFOLIO_FILE
        report_file = CRYPTO_LIVE_REPORT_FILE
        log_file = CRYPTO_LIVE_LOG_FILE
        events_file = CRYPTO_LIVE_EVENTS_FILE
    else:
        portfolio_file = CRYPTO_PAPER_PORTFOLIO_FILE
        report_file = CRYPTO_PAPER_REPORT_FILE
        log_file = CRYPTO_PAPER_LOG_FILE
        events_file = CRYPTO_PAPER_EVENTS_FILE
    portfolio = read_json(
        portfolio_file,
        {"mode": crypto_mode, "starting_balance": 1000, "balance": 1000, "bets": []},
    )
    report = read_json(
        report_file,
        {"top_candidates": [], "open_bets": [], "recent_bets": [], "placed_count": 0, "settled_count": 0},
    )
    bets = portfolio.get("bets", [])
    open_rows = [bet for bet in bets if bet.get("status") == "open"]
    analytics_min_entry = parse_float(crypto_settings.get("CRYPTO_ANALYTICS_MIN_ENTRY_PRICE_CENTS"), 2.0)
    settled_rows = [
        bet for bet in bets
        if bet.get("status") == "settled" and parse_float(bet.get("entry_price"), 0) >= analytics_min_entry
    ]
    bot_settled_rows = [bet for bet in settled_rows if not is_user_bet_row(bet)]
    bot_open_rows = [bet for bet in open_rows if not is_user_bet_row(bet)]
    bot_today_rows = [
        bet for bet in bot_settled_rows
        if is_today_timestamp(bet.get("settled_at") or bet.get("settlement_ts"))
        and is_after_daily_reset(
            bet.get("settled_at") or bet.get("settlement_ts"),
            "crypto",
        )
    ]
    wins = sum(1 for bet in settled_rows if bet.get("result") == "WIN")
    losses = sum(1 for bet in settled_rows if bet.get("result") == "LOSS")
    starting_balance = money(portfolio.get("starting_balance", 1000))
    balance = money(portfolio.get("balance", starting_balance))
    exposure = money(sum(float(bet.get("stake") or 0) for bet in open_rows))
    realized_profit = money(sum(float(bet.get("profit") or 0) for bet in settled_rows))
    report_analytics = report.get("analytics") or {}
    crypto_events = list(reversed(read_jsonl_events(events_file, 100)))
    candidate_log = crypto_candidate_log(crypto_events)
    perps_report = read_json(
        CRYPTO_PERPS_SHADOW_REPORT_FILE,
        {"mode": "paper_shadow_only", "top_candidates": [], "open_positions": [], "recent_history": []},
    )
    settlement_lag_payload = read_json(
        CRYPTO_SETTLEMENT_LAG_SHADOW_FILE,
        {
            "summary": {
                "version": "btc-settlement-lag-shadow-v1",
                "mode": "paper_shadow_only",
                "affects_execution": False,
                "health": {"status": "waiting_for_first_tick"},
            }
        },
    )
    crypto_mode = crypto_mode or portfolio.get("mode", "paper")
    return {
        "mode": crypto_mode,
        "settings": crypto_settings,
        "active_files": {
            "portfolio": str(portfolio_file),
            "report": str(report_file),
            "log": str(log_file),
            "events": str(events_file),
        },
        "report": report,
        "balance": balance,
        "starting_balance": starting_balance,
        "open_exposure": exposure,
        "equity_estimate": money(balance + exposure),
        "pnl_estimate": realized_profit if crypto_mode == "live" else money(balance + exposure - starting_balance),
        "pnl_source": "crypto_settled_ledger" if crypto_mode == "live" else "paper_portfolio",
        "cash_scope": "shared_kalshi_account" if crypto_mode == "live" else "crypto_paper",
        "realized_profit": realized_profit,
        "bot_total_profit": money(sum(float(bet.get("profit") or 0) for bet in bot_settled_rows)),
        "bot_today_profit": money(sum(float(bet.get("profit") or 0) for bet in bot_today_rows)),
        "bot_today_wins": sum(1 for bet in bot_today_rows if bet.get("result") == "WIN"),
        "bot_today_losses": sum(1 for bet in bot_today_rows if bet.get("result") == "LOSS"),
        "daily_performance": build_daily_performance(bot_settled_rows),
        "bot_open_bets_count": len(bot_open_rows),
        "bot_open_bets": sorted(bot_open_rows, key=lambda bet: bet.get("placed_at", ""), reverse=True),
        "user_bet_analytics": user_bet_analytics(open_rows, settled_rows),
        "open_bets_count": len(open_rows),
        "settled_count": len(settled_rows),
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / max(1, wins + losses)) * 100, 1) if settled_rows else 0,
        "open_bets": sorted(open_rows, key=lambda bet: bet.get("placed_at", ""), reverse=True),
        "history": sorted(settled_rows, key=lambda bet: bet.get("settled_at", ""), reverse=True)[:200],
        "logs": list(reversed(read_tail(log_file, 80))),
        "log_analytics": build_log_analytics(log_file, "crypto"),
        "events": crypto_events,
        "candidate_log": candidate_log["rows"],
        "candidate_log_skip_reasons": candidate_log["skip_reasons"],
        "analytics": report_analytics,
        "settlement_lag_shadow": settlement_lag_payload.get("summary") or {},
        "research_expansion": read_json(CRYPTO_SHADOW_EXPANSION_REPORT_FILE, {}),
        "research_sizing": read_json(CRYPTO_SHADOW_SIZING_REPORT_FILE, {}),
        "btc_random_shadow": read_json(CRYPTO_BTC_RANDOM_REPORT_FILE, {}),
        "btc_value_shadow": read_json(CRYPTO_BTC_VALUE_REPORT_FILE, {}),
        "patient_control": patient_control_summary(
            crypto_settings, load_settings(), report, code_installed=patient_code_installed(Path.cwd()),
        ),
        "funding_control": {**crypto_funding_summary(
            read_json(Path("crypto_live_reconciliation.json"), {}),
            read_json(Path("crypto_funding_control.json"), {}),
        ), **shared_cash_summary()},
        "perps_shadow": {
            "report": perps_report,
            "portfolio": read_json(CRYPTO_PERPS_SHADOW_PORTFOLIO_FILE, {"positions": [], "history": []}),
            "logs": list(reversed(read_tail(CRYPTO_PERPS_SHADOW_LOG_FILE, 60))),
        },
    }


def build_sports_state():
    portfolio = read_json(
        SPORTS_PORTFOLIO_FILE,
        {"mode": "paper", "starting_balance": 300, "balance": 300, "bets": [], "history": []},
    )
    report = read_json(
        SPORTS_REPORT_FILE,
        {"odds_games": 0, "kalshi_markets": 0, "sports_markets": 0, "candidate_count": 0, "placed_count": 0, "top_candidates": []},
    )
    live_reconciliation = read_json(SPORTS_LIVE_RECONCILIATION_FILE, {})
    live_audit = read_json(SPORTS_LIVE_AUDIT_FILE, report.get("live_audit", {}))
    settings = load_settings()
    if not live_reconciliation.get("readiness"):
        live_reconciliation["readiness"] = local_live_readiness(settings)
    watchlist = read_json(SPORTS_WATCHLIST_FILE, {})
    open_bets = [bet for bet in portfolio.get("bets", []) if bet.get("status", "open") == "open"]
    history = portfolio.get("history", [])
    missed_fills_open = [row for row in portfolio.get("missed_fills", []) if row.get("status", "open") == "open"]
    missed_fill_history = [row for row in portfolio.get("missed_fill_history", []) if row.get("result") in {"WIN", "LOSS", "VOID"}]
    starting_balance = money(portfolio.get("starting_balance", 300))
    balance = money(portfolio.get("balance", starting_balance))
    sports_execution_mode = settings.get("SPORTS_EXECUTION_MODE", portfolio.get("mode", "paper"))
    live_cash_balance = ((live_reconciliation.get("account") or {}).get("cash_balance"))
    if sports_execution_mode == "live" and live_cash_balance is not None:
        balance = money(live_cash_balance)
        portfolio["mode"] = "live"
    exposure = money(sum(bet.get("stake", 0) for bet in open_bets))
    current_sports_equity = money(balance + exposure)
    unit_session_key, unit_local_date, unit_reset_at = sports_unit_session_identity()
    stored_unit_snapshot = portfolio.get("sports_unit_daily_snapshot") or {}
    unit_snapshot_valid = (
        stored_unit_snapshot.get("session_key") == unit_session_key
        and parse_float(stored_unit_snapshot.get("bankroll_base"), 0) > 0
    )
    unit_bankroll_base = money(
        stored_unit_snapshot.get("bankroll_base") if unit_snapshot_valid else current_sports_equity
    )
    unit_size_pct = parse_float(settings.get("SPORTS_UNIT_SIZE_PCT"), 1.0)
    effective_unit_size = max(0.01, money(unit_bankroll_base * unit_size_pct / 100.0))
    clv_values = [float(bet.get("clv_vs_ask", 0)) for bet in open_bets if "clv_vs_ask" in bet]
    wins = sum(1 for bet in history if bet.get("result") == "WIN")
    losses = sum(1 for bet in history if bet.get("result") == "LOSS")
    today_history = [
        bet for bet in history
        if is_today_timestamp(bet.get("settled_at") or bet.get("settlement_ts"))
        and is_after_daily_reset(
            bet.get("settled_at") or bet.get("settlement_ts"),
            "sports",
        )
    ]
    bot_history = [bet for bet in history if is_regular_sports_bot_row(bet)]
    bot_today_history = [bet for bet in today_history if is_regular_sports_bot_row(bet)]
    bot_open_bets = [bet for bet in open_bets if is_regular_sports_bot_row(bet)]
    source_analytics = sports_source_analytics(open_bets, history, today_history)
    aibetpicks_results = aibetpicks_analytics(portfolio.get("bets", []), history)
    today_wins = sum(1 for bet in today_history if bet.get("result") == "WIN")
    today_losses = sum(1 for bet in today_history if bet.get("result") == "LOSS")
    today_profit = money(sum(bet.get("profit", 0) for bet in today_history if "profit" in bet))
    settled_profit = money(sum(bet.get("profit", 0) for bet in history if "profit" in bet))
    trading_pnl_estimate = settled_profit
    deposited_capital_estimate = starting_balance
    if sports_execution_mode == "live":
        deposited_capital_estimate = money(balance + exposure - trading_pnl_estimate)
    recovery_carryover = settings.get("SPORTS_RECOVERY_CARRYOVER_ENABLED", "true") == "true"
    recovery_profit = settled_profit if recovery_carryover else money(report.get("daily_recovery_staking", {}).get("daily_realized_profit", settled_profit))
    recovery_drawdown = money(abs(min(0.0, recovery_profit)))
    open_recovery_bet = None
    for bet in open_bets:
        recovery = bet.get("recovery_staking") or {}
        if recovery.get("active"):
            open_recovery_bet = {
                "placed_at": bet.get("placed_at"),
                "ticker": bet.get("kalshi_ticker"),
                "selection": bet.get("selected_team") or bet.get("selection"),
                "market_type": bet.get("market_type"),
                "stake": money(bet.get("stake", 0)),
                "entry_price": bet.get("entry_price"),
                "target_profit": recovery.get("target_profit"),
                "drawdown_at_entry": recovery.get("drawdown"),
                "recovery_attempt_id": recovery.get("recovery_attempt_id"),
            }
            break
    try:
        recovery_target_multiplier = float(settings.get("SPORTS_RECOVERY_TARGET_PROFIT_MULTIPLIER", "1") or 1)
    except ValueError:
        recovery_target_multiplier = 1.0
    try:
        recovery_max_stake_pct = float(settings.get("SPORTS_RECOVERY_MAX_STAKE_PCT", "0.12") or 0.12)
    except ValueError:
        recovery_max_stake_pct = 0.12
    recovery_target_profit = money(recovery_drawdown * recovery_target_multiplier)
    recovery_bankroll_base = balance if balance > 0 else starting_balance
    try:
        recovery_min_daily_loss = float(settings.get("SPORTS_RECOVERY_MIN_DAILY_LOSS", "8") or 8)
    except ValueError:
        recovery_min_daily_loss = 8.0
    try:
        recovery_min_daily_loss_pct = float(settings.get("SPORTS_RECOVERY_MIN_DAILY_LOSS_PCT", "0.05") or 0.05)
    except ValueError:
        recovery_min_daily_loss_pct = 0.05
    recovery_effective_min_daily_loss = money(max(recovery_min_daily_loss, recovery_bankroll_base * recovery_min_daily_loss_pct))
    recovery_max_stake_tiers = settings.get("SPORTS_RECOVERY_MAX_STAKE_TIERS", "")
    recovery_effective_max_stake_pct = tiered_percent_for_value(recovery_bankroll_base, recovery_max_stake_tiers, recovery_max_stake_pct)
    recovery_max_stake = money(recovery_bankroll_base * recovery_effective_max_stake_pct)
    recovery_next_even = 0.0 if open_recovery_bet else money(min(recovery_target_profit, recovery_max_stake, max(0.0, balance)))
    recovery_fallback = {
        "enabled": settings.get("SPORTS_RECOVERY_STAKING_ENABLED", "true") == "true",
        "carryover_enabled": recovery_carryover,
        "active": (not open_recovery_bet) and recovery_drawdown >= recovery_effective_min_daily_loss,
        "pending": bool(open_recovery_bet),
        "open_recovery_bet": open_recovery_bet,
        "realized_profit": recovery_profit,
        "daily_realized_profit": report.get("daily_recovery_staking", {}).get("daily_realized_profit"),
        "drawdown": recovery_drawdown,
        "min_daily_loss": recovery_min_daily_loss,
        "min_daily_loss_pct": recovery_min_daily_loss_pct,
        "effective_min_daily_loss": recovery_effective_min_daily_loss,
        "bankroll_base": recovery_bankroll_base,
        "max_stake_pct": recovery_effective_max_stake_pct,
        "max_stake_tiers": recovery_max_stake_tiers,
        "target_profit": recovery_target_profit,
        "next_stake_even_money": recovery_next_even,
        "max_recovery_stake": recovery_max_stake,
    }
    recovery_state = {**recovery_fallback, **(report.get("daily_recovery_staking") or {})}
    live_campaign_state = report.get("live_campaign") or {}
    phase_two_state = (
        {"enabled": False, "active": False, "status": "retired_live_campaign"}
        if live_campaign_state.get("enabled")
        else {**(report.get("phase_two") or {}), **read_json(SPORTS_PHASE_TWO_STATE_FILE, {})}
    )
    return {
        "mode": sports_execution_mode,
        "starting_balance": starting_balance,
        "deposited_capital_estimate": deposited_capital_estimate,
        "balance": balance,
        "open_exposure": exposure,
        "equity_estimate": money(balance + exposure),
        "unit_sizing": {
            "percentage": unit_size_pct,
            "bankroll_base": unit_bankroll_base,
            "unit_size": effective_unit_size,
            "current_equity": current_sports_equity,
            "locked_for_day": unit_snapshot_valid,
            "session_key": unit_session_key,
            "snapshot_date": unit_local_date,
            "snapshot_reset_at": unit_reset_at,
            "snapshot_captured_at": stored_unit_snapshot.get("captured_at") if unit_snapshot_valid else None,
        },
        "paper_pnl_estimate": trading_pnl_estimate if sports_execution_mode == "live" else money(balance + exposure - starting_balance),
        "trading_pnl_estimate": trading_pnl_estimate,
        "open_bets_count": len(open_bets),
        "open_bets": sorted(open_bets, key=lambda bet: bet.get("placed_at", ""), reverse=True),
        "history": sorted(history, key=lambda bet: bet.get("settled_at", ""), reverse=True)[:500],
        "missed_fills_open": sorted(missed_fills_open, key=lambda row: row.get("missed_at") or row.get("placed_at", ""), reverse=True)[:100],
        "missed_fill_history": sorted(missed_fill_history, key=lambda row: row.get("settled_at", ""), reverse=True)[:500],
        "settled_count": wins + losses,
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / (wins + losses)) * 100, 1) if wins + losses else 0,
        "today_wins": today_wins,
        "today_losses": today_losses,
        "today_settled_count": today_wins + today_losses,
        "today_win_rate": round((today_wins / (today_wins + today_losses)) * 100, 1) if today_wins + today_losses else 0,
        "today_profit": today_profit,
        "settled_profit": settled_profit,
        "bot_today_profit": money(sum(bet.get("profit", 0) for bet in bot_today_history if "profit" in bet)),
        "bot_total_profit": money(sum(bet.get("profit", 0) for bet in bot_history if "profit" in bet)),
        "bot_today_wins": sum(1 for bet in bot_today_history if bet.get("result") == "WIN"),
        "bot_today_losses": sum(1 for bet in bot_today_history if bet.get("result") == "LOSS"),
        "daily_performance": build_daily_performance(bot_history),
        "bot_open_bets_count": len(bot_open_bets),
        "bot_open_bets": sorted(bot_open_bets, key=lambda bet: bet.get("placed_at", ""), reverse=True),
        "source_analytics": source_analytics,
        "user_bet_analytics": user_bet_analytics(open_bets, history),
        "aibetpicks_results": aibetpicks_results,
        "recovery": recovery_state,
        "phase_two": phase_two_state,
        "live_campaign": live_campaign_state,
        "avg_clv_vs_ask": round(sum(clv_values) / len(clv_values), 2) if clv_values else 0,
        "watchlist_count": len(watchlist),
        "report": report,
        "live_reconciliation": live_reconciliation,
        "live_audit": live_audit,
        "logs": list(reversed(read_tail(SPORTS_LOG_FILE, 80))),
        "log_analytics": build_log_analytics(SPORTS_LOG_FILE, "sports"),
    }


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Betting Dashboard</title>
  <style>
/* DASHBOARD_STYLES */
  </style>
</head>
<body>
  <a class="skip-link" href="#workspace">Skip to dashboard</a>
  <aside class="app-nav">
    <div class="app-brand"><span class="brand-mark" aria-hidden="true">P</span><div><strong>Polymaker</strong><span>Trading workspace</span></div></div>
    <div class="nav-label">WORKSPACE</div>
    <nav class="tabs" aria-label="Dashboard pages">
      <button type="button" data-view-tab="overview" onclick="showView('overview')"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg><span>Overview</span></button>
      <button type="button" data-view-tab="sports" onclick="showView('sports')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3h8v6a4 4 0 0 1-8 0V3Zm4 10v5m-5 3h10M8 5H4v3a4 4 0 0 0 4 4m8-7h4v3a4 4 0 0 1-4 4"/></svg><span>Sports</span></button>
      <button type="button" data-view-tab="crypto" onclick="showView('crypto')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 2 7 10-7 4-7-4 7-10Zm-7 13 7 7 7-7M12 2v14"/></svg><span>Crypto</span></button>
      <button type="button" data-view-tab="analytics" onclick="showAnalytics(localStorage.getItem('analyticsBot') || 'sports')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 3v17h17M8 15V9m5 6V5m5 10v-4"/></svg><span>Analytics</span></button>
      <button type="button" data-view-tab="control" onclick="showView('control')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h7m4 0h5M4 17h3m4 0h9"/><circle cx="13" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></svg><span>Controls</span></button>
      <button type="button" data-view-tab="logs" onclick="showView('logs')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4h13M8 12h13M8 20h13M3 4h.01M3 12h.01M3 20h.01"/></svg><span>Logs</span></button>
    </nav>
    <div class="nav-footer"><span class="nav-label">EXECUTION MODES</span><div>Sports <span id="sports-header-mode" class="pill">paper</span></div><div>Crypto <span id="crypto-header-mode" class="pill">paper</span></div><small>All times America/Chicago</small></div>
  </aside>
  <header class="app-header">
    <div class="status-row">
      <div class="system-strip" aria-label="System health summary">
        <span id="system-health" class="health-chip" data-testid="system-health">Checking services</span>
        <span id="system-cash" class="health-chip">Cash --</span>
        <span id="system-exposure" class="health-chip" data-testid="system-exposure">Exposure --</span>
        <span id="system-profit" class="health-chip">Bot Today --</span>
      </div>
      <details class="system-details"><summary>System details</summary><div>
        <span id="system-cycle" class="health-chip">Sports capacity --</span>
        <span id="system-crypto-cycle" class="health-chip">Crypto opportunities --</span>
        <span id="system-storage" class="health-chip">Storage --</span>
      </div></details>
    </div>
    <div id="generated" class="sub">Connecting to live state...</div>
  </header>
  <main class="grid" id="workspace" tabindex="-1">
    <div class="workspace-heading"><div><div class="eyebrow" id="page-eyebrow">ACCOUNT &amp; PERFORMANCE</div><h1 id="page-title">Overview</h1><p id="page-description">Your bankroll, exposure and automated results in one place.</p></div><button type="button" class="refresh-button" onclick="refresh()">Refresh data <span aria-hidden="true">↻</span></button></div>
    <div class="view active" data-view="overview">
      <section class="performance-overview" aria-labelledby="overview-performance-title">
        <div class="section-title">
          <div class="performance-toolbar">
            <div class="section-title"><h2 id="overview-performance-title">Current Account Equity &amp; Automated Bot Performance</h2></div>
            <div class="range-buttons" data-performance-range="overview" aria-label="Overview chart range">
              <button type="button" data-range="7d" onclick="setPerformanceRange('overview','7d')">7D</button>
              <button type="button" class="active" data-range="30d" onclick="setPerformanceRange('overview','30d')">30D</button>
              <button type="button" data-range="90d" onclick="setPerformanceRange('overview','90d')">90D</button>
              <button type="button" data-range="all" onclick="setPerformanceRange('overview','all')">All</button>
            </div>
          </div>
        </div>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">Current Account Equity</div><div id="overview-bankroll" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Bot Period P&amp;L</div><div id="overview-period-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Regular Sports Bot P&amp;L</div><div id="overview-period-sports" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Crypto Bot P&amp;L</div><div id="overview-period-crypto" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Period ROI</div><div id="overview-period-roi" class="value">0.0%</div></div>
          <div class="stat"><div class="label">Max Drawdown</div><div id="overview-period-drawdown" class="value">$0.00</div></div>
        </div>
        <div class="notice">Current equity includes account cash and open positions. Period P&amp;L and the reconstructed curve use regular sports bot plus crypto bot settlements only; deposits, withdrawals, user live bets, and legacy imported picks are not treated as bot performance.</div>
        <div class="performance-grid">
          <div class="performance-chart">
            <h3>Bot-Only Reconstructed Equity</h3>
            <div class="chart-wrap"><canvas id="overview-bankroll-chart" role="img" aria-label="Bot-only reconstructed equity over time"></canvas></div>
            <div id="overview-bankroll-summary" class="performance-summary">Waiting for bankroll history.</div>
          </div>
          <div class="performance-chart">
            <h3>Automated Bot Daily P&amp;L</h3>
            <div class="chart-wrap"><canvas id="overview-daily-chart" role="img" aria-label="Automated sports and crypto bot daily profit and loss"></canvas></div>
            <div id="overview-daily-summary" class="performance-summary">Waiting for settled bot results.</div>
          </div>
        </div>
      </section>

      <section class="bot-overview">
        <div class="section-title">
          <h2>Sports Bot</h2>
          <span id="overview-sports-status" class="health-chip">Checking</span>
        </div>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">Regular Bot Today P&amp;L</div><div id="overview-sports-daily" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Regular Bot All-Time P&amp;L</div><div id="overview-sports-total" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Active Bets</div><div id="overview-sports-open" class="value">0 / 3</div></div>
          <div class="stat"><div class="label">Unit Size</div><div id="overview-sports-unit-size" class="value">$15.00</div></div>
          <div class="stat"><div class="label">Maximum Per Market</div><div id="overview-sports-unit-cap" class="value">5u</div></div>
          <div class="stat"><div class="label">Daily Loss Room</div><div id="overview-sports-loss-room" class="value">$300.00</div></div>
        </div>
        <div class="notice">Regular bot results exclude legacy imported picks and user-entered live bets. The source ledger below includes every tracked sports settlement; deposits and withdrawals are not betting P&amp;L.</div>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">All Sports Ledger P&amp;L</div><div id="overview-sports-account-pnl" class="value">$0.00</div></div>
          <div class="stat"><div class="label">All Sports MTD P&amp;L</div><div id="overview-sports-account-mtd" class="value">$0.00</div></div>
          <div class="stat"><div class="label">AIBetPicks P&amp;L</div><div id="overview-sports-aibetpicks-pnl" class="value">$0.00</div><div id="overview-sports-aibetpicks-units" class="muted"></div></div>
          <div class="stat"><div class="label">User Live P&amp;L</div><div id="overview-sports-user-pnl" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Historical System Test P&amp;L</div><div id="overview-sports-test-pnl" class="value">$0.00</div></div>
        </div>
        <div id="overview-sports-campaign-cards" class="campaign-lane-grid"></div>
        <div class="table-scroll"><table>
          <thead><tr><th class="num">Bot</th><th>Active Bet</th><th>Pick</th><th class="num">Stake</th><th class="num">Price</th><th class="num">Net Edge</th><th class="num">Confidence</th></tr></thead>
          <tbody id="overview-sports-bets"></tbody>
        </table></div>
        <div id="overview-sports-strategy" class="strategy-summary">Live moneylines and spreads · fee-adjusted quality · maximum three open bets.</div>
      </section>

      <section class="bot-overview">
        <div class="section-title">
          <h2>Crypto Bot</h2>
          <span id="overview-crypto-status" class="health-chip">Checking</span>
        </div>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">Today P&amp;L</div><div id="overview-crypto-daily" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Total P&amp;L</div><div id="overview-crypto-total" class="value">$0.00</div></div>
          <div class="stat" data-testid="crypto-current-strategy-performance"><div class="label">Current Strategy P&amp;L</div><div id="overview-crypto-current-pnl" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Current Record</div><div id="overview-crypto-current-record" class="value">Collecting</div></div>
          <div class="stat"><div class="label">Current ROI</div><div id="overview-crypto-current-roi" class="value">0.00%</div></div>
          <div class="stat"><div class="label">Active Bets</div><div id="overview-crypto-open" class="value">0 / 1</div></div>
          <div class="stat"><div class="label">Unit Size</div><div id="overview-crypto-cycle" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Maximum Size</div><div id="overview-crypto-goal" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Daily Loss Room</div><div id="overview-crypto-loss-room" class="value">$300.00</div></div>
        </div>
        <div id="overview-crypto-settlement-alert" class="strategy-summary" style="display:none;"></div>
        <div id="overview-crypto-network-alert" class="strategy-summary" style="display:none;"></div>
        <div id="overview-crypto-campaign-cards" class="campaign-lane-grid"></div>
        <div class="table-scroll"><table>
          <thead><tr><th class="num">Bot</th><th>Active Bet</th><th>Side</th><th class="num">Stake</th><th class="num">Price</th><th class="num">Net Edge</th><th class="num">Calibrated Win Prob</th><th class="num">90% Prob Interval</th><th class="num">Edge Certainty</th></tr></thead>
          <tbody id="overview-crypto-bets"></tbody>
        </table></div>
        <div id="overview-crypto-strategy" class="strategy-summary">Live 15-minute targets · fee-adjusted quality · maximum one automated bet.</div>
      </section>
    </div>

    <section class="view" data-view="control">
      <h2>Control Center</h2>
      <div class="card" style="margin-bottom:16px;">
        <h3>Crypto Bot Configuration</h3>
        <div class="notice">Set how many independent crypto bots may run. Each bot takes qualified opportunities continuously with the probability/edge unit ladder, one open position per bot, and its own high-water drawdown ledger.</div>
        <div class="form-grid" style="padding-top:12px;">
          <!-- controls:
            CONTROL_CRYPTO_15M_CAMPAIGN_BOT_COUNT
            -->
        </div>
        <div style="padding-top:10px; display:flex; gap:8px; flex-wrap:wrap;">
          <button class="primary" onclick="saveCryptoBotConfiguration()">Save Crypto Bot Configuration</button>
          <button onclick="showView('crypto-control')">Open All Crypto Settings</button>
        </div>
        <div id="control-crypto-bot-status" class="notice">Crypto bot configuration loads from crypto_settings.json.</div>
      </div>
      <div class="controls">
        <div>
          <div class="form-grid">
            <!-- controls:
            SPORTS_EXECUTION_MODE ALLOW_LIVE_TRADING SPORTS_STARTING_BALANCE
            SPORTS_SCAN_INTERVAL_MINUTES SPORTS_LIVE_CAMPAIGN_BOT_COUNT
            SPORTS_LIVE_CAMPAIGN_MAX_OPEN SPORTS_UNIT_STAKING_ENABLED SPORTS_UNIT_SIZE_PCT
            SPORTS_UNIT_MAX_PER_MARKET SPORTS_UNIT_INCREMENT SPORTS_AIBETPICKS_ENABLED
            SPORTS_MODEST_RECOVERY_ENABLED SPORTS_GAME_ODDS_ENABLED SPORTS_GAME_ODDS_API_KEY
            SPORTS_GAME_ODDS_MAX_OPEN SPORTS_GAME_ODDS_UNIT_SIZE SPORTS_GAME_ODDS_MAX_UNITS
            SPORTS_GAME_ODDS_PREGAME_MIN_EDGE SPORTS_GAME_ODDS_LIVE_MIN_EDGE SPORTS_GAME_ODDS_MIN_CONFIDENCE
            SPORTS_GAME_ODDS_MIN_PRO_SCORE SPORTS_GAME_ODDS_MIN_FINAL_SCORE SPORTS_GAME_ODDS_MIN_BOOK_FAMILIES
            SPORTS_MLB_MONEYLINE_MAX_UNITS SPORTS_LIVE_MISSING_STATE_MAX_UNITS SPORTS_AI_MEDIUM_RISK_MAX_UNITS
            SPORTS_AI_UNRESOLVED_MAX_UNITS SPORTS_AI_OVERRIDE_MAX_UNITS
            SPORTS_POST_AI_ODDS_REVALIDATION_ENABLED SPORTS_POST_AI_ODDS_REVALIDATION_MIN_RAW_UNITS
            SPORTS_POST_AI_EDGE_STABILITY_SECONDS SPORTS_POST_AI_REVALIDATION_MAX_CALLS_PER_SCAN
            SPORTS_POST_AI_REVALIDATION_FAILURE_MAX_UNITS SPORTS_POST_AI_LIVE_MIN_RAW_UNITS
            SPORTS_POST_AI_EDGE_TOLERANCE_PP SPORTS_POST_AI_MAX_ADVERSE_PRICE_MOVE_CENTS
            SPORTS_REQUIRE_AUTHORITATIVE_LIVE_DERIVATIVE_STATE SPORTS_ALLOW_SCORE_ONLY_LIVE_DERIVATIVES_1U
            SPORTS_NEGATIVE_CLV_FLAG_MIN_BETS SPORTS_NEGATIVE_CLV_FLAG_THRESHOLD_CENTS
            SPORTS_AI_SKIP_SEARCH_WITH_AUTHORITATIVE_STATE SPORTS_AI_DETERMINISTIC_BYPASS_ENABLED
            SPORTS_AI_REVIEW_CACHE_LIVE_SECONDS SPORTS_AI_REVIEW_CACHE_PREGAME_SECONDS
            SPORTS_HOT_RECHECK_ENABLED SPORTS_HOT_RECHECK_INTERVAL_SECONDS SPORTS_HOT_RECHECK_MAX_BURST_SCANS
            SPORTS_HOT_RECHECK_EDGE_WINDOW_PP SPORTS_HOT_RECHECK_MAX_CANDIDATES
            SPORTS_LOW_EDGE_CLV_AUTO_TIGHTEN_ENABLED SPORTS_LOW_EDGE_CLV_MIN_SAMPLES
            SPORTS_LOW_EDGE_CLV_THRESHOLD_CENTS SPORTS_LOW_EDGE_CLV_RESTORED_MIN_EDGE
            -->
            <div id="sports-unit-control-table" data-control-block="sports-unit-requirements" style="grid-column:1/-1; overflow-x:auto; padding:10px; border:1px solid var(--line); border-radius:12px;">
              <div style="font-weight:700; margin-bottom:8px;">Sports unit requirements</div>
              <table style="width:100%; border-collapse:collapse; min-width:760px;">
                <thead><tr><th>Units</th><th>Net edge &gt;</th><th>Confidence ≥</th><th>Pro score ≥</th><th>Final score ≥</th><th>Book families ≥</th></tr></thead>
                <tbody>
                  <tr><td>1u</td><td><input id="SPORTS_UNIT_1_MIN_EDGE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_1_MIN_CONFIDENCE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_1_MIN_PRO_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_1_MIN_FINAL_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_1_MIN_BOOK_FAMILIES" inputmode="numeric" min="2"></td></tr>
                  <tr><td>2u</td><td><input id="SPORTS_UNIT_2_MIN_EDGE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_2_MIN_CONFIDENCE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_2_MIN_PRO_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_2_MIN_FINAL_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_2_MIN_BOOK_FAMILIES" inputmode="numeric" min="2"></td></tr>
                  <tr><td>3u</td><td><input id="SPORTS_UNIT_3_MIN_EDGE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_3_MIN_CONFIDENCE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_3_MIN_PRO_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_3_MIN_FINAL_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_3_MIN_BOOK_FAMILIES" inputmode="numeric" min="2"></td></tr>
                  <tr><td>4u</td><td><input id="SPORTS_UNIT_4_MIN_EDGE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_4_MIN_CONFIDENCE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_4_MIN_PRO_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_4_MIN_FINAL_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_4_MIN_BOOK_FAMILIES" inputmode="numeric" min="2"></td></tr>
                  <tr><td>5u</td><td><input id="SPORTS_UNIT_5_MIN_EDGE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_5_MIN_CONFIDENCE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_5_MIN_PRO_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_5_MIN_FINAL_SCORE" inputmode="decimal"></td><td><input id="SPORTS_UNIT_5_MIN_BOOK_FAMILIES" inputmode="numeric" min="2"></td></tr>
                </tbody>
              </table>
              <div class="notice">The 1.5u, 2.5u, 3.5u, and 4.5u criteria are calculated as exact midpoints between the surrounding whole-unit rows. The 0.5u live size is reserved for qualified bets reduced by probability or risk; looser 0.5u entries remain shadow-only until their segment passes walk-forward promotion.</div>
            </div>
            <!-- controls:
            SPORTS_LIVE_CAMPAIGN_SAME_GAME_MAX_POSITIONS SPORTS_LIVE_CAMPAIGN_SAME_DIRECTION_MAX_POSITIONS
            SPORTS_LIVE_CAMPAIGN_SAME_GAME_RISK_CAP SPORTS_LIVE_CAMPAIGN_SAME_GAME_RISK_PCT
            SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_CONSERVATIVE_EDGE
            SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_CONFIDENCE
            SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_PRO_SCORE
            SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_FINAL_SCORE
            SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_BOOK_FAMILIES
            SPORTS_LIVE_CAMPAIGN_SECOND_POSITION_MIN_SHARP_BOOKS SPORTS_LIVE_CAMPAIGN_OPPOSITE_HEDGE_ENABLED
            SPORTS_LIVE_CAMPAIGN_HEDGE_MIN_WORST_CASE_PROFIT SPORTS_LIVE_CAMPAIGN_MARKET_TYPES
            SPORTS_LIVE_CAMPAIGN_SPREAD_MIN_CONSERVATIVE_EDGE
            SPORTS_LIVE_CAMPAIGN_MID_LIVE_SPREAD_MIN_CONSERVATIVE_EDGE SPORTS_LIVE_CAMPAIGN_TOTAL_MIN_TIER
            SPORTS_LIVE_CAMPAIGN_TOTAL_MIN_CONSERVATIVE_EDGE SPORTS_LIVE_CAMPAIGN_TOTAL_LINE_TOLERANCE
            SPORTS_LIVE_CAMPAIGN_TOTAL_MIN_BOOK_FAMILIES SPORTS_LIVE_CAMPAIGN_TOTAL_MIN_SHARP_BOOKS
            SPORTS_MLB_TOTAL_MIN_PRICE_CENTS SPORTS_MLB_TOTAL_MAX_PRICE_CENTS SPORTS_MLB_TOTAL_MIN_EDGE
            SPORTS_MLB_TOTAL_MIN_CONFIDENCE SPORTS_MLB_TOTAL_MIN_PRO_SCORE SPORTS_MLB_TOTAL_MIN_FINAL_SCORE
            SPORTS_MLB_TOTAL_MIN_BOOK_FAMILIES SPORTS_MLB_TOTAL_MIN_SHARP_BOOKS SPORTS_MLB_TOTAL_MAX_UNITS
            SPORTS_PRICING_V2_ENABLED SPORTS_PRICING_V2_ENFORCEMENT SPORTS_PRICING_V2_MIN_BOOK_FAMILIES
            SPORTS_PRICING_V2_MAX_BOOK_AGE_MINUTES SPORTS_PRICING_V2_MIN_UNCERTAINTY_PP
            SPORTS_PRICING_V2_REQUIRE_CONSERVATIVE_EDGE SPORTS_KALSHI_STREAM_ENABLED
            SPORTS_KALSHI_STREAM_REQUIRE_FOR_LIVE SPORTS_KALSHI_STREAM_MAX_AGE_SECONDS
            SPORTS_DECISION_ANALYTICS_ENABLED SPORTS_LIVE_DATA_PROVIDER SPORTRADAR_API_KEY
            SPORTS_SPORTRADAR_ACCESS_LEVEL SPORTS_SPORTRADAR_VERSION SPORTS_SPORTRADAR_FAILURE_THRESHOLD
            SPORTS_SPORTRADAR_CIRCUIT_COOLDOWN_SECONDS SPORTS_LIVE_DATA_MAX_GAMES_PER_SCAN
            SPORTS_PUBLIC_STATE_CACHE_SECONDS SPORTS_PUBLIC_STATE_FAILURE_THRESHOLD
            SPORTS_PUBLIC_STATE_CIRCUIT_COOLDOWN_SECONDS SPORTS_GAME_STATE_MODEL_ENABLED
            SPORTS_NO_MATCH_BACKOFF_ENABLED SPORTS_NO_MATCH_BACKOFF_MINUTES SPORTS_QUIET_HOURS_ENABLED
            SPORTS_QUIET_START_HOUR SPORTS_QUIET_END_HOUR SPORTS_ODDS_MONTHLY_CREDIT_LIMIT
            SPORTS_ODDS_PACING_CREDIT_TARGET SPORTS_ODDS_HARD_CREDIT_LIMIT SPORTS_ODDS_ADAPTIVE_PACING_ENABLED
            SPORTS_ODDS_PACING_WINDOW_HOURS SPORTS_ODDS_PACING_TRIGGER_RATIO SPORTS_ODDS_PACING_MAX_MULTIPLIER
            SPORTS_ODDS_PROVIDER_RESERVE_CREDITS SPORTS_ODDS_SHADOW_BUDGET_GUARD_ENABLED
            SPORTS_OVERNIGHT_ODDS_ROUTING_ENABLED SPORTS_OVERNIGHT_ODDS_START_HOUR
            SPORTS_OVERNIGHT_ODDS_END_HOUR SPORTS_OVERNIGHT_OVERSEAS_RESEARCH_CACHE_MINUTES
            SPORTS_OVERNIGHT_PAUSE_CRICKET_SHADOW_ON_BUDGET_PRESSURE
            SPORTS_OVERSEAS_DERIVATIVE_SHADOW_BUDGET_GUARD_ENABLED SPORTS_ODDS_CACHE_MAX_HOURS
            SPORTS_LIVE_ODDS_CACHE_MAX_MINUTES SPORTS_LIVE_TENNIS_REFRESH_SECONDS SPORTS_ODDS_PRIMARY_REGION
            SPORTS_ODDS_SECONDARY_REGIONS SPORTS_ODDS_SECONDARY_ON_DEMAND_ENABLED
            SPORTS_ODDS_SECONDARY_MIN_BOOKMAKERS SPORTS_ODDS_ACTIONABLE_EVENT_FILTER_ENABLED
            SPORTS_ODDS_UNCHANGED_BACKOFF_MINUTES SPORTS_ODDS_PROVIDER_RESET_DAY SPORTS_ODDS_SKIP_INACTIVE
            SPORTS_SCORES_ENABLED SPORTS_SCORES_CACHE_MINUTES SPORTS_SCORES_FALLBACK_CACHE_MINUTES
            SPORTS_SCORES_DAYS_FROM SPORTS_EARLY_RESULT_CHECK_ENABLED SPORTS_EARLY_RESULT_SCORE_CACHE_MINUTES
            SPORTS_EARLY_RESULT_MIN_STAKE SPORTS_EARLY_RESULT_MIN_STAKE_PCT
            SPORTS_EARLY_RESULT_MARKET_PRICE_CENTS SPORTS_EARLY_RESULT_MIN_MINUTES SPORTS_KEYS
            SPORTS_ALL_ACTIVE_ENABLED SPORTS_ALL_ACTIVE_INCLUDE_OUTRIGHTS SPORTS_ALL_ACTIVE_EXCLUDED_GROUPS
            SPORTS_DYNAMIC_KALSHI_SERIES_ENABLED SPORTS_KALSHI_SERIES_PREFIXES SPORTS_KALSHI_MARKET_SERIES
            SPORTS_KALSHI_DATE_MAX_DAYS SPORTS_MIN_ENTRY_PRICE_CENTS SPORTS_PRICE_DISCIPLINE_ENABLED
            SPORTS_PLUS_PRICE_MAX_CENTS SPORTS_FAVORITE_MAX_PRICE_CENTS SPORTS_HEAVY_FAVORITE_MAX_PRICE_CENTS
            SPORTS_LOW_PRICE_GUARD_ENABLED SPORTS_MOONSHOT_MIN_ENTRY_CENTS SPORTS_LOW_PRICE_STRICT_MAX_CENTS
            SPORTS_LOW_PRICE_FOCUS_MIN_CENTS SPORTS_LOW_PRICE_FOCUS_MAX_CENTS SPORTS_LOW_PRICE_MIN_EDGE
            SPORTS_LOW_PRICE_MIN_CONFIDENCE SPORTS_LOW_PRICE_MIN_PRO_SCORE SPORTS_LOW_PRICE_MIN_FINAL_SCORE
            SPORTS_MOONSHOT_ELITE_MIN_EDGE SPORTS_MOONSHOT_ELITE_MIN_CONFIDENCE
            SPORTS_MOONSHOT_ELITE_MIN_PRO_SCORE SPORTS_MOONSHOT_ELITE_MIN_FINAL_SCORE SPORTS_FAVORITE_MIN_EDGE
            SPORTS_FAVORITE_MIN_CONFIDENCE SPORTS_FAVORITE_MIN_PRO_SCORE SPORTS_FAVORITE_MIN_FINAL_SCORE
            SPORTS_HEAVY_FAVORITE_MIN_EDGE SPORTS_HEAVY_FAVORITE_MIN_CONFIDENCE
            SPORTS_HEAVY_FAVORITE_MIN_PRO_SCORE SPORTS_HEAVY_FAVORITE_MIN_FINAL_SCORE
            SPORTS_FAVORITE_WATCH_ENABLED SPORTS_FAVORITE_WATCH_BOOK_ODDS_MAX
            SPORTS_FAVORITE_WATCH_MIN_MODEL_PROB SPORTS_FAVORITE_WATCH_ENTRY_MIN_CENTS
            SPORTS_FAVORITE_WATCH_ENTRY_MAX_CENTS SPORTS_FAVORITE_WATCH_MIN_CONFIDENCE
            SPORTS_FAVORITE_WATCH_MIN_PRO_SCORE SPORTS_FAVORITE_WATCH_MAX_MINUTES_AFTER_START
            SPORTS_FAVORITE_WATCH_STAKE_MULTIPLIER SPORTS_FAVORITE_WATCH_MAX_STAKE_PCT
            SPORTS_FAVORITE_WATCH_EXPIRE_HOURS SPORTS_FAVORITE_WATCH_REQUIRE_LIVE_SCORE SPORTS_EDGE_THRESHOLD
            SPORTS_PRO_MODE_ENABLED SPORTS_PRO_MIN_EDGE SPORTS_PRO_MIN_SCORE SPORTS_PRO_MIN_CONFIDENCE
            SPORTS_FINAL_SCORE_ENABLED SPORTS_FINAL_SCORE_MIN SPORTS_FINAL_SCORE_LOW_EDGE_MIN
            SPORTS_FINAL_SCORE_LOW_EDGE_MIN_SCORE SPORTS_FINAL_SCORE_LOW_EDGE_MIN_CONFIDENCE
            SPORTS_FINAL_SCORE_LOW_EDGE_MIN_PRO_SCORE SPORTS_LIVE_ELITE_RETRY_ENABLED
            SPORTS_LIVE_ELITE_RETRY_MAX_BUMP_CENTS SPORTS_LIVE_ELITE_RETRY_MIN_EDGE
            SPORTS_LIVE_ELITE_RETRY_MIN_CONFIDENCE SPORTS_LIVE_ELITE_RETRY_MIN_PRO_SCORE
            SPORTS_LIVE_ELITE_RETRY_MIN_FINAL_SCORE SPORTS_LIVE_ELITE_RETRY_MIN_PRICE_CENTS
            SPORTS_LIVE_ELITE_RETRY_MAX_PRICE_CENTS SPORTS_FOK_RECHECK_ENABLED SPORTS_FOK_RECHECK_ATTEMPTS
            SPORTS_FOK_RECHECK_INTERVAL_SECONDS SPORTS_FOK_RECHECK_MAX_WAIT_SECONDS
            SPORTS_CLV_BOOK_IDENTITY_RETRY_MINUTES SPORTS_CLV_BOOK_IDENTITY_MAX_ATTEMPTS SPORTS_MIN_CONFIDENCE
            SPORTS_EDGE_MARKET_TYPES SPORTS_MAX_PAPER_BETS_PER_SCAN SPORTS_BOT_PICK_MIN_CONFIDENCE
            BOT_PICKS_ENABLED BOT_PICKS_ACTIVE_KEYS BOT_PICKS_PUBLIC_FADE_MIN_PCT BOT_PICKS_MORNING_HOUR
            BOT_PICKS_AFTERNOON_HOUR BOT_PICKS_EVENING_HOUR SPORTS_BOT_PICK_FIXED_STAKE
            SPORTS_FADE_PUBLIC_BOT_STAKE SPORTS_BOT_PICK_REQUIRE_ACTIONABLE SPORTS_BOT_PICK_ALWAYS_PAPER
            SPORTS_BOT_PICK_MARKETLESS_PAPER
            SPORTS_BOT_PICK_MAX_LINE_IMPROVEMENT SPORTS_TRACKING_EXPIRE_HOURS SPORTS_STALE_OPEN_HOURS
            SPORTS_MAX_STAKE_PCT SPORTS_KELLY_FRACTION SPORTS_SPREAD_STAKE_MULTIPLIER
            SPORTS_SPREAD_MIN_CONFIDENCE SPORTS_SPREAD_MIN_PRO_SCORE SPORTS_SPREAD_MIN_FINAL_SCORE
            SPORTS_SELECTIVE_STAKING_ENABLED SPORTS_SELECTIVE_BASE_STAKE SPORTS_SELECTIVE_BASE_STAKE_PCT
            SPORTS_SELECTIVE_GAME_EXPOSURE_PCT SPORTS_SELECTIVE_MIN_EDGE SPORTS_SELECTIVE_MIN_CONFIDENCE
            SPORTS_SELECTIVE_MIN_PRO_SCORE SPORTS_FINAL_SMALL_EDGE_ENABLED SPORTS_FINAL_SMALL_EDGE_MIN_EDGE
            SPORTS_FINAL_SMALL_EDGE_MIN_CONFIDENCE SPORTS_FINAL_SMALL_EDGE_MIN_PRO_SCORE
            SPORTS_FINAL_SMALL_EDGE_MIN_FINAL_SCORE SPORTS_SMALL_EDGE_SCALING_ENABLED
            SPORTS_SMALL_EDGE_STRONG_MIN_EDGE SPORTS_SMALL_EDGE_STRONG_MIN_CONFIDENCE
            SPORTS_SMALL_EDGE_STRONG_MIN_PRO_SCORE SPORTS_SMALL_EDGE_STRONG_MIN_FINAL_SCORE
            SPORTS_SMALL_EDGE_STRONG_STAKE_PCT SPORTS_SMALL_EDGE_STRONG_MAX_STAKE
            SPORTS_SMALL_EDGE_ELITE_MIN_EDGE SPORTS_SMALL_EDGE_ELITE_MIN_CONFIDENCE
            SPORTS_SMALL_EDGE_ELITE_MIN_PRO_SCORE SPORTS_SMALL_EDGE_ELITE_MIN_FINAL_SCORE
            SPORTS_SMALL_EDGE_ELITE_STAKE_PCT SPORTS_SMALL_EDGE_ELITE_MAX_STAKE
            SPORTS_SMALL_EDGE_PHASE_TWO_EXPOSURE_PCT SPORTS_SELECTIVE_WAIT_FOR_SETTLEMENT
            SPORTS_SELECTIVE_ELITE_BYPASS_CONFIDENCE SPORTS_SELECTIVE_ELITE_BYPASS_EDGE
            SPORTS_SELECTIVE_ELITE_BYPASS_PRO_SCORE SPORTS_LOSS_STREAK_STAKING_ENABLED
            SPORTS_LOSS_STREAK_MULTIPLIER SPORTS_LOSS_STREAK_MAX_MULTIPLIER SPORTS_LOSS_STREAK_MAX_STAKE_PCT
            SPORTS_TOTAL_LIFELINE_ENABLED SPORTS_TOTAL_LIFELINE_MAX_ADD_PCT
            SPORTS_TOTAL_LIFELINE_MAX_OPEN_STAKE SPORTS_TOTAL_LIFELINE_STAKE_MULTIPLIER
            SPORTS_TOTAL_LIFELINE_MIN_EDGE SPORTS_TOTAL_LIFELINE_MIN_CONFIDENCE
            SPORTS_TOTAL_LIFELINE_MIN_PRO_SCORE SPORTS_TOTAL_LIFELINE_MIN_FINAL_SCORE
            SPORTS_RECOVERY_STAKING_ENABLED SPORTS_RECOVERY_CARRYOVER_ENABLED
            SPORTS_RECOVERY_HIGH_WATER_ENABLED SPORTS_RECOVERY_ONLY_WHEN_ACTIVE
            SPORTS_RECOVERY_DISABLE_WHEN_DAILY_GREEN SPORTS_RECOVERY_PAUSE_FOR_PHASE_TWO
            SPORTS_RECOVERY_PHASE_TWO_OFFSET_ENABLED SPORTS_RECOVERY_MIN_DAILY_LOSS
            SPORTS_RECOVERY_MIN_DAILY_LOSS_PCT SPORTS_RECOVERY_MIN_LOSS_STREAK SPORTS_RECOVERY_MIN_CONFIDENCE
            SPORTS_RECOVERY_MIN_EDGE SPORTS_RECOVERY_MIN_PRO_SCORE SPORTS_RECOVERY_REQUIRE_EXTRA_QUALITY
            SPORTS_RECOVERY_ALL_AVAILABLE_ENABLED SPORTS_RECOVERY_TARGET_PROFIT_MULTIPLIER
            SPORTS_RECOVERY_MAX_STAKE_PCT SPORTS_RECOVERY_MAX_STAKE_TIERS SPORTS_RECOVERY_MAX_DAILY_LOSS_PCT
            SPORTS_RECOVERY_MAX_SPORT_EXPOSURE_PCT SPORTS_RECOVERY_MARKET_TYPES SPORTS_RECOVERY_BASKET_ENABLED
            SPORTS_RECOVERY_BASKET_ONLY_WHEN_PHASE_TWO_PENDING SPORTS_RECOVERY_BASKET_MAX_OPEN
            SPORTS_RECOVERY_BASKET_MAX_TOTAL_STAKE_PCT SPORTS_RECOVERY_BASKET_MAX_STAKE_PCT
            SPORTS_RECOVERY_BASKET_MIN_STAKE SPORTS_RECOVERY_BASKET_TARGET_PROFIT_MULTIPLIER
            SPORTS_RECOVERY_BASKET_MIN_EDGE SPORTS_RECOVERY_BASKET_MIN_CONFIDENCE
            SPORTS_RECOVERY_BASKET_MIN_PRO_SCORE SPORTS_RECOVERY_BASKET_MIN_FINAL_SCORE
            SPORTS_RECOVERY_BASKET_MAX_SAME_GAME_OPEN SPORTS_RECOVERY_BASKET_MARKET_TYPES
            SPORTS_EDGE_BOOST_CONFIDENCE SPORTS_EDGE_BOOST_MIN_EDGE SPORTS_EDGE_STAKE_MULTIPLIER
            SPORTS_DAILY_PROFIT_TARGET_PCT SPORTS_DAILY_PROFIT_TARGET_MIN SPORTS_DAILY_PROFIT_TARGET_MAX
            SPORTS_PROFIT_LOCK_START_MULTIPLE SPORTS_PROFIT_LOCK_FULL_MULTIPLE
            SPORTS_PROFIT_LOCK_STAKE_MULTIPLIER SPORTS_PROFIT_LOCK_ELITE_MULTIPLIER
            SPORTS_PROFIT_LOCK_MIN_CONFIDENCE SPORTS_PROFIT_LOCK_MIN_EDGE SPORTS_PROFIT_LOCK_MIN_PRO_SCORE
            SPORTS_PROFIT_LOCK_MIN_STAKE SPORTS_PROFIT_LOCK_MAX_STAKE SPORTS_BOT_PICK_STAKE_PCT
            SPORTS_MAX_GAME_EXPOSURE_PCT SPORTS_MAX_SPORT_EXPOSURE_PCT SPORTS_SMALL_EDGE_SAME_GAME_CAP_ENABLED
            SPORTS_SMALL_EDGE_MAX_SAME_GAME_OPEN SPORTS_SELECTIVE_MAX_SAME_GAME_OPEN SPORTS_TRACK_MISSED_FILLS
            SPORTS_MISSED_FILL_HISTORY_LIMIT SPORTS_HEDGE_ENABLED SPORTS_ALLOW_IN_GAME_EDGES
            SPORTS_IN_GAME_GRACE_MINUTES SPORTS_REQUIRE_LIVE_SCORE_FOR_IN_GAME
            SPORTS_LIVE_GAME_BETTING_ENABLED SPORTS_LIVE_GAME_MAX_ODDS_AGE_MINUTES
            SPORTS_LIVE_GAME_MAX_MINUTES_AFTER_START SPORTS_LIVE_GAME_MIN_EDGE SPORTS_LIVE_GAME_MIN_CONFIDENCE
            SPORTS_LIVE_GAME_MIN_PRO_SCORE SPORTS_LIVE_GAME_NO_SCORE_GRACE_MINUTES SPORTS_PREGAME_ENABLED
            SPORTS_PREGAME_ALL_OPEN_ENABLED SPORTS_PREGAME_MAX_MINUTES_BEFORE_START
            SPORTS_PREGAME_FAST_POLL_MINUTES_BEFORE_START SPORTS_PREGAME_ODDS_CACHE_MAX_MINUTES
            SPORTS_PREGAME_FAST_ODDS_CACHE_MAX_MINUTES SPORTS_PREGAME_FAR_POLL_MINUTES_BEFORE_START
            SPORTS_PREGAME_FAR_ODDS_CACHE_MAX_MINUTES SPORTS_PREGAME_MAX_OPEN SPORTS_PROTECTED_CONFIDENCE
            SPORTS_DIRECT_OPPOSITE_MIN_EDGE_ADVANTAGE SPORTS_TRUE_HEDGE_MIN_PROFIT SPORTS_AI_VOTING_ENABLED
            SPORTS_LOCAL_AI_ENABLED SPORTS_LOCAL_AI_MODEL SPORTS_LOCAL_AI_URL SPORTS_OPENAI_ENABLED
            SPORTS_OPENAI_API_KEY SPORTS_OPENAI_MODEL SPORTS_GROK_FALLBACK_ENABLED SPORTS_ENABLE_GROK
            SPORTS_GROK_SEARCH_ENABLED SPORTS_GROK_WEB_SEARCH_ENABLED SPORTS_GROK_X_SEARCH_ENABLED
            BOT_PICKS_GROK_SEARCH_ENABLED ODDS_API_KEY XAI_API_KEY XAI_MODEL XAI_SEARCH_MODEL LIVE_MAX_STAKE
            LIVE_MAX_STAKE_PCT LIVE_MAX_STAKE_CAP SPORTS_LIVE_ORDER_ENABLED SPORTS_LIVE_EDGE_ORDER_ENABLED
            SPORTS_LIVE_FALLBACK_TO_PAPER SPORTS_RECONCILE_LIVE_ON_SCAN SPORTS_LIVE_AUDIT_ENABLED
            SPORTS_LIVE_AUDIT_INTERVAL_MINUTES SPORTS_LIVE_AUDIT_CASH_TOLERANCE SPORTS_LIVE_TIME_IN_FORCE
            SPORTS_LIVE_MAX_PRICE_CENTS SPORTS_BET_MORE_MAX_PRICE_MOVE_CENTS SPORTS_BET_MORE_ENABLED
            LIVE_MAX_DAILY_LOSS LIVE_MAX_DAILY_LOSS_PCT LIVE_MAX_DAILY_LOSS_CAP LIVE_MAX_OPEN_EXPOSURE
            LIVE_MAX_OPEN_EXPOSURE_PCT LIVE_MAX_OPEN_EXPOSURE_CAP KALSHI_API_KEY KALSHI_PRIVATE_KEY_PATH
            -->
          </div>
          <div style="padding-top:10px; display:flex; gap:8px; flex-wrap:wrap;">
            <button class="primary" onclick="saveSettings()">Save Settings</button>
            <button onclick="rerunBotPicks()">Rerun Bot Picks</button>
            <button onclick="resetOdds()">Reset Odds Pause/Daily Counter</button>
            <button onclick="refresh()">Refresh</button>
          </div>
          <div id="settings-status" class="notice">Settings load locally from bot_settings.json.</div>
        </div>
        <div>
          <div id="processes"></div>
          <div class="notice">
            Live mode is still guarded in code. Only use it after exchange order placement and auth are fully tested with tiny limits.
          </div>
        </div>
      </div>
    </section>

    <section class="view" data-view="control">
      <h2>Live Readiness</h2>
      <div class="grid stats" style="grid-template-columns: repeat(5, minmax(150px, 1fr)); padding: 12px;">
        <div class="stat"><div class="label">Ready</div><div id="live-ready" class="value">No</div></div>
        <div class="stat"><div class="label">Kalshi Cash</div><div id="live-cash" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Local Live Open</div><div id="live-local-open" class="value">0</div></div>
        <div class="stat"><div class="label">Kalshi Positions</div><div id="live-remote-open" class="value">0</div></div>
        <div class="stat"><div class="label">Blocking</div><div id="live-blocking" class="value">0</div></div>
        <div class="stat"><div class="label">Live Audit</div><div id="live-audit-status" class="value">N/A</div></div>
        <div class="stat"><div class="label">Cash Drift</div><div id="live-audit-cash-drift" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Audit Warnings</div><div id="live-audit-warnings" class="value">0</div></div>
        <div class="stat"><div class="label">Last Audit</div><div id="live-audit-time" class="value">N/A</div></div>
      </div>
      <div style="overflow:auto"><table>
        <thead><tr><th>Check</th><th>Status</th></tr></thead>
        <tbody id="live-checks"></tbody>
      </table></div>
    </section>

    <div class="grid view" data-view="sports">
      <section>
        <div class="section-title"><h2>Sports Strategy</h2><span id="sports-status" class="health-chip">Checking</span></div>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">Regular Bot Today P&amp;L</div><div id="sports-today-pnl" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Regular Bot All-Time P&amp;L</div><div id="sports-pnl" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Active Bets</div><div id="sports-active-count" class="value">0 / 3</div></div>
          <div class="stat"><div class="label">Unit Size</div><div id="sports-unit-size" class="value">$15.00</div></div>
          <div class="stat"><div class="label">Maximum Per Market</div><div id="sports-unit-cap" class="value">5u</div></div>
          <div class="stat"><div class="label">Daily Loss Room</div><div id="sports-loss-room" class="value">$300.00</div></div>
        </div>
        <div class="notice">Regular bot results exclude legacy imported picks and user-entered live bets. Source totals reconcile to the shared sports settlement ledger.</div>
        <div class="subtabs" aria-label="Sports sections">
          <button type="button" class="active" data-sports-tab="open" onclick="showSportsPanel('open')">Active Bets</button>
          <button type="button" data-sports-tab="settled" onclick="showSportsPanel('settled')">Recent Results</button>
        </div>
        <div class="sports-panel active table-scroll xwide" data-sports-panel="open" style="padding-bottom:14px;"><table>
          <thead><tr><th>Placed</th><th class="num">Bot</th><th>Source / Mode</th><th>Game</th><th>Pick</th><th>Kalshi</th><th class="num">Odds</th><th class="num">Stake</th><th class="num">Conf</th><th>Actions</th></tr></thead>
          <tbody id="sports-open-bets"></tbody>
        </table></div>
        <div class="sports-panel table-scroll xwide" data-sports-panel="settled" style="padding-top:14px;"><table>
          <thead><tr><th>Settled</th><th class="num">Bot</th><th>Source / Mode</th><th>Result</th><th>Game</th><th>Pick</th><th>Kalshi</th><th class="num">Odds</th><th class="num">Stake</th><th class="num">Payout</th><th class="num">Profit</th></tr></thead>
          <tbody id="sports-history"></tbody>
        </table></div>
        <div id="sports-campaign-cards" class="campaign-lane-grid"></div>
        <div class="strategy-summary">Always-on unit portfolio: qualifying bets are accepted until an open-position or daily-loss limit is reached.</div>
        <div id="sports-strategy-summary" class="strategy-summary"></div>
        <div class="analytics-block" style="margin:14px 0;" data-testid="aibetpicks-panel">
          <div class="section-title"><h3>AIBetPicks Bots</h3><span id="aibetpicks-health" class="health-chip">Waiting</span></div>
          <div id="aibetpicks-poll" class="notice"></div>
          <div id="aibetpicks-policy" class="notice"></div>
          <div id="aibetpicks-accounting" class="notice"></div>
          <div class="table-scroll aibetpicks-table"><table>
            <colgroup><col style="width:19%"><col style="width:21%"><col style="width:12%"><col style="width:7%"><col style="width:11%"><col style="width:16%"><col style="width:14%"></colgroup>
            <thead><tr><th>Bot / Local Kalshi record</th><th>Today's pick</th><th>Starts (Central)</th><th class="num">Posted odds</th><th class="num">Units / Stake</th><th>Status / Reason</th><th>Kalshi contract / Side</th></tr></thead>
            <tbody id="aibetpicks-rows"></tbody>
          </table></div>
          <div class="notice">Local results count only filled Kalshi bets and include fees. Website figures are a separate verified full-game sample from the last 90 days, weighted by published stake units at posted odds. Different prices, fees, and unfilled picks can make local results differ.</div>
          <details><summary>Local AIBetPicks results · exact units</summary>
            <div class="table-scroll" style="max-height:460px"><table>
              <thead><tr><th>Entered / Settled (Central)</th><th>Bot / Pick</th><th>Contract / Side</th><th class="num">Entry unit</th><th class="num">Stake / Units</th><th class="num">Fees / Total risk</th><th class="num">Payout</th><th class="num">Result / Net profit</th></tr></thead>
              <tbody id="aibetpicks-results-rows"></tbody>
            </table></div>
            <h4>Daily local results</h4><div class="table-scroll"><table>
              <thead><tr><th>Settlement date (Central)</th><th>W / L / Void / Other</th><th class="num">Net profit</th><th class="num">Net units</th><th class="num">ROI on stake</th></tr></thead>
              <tbody id="aibetpicks-daily-rows"></tbody>
            </table></div>
          </details>
        </div>
        <details class="diagnostic-panel" data-testid="sports-unit-requirements" style="margin-top:18px;">
          <summary>Current unit requirements · 0.5U–5U</summary>
          <div class="section-title"><h3>Sports Unit Requirements · 0.5U–5U</h3><span id="sports-unit-requirements-meta" class="health-chip">Loading live settings</span></div>
          <div class="notice">These are the current live quality thresholds. Edge must be strictly greater than the displayed minimum after pricing costs. Confidence, Pro Score, Final Score, and independent-book families must meet or exceed their displayed minimums.</div>
          <div class="table-scroll xwide" style="margin-top:10px;"><table class="compact-table">
            <thead><tr><th class="num">Units</th><th class="num">Min Edge</th><th class="num">Min Confidence</th><th class="num">Min Pro Score</th><th class="num">Min Final Score</th><th class="num">Independent Books</th><th>How This Tier Is Used</th></tr></thead>
            <tbody id="sports-unit-requirements-rows"></tbody>
          </table></div>
          <div class="shadow-note">The normal 0.5U live size is a risk-reduced bet that already passed the 1U quality gate. The separate relaxed 0.5U qualification lane stays in shadow mode until its sport/market segment passes the automatic walk-forward profitability, calibration, drawdown, and CLV promotion gates.</div>
        </details>
      </section>
    </div>

    <div class="grid view" data-view="crypto">
      <section>
        <div class="section-title"><h2>Crypto Strategy</h2><span id="crypto-status" class="health-chip">Checking</span></div>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">Today P&amp;L</div><div id="crypto-today-pnl" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Total P&amp;L</div><div id="crypto-pnl" class="value">$0.00</div></div>
          <div class="stat" data-testid="crypto-current-strategy-performance"><div class="label">Current Strategy P&amp;L</div><div id="crypto-current-pnl" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Current Record</div><div id="crypto-current-record" class="value">Collecting</div></div>
          <div class="stat"><div class="label">Current ROI</div><div id="crypto-current-roi" class="value">0.00%</div></div>
          <div class="stat"><div class="label">Active Bets</div><div id="crypto-open-count" class="value">0 / 1</div></div>
          <div class="stat"><div class="label">Unit Size</div><div id="crypto-cycle" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Maximum Size</div><div id="crypto-goal-remaining" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Daily Loss Room</div><div id="crypto-loss-room" class="value">$300.00</div></div>
        </div>
        <div id="crypto-settlement-alert" class="strategy-summary" style="display:none;"></div>
        <div id="crypto-network-alert" class="strategy-summary" style="display:none;"></div>
        <div id="crypto-patient-control" class="strategy-summary" data-testid="crypto-patient-control">
          <div class="section-title"><h2>Patient ETH · Capped Recovery</h2><span id="crypto-patient-status" class="health-chip">Loading</span></div>
          <p>Start from <strong>1% of bankroll</strong>, scaled by signal strength. Wait up to 60 seconds for a one-cent better entry.</p>
          <div class="row-actions" style="display:flex;gap:10px;flex-wrap:wrap;">
            <button id="crypto-patient-activate" type="button" onclick="controlPatientCrypto('make_live')" disabled>Make live</button>
            <button id="crypto-patient-pause" type="button" onclick="controlPatientCrypto('pause')" disabled>Pause new entries</button>
          </div>
          <div id="crypto-patient-detail" class="notice" role="status" aria-live="polite">Loading strategy status.</div>
          <div id="crypto-funding-balances" class="notice">Loading available Crypto cash.</div>
          <button id="crypto-funding-apply" type="button" onclick="fundCryptoBankroll()">Use shared bankroll for Sports and Crypto</button>
          <div class="notice">Removes fixed percentage allocations. Both bots can use available cash as needed, with shared reservations and their existing bet limits. Reload both live workers after enabling this change.</div>
          <div id="crypto-funding-detail" class="notice" role="status" aria-live="polite"></div>
          <div class="notice">Activation allows real-money ETH orders. Existing bankroll and execution safeguards stay enabled. Pausing leaves existing positions open.</div>
        </div>
        <div class="subtabs" aria-label="Crypto sections">
          <button type="button" class="active" data-crypto-tab="open" onclick="showCryptoPanel('open')">Active Bets</button>
          <button type="button" data-crypto-tab="settled" onclick="showCryptoPanel('settled')">Recent Results</button>
        </div>
        <div class="crypto-panel active table-scroll xwide" data-crypto-panel="open" style="padding-bottom:14px;"><table>
          <thead><tr><th>Placed</th><th class="num">Bot</th><th>Asset</th><th>Side</th><th>Market</th><th class="num">Stake</th><th class="num">Entry</th><th class="num">Net Edge</th><th class="num">Calibrated Win Prob</th><th class="num">90% Prob Interval</th><th class="num">Edge Certainty</th></tr></thead>
          <tbody id="crypto-open-bets"></tbody>
        </table></div>
        <div class="crypto-panel table-scroll xwide" data-crypto-panel="settled" style="padding-top:14px;"><table>
          <thead><tr><th>Settled</th><th class="num">Bot</th><th>Asset</th><th>Result</th><th>Side</th><th>Market</th><th class="num">Stake</th><th class="num">Profit</th><th class="num">Spot</th></tr></thead>
          <tbody id="crypto-history"></tbody>
        </table></div>
        <div id="crypto-campaign-cards" class="campaign-lane-grid"></div>
        <div class="strategy-summary">Continuous opportunity mode has no profit target or cycle stop. Recovery follows one pooled crypto-campaign high-water drawdown, while individual bot ledgers remain visible for attribution.</div>
        <div id="crypto-strategy-summary" class="strategy-summary"></div>
      </section>
      <section id="crypto-market-trend-card" class="shadow-lab crypto-regime-overview" data-testid="crypto-market-trend-card">
        <h3>Forward Market Trend · 1h + 4h + 24h</h3>
        <div class="regime-signal-board" aria-label="Live crypto market trend signals">
          <div class="regime-signal-grid">
            <div id="crypto-overview-regime-1h-card" class="regime-signal-card neutral">
              <div class="regime-signal-label">Next 1 Hour</div>
              <div id="crypto-overview-regime-1h-pill" class="trend-pill neutral">WAITING</div>
              <div id="crypto-overview-regime-1h-detail" class="regime-signal-detail">Short-horizon directional read.</div>
            </div>
            <div id="crypto-overview-regime-4h-card" class="regime-signal-card neutral">
              <div class="regime-signal-label">Next 4 Hours</div>
              <div id="crypto-overview-regime-4h-pill" class="trend-pill neutral">WAITING</div>
              <div id="crypto-overview-regime-4h-detail" class="regime-signal-detail">Broader trend and momentum read.</div>
            </div>
            <div id="crypto-overview-regime-24h-card" class="regime-signal-card neutral">
              <div class="regime-signal-label">Next 24 Hours</div>
              <div id="crypto-overview-regime-24h-pill" class="trend-pill neutral">WAITING</div>
              <div id="crypto-overview-regime-24h-detail" class="regime-signal-detail">Daily trend with its own history and validation.</div>
            </div>
          </div>
          <div class="regime-health-strip">
            <span id="crypto-overview-regime-freshness" class="health-chip warn">Waiting for refresh</span>
            <span id="crypto-overview-regime-news-health" class="health-chip warn">News waiting</span>
            <span id="crypto-overview-regime-ai-health" class="health-chip warn">AI waiting</span>
            <span id="crypto-overview-regime-execution-health" class="health-chip good">Shadow only</span>
          </div>
        </div>
      </section>
        <section class="shadow-lab" data-testid="crypto-spot-flow-live-pilot" style="margin-top:14px;">
          <div class="section-title"><h3>Spot Lead · Two-Tier Live Pilot</h3><span id="crypto-spot-flow-pilot-status" class="health-chip">Waiting</span></div>
          <div class="shadow-note">Tier A requires same-direction Coinbase/Kraken 60-second spot movement and trade flow inside a 2–13 minute window. Tier B permits a stronger standalone spot lead only at 35–80¢ with 5–13 minutes left, while any registered opposing flow remains a hard veto. Both tiers preserve fresh-feed, spread, authoritative-fee, FOK, one-cent adverse-move, exposure, and reconciliation safeguards. Recovery, cycles, legacy edge, and confidence-based unit boosts remain disabled.</div>
          <div id="crypto-spot-flow-pilot-funnel" class="shadow-note">Waiting for the next live scan.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">Verified Bankroll</div><div id="crypto-spot-flow-pilot-bankroll" class="value">$0.00 / $3,000</div></div>
            <div class="stat"><div class="label">Signal Gate</div><div id="crypto-spot-flow-pilot-signal-gate" class="value">COLLECTING</div></div>
            <div class="stat"><div class="label">Forward Combo</div><div id="crypto-spot-flow-pilot-forward" class="value">0 / 100 · 0 / 3 days</div></div>
            <div class="stat"><div class="label">Sizing</div><div id="crypto-spot-flow-pilot-sizing" class="value">0.05% · max 5 ct</div></div>
            <div class="stat"><div class="label">Daily Loss / Entries</div><div id="crypto-spot-flow-pilot-daily" class="value">$0 / $0 · 0 / 25</div></div>
            <div class="stat"><div class="label">Open Exposure</div><div id="crypto-spot-flow-pilot-open" class="value">$0 / $0 · 0 / 4</div></div>
            <div class="stat"><div class="label">Live Review</div><div id="crypto-spot-flow-pilot-review" class="value">0 / 100 settled</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Asset / Contract</th><th>Tier</th><th>Side</th><th class="num">Price</th><th class="num">Contracts / Stake</th><th>Decision</th></tr></thead>
            <tbody id="crypto-spot-flow-pilot-recent"></tbody>
          </table></div>
        </section>
    </div>

    <section class="view" data-view="crypto-analytics">
      <h2>Analytics</h2>
      <div class="subtabs" aria-label="Analytics bot">
        <button type="button" data-analytics-tab="sports" onclick="showAnalytics('sports')">Sports</button>
        <button type="button" class="active" data-analytics-tab="crypto" onclick="showAnalytics('crypto')">Crypto</button>
        <button type="button" data-analytics-tab="perps" onclick="showAnalytics('perps')">Perpetuals Paper Lab</button>
      </div>
      <h3>Crypto Performance</h3>
      <div class="notice">Tracks settled 15-minute opportunity bets, current candidates, fee-adjusted edge, confidence, price, asset, timing, quality tier, and bot assignment. Historical strategy rows remain separated so they do not get confused with the current bot.</div>
      <div class="grid stats" style="grid-template-columns: repeat(6, minmax(130px, 1fr)); padding: 12px;">
        <div class="stat"><div class="label">Settled</div><div id="crypto-analytics-settled" class="value">0</div></div>
        <div class="stat"><div class="label">ROI</div><div id="crypto-analytics-roi" class="value">0%</div></div>
        <div class="stat"><div class="label">Realized P&L</div><div id="crypto-analytics-profit" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Last 24h P&L</div><div id="crypto-analytics-24h-profit" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Open Exposure</div><div id="crypto-analytics-open-risk" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Avg Stake</div><div id="crypto-analytics-avg-stake" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Candidates</div><div id="crypto-analytics-candidates" class="value">0</div></div>
        <div class="stat"><div class="label">Eligible</div><div id="crypto-analytics-eligible" class="value">0</div></div>
        <div class="stat"><div class="label">Placed Scan</div><div id="crypto-analytics-placed" class="value">0</div></div>
        <div class="stat"><div class="label">35c-55c Candidates</div><div id="crypto-analytics-preferred" class="value">0</div></div>
        <div class="stat"><div class="label">Strong Edge</div><div id="crypto-analytics-strong" class="value">0</div></div>
        <div class="stat"><div class="label">Elite Edge</div><div id="crypto-analytics-elite" class="value">0</div></div>
      </div>
      <section class="analytics-block" data-testid="crypto-scan-intelligence">
        <h3>Scan Intelligence · Complete Candidate Evidence</h3>
        <div class="notice">Every 15-minute candidate is retained with market, heuristic and learned probabilities; uncertainty components; exact costs; multi-window path and flow; executable depth by unit tier; source presence; and the precise change required to qualify. New research features remain shadow-only until independent-market walk-forward validation passes.</div>
        <div class="grid stats" style="grid-template-columns: repeat(8, minmax(125px, 1fr)); padding: 12px;">
          <div class="stat"><div class="label">Recorded Scans</div><div id="crypto-intelligence-scans" class="value">0</div></div>
          <div class="stat"><div class="label">Candidate Records</div><div id="crypto-intelligence-records" class="value">0</div></div>
          <div class="stat"><div class="label">Lifecycle Records</div><div id="crypto-intelligence-lifecycle" class="value">0</div></div>
          <div class="stat"><div class="label">Awaiting Settlement</div><div id="crypto-intelligence-active" class="value">0</div></div>
          <div class="stat"><div class="label">Research Promotion</div><div id="crypto-intelligence-validation" class="value">Collecting</div></div>
          <div class="stat"><div class="label">Dynamic Policy</div><div id="crypto-dynamic-status" class="value">Shadow</div></div>
          <div class="stat"><div class="label">Independent Markets</div><div id="crypto-dynamic-markets" class="value">0 / 100</div></div>
          <div class="stat"><div class="label">Shadow ROI</div><div id="crypto-dynamic-roi" class="value">--</div></div>
        </div>
        <div class="table-scroll xwide"><table class="compact-table">
          <thead><tr><th>Scanned</th><th>Asset / Contract</th><th>Side</th><th class="num">Ask</th><th class="num">Market P</th><th class="num">Final P</th><th class="num">90% Interval</th><th class="num">Break-even</th><th class="num">Edge / Low</th><th class="num">Edge +</th><th class="num">Data</th><th class="num">Current Units</th><th>Dynamic Lane</th><th>Decision</th><th>What Changes It</th></tr></thead>
          <tbody id="crypto-intelligence-recent"></tbody>
        </table></div>
      </section>
      <section class="analytics-block" data-testid="crypto-execution-quality">
        <h3>Price Capture &amp; Execution Quality</h3>
        <div class="notice">Tracks every live quote rejection and order attempt from the initial displayed opportunity through the refreshed full-depth quote and actual fill. Positive slippage means the entry became more expensive; positive price improvement means the fill beat the refreshed limit.</div>
        <div class="grid stats" style="grid-template-columns: repeat(8, minmax(120px, 1fr)); padding: 12px;">
          <div class="stat"><div class="label">Attempts</div><div id="crypto-execution-attempts" class="value">0</div></div>
          <div class="stat"><div class="label">Filled</div><div id="crypto-execution-filled" class="value">0</div></div>
          <div class="stat"><div class="label">Fill Rate</div><div id="crypto-execution-fill-rate" class="value">0%</div></div>
          <div class="stat"><div class="label">Avg Quote Age</div><div id="crypto-execution-quote-age" class="value">--</div></div>
          <div class="stat"><div class="label">Avg Total Slippage</div><div id="crypto-execution-slippage" class="value">--</div></div>
          <div class="stat"><div class="label">Avg Post-Fill Edge</div><div id="crypto-execution-post-edge" class="value">--</div></div>
          <div class="stat"><div class="label">Improved Fills</div><div id="crypto-execution-improved" class="value">0</div></div>
          <div class="stat"><div class="label">Depth Blocks</div><div id="crypto-execution-depth-blocks" class="value">0</div></div>
        </div>
        <div class="grid two">
          <section>
            <h3>Execution By Asset</h3>
            <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Asset</th><th class="num">Attempts</th><th class="num">Fill %</th><th class="num">Avg Slip</th><th class="num">Post Edge</th><th class="num">Improved</th></tr></thead><tbody id="crypto-execution-assets"></tbody></table></div>
          </section>
          <section>
            <h3>Execution Blocks</h3>
            <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Reason</th><th class="num">Count</th></tr></thead><tbody id="crypto-execution-failures"></tbody></table></div>
          </section>
        </div>
        <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Captured</th><th>Asset / Contract</th><th>Side</th><th class="num">Initial</th><th class="num">Fresh</th><th class="num">Fill</th><th class="num">Total Slip</th><th class="num">Quote Age</th><th class="num">Post Edge</th><th>Outcome</th></tr></thead><tbody id="crypto-execution-recent"></tbody></table></div>
      </section>
      <section class="analytics-block">
        <h3>Correlated 15-Minute Windows</h3>
        <div class="notice">Tracks occasions where multiple crypto bots settle in the same 15-minute window on different assets. Exact-contract overlap is blocked globally; different assets remain allowed.</div>
        <div class="grid stats" style="grid-template-columns: repeat(6, minmax(130px, 1fr)); padding: 12px;">
          <div class="stat"><div class="label">Overlap Windows</div><div id="crypto-correlation-windows" class="value">0</div></div>
          <div class="stat"><div class="label">Overlap Record</div><div id="crypto-correlation-record" class="value">0W / 0L</div></div>
          <div class="stat"><div class="label">Overlap P&amp;L</div><div id="crypto-correlation-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Overlap ROI</div><div id="crypto-correlation-roi" class="value">0%</div></div>
          <div class="stat"><div class="label">Same Direction</div><div id="crypto-correlation-same" class="value">0</div></div>
          <div class="stat"><div class="label">Mixed Direction</div><div id="crypto-correlation-mixed" class="value">0</div></div>
          <div class="stat"><div class="label">Exact Contract Conflicts</div><div id="crypto-correlation-exact" class="value">0</div></div>
        </div>
        <div style="overflow:auto"><table class="compact-table">
          <thead><tr><th>Close</th><th>Bots</th><th>Assets</th><th>Sides</th><th class="num">Record</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead>
          <tbody id="crypto-correlation-windows-table"></tbody>
        </table></div>
      </section>
      <section class="shadow-lab" data-testid="crypto-recovery-counterfactual">
        <h3>Recovery Counterfactual Analytics</h3>
        <div id="crypto-recovery-counterfactual-status" class="shadow-note">Historical recovery add-ons remain available for counterfactual analysis; new recovery bonus stakes are disabled.</div>
        <div class="grid shadow-stats">
          <div class="stat"><div class="label">Bonus Bets</div><div id="crypto-recovery-cf-settled" class="value">0</div></div>
          <div class="stat"><div class="label">Actual P&amp;L</div><div id="crypto-recovery-cf-actual-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Base-Only P&amp;L</div><div id="crypto-recovery-cf-base-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Incremental P&amp;L</div><div id="crypto-recovery-cf-incremental-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Incremental ROI</div><div id="crypto-recovery-cf-incremental-roi" class="value">0%</div></div>
          <div class="stat"><div class="label">Max Incremental Loss Streak</div><div id="crypto-recovery-cf-loss-streak" class="value">0</div></div>
          <div class="stat"><div class="label">Actual Max Drawdown</div><div id="crypto-recovery-cf-actual-drawdown" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Base-Only Max Drawdown</div><div id="crypto-recovery-cf-base-drawdown" class="value">$0.00</div></div>
        </div>
        <div class="shadow-layout">
          <div class="shadow-table-wrap"><table class="compact-table">
            <thead><tr><th>Bot</th><th class="num">Add-ons</th><th class="num">Actual Drawdown</th><th class="num">Base-Only Drawdown</th><th class="num">Actual Avg Recovery</th><th class="num">Base Avg Recovery</th><th class="num">Open Drawdown Age</th></tr></thead>
            <tbody id="crypto-recovery-cf-bots"></tbody>
          </table></div>
          <div class="shadow-table-wrap"><table class="compact-table">
            <thead><tr><th>Settled</th><th>Bot / Asset</th><th>Result</th><th class="num">Base</th><th class="num">Bonus</th><th class="num">Actual P&amp;L</th><th class="num">Base-Only P&amp;L</th><th class="num">Incremental</th></tr></thead>
            <tbody id="crypto-recovery-cf-recent"></tbody>
          </table></div>
        </div>
      </section>
      <div class="daily-performance-panel" aria-labelledby="crypto-daily-performance-title">
        <div class="performance-toolbar">
          <h3 id="crypto-daily-performance-title">Crypto Daily P&amp;L</h3>
          <div class="range-buttons" data-performance-range="crypto" aria-label="Crypto daily profit chart range">
            <button type="button" data-range="7d" onclick="setPerformanceRange('crypto','7d')">7D</button>
            <button type="button" class="active" data-range="30d" onclick="setPerformanceRange('crypto','30d')">30D</button>
            <button type="button" data-range="90d" onclick="setPerformanceRange('crypto','90d')">90D</button>
            <button type="button" data-range="all" onclick="setPerformanceRange('crypto','all')">All</button>
          </div>
        </div>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">Period P&amp;L</div><div id="crypto-period-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Period ROI</div><div id="crypto-period-roi" class="value">0.0%</div></div>
          <div class="stat"><div class="label">Period Record</div><div id="crypto-period-record" class="value">0W / 0L</div></div>
          <div class="stat"><div class="label">Average Per Day</div><div id="crypto-period-average" class="value">$0.00</div></div>
        </div>
        <div class="chart-wrap"><canvas id="crypto-daily-pnl-chart" role="img" aria-label="Crypto bot daily profit and loss"></canvas></div>
        <div id="crypto-daily-performance-summary" class="performance-summary">Waiting for settled crypto results.</div>
      </div>
      <section id="crypto-market-regime-lab" class="shadow-lab" data-testid="crypto-market-regime-lab">
        <h3>Market Forecast Research · 1h + 4h + 24h</h3>
        <div class="shadow-note">Each horizon uses separate volatility-normalized momentum, trend, flow, derivatives, breadth, and freshness inputs. Forecasts are scored forward after their due time and remain shadow-only until independent validation clears the live lock.</div>
        <div class="regime-signal-board" aria-label="Current crypto market trend signals">
          <div class="regime-signal-grid">
            <div id="crypto-regime-1h-card" class="regime-signal-card neutral">
              <div class="regime-signal-label">Next 1 Hour</div>
              <div id="crypto-regime-1h-pill" class="trend-pill neutral">WAITING</div>
              <div id="crypto-regime-1h-detail" class="regime-signal-detail">Short-horizon directional read.</div>
            </div>
            <div id="crypto-regime-4h-card" class="regime-signal-card neutral">
              <div class="regime-signal-label">Next 4 Hours</div>
              <div id="crypto-regime-4h-pill" class="trend-pill neutral">WAITING</div>
              <div id="crypto-regime-4h-detail" class="regime-signal-detail">Broader trend and momentum read.</div>
            </div>
            <div id="crypto-regime-24h-card" class="regime-signal-card neutral">
              <div class="regime-signal-label">Next 24 Hours</div>
              <div id="crypto-regime-24h-pill" class="trend-pill neutral">WAITING</div>
              <div id="crypto-regime-24h-detail" class="regime-signal-detail">Daily trend with a dedicated 15-minute history.</div>
            </div>
          </div>
          <div class="regime-health-strip">
            <span id="crypto-regime-freshness" class="health-chip warn">Waiting for refresh</span>
            <span id="crypto-regime-news-health" class="health-chip warn">News waiting</span>
            <span id="crypto-regime-ai-health" class="health-chip warn">AI waiting</span>
            <span id="crypto-regime-execution-health" class="health-chip good">Shadow only</span>
          </div>
        </div>
        <div class="grid shadow-stats">
          <div class="stat"><div class="label">Mode</div><div id="crypto-regime-mode" class="value">Waiting</div></div>
          <div class="stat"><div class="label">Global Direction</div><div id="crypto-regime-direction" class="value">Neutral</div></div>
          <div class="stat"><div class="label">Regime</div><div id="crypto-regime-state" class="value">Waiting</div></div>
          <div class="stat"><div class="label">Breadth</div><div id="crypto-regime-breadth" class="value">0%</div></div>
          <div class="stat"><div class="label">News Risk</div><div id="crypto-regime-news" class="value">Normal</div></div>
          <div class="stat"><div class="label">AI Context</div><div id="crypto-regime-ai" class="value">Waiting</div></div>
          <div class="stat"><div class="label">Forward Windows</div><div id="crypto-regime-settled" class="value">0 / 100</div></div>
          <div class="stat"><div class="label">1h Brier Score</div><div id="crypto-regime-brier" class="value">Collecting</div></div>
        </div>
        <div id="crypto-regime-updated" class="shadow-note">Waiting for the next five-minute regime refresh.</div>
        <div class="shadow-table-wrap"><table class="compact-table">
          <thead><tr><th>Asset</th><th>1h</th><th>4h</th><th>24h</th><th class="num">P(up) 1h</th><th class="num">P(up) 4h</th><th class="num">P(up) 24h</th><th class="num">Data Quality</th></tr></thead>
          <tbody id="crypto-regime-assets"></tbody>
        </table></div>
      </section>
      <section class="shadow-lab" data-testid="crypto-shadow-lab">
        <h3>Shadow Lab · Fee + Correlation Policy</h3>
        <div class="shadow-note">Compares the current candidate stream with fee-adjusted net edge, fee-aware Kelly sizing, and event/asset concentration limits. It records recommendations only and cannot place or resize a bet.</div>
        <div class="grid shadow-stats">
          <div class="stat"><div class="label">Mode</div><div id="crypto-shadow-mode" class="value">Waiting</div></div>
          <div class="stat"><div class="label">Raw +EV</div><div id="crypto-shadow-raw" class="value">0</div></div>
          <div class="stat"><div class="label">Fee +EV</div><div id="crypto-shadow-fee-positive" class="value">0</div></div>
          <div class="stat"><div class="label">Net Threshold</div><div id="crypto-shadow-threshold" class="value">0</div></div>
          <div class="stat"><div class="label">Correlation Approved</div><div id="crypto-shadow-approved" class="value">0</div></div>
          <div class="stat"><div class="label">Shadow Stake</div><div id="crypto-shadow-stake" class="value">$0.00</div></div>
        </div>
        <div id="crypto-shadow-outcomes" class="shadow-note">Outcome tracking begins when shadow-tagged bets settle.</div>
        <div class="shadow-layout">
          <div class="shadow-table-wrap"><table class="compact-table">
            <thead><tr><th>Opportunity</th><th>Group</th><th class="num">Entry</th><th class="num">Raw Edge</th><th class="num">Net Edge</th><th class="num">Fee Drag</th><th class="num">Kelly</th><th class="num">After Correlation</th><th>Decision</th></tr></thead>
            <tbody id="crypto-shadow-opportunities"></tbody>
          </table></div>
          <div class="shadow-table-wrap"><table class="compact-table"><thead><tr><th>Shadow Filter</th><th class="num">Count</th></tr></thead><tbody id="crypto-shadow-reasons"></tbody></table></div>
        </div>
      </section>
      <section class="shadow-lab" data-testid="crypto-btc-15m-sprint">
        <div class="section-title"><h3>BTC 15-Minute Flow-Veto Sprint · Paper Only</h3><span id="crypto-sprint-status" class="health-chip">Waiting</span></div>
        <div class="shadow-note">Locked 72-hour forward test of the model-selected BTC side when directional opposition or weak flow is the only veto. One contract per market, 35–49¢, 2–12 minutes remaining, exact fees, one-cent reserve, fresh sequence-valid WebSocket depth, and no automatic promotion. The +150 and 40–44¢ lanes are comparison baselines only.</div>
        <div class="grid shadow-stats">
          <div class="stat"><div class="label">Freeze</div><div id="crypto-sprint-freeze" class="value">Checking</div></div>
          <div class="stat"><div class="label">Decision</div><div id="crypto-sprint-decision" class="value">Collecting</div></div>
          <div class="stat"><div class="label">Forward Sample</div><div id="crypto-sprint-progress" class="value">0 / 60</div></div>
          <div class="stat"><div class="label">Window Ends (Central)</div><div id="crypto-sprint-ends" class="value">Waiting</div></div>
          <div class="stat"><div class="label">Record</div><div id="crypto-sprint-record" class="value">0W / 0L</div></div>
          <div class="stat"><div class="label">Reserved P&amp;L</div><div id="crypto-sprint-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Reserved ROI</div><div id="crypto-sprint-roi" class="value">0%</div></div>
          <div class="stat"><div class="label">Win Rate / Break-Even</div><div id="crypto-sprint-break-even" class="value">0% / 0%</div></div>
          <div class="stat"><div class="label">90% Lower Bound</div><div id="crypto-sprint-lower-bound" class="value">Collecting</div></div>
          <div class="stat"><div class="label">First / Second Half</div><div id="crypto-sprint-halves" class="value">$0 / $0</div></div>
          <div class="stat"><div class="label">Maximum Drawdown</div><div id="crypto-sprint-drawdown" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Development Replay · Never Qualifying</div><div id="crypto-sprint-development" class="value">0 markets</div></div>
        </div>
        <div id="crypto-sprint-funnel" class="shadow-note">Waiting for the first locked sprint scan.</div>
        <div class="shadow-layout">
          <div class="shadow-table-wrap"><table class="compact-table">
            <thead><tr><th>Lane</th><th class="num">Tracked</th><th class="num">Record</th><th class="num">Win Rate</th><th class="num">Break-Even</th><th class="num">Reserved P&amp;L</th><th class="num">Reserved ROI</th><th>Promotable</th></tr></thead>
            <tbody id="crypto-sprint-lanes"></tbody>
          </table></div>
          <div class="shadow-table-wrap"><table class="compact-table">
            <thead><tr><th>Control</th><th class="num">Settled</th><th class="num">Record</th><th class="num">Reserved P&amp;L</th><th class="num">Reserved ROI</th></tr></thead>
            <tbody id="crypto-sprint-controls"></tbody>
          </table></div>
        </div>
        <div class="shadow-table-wrap"><table class="compact-table">
          <thead><tr><th>Captured / Settled</th><th>Lane</th><th>Contract</th><th>Pick</th><th class="num">Entry</th><th class="num">Reserved Edge</th><th>Result</th><th class="num">Reserved P&amp;L</th></tr></thead>
          <tbody id="crypto-sprint-recent"></tbody>
        </table></div>
      </section>
      <section class="shadow-lab" data-testid="crypto-low-edge-live-pilot">
        <h3>Low-Edge Live Pilot</h3>
        <div class="shadow-note">Retired low-edge pilot analytics. The current live strategy uses the probability/edge/Kelly ladder across its full entry range.</div>
        <div class="grid shadow-stats">
          <div class="stat"><div class="label">Status</div><div id="crypto-live-pilot-status" class="value">Waiting</div></div>
          <div class="stat"><div class="label">Progress</div><div id="crypto-live-pilot-progress" class="value">0 / 50</div></div>
          <div class="stat"><div class="label">Open</div><div id="crypto-live-pilot-open" class="value">0 / 1</div></div>
          <div class="stat"><div class="label">Record</div><div id="crypto-live-pilot-record" class="value">0W / 0L</div></div>
          <div class="stat"><div class="label">Win Rate</div><div id="crypto-live-pilot-win-rate" class="value">0%</div></div>
          <div class="stat"><div class="label">Max Loss Streak</div><div id="crypto-live-pilot-loss-streak" class="value">0</div></div>
          <div class="stat"><div class="label">Live P&amp;L</div><div id="crypto-live-pilot-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">ROI</div><div id="crypto-live-pilot-roi" class="value">0%</div></div>
        </div>
        <div class="shadow-table-wrap"><table class="compact-table">
          <thead><tr><th>Settled</th><th>Bot / Asset</th><th>Pick</th><th class="num">Entry</th><th class="num">Net Edge</th><th class="num">Conf</th><th>Result</th><th class="num">Stake</th><th class="num">Profit</th></tr></thead>
          <tbody id="crypto-live-pilot-recent"></tbody>
        </table></div>
      </section>
      <section class="shadow-lab" data-testid="crypto-low-edge-shadow-lab">
        <h3>Low-Edge 65% Shadow Test</h3>
        <div class="shadow-note">Paper-only comparison of 1-2%, 2-3%, and 3-4% fee-adjusted net edge at 65%+ confidence. It records the first executable quote, uses a fixed $1 virtual stake, and settles only from finalized Kalshi outcomes. It cannot affect live bets, cycles, recovery, bankroll, or position limits.</div>
        <div class="grid shadow-stats">
          <div class="stat"><div class="label">Mode</div><div id="crypto-low-edge-mode" class="value">Waiting</div></div>
          <div class="stat"><div class="label">Tracked</div><div id="crypto-low-edge-tracked" class="value">0</div></div>
          <div class="stat"><div class="label">Open</div><div id="crypto-low-edge-open" class="value">0</div></div>
          <div class="stat"><div class="label">Settled</div><div id="crypto-low-edge-settled" class="value">0</div></div>
          <div class="stat"><div class="label">Record</div><div id="crypto-low-edge-record" class="value">0W / 0L</div></div>
          <div class="stat"><div class="label">Virtual P&amp;L</div><div id="crypto-low-edge-profit" class="value">$0.00</div></div>
        </div>
        <div id="crypto-low-edge-funnel" class="shadow-note">Waiting for the next candidate scan.</div>
        <div class="shadow-layout">
          <div class="shadow-table-wrap"><table class="compact-table">
            <thead><tr><th>Net-Edge Cohort</th><th class="num">Tracked</th><th class="num">Open</th><th class="num">Record</th><th class="num">Win Rate</th><th class="num">Avg Conf</th><th class="num">Virtual P&amp;L</th><th class="num">ROI</th></tr></thead>
            <tbody id="crypto-low-edge-cohorts"></tbody>
          </table></div>
          <div class="shadow-table-wrap"><table class="compact-table">
            <thead><tr><th>Captured / Settled</th><th>Contract</th><th>Pick</th><th class="num">Entry</th><th class="num">Net Edge</th><th class="num">Conf</th><th>Result</th><th class="num">Virtual P&amp;L</th></tr></thead>
            <tbody id="crypto-low-edge-recent"></tbody>
          </table></div>
        </div>
      </section>
      <div class="grid three analytics-block">
        <section>
          <h3>Candidate Funnel</h3>
          <div class="chart-wrap"><canvas id="chart-crypto-funnel"></canvas></div>
        </section>
        <section>
          <h3>Skip Reasons</h3>
          <div class="chart-wrap"><canvas id="chart-crypto-skips"></canvas></div>
        </section>
        <section>
          <h3>Asset P&L</h3>
          <div class="chart-wrap"><canvas id="chart-crypto-asset-profit"></canvas></div>
        </section>
      </div>
      <div class="grid three analytics-block">
        <section>
          <h3>Price Band ROI</h3>
          <div class="chart-wrap"><canvas id="chart-crypto-price-roi"></canvas></div>
        </section>
        <section>
          <h3>Confidence Win %</h3>
          <div class="chart-wrap"><canvas id="chart-crypto-confidence-win"></canvas></div>
        </section>
        <section>
          <h3>Edge ROI</h3>
          <div class="chart-wrap"><canvas id="chart-crypto-edge-roi"></canvas></div>
        </section>
      </div>
      <div class="grid three analytics-block">
        <section>
          <h3>Performance By Asset</h3>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Asset</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="crypto-analytics-asset"></tbody></table></div>
        </section>
        <section>
          <h3>Performance By Price</h3>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Band</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="crypto-analytics-price"></tbody></table></div>
        </section>
        <section>
          <h3>Performance By Market</h3>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Kind</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="crypto-analytics-kind"></tbody></table></div>
        </section>
      </div>
      <div class="grid three analytics-block">
        <section>
          <h3>Candidate Price Bands</h3>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Band</th><th class="num">Count</th><th class="num">Eligible</th><th class="num">Placed</th><th class="num">Avg Edge</th><th class="num">Avg Conf</th></tr></thead><tbody id="crypto-candidate-price"></tbody></table></div>
        </section>
        <section>
          <h3>Candidate Assets</h3>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Asset</th><th class="num">Count</th><th class="num">Eligible</th><th class="num">Placed</th><th class="num">Avg Edge</th><th class="num">Avg Conf</th></tr></thead><tbody id="crypto-candidate-asset"></tbody></table></div>
        </section>
        <section>
          <h3>Candidate Time To Close</h3>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Window</th><th class="num">Count</th><th class="num">Eligible</th><th class="num">Placed</th><th class="num">Avg Edge</th><th class="num">Avg Conf</th></tr></thead><tbody id="crypto-candidate-time"></tbody></table></div>
        </section>
      </div>
      <div class="grid three analytics-block">
        <section>
          <h3>Skip Reason Counts</h3>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Reason</th><th class="num">Count</th></tr></thead><tbody id="crypto-analytics-skips"></tbody></table></div>
        </section>
        <section>
          <h3>Quality Tier</h3>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Tier</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="crypto-analytics-selective"></tbody></table></div>
        </section>
        <section>
          <h3>Bot / Unit Mode</h3>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>State</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="crypto-analytics-recovery"></tbody></table></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h3>Latest Crypto Events</h3>
          <div id="crypto-events" class="events"></div>
        </section>
        <section>
          <h3>Latest Crypto Notices</h3>
          <div id="crypto-analytics-notices" class="logs"></div>
        </section>
      </div>
      <details id="crypto-cycle-shadow-panel" class="shadow-lab" data-testid="crypto-cycle-shadow-panel" style="grid-column:1/-1;">
        <summary>Crypto research · shadow studies and historical results</summary>
        <div class="section-title"><h2>Crypto V6 Strategy Ledger · Shadow Only</h2><span id="crypto-cycle-shadow-status" class="health-chip">Waiting</span></div>
        <div class="shadow-note">V6 tests two independent entry lanes: an early 10–13 minute flow fade and a late 1.75–2.5 minute convex setup. XRP is excluded, only one virtual position is allowed per expiry, and every candidate must have exact fees, verified settlement mapping, fresh sequence-valid quotes, and executable depth. Recovery waits for another fully qualified V6 setup and targets only a $0.25 deficit episode, with hard caps of four attempts, three contracts, $1.50 per attempt, and $3 per episode. Flat one-contract and maker/taker results are tracked on the same candidates. This is unvalidated research with no automatic promotion. This panel cannot place orders or affect live bankroll, recovery, limits, or qualification.</div>
        <div id="crypto-cycle-shadow-funnel" class="shadow-note">Waiting for the first V6 shadow scan.</div>
        <div class="grid shadow-stats">
          <div class="stat"><div class="label">Independent Sample</div><div id="crypto-cycle-shadow-cycles" class="value">0 / 100</div></div>
          <div class="stat"><div class="label">Resolved Episodes</div><div id="crypto-cycle-shadow-finish" class="value">0</div></div>
          <div class="stat"><div class="label">Open</div><div id="crypto-cycle-shadow-open" class="value">0 / 3</div></div>
          <div class="stat"><div class="label">Record</div><div id="crypto-cycle-shadow-record" class="value">0W / 0L</div></div>
          <div class="stat"><div class="label">Virtual P&amp;L</div><div id="crypto-cycle-shadow-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Virtual ROI</div><div id="crypto-cycle-shadow-roi" class="value">0.0%</div></div>
          <div class="stat"><div class="label">Winning Episodes</div><div id="crypto-cycle-shadow-targets" class="value">0</div></div>
          <div class="stat"><div class="label">Losing Episodes</div><div id="crypto-cycle-shadow-partial" class="value">0</div></div>
          <div class="stat"><div class="label">Recovery Caps</div><div id="crypto-cycle-shadow-risk-capped" class="value">4 tries · 3 ct · $1.50</div></div>
          <div class="stat"><div class="label">Net-Positive Episodes</div><div id="crypto-cycle-shadow-profitable-cycles" class="value">0 / 0</div></div>
          <div class="stat"><div class="label">Early / Late ROI</div><div id="crypto-cycle-shadow-lane-roi" class="value">0% / 0%</div></div>
          <div class="stat"><div class="label">Maker vs Taker Δ</div><div id="crypto-cycle-shadow-correlation" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Flat 1-Contract P&amp;L</div><div id="crypto-cycle-shadow-flat-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Recovery vs Flat Δ</div><div id="crypto-cycle-shadow-recovery-incremental" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Review Gate</div><div id="crypto-cycle-shadow-evaluation" class="value">0 / 100</div></div>
        </div>
        <div id="crypto-cycle-shadow-bots" class="campaign-lane-grid"></div>
        <div class="grid two">
          <section>
            <h3>Performance by Attempt</h3>
            <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Attempt</th><th class="num">Record</th><th class="num">Cost</th><th class="num">P&amp;L</th><th class="num">ROI</th></tr></thead><tbody id="crypto-cycle-shadow-attempt-performance"></tbody></table></div>
          </section>
          <section>
            <h3>Performance by Price</h3>
            <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Band</th><th class="num">Record</th><th class="num">Cost</th><th class="num">P&amp;L</th><th class="num">ROI</th></tr></thead><tbody id="crypto-cycle-shadow-price-performance"></tbody></table></div>
          </section>
        </div>
        <section class="shadow-lab" data-testid="crypto-cycle-cap-counterfactual" style="margin-top:14px;">
          <h3>Maker / Taker Counterfactual · Analytics Only</h3>
          <div id="crypto-cycle-cap-counterfactual-status" class="shadow-note">Early-lane maker quotes improve the public bid by one cent and require a later public trade at or through the limit. Queue position is not modeled, so fills are optimistic diagnostics—not executable claims. Unfilled quotes expire with 8.5 minutes left. This report cannot change a stake, add a bet, or promote itself.</div>
        </section>
        <section class="shadow-lab" data-testid="crypto-positive-edge-discovery" style="margin-top:14px;">
          <h3>Positive Edge Discovery V3 · Bounded-Residual Shadow</h3>
          <div class="shadow-note">V3 re-anchors the learned market residual to the same fresh executable book and caps its correction at ±5 percentage points, preventing stale or extreme tail-model outputs from manufacturing edge. It forward-tests the bounded model taker forecast, current market-anchored forecast, and post-only maker quote, with one candidate per expiry. Pre-V3 records remain auditable but are excluded from current results. This research cannot place orders, affect V6 or live qualification, or promote itself automatically.</div>
          <div id="crypto-edge-discovery-funnel" class="shadow-note">Waiting for the first positive-edge discovery scan.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">Independent Sample</div><div id="crypto-edge-discovery-sample" class="value">0 / 100</div></div>
            <div class="stat"><div class="label">Settled Record</div><div id="crypto-edge-discovery-record" class="value">0W / 0L</div></div>
            <div class="stat"><div class="label">Raw Taker P&amp;L</div><div id="crypto-edge-discovery-raw-profit" class="value">$0.00</div></div>
            <div class="stat"><div class="label">Raw Taker ROI</div><div id="crypto-edge-discovery-raw-roi" class="value">0.0%</div></div>
            <div class="stat"><div class="label">Avg Synchronized Edge / Stress Low</div><div id="crypto-edge-discovery-raw-edge" class="value">--</div></div>
            <div class="stat"><div class="label">Avg Anchored Edge</div><div id="crypto-edge-discovery-anchored-edge" class="value">--</div></div>
            <div class="stat"><div class="label">Avg Guardrail Adjustment</div><div id="crypto-edge-discovery-guardrail" class="value">--</div></div>
            <div class="stat"><div class="label">Avg Execution Friction</div><div id="crypto-edge-discovery-friction" class="value">--</div></div>
            <div class="stat"><div class="label">Maker Fill / P&amp;L</div><div id="crypto-edge-discovery-maker" class="value">0 / $0.00</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Time</th><th>Asset / Contract</th><th>Side</th><th class="num">Entry</th><th class="num">Sync / Anchored P</th><th class="num">Break-even P</th><th class="num">Sync Edge / Stress</th><th class="num">Guardrail</th><th class="num">Spread / Fee / Slip</th><th>Maker</th><th>Result</th><th class="num">Sync P&amp;L</th></tr></thead>
            <tbody id="crypto-edge-discovery-recent"></tbody>
          </table></div>
        </section>
        <section class="shadow-lab" data-testid="crypto-settlement-lag-shadow" style="margin-top:14px;">
          <div class="section-title"><h3>BTC Settlement Lag V1 · Exact BRTI Shadow</h3><span id="crypto-settlement-lag-status" class="health-chip">Waiting</span></div>
          <div class="shadow-note">This isolated research lane listens to Kalshi's authenticated BRTI feed during the exact 60-reading settlement window. Source timestamps must agree with the official 1–60 reading number; reconnect fragments are blocked and retained only as diagnostic history. Headline P&amp;L counts each market once, while the 4c, 6c, and 8c policy arms remain separate diagnostics. Probability forecasts are frozen at readings 25, 35, and 45. Finalized official market results alone settle records. A fresh next-tick, integer 10-contract FOK counterfactual now tests whether the displayed edge survives realistic latency, book walking, exact fees, and slippage. It cannot place orders, affect live qualification or sizing, use recovery, or promote itself automatically.</div>
          <div id="crypto-settlement-lag-current" class="shadow-note">Waiting for the next BTC final-minute BRTI window.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">Independent Markets</div><div id="crypto-settlement-lag-sample" class="value">0 / 50</div></div>
            <div class="stat"><div class="label">Independent Record</div><div id="crypto-settlement-lag-record" class="value">0W / 0L</div></div>
            <div class="stat"><div class="label">Settled Policy Arms</div><div id="crypto-settlement-lag-arms" class="value">0</div></div>
            <div class="stat"><div class="label">Fixed Forecasts</div><div id="crypto-settlement-lag-observations" class="value">0 / 0 settled</div></div>
            <div class="stat"><div class="label">Hold P&amp;L / ROI</div><div id="crypto-settlement-lag-hold" class="value">$0.00 / 0.0%</div></div>
            <div class="stat"><div class="label">Observation Days</div><div id="crypto-settlement-lag-days" class="value">0 / 30</div></div>
            <div class="stat"><div class="label">Hold Halves</div><div id="crypto-settlement-lag-halves" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Hold Day 90% Lower</div><div id="crypto-settlement-lag-day-lower" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Next-tick 10-contract FOK</div><div id="crypto-settlement-lag-fok" class="value">0 / 50 · $0.00</div></div>
            <div class="stat"><div class="label">FOK Day 90% Lower</div><div id="crypto-settlement-lag-fok-lower" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Exit Policy P&amp;L / ROI</div><div id="crypto-settlement-lag-exit" class="value">$0.00 / 0.0%</div></div>
            <div class="stat"><div class="label">Calibration Residuals</div><div id="crypto-settlement-lag-calibration" class="value">0</div></div>
            <div class="stat"><div class="label">Average Brier</div><div id="crypto-settlement-lag-brier" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Invalid Legacy Arms</div><div id="crypto-settlement-lag-invalid" class="value">0</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Time</th><th>Contract / Arm</th><th>Side</th><th class="num">Reading</th><th class="num">Entry</th><th class="num">P / Interval</th><th class="num">Required Avg</th><th class="num">Edge / Low</th><th class="num">Spread / Slip / Fee</th><th>Exit Policy</th><th>Result</th><th class="num">Hold / Exit P&amp;L</th></tr></thead>
            <tbody id="crypto-settlement-lag-recent"></tbody>
          </table></div>
        </section>
        <section class="shadow-lab" data-testid="crypto-directional-opposition-v2" style="margin-top:14px;">
          <h3>Directional Opposition V2 · Legacy Observational Comparison</h3>
          <div class="shadow-note">This original ledger is permanently observational and cannot qualify for promotion. Its opposition and control rows were captured at different times, may use different tickers or sides, and are not a causal paired test. The raw unequal-sample P&amp;L gap is retained only for audit continuity; the matched-stratum diagnostic below is also noncontemporaneous. The live directional veto is unchanged.</div>
          <div id="crypto-directional-v2-funnel" class="shadow-note">Waiting for the next qualified shadow comparison scan.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">Opposition Sample</div><div id="crypto-directional-v2-opposition-sample" class="value">0 / 200</div></div>
            <div class="stat"><div class="label">Opposition P&amp;L / ROI</div><div id="crypto-directional-v2-opposition-profit" class="value">$0.00 / 0.0%</div></div>
            <div class="stat"><div class="label">Control Sample</div><div id="crypto-directional-v2-control-sample" class="value">0 / 200</div></div>
            <div class="stat"><div class="label">Control P&amp;L / ROI</div><div id="crypto-directional-v2-control-profit" class="value">$0.00 / 0.0%</div></div>
            <div class="stat"><div class="label">Raw Unmatched P&amp;L Gap</div><div id="crypto-directional-v2-delta" class="value">$0.00</div></div>
            <div class="stat"><div class="label">Observational Match</div><div id="crypto-directional-v2-review" class="value">0 / 200 strata-matched</div></div>
            <div class="stat"><div class="label">Matched Diagnostic Gap</div><div id="crypto-directional-v2-matched-delta" class="value">$0.00</div></div>
            <div class="stat"><div class="label">Same Ticker / Same Side</div><div id="crypto-directional-v2-match-quality" class="value">0 / 0</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Time</th><th>Lane</th><th>Asset / Contract</th><th>Side</th><th class="num">Entry + Fee</th><th class="num">P / Interval</th><th class="num">Edge / Low</th><th class="num">Flow / Sources</th><th>Version</th><th>Result</th><th class="num">P&amp;L</th></tr></thead>
            <tbody id="crypto-directional-v2-recent"></tbody>
          </table></div>
          <h4 style="margin-top:16px;">35–44¢ Contemporaneous Paired Forward Test</h4>
          <div class="shadow-note">Preregistered from its displayed start time with no historical backfill. Each qualifying opposition market freezes simultaneous fade and follow arms from the same ticker and executable book. Readiness now requires 100 new unique markets, 50 expiry windows, 30 Chicago observation days, profitable chronological halves, positive expiry- and day-clustered bounds, and confirmation in the fresh equal-integer-contract execution revision. Selected-NO results are diagnostic only. This test cannot place orders or change live qualification.</div>
          <div id="crypto-directional-pair-funnel" class="shadow-note">Waiting for the first registered 35–44¢ opposition pair.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">Unique Markets</div><div id="crypto-directional-pair-sample" class="value">0 / 100</div></div>
            <div class="stat"><div class="label">Expiry Windows</div><div id="crypto-directional-pair-expiries" class="value">0 / 50</div></div>
            <div class="stat"><div class="label">Observation Days</div><div id="crypto-directional-pair-days" class="value">0 / 30</div></div>
            <div class="stat"><div class="label">Fade P&amp;L / ROI</div><div id="crypto-directional-pair-fade" class="value">$0.00 / 0.0%</div></div>
            <div class="stat"><div class="label">Follow P&amp;L / ROI</div><div id="crypto-directional-pair-follow" class="value">$0.00 / 0.0%</div></div>
            <div class="stat"><div class="label">Fade − Follow</div><div id="crypto-directional-pair-delta" class="value">$0.00</div></div>
            <div class="stat"><div class="label">Expiry / Day 90% Lower</div><div id="crypto-directional-pair-lower" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Fade Halves</div><div id="crypto-directional-pair-halves" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Integer Forward P&amp;L</div><div id="crypto-directional-pair-integer" class="value">0 / 100 · Collecting</div></div>
            <div class="stat"><div class="label">Integer Day Δ Lower</div><div id="crypto-directional-pair-integer-lower" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Manual Review</div><div id="crypto-directional-pair-review" class="value">COLLECTING</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Time</th><th>Asset / Contract</th><th>Fade</th><th>Follow</th><th class="num">Flow / Sources</th><th>Market Result</th><th class="num">Fade / Follow / Δ P&amp;L</th></tr></thead>
            <tbody id="crypto-directional-pair-recent"></tbody>
          </table></div>
          <h4 style="margin-top:16px;">Preregistered Asset Replications · Fresh Forward Only</h4>
          <div class="shadow-note">Two separately preregistered hypotheses collect only post-registration evidence: DOGE fade and ETH follow. Each needs 100 unique settled markets across 30 Chicago days, profitable chronological halves, positive equal-integer-contract confirmation, and a positive 97.5% one-sided day-clustered lower bound. The 97.5% per-hypothesis threshold controls the two-hypothesis family at 95%. No historical backfill and no automatic promotion are allowed.</div>
          <div id="crypto-directional-assets-funnel" class="shadow-note">Waiting for fresh DOGE and ETH registered pairs.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">DOGE Fade Sample / Days</div><div id="crypto-directional-doge-sample" class="value">0 / 100 · 0 / 30</div></div>
            <div class="stat"><div class="label">DOGE Fade P&amp;L / ROI</div><div id="crypto-directional-doge-profit" class="value">$0.00 / 0.0%</div></div>
            <div class="stat"><div class="label">DOGE 97.5% Day Lower</div><div id="crypto-directional-doge-lower" class="value">Collecting</div></div>
            <div class="stat"><div class="label">ETH Follow Sample / Days</div><div id="crypto-directional-eth-sample" class="value">0 / 100 · 0 / 30</div></div>
            <div class="stat"><div class="label">ETH Follow P&amp;L / ROI</div><div id="crypto-directional-eth-profit" class="value">$0.00 / 0.0%</div></div>
            <div class="stat"><div class="label">ETH 97.5% Day Lower</div><div id="crypto-directional-eth-lower" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Replication Review</div><div id="crypto-directional-assets-review" class="value">COLLECTING</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Time</th><th>Hypothesis</th><th>Asset / Contract</th><th>Primary</th><th>Comparator</th><th>Result</th><th class="num">Primary / Comparator / Δ P&amp;L</th></tr></thead>
            <tbody id="crypto-directional-assets-recent"></tbody>
          </table></div>
        </section>
        <section class="shadow-lab" data-testid="crypto-signal-tournament-shadow" style="margin-top:14px;">
          <div class="section-title"><h3>15-Minute Signal Tournament V1 · Preregistered Forward Shadow</h3><span id="crypto-signal-tournament-status" class="health-chip">Waiting</span></div>
          <div class="shadow-note">Seven fixed hypotheses compete from the same fresh executable books. Raw signal arms preserve the decision-time snapshot for research, but they are not treated as fills. Every new observation now also runs through a fresh executable-price, visible-depth, exact-fee, one-contract taker/FOK parity check with the same one-cent adverse-move rule used by production. The promoted spot lane is then overwritten with the exact live signal revalidation, risk, sizing, depth, and order result. Only the execution-parity result is production-fill evidence; there is no recovery, historical backfill, execution impact, or automatic promotion.</div>
          <div id="crypto-signal-tournament-funnel" class="shadow-note">Waiting for the first fresh registered signals.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">Forward Records</div><div id="crypto-signal-tournament-records" class="value">0</div></div>
            <div class="stat"><div class="label">Settled</div><div id="crypto-signal-tournament-settled" class="value">0</div></div>
            <div class="stat"><div class="label">Snapshot Leader · Diagnostic</div><div id="crypto-signal-tournament-leader" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Interim Eligible</div><div id="crypto-signal-tournament-interim" class="value">0 / 7</div></div>
            <div class="stat"><div class="label">Final Eligible</div><div id="crypto-signal-tournament-final" class="value">0 / 7</div></div>
            <div class="stat"><div class="label">Per-Hypothesis Bound</div><div id="crypto-signal-tournament-confidence" class="value">99.3%</div></div>
            <div class="stat"><div class="label">Parity Fillable / Tested</div><div id="crypto-signal-parity-fillable" class="value">0 / 0</div></div>
            <div class="stat"><div class="label">Parity P&amp;L / ROI</div><div id="crypto-signal-parity-profit" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Exact Live Policy</div><div id="crypto-signal-live-parity" class="value">0 attempts</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Hypothesis</th><th class="num">Markets / Days</th><th class="num">Record</th><th class="num">Snapshot P&amp;L / ROI</th><th class="num">Parity Fill / Tested</th><th class="num">Parity P&amp;L / ROI</th><th class="num">Signal − Opposite</th><th class="num">Day Lower</th><th>Review</th></tr></thead>
            <tbody id="crypto-signal-tournament-lanes"></tbody>
          </table></div>
          <div class="table-scroll xwide" style="margin-top:12px;"><table class="compact-table">
            <thead><tr><th>Time</th><th>Hypothesis</th><th>Asset / Contract</th><th>Signal</th><th>Opposite</th><th class="num">Strength</th><th>Execution Parity</th><th>Result</th><th class="num">Signal / Opposite / Δ P&amp;L</th></tr></thead>
            <tbody id="crypto-signal-tournament-recent"></tbody>
          </table></div>
        </section>
        <section class="shadow-lab" data-testid="crypto-execution-lab-shadow" style="margin-top:14px;">
          <div class="section-title"><h3>Production-Parity Execution Lab V1 · Fresh Forward Shadow</h3><span id="crypto-execution-lab-status" class="health-chip">Waiting</span></div>
          <div class="shadow-note">Nine prospective hypotheses address the observed execution weakness: confirmed mid-price flow, late-window and BTC/ETH replications, a NO-side diagnostic, multi-horizon flow persistence, fresh no-chase spot leads, fresh spot-plus-flow consensus, delayed pullback re-entry, and settlement-distance favorites. Every scored entry uses refreshed Coinbase/Kraken inputs, a fresh Kalshi book, authoritative fees, full configured-size visible depth, a second FOK-style quote confirmation, and official settlement. Raw snapshots and rejected quotes never count as fills. This lab cannot place orders or promote itself.</div>
          <div id="crypto-execution-lab-funnel" class="shadow-note">Waiting for the first production-parity scan.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">Tracked / Settled</div><div id="crypto-execution-lab-records" class="value">0 / 0</div></div>
            <div class="stat"><div class="label">Would Submit / Attempts</div><div id="crypto-execution-lab-attempts" class="value">0 / 0</div></div>
            <div class="stat"><div class="label">Pending Pullbacks</div><div id="crypto-execution-lab-pending" class="value">0</div></div>
            <div class="stat"><div class="label">Production-Parity Leader</div><div id="crypto-execution-lab-leader" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Manual Review Ready</div><div id="crypto-execution-lab-ready" class="value">0 / 9</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Hypothesis</th><th class="num">Submit / Attempts</th><th class="num">Markets / Days</th><th class="num">Record</th><th class="num">P&amp;L / ROI</th><th class="num">First / Second Half</th><th class="num">Day 90% Lower</th><th>Review</th></tr></thead>
            <tbody id="crypto-execution-lab-lanes"></tbody>
          </table></div>
          <div class="table-scroll xwide" style="margin-top:12px;"><table class="compact-table">
            <thead><tr><th>Time</th><th>Hypothesis</th><th>Asset / Contract</th><th>Side</th><th class="num">Signal / Confirmed</th><th class="num">Contracts</th><th>Production Decision</th></tr></thead>
            <tbody id="crypto-execution-lab-recent"></tbody>
          </table></div>
        </section>
        <section class="shadow-lab" data-testid="crypto-research-expansion" style="margin-top:14px;">
          <h3>New Crypto Shadow Studies</h3>
          <div id="crypto-expansion-status" class="shadow-note">Waiting for the research companion.</div>
          <div class="table-wrap"><table><thead><tr><th>Study</th><th>Evidence</th><th>Result</th><th>Comparison / limitation</th></tr></thead><tbody id="crypto-expansion-studies"></tbody></table></div>
          <div id="crypto-expansion-coverage" class="shadow-note"></div>
          <div id="crypto-expansion-rejections" class="shadow-note"></div>
          <div class="shadow-note">Separate registered studies: do not add their profits. All trading results are simulated. The one-hour reversal study scores forecasts only. No automatic live promotion.</div>
        </section>
        <section class="shadow-lab" data-testid="crypto-btc-random-shadow" style="margin-top:14px;">
          <div class="section-title"><h3>Bitcoin · Random and Prediction Cycles</h3><span class="health-chip good">SHADOW ONLY</span></div>
          <div id="crypto-btc-random-status" class="shadow-note">Waiting for the BTC shadow companion.</div>
          <div class="table-scroll xwide"><table class="compact-table"><thead><tr><th>Strategy</th><th>Cash / Open risk</th><th>Net / Extra-cost P&amp;L</th><th>Wins / Settled / Missed</th><th>Drawdown / Daily losses</th><th>Current cycle</th><th>Confidence / Stake</th></tr></thead><tbody id="crypto-btc-random-arms"></tbody></table></div>
          <div id="crypto-btc-random-forecast" class="shadow-note"></div>
          <div id="crypto-btc-random-rejections" class="shadow-note"></div>
          <div class="shadow-note">Each strategy starts with $5,000 and a $5 base unit. Entry odds −150 to +200; integer contracts and fees must fit the unit. Directions lock before the 15-minute start. One open bet per strategy; delayed settlement can skip windows. Recovery is capped at $1.25 extra, total risk at $10, daily losses at $50, and drawdown at $250. Confidence is experimental model support, not proven accuracy. A large bankroll does not make random betting profitable. Simulations overlap; do not add their profits.</div>
        </section>
        <section class="shadow-lab" data-testid="crypto-btc-value-shadow" style="margin-top:14px;">
          <div class="section-title"><h3>Bitcoin · Value Entry Experiments</h3><span class="health-chip good">SHADOW ONLY</span></div>
          <div id="crypto-btc-value-status" class="shadow-note">Waiting for registration.</div>
          <div class="table-scroll xwide"><table class="compact-table"><thead><tr><th>Strategy</th><th>Cash / Open risk</th><th>Net / Extra-cost P&amp;L</th><th>Wins / Settled / Missed</th><th>Drawdown / Daily losses</th><th>Current cycle</th><th>Review progress</th></tr></thead><tbody id="crypto-btc-value-arms"></tbody></table></div>
          <div id="crypto-btc-value-paired" class="shadow-note"></div>
          <div id="crypto-btc-value-rejections" class="shadow-note"></div>
          <div class="shadow-note">Three separate $5,000 portfolios, $5 fee-inclusive base stakes. Enter at 33⅓–49¢ in the first five minutes with spread at most 2¢. Patient entry uses the same random draw and waits at most 60 seconds after the reference fill for 1¢ better. Forecast fade takes the opposite of a fresh pre-start prediction. Daily gross-loss cap $50; drawdown reduces stakes at $100 and stops entries at $250. Review after 200 settled markets and ten entry days. Experiments overlap; do not add profits. No automatic live promotion.</div>
        </section>
        <section class="shadow-lab" data-testid="crypto-sizing-research" style="margin-top:14px;">
          <h3>ETH Persistence · Entry Timing and Dynamic Sizing</h3>
          <div id="crypto-sizing-status" class="shadow-note">Waiting for the sizing companion.</div>
          <div class="table-scroll xwide"><table class="compact-table"><thead><tr><th>Entry / Sizing</th><th>Filled / Opportunities / Days</th><th>Average Contracts</th><th>Net / Extra-cost P&amp;L</th><th>ROI / Dollars Risked</th><th>Realized Drawdown / Open Risk</th><th>Extra-cost Difference vs Same-entry Fixed</th></tr></thead><tbody id="crypto-sizing-arms"></tbody></table></div>
          <div id="crypto-sizing-rejections" class="shadow-note"></div>
          <div class="shadow-note">Each row has its own $500 simulated bankroll. Immediate and patient entries each compare fixed 3 contracts, signal sizing (1–5), drawdown reduction, capped recovery (+1 only on strong signals), and qualified probability-bound sizing. Patient entries wait up to 60 seconds for a 1¢ improvement. Costs and visible depth are checked for every size. Results overlap: do not add rows. Signal strength is not a calibrated win probability. No automatic live promotion.</div>
        </section>
        <section class="shadow-lab" data-testid="crypto-prospective-research" style="margin-top:14px;">
          <div class="section-title"><h3>Prospective Crypto Experiments</h3><span class="health-chip good">SHADOW ONLY</span></div>
          <div id="crypto-prospective-registration" class="shadow-note">Waiting for registration.</div>
          <div id="crypto-prospective-portfolio" class="shadow-note"></div>
          <div class="table-wrap"><table><thead><tr><th>Primary strategy</th><th>Markets / expiries / dates</th><th>Net / stressed P&amp;L</th><th>Day / expiry lower bound</th><th>Fixed review</th></tr></thead><tbody id="crypto-prospective-lanes"></tbody></table></div>
          <div id="crypto-prospective-rejections" class="shadow-note"></div>
          <div class="shadow-note">ETH flow and persistence use a fixed ticker assignment. Controls are diagnostic. Strategy results overlap; only the shared virtual portfolio combines allocations. Fees use conservative cash rounding. Live activation requires a separate manual decision.</div>
        </section>
        <section class="shadow-lab" data-testid="crypto-asset-specialist-shadow" style="margin-top:14px;">
          <div class="section-title"><h3>ETH/XRP Legacy Specialist · Historical Evidence</h3><span id="crypto-asset-specialist-status" class="health-chip">Waiting</span></div>
          <div class="shadow-note">Six hypotheses were frozen after the broad lab exposed a post-hoc asset split: ETH core, ETH late, ETH NO, XRP multi-horizon persistence, XRP late, and one de-duplicated ETH/XRP portfolio. No historical records are backfilled. Every entry reuses the production-parity fresh book, exact fee, configured-size visible depth, and second FOK-style confirmation. The portfolio lane emits at most one position per ticker and uses five contracts only when independent horizons agree. Familywise 95% review uses a Bonferroni-adjusted bound across all six hypotheses. This lab cannot place orders or promote itself.</div>
          <div id="crypto-asset-specialist-funnel" class="shadow-note">Waiting for fresh post-registration ETH/XRP signals.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">Tracked / Settled</div><div id="crypto-asset-specialist-records" class="value">0 / 0</div></div>
            <div class="stat"><div class="label">Specialist Leader</div><div id="crypto-asset-specialist-leader" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Portfolio Volume</div><div id="crypto-asset-specialist-volume" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Portfolio P&amp;L / ROI</div><div id="crypto-asset-specialist-portfolio" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Per-Lane Confidence</div><div id="crypto-asset-specialist-confidence" class="value">99.17%</div></div>
            <div class="stat"><div class="label">Manual Review Ready</div><div id="crypto-asset-specialist-ready" class="value">0 / 6</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Hypothesis</th><th class="num">Submit / Attempts</th><th class="num">Markets / Days</th><th class="num">Record</th><th class="num">P&amp;L / ROI</th><th class="num">First / Second Half</th><th class="num">Expiry / Day Lower</th><th>Review</th></tr></thead>
            <tbody id="crypto-asset-specialist-lanes"></tbody>
          </table></div>
          <div class="table-scroll xwide" style="margin-top:12px;"><table class="compact-table">
            <thead><tr><th>Time</th><th>Hypothesis</th><th>Asset / Contract</th><th>Side</th><th class="num">Signal / Confirmed</th><th class="num">Contracts</th><th>Production Decision</th></tr></thead>
            <tbody id="crypto-asset-specialist-recent"></tbody>
          </table></div>
        </section>
        <section class="shadow-lab" data-testid="crypto-complement-arb-shadow" style="margin-top:14px;">
          <div class="section-title"><h3>Complementary Maker Capture V1 · YES/NO Shadow</h3><span id="crypto-complement-arb-status" class="health-chip">Waiting</span></div>
          <div class="shadow-note">This lane measures the apparent arbitrage of resting one YES bid and one NO bid on the same contract. A leg counts as filled only after a later public trade strictly crosses its limit; queue position is not assumed. Both-fill, single-leg, and no-fill outcomes all remain in the denominator, so legging risk cannot disappear from the result. A separate taker-parity diagnostic checks exact executable two-leg cost and treats crossed or inconsistent books as integrity failures. It is shadow-only and cannot place or promote orders.</div>
          <div id="crypto-complement-arb-funnel" class="shadow-note">Waiting for the first qualified two-sided book.</div>
          <div class="grid shadow-stats">
            <div class="stat"><div class="label">Tracked / Working</div><div id="crypto-complement-arb-tracked" class="value">0 / 0</div></div>
            <div class="stat"><div class="label">Settled / Days</div><div id="crypto-complement-arb-settled" class="value">0 / 0</div></div>
            <div class="stat"><div class="label">Both / Single / None</div><div id="crypto-complement-arb-fills" class="value">0 / 0 / 0</div></div>
            <div class="stat"><div class="label">All-outcome P&amp;L / ROI</div><div id="crypto-complement-arb-profit" class="value">$0.00 / 0.0%</div></div>
            <div class="stat"><div class="label">95% Day Lower</div><div id="crypto-complement-arb-lower" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Minimum Taker Pair Cost</div><div id="crypto-complement-arb-parity" class="value">Collecting</div></div>
            <div class="stat"><div class="label">Interim / Final Review</div><div id="crypto-complement-arb-review" class="value">COLLECTING</div></div>
          </div>
          <div class="table-scroll xwide"><table class="compact-table">
            <thead><tr><th>Time</th><th>Asset / Contract</th><th class="num">YES / NO Limits</th><th class="num">Locked If Both</th><th>Fill Outcome</th><th>Market Result</th><th class="num">Realized Shadow P&amp;L</th></tr></thead>
            <tbody id="crypto-complement-arb-recent"></tbody>
          </table></div>
        </section>
        <h3>Recent Virtual Attempts</h3>
        <div class="table-scroll xwide"><table class="compact-table">
          <thead><tr><th>Time</th><th>Bot / Episode</th><th>Asset</th><th>Side</th><th>Lane / Attempt</th><th class="num">Entry</th><th class="num">Contracts</th><th class="num">Cost</th><th class="num">Model / Market</th><th class="num">Target σ</th><th class="num">Advantage</th><th>Maker CF</th><th>Result</th><th class="num">V6 / Flat P&amp;L</th></tr></thead>
          <tbody id="crypto-cycle-shadow-recent"></tbody>
        </table></div>
      </details>
    </section>

    <section class="view" data-view="perps-shadow">
      <h2>Analytics</h2>
      <div class="subtabs" aria-label="Analytics bot">
        <button type="button" data-analytics-tab="sports" onclick="showAnalytics('sports')">Sports</button>
        <button type="button" data-analytics-tab="crypto" onclick="showAnalytics('crypto')">Crypto</button>
        <button type="button" class="active" data-analytics-tab="perps" onclick="showAnalytics('perps')">Perpetuals Paper Lab</button>
      </div>
      <h3>Crypto Perpetuals · Paper Shadow Only</h3>
      <div class="notice" id="perps-status-note">This research engine has no authenticated order path. Every position, fee, funding payment, stop, target, and exit is simulated in an isolated paper ledger.</div>
      <div class="grid stats" style="grid-template-columns: repeat(6, minmax(130px, 1fr)); padding: 12px;">
        <div class="stat"><div class="label">Mode</div><div id="perps-mode" class="value">Waiting</div></div>
        <div class="stat"><div class="label">Paper Equity</div><div id="perps-equity" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Total P&L</div><div id="perps-pnl" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Active Markets</div><div id="perps-markets" class="value">0</div></div>
        <div class="stat"><div class="label">Eligible / Candidates</div><div id="perps-eligible" class="value">0 / 0</div></div>
        <div class="stat"><div class="label">Open / Settled</div><div id="perps-counts" class="value">0 / 0</div></div>
        <div class="stat"><div class="label">Paper Record</div><div id="perps-record" class="value">0W / 0L</div></div>
        <div class="stat"><div class="label">Funding P&L</div><div id="perps-funding" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Validated Assets</div><div id="perps-validated" class="value">0 / 0</div></div>
        <div class="stat"><div class="label">Replay Win % / Avg Net</div><div id="perps-walk-forward" class="value">0% / 0%</div></div>
        <div class="stat"><div class="label">Forecasts Open / Settled</div><div id="perps-forecast-counts" class="value">0 / 0</div></div>
      </div>
      <section>
        <h3>Current Paper Strategy</h3>
        <div id="perps-strategy" class="insight-list"></div>
      </section>
      <div class="grid two analytics-block">
        <section>
          <h3>Current Signal Candidates</h3>
          <div class="table-scroll wide"><table class="compact-table">
            <thead><tr><th>Asset</th><th>Side</th><th class="num">Signal</th><th class="num">Confidence</th><th class="num">Spread</th><th class="num">Replay Win %</th><th class="num">Replay Avg Net</th><th>Decision</th></tr></thead>
            <tbody id="perps-candidates"></tbody>
          </table></div>
        </section>
        <section>
          <h3>Open Paper Positions</h3>
          <div class="table-scroll"><table class="compact-table">
            <thead><tr><th>Asset</th><th>Side</th><th class="num">Notional</th><th class="num">Entry</th><th class="num">Unrealized P&L</th></tr></thead>
            <tbody id="perps-open"></tbody>
          </table></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h3>Settled Paper Positions</h3>
          <div class="table-scroll"><table class="compact-table">
            <thead><tr><th>Asset</th><th>Result</th><th>Exit</th><th class="num">P&L</th><th class="num">Hours Held</th></tr></thead>
            <tbody id="perps-history"></tbody>
          </table></div>
        </section>
        <section>
          <h3>Forward Forecast Scorecard</h3>
          <div class="table-scroll"><table class="compact-table">
            <thead><tr><th>Model</th><th class="num">Horizon</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Avg Net</th></tr></thead>
            <tbody id="perps-forecasts"></tbody>
          </table></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h3>Historical Specialist Replay</h3>
          <div class="table-scroll"><table class="compact-table">
            <thead><tr><th>Model</th><th class="num">Trades</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Avg Net</th><th class="num">Profitable Assets</th></tr></thead>
            <tbody id="perps-historical-models"></tbody>
          </table></div>
        </section>
        <section>
          <h3>Latest Paper-Lab Activity</h3>
          <div id="perps-logs" class="logs"></div>
        </section>
      </div>
    </section>

    <section class="view" data-view="crypto-control">
      <h2>Crypto Control</h2>
      <div class="control-backbar">
        <button onclick="showView('control')">Back to Control Center</button>
      </div>
      <div class="grid two">
        <div>
          <div class="notice">Crypto settings save to crypto_settings.json. Live mode writes separate live portfolio, report, event, and log files.</div>
          <div class="settings-grid" style="padding-top:12px;">
            <!-- controls:
            CRYPTO_GROK_ENABLED CRYPTO_GROK_WEB_SEARCH_ENABLED CRYPTO_EXECUTION_MODE
            CRYPTO_15M_CAMPAIGN_BOT_COUNT CRYPTO_15M_UNIT_STAKING_ENABLED CRYPTO_15M_UNIT_SIZE_PCT
            CRYPTO_15M_UNIT_MAX_PER_MARKET CRYPTO_15M_UNIT_MIN_DATA_QUALITY
            CRYPTO_15M_UNIT_WIN_PROB_MODEL_WEIGHT CRYPTO_15M_UNIT_WIN_PROB_SHADOW_MAX_WEIGHT
            CRYPTO_15M_UNIT_MIN_CALIBRATION_RESERVE_PP CRYPTO_15M_MODEL_MATURITY_CAP_ENABLED
            CRYPTO_15M_MODEL_UNQUALIFIED_MAX_UNITS CRYPTO_15M_MODEL_TIER_2_MIN_MARKETS
            CRYPTO_15M_MODEL_TIER_3_MIN_MARKETS CRYPTO_15M_MODEL_TIER_4_MIN_MARKETS
            CRYPTO_15M_MODEL_TIER_5_MIN_MARKETS CRYPTO_15M_MODEL_TIER_MAX_CALIBRATION_GAP
            CRYPTO_15M_UNIT_KELLY_FRACTION CRYPTO_15M_GLOBAL_DAILY_LOSS_UNITS
            CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_ENABLED CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_MIN_UNITS
            CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS CRYPTO_LIVE_QUOTE_REFRESH_PRIORITY_MAX_ADVERSE_CENTS
            CRYPTO_LIVE_SLIPPAGE_RECONFIRM_ENABLED CRYPTO_LIVE_SLIPPAGE_RECONFIRM_SECONDS
            CRYPTO_LIVE_SLIPPAGE_RECONFIRM_MAX_MOVE_CENTS CRYPTO_15M_UNIT_RECOVERY_ENABLED
            CRYPTO_15M_POOLED_RECOVERY_ENABLED CRYPTO_15M_RECOVERY_MAX_OVERLAYS_PER_EXPIRY
            CRYPTO_15M_UNIT_RECOVERY_MIN_BASE_UNITS CRYPTO_15M_UNIT_RECOVERY_MIN_PROBABILITY_UNITS
            CRYPTO_15M_UNIT_RECOVERY_TARGET_DRAWDOWN_FRACTION CRYPTO_15M_UNIT_RECOVERY_MAX_BONUS_UNITS
            CRYPTO_15M_UNIT_RECOVERY_MAX_TOTAL_UNITS CRYPTO_15M_UNIT_HALF_MIN_WIN_PROB
            CRYPTO_15M_UNIT_HALF_MIN_WIN_PROB_LOW CRYPTO_15M_UNIT_HALF_MIN_CONFIDENCE
            CRYPTO_15M_UNIT_HALF_MIN_EDGE CRYPTO_15M_UNIT_HALF_MIN_EDGE_LOW CRYPTO_15M_UNIT_1_MIN_WIN_PROB
            CRYPTO_15M_UNIT_1_MIN_WIN_PROB_LOW CRYPTO_15M_UNIT_1_MIN_CONFIDENCE CRYPTO_15M_UNIT_1_MIN_EDGE
            CRYPTO_15M_UNIT_1_MIN_EDGE_LOW CRYPTO_15M_UNIT_2_MIN_WIN_PROB CRYPTO_15M_UNIT_2_MIN_WIN_PROB_LOW
            CRYPTO_15M_UNIT_2_MIN_CONFIDENCE CRYPTO_15M_UNIT_2_MIN_EDGE CRYPTO_15M_UNIT_2_MIN_EDGE_LOW
            CRYPTO_15M_UNIT_3_MIN_WIN_PROB CRYPTO_15M_UNIT_3_MIN_WIN_PROB_LOW CRYPTO_15M_UNIT_3_MIN_CONFIDENCE
            CRYPTO_15M_UNIT_3_MIN_EDGE CRYPTO_15M_UNIT_3_MIN_EDGE_LOW CRYPTO_15M_UNIT_4_MIN_WIN_PROB
            CRYPTO_15M_UNIT_4_MIN_WIN_PROB_LOW CRYPTO_15M_UNIT_4_MIN_CONFIDENCE CRYPTO_15M_UNIT_4_MIN_EDGE
            CRYPTO_15M_UNIT_4_MIN_EDGE_LOW CRYPTO_15M_UNIT_5_MIN_WIN_PROB CRYPTO_15M_UNIT_5_MIN_WIN_PROB_LOW
            CRYPTO_15M_UNIT_5_MIN_CONFIDENCE CRYPTO_15M_UNIT_5_MIN_EDGE CRYPTO_15M_UNIT_5_MIN_EDGE_LOW
            CRYPTO_15M_SAME_EXPIRY_ELITE_MIN_EDGE CRYPTO_15M_SAME_EXPIRY_ELITE_MIN_CONFIDENCE
            CRYPTO_15M_LOW_EDGE_PILOT_ENABLED CRYPTO_15M_LOW_EDGE_PILOT_MIN_EDGE
            CRYPTO_15M_LOW_EDGE_PILOT_MAX_EDGE CRYPTO_15M_LOW_EDGE_PILOT_MIN_CONFIDENCE
            CRYPTO_15M_LOW_EDGE_PILOT_MIN_PRICE_CENTS CRYPTO_15M_LOW_EDGE_PILOT_MAX_PRICE_CENTS
            CRYPTO_15M_LOW_EDGE_PILOT_MAX_OPEN CRYPTO_15M_LOW_EDGE_PILOT_MAX_SETTLED CRYPTO_LIVE_ORDER_ENABLED
            CRYPTO_LIVE_DRY_RUN CRYPTO_LIVE_REQUIRE_CONFIRMATION CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL
            SHARED_CRYPTO_ISOLATED_RISK_ENABLED SHARED_CRYPTO_DAILY_LOSS_CAP SHARED_CRYPTO_DAILY_LOSS_PCT
            SHARED_CRYPTO_MAX_OPEN_EXPOSURE_CAP SHARED_CRYPTO_MAX_OPEN_EXPOSURE_PCT
            CRYPTO_LIVE_FALLBACK_TO_PAPER CRYPTO_LIVE_MAX_STAKE CRYPTO_LIVE_TIME_IN_FORCE
            CRYPTO_RECONCILE_LIVE_ON_SCAN CRYPTO_NEWS_ENABLED CRYPTO_FREE_RSS_ENABLED CRYPTO_FREE_RSS_URLS
            CRYPTO_PROBABILITY_ENSEMBLE_ENABLED CRYPTO_MARKET_ANCHOR_WEIGHT CRYPTO_EXTREME_PROB_SHRINK
            CRYPTO_PRICE_DISCIPLINE_ENABLED CRYPTO_FAVORITE_PRICE_MIN_CENTS CRYPTO_FAVORITE_PRICE_MAX_CENTS
            CRYPTO_FAVORITE_MIN_EDGE CRYPTO_FAVORITE_MIN_CONFIDENCE CRYPTO_HEAVY_FAVORITE_PRICE_MAX_CENTS
            CRYPTO_HEAVY_FAVORITE_MIN_EDGE CRYPTO_HEAVY_FAVORITE_MIN_CONFIDENCE CRYPTO_AI_VOTING_ENABLED
            CRYPTO_LOCAL_AI_ENABLED CRYPTO_LOCAL_AI_MODEL CRYPTO_LOCAL_AI_URL CRYPTO_OPENAI_ENABLED
            CRYPTO_OPENAI_API_KEY CRYPTO_OPENAI_MODEL CRYPTO_GROK_FALLBACK_ENABLED CRYPTO_GROK_API_KEY
            CRYPTO_NEWS_API_KEY CRYPTO_CRYPTOPANIC_API_KEY CRYPTO_GROK_MODEL CRYPTO_PERPS_SHADOW_ENABLED
            CRYPTO_PERPS_SHADOW_EXECUTION_ENABLED CRYPTO_PERPS_SHADOW_PROBATION_ENABLED
            CRYPTO_PERPS_SHADOW_STARTING_BALANCE CRYPTO_PERPS_SHADOW_MAX_OPEN
            CRYPTO_PERPS_SHADOW_MAX_SAME_DIRECTION CRYPTO_PERPS_SHADOW_NOTIONAL_PCT
            CRYPTO_PERPS_SHADOW_MAX_NOTIONAL CRYPTO_PERPS_SHADOW_MIN_SIGNAL_SCORE
            CRYPTO_PERPS_SHADOW_MIN_CONFIDENCE CRYPTO_PERPS_SHADOW_MAX_HOLD_HOURS
            CRYPTO_PERPS_SHADOW_LOOKBACK_DAYS CRYPTO_PERPS_SHADOW_VALIDATION_HOURS
            CRYPTO_PERPS_SHADOW_MIN_VALIDATION_TRADES CRYPTO_PERPS_SHADOW_MIN_VALIDATION_PROFIT_FACTOR
            CRYPTO_PERPS_SHADOW_PROBATION_MIN_SIGNAL_SCORE CRYPTO_PERPS_SHADOW_PROBATION_MIN_CONFIDENCE
            CRYPTO_PERPS_SHADOW_PROBATION_MAX_SPREAD_PCT CRYPTO_PERPS_SHADOW_PROBATION_NOTIONAL_PCT
            CRYPTO_PERPS_SHADOW_PROBATION_MAX_NOTIONAL CRYPTO_PERPS_SHADOW_FORECAST_MIN_SCORE
            -->
          </div>
          <div style="padding-top:10px; display:flex; gap:8px; flex-wrap:wrap;">
            <button class="primary" onclick="saveCryptoSettings()">Save Crypto Settings</button>
            <button onclick="controlProcess('crypto','start')">Run Crypto Scan</button>
            <button onclick="refresh()">Refresh</button>
          </div>
          <div id="crypto-settings-status" class="notice">Crypto settings load locally from crypto_settings.json.</div>
        </div>
        <div>
          <h3>API Key Sources</h3>
          <div class="notice">
            <div><strong>xAI / Grok:</strong> <a href="https://console.x.ai/" target="_blank" rel="noopener">console.x.ai</a></div>
            <div><strong>NewsAPI:</strong> <a href="https://newsapi.org/register" target="_blank" rel="noopener">newsapi.org/register</a></div>
            <div><strong>CryptoPanic:</strong> <a href="https://cryptopanic.com/developers/api/" target="_blank" rel="noopener">cryptopanic.com/developers/api</a></div>
            <div><strong>No-key feeds:</strong> Cointelegraph RSS, CoinDesk RSS, Decrypt RSS</div>
          </div>
          <div class="notice" style="margin-top:10px;">Coinbase prices, CoinGecko fallback, Kalshi public market data, Fear & Greed, and RSS headlines work without CryptoPanic. Live mode uses real Kalshi orders and shared bankroll reconciliation.</div>
        </div>
      </div>
    </section>

    <section class="view" data-view="analytics">
      <h2>Analytics</h2>
      <div class="subtabs" aria-label="Analytics bot">
        <button type="button" class="active" data-analytics-tab="sports" onclick="showAnalytics('sports')">Sports</button>
        <button type="button" data-analytics-tab="crypto" onclick="showAnalytics('crypto')">Crypto</button>
        <button type="button" data-analytics-tab="perps" onclick="showAnalytics('perps')">Perpetuals Paper Lab</button>
      </div>
      <details class="diagnostic-panel source-review"><summary>Source reconciliation &amp; live spread review</summary>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">All Sports Ledger P&amp;L</div><div id="sports-account-pnl" class="value">$0.00</div></div>
          <div class="stat"><div class="label">All Sports MTD P&amp;L</div><div id="sports-account-mtd" class="value">$0.00</div></div>
          <div class="stat"><div class="label">AIBetPicks P&amp;L</div><div id="sports-aibetpicks-pnl" class="value">$0.00</div><div id="sports-aibetpicks-units" class="muted"></div></div>
          <div class="stat"><div class="label">User Live P&amp;L</div><div id="sports-user-pnl" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Historical System Test P&amp;L</div><div id="sports-test-pnl" class="value">$0.00</div></div>
        </div>
        <div class="analytics-block" style="margin:14px 0;">
          <h3>Sports Results by Source</h3>
          <div id="sports-audit-coverage" class="notice">Waiting for an audited scan.</div>
          <div id="sports-build-summary" class="notice"></div>
          <div id="sports-modest-recovery" class="notice"></div>
          <div class="table-scroll"><table>
            <thead><tr><th>Source</th><th>Window</th><th>Basis</th><th class="num">Fills</th><th class="num">Events</th><th class="num">ROI after fees</th><th class="num">Net units</th></tr></thead>
            <tbody id="sports-audit-results"></tbody>
          </table></div>
          <h3>Live Spread Review · Last 30 Days</h3>
          <div id="sports-spread-summary" class="notice"></div>
          <label>Compare by <select id="sports-spread-dimension" onchange="renderSportsSpreadAudit(window.latestSportsAuditReport || {})">
            <option value="league_line_timing">League, signed line and timing</option>
            <option value="game_phase">Game phase</option>
            <option value="exact_line">Exact-line verification</option>
            <option value="book_agreement">Bookmaker agreement</option>
            <option value="book_families">Independent bookmakers</option>
            <option value="oldest_quote_age">Oldest quote age</option>
            <option value="score_context">Score freshness</option>
            <option value="strategy_build">Code build</option>
          </select></label>
          <div class="table-scroll"><table>
            <thead><tr><th>Segment</th><th class="num">Fills</th><th class="num">Events</th><th class="num">Profit after fees</th><th class="num">ROI</th></tr></thead>
            <tbody id="sports-spread-results"></tbody>
          </table></div>
          <div class="notice">Spread results describe actual autonomous live entries. Missing historical evidence stays unverified. These comparisons do not automatically change entry rules or stake sizes.</div>
          <div class="notice">Manual bets are excluded. Multiple fills can belong to one event. Unit results use the reference unit value recorded at entry; small samples do not establish an edge.</div>
        </div>
      </details>
      <h3>Regular Sports Bot Performance</h3>
      <div class="notice" id="analytics-note">The headline statistics use the complete regular-bot ledger and exclude legacy imported picks and user-entered live bets. Detailed charts and tables use the recent bounded rows loaded by the dashboard; the separate source panels below use complete ledger aggregates.</div>
      <div class="grid stats" style="grid-template-columns: repeat(6, minmax(130px, 1fr)); padding: 12px;">
        <div class="stat"><div class="label">Settled Sports</div><div id="analytics-settled" class="value">0</div></div>
        <div class="stat"><div class="label">Sports ROI</div><div id="analytics-roi" class="value">0%</div></div>
        <div class="stat"><div class="label">Avg Stake</div><div id="analytics-avg-stake" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Avg Win</div><div id="analytics-avg-win" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Avg Loss</div><div id="analytics-avg-loss" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Open Sports Risk</div><div id="analytics-open-risk" class="value">$0.00</div></div>
      </div>
      <div class="grid stats" style="grid-template-columns: repeat(5, minmax(130px, 1fr)); padding: 0 12px 12px;">
        <div class="stat"><div class="label">Last 24h Settled</div><div id="analytics-24h-settled" class="value">0</div></div>
        <div class="stat"><div class="label">Last 24h ROI</div><div id="analytics-24h-roi" class="value">0%</div></div>
        <div class="stat"><div class="label">Last 24h Profit</div><div id="analytics-24h-profit" class="value">$0.00</div></div>
        <div class="stat"><div class="label">35c-55c Bets</div><div id="analytics-focus-count" class="value">0</div></div>
        <div class="stat"><div class="label">35c-55c ROI</div><div id="analytics-focus-roi" class="value">0%</div></div>
      </div>
      <div class="grid stats" style="grid-template-columns: repeat(6, minmax(130px, 1fr)); padding: 0 12px 12px;">
        <div class="stat"><div class="label">Yesterday Settled P&amp;L</div><div id="analytics-yesterday-settled" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Yesterday Decision P&amp;L</div><div id="analytics-yesterday-decisions" class="value">$0.00</div></div>
        <div class="stat"><div class="label">Peak Concurrent Units</div><div id="analytics-peak-units" class="value">0u</div></div>
        <div class="stat"><div class="label">Authoritative State</div><div id="analytics-state-quality" class="value">0%</div></div>
        <div class="stat"><div class="label">Average AI Latency</div><div id="analytics-ai-latency" class="value">n/a</div></div>
        <div class="stat"><div class="label">Post-AI Edge Retention</div><div id="analytics-edge-retention" class="value">n/a</div></div>
      </div>
      <div class="daily-performance-panel" aria-labelledby="sports-daily-performance-title">
        <div class="performance-toolbar">
          <h3 id="sports-daily-performance-title">Regular Bot Daily P&amp;L</h3>
          <div class="range-buttons" data-performance-range="sports" aria-label="Sports daily profit chart range">
            <button type="button" data-range="7d" onclick="setPerformanceRange('sports','7d')">7D</button>
            <button type="button" class="active" data-range="30d" onclick="setPerformanceRange('sports','30d')">30D</button>
            <button type="button" data-range="90d" onclick="setPerformanceRange('sports','90d')">90D</button>
            <button type="button" data-range="all" onclick="setPerformanceRange('sports','all')">All</button>
          </div>
        </div>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">Period P&amp;L</div><div id="sports-period-profit" class="value">$0.00</div></div>
          <div class="stat"><div class="label">Period ROI</div><div id="sports-period-roi" class="value">0.0%</div></div>
          <div class="stat"><div class="label">Period Record</div><div id="sports-period-record" class="value">0W / 0L</div></div>
          <div class="stat"><div class="label">Average Per Day</div><div id="sports-period-average" class="value">$0.00</div></div>
        </div>
        <div class="chart-wrap"><canvas id="sports-daily-pnl-chart" role="img" aria-label="Sports bot daily profit and loss"></canvas></div>
        <div id="sports-daily-performance-summary" class="performance-summary">Waiting for settled sports results.</div>
      </div>
      <section class="shadow-lab" data-testid="sports-shadow-lab">
        <h3>Shadow Lab · Fee + Correlation Policy</h3>
        <div class="shadow-note">Evaluates sports candidates after estimated entry fees and against same-game/sport concentration. This is observation-only until its tracked results justify a separately approved live promotion.</div>
        <div class="grid shadow-stats">
          <div class="stat"><div class="label">Mode</div><div id="sports-shadow-mode" class="value">Waiting</div></div>
          <div class="stat"><div class="label">Raw +EV</div><div id="sports-shadow-raw" class="value">0</div></div>
          <div class="stat"><div class="label">Fee +EV</div><div id="sports-shadow-fee-positive" class="value">0</div></div>
          <div class="stat"><div class="label">Net Threshold</div><div id="sports-shadow-threshold" class="value">0</div></div>
          <div class="stat"><div class="label">Correlation Approved</div><div id="sports-shadow-approved" class="value">0</div></div>
          <div class="stat"><div class="label">Shadow Stake</div><div id="sports-shadow-stake" class="value">$0.00</div></div>
        </div>
        <div id="sports-shadow-outcomes" class="shadow-note">Outcome tracking begins when shadow-tagged bets settle.</div>
        <div class="shadow-layout">
          <div class="shadow-table-wrap"><table class="compact-table">
            <thead><tr><th>Opportunity</th><th>Group</th><th class="num">Entry</th><th class="num">Raw Edge</th><th class="num">Net Edge</th><th class="num">Fee Drag</th><th class="num">Kelly</th><th class="num">After Correlation</th><th>Decision</th></tr></thead>
            <tbody id="sports-shadow-opportunities"></tbody>
          </table></div>
          <div class="shadow-table-wrap"><table class="compact-table"><thead><tr><th>Shadow Filter</th><th class="num">Count</th></tr></thead><tbody id="sports-shadow-reasons"></tbody></table></div>
        </div>
      </section>
      <div class="grid two analytics-block">
        <section>
          <h2>User Live Bets · Complete Ledger</h2>
          <div class="grid stats" style="grid-template-columns: repeat(4, minmax(110px, 1fr)); padding: 10px 0;">
            <div class="stat"><div class="label">Open Risk</div><div id="user-bet-open-risk" class="value">$0.00</div></div>
            <div class="stat"><div class="label">Record</div><div id="user-bet-record" class="value">0W/0L</div></div>
            <div class="stat"><div class="label">P/L</div><div id="user-bet-profit" class="value">$0.00</div></div>
            <div class="stat"><div class="label">ROI</div><div id="user-bet-roi" class="value">0%</div></div>
          </div>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Bet</th><th>Status</th><th class="num">Side</th><th class="num">Stake</th><th class="num">Entry</th><th class="num">P/L</th></tr></thead><tbody id="analytics-user-bets"></tbody></table></div>
        </section>
        <section>
          <h2>Recent User Live Bet Splits</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Market</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-user-market"></tbody></table></div>
        </section>
      </div>
      <div class="grid three">
        <section>
          <h2>Win Rate By Source</h2>
          <div class="chart-wrap"><canvas id="chart-source"></canvas></div>
          <div style="overflow:auto"><table><thead><tr><th>Source</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Profit</th></tr></thead><tbody id="analytics-source"></tbody></table></div>
        </section>
        <section>
          <h2>Confidence Buckets</h2>
          <div class="chart-wrap"><canvas id="chart-confidence"></canvas></div>
          <div style="overflow:auto"><table><thead><tr><th>Bucket</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Profit</th></tr></thead><tbody id="analytics-confidence"></tbody></table></div>
        </section>
        <section>
          <h2>Odds Profile</h2>
          <div class="chart-wrap"><canvas id="chart-odds"></canvas></div>
          <div style="overflow:auto"><table><thead><tr><th>Odds</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Profit</th></tr></thead><tbody id="analytics-odds"></tbody></table></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h2>Strategy Owners</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Owner</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-strategy-owner"></tbody></table></div>
        </section>
        <section>
          <h2>Strategy Owner Notes</h2>
          <div id="strategy-owner-insights" class="insight-list"></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h2>Live Campaign Bets</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Group</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-phase-two"></tbody></table></div>
        </section>
        <section>
          <h2>Live Campaign Notes</h2>
          <div id="phase-two-insights" class="insight-list"></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h2>Missed Fill Analytics</h2>
          <div class="grid stats" style="grid-template-columns: repeat(4, minmax(110px, 1fr)); padding: 10px 0;">
            <div class="stat"><div class="label">Would Record</div><div id="missed-fill-record" class="value">0W/0L</div></div>
            <div class="stat"><div class="label">Would P/L</div><div id="missed-fill-profit" class="value">$0.00</div></div>
            <div class="stat"><div class="label">Would ROI</div><div id="missed-fill-roi" class="value">0%</div></div>
            <div class="stat"><div class="label">Open Missed</div><div id="missed-fill-open" class="value">0</div></div>
          </div>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Split</th><th class="num">Record</th><th class="num">Stake</th><th class="num">Would P/L</th><th class="num">ROI</th></tr></thead><tbody id="analytics-missed-fill"></tbody></table></div>
        </section>
        <section>
          <h2>Latest Missed Fills</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Bet</th><th class="num">Result</th><th class="num">Stake</th><th class="num">Would P/L</th><th>Owner</th></tr></thead><tbody id="analytics-missed-fill-latest"></tbody></table></div>
        </section>
      </div>
      <div class="grid three analytics-block">
        <section>
          <h2>Profit By Market</h2>
          <div class="chart-wrap"><canvas id="chart-market-profit"></canvas></div>
        </section>
        <section>
          <h2>ROI By Timing</h2>
          <div class="chart-wrap"><canvas id="chart-timing-roi"></canvas></div>
        </section>
        <section>
          <h2>Log Pattern Counts</h2>
          <div class="chart-wrap"><canvas id="chart-log-patterns"></canvas></div>
        </section>
      </div>
      <div class="grid three analytics-block">
        <section>
          <h2>Market Types</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Type</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-market"></tbody></table></div>
        </section>
        <section>
          <h2>Timing Splits</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Timing</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-timing"></tbody></table></div>
        </section>
        <section>
          <h2>Sport Splits</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Sport</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-sport"></tbody></table></div>
        </section>
      </div>
      <div class="analytics-block">
        <section>
          <h2>Sport &times; Market &times; Unit Performance</h2>
          <p class="muted">Settled bot bets only. Five-minute CLV uses on-time fixed-horizon marks; its sample count is shown separately.</p>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Sport</th><th>Market</th><th class="num">Units</th><th class="num">Bets</th><th class="num">Record</th><th class="num">Avg edge</th><th class="num">5m CLV</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-sport-market-units"></tbody></table></div>
        </section>
      </div>
      <div class="grid three analytics-block">
        <section>
          <h2>Bot Splits</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Bot</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-bot"></tbody></table></div>
        </section>
        <section>
          <h2>Entry Price Buckets</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Price</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-price"></tbody></table></div>
        </section>
        <section>
          <h2>Preferred Price Band</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Band</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-price-focus"></tbody></table></div>
        </section>
        <section>
          <h2>Price-Tier Skips</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Reason</th><th class="num">Latest</th><th class="num">Recent Log</th></tr></thead><tbody id="analytics-price-skips"></tbody></table></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h2>Last 24h Price Band</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Band</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-24h-price-focus"></tbody></table></div>
        </section>
        <section>
          <h2>Stake Buckets</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Stake</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-stake"></tbody></table></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h2>Price By Market</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Price / Market</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-price-market"></tbody></table></div>
        </section>
        <section>
          <h2>Final Score By Market</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Final / Market</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-final-market"></tbody></table></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h2>Timing By Market</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Timing / Market</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-timing-market"></tbody></table></div>
        </section>
        <section>
          <h2>Owner By Market</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Owner / Market</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-owner-market"></tbody></table></div>
        </section>
      </div>
      <div class="grid three analytics-block">
        <section>
          <h2>CLV Buckets</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>CLV</th><th class="num">Record</th><th class="num">Win %</th><th class="num">Stake</th><th class="num">ROI</th><th class="num">Profit</th></tr></thead><tbody id="analytics-clv"></tbody></table></div>
        </section>
        <section>
          <h2>Best Wins</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Bet</th><th class="num">Stake</th><th class="num">Profit</th><th>Source</th></tr></thead><tbody id="analytics-best-wins"></tbody></table></div>
        </section>
        <section>
          <h2>Worst Losses</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Bet</th><th class="num">Stake</th><th class="num">Profit</th><th>Source</th></tr></thead><tbody id="analytics-worst-losses"></tbody></table></div>
        </section>
      </div>
      <div class="grid three analytics-block">
        <section>
          <h2>Open Exposure</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Group</th><th class="num">Open</th><th class="num">Exposure</th></tr></thead><tbody id="analytics-exposure"></tbody></table></div>
        </section>
        <section>
          <h2>Log Categories</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Pattern</th><th class="num">Count</th></tr></thead><tbody id="analytics-log-categories"></tbody></table></div>
        </section>
        <section>
          <h2>Skip Reasons</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Reason</th><th class="num">Count</th></tr></thead><tbody id="analytics-skip-reasons"></tbody></table></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h2>Reverse Strategy Test</h2>
          <div class="grid stats" style="grid-template-columns: repeat(4, minmax(110px, 1fr)); padding: 10px 0;">
            <div class="stat"><div class="label">Reverse Record</div><div id="reverse-record" class="value">0W/0L</div></div>
            <div class="stat"><div class="label">Reverse Profit</div><div id="reverse-profit" class="value">$0.00</div></div>
            <div class="stat"><div class="label">Reverse ROI</div><div id="reverse-roi" class="value">0%</div></div>
            <div class="stat"><div class="label">Reverse Delta</div><div id="reverse-delta" class="value">$0.00</div></div>
          </div>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Split</th><th class="num">Original</th><th class="num">Reverse</th><th class="num">Orig P/L</th><th class="num">Rev P/L</th><th class="num">Delta</th></tr></thead><tbody id="analytics-reverse"></tbody></table></div>
        </section>
        <section>
          <h2>Reverse Notes</h2>
          <div id="reverse-insights" class="insight-list"></div>
        </section>
      </div>
      <div class="grid two analytics-block">
        <section>
          <h2>Matcher Diagnostics</h2>
          <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Diagnostic</th><th class="num">Count</th></tr></thead><tbody id="analytics-diagnostics"></tbody></table></div>
        </section>
        <section>
          <h2>Interesting Log Notes</h2>
          <div id="analytics-insights" class="insight-list"></div>
        </section>
      </div>
        <details class="analytics-block diagnostic-panel" style="margin:14px 0;" data-testid="itf-shadow-panel">
          <summary>ITF research · shadow experiments and follow-ups</summary>
          <div class="section-title"><h3>ITF Upset Experiment · SHADOW ONLY</h3><span id="itf-shadow-health" class="health-chip">Waiting</span></div>
          <div id="itf-shadow-window" class="notice"></div>
          <div id="itf-shadow-coverage" class="notice"></div>
          <div id="itf-shadow-accounting" class="notice"></div>
          <div class="table-scroll"><table>
            <thead><tr><th>Strategy / Entry rule</th><th class="num">Bets / Open</th><th class="num">W / L / Other</th><th class="num">Net P&amp;L after fees</th><th class="num">ROI</th><th class="num">Open risk</th><th class="num">Settled drawdown</th></tr></thead>
            <tbody id="itf-shadow-strategies"></tbody>
          </table></div>
          <details style="margin-top:10px;"><summary>Shadow picks and results · exact units</summary>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0;">
              <label>Results <select id="itf-shadow-result-filter" onchange="renderITFShadowPicks()"><option value="settled">Settled picks</option><option value="open">Open picks</option><option value="all">All picks</option></select></label>
              <label>Strategy <select id="itf-shadow-strategy-filter" onchange="renderITFShadowPicks()"><option value="all">All strategies</option><option value="all_underdogs">Every underdog</option><option value="mid_price">Mid-price dogs</option><option value="longshots">Longshots</option><option value="strengthening">Strengthening dogs</option></select></label>
              <span id="itf-shadow-pick-count" class="muted"></span>
            </div>
            <div class="table-scroll xwide" style="max-height:460px;"><table><thead><tr><th>Entered (Central)</th><th>Strategy</th><th>Match / Pick</th><th>Phase</th><th>Contract / Side</th><th class="num">Entry / Contracts</th><th class="num">Risked incl. fees</th><th class="num">Net profit if win</th><th class="num">Settlement payout</th><th class="num">Result / Net profit</th></tr></thead><tbody id="itf-shadow-picks"></tbody></table></div>
          </details>
          <details style="margin-top:8px;"><summary>Pregame/live and singles/doubles breakdown</summary><div id="itf-shadow-segments" class="notice"></div></details>
          <div class="notice">Four separate hypothetical portfolios, 1U per entry, held to Kalshi settlement. Whole contracts, displayed depth, and estimated taker fees. Overlapping picks are correlated. No real orders or automatic promotion; open risk is excluded from settled ROI.</div>
          <h4>New hypothesis tests · SHADOW ONLY</h4>
          <div id="itf-followups-window" class="notice"></div>
          <div class="table-scroll"><table>
            <thead><tr><th>Fixed test rule</th><th class="num">Historical discovery</th><th class="num">New settled / Open</th><th class="num">New net P&amp;L / ROI</th><th class="num">Open risk / Drawdown</th><th>Evidence</th></tr></thead>
            <tbody id="itf-followups-strategies"></tbody>
          </table></div>
          <div class="notice">Historical results selected these hypotheses and do not count as new validation. Each test follows new matching entries from its original strategy, with the same 1U simulated fill and fees. Previously observed matches are excluded. A positive sample does not establish future profitability.</div>
          <h4>Price-quality comparison · SHADOW ONLY</h4>
          <div id="itf-price-quality-window" class="notice"></div>
          <div id="itf-price-quality-comparison" class="notice"></div>
          <div class="table-scroll"><table>
            <thead><tr><th>Registered test</th><th class="num">Settled / Open</th><th class="num">Net P&amp;L / ROI</th><th class="num">Open risk / Drawdown</th><th class="num">Available cash</th><th>Evidence</th></tr></thead>
            <tbody id="itf-price-quality-strategies"></tbody>
          </table></div>
          <details><summary>New comparison picks</summary><div class="table-scroll" style="max-height:350px;"><table><thead><tr><th>Entered (Central)</th><th>Test / Pick</th><th>Phase</th><th class="num">Price / Contracts</th><th class="num">Risk incl. fees</th><th class="num">Result / Net profit</th></tr></thead><tbody id="itf-price-quality-picks"></tbody></table></div></details>
          <div class="notice">Three separate $5,000 virtual portfolios; fixed $5 stakes including fees. Maximum 4c spread, $100 open risk and $25 per tournament per portfolio; entry stops after $250 drawdown. Pregame underdogs and favorites enter the same matches as alternative simulations. Two confirmed books and official settlement required. Review starts at 200 new settlements across five entry days; no automatic live promotion.</div>
        </details>
    </section>

    <div class="grid two view logs-page" data-view="logs">
      <section style="grid-column:1/-1">
        <h2>Recent Errors &amp; Warnings</h2>
        <div id="logs-notices" class="logs" role="status">Waiting for recent activity.</div>
      </section>
      <nav class="subtabs log-tabs" aria-label="Log sections">
        <button type="button" data-log-tab="sports" onclick="showLogPanel('sports')">Sports decisions</button>
        <button type="button" data-log-tab="crypto" onclick="showLogPanel('crypto')">Crypto decisions</button>
        <button type="button" data-log-tab="diagnostics" onclick="showLogPanel('diagnostics')">Raw logs &amp; API usage</button>
      </nav>
      <section class="log-panel" data-log-panel="crypto" style="grid-column:1/-1">
        <h2>Recent Crypto Candidates and Decisions</h2>
        <div class="notice">Newest scans first. These rows show the bot's selected side and the exact reason it was placed or skipped.</div>
        <div class="log-filter-bar">
          <label class="log-filter-field">Time range
            <select id="crypto-log-time-range">
              <option value="15">Last 15 minutes</option>
              <option value="30">Last 30 minutes</option>
              <option value="60" selected>Last hour</option>
              <option value="360">Last 6 hours</option>
              <option value="1440">Last 24 hours</option>
              <option value="0">All available</option>
            </select>
          </label>
          <label class="log-filter-field">Minimum price
            <input id="crypto-log-min-price" type="number" min="0" max="100" step="1" value="0" inputmode="numeric">
          </label>
          <label class="log-filter-field">Maximum price
            <input id="crypto-log-max-price" type="number" min="0" max="100" step="1" value="100" inputmode="numeric">
          </label>
          <label class="log-filter-field">Minimum edge %
            <input id="crypto-log-min-edge" type="number" step="0.1" value="-100">
          </label>
          <label class="log-filter-field">Maximum edge %
            <input id="crypto-log-max-edge" type="number" step="0.1" value="100">
          </label>
          <label class="log-filter-field">Minimum confidence
            <input id="crypto-log-min-confidence" type="number" min="0" max="100" step="0.1" value="0">
          </label>
          <label class="log-filter-field">Maximum confidence
            <input id="crypto-log-max-confidence" type="number" min="0" max="100" step="0.1" value="100">
          </label>
          <label class="log-filter-field">Asset
            <select id="crypto-log-asset"><option value="">All assets</option><option>BTC</option><option>ETH</option><option>SOL</option><option>XRP</option><option>DOGE</option><option>GOLD</option><option>SILVER</option><option>WTI</option></select>
          </label>
          <label class="log-filter-field">Side
            <select id="crypto-log-side"><option value="">Both sides</option><option value="yes">YES</option><option value="no">NO</option></select>
          </label>
          <label class="log-filter-field">Decision
            <select id="crypto-log-decision"><option value="">All decisions</option><option value="placed">Placed</option><option value="eligible">Eligible</option><option value="skipped">Skipped</option></select>
          </label>
          <label class="log-filter-field">Reason / ticker
            <input id="crypto-log-search" type="search" placeholder="quality, price, BTC..." style="width:170px">
          </label>
          <div id="crypto-log-filter-count" class="log-filter-count">Showing all candidate prices</div>
        </div>
        <div style="overflow:auto;max-height:560px">
          <table class="compact-table">
            <thead><tr><th>Scan</th><th>Asset</th><th>Side</th><th>Market</th><th class="num">Price</th><th class="num">p(YES)</th><th class="num">90% p interval</th><th class="num">Expected edge</th><th class="num">Edge low</th><th class="num">Pr(edge &gt; 0)</th><th class="num">Data quality</th><th>Decision</th><th>Skip Reasons</th></tr></thead>
            <tbody id="logs-crypto-candidates"></tbody>
          </table>
        </div>
      </section>
      <section class="log-panel" data-log-panel="sports" style="grid-column:1/-1">
        <h2>Sports Candidates and Bets</h2>
        <div class="notice">Newest first. Filter candidate snapshots and placed orders by time, Kalshi entry price, net edge, sport, market, or outcome. Books are shown as fresh independent families / raw exact-line books / stale books.</div>
        <div class="log-filter-bar">
          <label class="log-filter-field">Time range
            <select id="sports-log-time-range">
              <option value="15">Last 15 minutes</option>
              <option value="60">Last hour</option>
              <option value="360" selected>Last 6 hours</option>
              <option value="1440">Last 24 hours</option>
              <option value="4320">Last 3 days</option>
              <option value="10080">Last 7 days</option>
              <option value="0">All cached</option>
            </select>
          </label>
          <label class="log-filter-field">Min cents
            <input id="sports-log-min-price" type="number" min="0" max="100" step="1" value="0">
          </label>
          <label class="log-filter-field">Max cents
            <input id="sports-log-max-price" type="number" min="0" max="100" step="1" value="100">
          </label>
          <label class="log-filter-field">Min edge %
            <input id="sports-log-min-edge" type="number" step="0.1" value="-100">
          </label>
          <label class="log-filter-field">Max edge %
            <input id="sports-log-max-edge" type="number" step="0.1" value="100">
          </label>
          <label class="log-filter-field">Decision
            <select id="sports-log-decision">
              <option value="">All</option><option value="placed">Placed bets</option><option value="eligible">Eligible</option>
              <option value="skipped">Skipped</option><option value="not_filled">Not filled</option>
            </select>
          </label>
          <label class="log-filter-field">Sport
            <select id="sports-log-sport"><option value="">All sports</option></select>
          </label>
          <label class="log-filter-field">Market
            <select id="sports-log-market"><option value="">All markets</option><option value="moneyline">Moneyline</option><option value="spread">Spread</option><option value="total">Total</option></select>
          </label>
          <label class="log-filter-field">Search
            <input id="sports-log-search" type="search" placeholder="team or ticker" style="width:150px">
          </label>
          <div id="sports-log-filter-count" class="log-filter-count">Loading sports history...</div>
        </div>
        <div style="overflow:auto;max-height:640px">
          <table class="compact-table">
            <thead><tr><th>Time</th><th>Sport</th><th>Status</th><th>Market</th><th>Selection</th><th>Ticker</th><th class="num">Cents</th><th class="num">Edge</th><th class="num">Conf</th><th class="num">Pro</th><th class="num">Final</th><th class="num">Books F/R/S</th><th class="num">Units</th><th class="num">Stake</th><th>Reason</th></tr></thead>
            <tbody id="logs-sports-candidates"></tbody>
          </table>
        </div>
      </section>
      <section class="log-panel" data-log-panel="diagnostics">
        <h2>Odds API Usage</h2>
        <div class="notice" id="odds-api-usage-status">Loading authoritative provider usage...</div>
        <div class="cards" style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:10px">
          <div class="card"><div class="label">Credits Used</div><div class="value" id="odds-api-used">--</div></div>
          <div class="card"><div class="label">Provider Remaining</div><div class="value" id="odds-api-remaining">--</div></div>
          <div class="card"><div class="label">Projected This Cycle</div><div class="value" id="odds-api-projected">--</div></div>
          <div class="card"><div class="label">Current Daily Pace</div><div class="value" id="odds-api-daily-rate">--</div></div>
        </div>
        <div style="height:10px;background:rgba(255,255,255,.08);border-radius:999px;overflow:hidden;margin-top:12px">
          <div id="odds-api-usage-bar" style="height:100%;width:0;background:var(--good);transition:width .25s ease"></div>
        </div>
        <div class="sub" id="odds-api-usage-detail" style="margin-top:8px">Waiting for usage data.</div>
        <div class="sub" id="odds-api-key-detail" style="margin-top:6px"></div>
      </section>
      <section class="log-panel" data-log-panel="diagnostics">
        <h2>Crypto Candidate Skip Reasons</h2>
        <div style="overflow:auto"><table class="compact-table"><thead><tr><th>Reason</th><th class="num">Count</th></tr></thead><tbody id="logs-crypto-skips"></tbody></table></div>
      </section>
      <details class="diagnostic-panel log-panel" data-log-panel="diagnostics"><summary>Raw Sports Log</summary>
        <div id="sports-logs" class="logs"></div>
      </details>
      <details class="diagnostic-panel log-panel" data-log-panel="diagnostics"><summary>Crypto Live Log</summary>
        <div id="logs-crypto-text" class="logs"></div>
      </details>
    </div>
  </main>
  <script>
    const money = n => `$${Number(n || 0).toFixed(2)}`;
    const fileSize = n => {
      let value = Number(n || 0);
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let unit = 0;
      while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
      return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
    };

    async function runStorageCleanup() {
      setText('storage-cleanup-status', 'Cleanup running; analytics will be compressed, not deleted.', 'notice');
      try {
        const response = await fetch('/api/storage/cleanup', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || result.skipped || 'cleanup failed');
        setText('storage-cleanup-status', `Cleanup complete · ${result.rotated_count || 0} rotated · ${result.compressed_count || 0} compressed · ${result.deleted_count || 0} expired files removed.`, 'notice positive');
        await refresh();
      } catch (error) {
        setText('storage-cleanup-status', `Cleanup failed · ${error.message || error}`, 'notice negative');
      }
    }
    const CENTRAL_TIME_ZONE = 'America/Chicago';
    const centralTimeFormatter = new Intl.DateTimeFormat('en-US', {
      timeZone: CENTRAL_TIME_ZONE,
      year: '2-digit', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: true, timeZoneName: 'short'
    });
    const centralOffsetFormatter = new Intl.DateTimeFormat('en-US', {
      timeZone: CENTRAL_TIME_ZONE,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hourCycle: 'h23'
    });
    const isoTimestampPattern = /\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?/g;

    function centralOffsetAt(epoch) {
      const parts = Object.fromEntries(
        centralOffsetFormatter.formatToParts(new Date(epoch))
          .filter(part => part.type !== 'literal')
          .map(part => [part.type, part.value])
      );
      const representedAsUtc = Date.UTC(
        Number(parts.year), Number(parts.month) - 1, Number(parts.day),
        Number(parts.hour), Number(parts.minute), Number(parts.second)
      );
      return representedAsUtc - epoch;
    }

    function parseCentralTime(value) {
      if (!value) return null;
      const text = String(value).trim();
      const naive = text.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$/);
      if (naive) {
        const guess = Date.UTC(
          Number(naive[1]), Number(naive[2]) - 1, Number(naive[3]),
          Number(naive[4]), Number(naive[5]), Number(naive[6] || 0)
        );
        let epoch = guess - centralOffsetAt(guess);
        epoch = guess - centralOffsetAt(epoch);
        return new Date(epoch);
      }
      const epoch = Date.parse(text);
      return Number.isFinite(epoch) ? new Date(epoch) : null;
    }

    function shortTime(value) {
      if (!value) return '';
      const text = String(value).trim();
      if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
        const [year, month, day] = text.split('-').map(Number);
        return new Intl.DateTimeFormat('en-US', {
          timeZone: CENTRAL_TIME_ZONE, year: 'numeric', month: 'short', day: 'numeric'
        }).format(new Date(Date.UTC(year, month - 1, day, 18)));
      }
      const parsed = parseCentralTime(text);
      return parsed ? centralTimeFormatter.format(parsed) : text;
    }

    function centralizeTextTimes(value) {
      return String(value ?? '').replace(isoTimestampPattern, match => shortTime(match));
    }

    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    let latestCryptoCandidateRows = [];
    let cryptoCandidateHistoryLoaded = false;
    let latestSportsCandidateRows = [];
    let sportsCandidateHistoryLoaded = false;
    let latestSportsMatchedRows = 0;
    let sportsFilterTimer = null;

    function cryptoCandidateTimeLabel() {
      const select = document.getElementById('crypto-log-time-range');
      return select?.selectedOptions?.[0]?.textContent || 'selected period';
    }

    async function loadCryptoCandidateHistory() {
      const select = document.getElementById('crypto-log-time-range');
      const minutes = Number(select?.value ?? 60);
      setText('crypto-log-filter-count', `Loading ${cryptoCandidateTimeLabel().toLowerCase()}...`);
      try {
        const response = await fetch(`/api/crypto/candidate-log?minutes=${encodeURIComponent(minutes)}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        cryptoCandidateHistoryLoaded = true;
        renderCryptoCandidateLog(payload.rows || []);
      } catch (error) {
        setText('crypto-log-filter-count', `Candidate history unavailable: ${error.message || error}`);
      }
    }

    function cryptoCandidatePriceBounds() {
      const minInput = document.getElementById('crypto-log-min-price');
      const maxInput = document.getElementById('crypto-log-max-price');
      let minimum = Number(minInput?.value);
      let maximum = Number(maxInput?.value);
      if (!Number.isFinite(minimum)) minimum = 0;
      if (!Number.isFinite(maximum)) maximum = 100;
      minimum = Math.max(0, Math.min(100, minimum));
      maximum = Math.max(0, Math.min(100, maximum));
      return minimum <= maximum
        ? {minimum, maximum}
        : {minimum: maximum, maximum: minimum};
    }

    function cryptoLogNumber(id, fallback) {
      const value = Number(document.getElementById(id)?.value);
      return Number.isFinite(value) ? value : fallback;
    }

    function cryptoFailedCheckText(check) {
      const metric = String(check?.metric || 'requirement').replaceAll('_', ' ');
      const actual = check?.actual;
      if (check?.minimum !== undefined && check?.maximum !== undefined) {
        return `${metric} ${actual} (needs ${check.minimum}-${check.maximum})`;
      }
      if (check?.minimum !== undefined) return `${metric} ${actual} (needs ${check.minimum}+)`;
      if (check?.maximum !== undefined) return `${metric} ${actual} (max ${check.maximum})`;
      if (check?.required !== undefined) return `${metric} ${actual} (needs ${check.required})`;
      return `${metric} ${actual ?? 'failed'}`;
    }

    function cryptoUnitMetrics(row) {
      const campaign = row?.crypto_live_campaign || row?.campaign_review || {};
      const units = row?.crypto_units || campaign.crypto_units || {};
      return units.metrics || {};
    }

    function cryptoWinProbability(row) {
      const metrics = cryptoUnitMetrics(row);
      return Number(
        metrics.calibrated_win_probability
        ?? metrics.win_probability
        ?? row?.selected_side_probability
        ?? row?.model_selected_probability
        ?? 0
      );
    }

    function cryptoWinProbabilityInterval(row) {
      const metrics = cryptoUnitMetrics(row);
      const interval = metrics.win_probability_interval || {};
      const low = Number(
        interval.low
        ?? metrics.calibrated_win_probability_low
        ?? metrics.win_probability_low
      );
      const high = Number(
        interval.high
        ?? metrics.calibrated_win_probability_high
        ?? metrics.win_probability_high
      );
      if (!Number.isFinite(low) || !Number.isFinite(high)) return '';
      return `${low.toFixed(1)}–${high.toFixed(1)}%`;
    }

    function cryptoEdgeCertainty(row) {
      const metrics = cryptoUnitMetrics(row);
      return Number(
        metrics.edge_certainty
        ?? row?.probability_net_edge_positive
        ?? row?.confidence
        ?? 0
      );
    }

    function cryptoSkipReasonText(row) {
      const reasons = row.skip_reasons || [];
      if (!reasons.length) return row.decision === 'placed_live' || row.decision === 'placed' ? 'placed' : 'eligible';
      const campaign = row.campaign_review || {};
      const failed = campaign.failed_checks || [];
      const disagreement = row.disagreement_guard || {};
      const directional = row.directional_confirmation || {};
      const units = row.crypto_units || campaign.crypto_units || {};
      return reasons.map(reason => {
        if ([
          'crypto_unit_quality_filter',
          'crypto_unit_data_quality',
          'crypto_unit_metrics_unavailable',
          'crypto_unit_win_probability_filter',
          'crypto_unit_edge_filter',
          'crypto_unit_kelly_filter',
          'crypto_unit_portfolio_risk_cap',
          'crypto_unit_model_maturity_cap',
          'crypto_dynamic_qualification_filter'
        ].includes(reason)) {
          const metrics = units.metrics || {};
          return `${reason}: tier ${Number(units.target_units || 0)}u; calibrated win probability ${cryptoWinProbability(row).toFixed(1)}% [${cryptoWinProbabilityInterval(row) || 'interval unavailable'}], conservative sizing low ${Number(metrics.conservative_sizing_probability_low || 0).toFixed(1)}%, expected edge ${Number(metrics.expected_edge || 0).toFixed(2)}c, edge low ${Number(metrics.edge_low || 0).toFixed(2)}c, edge certainty ${Number(metrics.edge_certainty || metrics.confidence || 0).toFixed(1)}%, maturity cap ${Number(units.model_maturity_unit_cap || 0)}u, Kelly cap ${Number(units.kelly_unit_cap || 0)}u, portfolio cap ${Number(units.portfolio_unit_cap || 0)}u, data quality ${Number(metrics.data_quality || 0).toFixed(3)}`;
        }
        if (reason === campaign.reason && failed.length) {
          return `${reason}: ${failed.map(cryptoFailedCheckText).join('; ')}`;
        }
        if (reason === 'model_market_disagreement_unconfirmed') {
          return `${reason}: model/market gap ${Number(disagreement.raw_market_gap || 0).toFixed(2)}pp (max ${Number(disagreement.maximum_unconfirmed_gap || 0).toFixed(2)}); flow ${String(disagreement.flow_direction || 'neutral').toUpperCase()} vs ${String(disagreement.selected_side || row.side || '').toUpperCase()}, strength ${Number(disagreement.flow_strength || 0).toFixed(3)}/${Number(disagreement.minimum_flow_strength || 0).toFixed(3)}, sources ${Number(disagreement.confirming_source_count || 0)}/${Number(disagreement.minimum_sources || 0)}, dispersion ${Number(disagreement.cross_exchange_dispersion_bps || 0).toFixed(2)}/${Number(disagreement.maximum_dispersion_bps || 0).toFixed(2)}bps`;
        }
        if ([
          'campaign_directional_confirmation',
          'directional_opposition',
          'directional_flow_too_weak',
          'directional_insufficient_consensus',
          'directional_data_unavailable'
        ].includes(reason) && directional.enabled) {
          return `${reason}: flow ${String(directional.flow_direction || 'neutral').toUpperCase()} vs ${String(directional.selected_side || row.side || '').toUpperCase()}, strength ${Number(directional.flow_strength || 0).toFixed(3)}/${Number(directional.minimum_flow_strength || 0).toFixed(3)}, sources ${Number(directional.selected_side_source_count || 0)}/${Number(directional.minimum_sources || 0)}`;
        }
        if (reason === 'campaign_price_range') return `${reason}: price ${row.entry_price}c (current gate 35-70c)`;
        if (reason === 'campaign_quality_filter') return `${reason}: expected edge ${row.edge}c / Pr(net edge > 0) ${row.confidence}% (minimum 1u tier is 2c / 75%)`;
        if (reason === 'campaign_entry_window') return `${reason}: outside current 2-12 minute window`;
        if (reason === 'live_quote_slippage_limit') {
          const refresh = row.live_quote_refresh || {};
          return `${reason}: fresh full-size quote ${refresh.refreshed_price_cents ?? row.entry_price ?? '—'}c moved ${refresh.adverse_move_cents ?? '—'}c (immediate max ${refresh.max_adverse_move_cents ?? 6}c); waiting for a stable second quote`;
        }
        if (reason === 'live_quote_slippage_reconfirm_wait') {
          const reconfirm = row.live_slippage_reconfirmation || row.live_quote_refresh?.reconfirmation || {};
          return `${reason}: second quote available in ${Number(reconfirm.seconds_remaining || 0).toFixed(1)}s; must remain within ${Number(reconfirm.maximum_confirmation_move_cents ?? 2).toFixed(1)}c`;
        }
        if (reason === 'live_quote_slippage_reconfirm_unstable') return `${reason}: second quote moved more than 2c; 15-second stability timer restarted`;
        if (reason === 'commodity_live_shadow') return `${reason}: GOLD, SILVER, and WTI are tracked for counterfactual results but cannot place live orders`;
        return reason;
      }).join(' | ');
    }

    function renderCryptoCandidateLog(rows = latestCryptoCandidateRows) {
      latestCryptoCandidateRows = Array.isArray(rows) ? rows : [];
      const {minimum, maximum} = cryptoCandidatePriceBounds();
      const minEdge = cryptoLogNumber('crypto-log-min-edge', -100);
      const maxEdge = cryptoLogNumber('crypto-log-max-edge', 100);
      const minConfidence = cryptoLogNumber('crypto-log-min-confidence', 0);
      const maxConfidence = cryptoLogNumber('crypto-log-max-confidence', 100);
      const asset = String(document.getElementById('crypto-log-asset')?.value || '').toUpperCase();
      const side = String(document.getElementById('crypto-log-side')?.value || '').toLowerCase();
      const decision = String(document.getElementById('crypto-log-decision')?.value || '').toLowerCase();
      const search = String(document.getElementById('crypto-log-search')?.value || '').trim().toLowerCase();
      const filtered = latestCryptoCandidateRows.filter(row => {
        const price = Number(row.entry_price);
        const edge = Number(row.edge);
        const confidence = Number(row.confidence);
        const rowDecision = String(row.decision || '').toLowerCase();
        const searchable = `${row.asset || ''} ${row.ticker || ''} ${(row.skip_reasons || []).join(' ')} ${cryptoSkipReasonText(row)}`.toLowerCase();
        return Number.isFinite(price) && price >= minimum && price <= maximum
          && Number.isFinite(edge) && edge >= Math.min(minEdge, maxEdge) && edge <= Math.max(minEdge, maxEdge)
          && Number.isFinite(confidence) && confidence >= Math.min(minConfidence, maxConfidence) && confidence <= Math.max(minConfidence, maxConfidence)
          && (!asset || String(row.asset || '').toUpperCase() === asset)
          && (!side || String(row.side || '').toLowerCase() === side)
          && (!decision || rowDecision === decision || (decision === 'placed' && rowDecision.startsWith('placed')))
          && (!search || searchable.includes(search));
      });
      const reasonCounts = new Map();
      filtered.forEach(row => (row.skip_reasons || []).forEach(reason => {
        reasonCounts.set(reason, (reasonCounts.get(reason) || 0) + 1);
      }));
      const reasons = [...reasonCounts.entries()]
        .map(([label, count]) => ({label, count}))
        .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
        .slice(0, 20);
      renderCountTable('logs-crypto-skips', reasons, 'No skip reasons in this price range.');
      renderRows('logs-crypto-candidates', filtered.slice(0, 200), [
        {render: c => shortTime(c.scanned_at)},
        {render: c => c.asset || ''},
        {render: c => String(c.side || '').toUpperCase()},
        {render: c => c.ticker || ''},
        {num: true, render: c => c.entry_price === undefined || c.entry_price === null ? '' : `${c.entry_price}c`},
        {num: true, render: c => c.probability?.p_yes == null ? (c.model_prob_yes == null ? '' : `${c.model_prob_yes}%`) : `${Number(c.probability.p_yes).toFixed(1)}%`},
        {num: true, render: c => c.probability?.p_low == null || c.probability?.p_high == null ? '' : `${Number(c.probability.p_low).toFixed(1)}–${Number(c.probability.p_high).toFixed(1)}%`},
        {num: true, render: c => c.expected_edge === undefined ? `${c.edge ?? ''}c` : `${Number(c.expected_edge).toFixed(2)}c`},
        {num: true, render: c => c.edge_low === undefined ? '' : `${Number(c.edge_low).toFixed(2)}c`},
        {num: true, render: c => `${Number(c.probability_net_edge_positive ?? c.confidence ?? 0).toFixed(1)}%`},
        {num: true, render: c => c.data_quality?.score === undefined ? '' : `${(Number(c.data_quality.score) * 100).toFixed(0)}%`},
        {render: c => c.decision || ''},
        {render: c => cryptoSkipReasonText(c)}
      ], 'No recent crypto candidates in this price range.');
      setText(
        'crypto-log-filter-count',
        `Showing ${filtered.length} of ${latestCryptoCandidateRows.length} candidates, ${minimum}-${maximum}c, expected edge ${Math.min(minEdge, maxEdge)} to ${Math.max(minEdge, maxEdge)}c, Pr(edge > 0) ${Math.min(minConfidence, maxConfidence)}-${Math.max(minConfidence, maxConfidence)}%, ${cryptoCandidateTimeLabel().toLowerCase()}`
      );
    }

    function sportsCandidateTimeLabel() {
      const select = document.getElementById('sports-log-time-range');
      return select?.selectedOptions?.[0]?.textContent || 'selected period';
    }

    async function loadSportsCandidateHistory() {
      const minutes = Number(document.getElementById('sports-log-time-range')?.value ?? 360);
      setText('sports-log-filter-count', `Loading ${sportsCandidateTimeLabel().toLowerCase()}...`);
      try {
        const params = new URLSearchParams({minutes: String(minutes)});
        const minPrice = sportsFilterNumber('sports-log-min-price', 0);
        const maxPrice = sportsFilterNumber('sports-log-max-price', 100);
        if (minPrice > 0) params.set('min_price', String(minPrice));
        if (maxPrice < 100) params.set('max_price', String(maxPrice));
        params.set('min_edge', String(sportsFilterNumber('sports-log-min-edge', -100)));
        params.set('max_edge', String(sportsFilterNumber('sports-log-max-edge', 100)));
        for (const [key, id] of [['decision', 'sports-log-decision'], ['sport', 'sports-log-sport'], ['market', 'sports-log-market'], ['search', 'sports-log-search']]) {
          const value = document.getElementById(id)?.value || '';
          if (value) params.set(key, value);
        }
        const response = await fetch(`/api/sports/candidate-log?${params.toString()}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        sportsCandidateHistoryLoaded = true;
        updateSportsFilterSports(payload.sports || []);
        latestSportsMatchedRows = Number(payload.matched_rows || 0);
        renderSportsCandidateLog(payload.rows || []);
      } catch (error) {
        setText('sports-log-filter-count', `Sports history unavailable: ${error.message || error}`);
      }
    }

    function sportsFilterNumber(id, fallback) {
      const value = Number(document.getElementById(id)?.value);
      return Number.isFinite(value) ? value : fallback;
    }

    function updateSportsFilterSports(sports) {
      const select = document.getElementById('sports-log-sport');
      if (!select) return;
      const selected = select.value;
      sports = [...new Set((sports || []).filter(Boolean))].sort();
      select.innerHTML = '<option value="">All sports</option>' + sports.map(sport => `<option value="${esc(sport)}">${esc(sport)}</option>`).join('');
      if (sports.includes(selected)) select.value = selected;
    }

    function renderSportsCandidateLog(rows = latestSportsCandidateRows) {
      if (rows !== latestSportsCandidateRows) {
        latestSportsCandidateRows = Array.isArray(rows) ? rows : [];
      }
      const minPrice = Math.max(0, Math.min(100, sportsFilterNumber('sports-log-min-price', 0)));
      const maxPrice = Math.max(0, Math.min(100, sportsFilterNumber('sports-log-max-price', 100)));
      const priceLow = Math.min(minPrice, maxPrice);
      const priceHigh = Math.max(minPrice, maxPrice);
      const minEdge = sportsFilterNumber('sports-log-min-edge', -100);
      const maxEdge = sportsFilterNumber('sports-log-max-edge', 100);
      const edgeLow = Math.min(minEdge, maxEdge);
      const edgeHigh = Math.max(minEdge, maxEdge);
      const decision = document.getElementById('sports-log-decision')?.value || '';
      const sport = document.getElementById('sports-log-sport')?.value || '';
      const market = document.getElementById('sports-log-market')?.value || '';
      const search = String(document.getElementById('sports-log-search')?.value || '').trim().toLowerCase();
      const fullPriceRange = priceLow === 0 && priceHigh === 100;
      const filtered = latestSportsCandidateRows.filter(row => {
        const price = row.entry_price === null || row.entry_price === undefined ? null : Number(row.entry_price);
        const edge = Number(row.edge);
        if (price === null ? !fullPriceRange : (!Number.isFinite(price) || price < priceLow || price > priceHigh)) return false;
        if (!Number.isFinite(edge) || edge < edgeLow || edge > edgeHigh) return false;
        if (decision && row.decision !== decision) return false;
        if (sport && row.sport !== sport) return false;
        if (market && row.market_type !== market) return false;
        if (search && !`${row.selection || ''} ${row.ticker || ''} ${(row.skip_reasons || []).join(' ')}`.toLowerCase().includes(search)) return false;
        return true;
      });
      renderRows('logs-sports-candidates', filtered.slice(0, 1000), [
        {render: row => shortTime(row.scanned_at)},
        {render: row => row.sport || ''},
        {render: row => String(row.decision || '').replaceAll('_', ' ')},
        {render: row => row.market_type || ''},
        {render: row => row.selection || ''},
        {render: row => row.ticker || ''},
        {num: true, render: row => row.entry_price === null || row.entry_price === undefined ? '' : `${Number(row.entry_price).toFixed(1)}c`},
        {num: true, render: row => row.edge === null || row.edge === undefined ? '' : `${Number(row.edge).toFixed(3)}%`},
        {num: true, render: row => row.confidence ?? ''},
        {num: true, render: row => row.pro_score ?? ''},
        {num: true, render: row => row.final_score ?? ''},
        {num: true, render: row => {
          if (row.families == null && row.raw_books == null && row.stale_books == null) return '';
          return `${row.families ?? 0}/${row.raw_books ?? '?'}/${row.stale_books ?? 0}`;
        }},
        {num: true, render: row => row.units === null || row.units === undefined ? '' : `${Number(Number(row.units).toFixed(3))}u${row.decision === 'placed' && row.requested_units != null ? ` / ${Number(row.requested_units)}u requested` : ''}`},
        {num: true, render: row => row.stake === null || row.stake === undefined ? '' : money(row.stake)},
        {render: row => (row.skip_reasons || []).join(', ') || (row.decision === 'placed' ? 'order filled' : 'passed logged filters')}
      ], 'No sports rows match these filters.');
      const placed = filtered.filter(row => row.decision === 'placed').length;
      setText('sports-log-filter-count', `Showing ${Math.min(filtered.length, 1000)} of ${latestSportsMatchedRows || filtered.length} matches (${placed} placed in loaded rows) · ${priceLow}-${priceHigh}c · edge ${edgeLow}% to ${edgeHigh}% · ${sportsCandidateTimeLabel().toLowerCase()}`);
    }

    function scheduleSportsCandidateHistory() {
      clearTimeout(sportsFilterTimer);
      sportsFilterTimer = setTimeout(loadSportsCandidateHistory, 250);
    }

    const workspacePages = {
      overview: ['Overview', 'ACCOUNT & PERFORMANCE', 'Your bankroll, exposure and automated results in one place.'],
      sports: ['Sports', 'LIVE OPERATIONS', 'Active bets, bot capacity and current picks.'],
      crypto: ['Crypto', 'LIVE OPERATIONS', 'Current signals, open positions and execution status.'],
      analytics: ['Sports analytics', 'PERFORMANCE & RESEARCH', 'Results, source reconciliation and strategy evidence.'],
      'crypto-analytics': ['Crypto analytics', 'PERFORMANCE & RESEARCH', 'Strategy performance, forward studies and research.'],
      'perps-shadow': ['Perpetuals paper lab', 'PERFORMANCE & RESEARCH', 'Isolated simulations and validation results.'],
      control: ['Controls', 'SPORTS & SYSTEM', 'Everyday inputs first. Expand a group for detailed tuning.'],
      'crypto-control': ['Crypto controls', 'STRATEGY SETTINGS', 'Execution, sizing and market inputs for the Crypto service.'],
      logs: ['Logs', 'ACTIVITY & DIAGNOSTICS', 'Warnings, candidate decisions and service diagnostics.']
    };

    function showLogPanel(name) {
      const selected = ['sports', 'crypto', 'diagnostics'].includes(name) ? name : 'sports';
      document.querySelectorAll('[data-log-panel]').forEach(el => { el.hidden = el.dataset.logPanel !== selected; });
      document.querySelectorAll('[data-log-tab]').forEach(btn => {
        const active = btn.dataset.logTab === selected;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', String(active));
      });
      localStorage.setItem('logPanel', selected);
      refreshCandidateHistories();
      setTimeout(updateTableScrollHelpers, 30);
    }

    function showView(name) {
      const page = workspacePages[name] || workspacePages.overview;
      document.getElementById('page-title').textContent = page[0];
      document.getElementById('page-eyebrow').textContent = page[1];
      document.getElementById('page-description').textContent = page[2];
      document.querySelectorAll('[data-view]').forEach(el => el.classList.toggle('active', el.dataset.view === name));
      const tabName = name === 'analytics' || name === 'crypto-analytics' || name === 'perps-shadow'
        ? 'analytics'
        : (name === 'crypto-control' ? 'control' : name);
      document.querySelectorAll('[data-view-tab]').forEach(btn => {
        const active = btn.dataset.viewTab === tabName;
        btn.classList.toggle('active', active);
        if (active) btn.setAttribute('aria-current', 'page');
        else btn.removeAttribute('aria-current');
      });
      document.querySelectorAll('[data-control-view]').forEach(btn => {
        const active = btn.dataset.controlView === name;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', String(active));
      });
      localStorage.setItem('dashboardView', name);
      if (tabName === 'analytics') setTimeout(() => refresh(), 20);
      if (name === 'logs') refreshCandidateHistories();
      if (name === 'overview') setTimeout(() => renderPerformanceScope('overview'), 35);
      if (name === 'analytics') setTimeout(() => renderPerformanceScope('sports'), 35);
      if (name === 'crypto-analytics') setTimeout(() => renderPerformanceScope('crypto'), 35);
      window.scrollTo({top: 0, behavior: 'auto'});
      setTimeout(updateTableScrollHelpers, 30);
    }

    function createControlAccordion(title, description, options = {}) {
      const details = document.createElement('details');
      details.className = `control-accordion ${options.className || ''}`.trim();
      details.open = Boolean(options.open);
      if (options.key) {
        const stored = localStorage.getItem(`controlAccordion:${options.key}`);
        if (stored !== null) details.open = stored === 'open';
        details.addEventListener('toggle', () => {
          localStorage.setItem(`controlAccordion:${options.key}`, details.open ? 'open' : 'closed');
        });
      }

      const summary = document.createElement('summary');
      const copy = document.createElement('span');
      copy.className = 'control-summary-copy';
      const titleEl = document.createElement('span');
      titleEl.className = 'control-summary-title';
      titleEl.textContent = title;
      const descriptionEl = document.createElement('span');
      descriptionEl.className = 'control-summary-description';
      descriptionEl.textContent = description;
      const count = document.createElement('span');
      count.className = 'control-summary-count';
      count.textContent = '0 settings';
      copy.append(titleEl, descriptionEl);
      summary.append(copy, count);

      const body = document.createElement('div');
      body.className = 'control-accordion-body';
      details.append(summary, body);
      return {details, body, count};
    }

    function setControlAccordionCount(group, count) {
      group.count.textContent = `${count} setting${count === 1 ? '' : 's'}`;
    }

    function moveLabelsToControlGroup(labels, group) {
      const grid = document.createElement('div');
      grid.className = 'form-grid';
      labels.forEach(label => grid.append(label));
      group.body.append(grid);
      setControlAccordionCount(group, labels.length);
    }

    const mainSportsControls = new Set([
      'SPORTS_EXECUTION_MODE', 'ALLOW_LIVE_TRADING', 'SPORTS_LIVE_ORDER_ENABLED',
      'SPORTS_LIVE_CAMPAIGN_MAX_OPEN', 'SPORTS_LIVE_CAMPAIGN_BOT_COUNT',
      'SPORTS_UNIT_SIZE_PCT', 'SPORTS_UNIT_MAX_PER_MARKET', 'SPORTS_UNIT_INCREMENT',
      'SPORTS_EDGE_THRESHOLD', 'SPORTS_MIN_CONFIDENCE', 'SPORTS_SCAN_INTERVAL_MINUTES',
      'SPORTS_KEYS', 'SPORTS_AIBETPICKS_ENABLED', 'SPORTS_MODEST_RECOVERY_ENABLED'
    ]);
    const mainCryptoControls = new Set([
      'CRYPTO_EXECUTION_MODE', 'CRYPTO_LIVE_ORDER_ENABLED', 'CRYPTO_LIVE_DRY_RUN',
      'CRYPTO_15M_CAMPAIGN_BOT_COUNT', 'CRYPTO_15M_UNIT_SIZE_PCT',
      'CRYPTO_15M_UNIT_MAX_PER_MARKET', 'CRYPTO_15M_UNIT_RECOVERY_ENABLED'
    ]);

    function sportsControlGroup(id) {
      if (mainSportsControls.has(id)) return 'main';
      if (/^(SPORTS_AI_|SPORTS_LOCAL_AI_|SPORTS_OPENAI_|SPORTS_ENABLE_GROK|SPORTS_GROK_|BOT_PICKS_GROK_|ODDS_API_KEY$|XAI_|SPORTRADAR_|SPORTS_LIVE_DATA_|SPORTS_PUBLIC_STATE_|SPORTS_GAME_STATE_MODEL|SPORTS_DECISION_ANALYTICS|SPORTS_SCORES_)/.test(id)) return 'ai';
      if (/^(SPORTS_EXECUTION_MODE$|ALLOW_LIVE_TRADING$|LIVE_|KALSHI_|SPORTS_RECONCILE_|SPORTS_BET_MORE_|SPORTS_GAME_ODDS_|SPORTS_LIVE_(ORDER|EDGE_ORDER|FALLBACK|AUDIT|TIME_IN_FORCE|MAX_PRICE|ELITE_RETRY))/.test(id)) return 'live';
      if (/^SPORTS_(LOSS_STREAK_|TOTAL_LIFELINE_|RECOVERY_|DAILY_PROFIT_|PROFIT_LOCK_)/.test(id)) return 'recovery';
      if (/^SPORTS_(.*STAK|KELLY|SPREAD_|SELECTIVE_|SMALL_EDGE_|MAX_GAME_|MAX_SPORT_|HEDGE_ENABLED|TRACK_MISSED|MISSED_FILL|EARLY_RESULT_|MAX_PAPER_)/.test(id)) return 'staking';
      if (/^SPORTS_(PRICING_|MIN_ENTRY_|PRICE_|PLUS_PRICE_|FAVORITE_|HEAVY_FAVORITE_|LOW_PRICE_|MOONSHOT_|EDGE_THRESHOLD|PRO_|FINAL_SCORE_|MIN_CONFIDENCE$|EDGE_MARKET_|ALLOW_IN_GAME_|IN_GAME_|REQUIRE_LIVE_SCORE_|LIVE_GAME_|PROTECTED_|DIRECT_OPPOSITE_|TRUE_HEDGE_|KEYS$|ALL_ACTIVE_|DYNAMIC_KALSHI_|KALSHI_SERIES|KALSHI_DATE_)/.test(id)) return 'pricing';
      if (/^(SPORTS_|BOT_PICKS_)/.test(id)) return 'core';
      return 'advanced';
    }

    function organizeControlCenter() {
      const pages = document.querySelectorAll('section.view[data-view="control"]');
      const page = pages[0];
      if (!page || page.dataset.organized === 'true') return;
      page.dataset.organized = 'true';
      page.classList.add('control-center-page');

      const heading = page.querySelector(':scope > h2');
      if (heading) heading.textContent = 'Control Center';
      const cryptoCard = page.querySelector(':scope > .card');
      const legacyLayout = page.querySelector(':scope > .controls');
      const settingsColumn = legacyLayout?.children?.[0];
      const processColumn = legacyLayout?.children?.[1];
      const settingsGrid = settingsColumn?.querySelector(':scope > .form-grid');
      if (!cryptoCard || !legacyLayout || !settingsColumn || !settingsGrid) return;

      const statusBoard = document.createElement('div');
      statusBoard.className = 'control-status-board';
      const statusHead = document.createElement('div');
      statusHead.className = 'control-status-head';
      const statusTitle = document.createElement('h3');
      statusTitle.textContent = 'Services';
      const alwaysVisible = document.createElement('span');
      alwaysVisible.className = 'pill';
      alwaysVisible.textContent = 'Process controls';
      statusHead.append(statusTitle, alwaysVisible);
      statusBoard.append(statusHead);
      const processes = document.getElementById('processes');
      if (processes) statusBoard.append(processes);
      const processNotice = processColumn?.querySelector('.notice');
      if (processNotice) statusBoard.append(processNotice);
      heading?.insertAdjacentElement('afterend', statusBoard);

      const storageCard = document.createElement('div');
      storageCard.className = 'card storage-control-card';
      storageCard.innerHTML = `
        <div class="section-title">
          <div><h3>Storage & Retention</h3><div class="notice">Operational logs: 7 days · errors/audits: 14 days · analytics: compressed and retained.</div></div>
          <button type="button" onclick="runStorageCleanup()">Run cleanup now</button>
        </div>
        <div class="grid simple-stats">
          <div class="stat"><div class="label">Total Managed</div><div id="storage-total" class="value">--</div></div>
          <div class="stat"><div class="label">Active Files</div><div id="storage-active" class="value">--</div></div>
          <div class="stat"><div class="label">Analytics</div><div id="storage-analytics" class="value">--</div></div>
          <div class="stat"><div class="label">Archives</div><div id="storage-archives" class="value">--</div></div>
          <div class="stat"><div class="label">Temporary</div><div id="storage-temp" class="value">--</div></div>
          <div class="stat"><div class="label">Last Cleanup</div><div id="storage-last-cleanup" class="value" style="font-size:14px">Never</div></div>
        </div>
        <div id="storage-cleanup-status" class="notice">Daily cleanup runs at 3:15 AM Central and catches up after missed runs.</div>`;
      statusBoard.insertAdjacentElement('afterend', storageCard);

      const toolbar = document.createElement('div');
      toolbar.className = 'control-toolbar';
      const actionRow = settingsColumn.querySelector(':scope > .form-grid + div');
      if (actionRow) {
        actionRow.className = 'control-toolbar-actions';
        actionRow.removeAttribute('style');
        toolbar.append(actionRow);
      }
      const settingsStatus = document.getElementById('settings-status');
      if (settingsStatus) toolbar.append(settingsStatus);
      storageCard.insertAdjacentElement('afterend', toolbar);

      const stack = document.createElement('div');
      stack.className = 'control-accordion-stack';
      toolbar.insertAdjacentElement('afterend', stack);
      const groups = {
        main: createControlAccordion('Sports · Main Controls', 'Mode, active strategies, bot capacity, unit size, and entry thresholds.', {open:true, key:'sports-main'}),
        units: createControlAccordion('Sports · Unit Requirements', 'Edge, confidence, quality scores, and independent books for each unit tier.', {key:'sports-units'}),
        core: createControlAccordion('Sports Bot · Scanner & Campaign', 'Mode, bankroll, scan cadence, campaign bots, schedules, and general scanner behavior.', {key:'sports-core'}),
        crypto: createControlAccordion('Crypto Bot · Opportunity Controls', 'Quick bot-count control with access to all Crypto settings.', {className:'crypto-group', key:'crypto'}),
        pricing: createControlAccordion('Sports Bot · Pricing & Market Filters', 'Entry prices, edge and confidence gates, league coverage, favorites, and live-game qualification.', {key:'sports-pricing'}),
        staking: createControlAccordion('Sports Bot · Staking & Exposure', 'Base sizing, selective and small-edge stakes, spread rules, open exposure, and hedge controls.', {key:'sports-staking'}),
        recovery: createControlAccordion('Sports Bot · Recovery & Profit Protection', 'Loss recovery, lifelines, recovery baskets, daily profit targets, and profit locks.', {key:'sports-recovery'}),
        ai: createControlAccordion('Sports Bot · AI & Data Sources', 'Local AI, OpenAI, Grok, sportsbook odds, scores, streams, and premium live-data providers.', {key:'sports-ai'}),
        live: createControlAccordion('Sports Bot · Live Trading & Safety', 'Authenticated orders, hard loss limits, reconciliation, audits, and exchange credentials.', {className:'danger-group', key:'sports-live'}),
        advanced: createControlAccordion('Other & Advanced', 'Settings that do not belong to a bot-specific group.', {key:'advanced'})
      };
      ['main','units','crypto','live','core','pricing','staking','recovery','ai','advanced'].forEach(key => stack.append(groups[key].details));

      const labelsByGroup = {main:[], core:[], pricing:[], staking:[], recovery:[], ai:[], live:[], advanced:[]};
      [...settingsGrid.querySelectorAll(':scope > label')].forEach(label => {
        const field = label.querySelector('input[id], select[id]');
        labelsByGroup[sportsControlGroup(field?.id || '')].push(label);
      });
      Object.entries(labelsByGroup).forEach(([key, labels]) => moveLabelsToControlGroup(labels, groups[key]));
      const unitTable = document.getElementById('sports-unit-control-table');
      if (unitTable) {
        groups.units.body.append(unitTable);
        setControlAccordionCount(groups.units, unitTable.querySelectorAll('input').length);
      }
      if (!labelsByGroup.advanced.length) groups.advanced.details.remove();

      const cryptoHeading = cryptoCard.querySelector(':scope > h3');
      if (cryptoHeading) cryptoHeading.remove();
      const cryptoFields = cryptoCard.querySelectorAll('input[id], select[id]').length;
      while (cryptoCard.firstChild) groups.crypto.body.append(cryptoCard.firstChild);
      setControlAccordionCount(groups.crypto, cryptoFields);

      page.append(statusBoard);
      const maintenance = document.createElement('details');
      maintenance.className = 'diagnostic-panel maintenance-panel';
      const maintenanceTitle = document.createElement('summary');
      maintenanceTitle.textContent = 'Maintenance & storage';
      maintenance.append(maintenanceTitle, storageCard);
      const maintenanceActions = document.createElement('div');
      maintenanceActions.className = 'control-toolbar-actions maintenance-actions';
      toolbar.querySelectorAll('button').forEach(button => {
        if (/rerun|reset odds/i.test(button.textContent)) maintenanceActions.append(button);
      });
      maintenance.append(maintenanceActions);
      page.append(maintenance);
      cryptoCard.remove();
      legacyLayout.remove();
    }

    function cryptoControlGroup(id) {
      if (mainCryptoControls.has(id)) return 'main';
      if (/^CRYPTO_PERPS_/.test(id)) return 'perps';
      if (/^CRYPTO_(EXECUTION_MODE$|LIVE_|RECONCILE_)/.test(id)) return 'live';
      if (/^CRYPTO_(AI_|LOCAL_AI_|OPENAI_|GROK_|NEWS_|FREE_RSS_|CRYPTOPANIC_)/.test(id)) return 'ai';
      if (/^CRYPTO_(PROBABILITY_|MARKET_ANCHOR_|EXTREME_PROB_|PRICE_|FAVORITE_|HEAVY_FAVORITE_)/.test(id)) return 'pricing';
      return 'core';
    }

    function organizeCryptoControl() {
      const page = document.querySelector('section.view[data-view="crypto-control"]');
      if (!page || page.dataset.organized === 'true') return;
      page.dataset.organized = 'true';
      page.classList.add('control-center-page');
      const heading = page.querySelector(':scope > h2');
      if (heading) heading.textContent = 'Crypto Bot Controls';
      const legacyLayout = page.querySelector(':scope > .grid.two');
      const settingsColumn = legacyLayout?.children?.[0];
      const helpColumn = legacyLayout?.children?.[1];
      const settingsGrid = settingsColumn?.querySelector('.settings-grid');
      if (!legacyLayout || !settingsColumn || !settingsGrid) return;

      const toolbar = document.createElement('div');
      toolbar.className = 'control-toolbar';
      const actionRow = settingsColumn.querySelector('.settings-grid + div');
      if (actionRow) {
        actionRow.className = 'control-toolbar-actions';
        actionRow.removeAttribute('style');
        toolbar.append(actionRow);
      }
      const settingsStatus = document.getElementById('crypto-settings-status');
      if (settingsStatus) toolbar.append(settingsStatus);
      legacyLayout.insertAdjacentElement('beforebegin', toolbar);

      const stack = document.createElement('div');
      stack.className = 'control-accordion-stack';
      toolbar.insertAdjacentElement('afterend', stack);
      const groups = {
        main: createControlAccordion('Crypto · Main Controls', 'Mode, live switches, bot capacity, and unit sizing.', {className:'crypto-group', open:true, key:'crypto-main'}),
        core: createControlAccordion('Crypto Bot · Strategy Tuning', 'Execution behavior, continuous bot slots, unit sizing, and general strategy settings.', {className:'crypto-group', key:'crypto-core-full'}),
        pricing: createControlAccordion('Crypto Bot · Pricing & Market Filters', 'Probability ensemble, market anchoring, and favorite-price discipline.', {className:'crypto-group', key:'crypto-pricing'}),
        ai: createControlAccordion('Crypto Bot · AI, News & Data', 'Local AI, OpenAI, Grok, RSS, NewsAPI, and CryptoPanic integrations.', {className:'crypto-group', key:'crypto-ai'}),
        live: createControlAccordion('Crypto Bot · Live Trading & Safety', 'Live order switches, confirmation, shared-bankroll checks, and reconciliation.', {className:'crypto-group danger-group', key:'crypto-live'}),
        perps: createControlAccordion('Crypto Perpetuals · Paper Lab', 'Isolated shadow strategy, validation, probation, position limits, and research settings.', {className:'crypto-group', key:'crypto-perps'}),
        help: createControlAccordion('Providers & API Key Help', 'Source links and notes for optional Crypto data integrations.', {className:'crypto-group', key:'crypto-help'})
      };
      ['main','live','core','pricing','ai','perps','help'].forEach(key => stack.append(groups[key].details));
      const labelsByGroup = {main:[], core:[], pricing:[], ai:[], live:[], perps:[]};
      [...settingsGrid.querySelectorAll(':scope > label')].forEach(label => {
        const field = label.querySelector('input[id], select[id]');
        labelsByGroup[cryptoControlGroup(field?.id || '')].push(label);
      });
      Object.entries(labelsByGroup).forEach(([key, labels]) => moveLabelsToControlGroup(labels, groups[key]));
      if (helpColumn) {
        while (helpColumn.firstChild) groups.help.body.append(helpColumn.firstChild);
        setControlAccordionCount(groups.help, 0);
        groups.help.count.textContent = 'reference';
      }
      legacyLayout.remove();
    }

    function installControlSearch() {
      document.querySelectorAll('.control-center-page').forEach(page => {
        const bar = document.createElement('div');
        bar.className = 'control-search';
        bar.innerHTML = `<nav aria-label="Control sections"><button type="button" data-control-view="control" onclick="showView('control')">Sports & system</button> <button type="button" data-control-view="crypto-control" onclick="showView('crypto-control')">Crypto</button></nav>
          <label>Find a setting <input type="search" placeholder="Unit size, exposure, AI, or setting name"></label><span role="status"></span>`;
        page.querySelector('h2').insertAdjacentElement('afterend', bar);
        const input = bar.querySelector('input');
        const count = bar.querySelector('[role="status"]');
        const groups = [...page.querySelectorAll('.control-accordion')];
        const fieldSelector = 'label[data-setting], [data-control-block]';
        const labels = [...page.querySelectorAll(fieldSelector)];
        let priorOpen = null;
        input.addEventListener('input', () => {
          const query = input.value.trim().toLowerCase();
          if (query && priorOpen === null) priorOpen = groups.map(group => group.open);
          labels.forEach(label => {
            const ids = [...label.querySelectorAll('input[id], select[id]')].map(field => field.id).join(' ');
            label.hidden = !!query && !`${ids} ${label.textContent}`.toLowerCase().includes(query);
          });
          groups.forEach((group, index) => {
            const fields = [...group.querySelectorAll(fieldSelector)];
            group.hidden = !!query && !fields.some(label => !label.hidden);
            if (query && !group.hidden) group.open = true;
            else if (!query && priorOpen) group.open = priorOpen[index];
          });
          const matches = labels.filter(label => !label.hidden).reduce((total, label) => total + label.querySelectorAll('input, select').length, 0);
          count.textContent = query ? `${matches} matching settings` : 'Main controls first · expand a group for tuning';
          if (!query) priorOpen = null;
        });
        count.textContent = 'Main controls first · expand a group for tuning';
      });
    }

    function showAnalytics(bot) {
      const selected = bot === 'crypto' || bot === 'perps' ? bot : 'sports';
      localStorage.setItem('analyticsBot', selected);
      document.querySelectorAll('[data-analytics-tab]').forEach(btn => btn.classList.toggle('active', btn.dataset.analyticsTab === selected));
      showView(selected === 'perps' ? 'perps-shadow' : (selected === 'crypto' ? 'crypto-analytics' : 'analytics'));
    }

    function showSportsPanel(name) {
      document.querySelectorAll('[data-sports-panel]').forEach(el => el.classList.toggle('active', el.dataset.sportsPanel === name));
      document.querySelectorAll('[data-sports-tab]').forEach(btn => btn.classList.toggle('active', btn.dataset.sportsTab === name));
      localStorage.setItem('sportsPanel', name);
      setTimeout(updateTableScrollHelpers, 30);
    }

    function showCryptoPanel(name) {
      document.querySelectorAll('[data-crypto-panel]').forEach(el => el.classList.toggle('active', el.dataset.cryptoPanel === name));
      document.querySelectorAll('[data-crypto-tab]').forEach(btn => btn.classList.toggle('active', btn.dataset.cryptoTab === name));
      localStorage.setItem('cryptoPanel', name);
      setTimeout(updateTableScrollHelpers, 30);
    }

    function isVisible(el) {
      return Boolean(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    }

    function tableScrollNeedsHelper(scroller) {
      return isVisible(scroller) && scroller.scrollWidth > scroller.clientWidth + 1;
    }

    function updateTableScrollHelpers() {
      document.querySelectorAll('.table-scroll').forEach(scroller => {
        const top = scroller.previousElementSibling;
        if (!top || !top.classList || !top.classList.contains('table-scroll-top')) return;
        const inner = top.firstElementChild;
        if (!inner) return;
        inner.style.width = `${scroller.scrollWidth}px`;
        const show = tableScrollNeedsHelper(scroller);
        top.style.display = show ? 'block' : 'none';
        if (show) top.scrollLeft = scroller.scrollLeft;
      });
    }

    function installTableScrollHelpers() {
      document.querySelectorAll('.table-scroll').forEach(scroller => {
        if (scroller.dataset.scrollEnhanced === 'true') return;
        scroller.dataset.scrollEnhanced = 'true';
        const top = document.createElement('div');
        top.className = 'table-scroll-top';
        top.setAttribute('aria-hidden', 'true');
        top.innerHTML = '<div></div>';
        scroller.parentNode.insertBefore(top, scroller);

        let syncing = false;
        top.addEventListener('scroll', () => {
          if (syncing) return;
          syncing = true;
          scroller.scrollLeft = top.scrollLeft;
          syncing = false;
        });
        scroller.addEventListener('scroll', () => {
          if (syncing) return;
          syncing = true;
          top.scrollLeft = scroller.scrollLeft;
          syncing = false;
        });
        scroller.addEventListener('wheel', event => {
          const maxLeft = scroller.scrollWidth - scroller.clientWidth;
          if (maxLeft <= 1) return;
          const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
          if (!delta) return;
          const nextLeft = Math.max(0, Math.min(maxLeft, scroller.scrollLeft + delta));
          if (nextLeft === scroller.scrollLeft) return;
          scroller.scrollLeft = nextLeft;
          event.preventDefault();
        }, {passive: false});
        if (window.ResizeObserver) {
          const observer = new ResizeObserver(updateTableScrollHelpers);
          observer.observe(scroller);
        }
      });
      updateTableScrollHelpers();
    }

    function parseTime(value) {
      const parsed = parseCentralTime(value);
      return parsed ? parsed.getTime() : null;
    }

    function betTiming(row) {
      if (row.bet_timing_bucket) {
        const bucket = String(row.bet_timing_bucket).replaceAll('_', '-');
        if (bucket === 'early-live' || bucket === 'mid-live' || bucket === 'late-live') return bucket;
        if (bucket === 'in-game') return 'in-game';
        if (bucket === 'pregame') return 'pregame';
      }
      const placed = parseTime(row.placed_at);
      const start = parseTime(row.commence_time);
      if (!placed || !start) return 'timing-unknown';
      return placed >= start ? 'in-game' : 'pregame';
    }

    function sourceLabel(row) {
      const mode = row.mode || 'paper';
      return `${strategyOwner(row)}-${mode}-${betTiming(row)}`;
    }

    function fmtSetting(value, suffix = '') {
      if (value === undefined || value === null || value === '') return 'n/a';
      const num = Number(value);
      if (Number.isFinite(num)) return `${Number.isInteger(num) ? num.toFixed(0) : num.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')}${suffix}`;
      return `${value}${suffix}`;
    }

    function onOff(value) {
      return String(value).toLowerCase() === 'true' ? 'on' : 'off';
    }

    function marketShort(row) {
      const title = row.title || row.ticker || '';
      return title.length > 72 ? `${title.slice(0, 72)}...` : title;
    }

    function strategyOwner(row) {
      if (row.source === 'trusted_capper' || row.capper_source) return row.capper_source || 'trusted_capper';
      const lifeline = row.total_lifeline || {};
      if (lifeline.ok && (!row.strategy_owner || row.strategy_owner === 'small_edge')) return 'total_lifeline';
      if (row.strategy_owner) return row.strategy_owner;
      if ((row.topups || []).length) return 'manual_bet_more';
      const phase = row.phase_two || {};
      if (phase.active || phase.attempt_id || row.phase_two_attempt_id) return 'phase_two_cycle';
      const recovery = row.recovery_staking || {};
      if (recovery.active || recovery.recovery_attempt_id) return 'recovery';
      const recoveryBasket = row.recovery_basket || {};
      if (recoveryBasket.active || recoveryBasket.basket_attempt_id) return 'recovery_basket';
      const selective = row.selective_staking || {};
      if (selective.active) return 'selective_edge';
      if (row.source === 'bot_pick' || row.bot_name) return 'bot_pick';
      if (row.source === 'manual_live_test') return 'manual_bet_more';
      return 'small_edge';
    }

    function isUserBet(row) {
      return ['user_bet', 'manual_live_import'].includes(row.strategy_owner) || row.source === 'user_manual' || Boolean(row.user_bet) || Boolean(row.manual_bet);
    }

    function isTrustedCapperBet(row) {
      return row.source === 'trusted_capper' || row.strategy_owner === 'trusted_capper' || Boolean(row.trusted_capper_ticket_id);
    }

    function isSystemTestSportsBet(row) {
      return row.source === 'manual_live_test' || row.strategy_owner === 'manual_live_test';
    }

    function isRegularSportsBotBet(row) {
      return !isUserBet(row) && !isTrustedCapperBet(row) && row.source !== 'aibetpicks' && row.strategy_owner !== 'aibetpicks' && !isSystemTestSportsBet(row);
    }

    function americanFromCents(cents) {
      const price = Number(cents);
      if (!Number.isFinite(price) || price <= 0 || price >= 100) return '';
      const p = price / 100;
      if (p >= 0.5) return `-${Math.round((100 * p) / (1 - p))}`;
      return `+${Math.round((100 * (1 - p)) / p)}`;
    }

    function bookOddsLabel(row) {
      if (row.odds) return row.odds;
      const price = row.book_price;
      if (price === undefined || price === null || price === '') return '';
      const num = Number(price);
      if (!Number.isFinite(num)) return String(price);
      return num > 0 ? `+${num}` : String(num);
    }

    function kalshiOddsLabel(row) {
      const label = americanFromCents(row.entry_price);
      return label ? `${label} (${Number(row.entry_price).toFixed(0)}c)` : '';
    }

    function oddsLabel(row) {
      const kalshi = kalshiOddsLabel(row);
      const book = bookOddsLabel(row);
      if (kalshi && book) return `${kalshi} / book ${book}`;
      return kalshi || book;
    }

    function renderRows(id, rows, columns, emptyText) {
      const body = document.getElementById(id);
      if (!body) return;
      if (!rows.length) {
        body.innerHTML = `<tr><td colspan="${columns.length}">${emptyText}</td></tr>`;
        return;
      }
      body.innerHTML = rows.map(row => `<tr>${columns.map(col => {
        const value = col.render(row);
        return `<td class="${col.num ? 'num' : ''}">${col.html ? value : esc(value)}</td>`;
      }).join('')}</tr>`).join('');
    }

    function settledSportsRows(sports) {
      return (sports.history || []).filter(row => row.result === 'WIN' || row.result === 'LOSS');
    }

    function normalizedConfidence(row) {
      const raw = row.confidence ?? row.confidence_score;
      const num = Number(raw || 0);
      if (!Number.isFinite(num)) return 0;
      return num <= 1 ? num * 100 : num;
    }

    function confidenceBucket(row) {
      const conf = normalizedConfidence(row);
      if (!conf) return 'unknown';
      if (conf < 60) return '<60%';
      if (conf < 70) return '60-69%';
      if (conf < 80) return '70-79%';
      return '80%+';
    }

    function oddsBucket(row) {
      const odds = String(americanFromCents(row.entry_price) || bookOddsLabel(row) || '').replace('+', '');
      const num = Number(odds);
      if (!Number.isFinite(num)) return 'no odds';
      if (num > 0) return 'plus money';
      if (num < 0) return 'favorite';
      return 'even';
    }

    function priceBucket(row) {
      const price = Number(row.entry_price ?? row.current_yes_ask ?? row.book_price);
      if (!Number.isFinite(price)) return 'no price';
      if (price < 25) return '<25c';
      if (price < 35) return '25-34c';
      if (price < 45) return '35-44c';
      if (price <= 55) return '45-55c';
      if (price < 70) return '56-69c';
      return '70c+';
    }

    function preferredPriceBandBucket(row) {
      const price = Number(row.entry_price ?? row.current_yes_ask ?? row.book_price);
      if (!Number.isFinite(price)) return 'no price';
      return price >= 35 && price <= 55 ? '35c-55c preferred' : 'outside preferred band';
    }

    function finalScoreBucket(row) {
      const final = Number(row.final_bet_score ?? row.final_score ?? row.final_score_review?.score);
      if (!Number.isFinite(final) || final <= 0) return 'no final';
      if (final < 80) return '<80';
      if (final < 90) return '80-89';
      if (final < 95) return '90-94';
      if (final < 100) return '95-99';
      return '100+';
    }

    function combinedBucket(row, firstFn, secondFn) {
      return `${firstFn(row)} / ${secondFn(row)}`;
    }

    function stakeBucket(row) {
      const stake = Number(row.stake || 0);
      if (stake <= 0) return '$0 tracking';
      if (stake < 5) return '<$5';
      if (stake < 10) return '$5-$9';
      if (stake < 20) return '$10-$19';
      return '$20+';
    }

    function clvBucket(row) {
      const horizons = row.fixed_horizon_clv || {};
      const fixed = horizons['5m']?.clv_vs_entry_ask_cents
        ?? horizons['15m']?.clv_vs_entry_ask_cents
        ?? horizons['1m']?.clv_vs_entry_ask_cents;
      const clv = Number(fixed);
      if (!Number.isFinite(clv)) return 'no CLV';
      if (clv < -2) return '<-2c';
      if (clv < 0) return '-2c to 0c';
      if (clv === 0) return '0c';
      if (clv <= 2) return '0c to +2c';
      return '+2c+';
    }

    function timingBucket(row) {
      const timing = betTiming(row);
      if (timing === 'early-live') return 'early live';
      if (timing === 'mid-live') return 'mid live';
      if (timing === 'late-live') return 'late live';
      if (timing === 'in-game') {
        const minutes = Number(row.bet_timing_minutes_since_start ?? row.minutes_since_start);
        if (Number.isFinite(minutes)) {
          if (minutes <= 60) return 'early live';
          if (minutes <= 150) return 'mid live';
          return 'late live';
        }
        return 'in-game';
      }
      if (timing === 'pregame') return 'pregame';
      return 'unknown';
    }

    function sportLabel(row) {
      const raw = row.sport_key || row.sport || 'unknown';
      const map = {
        baseball_mlb: 'MLB',
        basketball_nba: 'NBA',
        basketball_wnba: 'WNBA',
        basketball_ncaab: 'NCAAB',
        americanfootball_ncaaf: 'NCAAF',
        americanfootball_nfl: 'NFL',
        americanfootball_nfl_preseason: 'NFL Preseason',
        icehockey_nhl: 'NHL'
      };
      return map[raw] || String(raw).replaceAll('_', ' ');
    }

    function betLabel(row) {
      const game = row.game || `${row.away_team || ''} @ ${row.home_team || ''}`.trim();
      const pick = row.pick || row.selected_team || row.selection || '';
      const market = row.market_type || row.bet_type || '';
      const line = row.line ?? row.market_line ?? '';
      const label = `${pick} ${market} ${line} - ${game}`.replace(/\s+/g, ' ').trim();
      return label && label !== '-' ? label : (row.market || row.market_title || row.title || row.kalshi_ticker || row.ticker || 'Bet');
    }

    function analyticsSource(row) {
      if (row.source === 'bot_pick' || row.bot_name) return row.bot_name || 'Bot pick';
      if (row.source === 'edge_scanner' || !row.source) return 'Edge scanner';
      return row.source;
    }

    function phaseTwoBucket(row) {
      const campaign = row.live_campaign || {};
      if (campaign.active || row.strategy_owner === 'live_campaign') {
        const bot = Number(campaign.bot_number || row.bot_number || 1);
        return campaign.role === 'support'
          ? `Live Campaign Bot ${bot} Support`
          : `Live Campaign Bot ${bot} Primary`;
      }
      const phase = row.phase_two || {};
      if (phase.active || phase.attempt_id || row.phase_two_attempt_id) return 'Retired Strategy';
      return 'Other Sports Bets';
    }

    function summarizeRows(rows, groupFn) {
      const map = new Map();
      for (const row of rows) {
        const key = groupFn(row) || 'unknown';
        if (!map.has(key)) map.set(key, {label: key, wins: 0, losses: 0, stake: 0, profit: 0});
        const item = map.get(key);
        item.wins += row.result === 'WIN' ? 1 : 0;
        item.losses += row.result === 'LOSS' ? 1 : 0;
        item.stake += Number(row.stake || 0);
        item.profit += Number(row.profit || 0);
      }
      return [...map.values()].map(item => ({
        ...item,
        settled: item.wins + item.losses,
        winRate: item.wins + item.losses ? (item.wins / (item.wins + item.losses)) * 100 : 0,
        stake: Number(item.stake.toFixed(2)),
        profit: Number(item.profit.toFixed(2)),
        roi: item.stake ? (item.profit / item.stake) * 100 : 0
      })).sort((a, b) => b.settled - a.settled || b.profit - a.profit);
    }

    function recentRows(rows, hours) {
      const cutoff = Date.now() - hours * 60 * 60 * 1000;
      return rows.filter(row => {
        const ts = parseTime(row.settled_at || row.closed_at || row.placed_at);
        return ts && ts >= cutoff;
      });
    }

    function priceSkipRows(sports) {
      const reasons = [
        'low_price_needs_stronger_quality',
        'moonshot_price_blocked',
        'moonshot_price_needs_insane_quality',
        'favorite_price_needs_elite_edge',
        'heavy_favorite_needs_elite_edge'
      ];
      const latest = new Map(reasons.map(reason => [reason, 0]));
      for (const row of (((sports.report || {}).top_candidates) || [])) {
        for (const reason of (row.skip_reasons || [])) {
          if (latest.has(reason)) latest.set(reason, latest.get(reason) + 1);
        }
      }
      const recent = new Map(reasons.map(reason => [reason, 0]));
      for (const row of (((sports.log_analytics || {}).skip_reasons) || [])) {
        if (recent.has(row.label)) recent.set(row.label, Number(row.count || 0));
      }
      return reasons.map(reason => ({label: reason, latest: latest.get(reason) || 0, recent: recent.get(reason) || 0}));
    }

    function renderPriceSkipTable(id, rows) {
      const body = document.getElementById(id);
      if (!body) return;
      body.innerHTML = rows.map(row => `<tr><td>${esc(row.label.replaceAll('_', ' '))}</td><td class="num">${row.latest}</td><td class="num">${row.recent}</td></tr>`).join('');
    }

    function reverseBetMath(row) {
      const stake = Number(row.stake || 0);
      const entry = Number(row.entry_price ?? row.current_yes_ask ?? row.book_price);
      if (!stake || !Number.isFinite(entry) || entry <= 0 || entry >= 100) return null;
      const reversePrice = 100 - entry;
      if (reversePrice <= 0 || reversePrice >= 100) return null;
      const reverseResult = row.result === 'LOSS' ? 'WIN' : 'LOSS';
      const reversePayout = reverseResult === 'WIN' ? stake / (reversePrice / 100) : 0;
      const reverseProfit = reverseResult === 'WIN' ? reversePayout - stake : -stake;
      return {
        ...row,
        originalProfit: Number(row.profit || 0),
        reverseResult,
        reversePrice,
        reverseProfit: Number(reverseProfit.toFixed(2)),
        reversePayout: Number(reversePayout.toFixed(2))
      };
    }

    function summarizeReverseRows(rows, groupFn) {
      const map = new Map();
      for (const row of rows) {
        const reversed = reverseBetMath(row);
        if (!reversed) continue;
        const key = groupFn(row) || 'unknown';
        if (!map.has(key)) {
          map.set(key, {
            label: key,
            originalWins: 0,
            originalLosses: 0,
            reverseWins: 0,
            reverseLosses: 0,
            stake: 0,
            originalProfit: 0,
            reverseProfit: 0
          });
        }
        const item = map.get(key);
        item.originalWins += row.result === 'WIN' ? 1 : 0;
        item.originalLosses += row.result === 'LOSS' ? 1 : 0;
        item.reverseWins += reversed.reverseResult === 'WIN' ? 1 : 0;
        item.reverseLosses += reversed.reverseResult === 'LOSS' ? 1 : 0;
        item.stake += Number(row.stake || 0);
        item.originalProfit += Number(row.profit || 0);
        item.reverseProfit += Number(reversed.reverseProfit || 0);
      }
      return [...map.values()].map(item => {
        const settled = item.originalWins + item.originalLosses;
        return {
          ...item,
          settled,
          stake: Number(item.stake.toFixed(2)),
          originalProfit: Number(item.originalProfit.toFixed(2)),
          reverseProfit: Number(item.reverseProfit.toFixed(2)),
          delta: Number((item.reverseProfit - item.originalProfit).toFixed(2)),
          originalWinRate: settled ? (item.originalWins / settled) * 100 : 0,
          reverseWinRate: settled ? (item.reverseWins / settled) * 100 : 0,
          reverseRoi: item.stake ? (item.reverseProfit / item.stake) * 100 : 0
        };
      }).sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta) || b.settled - a.settled);
    }

    function reverseTotals(rows) {
      const items = rows.map(reverseBetMath).filter(Boolean);
      const stake = items.reduce((sum, row) => sum + Number(row.stake || 0), 0);
      const originalProfit = items.reduce((sum, row) => sum + Number(row.originalProfit || 0), 0);
      const reverseProfit = items.reduce((sum, row) => sum + Number(row.reverseProfit || 0), 0);
      const reverseWins = items.filter(row => row.reverseResult === 'WIN').length;
      const reverseLosses = items.filter(row => row.reverseResult === 'LOSS').length;
      return {
        count: items.length,
        stake,
        originalProfit,
        reverseProfit,
        reverseWins,
        reverseLosses,
        reverseRoi: stake ? (reverseProfit / stake) * 100 : 0,
        delta: reverseProfit - originalProfit
      };
    }

    function renderReverseTable(id, rows) {
      renderRows(id, rows, [
        {render: r => r.label},
        {num: true, render: r => `${r.originalWins}W/${r.originalLosses}L (${r.originalWinRate.toFixed(1)}%)`},
        {num: true, render: r => `${r.reverseWins}W/${r.reverseLosses}L (${r.reverseWinRate.toFixed(1)}%)`},
        {num: true, render: r => money(r.originalProfit)},
        {num: true, render: r => money(r.reverseProfit)},
        {num: true, render: r => money(r.delta)}
      ], 'No reverse test data yet.');
    }

    function renderAnalyticsTable(id, rows, includeStake = false, includeRoi = false) {
      renderRows(id, rows, [
        {render: r => r.label},
        {num: true, render: r => `${r.wins}W/${r.losses}L (${r.settled})`},
        {num: true, render: r => `${r.winRate.toFixed(1)}%`},
        ...(includeStake ? [{num: true, render: r => money(r.stake)}] : []),
        ...(includeRoi ? [{num: true, render: r => `${r.roi.toFixed(1)}%`}] : []),
        {num: true, render: r => money(r.profit)}
      ], 'No settled sports data yet.');
    }

    function renderCountTable(id, rows, emptyText = 'No log data yet.') {
      renderRows(id, rows || [], [
        {render: r => r.label},
        {num: true, render: r => r.count}
      ], emptyText);
    }

    function renderExposureTable(id, rows) {
      renderRows(id, rows, [
        {render: r => r.label},
        {num: true, render: r => r.count},
        {num: true, render: r => money(r.exposure)}
      ], 'No open sports exposure.');
    }

    function summarizeOpenExposure(openRows, groupFn) {
      const map = new Map();
      for (const row of openRows || []) {
        const key = groupFn(row) || 'unknown';
        if (!map.has(key)) map.set(key, {label: key, count: 0, exposure: 0});
        const item = map.get(key);
        item.count += 1;
        item.exposure += Number(row.stake || 0);
      }
      return [...map.values()]
        .map(row => ({...row, exposure: Number(row.exposure.toFixed(2))}))
        .sort((a, b) => b.exposure - a.exposure || b.count - a.count);
    }

    function analyticsTotals(rows, openRows) {
      const stake = rows.reduce((sum, row) => sum + Number(row.stake || 0), 0);
      const profit = rows.reduce((sum, row) => sum + Number(row.profit || 0), 0);
      const wins = rows.filter(row => row.result === 'WIN');
      const losses = rows.filter(row => row.result === 'LOSS');
      const winProfit = wins.reduce((sum, row) => sum + Number(row.profit || 0), 0);
      const lossProfit = losses.reduce((sum, row) => sum + Number(row.profit || 0), 0);
      const openRisk = (openRows || []).reduce((sum, row) => sum + Number(row.stake || 0), 0);
      return {
        stake,
        profit,
        settled: rows.length,
        wins: wins.length,
        losses: losses.length,
        roi: stake ? (profit / stake) * 100 : 0,
        avgStake: rows.length ? stake / rows.length : 0,
        avgWin: wins.length ? winProfit / wins.length : 0,
        avgLoss: losses.length ? lossProfit / losses.length : 0,
        openRisk
      };
    }

    function missedFillRows(sports) {
      return (sports.missed_fill_history || []).filter(row => row.result === 'WIN' || row.result === 'LOSS');
    }

    function chartText(ctx, text, x, y, maxWidth) {
      const label = String(text || '');
      if (!maxWidth || ctx.measureText(label).width <= maxWidth) {
        ctx.fillText(label, x, y);
        return;
      }
      let clipped = label;
      while (clipped.length > 3 && ctx.measureText(`${clipped}...`).width > maxWidth) {
        clipped = clipped.slice(0, -1);
      }
      ctx.fillText(`${clipped}...`, x, y);
    }

    function themeColor(name, fallback) {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
    }

    function chartTheme() {
      return {
        text: themeColor('--ink', '#edf3ff'),
        muted: themeColor('--muted', '#9bacbf'),
        grid: themeColor('--chart-grid', '#24344b'),
        accent: themeColor('--accent', '#5aa2ff'),
        good: themeColor('--good', '#43d17f'),
        bad: themeColor('--bad', '#ff6b6b')
      };
    }

    const performanceRangeState = {
      overview: localStorage.getItem('performanceRangeOverview') || '30d',
      sports: localStorage.getItem('performanceRangeSports') || '30d',
      crypto: localStorage.getItem('performanceRangeCrypto') || '30d'
    };
    let latestPerformancePayload = null;

    function performanceRangeLabel(range) {
      return ({'7d': 'Past 7 days', '30d': 'Past 30 days', '90d': 'Past 90 days', all: 'All time'})[range] || 'Past 30 days';
    }

    function filterPerformanceRange(rows, range) {
      const ordered = (rows || []).slice().sort((a, b) => String(a.date).localeCompare(String(b.date)));
      if (!ordered.length || range === 'all') return ordered;
      const days = range === '7d' ? 7 : (range === '90d' ? 90 : 30);
      const end = new Date(`${ordered[ordered.length - 1].date}T12:00:00`);
      const cutoff = new Date(end);
      cutoff.setDate(cutoff.getDate() - days + 1);
      return ordered.filter(row => new Date(`${row.date}T12:00:00`) >= cutoff);
    }

    function summarizeDailyPerformance(rows) {
      const values = rows || [];
      const profit = values.reduce((sum, row) => sum + Number(row.profit || 0), 0);
      const stake = values.reduce((sum, row) => sum + Number(row.stake || 0), 0);
      const wins = values.reduce((sum, row) => sum + Number(row.wins || 0), 0);
      const losses = values.reduce((sum, row) => sum + Number(row.losses || 0), 0);
      const sportsProfit = values.reduce((sum, row) => sum + Number(row.sports_profit || 0), 0);
      const cryptoProfit = values.reduce((sum, row) => sum + Number(row.crypto_profit || 0), 0);
      return {
        profit,
        stake,
        wins,
        losses,
        sportsProfit,
        cryptoProfit,
        roi: stake ? (profit / stake) * 100 : 0,
        average: values.length ? profit / values.length : 0
      };
    }

    function maxBankrollDrawdown(rows) {
      let peak = null;
      let drawdown = 0;
      for (const row of rows || []) {
        const value = Number(row.bankroll || 0);
        peak = peak === null ? value : Math.max(peak, value);
        drawdown = Math.max(drawdown, peak - value);
      }
      return drawdown;
    }

    function preparePerformanceCanvas(id) {
      const canvas = document.getElementById(id);
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      if (rect.width < 20) return null;
      const scale = window.devicePixelRatio || 1;
      const width = Math.max(320, rect.width);
      const height = Math.max(230, rect.height || 260);
      canvas.width = Math.floor(width * scale);
      canvas.height = Math.floor(height * scale);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.font = '12px Segoe UI, Arial';
      return {canvas, ctx, width, height, theme: chartTheme()};
    }

    function shortChartDate(value) {
      const date = new Date(`${value}T12:00:00`);
      return date.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
    }

    function drawDailyPnlChart(id, rows, label) {
      const chart = preparePerformanceCanvas(id);
      if (!chart) return;
      const {canvas, ctx, width, height, theme} = chart;
      const data = rows || [];
      if (!data.length) {
        ctx.fillStyle = theme.muted;
        ctx.fillText('No settled bot results yet.', 14, 28);
        return;
      }
      const left = 58;
      const right = 18;
      const top = 28;
      const bottom = 42;
      const plotWidth = width - left - right;
      const plotHeight = height - top - bottom;
      const maxAbs = Math.max(...data.map(row => Math.abs(Number(row.profit || 0))), 1);
      const zeroY = top + plotHeight / 2;
      ctx.strokeStyle = theme.grid;
      ctx.lineWidth = 1;
      for (const y of [top, zeroY, top + plotHeight]) {
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(width - right, y);
        ctx.stroke();
      }
      ctx.fillStyle = theme.muted;
      ctx.fillText('Daily P&L', left, 16);
      ctx.fillText(money(maxAbs), 4, top + 4);
      ctx.fillText('$0', 30, zeroY + 4);
      ctx.fillText(`-${money(maxAbs)}`, 4, top + plotHeight + 4);
      const slot = plotWidth / Math.max(1, data.length);
      const gap = data.length > 60 ? 0.5 : 2;
      const barWidth = Math.max(1, slot - gap);
      const labelEvery = Math.max(1, Math.ceil(data.length / 7));
      data.forEach((row, index) => {
        const value = Number(row.profit || 0);
        const barHeight = value === 0 ? 1 : Math.max(2, (plotHeight / 2) * Math.abs(value) / maxAbs);
        const x = left + index * slot + gap / 2;
        const y = value >= 0 ? zeroY - barHeight : zeroY;
        ctx.fillStyle = value > 0 ? theme.good : (value < 0 ? theme.bad : theme.grid);
        ctx.fillRect(x, y, barWidth, barHeight);
        if (data.length <= 14 && value !== 0) {
          ctx.fillStyle = theme.text;
          const valueY = value > 0 ? Math.max(13, y - 5) : Math.min(height - bottom + 16, y + barHeight + 14);
          chartText(ctx, money(value), x, valueY, Math.max(slot * 1.8, 54));
        }
        if (index % labelEvery === 0 || index === data.length - 1) {
          ctx.fillStyle = theme.muted;
          const dateLabel = shortChartDate(row.date);
          const measured = ctx.measureText(dateLabel).width;
          ctx.fillText(dateLabel, Math.max(left, Math.min(width - right - measured, x - measured / 2)), height - 12);
        }
      });
      canvas.setAttribute('aria-label', `${label}: ${data.length} daily bars from ${data[0].date} through ${data[data.length - 1].date}. Green bars are profit and red bars are loss.`);
    }

    function drawBankrollChart(id, rows) {
      const chart = preparePerformanceCanvas(id);
      if (!chart) return;
      const {canvas, ctx, width, height, theme} = chart;
      const data = rows || [];
      if (!data.length) {
        ctx.fillStyle = theme.muted;
        ctx.fillText('No bankroll history yet.', 14, 28);
        return;
      }
      const left = 62;
      const right = 20;
      const top = 28;
      const bottom = 42;
      const plotWidth = width - left - right;
      const plotHeight = height - top - bottom;
      const values = data.map(row => Number(row.bankroll || 0));
      let minValue = Math.min(...values);
      let maxValue = Math.max(...values);
      const padding = Math.max((maxValue - minValue) * 0.14, Math.max(1, Math.abs(maxValue) * 0.01));
      minValue -= padding;
      maxValue += padding;
      const range = Math.max(1, maxValue - minValue);
      const point = (row, index) => ({
        x: left + (data.length === 1 ? plotWidth : plotWidth * index / (data.length - 1)),
        y: top + plotHeight - ((Number(row.bankroll || 0) - minValue) / range) * plotHeight
      });
      ctx.strokeStyle = theme.grid;
      ctx.lineWidth = 1;
      for (const fraction of [0, 0.5, 1]) {
        const y = top + plotHeight * fraction;
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(width - right, y);
        ctx.stroke();
      }
      ctx.fillStyle = theme.muted;
      ctx.fillText('Bankroll', left, 16);
      ctx.fillText(money(maxValue - padding), 4, top + 4);
      ctx.fillText(money(minValue + padding), 4, top + plotHeight + 4);
      const points = data.map(point);
      const gradient = ctx.createLinearGradient(0, top, 0, top + plotHeight);
      gradient.addColorStop(0, 'rgba(90,162,255,.26)');
      gradient.addColorStop(1, 'rgba(90,162,255,.015)');
      ctx.beginPath();
      ctx.moveTo(points[0].x, top + plotHeight);
      points.forEach((p, index) => index ? ctx.lineTo(p.x, p.y) : ctx.lineTo(p.x, p.y));
      ctx.lineTo(points[points.length - 1].x, top + plotHeight);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();
      ctx.beginPath();
      points.forEach((p, index) => index ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
      ctx.strokeStyle = theme.accent;
      ctx.lineWidth = 2.5;
      ctx.stroke();
      const last = points[points.length - 1];
      ctx.beginPath();
      ctx.arc(last.x, last.y, 4, 0, Math.PI * 2);
      ctx.fillStyle = theme.accent;
      ctx.fill();
      ctx.fillStyle = theme.text;
      const currentLabel = money(values[values.length - 1]);
      const labelWidth = ctx.measureText(currentLabel).width;
      ctx.fillText(currentLabel, Math.max(left, Math.min(width - right - labelWidth, last.x - labelWidth)), Math.max(15, last.y - 9));
      const labelEvery = Math.max(1, Math.ceil(data.length / 6));
      data.forEach((row, index) => {
        if (index % labelEvery !== 0 && index !== data.length - 1) return;
        const p = points[index];
        const dateLabel = shortChartDate(row.date);
        const measured = ctx.measureText(dateLabel).width;
        ctx.fillStyle = theme.muted;
        ctx.fillText(dateLabel, Math.max(left, Math.min(width - right - measured, p.x - measured / 2)), height - 12);
      });
      canvas.setAttribute('aria-label', `Bot-only reconstructed equity from ${data[0].date} through ${data[data.length - 1].date}, ending at ${money(values[values.length - 1])}.`);
    }

    function renderBotDailyPerformance(scope, rows) {
      const range = performanceRangeState[scope] || '30d';
      const filtered = filterPerformanceRange(rows, range);
      const totals = summarizeDailyPerformance(filtered);
      setText(`${scope}-period-profit`, money(totals.profit), `value ${totals.profit >= 0 ? 'positive' : 'negative'}`);
      setText(`${scope}-period-roi`, `${totals.roi.toFixed(1)}%`, `value ${totals.roi >= 0 ? 'positive' : 'negative'}`);
      setText(`${scope}-period-record`, `${totals.wins}W / ${totals.losses}L`, 'value');
      setText(`${scope}-period-average`, money(totals.average), `value ${totals.average >= 0 ? 'positive' : 'negative'}`);
      drawDailyPnlChart(`${scope}-daily-pnl-chart`, filtered, `${scope === 'sports' ? 'Sports' : 'Crypto'} daily profit and loss`);
      const activeDays = filtered.filter(row => Number(row.settled || 0) > 0);
      const best = activeDays.slice().sort((a, b) => Number(b.profit || 0) - Number(a.profit || 0))[0];
      const worst = activeDays.slice().sort((a, b) => Number(a.profit || 0) - Number(b.profit || 0))[0];
      const details = activeDays.length
        ? `${performanceRangeLabel(range)}: ${activeDays.length} active day${activeDays.length === 1 ? '' : 's'}, ${totals.wins + totals.losses} settled bets. Best ${best.date} ${money(best.profit)}; worst ${worst.date} ${money(worst.profit)}.`
        : `${performanceRangeLabel(range)}: no settled automated bets.`;
      setText(`${scope}-daily-performance-summary`, details, 'performance-summary');
    }

    function renderOverviewPerformance(performance) {
      const range = performanceRangeState.overview || '30d';
      const daily = filterPerformanceRange((performance || {}).daily || [], range);
      const bankroll = filterPerformanceRange((performance || {}).bankroll || [], range);
      const totals = summarizeDailyPerformance(daily);
      const drawdown = maxBankrollDrawdown(bankroll);
      setText('overview-bankroll', money((performance || {}).current_bankroll || 0), 'value');
      setText('overview-period-profit', money(totals.profit), `value ${totals.profit >= 0 ? 'positive' : 'negative'}`);
      setText('overview-period-sports', money(totals.sportsProfit), `value ${totals.sportsProfit >= 0 ? 'positive' : 'negative'}`);
      setText('overview-period-crypto', money(totals.cryptoProfit), `value ${totals.cryptoProfit >= 0 ? 'positive' : 'negative'}`);
      setText('overview-period-roi', `${totals.roi.toFixed(1)}%`, `value ${totals.roi >= 0 ? 'positive' : 'negative'}`);
      setText('overview-period-drawdown', money(drawdown), `value ${drawdown > 0 ? 'negative' : 'positive'}`);
      drawBankrollChart('overview-bankroll-chart', bankroll);
      drawDailyPnlChart('overview-daily-chart', daily, 'Automated sports and crypto bot daily profit and loss');
      const first = bankroll[0];
      const last = bankroll[bankroll.length - 1];
      const change = first && last ? Number(last.bankroll || 0) - Number(first.bankroll || 0) : 0;
      setText('overview-bankroll-summary', bankroll.length
        ? `${performanceRangeLabel(range)}: ${money(first.bankroll)} to ${money(last.bankroll)} (${change >= 0 ? '+' : ''}${money(change)}). Maximum closing drawdown ${money(drawdown)}.`
        : 'No bankroll history yet.', 'performance-summary');
      const activeDays = daily.filter(row => Number(row.settled || 0) > 0);
      const best = activeDays.slice().sort((a, b) => Number(b.profit || 0) - Number(a.profit || 0))[0];
      const worst = activeDays.slice().sort((a, b) => Number(a.profit || 0) - Number(b.profit || 0))[0];
      setText('overview-daily-summary', activeDays.length
        ? `${totals.wins}W / ${totals.losses}L across ${activeDays.length} active days. Best ${best.date} ${money(best.profit)}; worst ${worst.date} ${money(worst.profit)}.`
        : `${performanceRangeLabel(range)}: no settled automated bets.`, 'performance-summary');
    }

    function renderPerformanceScope(scope) {
      if (!latestPerformancePayload) return;
      document.querySelectorAll(`[data-performance-range="${scope}"] button`).forEach(button => {
        button.classList.toggle('active', button.dataset.range === performanceRangeState[scope]);
      });
      if (scope === 'overview') renderOverviewPerformance(latestPerformancePayload.performance);
      if (scope === 'sports') renderBotDailyPerformance('sports', latestPerformancePayload.sports.daily_performance || []);
      if (scope === 'crypto') renderBotDailyPerformance('crypto', latestPerformancePayload.crypto.daily_performance || []);
    }

    function setPerformanceRange(scope, range) {
      if (!['overview', 'sports', 'crypto'].includes(scope) || !['7d', '30d', '90d', 'all'].includes(range)) return;
      performanceRangeState[scope] = range;
      localStorage.setItem(`performanceRange${scope[0].toUpperCase()}${scope.slice(1)}`, range);
      renderPerformanceScope(scope);
    }

    function renderPerformance(data, sports, crypto) {
      latestPerformancePayload = {performance: data.performance || {}, sports: sports || {}, crypto: crypto || {}};
      renderPerformanceScope('overview');
      renderPerformanceScope('sports');
      renderPerformanceScope('crypto');
    }

    function drawBarChart(id, rows, valueFn, options = {}) {
      const canvas = document.getElementById(id);
      if (!canvas) return;
      const theme = chartTheme();
      const opts = typeof options === 'string' ? {color: options} : options;
      const color = opts.color || theme.accent;
      const format = opts.format || (value => String(Math.round(value)));
      const axisLabel = opts.axisLabel || '';
      const rect = canvas.getBoundingClientRect();
      const scale = window.devicePixelRatio || 1;
      canvas.width = Math.max(320, Math.floor(rect.width * scale));
      canvas.height = Math.max(220, Math.floor(rect.height * scale));
      const ctx = canvas.getContext('2d');
      ctx.scale(scale, scale);
      const width = canvas.width / scale;
      const height = canvas.height / scale;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = theme.muted;
      ctx.font = '12px Segoe UI, Arial';
      if (!rows.length) {
        ctx.fillText(opts.empty || 'No settled data yet.', 14, 28);
        return;
      }
      const data = rows.slice(0, 8);
      const max = Math.max(...data.map(valueFn), 1);
      const left = 44;
      const right = 14;
      const top = 30;
      const bottom = 64;
      const plotWidth = width - left - right;
      const plotHeight = height - top - bottom;
      const barGap = 10;
      const barWidth = Math.max(18, (plotWidth - barGap * (data.length - 1)) / data.length);
      ctx.strokeStyle = theme.grid;
      ctx.lineWidth = 1;
      for (const tick of [0, 0.5, 1]) {
        const y = top + plotHeight - plotHeight * tick;
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(width - right, y);
        ctx.stroke();
      }
      ctx.fillStyle = theme.muted;
      ctx.fillText(axisLabel, left, 16);
      ctx.fillText(format(max), 6, top + 4);
      ctx.fillText('0', 28, top + plotHeight + 4);
      data.forEach((row, index) => {
        const value = valueFn(row);
        const barHeight = Math.max(3, plotHeight * value / max);
        const x = left + index * (barWidth + barGap);
        const y = top + plotHeight - barHeight;
        ctx.fillStyle = color;
        ctx.fillRect(x, y, barWidth, barHeight);
        ctx.fillStyle = theme.text;
        chartText(ctx, format(value), x, Math.max(14, y - 6), barWidth + barGap);
        ctx.save();
        ctx.translate(x + 2, height - 18);
        ctx.rotate(-0.45);
        ctx.fillStyle = theme.muted;
        chartText(ctx, row.label, 0, 0, 86);
        ctx.restore();
      });
    }

    function drawProfitChart(id, rows) {
      const canvas = document.getElementById(id);
      if (!canvas) return;
      const theme = chartTheme();
      const rect = canvas.getBoundingClientRect();
      const scale = window.devicePixelRatio || 1;
      canvas.width = Math.max(320, Math.floor(rect.width * scale));
      canvas.height = Math.max(220, Math.floor(rect.height * scale));
      const ctx = canvas.getContext('2d');
      ctx.scale(scale, scale);
      const width = canvas.width / scale;
      const height = canvas.height / scale;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = theme.muted;
      ctx.font = '12px Segoe UI, Arial';
      if (!rows.length) {
        ctx.fillText('No settled data yet.', 14, 28);
        return;
      }
      const data = rows.slice(0, 8);
      const left = 44;
      const right = 14;
      const top = 30;
      const bottom = 64;
      const plotWidth = width - left - right;
      const plotHeight = height - top - bottom;
      const maxAbs = Math.max(...data.map(row => Math.abs(Number(row.profit || 0))), 1);
      const zeroY = top + plotHeight / 2;
      const barGap = 10;
      const barWidth = Math.max(18, (plotWidth - barGap * (data.length - 1)) / data.length);
      ctx.strokeStyle = theme.grid;
      ctx.lineWidth = 1;
      for (const y of [top, zeroY, top + plotHeight]) {
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(width - right, y);
        ctx.stroke();
      }
      ctx.fillStyle = theme.muted;
      ctx.fillText('Profit $', left, 16);
      ctx.fillText(money(maxAbs), 2, top + 4);
      ctx.fillText('$0', 24, zeroY + 4);
      ctx.fillText(`-${money(maxAbs)}`, 2, top + plotHeight + 4);
      data.forEach((row, index) => {
        const value = Number(row.profit || 0);
        const barHeight = Math.max(3, (plotHeight / 2) * Math.abs(value) / maxAbs);
        const x = left + index * (barWidth + barGap);
        const y = value >= 0 ? zeroY - barHeight : zeroY;
        ctx.fillStyle = value >= 0 ? theme.good : theme.bad;
        ctx.fillRect(x, y, barWidth, barHeight);
        ctx.fillStyle = theme.text;
        chartText(ctx, money(value), x, value >= 0 ? Math.max(14, y - 6) : y + barHeight + 14, barWidth + barGap);
        ctx.save();
        ctx.translate(x + 2, height - 18);
        ctx.rotate(-0.45);
        ctx.fillStyle = theme.muted;
        chartText(ctx, row.label, 0, 0, 86);
        ctx.restore();
      });
    }

    function drawSignedChart(id, rows, valueFn, options = {}) {
      const canvas = document.getElementById(id);
      if (!canvas) return;
      const theme = chartTheme();
      const rect = canvas.getBoundingClientRect();
      const scale = window.devicePixelRatio || 1;
      canvas.width = Math.max(320, Math.floor(rect.width * scale));
      canvas.height = Math.max(220, Math.floor(rect.height * scale));
      const ctx = canvas.getContext('2d');
      ctx.scale(scale, scale);
      const width = canvas.width / scale;
      const height = canvas.height / scale;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = theme.muted;
      ctx.font = '12px Segoe UI, Arial';
      if (!rows.length) {
        ctx.fillText(options.empty || 'No settled sports data yet.', 14, 28);
        return;
      }
      const data = rows.slice(0, 8);
      const left = 44;
      const right = 14;
      const top = 30;
      const bottom = 64;
      const plotWidth = width - left - right;
      const plotHeight = height - top - bottom;
      const values = data.map(row => Number(valueFn(row) || 0));
      const maxAbs = Math.max(...values.map(value => Math.abs(value)), 1);
      const zeroY = top + plotHeight / 2;
      const barGap = 10;
      const barWidth = Math.max(18, (plotWidth - barGap * (data.length - 1)) / data.length);
      const format = options.format || (value => String(Math.round(value)));
      ctx.strokeStyle = theme.grid;
      for (const y of [top, zeroY, top + plotHeight]) {
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(width - right, y);
        ctx.stroke();
      }
      ctx.fillStyle = theme.muted;
      ctx.fillText(options.axisLabel || '', left, 16);
      ctx.fillText(format(maxAbs), 2, top + 4);
      ctx.fillText('0', 28, zeroY + 4);
      ctx.fillText(format(-maxAbs), 2, top + plotHeight + 4);
      data.forEach((row, index) => {
        const value = Number(valueFn(row) || 0);
        const barHeight = Math.max(3, (plotHeight / 2) * Math.abs(value) / maxAbs);
        const x = left + index * (barWidth + barGap);
        const y = value >= 0 ? zeroY - barHeight : zeroY;
        ctx.fillStyle = value >= 0 ? theme.good : theme.bad;
        ctx.fillRect(x, y, barWidth, barHeight);
        ctx.fillStyle = theme.text;
        chartText(ctx, format(value), x, value >= 0 ? Math.max(14, y - 6) : y + barHeight + 14, barWidth + barGap);
        ctx.save();
        ctx.translate(x + 2, height - 18);
        ctx.rotate(-0.45);
        ctx.fillStyle = theme.muted;
        chartText(ctx, row.label, 0, 0, 86);
        ctx.restore();
      });
    }

    function settledCryptoRows(crypto) {
      return (crypto.history || []).filter(row => row.result === 'WIN' || row.result === 'LOSS');
    }

    function cryptoPriceBucket(row) {
      const price = Number(row.entry_price);
      if (!Number.isFinite(price)) return 'no price';
      if (price < 20) return '<20c';
      if (price < 35) return '20-34c';
      if (price < 45) return '35-44c';
      if (price < 63) return '45-62c';
      if (price <= 70) return '63-70c';
      if (price <= 80) return '71-80c';
      return '80c+';
    }

    function cryptoEdgeBucket(row) {
      const edge = Number(row.edge || 0);
      if (!Number.isFinite(edge)) return 'no edge';
      if (edge < 0) return 'negative';
      if (edge < 6) return '0-5%';
      if (edge < 10) return '6-9%';
      if (edge < 15) return '10-14%';
      if (edge < 20) return '15-19%';
      return '20%+';
    }

    function cryptoConfidenceBucket(row) {
      const conf = normalizedConfidence(row);
      if (!conf) return 'unknown';
      if (conf < 60) return '<60';
      if (conf < 70) return '60-69';
      if (conf < 80) return '70-79';
      if (conf < 90) return '80-89';
      return '90+';
    }

    function cryptoSelectiveBucket(row) {
      const campaign = row.crypto_live_campaign || {};
      if (campaign.quality_tier) return String(campaign.quality_tier);
      const selective = row.selective_edge || {};
      if (selective.active) return `retired: ${selective.tier || 'selective'}`;
      return 'unclassified';
    }

    function cryptoRecoveryBucket(row) {
      const campaign = row.crypto_live_campaign || {};
      if (campaign.active || row.strategy_owner === 'crypto_15m_campaign') {
        const role = campaign.role === 'support' ? 'support' : 'primary';
        const bot = Number(campaign.bot_number || row.bot_number || 1);
        const units = Number((campaign.crypto_units || row.crypto_units || {}).applied_total_units || 0);
        return `Bot ${bot} / ${units > 0 ? `${units.toFixed(units % 1 ? 1 : 0)}u` : role}`;
      }
      const recovery = row.shared_recovery || {};
      if (recovery.active) return 'retired: shared recovery';
      return 'other crypto bets';
    }

    function cryptoCandidateRows(analytics, key) {
      return ((analytics || {})[key] || []).map(row => ({
        label: row.label || 'unknown',
        count: Number(row.count || 0),
        eligible: Number(row.eligible || 0),
        placed: Number(row.placed || 0),
        avg_edge: Number(row.avg_edge || 0),
        avg_confidence: Number(row.avg_confidence || 0)
      }));
    }

    function renderCryptoCandidateTable(id, rows) {
      renderRows(id, rows || [], [
        {render: r => r.label},
        {num: true, render: r => r.count},
        {num: true, render: r => r.eligible},
        {num: true, render: r => r.placed},
        {num: true, render: r => `${Number(r.avg_edge || 0).toFixed(1)}%`},
        {num: true, render: r => Number(r.avg_confidence || 0).toFixed(1)}
      ], 'No crypto candidate analytics yet.');
    }

    function compactCryptoEvent(row) {
      const out = {ts: row.ts, type: row.type};
      for (const key of ['markets', 'candidates', 'placed', 'settled', 'balance', 'open_exposure']) {
        if (row[key] !== undefined) out[key] = row[key];
      }
      if (row.summary) out.summary = row.summary;
      if (row.skip_reasons) out.skip_reasons = row.skip_reasons;
      return centralizeTextTimes(JSON.stringify(out));
    }

    function setText(id, value, className) {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = value;
      if (className !== undefined) el.className = className;
    }

    function setPnl(id, value) {
      const amount = Number(value || 0);
      setText(id, money(amount), `value ${amount >= 0 ? 'positive' : 'negative'}`);
    }

    function campaignBotNumber(row) {
      const campaign = (row || {}).live_campaign || {};
      if (!campaign.active && (row || {}).strategy_owner !== 'live_campaign') return '-';
      const value = Number(campaign.bot_number || (row || {}).bot_number || 1);
      return Number.isFinite(value) && value > 0 ? Math.floor(value) : 1;
    }

    function sportsCampaignBots(campaign) {
      const rows = Array.isArray((campaign || {}).bots) ? campaign.bots : [];
      return rows.length ? rows : [campaign || {}];
    }

    function cryptoCampaignBotNumber(row) {
      const campaign = (row || {}).crypto_live_campaign || {};
      if (!campaign.enabled && (row || {}).strategy_owner !== 'crypto_15m_campaign') return '-';
      const value = Number(campaign.bot_number || (row || {}).bot_number || 1);
      return Number.isFinite(value) && value > 0 ? Math.floor(value) : 1;
    }

    function cryptoCampaignBots(campaign) {
      const enabled = Array.isArray((campaign || {}).bots) ? campaign.bots : [];
      const disabled = Array.isArray((campaign || {}).disabled_bots) ? campaign.disabled_bots : [];
      const rows = [...enabled, ...disabled];
      return rows.length ? rows : [campaign || {}];
    }

    function renderSportsCampaignCards(id, campaign) {
      const target = document.getElementById(id);
      if (!target) return;
      const bots = sportsCampaignBots(campaign);
      target.innerHTML = bots.map((bot, index) => {
        const botNumber = Number(bot.bot_number || index + 1);
        const active = Number(bot.bot_live_active_open_count || 0);
        const assumed = Number(bot.assumed_loss_open_count || 0);
        const status = String(bot.status || 'waiting').replaceAll('_', ' ');
        return `
          <div class="campaign-lane-card">
            <div class="campaign-lane-head"><strong>Sports Bot ${botNumber}</strong><span class="health-chip ${status === 'active' ? 'good' : ''}">${esc(status.toUpperCase())}</span></div>
            <div class="campaign-lane-metrics">
              <span>Active <b>${active} / ${Number(bot.max_open || 1)}</b></span>
              <span>Mode <b>always-on units</b></span>
              <span>Assumed <b>${assumed}</b></span>
              <span>Daily loss <b>${Number(bot.daily_loss_cap || 0) > 0 ? money(bot.daily_loss_remaining || 0) + ' room' : 'uncapped'}</b></span>
              <span>Today P/L <b>${money(bot.bot_daily_realized_profit ?? 0)}</b></span>
              <span>Total P/L <b>${money(bot.realized_profit ?? 0)}</b></span>
            </div>
          </div>`;
      }).join('');
    }

    function renderCryptoCampaignCards(id, campaign) {
      const target = document.getElementById(id);
      if (!target) return;
      const bots = cryptoCampaignBots(campaign);
      const recoveryEnabled = (campaign?.bounded_unit_recovery || {}).enabled === true;
      const recoveryMinimumProbability = Number(
        (campaign?.bounded_unit_recovery || {}).minimum_probability_units || 3
      );
      target.innerHTML = bots.map((bot, index) => {
        const botNumber = Number(bot.bot_number || index + 1);
        const active = Number(bot.bot_live_open_count || 0);
        const disabled = bot.configured === false;
        const status = disabled ? 'disabled' : String(bot.status || 'waiting').replaceAll('_', ' ');
        const ledger = bot.high_water_drawdown || {};
        const drawdown = Number(bot.outstanding_drawdown ?? ledger.outstanding_drawdown ?? 0);
        const pooledDrawdown = Number(bot.pooled_outstanding_drawdown ?? 0);
        const settled = Number(ledger.settled_since_migration || 0);
        const wins = Number(ledger.wins_since_migration || 0);
        const losses = Number(ledger.losses_since_migration || 0);
        return `
          <div class="campaign-lane-card">
            <div class="campaign-lane-head"><strong>Crypto Bot ${botNumber}</strong><span class="health-chip ${!disabled && status === 'active' ? 'good' : ''}">${esc(status.toUpperCase())}</span></div>
            <div class="campaign-lane-metrics">
              <span>Active <b>${active} / ${Number(bot.max_open || 1)}</b></span>
              <span>Mode <b>continuous units</b></span>
              <span>High water <b>${money(ledger.high_water_profit || 0)}</b></span>
              <span>Ledger P/L <b>${money(ledger.current_profit || 0)}</b></span>
              <span>Drawdown <b>${money(drawdown)}</b></span>
              <span>Recovery <b>${recoveryEnabled ? (pooledDrawdown > 0.009 ? `pooled ${recoveryMinimumProbability}u+ probability lane` : 'clear') : (pooledDrawdown > 0.009 ? 'bonus off · ledger in drawdown' : 'bonus off')}</b></span>
              <span>Settled <b>${settled} (${wins}W-${losses}L)</b></span>
              <span>Today P/L <b>${money(bot.bot_daily_realized_profit || 0)}</b></span>
              <span>Loss room <b>${money(bot.daily_loss_remaining || 0)}${bot.unit_capacity_limited ? ` / ${money(bot.minimum_unit_capacity || 0)} minimum` : ''}</b></span>
            </div>
          </div>`;
      }).join('');
    }

    function renderSportsUnitSizePreview(bankrollOverride = null, pctOverride = null, snapshotDate = null, lockedForDay = null, currentEquity = null) {
      const preview = document.getElementById('sports-unit-size-preview');
      const input = document.getElementById('SPORTS_UNIT_SIZE_PCT');
      if (!preview) return;
      if (bankrollOverride !== null && Number.isFinite(Number(bankrollOverride))) {
        preview.dataset.bankroll = String(Number(bankrollOverride));
      }
      if (snapshotDate !== null) preview.dataset.snapshotDate = String(snapshotDate || '');
      if (lockedForDay !== null) preview.dataset.locked = lockedForDay ? 'true' : 'false';
      if (currentEquity !== null && Number.isFinite(Number(currentEquity))) preview.dataset.currentEquity = String(Number(currentEquity));
      const bankroll = Number(preview.dataset.bankroll || 0);
      const pct = Number(pctOverride ?? (input ? input.value : 2));
      const safePct = Number.isFinite(pct) ? pct : 0;
      const pctLabel = Number(safePct.toFixed(3)).toString();
      const locked = preview.dataset.locked === 'true';
      const dateLabel = preview.dataset.snapshotDate || '';
      const equity = Number(preview.dataset.currentEquity || bankroll);
      const lockLabel = locked ? `; daily base locked for ${dateLabel}` : '; snapshot will lock on the next sports scan';
      const equityLabel = Math.abs(equity - bankroll) >= 0.005 ? `; current equity ${money(equity)}` : '';
      setText('sports-unit-size-preview', `${pctLabel}% of ${money(bankroll)} daily bankroll base = ${money(bankroll * safePct / 100)} per unit${lockLabel}${equityLabel}`);
    }

    function renderSportsUnitRequirements(sports, dashboardSettings = {}) {
      const target = document.getElementById('sports-unit-requirements-rows');
      if (!target) return;
      const reportSettings = (((sports || {}).report || {}).settings || {});
      const configured = reportSettings.unit_tiers || {};
      const fallback = {
        '0.5': {min_edge: 0, min_confidence: 70, min_pro_score: 85, min_final_score: 80, min_book_families: 2, qualification_mode: 'qualified_risk_reduction_only'},
        '1': {min_edge: 0, min_confidence: 70, min_pro_score: 85, min_final_score: 80, min_book_families: 2},
        '1.5': {min_edge: 0.5, min_confidence: 73, min_pro_score: 87.5, min_final_score: 82.5, min_book_families: 2, interpolated: true},
        '2': {min_edge: 1, min_confidence: 76, min_pro_score: 90, min_final_score: 85, min_book_families: 2},
        '2.5': {min_edge: 1.5, min_confidence: 79, min_pro_score: 92.5, min_final_score: 87, min_book_families: 2, interpolated: true},
        '3': {min_edge: 2, min_confidence: 82, min_pro_score: 95, min_final_score: 89, min_book_families: 2},
        '3.5': {min_edge: 3, min_confidence: 85, min_pro_score: 97.5, min_final_score: 90.5, min_book_families: 2, interpolated: true},
        '4': {min_edge: 4, min_confidence: 88, min_pro_score: 100, min_final_score: 92, min_book_families: 2},
        '4.5': {min_edge: 5, min_confidence: 90, min_pro_score: 102.5, min_final_score: 93.5, min_book_families: 2, interpolated: true},
        '5': {min_edge: 6, min_confidence: 92, min_pro_score: 105, min_final_score: 95, min_book_families: 2},
      };
      const units = [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5];
      const display = value => {
        const number = Number(value);
        return Number.isFinite(number) ? Number(number.toFixed(3)).toString() : '--';
      };
      target.innerHTML = units.map(unit => {
        const key = Number.isInteger(unit) ? String(Math.trunc(unit)) : String(unit);
        const tier = configured[key] || fallback[key] || {};
        const usage = unit === 0.5
          ? 'Live risk reduction · relaxed entry shadow-tested'
          : tier.interpolated
            ? 'Live · midpoint between configured tiers'
            : 'Live · configured quality tier';
        return `<tr>
          <td class="num"><strong>${display(unit)}U</strong></td>
          <td class="num">&gt;${display(tier.min_edge)}%</td>
          <td class="num">${display(tier.min_confidence)}</td>
          <td class="num">${display(tier.min_pro_score)}</td>
          <td class="num">${display(tier.min_final_score)}</td>
          <td class="num">${display(tier.min_book_families)}</td>
          <td>${esc(usage)}</td>
        </tr>`;
      }).join('');
      const unitSize = Number(
        (sports || {}).unit_sizing?.unit_size
        ?? reportSettings.unit_size
        ?? 0
      );
      const increment = Number(reportSettings.unit_increment ?? dashboardSettings.SPORTS_UNIT_INCREMENT ?? 0.5);
      const maximum = Number(reportSettings.unit_max_per_market ?? dashboardSettings.SPORTS_UNIT_MAX_PER_MARKET ?? 5);
      setText(
        'sports-unit-requirements-meta',
        `${unitSize > 0 ? `${money(unitSize)} per 1U · ` : ''}${display(increment)}U steps · ${display(maximum)}U max`,
        'health-chip good',
      );
    }

    function renderSimpleDashboard(data, sports, crypto, sportsCampaign, cryptoCampaign) {
      const processes = data.processes || {};
      const sportsRunning = Boolean((processes.sports || {}).running);
      const cryptoRunning = Boolean((processes.crypto || {}).running);
      const sportsProcessLabel = sportsRunning ? 'RUNNING' : String((processes.sports || {}).status || 'offline').toUpperCase();
      const cryptoProcessLabel = cryptoRunning ? 'RUNNING' : String((processes.crypto || {}).status || 'offline').toUpperCase();
      const sportsOpen = sports.bot_open_bets || [];
      const cryptoOpen = crypto.bot_open_bets || [];
      const sportsBots = sportsCampaignBots(sportsCampaign);
      const cryptoBots = cryptoCampaignBots(cryptoCampaign).filter(bot => bot.configured !== false);

      for (const prefix of ['overview-sports', 'sports']) {
        setPnl(`${prefix}-daily`, sports.bot_today_profit || 0);
        setPnl(`${prefix}-total`, sports.bot_total_profit || 0);
      }
      const sportsSources = sports.source_analytics || {};
      const allSportsLedger = sportsSources.all_sports || {};
      const influencedLedger = sportsSources.influenced_bets || {};
      const userLiveLedger = sportsSources.user_live || {};
      const systemTestLedger = sportsSources.system_test || {};
      for (const prefix of ['overview-sports', 'sports']) {
        setPnl(`${prefix}-account-pnl`, allSportsLedger.profit || 0);
        setPnl(`${prefix}-account-mtd`, (allSportsLedger.month_to_date || {}).profit || 0);
        setPnl(`${prefix}-aibetpicks-pnl`, sports.aibetpicks_results?.profit || 0);
        setText(`${prefix}-aibetpicks-units`, `${aibetUnits(sports.aibetpicks_results?.profit_units, true)} net · ${sports.aibetpicks_results?.wins || 0}W / ${sports.aibetpicks_results?.losses || 0}L`);
        setPnl(`${prefix}-user-pnl`, userLiveLedger.profit || 0);
        setPnl(`${prefix}-test-pnl`, systemTestLedger.profit || 0);
      }
      setPnl('sports-today-pnl', sports.bot_today_profit || 0);
      setPnl('sports-pnl', sports.bot_total_profit || 0);
      const sportsActiveOpen = sportsCampaign.campaign_lane_active_open_count ?? sportsCampaign.bot_live_active_open_count ?? sportsCampaign.bot_live_open_count ?? sportsOpen.length;
      const aibetpicksOpen = sports.aibetpicks_results?.open_count ?? (sports.open_bets || []).filter(row => row.source === 'aibetpicks' || row.strategy_owner === 'aibetpicks').length;
      const sportsAssumedOpen = sportsCampaign.assumed_loss_open_count || 0;
      const trustedCapperOpen = sportsCampaign.trusted_capper_open_count ?? sportsOpen.filter(row => row.source === 'trusted_capper' || row.trusted_capper_ticket_id).length;
      setText('overview-sports-open', `${sportsActiveOpen} / ${sportsCampaign.max_open || 1} normal active / ${aibetpicksOpen} AIBetPicks`);
      setText('sports-active-count', `${sportsActiveOpen} / ${sportsCampaign.max_open || 1} normal active / ${aibetpicksOpen} AIBetPicks / ${sportsAssumedOpen} assumed`);
      const sportsUnitSettings = ((sports.report || {}).settings || {});
      const sportsUnitSizing = sports.unit_sizing || {};
      const sportsUnitPct = Number(sportsUnitSizing.percentage ?? sportsUnitSettings.unit_size_pct ?? (data.settings || {}).SPORTS_UNIT_SIZE_PCT ?? 1);
      const sportsUnitBankroll = Number(sportsUnitSizing.bankroll_base ?? sports.equity_estimate ?? 0);
      const sportsUnitSize = Number(sportsUnitSizing.unit_size ?? sportsUnitSettings.unit_size ?? (sportsUnitBankroll * sportsUnitPct / 100));
      const sportsUnitCap = Number(sportsUnitSettings.unit_max_per_market || (data.settings || {}).SPORTS_UNIT_MAX_PER_MARKET || 5);
      setText('overview-sports-unit-size', money(sportsUnitSize));
      setText('sports-unit-size', money(sportsUnitSize));
      const sportsUnitPctInput = document.getElementById('SPORTS_UNIT_SIZE_PCT');
      const previewPct = sportsUnitPctInput && sportsUnitPctInput.dataset.dirty === 'true'
        ? Number(sportsUnitPctInput.value)
        : sportsUnitPct;
      renderSportsUnitSizePreview(
        sportsUnitBankroll,
        previewPct,
        sportsUnitSizing.snapshot_date || '',
        Boolean(sportsUnitSizing.locked_for_day),
        Number(sportsUnitSizing.current_equity ?? sports.equity_estimate ?? sportsUnitBankroll),
      );
      setText('overview-sports-unit-cap', `${sportsUnitCap}u`);
      setText('sports-unit-cap', `${sportsUnitCap}u`);
      const sportsLossRoom = Number(sportsCampaign.daily_loss_cap || 0) > 0
        ? money(sportsCampaign.daily_loss_remaining || 0)
        : 'Uncapped';
      setText('overview-sports-loss-room', sportsLossRoom);
      setText('sports-loss-room', sportsLossRoom);
      setText('overview-sports-status', sportsProcessLabel, `health-chip ${sportsRunning ? 'good' : 'bad'}`);
      setText('sports-status', sportsProcessLabel, `health-chip ${sportsRunning ? 'good' : 'bad'}`);
      renderSportsCampaignCards('overview-sports-campaign-cards', sportsCampaign);
      renderSportsCampaignCards('sports-campaign-cards', sportsCampaign);
      renderSportsUnitRequirements(sports, data.settings || {});

      const cryptoReport = crypto.report || {};
      const currentCryptoStrategy = cryptoReport.current_strategy_performance || {};
      for (const prefix of ['overview-crypto', 'crypto']) {
        setPnl(`${prefix}-daily`, crypto.bot_today_profit || 0);
        setPnl(`${prefix}-total`, crypto.bot_total_profit || 0);
        setPnl(`${prefix}-current-pnl`, currentCryptoStrategy.profit || 0);
        const currentSettled = Number(currentCryptoStrategy.settled_count || 0);
        const currentStatus = String(currentCryptoStrategy.status || 'collecting').replaceAll('_', ' ');
        const currentRecord = currentSettled
          ? `${currentSettled} (${Number(currentCryptoStrategy.wins || 0)}W-${Number(currentCryptoStrategy.losses || 0)}L)`
          : currentStatus.charAt(0).toUpperCase() + currentStatus.slice(1);
        setText(`${prefix}-current-record`, currentRecord);
        setText(`${prefix}-current-roi`, `${Number(currentCryptoStrategy.roi || 0).toFixed(2)}%`);
      }
      setPnl('crypto-today-pnl', crypto.bot_today_profit || 0);
      setPnl('crypto-pnl', crypto.bot_total_profit || 0);
      setText('overview-crypto-open', `${cryptoCampaign.bot_live_open_count ?? cryptoOpen.length} / ${cryptoCampaign.max_open || 1}`);
      setText('crypto-open-count', `${cryptoCampaign.bot_live_open_count ?? cryptoOpen.length} / ${cryptoCampaign.max_open || 1}`);
      const cryptoUnitEnabled = Boolean(cryptoCampaign.unit_staking_enabled);
      const cryptoUnitSize = Number(cryptoCampaign.unit_size || 0);
      const cryptoUnitMax = Math.max(1, Number(((crypto.settings || {}).CRYPTO_15M_UNIT_MAX_PER_MARKET) || 5));
      setText('overview-crypto-cycle', money(cryptoUnitSize));
      setText('crypto-cycle', money(cryptoUnitSize));
      setText('overview-crypto-goal', money(cryptoUnitSize * cryptoUnitMax));
      setText('crypto-goal-remaining', money(cryptoUnitSize * cryptoUnitMax));
      setText('overview-crypto-loss-room', money(cryptoCampaign.daily_loss_remaining || 0));
      setText('crypto-loss-room', money(cryptoCampaign.daily_loss_remaining || 0));
      setText('overview-crypto-status', cryptoProcessLabel, `health-chip ${cryptoRunning ? 'good' : 'bad'}`);
      setText('crypto-status', cryptoProcessLabel, `health-chip ${cryptoRunning ? 'good' : 'bad'}`);
      const delayedCryptoSettlements = ((crypto.live_reconciliation || {}).delayed_settlement_alerts || []);
      const delayedSettlementText = delayedCryptoSettlements.length
        ? `Delayed Kalshi settlement: ${delayedCryptoSettlements.map(row => `${row.asset || ''} ${row.ticker || ''} (${Number(row.delay_minutes || 0).toFixed(0)}m)`).join('; ')}. The position remains tracked and no outcome is assumed.`
        : '';
      for (const id of ['overview-crypto-settlement-alert', 'crypto-settlement-alert']) {
        const element = document.getElementById(id);
        if (!element) continue;
        element.textContent = delayedSettlementText;
        element.style.display = delayedSettlementText ? '' : 'none';
      }
      const networkOutage = crypto.network_outage || {};
      const networkOutageText = networkOutage.active
        ? `Kalshi connectivity outage: ${networkOutage.consecutive_failed_scans || 0} consecutive failed scans. The bot is failing closed and backing off to ${Number(networkOutage.next_scan_delay_seconds || 0) / 60} minutes; no live order can be submitted without fresh market data.`
        : '';
      for (const id of ['overview-crypto-network-alert', 'crypto-network-alert']) {
        const element = document.getElementById(id);
        if (!element) continue;
        element.textContent = networkOutageText;
        element.style.display = networkOutageText ? '' : 'none';
      }
      renderCryptoCampaignCards('overview-crypto-campaign-cards', cryptoCampaign);
      renderCryptoCampaignCards('crypto-campaign-cards', cryptoCampaign);

      renderRows('overview-sports-bets', sportsOpen, [
        {num: true, render: row => campaignBotNumber(row)},
        {render: row => row.game || `${row.away_team || ''} @ ${row.home_team || ''}`},
        {render: row => `${row.selected_team || row.selection || ''} ${row.market_type || ''} ${row.line ?? row.market_line ?? ''}`},
        {num: true, render: row => money(row.stake || 0)},
        {num: true, render: row => row.entry_price === undefined ? '' : `${row.entry_price}c`},
        {num: true, render: row => `${Number(row.net_edge ?? row.edge ?? 0).toFixed(2)}%`},
        {num: true, render: row => Number(row.confidence ?? row.confidence_score ?? 0).toFixed(1)}
      ], 'No automated sports bet is open. The bot is scanning live games.');
      renderRows('overview-crypto-bets', cryptoOpen, [
        {num: true, render: row => cryptoCampaignBotNumber(row)},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => money(row.stake || 0)},
        {num: true, render: row => row.entry_price === undefined ? '' : `${row.entry_price}c`},
        {num: true, render: row => `${Number(row.net_edge ?? row.edge ?? 0).toFixed(2)}%`},
        {num: true, render: row => `${cryptoWinProbability(row).toFixed(1)}%`},
        {num: true, render: row => cryptoWinProbabilityInterval(row)},
        {num: true, render: row => `${cryptoEdgeCertainty(row).toFixed(1)}%`}
      ], 'No automated crypto bet is open. The bot is scanning 15-minute markets.');

      const sportsSettings = (sports.report || {}).settings || {};
      const cryptoSettings = crypto.settings || {};
      const sportsBotCount = sportsCampaign.configured_bot_count || sportsSettings.live_campaign_bot_count || sportsBots.length || 1;
      const perBotLossCap = (sportsBots[0] || {}).daily_loss_cap || sportsSettings.live_campaign_effective_daily_loss_cap || 75;
      const sportsSummary = `Live moneyline + spread · ${sportsBotCount} isolated campaign bot${sportsBotCount === 1 ? '' : 's'} · one active slot per bot · calibrated same-book consensus · ${sportsSettings.live_campaign_min_price_cents || 25}-${sportsSettings.live_campaign_max_price_cents || 60}c · fee-adjusted edge ${sportsSettings.live_campaign_min_edge || 0.01}%+ · +${sportsSettings.live_campaign_assumed_loss_american_odds || 400} assumed-loss rollover · ${money(perBotLossCap)} loss ceiling per bot`;
      const actionGate = cryptoReport.crypto_15m_action_gate || {
        enabled: String(cryptoSettings.CRYPTO_15M_SIMPLE_ACTION_GATE_ENABLED || 'true').toLowerCase() === 'true',
        minimum_edge: Number(cryptoSettings.CRYPTO_15M_SIMPLE_MIN_EDGE || 2),
        minimum_confidence: Number(cryptoSettings.CRYPTO_15M_SIMPLE_MIN_CONFIDENCE || 67),
        minimum_price_cents: Number(cryptoSettings.CRYPTO_15M_SIMPLE_MIN_PRICE_CENTS || 35),
        maximum_price_cents: Number(cryptoSettings.CRYPTO_15M_SIMPLE_MAX_PRICE_CENTS || 70),
        minimum_minutes_remaining: Number(cryptoSettings.CRYPTO_15M_SIMPLE_MIN_MINUTES_REMAINING || 1),
        maximum_minutes_remaining: Number(cryptoSettings.CRYPTO_15M_SIMPLE_MAX_MINUTES_REMAINING || 15),
        maximum_same_expiry_open: Number(cryptoSettings.CRYPTO_15M_SIMPLE_MAX_SAME_EXPIRY_OPEN || 2),
        directional_confirmation_required: String(cryptoSettings.CRYPTO_15M_DIRECTIONAL_CONFIRMATION_REQUIRED || 'true').toLowerCase() === 'true',
        directional_confirmation_minimum_sources: Number(cryptoSettings.CRYPTO_15M_DIRECTIONAL_CONFIRMATION_MIN_SOURCES || 2)
      };
      const unitRecoveryEnabled = String(cryptoSettings.CRYPTO_15M_UNIT_RECOVERY_ENABLED || 'false').toLowerCase() === 'true';
      const pooledRecoveryEnabled = String(cryptoSettings.CRYPTO_15M_POOLED_RECOVERY_ENABLED || 'true').toLowerCase() === 'true';
      const recoveryMaxPerExpiry = Number(cryptoSettings.CRYPTO_15M_RECOVERY_MAX_OVERLAYS_PER_EXPIRY || 1);
      const unitRecoveryMinBase = Number(cryptoSettings.CRYPTO_15M_UNIT_RECOVERY_MIN_BASE_UNITS || 3);
      const unitRecoveryMinProbability = Number(cryptoSettings.CRYPTO_15M_UNIT_RECOVERY_MIN_PROBABILITY_UNITS || 3);
      const unitRecoveryTargetPct = Number(cryptoSettings.CRYPTO_15M_UNIT_RECOVERY_TARGET_DRAWDOWN_FRACTION || 0.33) * 100;
      const unitRecoveryMaxBonus = Number(cryptoSettings.CRYPTO_15M_UNIT_RECOVERY_MAX_BONUS_UNITS || 2);
      const unitRecoveryMaxTotal = Number(cryptoSettings.CRYPTO_15M_UNIT_RECOVERY_MAX_TOTAL_UNITS || 5);
      const commodityLive = String(cryptoSettings.MULTI_MARKET_COMMODITY_LIVE_ENABLED || 'true').toLowerCase() === 'true';
      const cryptoUnitPct = Number(cryptoSettings.CRYPTO_15M_UNIT_SIZE_PCT || 0.75);
      const cryptoUnitLadder = [0.5, 1, 2, 3, 4, 5].filter(units => units <= cryptoUnitMax).map(units => {
        const key = units === 0.5 ? 'HALF' : units;
        const probability = Number(cryptoSettings[`CRYPTO_15M_UNIT_${key}_MIN_WIN_PROB`] || 0);
        const probabilityLow = Number(cryptoSettings[`CRYPTO_15M_UNIT_${key}_MIN_WIN_PROB_LOW`] || 0);
        const edgeCertainty = Number(cryptoSettings[`CRYPTO_15M_UNIT_${key}_MIN_CONFIDENCE`] || 0);
        const edge = Number(cryptoSettings[`CRYPTO_15M_UNIT_${key}_MIN_EDGE`] || 0);
        const edgeLow = Number(cryptoSettings[`CRYPTO_15M_UNIT_${key}_MIN_EDGE_LOW`] || 0);
        return `${units}u: p ${fmtSetting(probability)}%/${fmtSetting(probabilityLow)}% low · edge certainty ${fmtSetting(edgeCertainty)}% · ${fmtSetting(edge, 'c')}/${fmtSetting(edgeLow, 'c')} low`;
      }).join(' · ');
      setText('overview-sports-strategy', sportsSummary, 'strategy-summary');
      setText('sports-strategy-summary', sportsSummary, 'strategy-summary');
      const cryptoBotCount = cryptoCampaign.configured_bot_count || cryptoSettings.CRYPTO_15M_CAMPAIGN_BOT_COUNT || cryptoBots.length || 1;
      const cryptoPerBotLossCap = (cryptoBots[0] || {}).daily_loss_cap || cryptoSettings.CRYPTO_15M_DAILY_LOSS_CAP || 300;
      const scanBetLimit = Number(cryptoReport.effective_bets_per_scan || cryptoReport.max_bets_per_scan || Math.min(5, cryptoBotCount));
      const directionValue = actionGate.directional_confirmation_required
        ? `${fmtSetting(actionGate.directional_confirmation_minimum_sources)}+ sources · matching flow · ${fmtSetting(actionGate.directional_confirmation_minimum_strength || 0.10)}+ strength`
        : 'Not required';
      const cryptoRequirementsHtml = `
        <div class="strategy-requirements">
          <div class="strategy-requirements-title">${cryptoUnitEnabled ? 'Probability/Edge Unit Gate' : '15-Minute Action Gate'} · Crypto Live</div>
          <div class="strategy-requirements-grid">
            <div class="strategy-requirement"><span>Entry price</span><strong>${fmtSetting(actionGate.minimum_price_cents, 'c')}–${fmtSetting(actionGate.maximum_price_cents, 'c')}</strong></div>
            <div class="strategy-requirement"><span>Net edge</span><strong>${fmtSetting(actionGate.minimum_edge, '%')}+</strong></div>
            <div class="strategy-requirement"><span>Edge certainty</span><strong>${fmtSetting(actionGate.minimum_confidence)}%+</strong></div>
            <div class="strategy-requirement"><span>Time remaining</span><strong>${fmtSetting(actionGate.minimum_minutes_remaining)}–${fmtSetting(actionGate.maximum_minutes_remaining)} min</strong></div>
            <div class="strategy-requirement"><span>Direction</span><strong>${esc(directionValue)}</strong></div>
            <div class="strategy-requirement"><span>Same expiry</span><strong>${fmtSetting(actionGate.maximum_same_expiry_open)} max</strong></div>
          </div>
          <div class="strategy-requirements-row"><strong>Execution:</strong> fresh quote + full depth · up to ${fmtSetting(cryptoSettings.CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS || 6, 'c')} adverse slippage immediately · larger moves wait ${fmtSetting(cryptoSettings.CRYPTO_LIVE_SLIPPAGE_RECONFIRM_SECONDS || 15)} seconds for a second quote within ${fmtSetting(cryptoSettings.CRYPTO_LIVE_SLIPPAGE_RECONFIRM_MAX_MOVE_CENTS || 2, 'c')}, then rerun every gate at base size only · fill-or-kill · when full fresh depth is unavailable, ${onOff(cryptoSettings.CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_ENABLED || 'true')} largest qualifying whole-unit downshift (minimum ${fmtSetting(cryptoSettings.CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_MIN_UNITS || 1)}u) with full repricing and risk revalidation · GOLD, SILVER &amp; WTI are ${commodityLive ? 'LIVE' : 'SHADOW ONLY (no orders)'}.</div>
          <div class="strategy-requirements-row"><strong>Continuous unit sizing:</strong> ${fmtSetting(cryptoUnitPct, '%')} of locked daily bankroll = ${money(cryptoUnitSize)} per unit; 0.5u-${fmtSetting(cryptoUnitMax)}u; final size is the minimum allowed by calibrated win probability and its conservative interval bound, conservative edge, fractional Kelly, live depth, and the ${fmtSetting(cryptoSettings.CRYPTO_15M_GLOBAL_DAILY_LOSS_UNITS || 6)}u global daily risk budget. ${esc(cryptoUnitLadder)}; no profit target or cycle stop · ${fmtSetting(cryptoBotCount)} isolated bots sharing one scan · up to ${fmtSetting(scanBetLimit)} qualifying bets per scan · one active bet per bot · ${fmtSetting(actionGate.maximum_same_expiry_open)} same-expiry max.</div>
          <div class="strategy-requirements-row"><strong>Bounded Recovery:</strong> ${cryptoUnitEnabled && unitRecoveryEnabled ? `bounded pooled recovery only for ${fmtSetting(unitRecoveryMinBase)}u+ base signals that independently qualify for the ${fmtSetting(unitRecoveryMinProbability)}u+ probability tier; targets ${fmtSetting(unitRecoveryTargetPct, '%')} of campaign drawdown; adds at most ${fmtSetting(unitRecoveryMaxBonus)}u and never exceeds the ${fmtSetting(unitRecoveryMaxTotal)}u combined, probability, Kelly, liquidity, execution, or portfolio cap; ${pooledRecoveryEnabled ? `${fmtSetting(recoveryMaxPerExpiry)} enhanced position per expiry with open-profit reservation` : 'pooled ledger disabled'}` : 'new recovery bonus stakes disabled; pooled high-water accounting and counterfactual analytics remain active'} · no loss-chasing size escalation · learned model remains shadow-only.</div>
        </div>`;
      ['overview-crypto-strategy', 'crypto-strategy-summary'].forEach(id => {
        const node = document.getElementById(id);
        if (node) {
          node.className = 'strategy-summary';
          node.innerHTML = cryptoRequirementsHtml;
        }
      });
    }

    function renderShadowLab(prefix, shadow) {
      const data = shadow || {};
      const active = data.mode === 'shadow_only';
      setText(`${prefix}-shadow-mode`, active ? 'SHADOW' : String(data.mode || 'WAITING').toUpperCase(), `value ${active ? 'shadow-value' : ''}`);
      setText(`${prefix}-shadow-raw`, Number(data.raw_positive_ev_count || 0));
      setText(`${prefix}-shadow-fee-positive`, Number(data.fee_positive_ev_count || 0));
      setText(`${prefix}-shadow-threshold`, Number(data.net_threshold_pass_count || 0));
      setText(`${prefix}-shadow-approved`, Number(data.correlation_approved_count || 0), `value ${Number(data.correlation_approved_count || 0) ? 'positive' : ''}`);
      setText(`${prefix}-shadow-stake`, money(data.shadow_recommended_stake || 0), 'value shadow-value');
      const outcomes = data.outcomes || {};
      const approvedOutcomes = outcomes.approved || {};
      const filteredOutcomes = outcomes.filtered || {};
      setText(
        `${prefix}-shadow-outcomes`,
        `Tracked: ${Number(outcomes.tracked_open || 0)} open · ${Number(outcomes.tracked_settled || 0)} settled · approved ${Number(approvedOutcomes.wins || 0)}W/${Number(approvedOutcomes.losses || 0)}L (${money(approvedOutcomes.profit || 0)}, ${Number(approvedOutcomes.roi_pct || 0).toFixed(1)}% ROI) · filtered ${Number(filteredOutcomes.wins || 0)}W/${Number(filteredOutcomes.losses || 0)}L (${money(filteredOutcomes.profit || 0)})`,
        'shadow-note'
      );
      renderRows(`${prefix}-shadow-opportunities`, (data.top_opportunities || []).slice(0, 20), [
        {render: row => `${row.ticker || ''}${row.label ? ' · ' + row.label : ''}`},
        {render: row => row.group || ''},
        {num: true, render: row => row.entry_price === undefined || row.entry_price === null ? '' : `${Number(row.entry_price).toFixed(1)}c`},
        {num: true, render: row => `${Number(row.raw_edge_pp || 0).toFixed(2)}%`},
        {num: true, render: row => `${Number(row.net_edge_pp || 0).toFixed(2)}%`},
        {num: true, render: row => `${Number(row.fee_drag_pp || 0).toFixed(2)}%`},
        {num: true, render: row => money(row.fee_aware_kelly_stake || 0)},
        {num: true, render: row => money(row.recommended_stake || 0)},
        {render: row => row.decision === 'shadow_approved' ? 'approved' : ((row.reasons || []).join(', ') || 'skip')}
      ], 'No shadow candidates have been evaluated yet. The next bot scan will populate this table.');
      renderCountTable(`${prefix}-shadow-reasons`, data.reason_counts || [], 'No shadow filters fired in the latest scan.');
    }

    function normalizedTrendDirection(value) {
      const direction = String(value || 'neutral').trim().toLowerCase();
      if (['up', 'bullish', 'higher', 'rise', 'rising'].includes(direction)) return 'up';
      if (['down', 'bearish', 'lower', 'fall', 'falling'].includes(direction)) return 'down';
      return 'neutral';
    }

    function trendPillLabel(value) {
      const direction = normalizedTrendDirection(value);
      if (direction === 'up') return '\u2191 UP';
      if (direction === 'down') return '\u2193 DOWN';
      return '\u2014 NEUTRAL';
    }

    function setTrendSignal(pillId, cardId, value) {
      const direction = normalizedTrendDirection(value);
      setText(pillId, trendPillLabel(direction), `trend-pill ${direction}`);
      const card = document.getElementById(cardId);
      if (card) card.className = `regime-signal-card ${direction}`;
    }

    function trendPillHtml(value) {
      const direction = normalizedTrendDirection(value);
      return `<span class="trend-pill compact ${direction}">${trendPillLabel(direction)}</span>`;
    }

    function renderCryptoMarketRegime(data) {
      data = data || {};
      const latest = data.latest || {};
      const global = latest.global || {};
      const news = (latest.news || {}).global || {};
      const ai = latest.ai_shadow || {};
      const evaluation = data.evaluation || {};
      const forecastValidation = data.forecast_validation || {};
      const validationHorizons = forecastValidation.horizons || {};
      const validation1h = validationHorizons['1h'] || {};
      const liveActive = Boolean(data.affects_execution);
      for (const id of ['crypto-market-trend-card', 'crypto-market-regime-lab']) {
        const panel = document.getElementById(id);
        if (panel) panel.classList.toggle('regime-live', liveActive);
      }
      const direction1h = global.direction_1h || global.direction || 'neutral';
      const direction4h = global.direction_4h || global.direction || 'neutral';
      const direction24h = global.direction_24h || 'neutral';
      setTrendSignal('crypto-regime-1h-pill', 'crypto-regime-1h-card', direction1h);
      setTrendSignal('crypto-regime-4h-pill', 'crypto-regime-4h-card', direction4h);
      setTrendSignal('crypto-regime-24h-pill', 'crypto-regime-24h-card', direction24h);
      setTrendSignal('crypto-overview-regime-1h-pill', 'crypto-overview-regime-1h-card', direction1h);
      setTrendSignal('crypto-overview-regime-4h-pill', 'crypto-overview-regime-4h-card', direction4h);
      setTrendSignal('crypto-overview-regime-24h-pill', 'crypto-overview-regime-24h-card', direction24h);
      const horizonDetail = (key, score, probability) => {
        const validation = validationHorizons[key] || {};
        const windows = Number(validation.independent_windows || 0);
        const accuracy = validation.directional_accuracy;
        const accuracyText = accuracy == null ? 'accuracy collecting' : `${(Number(accuracy) * 100).toFixed(1)}% forward accuracy`;
        return `P(up) ${(Number(probability ?? 0.5) * 100).toFixed(1)}% · score ${Number(score || 0).toFixed(3)} · ${accuracyText} · ${windows}/100 windows`;
      };
      const detail1h = horizonDetail('1h', global.score_1h, global.probability_up_1h);
      const detail4h = horizonDetail('4h', global.score_4h, global.probability_up_4h);
      const detail24h = horizonDetail('24h', global.score_24h, global.probability_up_24h);
      setText('crypto-regime-1h-detail', detail1h);
      setText('crypto-regime-4h-detail', detail4h);
      setText('crypto-regime-24h-detail', detail24h);
      setText('crypto-overview-regime-1h-detail', detail1h);
      setText('crypto-overview-regime-4h-detail', detail4h);
      setText('crypto-overview-regime-24h-detail', detail24h);
      setText('crypto-regime-freshness', latest.generated_at ? `Updated ${shortTime(latest.generated_at)}` : 'Waiting for refresh', `health-chip ${latest.generated_at ? 'good' : 'warn'}`);
      setText('crypto-regime-news-health', news.status === 'ok' ? `News live \u00b7 ${Number(news.count || 0)}` : `News ${String(news.status || 'waiting')}`, `health-chip ${news.status === 'ok' ? 'good' : 'warn'}`);
      setText('crypto-regime-ai-health', ai.status === 'ok' ? `AI live \u00b7 ${Number(((ai.asset_coverage || {}).returned) || 0)}/8` : `AI ${String(ai.status || 'waiting')}`, `health-chip ${ai.status === 'ok' ? 'good' : 'warn'}`);
      const liveCap = Number(data.maximum_live_adjustment_pp || 0);
      const executionLabel = liveActive ? `Live signal \u00b7 \u00b1${liveCap.toFixed(2)}pp max` : 'Shadow only \u00b7 no bet impact';
      setText('crypto-regime-execution-health', executionLabel, 'health-chip good');
      setText('crypto-overview-regime-freshness', latest.generated_at ? `Updated ${shortTime(latest.generated_at)}` : 'Waiting for refresh', `health-chip ${latest.generated_at ? 'good' : 'warn'}`);
      setText('crypto-overview-regime-news-health', news.status === 'ok' ? `News live \u00b7 ${Number(news.count || 0)}` : `News ${String(news.status || 'waiting')}`, `health-chip ${news.status === 'ok' ? 'good' : 'warn'}`);
      setText('crypto-overview-regime-ai-health', ai.status === 'ok' ? `AI live \u00b7 ${Number(((ai.asset_coverage || {}).returned) || 0)}/8` : `AI ${String(ai.status || 'waiting')}`, `health-chip ${ai.status === 'ok' ? 'good' : 'warn'}`);
      setText('crypto-overview-regime-execution-health', executionLabel, 'health-chip good');
      setText('crypto-regime-mode', liveActive ? 'LIVE BOUNDED' : (data.mode === 'shadow_only' ? 'SHADOW' : String(data.mode || 'WAITING').toUpperCase()), `value ${liveActive ? 'positive' : (data.mode === 'shadow_only' ? 'shadow-value' : '')}`);
      setText('crypto-regime-direction', `${String(direction1h).toUpperCase()} / ${String(direction4h).toUpperCase()} / ${String(direction24h).toUpperCase()}`);
      setText('crypto-regime-state', String(global.regime || 'waiting').replaceAll('_', ' '));
      setText('crypto-regime-breadth', `${(Number(global.breadth || 0) * 100).toFixed(0)}%`);
      setText('crypto-regime-news', String(news.risk_level || 'normal').toUpperCase());
      setText('crypto-regime-ai', ai.enabled ? `${String((ai.global || {}).direction_1h || ai.status || 'waiting').toUpperCase()} / ${String((ai.global || {}).direction_4h || '—').toUpperCase()} / ${String((ai.global || {}).direction_24h || '—').toUpperCase()} · ${ai.model || 'local'}` : 'DISABLED');
      setText('crypto-regime-settled', `${Number(validation1h.independent_windows || 0)} / ${Number(validation1h.minimum_independent_windows || 100)}`);
      const forecastBrier = validation1h.brier_score;
      setText('crypto-regime-brier', forecastBrier == null ? 'Collecting' : Number(forecastBrier).toFixed(4));
      setText('crypto-regime-updated', latest.generated_at
        ? `Updated ${shortTime(latest.generated_at)} · ${Number(data.annotated_candidates || 0)} candidates annotated · maximum hypothetical adjustment ±${Number(data.maximum_adjustment_pp || 0).toFixed(1)} percentage points · execution impact: none`
        : 'Waiting for the next five-minute regime refresh.', 'shadow-note');
      if (latest.generated_at && liveActive) {
        setText('crypto-regime-updated', `Updated ${shortTime(latest.generated_at)} \u00b7 ${Number(data.annotated_candidates || 0)} candidates annotated \u00b7 ${Number(data.live_adjusted_candidates || 0)} live-adjusted \u00b7 live cap \u00b1${liveCap.toFixed(2)} percentage points`, 'shadow-note');
      }
      const preferred = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'GOLD', 'SILVER', 'WTI'];
      const assets = latest.assets || {};
      const rows = preferred.filter(asset => assets[asset]).map(asset => ({asset, ...(assets[asset] || {})}));
      renderRows('crypto-regime-assets', rows, [
        {render: row => row.asset},
        {html: true, render: row => trendPillHtml(row.direction_1h || 'neutral')},
        {html: true, render: row => trendPillHtml(row.direction_4h || 'neutral')},
        {html: true, render: row => trendPillHtml(row.direction_24h || 'neutral')},
        {num: true, render: row => `${(Number(row.probability_up_1h ?? 0.5) * 100).toFixed(1)}%`},
        {num: true, render: row => `${(Number(row.probability_up_4h ?? 0.5) * 100).toFixed(1)}%`},
        {num: true, render: row => `${(Number(row.probability_up_24h ?? 0.5) * 100).toFixed(1)}%`},
        {num: true, render: row => `${(Number((((row.forecast_horizons || {})['1h'] || {}).data_quality) || row.reliability || 0) * 100).toFixed(0)}%`}
      ], 'No regime snapshot yet. The next crypto scan will populate this table.');
    }

    function renderCryptoLowEdgeLivePilot(data) {
      data = data || {};
      const config = data.configuration || {};
      const settled = Number(data.settled || 0);
      const maximum = Number(data.maximum_settled || 50);
      const profit = Number(data.profit || 0);
      const roi = Number(data.roi || 0);
      setText(
        'crypto-live-pilot-status',
        data.sample_complete ? 'Review due' : (data.enabled ? 'Active' : 'Disabled'),
        `value ${data.enabled && !data.sample_complete ? 'positive' : ''}`
      );
      setText('crypto-live-pilot-progress', `${settled} / ${maximum || '∞'}`);
      setText('crypto-live-pilot-open', `${Number(data.open || 0)} / ${Number(config.maximum_open || 1)}`);
      setText('crypto-live-pilot-record', `${Number(data.wins || 0)}W / ${Number(data.losses || 0)}L`);
      setText('crypto-live-pilot-win-rate', `${Number(data.win_rate || 0).toFixed(1)}%`);
      setText('crypto-live-pilot-loss-streak', Number(data.maximum_loss_streak || 0));
      setText('crypto-live-pilot-profit', money(profit), `value ${profit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-live-pilot-roi', `${roi.toFixed(1)}%`, `value ${roi >= 0 ? 'positive' : 'negative'}`);
      renderRows('crypto-live-pilot-recent', data.recent_records || [], [
        {render: row => shortTime(row.settled_at || '')},
        {render: row => `B${Number(row.bot_number || 1)} · ${row.asset || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => `${Number(row.entry_price || 0).toFixed(1)}c`},
        {num: true, render: row => `${Number(row.edge || 0).toFixed(2)}%`},
        {num: true, render: row => `${Number(row.confidence || 0).toFixed(1)}%`},
        {render: row => row.result || ''},
        {num: true, render: row => money(row.stake || 0)},
        {num: true, render: row => money(row.profit || 0)}
      ], 'No low-edge live pilot bets have settled yet.');
    }

    function renderCryptoBtc15mSprint(data) {
      data = data || {};
      const status = String(data.status || 'WAITING').toUpperCase();
      const primary = data.primary || {};
      const config = data.configuration || {};
      const freeze = data.execution_freeze || {};
      const firstHalf = data.first_half || {};
      const secondHalf = data.second_half || {};
      const reservedProfit = Number(primary.reserved_profit || 0);
      const reservedRoi = Number(primary.reserved_roi || 0);
      const statusGood = ['COLLECTING', 'FINALIZING', 'PASS'].includes(status);
      setText('crypto-sprint-status', status, `health-chip ${statusGood ? 'good' : (status === 'FAIL' ? 'bad' : 'warn')}`);
      setText('crypto-sprint-decision', status, `value ${status === 'PASS' ? 'positive' : (status === 'FAIL' ? 'negative' : '')}`);
      setText(
        'crypto-sprint-freeze',
        freeze.healthy_intentional_state ? 'FROZEN · HEALTHY' : (freeze.active ? 'FROZEN · CHECK SETTINGS' : 'NOT FROZEN'),
        `value ${freeze.healthy_intentional_state ? 'positive' : 'negative'}`
      );
      setText('crypto-sprint-progress', `${Number(primary.settled || 0)} / ${Number(config.minimum_forward_markets || 60)}`);
      setText('crypto-sprint-ends', data.prospective_ends_at ? shortTime(data.prospective_ends_at) : 'Waiting');
      setText('crypto-sprint-record', `${Number(primary.wins || 0)}W / ${Number(primary.losses || 0)}L`);
      setText('crypto-sprint-profit', money(reservedProfit), `value ${reservedProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-sprint-roi', `${reservedRoi.toFixed(1)}%`, `value ${reservedRoi >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-sprint-break-even', `${Number(primary.win_rate || 0).toFixed(1)}% / ${Number(primary.reserved_break_even_win_rate || 0).toFixed(1)}%`);
      const lower = primary.one_sided_90_lower_bound_profit;
      setText(
        'crypto-sprint-lower-bound',
        lower === null || lower === undefined ? 'Collecting' : money(lower),
        `value ${Number(lower || 0) > 0 ? 'positive' : 'negative'}`
      );
      setText('crypto-sprint-halves', `${money(firstHalf.reserved_profit || 0)} / ${money(secondHalf.reserved_profit || 0)}`);
      setText('crypto-sprint-drawdown', money(primary.maximum_drawdown || 0));
      const development = data.development || {};
      setText('crypto-sprint-development', `${Number(development.settled || 0)} markets · ${Number(development.reserved_roi || 0).toFixed(1)}% · development only`);
      const funnel = data.candidate_funnel || {};
      const reasons = Object.entries(funnel.rejection_reasons || {}).slice(0, 6)
        .map(([label, count]) => `${String(label).replaceAll('_', ' ')}: ${Number(count || 0)}`)
        .join(' · ');
      const hypothesisText = (data.hypotheses || [])
        .map(row => `${String(row.lane || '').replaceAll('_', ' ')} ${Number(row.settled || 0)}/${Number(row.minimum_independent_markets || 50)} (${String(row.role || '').replaceAll('_', ' ')})`)
        .join(' · ');
      setText(
        'crypto-sprint-funnel',
        `Latest scan: ${Number(funnel.reviewed || 0)} reviewed · ${Number(funnel.captured || 0)} records captured${reasons ? ' · primary misses: ' + reasons : ''}${hypothesisText ? ' · preregistered: ' + hypothesisText : ''} · policy ${config.policy_hash || 'not locked'}`,
        'shadow-note'
      );
      renderRows('crypto-sprint-lanes', data.lanes || [], [
        {render: row => String(row.lane || '').replaceAll('_', ' ')},
        {num: true, render: row => `${Number(row.settled || 0)} / ${Number(row.tracked || 0)}`},
        {num: true, render: row => `${Number(row.wins || 0)}W / ${Number(row.losses || 0)}L`},
        {num: true, render: row => `${Number(row.win_rate || 0).toFixed(1)}%`},
        {num: true, render: row => `${Number(row.reserved_break_even_win_rate || 0).toFixed(1)}%`},
        {num: true, render: row => money(row.reserved_profit || 0)},
        {num: true, render: row => `${Number(row.reserved_roi || 0).toFixed(1)}%`},
        {render: row => row.eligible_for_promotion ? 'Readiness only' : 'No'}
      ], 'No sprint lanes have captured a qualifying quote yet.');
      renderRows('crypto-sprint-controls', data.controls_by_asset || [], [
        {render: row => row.asset || ''},
        {num: true, render: row => Number(row.settled || 0)},
        {num: true, render: row => `${Number(row.wins || 0)}W / ${Number(row.losses || 0)}L`},
        {num: true, render: row => money(row.reserved_profit || 0)},
        {num: true, render: row => `${Number(row.reserved_roi || 0).toFixed(1)}%`}
      ], 'No non-BTC flow-veto controls have settled yet.');
      renderRows('crypto-sprint-recent', (data.recent_records || []).slice(0, 40), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => String(row.lane || '').replaceAll('_', ' ')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => `${Number(row.entry_price_cents || 0).toFixed(1)}c`},
        {num: true, render: row => row.reserved_edge_cents === null || row.reserved_edge_cents === undefined ? '—' : `${Number(row.reserved_edge_cents).toFixed(2)}c`},
        {render: row => row.result || 'OPEN'},
        {num: true, render: row => row.status === 'settled' ? money(row.reserved_profit || 0) : '—'}
      ], 'No sprint records yet.');
    }

    function renderCryptoLowEdgeShadow(data) {
      data = data || {};
      const profit = Number(data.virtual_profit || 0);
      setText('crypto-low-edge-mode', data.mode === 'paper_shadow_only' ? 'Paper only' : (data.mode || 'Waiting'));
      setText('crypto-low-edge-tracked', Number(data.tracked || 0));
      setText('crypto-low-edge-open', Number(data.open || 0));
      setText('crypto-low-edge-settled', Number(data.settled || 0));
      setText('crypto-low-edge-record', `${Number(data.wins || 0)}W / ${Number(data.losses || 0)}L`);
      setText('crypto-low-edge-profit', money(profit), `value ${profit >= 0 ? 'positive' : 'negative'}`);
      const funnel = data.candidate_funnel || {};
      const rejectionText = (funnel.quality_rejection_reasons || [])
        .map(row => `${row.label}: ${Number(row.count || 0)}`)
        .join(' · ');
      setText(
        'crypto-low-edge-funnel',
        `Latest scan: ${Number(funnel.targeted_this_scan || 0)} price/time/confidence matches · ${Number(funnel.quality_approved_this_scan || 0)} passed safeguards · ${Number(funnel.captured_this_scan || 0)} new records${rejectionText ? ' · rejected: ' + rejectionText : ''}`,
        'shadow-note'
      );
      renderRows('crypto-low-edge-cohorts', data.cohorts || [], [
        {render: row => row.cohort || ''},
        {num: true, render: row => Number(row.tracked || 0)},
        {num: true, render: row => Number(row.open || 0)},
        {num: true, render: row => `${Number(row.wins || 0)}W / ${Number(row.losses || 0)}L`},
        {num: true, render: row => `${Number(row.win_rate || 0).toFixed(1)}%`},
        {num: true, render: row => row.settled ? `${Number(row.average_confidence || 0).toFixed(1)}%` : '—'},
        {num: true, render: row => money(row.virtual_profit || 0)},
        {num: true, render: row => `${Number(row.roi || 0).toFixed(1)}%`}
      ], 'Waiting for qualifying 1-3% edge candidates.');
      renderRows('crypto-low-edge-recent', (data.recent_records || []).slice(0, 30), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => `${Number(row.entry_price || 0).toFixed(1)}c`},
        {num: true, render: row => `${Number(row.net_edge || 0).toFixed(2)}%`},
        {num: true, render: row => `${Number(row.confidence || 0).toFixed(1)}%`},
        {render: row => row.result || 'OPEN'},
        {num: true, render: row => row.status === 'settled' ? money(row.virtual_profit || 0) : '—'}
      ], 'No low-edge shadow records yet.');
    }

    function renderCryptoSettlementLagShadow(data) {
      data = data || {};
      const health = data.health || {};
      const evaluation = data.evaluation || {};
      const status = String(health.status || data.mode || 'waiting').replaceAll('_', ' ').toUpperCase();
      const healthy = ['collecting_final_minute', 'waiting_for_final_minute'].includes(String(health.status || ''));
      setText('crypto-settlement-lag-status', status, `health-chip ${healthy ? 'good' : (health.status === 'error' ? 'bad' : 'warn')}`);
      setText('crypto-settlement-lag-sample', `${Number(data.independent_markets || 0)} / ${Number(evaluation.minimum_independent_markets || 50)}`);
      setText('crypto-settlement-lag-record', `${Number(data.wins || 0)}W / ${Number(data.losses || 0)}L`);
      setText('crypto-settlement-lag-arms', `${Number(data.policy_arm_settled || 0)} · ${Number(data.policy_arm_wins || 0)}W / ${Number(data.policy_arm_losses || 0)}L`);
      const observations = data.observations || {};
      setText('crypto-settlement-lag-observations', `${Number(observations.tracked || 0)} / ${Number(observations.settled || 0)} settled`);
      const holdProfit = Number(data.hold_profit || 0);
      const exitProfit = Number(data.exit_policy_profit || 0);
      setText('crypto-settlement-lag-hold', `${money(holdProfit)} / ${Number(data.hold_roi || 0).toFixed(1)}%`, `value ${holdProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-settlement-lag-days', `${Number(data.observation_days || 0)} / ${Number(evaluation.minimum_observation_days || 30)}`);
      const holdFirst = Number(data.hold_chronological_halves?.first?.profit || 0);
      const holdSecond = Number(data.hold_chronological_halves?.second?.profit || 0);
      setText(
        'crypto-settlement-lag-halves',
        Number(data.settled || 0) < 2 ? 'Collecting' : `${money(holdFirst)} / ${money(holdSecond)}`,
        `value ${(holdFirst > 0 && holdSecond > 0) ? 'positive' : 'negative'}`
      );
      const holdDayLower = data.hold_day_cluster_inference?.one_sided_90_lower;
      setText(
        'crypto-settlement-lag-day-lower',
        holdDayLower === null || holdDayLower === undefined ? 'Collecting' : money(holdDayLower),
        `value ${Number(holdDayLower || 0) > 0 ? 'positive' : 'negative'}`
      );
      const fok = data.reconfirmed_fok || {};
      const fokProfit = Number(fok.profit || 0);
      setText(
        'crypto-settlement-lag-fok',
        `${Number(fok.settled || 0)} / ${Number(evaluation.minimum_independent_markets || 50)} · ${money(fokProfit)} / ${Number(fok.roi || 0).toFixed(1)}%`,
        `value ${fokProfit > 0 ? 'positive' : (Number(fok.settled || 0) ? 'negative' : '')}`
      );
      const fokDayLower = fok.day_cluster_inference?.one_sided_90_lower;
      setText(
        'crypto-settlement-lag-fok-lower',
        fokDayLower === null || fokDayLower === undefined ? 'Collecting' : money(fokDayLower),
        `value ${Number(fokDayLower || 0) > 0 ? 'positive' : 'negative'}`
      );
      setText('crypto-settlement-lag-exit', `${money(exitProfit)} / ${Number(data.exit_policy_roi || 0).toFixed(1)}%`, `value ${exitProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-settlement-lag-calibration', Number(data.calibration_residual_samples || 0));
      setText('crypto-settlement-lag-brier', data.average_brier === null || data.average_brier === undefined ? 'Collecting' : Number(data.average_brier).toFixed(4));
      setText('crypto-settlement-lag-invalid', Number(data.diagnostic_invalid_records || 0));
      const current = data.current_window || {};
      const probability = current.probability || {};
      const selected = current.selected || {};
      const reasons = current.reasons || [];
      const currentText = current.ticker
        ? `BRTI k=${Number(current.reading || 0)}/60 · ${current.ticker} · index ${Number(current.brti_value || 0).toFixed(2)} · accumulated avg ${Number(current.observed_average || 0).toFixed(2)} · required remaining avg ${probability.required_remaining_average === null || probability.required_remaining_average === undefined ? '—' : Number(probability.required_remaining_average).toFixed(2)} · ${String(selected.side || '').toUpperCase()} P ${Number(selected.selected_probability || 0).toFixed(1)}% (${Number(selected.selected_probability_low || 0).toFixed(1)}–${Number(selected.selected_probability_high || 0).toFixed(1)}%) · executable edge ${Number(selected.expected_edge_cents || 0).toFixed(2)}c / low ${Number(selected.edge_low_cents || 0).toFixed(2)}c · ${probability.method || 'collecting'}${reasons.length ? ' · blocked: ' + reasons.map(value => String(value).replaceAll('_', ' ')).join(', ') : ' · policy eligible'}`
        : `Waiting for the next BTC final-minute BRTI window · feed ${status.toLowerCase()}${health.error ? ' · ' + health.error : ''}`;
      setText('crypto-settlement-lag-current', currentText, 'shadow-note');
      renderRows('crypto-settlement-lag-recent', (data.recent_records || []).slice(0, 50), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => `${row.ticker || ''} · ${row.entry_band || ''} / ${Number(row.edge_threshold_cents || 0).toFixed(0)}c`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => `${Number(row.reading || 0)}/60`},
        {num: true, render: row => `${Number(row.entry_price_cents || 0).toFixed(2)}c`},
        {num: true, render: row => `${Number(row.selected_probability || 0).toFixed(1)}% / ${Number(row.selected_probability_low || 0).toFixed(1)}–${Number(row.selected_probability_high || 0).toFixed(1)}%`},
        {num: true, render: row => row.required_remaining_average === null || row.required_remaining_average === undefined ? '—' : Number(row.required_remaining_average).toFixed(2)},
        {num: true, render: row => `${Number(row.expected_edge_cents || 0).toFixed(2)}c / ${Number(row.edge_low_cents || 0).toFixed(2)}c`},
        {num: true, render: row => `${Number(row.spread_cents || 0).toFixed(2)}c / ${Number(row.book_walk_slippage_cents || 0).toFixed(2)}c / ${Number(row.fee_per_contract_cents || 0).toFixed(2)}c`},
        {render: row => String(row.exit_counterfactual?.status || 'monitoring').replaceAll('_', ' ')},
        {render: row => row.valid_for_evaluation === false ? 'INVALID · AUDIT ONLY' : (row.status === 'settled' ? (row.won ? 'WIN' : 'LOSS') : 'OPEN')},
        {num: true, render: row => row.valid_for_evaluation === false ? 'excluded' : (row.status === 'settled' ? `${money(row.hold_counterfactual?.virtual_profit || 0)} / ${money(row.exit_counterfactual?.virtual_profit || 0)}` : '—')}
      ], 'No settlement-lag policy arm has qualified yet.');
    }

    function renderCryptoDirectionalOppositionV2(data) {
      data = data || {};
      const lanes = data.lanes || [];
      const opposition = lanes.find(row => row.lane === 'directional_opposition') || {};
      const control = lanes.find(row => row.lane === 'confirmation_control') || {};
      const evaluation = data.evaluation || {};
      const required = Number(evaluation.minimum_independent_markets || 200);
      const oppositionProfit = Number(opposition.virtual_profit || 0);
      const controlProfit = Number(control.virtual_profit || 0);
      const delta = Number(data.raw_unpaired_opposition_minus_control_profit ?? data.opposition_minus_control_profit ?? 0);
      const matchedDelta = Number(data.matched_observational_profit_delta || 0);
      const matchDiagnostics = data.matched_observational_diagnostics || {};
      setText('crypto-directional-v2-opposition-sample', `${Number(opposition.settled || 0)} / ${required}`);
      setText('crypto-directional-v2-opposition-profit', `${money(oppositionProfit)} / ${Number(opposition.roi || 0).toFixed(1)}%`, `value ${oppositionProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-directional-v2-control-sample', `${Number(control.settled || 0)} / ${required}`);
      setText('crypto-directional-v2-control-profit', `${money(controlProfit)} / ${Number(control.roi || 0).toFixed(1)}%`, `value ${controlProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-directional-v2-delta', money(delta), `value ${delta >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-directional-v2-review', `${Number(evaluation.paired_markets_available || 0)} / ${required} strata-matched · never promotable`);
      setText('crypto-directional-v2-matched-delta', money(matchedDelta), `value ${matchedDelta >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-directional-v2-match-quality', `${Number(matchDiagnostics.same_ticker_pairs || 0)} / ${Number(matchDiagnostics.same_side_pairs || 0)} · median lag ${matchDiagnostics.median_capture_lag_seconds === null || matchDiagnostics.median_capture_lag_seconds === undefined ? '—' : Number(matchDiagnostics.median_capture_lag_seconds).toFixed(1) + 's'}`);
      const funnel = data.last_capture_funnel || {};
      const funnelReasons = Object.entries(funnel)
        .filter(([key]) => key.startsWith('reason:'))
        .slice(0, 6)
        .map(([key, count]) => `${key.slice(7).replaceAll('_', ' ')}: ${Number(count || 0)}`)
        .join(' · ');
      setText(
        'crypto-directional-v2-funnel',
        `Latest shadow review: ${Number(funnel.reviewed || 0)} candidates · ${Number(funnel.eligible || 0)} quality eligible · ${Number(funnel.rejected || 0)} rejected${funnelReasons ? ' · ' + funnelReasons : ''}`,
        'shadow-note'
      );
      renderRows('crypto-directional-v2-recent', (data.recent_records || []).slice(0, 60), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => String(row.lane || '').replaceAll('_', ' ')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => `${Number(row.entry_price_cents || 0).toFixed(1)}c + ${Number(row.exact_fee_cents || 0).toFixed(2)}c`},
        {num: true, render: row => `${Number(row.selected_side_probability || 0).toFixed(1)}% / ${Number(row.selected_side_probability_low || 0).toFixed(1)}–${Number(row.selected_side_probability_high || 0).toFixed(1)}%`},
        {num: true, render: row => `${Number(row.expected_edge || 0).toFixed(2)}c / ${Number(row.edge_low || 0).toFixed(2)}c`},
        {num: true, render: row => `${Number(row.directional_confirmation?.flow_strength || 0).toFixed(3)} / ${Number(row.directional_confirmation?.selected_side_source_count || 0)}`},
        {render: row => `${row.strategy_version || ''} · ${row.strategy_config_hash || ''}`},
        {render: row => row.result || 'OPEN'},
        {num: true, render: row => row.status === 'settled' ? money(row.virtual_profit || 0) : '—'}
      ], 'Waiting for a fresh, internally consistent candidate to enter either directional comparison lane.');

      const paired = data.paired_forward || {};
      const pairDefinition = paired.definition || {};
      const pairEvaluation = paired.evaluation || {};
      const pairFade = paired.fade || {};
      const pairFollow = paired.follow || {};
      const pairRequired = Number(pairDefinition.minimum_unique_markets || 100);
      const expiryRequired = Number(pairDefinition.minimum_expiry_windows || 50);
      const dayRequired = Number(pairDefinition.minimum_observation_days || 30);
      const fadeProfit = Number(pairFade.virtual_profit || 0);
      const followProfit = Number(pairFollow.virtual_profit || 0);
      const pairDelta = Number(paired.paired_profit_delta || 0);
      setText('crypto-directional-pair-sample', `${Number(paired.unique_settled_markets || 0)} / ${pairRequired}`);
      setText('crypto-directional-pair-expiries', `${Number(paired.independent_expiry_windows || 0)} / ${expiryRequired}`);
      setText('crypto-directional-pair-days', `${Number(paired.observation_days || 0)} / ${dayRequired}`);
      setText('crypto-directional-pair-fade', `${money(fadeProfit)} / ${Number(pairFade.roi || 0).toFixed(1)}%`, `value ${fadeProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-directional-pair-follow', `${money(followProfit)} / ${Number(pairFollow.roi || 0).toFixed(1)}%`, `value ${followProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-directional-pair-delta', money(pairDelta), `value ${pairDelta >= 0 ? 'positive' : 'negative'}`);
      const fadeLower = paired.fade_cluster_inference?.one_sided_90_lower;
      const deltaLower = paired.delta_cluster_inference?.one_sided_90_lower;
      const fadeDayLower = paired.fade_day_cluster_inference?.lower_bound;
      const deltaDayLower = paired.delta_day_cluster_inference?.lower_bound;
      setText(
        'crypto-directional-pair-lower',
        fadeLower === null || fadeLower === undefined || deltaLower === null || deltaLower === undefined
          ? 'Collecting'
          : `${money(fadeLower)} / ${money(deltaLower)} · day ${money(fadeDayLower || 0)} / ${money(deltaDayLower || 0)}`,
        `value ${(Number(fadeLower || 0) > 0 && Number(deltaLower || 0) > 0 && Number(fadeDayLower || 0) > 0 && Number(deltaDayLower || 0) > 0) ? 'positive' : 'negative'}`
      );
      const firstHalf = Number(pairFade.chronological_halves?.first?.virtual_profit || 0);
      const secondHalf = Number(pairFade.chronological_halves?.second?.virtual_profit || 0);
      setText(
        'crypto-directional-pair-halves',
        Number(paired.settled || 0) < 2 ? 'Collecting' : `${money(firstHalf)} / ${money(secondHalf)}`,
        `value ${(firstHalf > 0 && secondHalf > 0) ? 'positive' : 'negative'}`
      );
      const integerForward = paired.integer_contract_forward || {};
      const integerFadeProfit = Number(integerForward.fade?.virtual_profit || 0);
      const integerFollowProfit = Number(integerForward.follow?.virtual_profit || 0);
      setText(
        'crypto-directional-pair-integer',
        `${Number(integerForward.settled || 0)} / ${pairRequired} · ${money(integerFadeProfit)} / ${money(integerFollowProfit)} · Δ ${money(integerForward.profit_delta || 0)}`,
        `value ${(integerFadeProfit > integerFollowProfit && integerFadeProfit > 0) ? 'positive' : (Number(integerForward.settled || 0) ? 'negative' : '')}`
      );
      const integerLower = integerForward.delta_day_cluster_inference?.lower_bound;
      setText(
        'crypto-directional-pair-integer-lower',
        integerLower === null || integerLower === undefined ? 'Collecting' : money(integerLower),
        `value ${Number(integerLower || 0) > 0 ? 'positive' : 'negative'}`
      );
      setText(
        'crypto-directional-pair-review',
        pairEvaluation.ready_for_manual_review ? 'READY · MANUAL ONLY' : 'COLLECTING',
        `value ${pairEvaluation.ready_for_manual_review ? 'positive' : ''}`
      );
      const pairedFunnel = paired.last_capture_funnel || {};
      const pairedReasons = Object.entries(pairedFunnel)
        .filter(([key]) => key.startsWith('reason:'))
        .slice(0, 4)
        .map(([key, count]) => `${key.slice(7).replaceAll('_', ' ')}: ${Number(count || 0)}`)
        .join(' · ');
      setText(
        'crypto-directional-pair-funnel',
        `Registered ${shortTime(paired.registered_at || '') || 'now'} · latest: ${Number(pairedFunnel.reviewed_opposition || 0)} opposition cases · ${Number(pairedFunnel.captured || 0)} new pairs${pairedReasons ? ' · ' + pairedReasons : ''}`,
        'shadow-note'
      );
      renderRows('crypto-directional-pair-recent', (paired.recent_records || []).slice(0, 60), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => `${String(row.fade?.side || '').toUpperCase()} @ ${Number(row.fade?.entry_price_cents || 0).toFixed(1)}c`},
        {render: row => `${String(row.follow?.side || '').toUpperCase()} @ ${Number(row.follow?.entry_price_cents || 0).toFixed(1)}c`},
        {num: true, render: row => `${Number(row.directional_confirmation?.flow_strength || 0).toFixed(3)} / ${Number(row.directional_confirmation?.selected_side_source_count || 0)}`},
        {render: row => row.status === 'settled' ? String(row.market_result || '').toUpperCase() : 'OPEN'},
        {num: true, render: row => row.status === 'settled' ? `${money(row.fade?.virtual_profit || 0)} / ${money(row.follow?.virtual_profit || 0)} / ${money(row.profit_delta || 0)}` : '—'}
      ], 'No new 35–44¢ contemporaneous opposition pair has qualified since registration.');

      const replications = data.asset_replications || {};
      const hypotheses = replications.hypotheses || [];
      const doge = hypotheses.find(row => row.hypothesis === 'doge_fade') || {};
      const eth = hypotheses.find(row => row.hypothesis === 'eth_follow') || {};
      const replicationRequired = Number(replications.minimum_unique_markets_per_hypothesis || 100);
      const replicationDays = Number(replications.minimum_observation_days || 30);
      const setReplication = (prefix, row) => {
        const primary = row.primary || {};
        const profit = Number(primary.virtual_profit || 0);
        const lower = row.primary_day_cluster_inference?.lower_bound;
        setText(`crypto-directional-${prefix}-sample`, `${Number(row.settled || 0)} / ${replicationRequired} · ${Number(row.observation_days || 0)} / ${replicationDays}`);
        setText(`crypto-directional-${prefix}-profit`, `${money(profit)} / ${Number(primary.roi || 0).toFixed(1)}%`, `value ${profit > 0 ? 'positive' : (Number(row.settled || 0) ? 'negative' : '')}`);
        setText(`crypto-directional-${prefix}-lower`, lower === null || lower === undefined ? 'Collecting' : money(lower), `value ${Number(lower || 0) > 0 ? 'positive' : 'negative'}`);
      };
      setReplication('doge', doge);
      setReplication('eth', eth);
      const readyHypotheses = hypotheses.filter(row => row.evaluation?.ready_for_manual_review).length;
      setText('crypto-directional-assets-review', readyHypotheses === 2 ? 'BOTH READY · MANUAL ONLY' : `${readyHypotheses} / 2 READY · COLLECTING`, `value ${readyHypotheses === 2 ? 'positive' : ''}`);
      setText('crypto-directional-assets-funnel', `Registered ${shortTime(replications.registered_at || '') || 'now'} · fresh-only records ${Number((replications.recent_records || []).length)} · 97.5% one-sided per hypothesis · no backfill`, 'shadow-note');
      renderRows('crypto-directional-assets-recent', (replications.recent_records || []).slice(0, 60), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => String(row.hypothesis || '').replaceAll('_', ' ')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => { const arm = row[row.primary_arm_name] || {}; return `${String(arm.side || '').toUpperCase()} @ ${Number(arm.entry_price_cents || 0).toFixed(1)}c`; }},
        {render: row => { const arm = row[row.comparator_arm_name] || {}; return `${String(arm.side || '').toUpperCase()} @ ${Number(arm.entry_price_cents || 0).toFixed(1)}c`; }},
        {render: row => row.status === 'settled' ? String(row.market_result || '').toUpperCase() : 'OPEN'},
        {num: true, render: row => row.status === 'settled' ? `${money(row.primary_profit || 0)} / ${money(row.comparator_profit || 0)} / ${money(row.profit_delta || 0)}` : '—'}
      ], 'No fresh preregistered DOGE or ETH replication has qualified yet.');
    }

    function renderCryptoSignalTournament(data) {
      data = data || {};
      const lanes = data.lanes || [];
      const running = data.mode === 'paper_shadow_only';
      setText('crypto-signal-tournament-status', running ? 'FORWARD SHADOW RUNNING' : String(data.mode || 'waiting').replaceAll('_', ' ').toUpperCase(), `health-chip ${running ? 'good' : 'warn'}`);
      setText('crypto-signal-tournament-records', Number(data.tracked || 0));
      setText('crypto-signal-tournament-settled', Number(data.settled || 0));
      const leader = (data.ranking || [])[0] || {};
      setText(
        'crypto-signal-tournament-leader',
        leader.lane ? `Snapshot only · ${String(leader.lane).replaceAll('_', ' ')} · ${money(leader.profit || 0)} / ${Number(leader.roi || 0).toFixed(1)}%` : 'Collecting',
        `value ${Number(leader.profit || 0) > 0 ? 'positive' : (Number(leader.settled || 0) ? 'negative' : '')}`
      );
      const interim = lanes.filter(row => row.interim_review?.eligible).length;
      const final = lanes.filter(row => row.final_review?.eligible).length;
      setText('crypto-signal-tournament-interim', `${interim} / ${lanes.length || 7}`);
      setText('crypto-signal-tournament-final', `${final} / ${lanes.length || 7}`);
      setText('crypto-signal-tournament-confidence', `${(100 * Number(data.configuration?.per_hypothesis_one_sided_confidence_level || 0.992857)).toFixed(2)}%`);
      const parity = data.execution_parity || {};
      setText('crypto-signal-parity-fillable', `${Number(parity.book_fillable || 0)} / ${Number(parity.tracked || 0)}`);
      setText(
        'crypto-signal-parity-profit',
        Number(parity.settled || 0) ? `${money(parity.profit || 0)} / ${Number(parity.roi || 0).toFixed(1)}%` : 'Collecting',
        `value ${Number(parity.settled || 0) ? (Number(parity.profit || 0) >= 0 ? 'positive' : 'negative') : ''}`
      );
      const liveParity = parity.live_policy || {};
      setText(
        'crypto-signal-live-parity',
        `${Number(liveParity.tracked || 0)} attempts · ${Number(liveParity.would_submit || 0)} submit · ${Number(liveParity.actual_filled || 0)} filled · ${Number(liveParity.rejected || 0)} rejected`
      );
      const funnel = data.last_capture_funnel || {};
      const reasons = Object.entries(funnel).filter(([key]) => key.startsWith('reason:')).slice(0, 5)
        .map(([key, count]) => `${key.slice(7).replaceAll('_', ' ')}: ${Number(count || 0)}`).join(' · ');
      setText(
        'crypto-signal-tournament-funnel',
        `Registered ${shortTime(data.registered_at || '') || 'now'} · latest ${Number(funnel.reviewed || 0)} reviewed · ${Number(funnel.quality_eligible || 0)} quality books · ${Number(funnel.signal_instances || 0)} signal instances · ${Number(funnel.captured || 0)} new${reasons ? ' · ' + reasons : ''}`,
        'shadow-note'
      );
      renderRows('crypto-signal-tournament-lanes', lanes, [
        {render: row => String(row.lane || '').replaceAll('_', ' ')},
        {num: true, render: row => `${Number(row.unique_markets || 0)} / ${Number(row.observation_days || 0)}`},
        {num: true, render: row => `${Number(row.primary?.wins || 0)}W / ${Number(row.primary?.losses || 0)}L`},
        {num: true, render: row => `${money(row.primary?.profit || 0)} / ${Number(row.primary?.roi || 0).toFixed(1)}%`},
        {num: true, render: row => `${Number(row.execution_parity?.book_fillable || 0)} / ${Number(row.execution_parity?.tracked || 0)}`},
        {num: true, render: row => Number(row.execution_parity?.settled || 0) ? `${money(row.execution_parity?.profit || 0)} / ${Number(row.execution_parity?.roi || 0).toFixed(1)}%` : 'Collecting'},
        {num: true, render: row => money(row.profit_delta || 0)},
        {num: true, render: row => row.day_cluster_inference?.lower_bound === null || row.day_cluster_inference?.lower_bound === undefined ? 'Collecting' : money(row.day_cluster_inference.lower_bound)},
        {render: row => row.final_review?.eligible ? 'FINAL · MANUAL' : (row.interim_review?.eligible ? 'INTERIM' : 'COLLECTING')}
      ], 'No registered signal has qualified yet.');
      renderRows('crypto-signal-tournament-recent', (data.recent_records || []).slice(0, 60), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => String(row.lane || '').replaceAll('_', ' ')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => `${String(row.primary?.side || '').toUpperCase()} @ ${Number(row.primary?.entry_price_cents || 0).toFixed(1)}c`},
        {render: row => `${String(row.comparator?.side || '').toUpperCase()} @ ${Number(row.comparator?.entry_price_cents || 0).toFixed(1)}c`},
        {num: true, render: row => Number(row.signal_strength || 0).toFixed(3)},
        {render: row => {
          const parity = row.execution_parity || {};
          if (!parity.status) return 'Snapshot only';
          const price = parity.refreshed_entry_price_cents;
          return `${String(parity.status).replaceAll('_', ' ')}${price === null || price === undefined ? '' : ` @ ${Number(price).toFixed(1)}c`}${parity.reason && parity.reason !== parity.status ? ` · ${String(parity.reason).replaceAll('_', ' ')}` : ''}`;
        }},
        {render: row => row.status === 'settled' ? String(row.market_result || '').toUpperCase() : 'OPEN'},
        {num: true, render: row => row.status === 'settled' ? `${money(row.primary?.virtual_profit || 0)} / ${money(row.comparator?.virtual_profit || 0)} / ${money(row.profit_delta || 0)}` : '—'}
      ], 'Waiting for fresh forward signal observations.');
    }

    function renderCryptoExecutionLab(data) {
      data = data || {};
      const lanes = data.lanes || [];
      const running = data.mode === 'production_pipeline_shadow_only' && data.collection_status !== 'retired_settlement_only';
      setText('crypto-execution-lab-status', running ? 'PRODUCTION PARITY RUNNING' : String(data.mode || 'waiting').replaceAll('_', ' ').toUpperCase(), `health-chip ${running ? 'good' : 'warn'}`);
      setText('crypto-execution-lab-records', `${Number(data.tracked || 0)} / ${Number(data.settled || 0)}`);
      const attempts = lanes.reduce((sum, row) => sum + Number(row.attempts || 0), 0);
      const submits = lanes.reduce((sum, row) => sum + Number(row.would_submit || 0), 0);
      setText('crypto-execution-lab-attempts', `${submits} / ${attempts}`);
      setText('crypto-execution-lab-pending', Number(data.pending_pullbacks || 0));
      const leader = (data.ranking || []).find(row => Number(row.settled || 0) > 0) || {};
      setText(
        'crypto-execution-lab-leader',
        leader.lane ? `${String(leader.lane).replaceAll('_', ' ')} · ${money(leader.profit || 0)} / ${Number(leader.roi || 0).toFixed(1)}%` : 'Collecting',
        `value ${Number(leader.profit || 0) > 0 ? 'positive' : (Number(leader.settled || 0) ? 'negative' : '')}`
      );
      const ready = lanes.filter(row => row.review?.eligible).length;
      setText('crypto-execution-lab-ready', `${ready} / ${lanes.length || 9}`, `value ${ready ? 'positive' : ''}`);
      const funnel = data.last_capture_funnel || {};
      const blocked = Object.entries(funnel)
        .filter(([key]) => key.startsWith('reason:') || key.startsWith('rejected:'))
        .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
        .slice(0, 5)
        .map(([key, count]) => `${key.replace(/^reason:/, '').replace(/^rejected:/, '').replaceAll('_', ' ')}: ${Number(count || 0)}`)
        .join(' · ');
      setText(
        'crypto-execution-lab-funnel',
        `Registered ${shortTime(data.registered_at || '') || 'now'} · latest ${Number(funnel.reviewed || 0)} reviewed · ${Number(funnel.fresh_snapshots || 0)} refreshed · ${Number(funnel.quality_eligible || 0)} production-quality · ${Number(data.captured_this_scan || 0)} entries · ${Number(data.pending_pullbacks || 0)} pullbacks waiting${blocked ? ' · ' + blocked : ''}`,
        'shadow-note'
      );
      renderRows('crypto-execution-lab-lanes', lanes, [
        {render: row => String(row.lane || '').replaceAll('_', ' ')},
        {num: true, render: row => `${Number(row.would_submit || 0)} / ${Number(row.attempts || 0)}`},
        {num: true, render: row => `${Number(row.unique_markets || 0)} / ${Number(row.observation_days || 0)}`},
        {num: true, render: row => `${Number(row.wins || 0)}W / ${Number(row.losses || 0)}L`},
        {num: true, render: row => Number(row.settled || 0) ? `${money(row.profit || 0)} / ${Number(row.roi || 0).toFixed(1)}%` : 'Collecting'},
        {num: true, render: row => `${money(row.chronological_halves?.first_profit || 0)} / ${money(row.chronological_halves?.second_profit || 0)}`},
        {num: true, render: row => row.day_cluster_inference?.lower_bound === null || row.day_cluster_inference?.lower_bound === undefined ? 'Collecting' : money(row.day_cluster_inference.lower_bound)},
        {render: row => row.review?.eligible ? 'READY · MANUAL ONLY' : 'COLLECTING'}
      ], 'No production-parity strategy has generated an executable entry yet.');
      renderRows('crypto-execution-lab-recent', (data.recent_attempts || []).slice(0, 80), [
        {render: row => shortTime(row.captured_at || '')},
        {render: row => String(row.lane || '').replaceAll('_', ' ')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => `${Number(row.signal_entry_price_cents || 0).toFixed(1)}c / ${row.confirmed_entry_price_cents === null || row.confirmed_entry_price_cents === undefined ? '—' : Number(row.confirmed_entry_price_cents).toFixed(1) + 'c'}`},
        {num: true, render: row => Number(row.required_contracts || 0)},
        {render: row => row.would_submit ? 'WOULD SUBMIT · FOK' : String(row.reason || 'rejected').replaceAll('_', ' ')}
      ], 'No execution-lab attempts have matched yet.');
    }

    function renderCryptoExpansion(data) {
      data = data || {};
      const stale = !data.generated_at || Date.now() - Date.parse(data.generated_at) > 120000;
      setText('crypto-expansion-status', `${stale ? 'Waiting / stale worker' : 'Collecting'} · updated ${shortTime(data.generated_at || '') || '--'} · registered ${shortTime(data.registered_at || '') || '--'} · review ${shortTime(data.next_review_at || '') || '--'} · model ${String(data.model_status || 'waiting').replaceAll('_', ' ')}`);
      const c = data.challenger || {}, e = data.eth_entry || {}, r = data.regime || {};
      const pct = v => v == null ? '--' : `${(100 * v).toFixed(1)}%`;
      const score = v => v == null ? '--' : Number(v).toFixed(5);
      const rows = [
        {name:'Frozen research model', evidence:`${c.forecasts || 0} scored forecasts · ${c.settled || 0} settled trades · ${c.open || 0} open`, result:`${money(c.profit_dollars || 0)} net / ${money(c.stress_profit_dollars || 0)} stressed`, comparison:`Brier improvement vs market ${score(c.brier_improvement)}; positive is better`},
        {name:'ETH wait for 1c improvement', evidence:`${e.settled || 0} settled pairs · ${e.filled || 0} delayed entries · ${e.open || 0} open`, result:`${money(e.wait_profit || 0)} waiting vs ${money(e.immediate_profit || 0)} immediate`, comparison:`${e.missed_winners || 0} missed winners · ${e.avoided_losers || 0} avoided losers · paired stressed difference ${money(e.paired_stress_delta || 0)}`},
        {name:'One-hour regime reversal', evidence:`${r.settled || 0} scored · ${r.open || 0} open · ${r.unscored || 0} missing exits`, result:`${pct(r.reversal_accuracy)} reversal vs ${pct(r.original_accuracy)} original`, comparison:`Always-up ${pct(r.always_up_accuracy)} on same observations; forecast-only, no trade P&L`}
      ];
      renderRows('crypto-expansion-studies', rows, [{render: row => row.name}, {render: row => row.evidence}, {render: row => row.result}, {render: row => row.comparison}], 'Waiting for research.');
      setText('crypto-expansion-coverage', 'New feature coverage: ' + (Object.entries(data.feature_coverage || {}).map(([name, v]) => `${name.replaceAll('_', ' ')} ${v.available}/${v.observations}`).join(' · ') || 'warming up'));
      setText('crypto-expansion-rejections', Object.entries(data.rejections || {}).slice(0, 5).map(([name, count]) => `${name.replaceAll('_', ' ')}: ${count}`).join(' · ') || 'No rejected opportunities yet.');
    }

    function renderBtcRandomShadow(data) {
      const age = Date.now() - Date.parse(data.generated_at || '');
      const fresh = Number.isFinite(age) && age >= -1000 && age <= 120000;
      setText('crypto-btc-random-status', `${fresh ? (data.worker?.status || 'Collecting') : 'Waiting / stale worker'} · updated ${shortTime(data.generated_at || '') || '--'} · ${Number(data.scan_count || 0)} scans · independent $5,000 bankrolls`);
      renderRows('crypto-btc-random-arms', data.arms || [], [
        {render:r => r.name},
        {render:r => `${money(r.cash)} / ${money(r.exposure)}`},
        {render:r => `${money(r.profit)} / ${money(r.stress_profit)}`},
        {render:r => `${r.wins || 0} / ${r.settled || 0} / ${r.missed || 0}`},
        {render:r => `${money(r.drawdown)} / ${money(r.daily_gross_loss)}`},
        {render:r => r.paused ? 'Risk limit reached' : r.active ? `${r.active.side === 'yes' ? 'UP' : 'DOWN'} · ${r.active.status === 'filled' ? 'awaiting settlement' : 'waiting for price'} · ${shortTime(r.active.start_at)}` : 'Waiting for next pre-start draw'},
        {render:r => r.active ? `${r.active.model_available ? (100*r.active.side_confidence).toFixed(1)+'% model' : '50% random'} / ${r.active.cost == null ? 'not entered' : money(r.active.cost)}` : '--'}
      ], 'No forward cycles registered yet.');
      const f = data.forecast || {};
      const h = Object.entries(f.horizons || {}).map(([name,v]) => `${name}: ${v.direction} ${(100*v.probability_up).toFixed(1)}% up · ${v.evaluation?.matured || 0} scored`);
      setText('crypto-btc-random-forecast', `BTC forecast: ${f.available ? `${f.direction} · ${(100*f.probability_up).toFixed(1)}% up · ${f.evaluation?.matured || 0} settled 15m labels` : 'waiting for complete closed-candle history'} · ${h.join(' · ')}`);
      setText('crypto-btc-random-rejections', Object.entries(data.rejections || {}).sort((a,b) => b[1]-a[1]).slice(0,5).map(([key,count]) => `${key.replaceAll('_',' ')}: ${count}`).join(' · ') || 'No rejected scans yet.');
    }

    function renderBtcValueShadow(data) {
      const age = Date.now() - Date.parse(data.generated_at || '');
      const fresh = Number.isFinite(age) && age >= -1000 && age <= 120000;
      setText('crypto-btc-value-status', `${fresh ? (data.status || 'Collecting') : 'Waiting / stale worker'} · registered ${shortTime(data.registered_at || '') || '--'} · ends ${shortTime(data.ends_at || '') || '--'} · updated ${shortTime(data.generated_at || '') || '--'}`);
      renderRows('crypto-btc-value-arms', data.arms || [], [
        {render:r => r.name},
        {render:r => `${money(r.cash)} / ${money(r.exposure)}`},
        {render:r => `${money(r.profit)} / ${money(r.stress_profit)}`},
        {render:r => `${r.wins || 0} / ${r.settled || 0} / ${r.missed || 0}`},
        {render:r => `${money(r.drawdown)} / ${money(r.daily_gross_loss)}`},
        {render:r => r.drawdown >= 250 || r.daily_gross_loss >= 50 ? 'Risk limit reached' : r.active ? `${r.active.side === 'yes' ? 'UP' : 'DOWN'} · ${r.active.status === 'filled' ? 'awaiting settlement' : 'waiting for price'} · ${shortTime(r.active.start_at)}` : data.completed ? 'Observation window completed' : 'Waiting for next pre-start draw'},
        {render:r => `${r.settled || 0}/200 settled · ${r.entry_days || 0}/10 days${r.review_checkpoint ? ' · ready for review' : ''}`}
      ], 'No prospective cycles yet.');
      const p = data.paired || {};
      setText('crypto-btc-value-paired', `Patient versus reference: ${p.completed_windows || 0} completed paired windows · ${money(p.patient_minus_reference_profit || 0)} difference · ${p.patient_missed_reference_filled || 0} skipped reference fills. Missed trades count as zero.`);
      setText('crypto-btc-value-rejections', (data.errors || []).join(' · ') || Object.entries(data.rejections || {}).sort((a,b) => b[1]-a[1]).slice(0,5).map(([key,count]) => `${key.replaceAll('_',' ')}: ${count}`).join(' · ') || 'No rejected scans yet.');
    }

    function renderCryptoSizing(data) {
      data = data || {};
      const age = Date.now() - Date.parse(data.generated_at || '');
      const fresh = Number.isFinite(age) && age >= -1000 && age <= 120000;
      const status = fresh ? (data.worker?.report_fresh ? 'Collecting' : 'Scanner input stale; entries paused') : 'Waiting / stale worker';
      setText('crypto-sizing-status', `${status} · registered ${shortTime(data.registered_at || '') || '--'} · review ${shortTime(data.next_review_at || '') || '--'} · updated ${shortTime(data.generated_at || '') || '--'}`);
      const labels = {fixed_3:'Fixed 3',signal:'Signal strength',defensive:'Drawdown reduction',bounded_recovery:'Capped recovery',confidence_bound:'Qualified probability bound'};
      renderRows('crypto-sizing-arms', data.arms || [], [
        {render:r => {const parts = String(r.arm || '').split(':'); return `${parts[0] === 'patient' ? 'Patient' : 'Immediate'} / ${labels[parts[1]] || parts[1]}`;}},
        {render:r => `${r.filled || 0} / ${r.opportunities || 0} / ${r.days || 0}`},
        {render:r => r.average_contracts == null ? '--' : Number(r.average_contracts).toFixed(2)},
        {render:r => `${money(r.profit || 0)} / ${money(r.stress_profit || 0)}`},
        {render:r => `${r.roi == null ? '--' : (100*r.roi).toFixed(1)+'%'} / ${money(r.cost || 0)}`},
        {render:r => `${money(r.max_drawdown || 0)} / ${money(r.exposure || 0)}`},
        {render:r => money(r.paired_stress_delta || 0)}
      ], 'Waiting for new forward observations.');
      setText('crypto-sizing-rejections', Object.entries(data.rejections || {}).sort((a,b) => b[1]-a[1]).slice(0,5).map(([key,count]) => `${key.replaceAll('_',' ')}: ${count}`).join(' · ') || 'No rejected opportunities yet.');
    }

    function renderCryptoProspective(data) {
      data = data || {};
      setText('crypto-prospective-registration', `Registered ${shortTime(data.registered_at || '') || 'waiting'} · next fixed review ${shortTime(data.next_review_at || '') || 'waiting'} · ${Number(data.records || 0)} entries · ${Number(data.brti_fixed_observations || 0)} official BRTI observations`);
      const portfolio = data.portfolio || {};
      setText('crypto-prospective-portfolio', `Shared virtual portfolio: ${Number(portfolio.entries || 0)} entries · ${Number(portfolio.open || 0)} open · ${money(portfolio.exposure_dollars || 0)} exposure · ${money(portfolio.profit_dollars || 0)} realized`);
      const reviews = data.last_fixed_review?.lanes || [];
      renderRows('crypto-prospective-lanes', data.lanes || [], [
        {render: row => String(row.lane || '').replaceAll('_', ' ')},
        {num: true, render: row => `${Number(row.markets || 0)} / ${Number(row.expiries || 0)} / ${Number(row.active_dates || 0)}`},
        {num: true, render: row => `${money(row.profit_dollars || 0)} / ${money(row.stress_profit_dollars || 0)}`},
        {num: true, render: row => `${row.day_cluster_lower_per_contract == null ? '—' : money(row.day_cluster_lower_per_contract)} / ${row.expiry_cluster_lower_per_contract == null ? '—' : money(row.expiry_cluster_lower_per_contract)}`},
        {render: row => String(reviews.find(r => r.lane === row.lane)?.status || 'collecting until fixed review').replaceAll('_', ' ')}
      ], 'Waiting for the first prospective scan.');
      setText('crypto-prospective-rejections', Object.entries(data.rejections || {}).slice(0, 5).map(([reason, count]) => `${reason.replaceAll('_', ' ')}: ${count}`).join(' · ') || 'No rejected opportunities recorded yet.');
    }

    function renderCryptoAssetSpecialist(data) {
      data = data || {};
      const lanes = data.lanes || [];
      const running = data.mode === 'production_pipeline_shadow_only' && data.collection_status !== 'retired_settlement_only';
      setText('crypto-asset-specialist-status', running ? 'FORWARD RUNNING' : String(data.collection_status || data.mode || 'waiting').replaceAll('_', ' ').toUpperCase(), `health-chip ${running ? 'good' : 'warn'}`);
      setText('crypto-asset-specialist-records', `${Number(data.tracked || 0)} / ${Number(data.settled || 0)}`);
      const leader = (data.ranking || []).find(row => Number(row.settled || 0) > 0) || {};
      setText(
        'crypto-asset-specialist-leader',
        leader.lane ? `${String(leader.lane).replaceAll('_', ' ')} · ${money(leader.profit || 0)} / ${Number(leader.roi || 0).toFixed(1)}%` : 'Collecting',
        `value ${Number(leader.profit || 0) > 0 ? 'positive' : (Number(leader.settled || 0) ? 'negative' : '')}`
      );
      const portfolio = lanes.find(row => row.portfolio_candidate) || {};
      const projection = data.portfolio_projection || {};
      setText(
        'crypto-asset-specialist-volume',
        `${Number(projection.positions_per_hour || 0).toFixed(2)}/hr · ${Number(projection.settled || 0)} settled · max ${Number(projection.maximum_contracts || 5)} ct`
      );
      setText(
        'crypto-asset-specialist-portfolio',
        Number(portfolio.settled || 0) ? `${money(portfolio.profit || 0)} / ${Number(portfolio.roi || 0).toFixed(1)}%` : 'Collecting',
        `value ${Number(portfolio.profit || 0) > 0 ? 'positive' : (Number(portfolio.settled || 0) ? 'negative' : '')}`
      );
      setText('crypto-asset-specialist-confidence', `${(100 * Number(data.per_lane_confidence_level || 0.991667)).toFixed(2)}%`);
      const ready = lanes.filter(row => row.review?.eligible).length;
      setText('crypto-asset-specialist-ready', `${ready} / ${lanes.length || 6}`, `value ${ready ? 'positive' : ''}`);
      const funnel = data.last_capture_funnel || {};
      const blocked = Object.entries(funnel)
        .filter(([key]) => key.startsWith('reason:') || key.startsWith('rejected:'))
        .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
        .slice(0, 4)
        .map(([key, count]) => `${key.replace(/^reason:/, '').replace(/^rejected:/, '').replaceAll('_', ' ')}: ${Number(count || 0)}`)
        .join(' · ');
      setText(
        'crypto-asset-specialist-funnel',
        `Registered ${shortTime(data.policy_registered_at || data.registered_at || '') || 'now'} · latest ${Number(funnel.reviewed || 0)} reviewed · ${Number(funnel.fresh_snapshots || 0)} refreshed · ${Number(funnel.quality_eligible || 0)} production-quality · ${Number(data.captured_this_scan || 0)} entries${blocked ? ' · ' + blocked : ''}`,
        'shadow-note'
      );
      renderRows('crypto-asset-specialist-lanes', lanes, [
        {render: row => `${row.portfolio_candidate ? 'PORTFOLIO · ' : ''}${String(row.lane || '').replaceAll('_', ' ')}`},
        {num: true, render: row => `${Number(row.would_submit || 0)} / ${Number(row.attempts || 0)}`},
        {num: true, render: row => `${Number(row.unique_markets || 0)} / ${Number(row.observation_days || 0)}`},
        {num: true, render: row => `${Number(row.wins || 0)}W / ${Number(row.losses || 0)}L`},
        {num: true, render: row => Number(row.settled || 0) ? `${money(row.profit || 0)} / ${Number(row.roi || 0).toFixed(1)}%` : 'Collecting'},
        {num: true, render: row => `${money(row.chronological_halves?.first_profit || 0)} / ${money(row.chronological_halves?.second_profit || 0)}`},
        {num: true, render: row => {
          const expiry = row.expiry_cluster_inference?.lower_bound;
          const day = row.day_cluster_inference?.lower_bound;
          return `${expiry === null || expiry === undefined ? '—' : money(expiry)} / ${day === null || day === undefined ? '—' : money(day)}`;
        }},
        {render: row => row.review?.eligible ? 'READY · MANUAL ONLY' : 'COLLECTING'}
      ], 'No post-registration specialist strategy has generated an executable entry yet.');
      renderRows('crypto-asset-specialist-recent', (data.recent_attempts || []).slice(0, 80), [
        {render: row => shortTime(row.captured_at || '')},
        {render: row => String(row.lane || '').replaceAll('_', ' ')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => `${Number(row.signal_entry_price_cents || 0).toFixed(1)}c / ${row.confirmed_entry_price_cents === null || row.confirmed_entry_price_cents === undefined ? '—' : Number(row.confirmed_entry_price_cents).toFixed(1) + 'c'}`},
        {num: true, render: row => Number(row.required_contracts || 0)},
        {render: row => row.would_submit ? 'WOULD SUBMIT · FOK' : String(row.reason || 'rejected').replaceAll('_', ' ')}
      ], 'No specialist execution attempts have matched yet.');
    }

    let patientControlBusy = false;
    let cryptoFundingBusy = false;
    function renderCryptoFunding(data) {
      const money = value => value == null ? 'unverified' : `$${Number(value).toFixed(2)}`;
      setText('crypto-funding-balances', `Crypto available cash: ${money(data.crypto_cash)} · Total available account cash: ${money(data.account_cash)}`);
      if (!cryptoFundingBusy) setText('crypto-funding-detail', data.message || '');
      document.getElementById('crypto-funding-apply').disabled = cryptoFundingBusy;
    }
    async function fundCryptoBankroll() {
      if (cryptoFundingBusy) return;
      cryptoFundingBusy = true;
      document.getElementById('crypto-funding-apply').disabled = true;
      setText('crypto-funding-detail', 'Enabling shared cash with no percentage allocation…');
      try {
        const response = await fetch('/api/crypto/funding', {
          method: 'POST', headers: {'Content-Type': 'application/json', 'X-Crypto-Funding-Action': '1'},
          body: JSON.stringify({action: 'shared_pool'}),
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || 'Allocation was not confirmed.');
        setText('crypto-funding-detail', result.funding.message);
      } catch (error) {
        setText('crypto-funding-detail', error.message || 'Check Kalshi allocation before retrying.');
      } finally {
        cryptoFundingBusy = false;
        document.getElementById('crypto-funding-apply').disabled = false;
      }
    }
    function renderPatientCryptoControl(data) {
      data = data || {};
      setText('crypto-patient-status', data.label || 'LOADING', `health-chip ${data.status === 'active' ? 'good' : 'warn'}`);
      setText('crypto-patient-detail', data.detail || 'Loading strategy status.');
      const activate = document.getElementById('crypto-patient-activate');
      const pause = document.getElementById('crypto-patient-pause');
      activate.disabled = patientControlBusy || !data.can_make_live;
      activate.onclick = () => controlPatientCrypto('make_live');
      pause.disabled = patientControlBusy || !data.enabled;
      activate.textContent = data.can_make_live ? 'Make live' : (data.status === 'active' ? 'Live' : 'Make live');
    }

    async function controlPatientCrypto(action) {
      if (patientControlBusy) return;
      patientControlBusy = true;
      document.getElementById('crypto-patient-activate').disabled = true;
      document.getElementById('crypto-patient-pause').disabled = true;
      setText('crypto-patient-detail', action === 'pause' ? 'Pausing new entries…' : 'Loading the selected live strategy…');
      try {
        const response = await fetch('/api/crypto/patient', {
          method: 'POST', headers: {'Content-Type': 'application/json', 'X-Crypto-Patient-Action': '1'},
          body: JSON.stringify({action}),
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || 'Could not update the strategy.');
        patientControlBusy = false;
        renderPatientCryptoControl(result.control);
      } catch (error) {
        patientControlBusy = false;
        await refresh();
        setText('crypto-patient-detail', error.message || 'Could not update the strategy.');
      }
    }

    function renderCryptoSpotFlowLivePilot(data) {
      data = data || {};
      const config = data.configuration || {};
      const validation = data.validation || {};
      const forward = validation.prospective_combo || {};
      const risk = data.risk || {};
      const bankroll = risk.bankroll || {};
      const cash = Number(bankroll.cash_balance || 0);
      const equity = Number(bankroll.account_equity ?? bankroll.sizing_bankroll ?? cash);
      const minimum = Number(config.minimum_bankroll ?? 0);
      const validationReady = Boolean(validation.eligible);
      const funded = cash >= minimum;
      const ready = validationReady && funded && Boolean(risk.ok);
      const status = ready
        ? 'LIVE PILOT ELIGIBLE'
        : (!funded ? 'ARMED · WAITING FOR BANKROLL' : 'ARMED · VALIDATING');
      setText('crypto-spot-flow-pilot-status', status, `health-chip ${ready ? 'good' : 'warn'}`);
      setText('crypto-spot-flow-pilot-bankroll', `${money(equity)} equity · ${money(cash)} cash`, `value ${funded ? 'positive' : 'negative'}`);
      setText('crypto-spot-flow-pilot-signal-gate', validation.gates?.spot_tournament_interim ? 'PASSED' : 'COLLECTING', `value ${validation.gates?.spot_tournament_interim ? 'positive' : ''}`);
      setText('crypto-spot-flow-pilot-forward', `${Number(forward.settled || 0)} / ${Number(config.minimum_forward_markets || 100)} · ${Number(forward.observation_days || 0)} / ${Number(config.minimum_forward_days || 3)} days`, `value ${forward.eligible ? 'positive' : ''}`);
      setText('crypto-spot-flow-pilot-sizing', `${(100 * Number(config.stake_fraction || 0.0005)).toFixed(2)}% · A max ${Number(config.maximum_contracts || 5)} ct · B max ${Number(config.spot_only_maximum_contracts || 3)} ct · ${money(config.sizing_bankroll_cap || 5000)} basis cap`);
      setText('crypto-spot-flow-pilot-daily', `${money(Math.max(0, -Number(risk.daily_realized_profit || 0)))} / ${money(risk.daily_loss_cap || 0)} · ${Number(risk.daily_entries || 0)} / ${Number(risk.daily_entry_cap || 25)}`);
      setText('crypto-spot-flow-pilot-open', `${money(risk.open_exposure || 0)} / ${money(risk.open_exposure_cap || 0)} · ${Number(risk.open_positions || 0)} / ${Number(config.maximum_open_positions || 4)}`);
      setText('crypto-spot-flow-pilot-review', `${Number(risk.live_settled || 0)} / ${Number(risk.live_review_settled_cap || 100)} settled`);
      const funnel = data.funnel || {};
      const blockers = (risk.reasons || []).map(reason => String(reason).replaceAll('_', ' ')).join(' · ');
      setText('crypto-spot-flow-pilot-funnel', `Registered ${shortTime(config.registered_at || '') || 'now'} · latest ${Number(funnel.reviewed || 0)} reviewed · ${Number(funnel['signal_matched:spot_flow_confirmed'] || 0)} Tier A · ${Number(funnel['signal_matched:spot_lead_only'] || 0)} Tier B · ${Number(data.eligible_candidates || 0)} order-eligible${blockers ? ' · ' + blockers : ''}`, 'shadow-note');
      renderRows('crypto-spot-flow-pilot-recent', data.recent_candidates || [], [
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => row.tier === 'spot_flow_confirmed' ? 'A · spot + flow' : 'B · spot only'},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => `${Number(row.entry_price || 0).toFixed(1)}c`},
        {num: true, render: row => `${Number(row.sizing?.contracts || 0)} / ${money(row.sizing?.principal_stake || 0)}`},
        {render: row => row.eligible ? 'ELIGIBLE' : String(row.reason || 'blocked').replaceAll('_', ' ')}
      ], 'No Tier A or Tier B spot-lead match in the latest scan.');
    }

    function renderCryptoComplementArb(data) {
      data = data || {};
      const running = data.mode === 'paper_shadow_only';
      setText('crypto-complement-arb-status', running ? 'STRICT-FILL SHADOW RUNNING' : String(data.mode || 'waiting').replaceAll('_', ' ').toUpperCase(), `health-chip ${running ? 'good' : 'warn'}`);
      setText('crypto-complement-arb-tracked', `${Number(data.tracked || 0)} / ${Number(data.working || 0)}`);
      setText('crypto-complement-arb-settled', `${Number(data.settled || 0)} / ${Number(data.observation_days || 0)}`);
      setText('crypto-complement-arb-fills', `${Number(data.both_legs_filled || 0)} / ${Number(data.single_leg_filled || 0)} / ${Number(data.no_fill || 0)}`);
      const profit = Number(data.profit || 0);
      setText('crypto-complement-arb-profit', `${money(profit)} / ${Number(data.roi || 0).toFixed(1)}%`, `value ${profit > 0 ? 'positive' : (Number(data.settled || 0) ? 'negative' : '')}`);
      const lower = data.day_cluster_inference?.lower_bound;
      setText('crypto-complement-arb-lower', lower === null || lower === undefined ? 'Collecting' : money(lower), `value ${Number(lower || 0) > 0 ? 'positive' : 'negative'}`);
      const parity = data.taker_parity_diagnostic || {};
      setText('crypto-complement-arb-parity', parity.minimum_two_leg_cost_cents === null || parity.minimum_two_leg_cost_cents === undefined ? 'Collecting' : `${Number(parity.minimum_two_leg_cost_cents).toFixed(2)}c · ${Number(parity.executable_locked_profit_observations || 0)} executable`);
      setText('crypto-complement-arb-review', data.final_review?.eligible ? 'FINAL · MANUAL ONLY' : (data.interim_review?.eligible ? 'INTERIM EVIDENCE' : 'COLLECTING'), `value ${data.interim_review?.eligible ? 'positive' : ''}`);
      const funnel = data.last_capture_funnel || {};
      const reasons = Object.entries(funnel).filter(([key]) => key.startsWith('reason:')).slice(0, 5)
        .map(([key, count]) => `${key.slice(7).replaceAll('_', ' ')}: ${Number(count || 0)}`).join(' · ');
      setText('crypto-complement-arb-funnel', `Registered ${shortTime(data.registered_at || '') || 'now'} · latest ${Number(funnel.reviewed || 0)} reviewed · ${Number(funnel.quality_eligible || 0)} valid two-sided books · ${Number(funnel.captured || 0)} new maker pairs${reasons ? ' · ' + reasons : ''}`, 'shadow-note');
      renderRows('crypto-complement-arb-recent', (data.recent_records || []).slice(0, 60), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {num: true, render: row => `${Number(row.yes?.limit_price_cents || 0).toFixed(1)}c / ${Number(row.no?.limit_price_cents || 0).toFixed(1)}c`},
        {num: true, render: row => money(row.both_fill_locked_profit_dollars || 0)},
        {render: row => String(row.fill_outcome || `${Number(row.fill_count || 0)}/2 working`).replaceAll('_', ' ')},
        {render: row => row.status === 'settled' ? String(row.market_result || '').toUpperCase() : 'OPEN'},
        {num: true, render: row => row.status === 'settled' ? money(row.virtual_profit || 0) : '—'}
      ], 'No complementary maker pair has qualified yet.');
    }

    function renderCryptoCycleShadow(data) {
      data = data || {};
      const config = data.configuration || {};
      const profit = Number(data.profit ?? data.virtual_profit ?? 0);
      const roi = Number(data.roi || 0);
      const mode = String(data.mode || 'waiting');
      const running = mode === 'paper_shadow_only';
      setText(
        'crypto-cycle-shadow-status',
        mode === 'paper_shadow_only' ? 'V6 SHADOW RUNNING' : mode.replaceAll('_', ' ').toUpperCase(),
        `health-chip ${running ? 'good' : ''}`
      );
      const evaluation = data.evaluation || {};
      setText('crypto-cycle-shadow-cycles', `${Number(evaluation.unique_settled_markets || 0)} / ${Number(evaluation.minimum_unique_markets || config.research_goal_markets || 100)}`);
      setText('crypto-cycle-shadow-finish', Number(data.cycles_resolved || 0));
      setText('crypto-cycle-shadow-open', `${Number(data.open || 0)} / ${Number(config.bot_count || 3)}`);
      setText('crypto-cycle-shadow-record', `${Number(data.wins || 0)}W / ${Number(data.losses || 0)}L`);
      setText('crypto-cycle-shadow-profit', money(profit), `value ${profit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-cycle-shadow-roi', `${roi.toFixed(1)}%`, `value ${roi >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-cycle-shadow-targets', Number(data.cycles_won || 0));
      setText('crypto-cycle-shadow-partial', Number(data.cycles_lost || 0));
      setText('crypto-cycle-shadow-risk-capped', `${Number(config.max_attempts || 4)} tries · ${Number(config.max_contracts || 3)} ct · ${money(config.max_attempt_cost_dollars || 1.5)}`);
      setText('crypto-cycle-shadow-profitable-cycles', `${Number(data.cycles_net_profitable || 0)} / ${Number(data.cycles_resolved || 0)}`);
      const lanePerformance = data.performance_by_lane || [];
      const early = lanePerformance.find(row => row.lane === 'early_flow_fade') || {};
      const late = lanePerformance.find(row => row.lane === 'late_convex') || {};
      setText('crypto-cycle-shadow-lane-roi', `${Number(early.roi || 0).toFixed(1)}% / ${Number(late.roi || 0).toFixed(1)}%`);
      const maker = data.maker_taker_counterfactual || {};
      const makerIncremental = Number(maker.incremental_profit || 0);
      setText('crypto-cycle-shadow-correlation', money(makerIncremental), `value ${makerIncremental >= 0 ? 'positive' : 'negative'}`);
      const flat = data.flat_stake_counterfactual || {};
      const flatProfit = Number(flat.profit || 0);
      const incremental = Number(flat.recovery_incremental_profit || 0);
      setText('crypto-cycle-shadow-flat-profit', money(flatProfit), `value ${flatProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-cycle-shadow-recovery-incremental', money(incremental), `value ${incremental >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-cycle-shadow-evaluation', `${Number(evaluation.unique_settled_markets || 0)} / ${Number(evaluation.minimum_unique_markets || 100)}`);
      const funnel = data.candidate_funnel || {};
      const reasons = Object.entries(funnel.reason_counts || {}).slice(0, 6)
        .map(([label, count]) => `${String(label).replaceAll('_', ' ')}: ${Number(count || 0)}`)
        .join(' · ');
      setText(
        'crypto-cycle-shadow-funnel',
        `Latest V6 scan: ${Number(funnel.scanned || 0)} reviewed · ${Number(funnel.eligible || 0)} lane-qualified · ${Number(funnel.captured || 0)} opened · one global position per expiry${reasons ? ' · blocked: ' + reasons : ''}`,
        'shadow-note'
      );
      const botTarget = Number(config.cycle_count || 100);
      const botContainer = document.getElementById('crypto-cycle-shadow-bots');
      if (botContainer) {
        const bots = data.bots || [];
        botContainer.innerHTML = bots.length ? bots.map((bot, index) => {
          const open = bot.open_position || {};
          const status = String(bot.status || 'waiting').replaceAll('_', ' ');
          const totalProfit = Number(bot.total_profit || 0);
          return `
            <div class="campaign-lane-card">
              <div class="campaign-lane-head"><strong>Shadow Bot ${Number(bot.bot_number || index + 1)}</strong><span class="health-chip ${status === 'active' ? 'good' : ''}">${esc(status.toUpperCase())}</span></div>
              <div class="campaign-lane-metrics">
                <span>Episode <b>${Number(bot.cycle_number || 1)} / ${botTarget}</b></span>
                <span>Attempt <b>${Number(bot.attempts || 0)} / ${Number(config.max_attempts || 4)}</b></span>
                <span>Episodes <b>${Number(bot.cycles_won || 0)}W-${Number(bot.cycles_lost || 0)}L</b></span>
                <span>Episode P/L <b>${money(bot.cycle_profit || 0)}</b></span>
                <span>Deficit <b>${money(bot.cycle_deficit || 0)}</b></span>
                <span>Loss room <b>${money(bot.loss_room_remaining || 0)}</b></span>
                <span>Total P/L <b class="${totalProfit >= 0 ? 'positive' : 'negative'}">${money(totalProfit)}</b></span>
                <span>Virtual equity <b>${money(bot.virtual_equity || 0)}</b></span>
                <span>Record <b>${Number(bot.wins || 0)}W-${Number(bot.losses || 0)}L</b></span>
                <span>Open <b>${open.ticker ? `${open.asset || ''} ${String(open.side || '').toUpperCase()} · ${String(open.lane || '').replaceAll('_', ' ')}` : 'waiting'}</b></span>
              </div>
            </div>`;
        }).join('') : '<div class="notice">The next crypto scan will initialize three shadow bots.</div>';
      }
      renderRows('crypto-cycle-shadow-attempt-performance', data.performance_by_attempt || [], [
        {render: row => `Attempt ${Number(row.attempt || 0)}`},
        {num: true, render: row => `${Number(row.wins || 0)}W-${Number(row.losses || 0)}L`},
        {num: true, render: row => money(row.cost || 0)},
        {num: true, render: row => money(row.profit || 0)},
        {num: true, render: row => `${Number(row.roi || 0).toFixed(1)}%`}
      ], 'No settled attempts yet.');
      renderRows('crypto-cycle-shadow-price-performance', data.performance_by_price_band || [], [
        {render: row => row.band || ''},
        {num: true, render: row => `${Number(row.wins || 0)}W-${Number(row.losses || 0)}L`},
        {num: true, render: row => money(row.cost || 0)},
        {num: true, render: row => money(row.profit || 0)},
        {num: true, render: row => `${Number(row.roi || 0).toFixed(1)}%`}
      ], 'No settled price bands yet.');
      setText(
        'crypto-cycle-cap-counterfactual-status',
        `${Number(maker.offered || 0)} early quotes offered · ${Number(maker.filled || 0)} public-trade fills · ${Number(maker.expired || 0)} expired · maker ${money(maker.maker_profit || 0)} vs matched taker ${money(maker.matched_taker_profit || 0)} · Δ ${money(maker.incremental_profit || 0)} · queue position not modeled · analytics only · no automatic promotion`,
        'shadow-note'
      );
      const discovery = data.positive_edge_discovery || {};
      const discoveryEvaluation = discovery.evaluation || {};
      const rawTaker = discovery.raw_model_taker || {};
      const anchoredTaker = discovery.market_anchored_taker || {};
      const discoveryMaker = discovery.maker_post_only || {};
      const decomposition = discovery.average_decomposition || {};
      const discoveryProfit = Number(rawTaker.profit || 0);
      const friction = Number(decomposition.ask_spread_drag_cents || 0)
        + Number(decomposition.book_walk_drag_cents || 0)
        + Number(decomposition.exact_fee_drag_cents || 0)
        + Number(decomposition.expected_slippage_drag_cents || 0);
      setText('crypto-edge-discovery-sample', `${Number(discovery.independent_expiries || 0)} / ${Number(discoveryEvaluation.minimum_independent_expiries || 100)}`);
      setText('crypto-edge-discovery-record', `${Number(discovery.wins || 0)}W / ${Number(discovery.losses || 0)}L`);
      setText('crypto-edge-discovery-raw-profit', money(discoveryProfit), `value ${discoveryProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-edge-discovery-raw-roi', `${Number(rawTaker.roi || 0).toFixed(1)}%`, `value ${Number(rawTaker.roi || 0) >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-edge-discovery-raw-edge', `${Number(rawTaker.average_expected_edge_cents || 0).toFixed(2)}c / ${Number(rawTaker.average_edge_low_cents || 0).toFixed(2)}c`);
      setText('crypto-edge-discovery-anchored-edge', `${Number(anchoredTaker.average_expected_edge_cents || 0).toFixed(2)}c`);
      setText('crypto-edge-discovery-guardrail', `${Number(decomposition.market_guardrail_adjustment_pp || 0).toFixed(2)}pp`);
      setText('crypto-edge-discovery-friction', `${friction.toFixed(2)}c`);
      setText('crypto-edge-discovery-maker', `${Number(discoveryMaker.filled || 0)} / ${money(discoveryMaker.profit || 0)}`);
      const discoveryFunnel = discovery.candidate_funnel || {};
      const discoveryReasons = Object.entries(discoveryFunnel.reason_counts || {}).slice(0, 6)
        .map(([label, count]) => `${String(label).replaceAll('_', ' ')}: ${Number(count || 0)}`)
        .join(' · ');
      const bestDiscoveryReview = (discoveryFunnel.best_reviewed || [])[0] || {};
      const bestDiscoveryText = bestDiscoveryReview.ticker
        ? ` · closest: ${bestDiscoveryReview.asset || ''} ${String(bestDiscoveryReview.side || '').toUpperCase()} ${Number(bestDiscoveryReview.entry_price_cents || 0).toFixed(0)}c · sync edge ${Number(bestDiscoveryReview.synchronized_edge_cents || 0).toFixed(2)}c · stress ${Number(bestDiscoveryReview.stress_edge_cents || 0).toFixed(2)}c · blocked by ${(bestDiscoveryReview.reasons || []).map(reason => String(reason).replaceAll('_', ' ')).join(', ') || 'none'}`
        : '';
      setText(
        'crypto-edge-discovery-funnel',
        `Latest V3 discovery scan: ${Number(discoveryFunnel.scanned || 0)} reviewed · ${Number(discoveryFunnel.qualified_snapshots || 0)} bounded-edge qualified · ${Number(discoveryFunnel.captured || 0)} independent expiry captured · ${Number(discovery.legacy_diagnostic_records || 0)} pre-V3 record(s) excluded${bestDiscoveryText}${discoveryReasons ? ' · blocked: ' + discoveryReasons : ''}`,
        'shadow-note'
      );
      renderRows('crypto-edge-discovery-recent', (discovery.recent_records || []).slice(0, 40), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => `${Number(row.entry_price_cents || 0).toFixed(1)}c`},
        {num: true, render: row => `${Number(row.edge_decomposition?.raw_model_probability || 0).toFixed(1)}% / ${Number(row.edge_decomposition?.market_anchored_probability || 0).toFixed(1)}%`},
        {num: true, render: row => `${Number(row.edge_decomposition?.executable_break_even_probability || 0).toFixed(1)}%`},
        {num: true, render: row => `${Number(row.edge_decomposition?.raw_model_edge_cents || 0).toFixed(2)}c / ${Number(row.edge_decomposition?.raw_model_edge_low_cents || 0).toFixed(2)}c`},
        {num: true, render: row => `${Number(row.edge_decomposition?.market_guardrail_adjustment_pp || 0).toFixed(2)}pp`},
        {num: true, render: row => `${Number(row.edge_decomposition?.ask_spread_drag_cents || 0).toFixed(2)}c / ${Number(row.edge_decomposition?.exact_fee_drag_cents || 0).toFixed(2)}c / ${Number(row.edge_decomposition?.expected_slippage_drag_cents || 0).toFixed(2)}c`},
        {render: row => String(row.maker_counterfactual?.status || 'n/a').replaceAll('_', ' ')},
        {render: row => row.result || 'OPEN'},
        {num: true, render: row => row.status === 'settled' ? money(row.raw_model_taker_profit || 0) : '--'}
      ], 'No positive-edge discovery records yet.');
      renderRows('crypto-cycle-shadow-recent', (data.recent_records || []).slice(0, 40), [
        {render: row => shortTime(row.settled_at || row.captured_at || '')},
        {render: row => `B${Number(row.bot_number || 0)} · C${Number(row.cycle_number || 0)}`},
        {render: row => row.asset || ''},
        {render: row => String(row.side || '').toUpperCase()},
        {render: row => `${String(row.lane || '').replaceAll('_', ' ')} · A${Number(row.attempt || 0)} ${String(row.attempt_type || '').replaceAll('_', ' ')}`},
        {num: true, render: row => `${Number(row.entry_price_cents || 0).toFixed(1)}c`},
        {num: true, render: row => Number(row.contracts || 0)},
        {num: true, render: row => money(row.total_cost_dollars || 0)},
        {num: true, render: row => `${Number(row.model_probability || 0).toFixed(1)}% / ${Number(row.market_midpoint || 0).toFixed(1)}%`},
        {num: true, render: row => Number(row.target_distance_sigma || 0).toFixed(2)},
        {num: true, render: row => `${Number(row.model_advantage_pp || 0).toFixed(2)}pp`},
        {render: row => String((row.maker_counterfactual || {}).status || 'n/a').replaceAll('_', ' ')},
        {render: row => row.result || 'OPEN'},
        {num: true, render: row => row.status === 'settled' ? `${money(row.virtual_profit || 0)} / ${money((row.flat_counterfactual || {}).virtual_profit || 0)}` : '--'}
      ], 'No virtual cycle attempts yet.');
    }

    function renderCryptoAnalytics(crypto) {
      const report = (crypto || {}).report || {};
      const analytics = (crypto || {}).analytics || report.analytics || {};
      const summary = analytics.candidate_summary || {};
      const rows = settledCryptoRows(crypto || {});
      const rows24 = recentRows(rows, 24);
      const openRows = (crypto || {}).open_bets || [];
      const totals = analyticsTotals(rows, openRows);
      const totals24 = analyticsTotals(rows24, []);
      const byAsset = summarizeRows(rows, row => row.asset || 'unknown');
      const byPrice = summarizeRows(rows, cryptoPriceBucket);
      const byKind = summarizeRows(rows, row => row.market_kind || 'unknown');
      const byConfidence = summarizeRows(rows, cryptoConfidenceBucket);
      const byEdge = summarizeRows(rows, cryptoEdgeBucket);
      const bySelective = summarizeRows(rows, cryptoSelectiveBucket);
      const byRecovery = summarizeRows(rows, cryptoRecoveryBucket);
      const skipRows = (analytics.candidate_by_skip_reason || []).slice(0, 14);
      const candidatePrice = cryptoCandidateRows(analytics, 'candidate_by_price_band');
      const candidateAsset = cryptoCandidateRows(analytics, 'candidate_by_asset');
      const candidateTime = cryptoCandidateRows(analytics, 'candidate_by_time_to_close');
      const funnelRows = [
        {label: 'Candidates', count: Number(summary.total || (report.top_candidates || []).length || 0)},
        {label: 'Eligible', count: Number(summary.eligible || 0)},
        {label: 'Skipped', count: Number(summary.skipped || 0)},
        {label: 'Placed', count: Number(summary.placed || report.placed_count || 0)},
        {label: 'Open', count: Number(summary.open || (crypto.open_bets_count || 0))},
        {label: 'Settled', count: Number(summary.settled || (crypto.settled_count || 0))}
      ];

      setText('crypto-analytics-settled', `${totals.settled} (${totals.wins}W/${totals.losses}L)`);
      setText('crypto-analytics-roi', `${totals.roi.toFixed(1)}%`, `value ${totals.roi >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-analytics-profit', money(totals.profit), `value ${totals.profit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-analytics-24h-profit', money(totals24.profit), `value ${totals24.profit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-analytics-open-risk', money(totals.openRisk));
      setText('crypto-analytics-avg-stake', money(totals.avgStake));
      setText('crypto-analytics-candidates', Number(summary.total || (report.top_candidates || []).length || 0));
      setText('crypto-analytics-eligible', Number(summary.eligible || 0));
      setText('crypto-analytics-placed', Number(summary.placed || report.placed_count || 0));
      setText('crypto-analytics-preferred', Number(summary.preferred_band || 0));
      setText('crypto-analytics-strong', Number(summary.strong_edge || 0));
      setText('crypto-analytics-elite', Number(summary.elite_edge || 0));
      const intelligence = report.scan_intelligence || {};
      const intelligenceStats = intelligence.stats || {};
      const intelligenceValidation = intelligence.validation || {};
      const dynamicPolicy = intelligence.dynamic_policy || {};
      const dynamicMetrics = dynamicPolicy.validation?.metrics || {};
      setText('crypto-intelligence-scans', Number(intelligenceStats.scan_count || 0));
      setText('crypto-intelligence-records', Number(intelligenceStats.candidate_records || intelligence.candidate_records || 0));
      setText('crypto-intelligence-lifecycle', Number(intelligenceStats.lifecycle_records || intelligence.lifecycle_records || 0));
      setText('crypto-intelligence-active', Number(intelligenceStats.active_records || intelligence.active_records || 0));
      setText(
        'crypto-intelligence-validation',
        String(intelligenceValidation.status || intelligence.status || 'collecting').replaceAll('_', ' ')
      );
      setText('crypto-dynamic-status', String(dynamicPolicy.status || 'shadow').replaceAll('_', ' '));
      setText(
        'crypto-dynamic-markets',
        `${Number(dynamicPolicy.independent_markets || 0)} / ${Number(dynamicPolicy.minimum_independent_markets || 100)}`
      );
      setText(
        'crypto-dynamic-roi',
        dynamicMetrics.roi === null || dynamicMetrics.roi === undefined
          ? '--'
          : `${(Number(dynamicMetrics.roi) * 100).toFixed(1)}%`,
        `value ${Number(dynamicMetrics.roi || 0) >= 0 ? 'positive' : 'negative'}`
      );
      renderRows('crypto-intelligence-recent', intelligence.recent_candidates || [], [
        {render: row => shortTime(row.scanned_at || '')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => row.pricing?.entry_price_cents == null ? '—' : `${Number(row.pricing.entry_price_cents).toFixed(1)}c`},
        {num: true, render: row => row.probability?.market_implied_yes == null ? '—' : `${Number(row.probability.market_implied_yes).toFixed(1)}%`},
        {num: true, render: row => row.probability?.final_yes == null ? '—' : `${Number(row.probability.final_yes).toFixed(1)}%`},
        {num: true, render: row => row.probability?.low_yes == null || row.probability?.high_yes == null ? '—' : `${Number(row.probability.low_yes).toFixed(1)}–${Number(row.probability.high_yes).toFixed(1)}%`},
        {num: true, render: row => row.pricing?.break_even_probability == null ? '—' : `${Number(row.pricing.break_even_probability).toFixed(2)}%`},
        {num: true, render: row => `${Number(row.pricing?.expected_edge_cents || 0).toFixed(2)}c / ${Number(row.pricing?.edge_low_cents || 0).toFixed(2)}c`},
        {num: true, render: row => `${Number(row.pricing?.probability_net_edge_positive || 0).toFixed(1)}%`},
        {num: true, render: row => `${(Number(row.data_quality?.score || 0) * 100).toFixed(0)}%`},
        {num: true, render: row => `${Number(row.unit_sizing?.target_units || 0).toFixed(1)}u`},
        {render: row => {
          const dynamic = row.dynamic_qualification || {};
          const units = Number(dynamic.target_units || 0);
          return units > 0
            ? `${dynamic.status === 'active' ? 'LIVE' : 'SHADOW'} ${units.toFixed(1)}u · ${String(dynamic.lane || '').replaceAll('_', ' ')}`
            : `No · ${String(dynamic.reason || 'filter').replaceAll('_', ' ')}`;
        }},
        {render: row => row.decision || (row.skip_reasons || []).join(', ') || 'eligible'},
        {render: row => (row.what_would_change_decision || []).join(' ')}
      ], 'The complete evidence card will appear after the next crypto scan.');
      const execution = analytics.execution_quality || {};
      const executionSummary = execution.summary || {};
      const executionMetric = (value, suffix, digits = 2) => (
        value === null || value === undefined ? '--' : `${Number(value).toFixed(digits)}${suffix}`
      );
      const averageSlippage = executionSummary.average_total_slippage_cents;
      const averagePostEdge = executionSummary.average_post_fill_net_edge;
      setText('crypto-execution-attempts', Number(executionSummary.attempts || 0));
      setText('crypto-execution-filled', Number(executionSummary.filled || 0));
      setText('crypto-execution-fill-rate', `${Number(executionSummary.fill_rate || 0).toFixed(1)}%`);
      setText('crypto-execution-quote-age', executionMetric(executionSummary.average_quote_age_seconds, 's', 2));
      setText(
        'crypto-execution-slippage',
        executionMetric(averageSlippage, 'c', 2),
        `value ${Number(averageSlippage || 0) <= 0 ? 'positive' : 'negative'}`
      );
      setText(
        'crypto-execution-post-edge',
        executionMetric(averagePostEdge, '%', 2),
        `value ${Number(averagePostEdge || 0) >= 2 ? 'positive' : 'negative'}`
      );
      setText('crypto-execution-improved', Number(executionSummary.price_improved_fills || 0));
      setText('crypto-execution-depth-blocks', Number(executionSummary.depth_blocks || 0));
      renderRows('crypto-execution-assets', execution.by_asset || [], [
        {render: row => row.label || 'unknown'},
        {num: true, render: row => Number(row.attempts || 0)},
        {num: true, render: row => `${Number(row.fill_rate || 0).toFixed(1)}%`},
        {num: true, render: row => executionMetric(row.average_total_slippage_cents, 'c', 2)},
        {num: true, render: row => executionMetric(row.average_post_fill_net_edge, '%', 2)},
        {num: true, render: row => Number(row.price_improved_fills || 0)}
      ], 'No execution attempts recorded yet.');
      renderCountTable('crypto-execution-failures', execution.failure_reasons || [], 'No execution blocks recorded yet.');
      renderRows('crypto-execution-recent', (execution.recent || []).slice(0, 50), [
        {render: row => shortTime(row.captured_at || '')},
        {render: row => `${row.asset || ''} · ${row.ticker || ''}`},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => executionMetric(row.initial_quote_cents, 'c', 1)},
        {num: true, render: row => executionMetric(row.refreshed_quote_cents, 'c', 1)},
        {num: true, render: row => executionMetric(row.fill_price_cents, 'c', 1)},
        {num: true, render: row => executionMetric(row.total_slippage_cents, 'c', 2)},
        {num: true, render: row => executionMetric(row.quote_age_seconds, 's', 2)},
        {num: true, render: row => executionMetric(row.post_fill_net_edge, '%', 2)},
        {render: row => row.outcome === 'filled' ? 'FILLED' : String(row.reason || row.outcome || '').replaceAll('_', ' ')}
      ], 'Price-capture records will appear after the next live execution opportunity.');
      const correlation = analytics.correlated_windows || {};
      const correlationSummary = correlation.summary || {};
      const correlationProfit = Number(correlationSummary.overlap_profit || 0);
      const correlationRoi = Number(correlationSummary.overlap_roi || 0);
      setText('crypto-correlation-windows', Number(correlationSummary.overlap_windows || 0));
      setText('crypto-correlation-record', `${Number(correlationSummary.overlap_wins || 0)}W / ${Number(correlationSummary.overlap_losses || 0)}L`);
      setText('crypto-correlation-profit', money(correlationProfit), `value ${correlationProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-correlation-roi', `${correlationRoi.toFixed(1)}%`, `value ${correlationRoi >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-correlation-same', Number(correlationSummary.same_direction_windows || 0));
      setText('crypto-correlation-mixed', Number(correlationSummary.mixed_direction_windows || 0));
      setText('crypto-correlation-exact', Number(correlationSummary.exact_contract_overlap_windows || 0));
      const correlationTable = document.getElementById('crypto-correlation-windows-table');
      if (correlationTable) {
        const correlationRows = (correlation.recent_windows || []).slice(0, 20);
        correlationTable.innerHTML = correlationRows.length
          ? correlationRows.map(row => `<tr>
              <td>${esc(shortTime(row.close_time || ''))}</td>
              <td>${esc((row.bot_numbers || []).map(number => `B${number}`).join(', '))}</td>
              <td>${esc((row.assets || []).join(', '))}</td>
              <td>${esc((row.sides || []).map(side => String(side).toUpperCase()).join(', '))}</td>
              <td class="num">${Number(row.wins || 0)}W / ${Number(row.losses || 0)}L</td>
              <td class="num">${money(row.stake || 0)}</td>
              <td class="num ${Number(row.roi || 0) >= 0 ? 'positive' : 'negative'}">${Number(row.roi || 0).toFixed(1)}%</td>
              <td class="num ${Number(row.profit || 0) >= 0 ? 'positive' : 'negative'}">${money(row.profit || 0)}</td>
            </tr>`).join('')
          : '<tr><td colspan="8">No multi-bot correlated windows yet.</td></tr>';
      }
      const recoveryCounterfactual = analytics.recovery_counterfactual || {};
      const recoveryCfSummary = recoveryCounterfactual.summary || {};
      const recoveryIncrementalProfit = Number(recoveryCfSummary.incremental_profit || 0);
      const recoveryIncrementalRoi = Number(recoveryCfSummary.incremental_roi || 0);
      const recoveryActualProfit = Number(recoveryCfSummary.actual_profit || 0);
      const recoveryBaseProfit = Number(recoveryCfSummary.counterfactual_base_profit || 0);
      setText('crypto-recovery-cf-settled', Number(recoveryCfSummary.settled || 0));
      setText('crypto-recovery-cf-actual-profit', money(recoveryActualProfit), `value ${recoveryActualProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-recovery-cf-base-profit', money(recoveryBaseProfit), `value ${recoveryBaseProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-recovery-cf-incremental-profit', money(recoveryIncrementalProfit), `value ${recoveryIncrementalProfit >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-recovery-cf-incremental-roi', `${recoveryIncrementalRoi.toFixed(1)}%`, `value ${recoveryIncrementalRoi >= 0 ? 'positive' : 'negative'}`);
      setText('crypto-recovery-cf-loss-streak', Number(recoveryCfSummary.maximum_incremental_loss_streak || 0));
      setText('crypto-recovery-cf-actual-drawdown', money(recoveryCfSummary.actual_max_drawdown || 0));
      setText('crypto-recovery-cf-base-drawdown', money(recoveryCfSummary.counterfactual_base_max_drawdown || 0));
      const recoveryCfSettled = Number(recoveryCfSummary.settled || 0);
      const recoveryCfOpen = Number(recoveryCfSummary.open_recovery_overlays || 0);
      const recoveryCfReserved = Number(recoveryCfSummary.reserved_recovery_profit || 0);
      const recoveryReservationText = recoveryCfOpen
        ? `${recoveryCfOpen} open recovery allocation${recoveryCfOpen === 1 ? '' : 's'} reserve ${money(recoveryCfReserved)} of win-profit target.`
        : 'No recovery target is currently reserved by an open wager.';
      setText(
        'crypto-recovery-counterfactual-status',
        recoveryCfSettled
          ? `${recoveryCfSettled} settled recovery add-on${recoveryCfSettled === 1 ? '' : 's'} compared with the same fills at base size only. Incremental P&L isolates the bonus units. ${recoveryReservationText}`
          : `No historical recovery add-on has settled yet. New recovery bonus stakes are disabled; pooled high-water tracking remains available for analytics. ${recoveryReservationText}`
      );
      const recoveryHours = value => (
        value === null || value === undefined ? 'â€”' : `${Number(value).toFixed(1)}h`
      );
      renderRows('crypto-recovery-cf-bots', recoveryCounterfactual.by_bot || [], [
        {render: row => `Bot ${Number(row.bot_number || 0)}`},
        {num: true, render: row => Number(row.overlay_bets || 0)},
        {num: true, render: row => money((row.actual || {}).current_drawdown || 0)},
        {num: true, render: row => money((row.counterfactual_base_only || {}).current_drawdown || 0)},
        {num: true, render: row => recoveryHours((row.actual || {}).average_recovery_hours)},
        {num: true, render: row => recoveryHours((row.counterfactual_base_only || {}).average_recovery_hours)},
        {num: true, render: row => recoveryHours((row.actual || {}).open_drawdown_hours)}
      ], 'No continuous recovery ledgers are available yet.');
      renderRows('crypto-recovery-cf-recent', recoveryCounterfactual.recent || [], [
        {render: row => shortTime(row.settled_at || '')},
        {render: row => `B${Number(row.bot_number || 0)} Â· ${row.asset || ''}`},
        {render: row => row.result || ''},
        {num: true, render: row => `${Number(row.base_units || 0).toFixed(2)}u`},
        {num: true, render: row => `+${Number(row.bonus_units || 0).toFixed(2)}u`},
        {num: true, render: row => money(row.actual_profit || 0)},
        {num: true, render: row => money(row.counterfactual_base_profit || 0)},
        {num: true, render: row => money(row.incremental_profit || 0)}
      ], 'No settled recovery add-ons yet.');
      renderShadowLab('crypto', report.shadow_algorithm || {});
      renderCryptoMarketRegime(report.market_regime || {});
      renderCryptoLowEdgeLivePilot(report.low_edge_live_pilot || {});
      renderCryptoLowEdgeShadow(report.low_edge_shadow || {});

      drawBarChart('chart-crypto-funnel', funnelRows, row => row.count, {axisLabel: 'Count', color: '#5aa2ff', empty: 'No crypto scan data yet.'});
      drawBarChart('chart-crypto-skips', skipRows, row => row.count, {axisLabel: 'Count', color: '#f0b35a', empty: 'No crypto skip data yet.'});
      drawProfitChart('chart-crypto-asset-profit', byAsset);
      drawSignedChart('chart-crypto-price-roi', byPrice, row => row.roi, {axisLabel: 'ROI %', format: value => `${value.toFixed(0)}%`, empty: 'No settled crypto data yet.'});
      drawBarChart('chart-crypto-confidence-win', byConfidence, row => row.winRate, {axisLabel: 'Win %', format: value => `${value.toFixed(0)}%`, color: '#43d17f', empty: 'No settled crypto data yet.'});
      drawSignedChart('chart-crypto-edge-roi', byEdge, row => row.roi, {axisLabel: 'ROI %', format: value => `${value.toFixed(0)}%`, empty: 'No settled crypto data yet.'});

      renderAnalyticsTable('crypto-analytics-asset', byAsset, true, true);
      renderAnalyticsTable('crypto-analytics-price', byPrice, true, true);
      renderAnalyticsTable('crypto-analytics-kind', byKind, true, true);
      renderCryptoCandidateTable('crypto-candidate-price', candidatePrice);
      renderCryptoCandidateTable('crypto-candidate-asset', candidateAsset);
      renderCryptoCandidateTable('crypto-candidate-time', candidateTime);
      renderCountTable('crypto-analytics-skips', skipRows, 'No crypto skip reasons yet.');
      renderAnalyticsTable('crypto-analytics-selective', bySelective, true, true);
      renderAnalyticsTable('crypto-analytics-recovery', byRecovery, true, true);

      const events = (crypto.events || []).slice(0, 40);
      const eventBox = document.getElementById('crypto-events');
      if (eventBox) {
        eventBox.innerHTML = events.length
          ? events.map(event => `<div class="event">${esc(compactCryptoEvent(event))}</div>`).join('')
          : '<div class="event">No crypto events yet.</div>';
      }
      const notices = (((crypto.log_analytics || {}).recent_notices) || []).slice(-30).reverse();
      const noticeBox = document.getElementById('crypto-analytics-notices');
      if (noticeBox) {
        noticeBox.innerHTML = notices.length
          ? notices.map(line => `<div class="logline">${esc(centralizeTextTimes(line))}</div>`).join('')
          : '<div class="logline">No crypto notices yet.</div>';
      }
    }

    function renderPerpsShadow(crypto) {
      const data = (crypto || {}).perps_shadow || {};
      const report = data.report || {};
      const pnl = Number(report.realized_profit || 0) + Number(report.unrealized_profit || 0);
      const paused = report.error === 'disabled';
      const executionEnabled = Boolean(report.execution_enabled);
      const mode = paused ? 'PAUSED' : (report.error ? 'DEGRADED' : (executionEnabled ? 'PAPER ACTIVE' : 'OBSERVING'));
      setText('perps-mode', mode, `value ${report.error ? 'negative' : 'shadow-value'}`);
      setText('perps-equity', money(report.equity || report.balance || 0));
      setText('perps-pnl', money(pnl), `value ${pnl >= 0 ? 'positive' : 'negative'}`);
      setText('perps-markets', Number(report.market_count || 0));
      setText('perps-eligible', `${Number(report.eligible_count || 0)} / ${Number(report.candidate_count || 0)}`);
      setText('perps-counts', `${Number(report.open_count || 0)} / ${Number(report.settled_count || 0)}`);
      setText('perps-record', `${Number(report.wins || 0)}W / ${Number(report.losses || 0)}L`);
      setText('perps-funding', money(report.total_funding_pnl || 0), `value ${Number(report.total_funding_pnl || 0) >= 0 ? 'positive' : 'negative'}`);
      const validation = report.validation_summary || {};
      setText('perps-validated', `${Number(validation.assets_passing || 0)} / ${Number(validation.assets_validated || 0)}`);
      setText('perps-walk-forward', `${Number(validation.median_win_rate || 0).toFixed(1)}% / ${Number(validation.median_avg_net_pct || 0).toFixed(3)}%`);
      const forecasts = report.forward_forecasts || {};
      setText('perps-forecast-counts', `${Number(forecasts.open_count || 0)} / ${Number(forecasts.settled_count || 0)}`);
      const settings = report.settings || (crypto || {}).settings || {};
      const pct = value => `${(Number(value || 0) * 100).toFixed(2)}%`;
      const strategyRows = [
        'Paper safety: isolated 1x simulation with a separate ledger and no authenticated live-order code.',
        `Entry: multi-model ensemble, score ${Number(settings.CRYPTO_PERPS_SHADOW_MIN_SIGNAL_SCORE || 1).toFixed(2)}+, confidence ${Number(settings.CRYPTO_PERPS_SHADOW_MIN_CONFIDENCE || 75).toFixed(0)}+, spread at most ${pct(settings.CRYPTO_PERPS_SHADOW_MAX_SPREAD_PCT || 0.0025)}, plus walk-forward validation.`,
        `Validated sizing: ${pct(settings.CRYPTO_PERPS_SHADOW_NOTIONAL_PCT || 0.02)} of paper balance, capped at ${money(settings.CRYPTO_PERPS_SHADOW_MAX_NOTIONAL || 25)}; ${Number(settings.CRYPTO_PERPS_SHADOW_MAX_OPEN || 3)} open max and ${Number(settings.CRYPTO_PERPS_SHADOW_MAX_SAME_DIRECTION || 2)} in one direction.`,
        `Probation lane: stronger unvalidated signals only, ${pct(settings.CRYPTO_PERPS_SHADOW_PROBATION_NOTIONAL_PCT || 0.0025)} of paper balance capped at ${money(settings.CRYPTO_PERPS_SHADOW_PROBATION_MAX_NOTIONAL || 2.5)}, one position maximum.`,
        `Exit: volatility stop, ${Number(settings.CRYPTO_PERPS_SHADOW_REWARD_RISK || 1.8).toFixed(1)}R target, trailing protection, opposite-signal exit, funding and fees, or ${Number(settings.CRYPTO_PERPS_SHADOW_MAX_HOLD_HOURS || 24).toFixed(0)}-hour time exit.`,
        'Promotion rule: paper P&L alone is not enough; model replay, forward forecasts, costs, drawdown, and sufficient trade count must all remain positive before considering live use.'
      ];
      const strategyBox = document.getElementById('perps-strategy');
      if (strategyBox) strategyBox.innerHTML = strategyRows.map(text => `<div class="insight">${esc(text)}</div>`).join('');
      const statusBox = document.getElementById('perps-status-note');
      if (statusBox && report.error && !paused) {
        statusBox.textContent = `Paper lab is temporarily degraded: ${report.error}. It still has no live-order path.`;
      }
      renderRows('perps-candidates', (report.top_candidates || []).slice(0, 20), [
        {render: row => row.asset || ''},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => Number(row.signal_score || 0).toFixed(2)},
        {num: true, render: row => Number(row.confidence || 0).toFixed(1)},
        {num: true, render: row => `${(Number(row.spread_pct || 0) * 100).toFixed(3)}%`},
        {num: true, render: row => `${Number(row.validation_win_rate || 0).toFixed(1)}% (${Number(row.validation_trades || 0)})`},
        {num: true, render: row => `${Number(row.validation_avg_net_pct || 0).toFixed(3)}%`},
        {render: row => row.eligible ? 'paper entry' : ((row.skip_reasons || []).join(', ') || 'watching')}
      ], 'Waiting for the first perpetuals shadow scan.');
      renderRows('perps-open', (report.open_positions || []).slice(0, 20), [
        {render: row => row.asset || ''},
        {render: row => String(row.side || '').toUpperCase()},
        {num: true, render: row => money(row.notional || 0)},
        {num: true, render: row => Number(row.entry_price || 0).toFixed(4)},
        {num: true, render: row => money(row.unrealized_pnl || 0)}
      ], 'No perps paper positions are open; the engine is waiting for an aligned signal.');
      renderRows('perps-history', (report.recent_history || []).slice().reverse().slice(0, 20), [
        {render: row => row.asset || ''},
        {render: row => row.result || ''},
        {render: row => row.exit_reason || ''},
        {num: true, render: row => money(row.profit || 0)},
        {num: true, render: row => Number(row.held_hours || 0).toFixed(1)}
      ], 'No perpetuals shadow positions have settled yet.');
      renderRows('perps-forecasts', forecasts.models || [], [
        {render: row => String(row.model || '').replace('_', ' ')},
        {num: true, render: row => `${Number(row.horizon_hours || 0)}h`},
        {num: true, render: row => `${Number(row.wins || 0)}W / ${Number(row.losses || 0)}L`},
        {num: true, render: row => `${Number(row.win_rate || 0).toFixed(1)}%`},
        {num: true, render: row => `${Number(row.avg_net_pct || 0).toFixed(3)}%`}
      ], 'Forward forecasts are active; results appear after the first 1h and 4h horizons mature.');
      renderRows('perps-historical-models', report.historical_model_summary || [], [
        {render: row => String(row.model || '').replaceAll('_', ' ')},
        {num: true, render: row => Number(row.trades || 0)},
        {num: true, render: row => `${Number(row.wins || 0)}W / ${Number(row.losses || 0)}L`},
        {num: true, render: row => `${Number(row.win_rate || 0).toFixed(1)}%`},
        {num: true, render: row => `${Number(row.avg_net_pct || 0).toFixed(3)}%`},
        {num: true, render: row => Number(row.profitable_assets || 0)}
      ], 'Historical specialist replay is waiting for sufficient candles.');
      const logBox = document.getElementById('perps-logs');
      if (logBox) {
        const logs = (data.logs || []).slice(0, 40);
        logBox.innerHTML = logs.length
          ? logs.map(line => `<div class="logline">${esc(centralizeTextTimes(line))}</div>`).join('')
          : '<div class="logline">No perpetuals paper activity yet.</div>';
      }
    }

    function renderAnalytics(sports, crypto) {
      const allRows = settledSportsRows(sports);
      const allOpenRows = sports.open_bets || [];
      const rows = allRows.filter(isRegularSportsBotBet);
      const rows24 = recentRows(rows, 24);
      const openRows = allOpenRows.filter(isRegularSportsBotBet);
      const sourceAnalytics = sports.source_analytics || {};
      const botLedger = sourceAnalytics.regular_bot || {};
      const missedRows = missedFillRows(sports);
      const missedOpenRows = sports.missed_fills_open || [];
      const recentBotTotals = analyticsTotals(rows, openRows);
      const recent24Totals = analyticsTotals(rows24, []);
      const totals = Object.keys(botLedger).length ? {
        stake: Number(botLedger.stake || 0),
        profit: Number(botLedger.profit || 0),
        settled: Number(botLedger.settled_count || 0),
        wins: Number(botLedger.wins || 0),
        losses: Number(botLedger.losses || 0),
        roi: Number(botLedger.roi || 0),
        avgStake: Number(botLedger.average_stake || 0),
        avgWin: Number(botLedger.average_win || 0),
        avgLoss: Number(botLedger.average_loss || 0),
        openRisk: Number(botLedger.open_exposure || 0),
      } : recentBotTotals;
      const botLast24 = botLedger.last_24h || {};
      const totals24 = Object.keys(botLast24).length ? {
        stake: Number(botLast24.stake || 0),
        profit: Number(botLast24.profit || 0),
        settled: Number(botLast24.settled_count || 0),
        wins: Number(botLast24.wins || 0),
        losses: Number(botLast24.losses || 0),
        roi: Number(botLast24.roi || 0),
      } : recent24Totals;
      const missedTotals = analyticsTotals(missedRows, missedOpenRows);
      const userOpenRows = allOpenRows.filter(isUserBet);
      const userRows = allRows.filter(isUserBet);
      const recentUserTotals = analyticsTotals(userRows, userOpenRows);
      const userLedger = sourceAnalytics.user_live || sports.user_bet_analytics || {};
      const userTotals = Object.keys(userLedger).length ? {
        ...recentUserTotals,
        stake: Number(userLedger.stake || 0),
        profit: Number(userLedger.profit || 0),
        settled: Number(userLedger.settled_count || 0),
        wins: Number(userLedger.wins || 0),
        losses: Number(userLedger.losses || 0),
        roi: Number(userLedger.roi || 0),
        openRisk: Number(userLedger.open_exposure || 0),
      } : recentUserTotals;
      const userBetRows = [
        ...userOpenRows.map(row => ({...row, user_bet_status: 'OPEN'})),
        ...userRows.map(row => ({...row, user_bet_status: row.result || 'SETTLED'}))
      ].sort((a, b) => (parseTime(b.settled_at || b.closed_at || b.placed_at) || 0) - (parseTime(a.settled_at || a.closed_at || a.placed_at) || 0)).slice(0, 20);
      const bySource = summarizeRows(rows, analyticsSource);
      const byStrategyOwner = summarizeRows(rows, strategyOwner);
      const byPhaseTwo = summarizeRows(rows, phaseTwoBucket);
      const byMissedOwner = summarizeRows(missedRows, strategyOwner);
      const byUserMarket = summarizeRows(
        userRows,
        row => row.market_type || (row.asset ? `crypto ${row.asset} 15m` : 'unknown')
      );
      const byConfidence = summarizeRows(rows, confidenceBucket);
      const byOdds = summarizeRows(rows, oddsBucket);
      const byMarket = summarizeRows(rows, row => row.market_type || 'unknown');
      const byTiming = summarizeRows(rows, timingBucket);
      const bySport = summarizeRows(rows, sportLabel);
      const byBot = summarizeRows(rows.filter(row => row.source === 'bot_pick' || row.bot_name), row => row.bot_name || row.bot_key || 'Bot pick');
      const byPrice = summarizeRows(rows, priceBucket);
      const byPreferredPrice = summarizeRows(rows, preferredPriceBandBucket);
      const byRecentPreferredPrice = summarizeRows(rows24, preferredPriceBandBucket);
      const byStake = summarizeRows(rows, stakeBucket);
      const byClv = summarizeRows(rows, clvBucket);
      const byPriceMarket = summarizeRows(rows, row => combinedBucket(row, priceBucket, r => r.market_type || 'unknown'));
      const byFinalMarket = summarizeRows(rows, row => combinedBucket(row, finalScoreBucket, r => r.market_type || 'unknown'));
      const byTimingMarket = summarizeRows(rows, row => combinedBucket(row, timingBucket, r => r.market_type || 'unknown'));
      const byOwnerMarket = summarizeRows(rows, row => combinedBucket(row, strategyOwner, r => r.market_type || 'unknown'));
      const reverse = reverseTotals(rows);
      const reverseSplits = [
        ...summarizeReverseRows(rows, strategyOwner).map(row => ({...row, label: `Owner: ${row.label}`})),
        ...summarizeReverseRows(rows, analyticsSource).map(row => ({...row, label: `Source: ${row.label}`})),
        ...summarizeReverseRows(rows, row => row.market_type || 'unknown').map(row => ({...row, label: `Market: ${row.label}`})),
        ...summarizeReverseRows(rows, timingBucket).map(row => ({...row, label: `Timing: ${row.label}`})),
        ...summarizeReverseRows(rows, oddsBucket).map(row => ({...row, label: `Odds: ${row.label}`})),
        ...summarizeReverseRows(rows, priceBucket).map(row => ({...row, label: `Price: ${row.label}`}))
      ].sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta) || b.settled - a.settled).slice(0, 14);
      const exposureRows = summarizeOpenExposure(openRows, row => `${sportLabel(row)} / ${row.market_type || 'unknown'}`);
      const bestWins = rows.filter(row => Number(row.profit || 0) > 0).sort((a, b) => Number(b.profit || 0) - Number(a.profit || 0)).slice(0, 8);
      const worstLosses = rows.filter(row => Number(row.profit || 0) < 0).sort((a, b) => Number(a.profit || 0) - Number(b.profit || 0)).slice(0, 8);
      const logAnalytics = sports.log_analytics || {};
      renderShadowLab('sports', ((sports || {}).report || {}).shadow_algorithm || {});

      document.getElementById('analytics-settled').textContent = `${totals.settled}`;
      document.getElementById('analytics-roi').textContent = `${totals.roi.toFixed(1)}%`;
      document.getElementById('analytics-roi').className = `value ${totals.roi >= 0 ? 'positive' : 'negative'}`;
      document.getElementById('analytics-avg-stake').textContent = money(totals.avgStake);
      document.getElementById('analytics-avg-win').textContent = money(totals.avgWin);
      document.getElementById('analytics-avg-loss').textContent = money(totals.avgLoss);
      document.getElementById('analytics-avg-loss').className = `value ${totals.avgLoss >= 0 ? 'positive' : 'negative'}`;
      document.getElementById('analytics-open-risk').textContent = money(totals.openRisk);
      document.getElementById('analytics-24h-settled').textContent = `${totals24.settled}`;
      document.getElementById('analytics-24h-roi').textContent = `${totals24.roi.toFixed(1)}%`;
      document.getElementById('analytics-24h-roi').className = `value ${totals24.roi >= 0 ? 'positive' : 'negative'}`;
      document.getElementById('analytics-24h-profit').textContent = money(totals24.profit);
      document.getElementById('analytics-24h-profit').className = `value ${totals24.profit >= 0 ? 'positive' : 'negative'}`;
      const focusSummary = byPreferredPrice.find(row => row.label === '35c-55c preferred') || {settled: 0, roi: 0};
      document.getElementById('analytics-focus-count').textContent = `${focusSummary.settled}`;
      document.getElementById('analytics-focus-roi').textContent = `${focusSummary.roi.toFixed(1)}%`;
      document.getElementById('analytics-focus-roi').className = `value ${focusSummary.roi >= 0 ? 'positive' : 'negative'}`;
      const unitAnalytics = ((sports.report || {}).unit_analytics || {});
      const executionQuality = unitAnalytics.execution_quality || {};
      const peakRisk = unitAnalytics.peak_concurrent_risk || {};
      const settledProfit = Number((botLedger.yesterday_settled || {}).profit || 0);
      const decisionProfit = Number((botLedger.yesterday_decisions || {}).profit || 0);
      setText('analytics-yesterday-settled', money(settledProfit), `value ${settledProfit >= 0 ? 'positive' : 'negative'}`);
      setText('analytics-yesterday-decisions', money(decisionProfit), `value ${decisionProfit >= 0 ? 'positive' : 'negative'}`);
      setText('analytics-peak-units', `${Number(peakRisk.peak_open_units || 0)}u`);
      setText('analytics-state-quality', `${Number(executionQuality.authoritative_state_pct || 0).toFixed(1)}%`);
      setText('analytics-ai-latency', executionQuality.average_ai_latency_ms === null || executionQuality.average_ai_latency_ms === undefined ? 'n/a' : `${(Number(executionQuality.average_ai_latency_ms) / 1000).toFixed(1)}s`);
      setText('analytics-edge-retention', executionQuality.average_edge_retention_pct === null || executionQuality.average_edge_retention_pct === undefined ? 'n/a' : `${Number(executionQuality.average_edge_retention_pct).toFixed(1)}%`);
      setText('user-bet-open-risk', money(userTotals.openRisk));
      setText('user-bet-record', `${userTotals.wins}W/${userTotals.losses}L`);
      setText('user-bet-profit', money(userTotals.profit), `value ${userTotals.profit >= 0 ? 'positive' : 'negative'}`);
      setText('user-bet-roi', `${userTotals.roi.toFixed(1)}%`, `value ${userTotals.roi >= 0 ? 'positive' : 'negative'}`);
      document.getElementById('reverse-record').textContent = `${reverse.reverseWins}W/${reverse.reverseLosses}L`;
      document.getElementById('reverse-profit').textContent = money(reverse.reverseProfit);
      document.getElementById('reverse-profit').className = `value ${reverse.reverseProfit >= 0 ? 'positive' : 'negative'}`;
      document.getElementById('reverse-roi').textContent = `${reverse.reverseRoi.toFixed(1)}%`;
      document.getElementById('reverse-roi').className = `value ${reverse.reverseRoi >= 0 ? 'positive' : 'negative'}`;
      document.getElementById('reverse-delta').textContent = money(reverse.delta);
      document.getElementById('reverse-delta').className = `value ${reverse.delta >= 0 ? 'positive' : 'negative'}`;
      document.getElementById('missed-fill-record').textContent = `${missedTotals.wins || 0}W/${missedTotals.losses || 0}L`;
      document.getElementById('missed-fill-profit').textContent = money(missedTotals.profit);
      document.getElementById('missed-fill-profit').className = `value ${missedTotals.profit >= 0 ? 'positive' : 'negative'}`;
      document.getElementById('missed-fill-roi').textContent = `${missedTotals.roi.toFixed(1)}%`;
      document.getElementById('missed-fill-roi').className = `value ${missedTotals.roi >= 0 ? 'positive' : 'negative'}`;
      document.getElementById('missed-fill-open').textContent = `${missedOpenRows.length}`;

      renderAnalyticsTable('analytics-source', bySource);
      renderAnalyticsTable('analytics-strategy-owner', byStrategyOwner, true, true);
      renderAnalyticsTable('analytics-phase-two', byPhaseTwo, true, true);
      renderAnalyticsTable('analytics-missed-fill', byMissedOwner, true, true);
      renderAnalyticsTable('analytics-user-market', byUserMarket, true, true);
      renderRows('analytics-user-bets', userBetRows, [
        {render: row => betLabel(row)},
        {render: row => row.user_bet_status || row.result || row.status || 'open'},
        {num: true, render: row => String(row.side || row.order_side || '').toUpperCase()},
        {num: true, render: row => money(row.stake)},
        {num: true, render: row => row.entry_price !== undefined && row.entry_price !== null ? `${Number(row.entry_price).toFixed(1)}c` : ''},
        {num: true, render: row => row.result ? money(row.profit) : 'open'}
      ], 'No user-tagged bets yet.');
      renderAnalyticsTable('analytics-confidence', byConfidence);
      renderAnalyticsTable('analytics-odds', byOdds);
      renderAnalyticsTable('analytics-market', byMarket, true, true);
      renderAnalyticsTable('analytics-timing', byTiming, true, true);
      renderAnalyticsTable('analytics-sport', bySport, true, true);
      const unitBreakdown = ((sports.report || {}).unit_analytics || {}).by_sport_market_units || [];
      renderRows('analytics-sport-market-units', unitBreakdown, [
        {render: row => sportLabel(row)},
        {render: row => String(row.market_type || 'unknown').replaceAll('_', ' ')},
        {num: true, render: row => `${Number(row.units || 0)}u`},
        {num: true, render: row => String(row.bets || 0)},
        {num: true, render: row => `${row.wins || 0}W/${row.losses || 0}L`},
        {num: true, render: row => row.average_edge === null || row.average_edge === undefined ? `n/a (n=${row.edge_count || 0})` : `${Number(row.average_edge).toFixed(2)}% (n=${row.edge_count || 0})`},
        {num: true, render: row => row.average_clv_5m_cents === null || row.average_clv_5m_cents === undefined ? `n/a (n=${row.clv_5m_count || 0})` : `${Number(row.average_clv_5m_cents) >= 0 ? '+' : ''}${Number(row.average_clv_5m_cents).toFixed(2)}c (n=${row.clv_5m_count || 0})${row.negative_clv_flag ? ' ⚠ review' : ''}`},
        {num: true, render: row => money(row.stake)},
        {num: true, render: row => `${Number(row.roi || 0).toFixed(1)}%`},
        {num: true, render: row => money(row.profit)}
      ], 'No settled unit-tagged sports bets yet.');
      renderAnalyticsTable('analytics-bot', byBot, true, true);
      renderAnalyticsTable('analytics-price', byPrice, true, true);
      renderAnalyticsTable('analytics-price-focus', byPreferredPrice, true, true);
      renderAnalyticsTable('analytics-24h-price-focus', byRecentPreferredPrice, true, true);
      renderPriceSkipTable('analytics-price-skips', priceSkipRows(sports));
      renderAnalyticsTable('analytics-stake', byStake, true, true);
      renderAnalyticsTable('analytics-clv', byClv, true, true);
      renderAnalyticsTable('analytics-price-market', byPriceMarket, true, true);
      renderAnalyticsTable('analytics-final-market', byFinalMarket, true, true);
      renderAnalyticsTable('analytics-timing-market', byTimingMarket, true, true);
      renderAnalyticsTable('analytics-owner-market', byOwnerMarket, true, true);
      renderReverseTable('analytics-reverse', reverseSplits);
      renderExposureTable('analytics-exposure', exposureRows);
      renderCountTable('analytics-log-categories', (logAnalytics.categories || []).slice(0, 12));
      renderCountTable('analytics-skip-reasons', logAnalytics.skip_reasons || []);
      renderCountTable('analytics-diagnostics', logAnalytics.diagnostics || []);
      renderRows('analytics-best-wins', bestWins, [
        {render: row => betLabel(row)},
        {num: true, render: row => money(row.stake)},
        {num: true, render: row => money(row.profit)},
        {render: row => sourceLabel(row)}
      ], 'No winning sports bets yet.');
      renderRows('analytics-worst-losses', worstLosses, [
        {render: row => betLabel(row)},
        {num: true, render: row => money(row.stake)},
        {num: true, render: row => money(row.profit)},
        {render: row => sourceLabel(row)}
      ], 'No losing sports bets yet.');
      renderRows('analytics-missed-fill-latest', missedRows.slice(0, 12), [
        {render: row => betLabel(row)},
        {num: true, render: row => row.result},
        {num: true, render: row => money(row.stake || row.requested_stake)},
        {num: true, render: row => money(row.profit)},
        {render: row => strategyOwner(row)}
      ], 'No settled missed fills yet.');
      const usage = logAnalytics.latest_usage || {};
      const oddsProjection = logAnalytics.odds_monthly_projection || {};
      const scan = logAnalytics.latest_scan || {};
      const insightRows = [
        logAnalytics.line_count ? `Analyzed ${logAnalytics.line_count} recent sports log lines.` : 'No sports log lines analyzed yet.',
        usage.monthly_used !== undefined ? `Odds API latest usage: ${usage.monthly_used}/${usage.monthly_limit} monthly credits, provider quota remaining ${usage.quota_remaining}.` : '',
        oddsProjection.projected !== undefined
          ? `Odds API provider-cycle projection: ${oddsProjection.projected} credits (${String(oddsProjection.status || 'on_track').replaceAll('_', ' ')}); monitoring target ${oddsProjection.monitoring_target || oddsProjection.limit}, soft pacing target ${oddsProjection.pacing_target || 4750000}, hard boundary ${oddsProjection.hard_limit || 5000000}; current pace ${oddsProjection.daily_rate}/day, soft-target pace ${oddsProjection.safe_daily_rate}/day, paid-refresh pacing ${Number(oddsProjection.paid_refresh_multiplier || 1).toFixed(2)}x.`
          : '',
        scan.candidates !== undefined ? `Latest scan: ${scan.candidates} candidates, ${scan.placed} placed, balance ${money(scan.balance)}.` : '',
        ...(logAnalytics.recent_notices || []).slice(-8)
      ].filter(Boolean);
      document.getElementById('analytics-insights').innerHTML = insightRows.length
        ? insightRows.map(text => `<div class="insight">${esc(text)}</div>`).join('')
        : '<div class="insight">No notable log patterns yet.</div>';
      const strategyInsightRows = [
        byStrategyOwner.length ? `Best owner by profit: ${byStrategyOwner.slice().sort((a, b) => b.profit - a.profit)[0].label} at ${money(byStrategyOwner.slice().sort((a, b) => b.profit - a.profit)[0].profit)}.` : 'No settled strategy-owner data yet.',
        byStrategyOwner.length ? `Most-used owner: ${byStrategyOwner[0].label} with ${byStrategyOwner[0].settled} settled bets.` : '',
        byStrategyOwner.find(row => row.label === 'live_campaign') ? `Live campaign P/L: ${money(byStrategyOwner.find(row => row.label === 'live_campaign').profit)}.` : '',
        byStrategyOwner.find(row => row.label === 'manual_bet_more') ? `Manual Bet More rows include top-ups, so those results reflect the whole topped-up position.` : ''
      ].filter(Boolean);
      document.getElementById('strategy-owner-insights').innerHTML = strategyInsightRows
        .map(text => `<div class="insight">${esc(text)}</div>`)
        .join('');
      const phaseTwoState = sports.live_campaign || (sports.report || {}).live_campaign || {};
      const phaseTwoBots = sportsCampaignBots(phaseTwoState);
      const phaseTwoRows = rows.filter(row => String(phaseTwoBucket(row)).startsWith('Live Campaign'));
      const phaseTwoSummary = summarizeRows(phaseTwoRows, row => 'Live Campaign')[0];
      const campaignUnitAnalytics = (sports.report || {}).unit_analytics || {};
      const phaseTwoInsightRows = [
        `Status: ${String(phaseTwoState.status || 'unknown').replaceAll('_', ' ')}. ${phaseTwoState.configured_bot_count || phaseTwoBots.length} isolated campaign bots; ${phaseTwoState.available_bot_count ?? 0} currently available.`,
        `Portfolio P/L: ${money(phaseTwoState.realized_profit || 0)}; active positions ${phaseTwoState.bot_live_active_open_count ?? phaseTwoState.bot_live_open_count ?? 0}/${phaseTwoState.max_open || 1}; total unresolved ${phaseTwoState.bot_live_open_count || 0}.`,
        ...phaseTwoBots.map((bot, index) => `Bot ${bot.bot_number || index + 1}: always-on units, active ${bot.bot_live_active_open_count || 0}/${bot.max_open || 1}, today P/L ${money(bot.bot_daily_realized_profit || 0)}, loss room ${money(bot.daily_loss_remaining || 0)}.`),
        phaseTwoSummary ? `Settled campaign bets: ${phaseTwoSummary.wins}W/${phaseTwoSummary.losses}L, ROI ${phaseTwoSummary.roi.toFixed(1)}%, P/L ${money(phaseTwoSummary.profit)}.` : 'No live campaign bets have settled yet.',
        campaignUnitAnalytics.settled_bets ? `Unit results: ${Number(campaignUnitAnalytics.profit_units || 0).toFixed(2)}u profit from ${campaignUnitAnalytics.settled_units_risked || 0}u risked; ${campaignUnitAnalytics.open_units || 0}u currently open.` : 'Unit performance analytics will populate as tagged bets settle.',
        phaseTwoRows.length ? `Average campaign stake ${money(phaseTwoRows.reduce((sum, row) => sum + Number(row.stake || 0), 0) / phaseTwoRows.length)}.` : ''
      ].filter(Boolean);
      document.getElementById('phase-two-insights').innerHTML = phaseTwoInsightRows
        .map(text => `<div class="insight">${esc(text)}</div>`)
        .join('');
      const reverseInsightRows = [
        reverse.count ? `Reverse test uses ${reverse.count} settled bets with valid Kalshi entry prices and assumes the opposite side was available at ${'100 - entry'} cents.` : 'No valid settled entries for reverse testing yet.',
        reverse.count ? `Original P/L ${money(reverse.originalProfit)} vs reverse P/L ${money(reverse.reverseProfit)}; reverse delta ${money(reverse.delta)}.` : '',
        reverse.delta > 0 ? 'Reversing has outperformed on this loaded sample. That is a signal to inspect the worst original splits, not an automatic reason to flip every bet.' : '',
        reverse.delta < 0 ? 'Original direction is still outperforming the reverse simulation on this loaded sample.' : '',
        reverseSplits.length ? `Largest reverse split: ${reverseSplits[0].label} delta ${money(reverseSplits[0].delta)}.` : ''
      ].filter(Boolean);
      document.getElementById('reverse-insights').innerHTML = reverseInsightRows
        .map(text => `<div class="insight">${esc(text)}</div>`)
        .join('');

      const theme = chartTheme();
      drawBarChart('chart-source', bySource, row => row.winRate, {axisLabel: 'Win rate %', format: value => `${value.toFixed(0)}%`});
      drawBarChart('chart-confidence', byConfidence, row => row.winRate, {color: theme.good, axisLabel: 'Win rate %', format: value => `${value.toFixed(0)}%`});
      drawProfitChart('chart-odds', byOdds);
      drawSignedChart('chart-market-profit', byMarket, row => row.profit, {axisLabel: 'Profit $', format: value => money(value)});
      drawSignedChart('chart-timing-roi', byTiming, row => row.roi, {axisLabel: 'ROI %', format: value => `${value.toFixed(0)}%`});
      drawBarChart('chart-log-patterns', (logAnalytics.categories || []).slice(0, 8), row => row.count, {color: '#f0b35a', axisLabel: 'Count'});
    }

    const settingIds = [...document.querySelectorAll('[data-view="control"] input[id], [data-view="control"] select[id]')]
      .map(el => el.id).filter(id => !id.startsWith('CONTROL_'));
    const cryptoSettingIds = [...document.querySelectorAll('[data-view="crypto-control"] input[id], [data-view="crypto-control"] select[id]')]
      .map(el => el.id);
    const secretIds = settingIds.filter(id => document.getElementById(id).type === 'password');
    const controlCryptoBotSettingMap = {
      CONTROL_CRYPTO_15M_CAMPAIGN_BOT_COUNT: 'CRYPTO_15M_CAMPAIGN_BOT_COUNT',
    };
    const cryptoSecretIds = ['CRYPTO_OPENAI_API_KEY', 'CRYPTO_GROK_API_KEY', 'CRYPTO_NEWS_API_KEY', 'CRYPTO_CRYPTOPANIC_API_KEY'];
    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload || {})
      });
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }

    let currentSettings = {};
    const betMoreState = {};

    function betMoreAction(b) {
      if (String(currentSettings.SPORTS_BET_MORE_ENABLED || 'false') !== 'true') return '';
      const ticker = b.kalshi_ticker || '';
      if (!ticker || b.kalshi_matched === false) return '';
      const id = ticker.replace(/[^a-zA-Z0-9]/g, '_');
      const state = betMoreState[ticker] || {};
      const stake = state.stake ?? '';
      const maxMove = state.maxMove ?? document.getElementById('SPORTS_BET_MORE_MAX_PRICE_MOVE_CENTS')?.value ?? '20';
      const status = state.status ?? 'quote first';
      const statusClass = state.statusClass ?? 'muted';
      return `
        <div class="row-actions" data-ticker="${esc(ticker)}">
          <button type="button" onclick="quoteBetMore(this)">Get Live Odds</button>
          <span id="quote_${id}" class="${esc(statusClass)}">${esc(status)}</span>
          <input id="stake_${id}" inputmode="decimal" placeholder="$" value="${esc(stake)}" oninput="updateBetMoreState(this)">
          <input id="move_${id}" inputmode="numeric" value="${esc(maxMove)}" title="Max cents away from original entry" oninput="updateBetMoreState(this)">
          <button type="button" onclick="submitBetMore(this)">Bet More</button>
        </div>`;
    }

    function betMoreTickerId(el) {
      const wrap = el.closest('.row-actions');
      const ticker = wrap ? wrap.dataset.ticker : '';
      const id = ticker.replace(/[^a-zA-Z0-9]/g, '_');
      return {ticker, id};
    }

    function updateBetMoreState(el, patch) {
      const {ticker, id} = betMoreTickerId(el);
      if (!ticker) return;
      const status = document.getElementById(`quote_${id}`);
      betMoreState[ticker] = {
        ...(betMoreState[ticker] || {}),
        stake: document.getElementById(`stake_${id}`)?.value || '',
        maxMove: document.getElementById(`move_${id}`)?.value || '',
        status: status?.textContent || betMoreState[ticker]?.status || 'quote first',
        statusClass: status?.className || betMoreState[ticker]?.statusClass || 'muted',
        ...(patch || {})
      };
    }

    async function quoteBetMore(button) {
      const {ticker, id} = betMoreTickerId(button);
      const status = document.getElementById(`quote_${id}`);
      if (!ticker || !status) return;
      status.textContent = 'checking...';
      status.className = 'muted';
      updateBetMoreState(button, {status: 'checking...', statusClass: 'muted'});
      try {
        const maxMove = document.getElementById(`move_${id}`)?.value || 20;
        const result = await postJson('/api/sports/bet-more/quote', {ticker, max_move_cents: maxMove});
        const price = result.current_price === null || result.current_price === undefined ? 'N/A' : `${Number(result.current_price).toFixed(0)}c`;
        const move = result.price_move_cents === null || result.price_move_cents === undefined ? 'N/A' : `${Number(result.price_move_cents).toFixed(0)}c`;
        const american = result.current_american === null || result.current_american === undefined ? '' : ` (${Number(result.current_american) > 0 ? '+' : ''}${result.current_american})`;
        status.textContent = `${(result.side || '').toUpperCase()} ${price}${american}; move ${move}; ${result.within_price_guard ? 'ok' : 'moved'}`;
        status.className = result.within_price_guard ? 'positive' : 'negative';
        updateBetMoreState(button, {status: status.textContent, statusClass: status.className});
      } catch (err) {
        status.textContent = err.message || 'quote failed';
        status.className = 'negative';
        updateBetMoreState(button, {status: status.textContent, statusClass: status.className});
      }
    }

    async function submitBetMore(button) {
      const {ticker, id} = betMoreTickerId(button);
      const status = document.getElementById(`quote_${id}`);
      if (!ticker || !status) return;
      const stake = document.getElementById(`stake_${id}`)?.value || '';
      const maxMove = document.getElementById(`move_${id}`)?.value || 20;
      status.textContent = 'placing...';
      status.className = 'muted';
      updateBetMoreState(button, {status: 'placing...', statusClass: 'muted'});
      try {
        const result = await postJson('/api/sports/bet-more/order', {ticker, stake, max_move_cents: maxMove});
        if (!result.ok) {
          status.textContent = result.message || result.error || 'blocked';
          status.className = 'negative';
          updateBetMoreState(button, {status: status.textContent, statusClass: status.className});
          return;
        }
        const requested = result.order.requested_stake === undefined ? null : Number(result.order.requested_stake);
        const capped = result.order.capped_requested_stake === undefined ? null : Number(result.order.capped_requested_stake);
        const filled = Number(result.order.actual_stake || 0);
        const capNote = capped !== null && requested !== null && capped + 0.001 < requested
          ? `; requested ${money(requested)} capped ${money(capped)}`
          : '';
        status.textContent = `placed ${money(filled)} @ ${result.order.price}c${capNote}`;
        status.className = 'positive';
        updateBetMoreState(button, {status: status.textContent, statusClass: status.className, stake: ''});
        await refresh();
      } catch (err) {
        status.textContent = err.message || 'order failed';
        status.className = 'negative';
        updateBetMoreState(button, {status: status.textContent, statusClass: status.className});
      }
    }

    async function saveChangedSettings(ids, url, statusId) {
      const payload = {};
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el?.dataset.dirty === 'true') payload[id] = el.value;
      }
      const status = document.getElementById(statusId);
      const count = Object.keys(payload).length;
      if (!count) {
        status.textContent = 'No changed settings to save.';
        return true;
      }
      try {
        await postJson(url, payload);
      } catch (error) {
        status.textContent = `Save failed: ${error.message || error}. Your edits are still here.`;
        return false;
      }
      for (const [id, value] of Object.entries(payload)) {
        const el = document.getElementById(id);
        // Keep any edit made while the request was in flight.
        if (el?.value === value) el.dataset.dirty = '';
      }
      status.textContent = `Saved ${count} changed setting${count === 1 ? '' : 's'}. Bot counts apply on the next scan; other loop settings may require a restart.`;
      await refresh();
      return true;
    }

    async function saveSettings() {
      return saveChangedSettings(settingIds, '/api/settings', 'settings-status');
    }

    async function saveCryptoSettings() {
      return saveChangedSettings(cryptoSettingIds, '/api/crypto/settings', 'crypto-settings-status');
    }

    async function saveCryptoBotConfiguration() {
      for (const [controlId, settingId] of Object.entries(controlCryptoBotSettingMap)) {
        const control = document.getElementById(controlId);
        const setting = document.getElementById(settingId);
        if (control && setting && control.dataset.dirty === 'true') {
          setting.value = control.value;
          setting.dataset.dirty = 'true';
        }
      }
      if (!await saveChangedSettings(Object.values(controlCryptoBotSettingMap), '/api/crypto/settings', 'control-crypto-bot-status')) return;
      for (const [controlId, settingId] of Object.entries(controlCryptoBotSettingMap)) {
        const control = document.getElementById(controlId);
        const setting = document.getElementById(settingId);
        if (control?.value === setting?.value && setting?.dataset.dirty !== 'true') control.dataset.dirty = '';
      }
    }

    async function controlProcess(name, action) {
      await postJson(`/api/process/${action}`, {name});
      await refresh();
    }

    async function rerunBotPicks() {
      const status = document.getElementById('settings-status');
      status.textContent = 'Rerunning bot picks and checking Kalshi matches...';
      try {
        const result = await postJson('/api/sports/rerun-bot-picks', {});
        const tail = (result.logs || []).slice(-3).join(' | ');
        status.textContent = `Bot rerun finished rc=${result.returncode}; ${tail || 'check sports log for details'}`;
      } catch (err) {
        status.textContent = `Bot rerun failed: ${err.message}`;
      }
      await refresh();
    }

    async function resetOdds() {
      const result = await postJson('/api/odds/reset', {});
      document.getElementById('settings-status').textContent = `Reset odds state: ${result.removed.length ? result.removed.join(', ') : 'nothing to clear'}`;
      await refresh();
    }

    async function toggleSecret(id) {
      const el = document.getElementById(id);
      const btn = document.getElementById(`${id}_toggle`);
      if (el.type === 'password') {
        if (!el.value) {
          const result = await postJson('/api/settings/reveal', {key: id});
          el.value = result.value || '';
        }
        el.type = 'text';
        el.dataset.revealed = 'true';
        btn.textContent = 'Hide';
      } else {
        el.type = 'password';
        el.dataset.revealed = '';
        el.value = '';
        btn.textContent = '👁';
      }
    }

    async function toggleCryptoSecret(id) {
      const el = document.getElementById(id);
      const btn = document.getElementById(`${id}_toggle`);
      if (el.type === 'password') {
        if (!el.value) {
          const result = await postJson('/api/crypto/settings/reveal', {key: id});
          el.value = result.value || '';
        }
        el.type = 'text';
        el.dataset.revealed = 'true';
        btn.textContent = 'Hide';
      } else {
        el.type = 'password';
        el.dataset.revealed = '';
        el.value = '';
        btn.textContent = 'show';
      }
    }

    function renderSettings(settings) {
      if (!settings) return;
      for (const id of settingIds) {
        const el = document.getElementById(id);
        if (!el) continue;
        if (document.activeElement === el || el.dataset.dirty === 'true') continue;
        if (secretIds.includes(id) && el.dataset.revealed === 'true') continue;
        const value = settings[id] || '';
        if ((id.endsWith('API_KEY') || id.includes('SECRET') || id.includes('PRIVATE')) && value.startsWith('saved')) {
          el.placeholder = value;
          el.value = '';
          el.type = 'password';
        } else {
          el.value = value;
        }
      }
    }

    function renderCryptoControlSettings(settings) {
      if (!settings) return;
      for (const id of cryptoSettingIds) {
        const el = document.getElementById(id);
        if (!el) continue;
        if (document.activeElement === el || el.dataset.dirty === 'true') continue;
        if (cryptoSecretIds.includes(id) && el.dataset.revealed === 'true') continue;
        const value = settings[id] || '';
        if (cryptoSecretIds.includes(id) && String(value).startsWith('saved')) {
          el.placeholder = value;
          el.value = '';
          el.type = 'password';
        } else {
          el.value = value;
        }
      }
      for (const [controlId, settingId] of Object.entries(controlCryptoBotSettingMap)) {
        const el = document.getElementById(controlId);
        if (!el || document.activeElement === el || el.dataset.dirty === 'true') continue;
        el.value = settings[settingId] ?? '';
      }
    }

    for (const id of settingIds) {
      window.addEventListener('load', () => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', () => {
          el.dataset.dirty = 'true';
          if (id === 'SPORTS_UNIT_SIZE_PCT') renderSportsUnitSizePreview();
        });
      });
    }
    for (const id of cryptoSettingIds) {
      window.addEventListener('load', () => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', () => { el.dataset.dirty = 'true'; });
      });
    }
    for (const id of Object.keys(controlCryptoBotSettingMap)) {
      window.addEventListener('load', () => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', () => { el.dataset.dirty = 'true'; });
      });
    }

    function renderProcesses(processes, localAI) {
      const box = document.getElementById('processes');
      const rows = Object.values(processes || {});
      const processHtml = rows.map(p => `
        <div class="process-row">
          <div>
            <div><span class="status-dot ${p.running ? 'on' : ''}"></span><strong>${esc(p.label)}</strong></div>
            <div class="notice">PID ${esc(p.pid || '')} ${esc(p.status || (p.running ? 'running' : 'offline'))} ${p.started_at ? '| started ' + esc(shortTime(p.started_at)) : ''}</div>
          </div>
          <div class="process-actions">
            <button onclick="controlProcess('${esc(p.name)}','start')">Start</button>
            <button class="danger" onclick="controlProcess('${esc(p.name)}','stop')">Stop</button>
          </div>
        </div>
      `).join('') || '<div class="notice">No processes configured.</div>';
      const ai = localAI || {};
      const aiHtml = `
        <div class="process-row">
          <div>
            <div><span class="status-dot ${ai.healthy ? 'on' : ''}"></span><strong>${esc(ai.label || 'Local AI (Ollama)')}</strong></div>
            <div class="notice">${ai.healthy ? 'running locally' : (ai.running ? 'running; model unavailable' : 'stopped')} | ${esc(ai.model || 'gpt-oss:20b')} | shadow voter</div>
          </div>
          <div class="process-actions"><span class="health-chip ${ai.healthy ? 'good' : 'bad'}">${ai.healthy ? 'READY' : 'OFFLINE'}</span></div>
        </div>`;
      box.innerHTML = processHtml + aiHtml;
    }

    function renderSportsSpreadAudit(report) {
      window.latestSportsAuditReport = report;
      const cohort = report.spread_analysis?.windows?.['30d'] || {};
      const financial = cohort.financial || {};
      const current = cohort.current_build || {};
      const build = report.strategy_identity?.build_id;
      const route = report.next_focused_refresh || {};
      setText('sports-build-summary', build ? `Code build ${build} · Last scan ${report.scan_duration_seconds ?? '?'}s · ${route.enabled ? `${route.universe_count} focused targets; ${route.target_interval_seconds}s target cadence` : 'Broad discovery active'}` : 'Build tracking starts with the next worker update.');
      setText('sports-spread-summary', `${financial.records || 0} live spread fills · ${money(financial.after_fee_profit || 0)} after fees · ${current.records || 0} settled fills from the current build.`);
      const dimension = document.getElementById('sports-spread-dimension')?.value || 'league_line_timing';
      const rows = Object.entries(cohort.segments?.[dimension] || {}).map(([label, metrics]) => ({label, ...metrics}));
      rows.sort((a, b) => Number(a.after_fee_profit) - Number(b.after_fee_profit));
      renderRows('sports-spread-results', rows, [
        {render: row => row.label}, {num: true, render: row => row.records},
        {num: true, render: row => row.effective_events}, {num: true, render: row => money(row.after_fee_profit)},
        {num: true, render: row => row.roi_pct == null ? 'N/A' : `${Number(row.roi_pct).toFixed(2)}%`}
      ], 'No settled live spread results in this window.');
    }

    let itfShadowFeed = {};
    function itfUnits(value, signed = false) {
      if (value == null || !Number.isFinite(Number(value))) return 'Pending';
      const n = Number(value);
      return `${signed && n > 0 ? '+' : ''}${n.toFixed(4)}U`;
    }
    function renderITFShadow(feed) {
      itfShadowFeed = feed;
      const strategies = feed.strategies || [];
      const healthy = ['healthy', 'complete'].includes(feed.health);
      const label = feed.health === 'healthy' ? `Running · ${feed.status}` : feed.health || 'Not started';
      setText('itf-shadow-health', label, `health-chip ${healthy ? 'good' : ['error', 'stale'].includes(feed.health) ? 'bad' : ''}`);
      setText('itf-shadow-window', feed.started_at ? `${shortTime(feed.started_at)} → ${shortTime(feed.ends_at)} Central · Frozen 1U = ${money(feed.unit_dollars)} including fees · ${feed.status === 'collecting' ? 'Accepting new shadow picks' : feed.status === 'settling' ? 'Entry week ended; settling remaining picks' : 'Experiment complete'}` : 'Awaiting the independent ITF shadow worker.');
      const c = feed.coverage || {}, p = c.phases || {};
      setText('itf-shadow-coverage', `${c.events || 0} ITF matches / ${c.underdogs || 0} underdogs last scan · ${p.pregame || 0} pregame / ${p.live || 0} live / ${p.finished || 0} finished / ${p.unknown || 0} unknown · ${feed.scan_count || 0} scans · ${feed.last_scan_at ? shortTime(feed.last_scan_at) : 'No scan yet'} · ${c.poll_seconds || 60}s polling${feed.worker?.error ? ' · ' + feed.worker.error : ''}`);
      const audit = feed.accounting || {};
      setText('itf-shadow-accounting', `${audit.ok ? `Accounting verified across ${audit.checked_positions} picks (${audit.settled_positions} settled).` : 'Accounting verification unavailable or needs review.'} 1U is the amount risked, not a fixed win. Net profit = payout minus stake and fees, divided by the frozen ${money(feed.unit_dollars)} unit. Payout includes returned stake.`);
      renderRows('itf-shadow-strategies', strategies, [
        {html: true, render: r => `<strong>${esc(r.name)}</strong><div class="muted">${esc(r.description)}</div>`},
        {num: true, render: r => `${r.entries} / ${r.open}`},
        {num: true, render: r => `${r.wins} / ${r.losses} / ${r.non_binary}`},
        {num: true, html: true, render: r => `<strong>${itfUnits(r.profit_units, true)} (${money(r.profit)})</strong><div class="muted">Gains ${itfUnits(r.gains_units, true)} · Losses ${itfUnits(r.losses_units, true)}</div>`},
        {num: true, render: r => r.roi_percent == null ? 'Pending' : `${Number(r.roi_percent).toFixed(1)}%`},
        {num: true, render: r => `${Number(r.open_risk_units).toFixed(2)}U`},
        {num: true, render: r => `${Number(r.drawdown_units).toFixed(2)}U`}
      ], 'No ITF shadow data yet.');
      renderITFShadowPicks();
      const segments = document.getElementById('itf-shadow-segments');
      if (segments) segments.innerHTML = strategies.map(r => `<p><strong>${esc(r.name)}</strong><br>${(r.segments || []).map(s => `${esc(s.value)}: ${Number(s.entries)} bets, ${Number(s.settled)} settled, ${Number(s.profit_units).toFixed(2)}U`).join(' · ') || 'No entries yet'}</p>`).join('');
    }
    function renderITFShadowPicks() {
      const feed = itfShadowFeed;
      const names = Object.fromEntries((feed.strategies || []).map(r => [r.id, r.name]));
      const result = document.getElementById('itf-shadow-result-filter')?.value || 'settled';
      const strategy = document.getElementById('itf-shadow-strategy-filter')?.value || 'all';
      const rows = (feed.positions || []).filter(r => (result === 'all' || r.status === result) && (strategy === 'all' || r.strategy === strategy));
      setText('itf-shadow-pick-count', `${rows.length} shown · ${feed.position_count || 0} recorded picks`);
      renderRows('itf-shadow-picks', rows, [
        {render: r => shortTime(r.placed_at)}, {render: r => names[r.strategy] || r.strategy},
        {html: true, render: r => `<strong>${esc(r.selected)} to win</strong><div class="muted">vs ${esc(r.opponent)} · ${esc(r.tournament || '')}</div>`},
        {render: r => r.phase}, {render: r => `${r.ticker} / ${String(r.side).toUpperCase()}`},
        {num: true, render: r => `${(Number(r.entry_price) * 100).toFixed(2)}¢ / ${r.contracts}`},
        {num: true, render: r => `${itfUnits(r.staked_units)} (${money(r.cost)})`},
        {num: true, render: r => `${itfUnits(r.win_profit_units, true)} (${money(r.win_profit)})`},
        {num: true, render: r => r.status === 'open' ? 'Pending' : `${itfUnits(r.payout_units)} (${money(r.payout)})`},
        {num: true, render: r => r.status === 'open' ? 'Open · unsettled' : `${r.result} · ${itfUnits(r.profit_units, true)} (${money(r.profit)})`}
      ], 'No picks match these filters.');
    }

    function renderITFFollowups(feed) {
      setText('itf-followups-window', feed.started_at
        ? `${shortTime(feed.started_at)} → ${shortTime(feed.ends_at)} Central · ${feed.health || 'waiting'} · ${feed.excluded_event_count || 0} earlier matches excluded · Frozen 1U = ${money(feed.unit_dollars)}${feed.worker?.error ? ' · ' + feed.worker.error : ''}`
        : `Follow-up tests: ${feed.health || 'not started'}${feed.worker?.error ? ' · ' + feed.worker.error : ''}`);
      renderRows('itf-followups-strategies', feed.strategies || [], [
        {html: true, render: r => `<strong>${esc(r.name)}</strong><div class="muted">${esc(r.description)}</div>`},
        {num: true, render: r => `${itfUnits(r.historical?.profit_units, true)} / ${r.historical?.settled || 0} settled`},
        {num: true, render: r => `${r.settled} / ${r.open}`},
        {num: true, render: r => `${itfUnits(r.profit_units, true)} / ${r.roi_percent == null ? 'Pending' : Number(r.roi_percent).toFixed(1) + '%'}`},
        {num: true, render: r => `${itfUnits(r.open_risk_units)} / ${itfUnits(r.drawdown_units)}`},
        {render: r => r.evidence}
      ], 'No registered follow-up test results yet.');
    }

    function renderITFPriceQuality(feed) {
      const paired = feed.paired_comparison || {};
      setText('itf-price-quality-comparison', `${paired.completed_matches || 0} matches settled on both sides · Favorite minus underdog net profit: ${itfUnits(paired.favorite_minus_dog_profit_units, true)} · ${paired.pending_matches || 0} matched comparisons awaiting settlement`);
      setText('itf-price-quality-window', feed.started_at
        ? `${shortTime(feed.started_at)} → ${shortTime(feed.ends_at)} Central · ${feed.health || 'waiting'} · ${feed.excluded_event_count || 0} earlier matches excluded · 1U = ${money(feed.unit_dollars)}${feed.worker?.error ? ' · ' + feed.worker.error : ''}`
        : `Price-quality tests: ${feed.health || 'not started'}`);
      renderRows('itf-price-quality-strategies', feed.strategies || [], [
        {html: true, render: r => `<strong>${esc(r.name)}</strong><div class="muted">${esc(r.description)}</div>`},
        {num: true, render: r => `${r.settled} / ${r.open}`},
        {num: true, render: r => `${itfUnits(r.profit_units, true)} / ${r.roi_percent == null ? 'Pending' : Number(r.roi_percent).toFixed(1) + '%'}`},
        {num: true, render: r => `${itfUnits(r.open_risk_units)} / ${itfUnits(r.drawdown_units)}`},
        {num: true, render: r => money(r.available_cash)},
        {render: r => r.entries_stopped_at ? `Entries stopped ${shortTime(r.entries_stopped_at)} · settlements continue` : r.evidence}
      ], 'No registered price-quality results yet.');
      const names = Object.fromEntries((feed.strategies || []).map(r => [r.id, r.name]));
      renderRows('itf-price-quality-picks', feed.positions || [], [
        {render: r => shortTime(r.placed_at)},
        {html: true, render: r => `<strong>${esc(names[r.strategy] || r.strategy)}</strong><div>${esc(r.selected)} vs ${esc(r.opponent)}</div>`},
        {render: r => r.phase},
        {num: true, render: r => `${(Number(r.entry_price) * 100).toFixed(1)}c / ${r.contracts}`},
        {num: true, render: r => money(r.cost)},
        {num: true, render: r => r.status === 'settled' ? `${r.result} / ${money(r.profit)}` : 'Open'}
      ], 'Waiting for a new match and qualifying executable prices.');
    }

    function aibetUnits(value, signed=false) {
      if (value == null || !Number.isFinite(Number(value))) return 'Unavailable';
      return `${signed && Number(value) > 0 ? '+' : ''}${Number(value).toFixed(4)}U`;
    }
    function renderAIBetPicks(feed, results) {
      results = results || {};
      const bots = feed.bots || [];
      const error = feed.error;
      const health = feed.health || 'waiting';
      const label = health === 'disabled' ? 'Disabled' : error ? `Feed error: ${error}` : health === 'healthy' ? `${bots.length} bots · Current` : health === 'stale' ? 'Feed stale · Entries paused' : health === 'scheduled' ? 'Next scheduled check pending' : 'Awaiting first poll';
      setText('aibetpicks-health', label, `health-chip ${health === 'healthy' ? 'good' : ['error', 'stale'].includes(health) ? 'bad' : ''}`);
      setText('aibetpicks-poll', `${feed.schedule || 'Hourly, 10 AM–5 PM Central'} · Last check: ${feed.last_poll ? shortTime(feed.last_poll) : 'Awaiting first scheduled poll'} · Next: ${feed.next_poll ? shortTime(feed.next_poll) : 'pending'} · Local: ${results.wins || 0}W / ${results.losses || 0}L / ${results.voids || 0} void / ${results.other || 0} other · ${results.open_count || 0} open · ${money(results.profit || 0)} / ${aibetUnits(results.profit_units, true)} net`);
      setText('aibetpicks-policy', feed.sizing_policy || 'Use the published AIBetPicks unit size; shared execution limits apply.');
      const audit = results.accounting || {};
      setText('aibetpicks-accounting', `${audit.ok ? `${audit.checked_records} local records reconciled` : 'Accounting needs review'} · ${results.pending_fee_records || 0} awaiting exact exchange fees. Net units = each bet’s after-fee profit divided by its unit value at entry, then summed. Stake units exclude fees; total risk includes them. Dollar risk and net profit round to cents before unit conversion. Open bets have no realized profit.`);
      const localByBot = new Map((results.by_bot || []).map(row => [row.bot_id, row]));
      const resultBody = document.getElementById('aibetpicks-results-rows');
      if (resultBody) resultBody.innerHTML = (results.rows || []).map(r => `<tr><td>${esc(shortTime(r.placed_at))}<div class="muted">${r.status === 'settled' ? esc(shortTime(r.settled_at || r.settlement_ts)) : 'Open'}</div></td><td><strong>${esc(r.name)}</strong><div>${esc(r.pick || '—')}</div></td><td>${esc(r.kalshi_ticker || '—')}<div class="muted">${esc((r.kalshi_order_side || '').toUpperCase())} · ${Number(r.contracts || 0)} contracts</div></td><td class="num">${r.unit_size == null ? 'Unavailable' : money(r.unit_size)}</td><td class="num">${money(r.stake)}<div>${aibetUnits(r.stake_units)}</div><div class="muted">Published ${aibetUnits(r.published_units)}</div></td><td class="num">$${Number(r.fee || 0).toFixed(4)} fee<div>${aibetUnits(r.risk_units)} risk</div><div class="muted">${r.fee_verified ? 'Exchange fills verified' : 'Fee verification pending'}</div></td><td class="num">${r.status === 'settled' ? `${money(r.payout)}<div>${aibetUnits(r.payout_units)}</div>` : 'Pending'}</td><td class="num">${r.status === 'settled' ? `${esc(r.result)} · ${money(r.profit)}<div>${aibetUnits(r.profit_units, true)}</div>` : 'Open · unsettled'}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">No local fills yet.</td></tr>';
      const dailyBody = document.getElementById('aibetpicks-daily-rows');
      if (dailyBody) dailyBody.innerHTML = (results.daily || []).map(r => `<tr><td>${esc(r.date)}</td><td>${r.wins} / ${r.losses} / ${r.voids} / ${r.other}</td><td class="num">${money(r.profit)}</td><td class="num">${aibetUnits(r.profit_units, true)}</td><td class="num">${r.roi_pct == null ? '—' : `${Number(r.roi_pct).toFixed(2)}%`}</td></tr>`).join('');
      const body = document.getElementById('aibetpicks-rows');
      if (!body) return;
      body.innerHTML = bots.map(bot => {
        const p = bot.pick || {}, d = bot.decision || {}, stats = bot.performance || {};
        const local = localByBot.get(bot.bot_id) || {};
        const units = d.actual_units ?? d.units;
        const publishedUnits = p.stake_units;
        const recoveryExtra = Number((d.recovery || {}).added_units || 0);
        const publishedLabel = publishedUnits == null ? 'Published size unavailable' : `Published: ${Number(publishedUnits).toFixed(2)}U${recoveryExtra > 0 ? ` + ${recoveryExtra.toFixed(2)}U recovery` : ''}`;
        const stake = d.actual_stake ?? d.planned_stake;
        const statusLabel = d.status === 'rules_review' ? 'Matched · rules review' : d.status;
        const reasonLabel = d.reason === 'aibetpicks_tennis_settlement_rules_differ'
          ? 'LowVig needs one completed set; Kalshi uses match play. No order submitted.'
          : d.reason === 'aibetpicks_tennis_settlement_equivalence_unverified'
          ? 'Match found; bookmaker settlement equivalence is unverified. No order submitted.'
          : String(d.reason || '').replaceAll('_', ' ');
        return `<tr><td><strong>${esc(bot.name || bot.bot_id)}</strong><div>Local: ${local.wins || 0}W / ${local.losses || 0}L · ${money(local.profit || 0)} · ${aibetUnits(local.profit_units ?? (local.settled ? null : 0), true)}</div><div class="muted">Website verified subset: ${Number(stats.wins || 0)}W / ${Number(stats.losses || 0)}L · ${Number(stats.roi_pct || 0).toFixed(1)}% ROI · n=${Number(stats.sample || 0)}</div></td><td>${esc(p.pick || 'No pick posted')}<div class="muted">${esc(p.game || '')}</div></td><td>${p.commence_time ? esc(shortTime(p.commence_time)) : '—'}</td><td class="num">${esc(p.odds ?? '—')}</td><td class="num">${units == null ? '—' : `${Number(units).toFixed(2)}U · ${money(stake || 0)}`}<div class="muted">${esc(publishedLabel)}</div></td><td>${esc(statusLabel || (p.pick ? 'Awaiting scan' : 'Waiting for publication'))}<div class="muted">${esc(reasonLabel)}</div></td><td>${esc(d.ticker || '—')}<div class="muted">${esc((d.side || '').toUpperCase())}</div></td></tr>`;
      }).join('') || '<tr><td colspan="7" class="empty">Waiting for the first authenticated feed poll.</td></tr>';
    }

    let refreshInFlight = false;
    async function refresh() {
      if (refreshInFlight) return;
      refreshInFlight = true;
      try {
      const res = await fetch('/api/state', { cache: 'no-store' });
      if (!res.ok) throw new Error(`state request failed (${res.status})`);
      const data = await res.json();
      document.getElementById('generated').textContent = `Live refresh ${shortTime(data.generated_at)} · all times Central (America/Chicago)`;
      const processRows = Object.values(data.processes || {});
      const expectedBotNames = ['sports', 'crypto'];
      const trackedBots = processRows.filter(row => expectedBotNames.includes(row.name));
      const onlineBots = trackedBots.filter(row => row.running).length;
      const headerAuditWarnings = (((data.sports || {}).live_audit || {}).warnings || []).length;
      const healthy = trackedBots.length > 0 && onlineBots === trackedBots.length && headerAuditWarnings === 0;
      setText('system-health', `${onlineBots}/${trackedBots.length || 2} bots online${headerAuditWarnings ? ` · ${headerAuditWarnings} audit warning${headerAuditWarnings === 1 ? '' : 's'}` : ''}`, `health-chip ${healthy ? 'good' : 'bad'}`);
      const shared = data.shared_bankroll || {};
      setText('system-cash', `Cash ${money(shared.cash ?? (data.sports || {}).balance ?? 0)}`, 'health-chip good');
      const sportsHeader = data.sports || {};
      const cryptoHeader = data.crypto || {};
      const sportsExposure = Number(sportsHeader.open_exposure || 0);
      const cryptoExposure = Number(cryptoHeader.open_exposure || 0);
      const retiredExposure = Number(shared.retired_live_open_exposure || 0);
      const totalExposure = sportsExposure + cryptoExposure + retiredExposure;
      const totalOpenBets = Number(sportsHeader.open_bets_count || 0) + Number(cryptoHeader.open_bets_count || 0) + Number(shared.retired_live_open_count || 0);
      setText(
        'system-exposure',
        `Exposure ${money(totalExposure)} · ${totalOpenBets} open`,
        `health-chip ${totalExposure > 0 ? 'warn' : 'good'}`
      );
      const exposurePill = document.getElementById('system-exposure');
      if (exposurePill) exposurePill.title = `Sports ${money(sportsExposure)} · Crypto ${money(cryptoExposure)}${retiredExposure ? ` · Retired positions ${money(retiredExposure)}` : ""}`;
      const liveProfit = Number(sportsHeader.bot_today_profit || 0) + Number(cryptoHeader.bot_today_profit || 0);
      setText('system-profit', `Bot Today ${money(liveProfit)}`, `health-chip ${liveProfit >= 0 ? 'good' : 'bad'}`);
      const sportsHeaderCampaign = sportsHeader.live_campaign || ((sportsHeader.report || {}).live_campaign) || {};
      const cryptoHeaderCampaign = ((cryptoHeader.report || {}).crypto_15m_campaign) || {};
      const headerCryptoBots = cryptoCampaignBots(cryptoHeaderCampaign).filter(bot => bot.configured !== false);
      setText(
        'system-cycle',
        `Sports always on · ${sportsHeaderCampaign.campaign_lane_active_open_count ?? sportsHeaderCampaign.bot_live_active_open_count ?? 0}/${sportsHeaderCampaign.max_open || 1} normal active · ${(sportsHeader.open_bets || []).filter(row => row.source === 'aibetpicks' || row.strategy_owner === 'aibetpicks').length} AIBetPicks`,
        `health-chip ${sportsHeaderCampaign.status === 'active' ? 'good' : ''}`
      );
      const storage = data.storage || {};
      const storageClass = storage.status === 'critical' ? 'bad' : storage.status === 'warning' ? 'warn' : 'good';
      setText('system-storage', `Storage ${fileSize(storage.total_bytes)}`, `health-chip ${storageClass}`);
      setText('storage-total', fileSize(storage.total_bytes));
      setText('storage-active', fileSize(storage.active_bytes));
      setText('storage-analytics', fileSize(storage.analytics_bytes));
      setText('storage-archives', fileSize(storage.archive_bytes));
      setText('storage-temp', fileSize(storage.temp_bytes));
      setText('storage-last-cleanup', storage.last_cleanup ? shortTime(storage.last_cleanup) : 'Never');
      const cryptoHeaderBotLabel = headerCryptoBots
        .map((bot, index) => `B${bot.bot_number || index + 1} ${Number(bot.bot_live_open_count || 0)}/${Number(bot.max_open || 1)}`)
        .join(' / ');
      setText(
        'system-crypto-cycle',
        `Crypto ${cryptoHeaderBotLabel || 'waiting'} · ${money(cryptoHeaderCampaign.outstanding_drawdown || 0)} high-water drawdown`,
        `health-chip ${cryptoHeaderCampaign.status === 'active' ? 'good' : ''}`
      );
      currentSettings = data.settings || {};
      renderSettings(data.settings);
      renderProcesses(data.processes, data.local_ai);

      const crypto = data.crypto || { report: { top_candidates: [] }, logs: [] };
      const cryptoReport = crypto.report || {};
      if (!patientControlBusy) renderPatientCryptoControl(crypto.patient_control || {});
      renderCryptoFunding(crypto.funding_control || {});
      renderCryptoControlSettings(crypto.settings || {});
      document.getElementById('crypto-header-mode').textContent = String(crypto.mode || 'paper').toUpperCase();
      setPnl('crypto-pnl', crypto.bot_total_profit || 0);
      renderCryptoCycleShadow(cryptoReport.cycle_shadow || {});
      renderCryptoSettlementLagShadow(crypto.settlement_lag_shadow || cryptoReport.settlement_lag_shadow || {});
      renderCryptoDirectionalOppositionV2(cryptoReport.directional_opposition_v2 || {});
      renderCryptoSignalTournament(cryptoReport.signal_tournament_shadow || {});
      renderCryptoExecutionLab(cryptoReport.execution_lab_shadow || {});
      renderCryptoProspective(cryptoReport.prospective_shadow || {});
      renderCryptoExpansion(crypto.research_expansion || {});
      renderCryptoSizing(crypto.research_sizing || {});
      renderBtcRandomShadow(crypto.btc_random_shadow || {});
      renderBtcValueShadow(crypto.btc_value_shadow || {});
      renderCryptoAssetSpecialist(cryptoReport.asset_specialist_shadow || {});
      renderCryptoSpotFlowLivePilot(cryptoReport.spot_flow_live_pilot || {});
      renderCryptoComplementArb(cryptoReport.complement_arb_shadow || {});
      renderCryptoBtc15mSprint(cryptoReport.btc_15m_sprint || {});
      renderPerpsShadow(crypto);
      renderRows('crypto-open-bets', (crypto.bot_open_bets || []).slice(0, 40), [
        {render: b => shortTime(b.placed_at)},
        {num: true, render: b => cryptoCampaignBotNumber(b)},
        {render: b => b.asset || ''},
        {render: b => String(b.side || '').toUpperCase()},
        {render: b => `${b.ticker || ''}: ${marketShort(b)}`},
        {num: true, render: b => money(b.stake)},
        {num: true, render: b => `${b.entry_price ?? ''}c`},
        {num: true, render: b => `${b.edge ?? ''}%`},
        {num: true, render: b => `${cryptoWinProbability(b).toFixed(1)}%`},
        {num: true, render: b => cryptoWinProbabilityInterval(b)},
        {num: true, render: b => `${cryptoEdgeCertainty(b).toFixed(1)}%`}
      ], 'No crypto bets open in the active mode yet. Run the crypto scanner to populate this.');
      renderRows('crypto-history', (crypto.history || []).filter(row => !isUserBet(row)).slice(0, 40), [
        {render: b => shortTime(b.settled_at)},
        {num: true, render: b => cryptoCampaignBotNumber(b)},
        {render: b => b.asset || ''},
        {render: b => b.result || ''},
        {render: b => String(b.side || '').toUpperCase()},
        {render: b => b.ticker || ''},
        {num: true, render: b => money(b.stake)},
        {num: true, render: b => money(b.profit)},
        {num: true, render: b => b.settlement_spot ?? ''}
      ], 'No crypto bets have settled in the active mode yet.');
      renderCryptoAnalytics(crypto);

      const sports = data.sports || { report: { top_candidates: [] }, logs: [] };
      const sportsReport = sports.report || {};
      renderSportsSpreadAudit(sportsReport);
      const auditPerformance = sportsReport.source_performance || {};
      const auditRows = [];
      for (const [lane, label] of [['autonomous', 'Autonomous']]) {
        for (const window of ['7d', '30d']) {
          const cohort = ((auditPerformance.lanes || {})[lane] || {})[window];
          if (!cohort) continue;
          for (const [mode, modeCohort] of Object.entries(cohort.execution_modes || {unknown: cohort})) {
            const modeLabel = `${label} · ${mode}`;
            auditRows.push({label: modeLabel, window, basis: 'Intended selections', ...(modeCohort.intended_strategy || {})});
            if (Number((modeCohort.integrity_excluded || {}).records || 0)) auditRows.push({label: modeLabel, window, basis: 'All actual executions', ...(modeCohort.financial || {})});
          }
        }
      }
      renderRows('sports-audit-results', auditRows, [
        {render: row => row.label}, {render: row => row.window}, {render: row => row.basis},
        {num: true, render: row => row.records}, {num: true, render: row => row.effective_events},
        {num: true, render: row => row.roi_pct == null ? 'N/A' : `${Number(row.roi_pct).toFixed(2)}%`},
        {num: true, render: row => Number(row.missing_unit_records || 0) ? (Number(row.unit_records || 0) ? `${Number(row.net_units || 0).toFixed(2)}U (${row.unit_records}/${row.records} records)` : 'N/A') : `${Number(row.net_units || 0).toFixed(2)}U`}
      ], 'Source-separated results will appear after the updated scanner runs.');
      const broadDiscovery = sportsReport.broad_discovery || {};
      setText('sports-audit-coverage', auditPerformance.as_of ? `Results through ${shortTime(auditPerformance.as_of)}. Last broad discovery: ${broadDiscovery.last_completed_at ? shortTime(broadDiscovery.last_completed_at) : 'unknown'}. ${broadDiscovery.due ? 'Broad discovery is due.' : ''}` : 'Waiting for an audited scan.');
      renderAIBetPicks(sports.aibetpicks || {}, sports.aibetpicks_results || {});
      const modest = sports.modest_recovery || {}, modestLanes = modest.lanes || {};
      const modestLaneText = (key, label) => {
        const lane = modestLanes[key] || {};
        return `${label}: ${Number(lane.drawdown_units || 0).toFixed(2)}U deficit, ${Number(lane.daily_extra_units || 0).toFixed(2)}/3U extra today${lane.paused ? ' · additions paused' : ''}`;
      };
      setText('sports-modest-recovery', modest.enabled
        ? `Modest recovery on · ${modestLaneText('aibetpicks', 'AIBetPicks')} · ${modestLaneText('scanner', 'Scanner')} · Combined extra ${Number(modest.combined_daily_extra_units || 0).toFixed(2)}/4U today, ${Number(modest.combined_open_extra_units || 0).toFixed(2)}/2U open. 1U keeps its usual dollar value.`
        : 'Modest recovery off · Normal unit sizing applies.');
      renderITFShadow(sports.itf_shadow || {});
      renderITFFollowups(sports.itf_followups || {});
      renderITFPriceQuality(sports.itf_price_quality || {});
      document.getElementById('sports-header-mode').textContent = String(sports.mode || 'paper').toUpperCase();
      setPnl('sports-pnl', sports.bot_total_profit || 0);
      const campaign = sports.live_campaign || sportsReport.live_campaign || {};
      const campaignStatus = campaign.status || (campaign.enabled ? 'waiting' : 'disabled');
      const cryptoCampaign = cryptoReport.crypto_15m_campaign || {};
      renderSimpleDashboard(data, sports, crypto, campaign, cryptoCampaign);
      renderPerformance(data, sports, crypto);
      const liveRecon = sports.live_reconciliation || {};
      const liveReady = liveRecon.readiness || {};
      const liveAccount = liveRecon.account || {};
      const liveChecks = liveReady.checks || {};
      const blocking = liveReady.blocking || [];
      const localLiveOpen = liveRecon.local_live_open || {};
      const liveAudit = sports.live_audit || sportsReport.live_audit || {};
      const auditWarnings = liveAudit.warnings || [];
      document.getElementById('live-ready').textContent = liveReady.ready ? 'Yes' : 'No';
      document.getElementById('live-ready').className = `value ${liveReady.ready ? 'positive' : 'negative'}`;
      document.getElementById('live-cash').textContent = liveAccount.cash_balance === undefined || liveAccount.cash_balance === null ? 'N/A' : money(liveAccount.cash_balance);
      document.getElementById('live-local-open').textContent = Object.keys(localLiveOpen).length;
      document.getElementById('live-remote-open').textContent = liveAccount.position_count ?? 'N/A';
      document.getElementById('live-blocking').textContent = blocking.length;
      document.getElementById('live-audit-status').textContent = liveAudit.checked_at ? (liveAudit.ok ? 'OK' : 'Check') : 'N/A';
      document.getElementById('live-audit-status').className = `value ${liveAudit.ok ? 'positive' : 'negative'}`;
      document.getElementById('live-audit-cash-drift').textContent = liveAudit.cash_diff === undefined || liveAudit.cash_diff === null ? 'N/A' : money(liveAudit.cash_diff);
      document.getElementById('live-audit-cash-drift').className = `value ${Math.abs(Number(liveAudit.cash_diff || 0)) <= Number(liveAudit.cash_tolerance || 0.25) ? 'positive' : 'negative'}`;
      document.getElementById('live-audit-warnings').textContent = auditWarnings.length;
      document.getElementById('live-audit-warnings').className = `value ${auditWarnings.length ? 'negative' : 'positive'}`;
      document.getElementById('live-audit-time').textContent = liveAudit.checked_at ? shortTime(liveAudit.checked_at) : 'N/A';
      renderRows('live-checks', Object.entries(liveChecks).map(([key, ok]) => ({key, ok})), [
        {render: row => row.key.replaceAll('_', ' ')},
        {render: row => row.ok ? 'ok' : 'blocked'}
      ], auditWarnings.length ? `Audit warnings: ${auditWarnings.join(', ')}` : (liveAccount.reason || liveAccount.balance_error || 'No live readiness data yet.'));
      const activeInBetMore = document.activeElement && document.activeElement.closest && document.activeElement.closest('#sports-open-bets .row-actions');
      if (!activeInBetMore) {
        renderRows('sports-open-bets', (sports.open_bets || []).slice(0, 30), [
          {render: b => shortTime(b.placed_at)},
          {num: true, render: b => campaignBotNumber(b)},
          {render: b => sourceLabel(b)},
          {render: b => b.game || `${b.away_team || ''} @ ${b.home_team || ''}`},
          {render: b => `${b.selected_team || ''} ${b.market_type || ''} ${b.line ?? ''}`},
          {render: b => b.kalshi_matched === false ? 'unmatched paper' : (b.kalshi_ticker || '')},
          {num: true, render: b => oddsLabel(b)},
          {num: true, render: b => money(b.stake)},
          {num: true, render: b => b.confidence ?? b.confidence_score ?? ''},
          {html: true, render: b => betMoreAction(b)}
        ], 'No sports open bets yet.');
      }
      renderRows('sports-history', (sports.history || []).slice(0, 30), [
        {render: b => shortTime(b.settled_at)},
        {num: true, render: b => campaignBotNumber(b)},
        {render: b => sourceLabel(b)},
        {render: b => b.result || b.status || ''},
        {render: b => b.game || `${b.away_team || ''} @ ${b.home_team || ''}`},
        {render: b => `${b.selected_team || ''} ${b.market_type || ''} ${b.line ?? b.market_line ?? ''}`},
        {render: b => `${b.kalshi_ticker || ''} ${b.kalshi_result ? '(' + b.kalshi_result + ')' : ''}`},
        {num: true, render: b => oddsLabel(b)},
        {num: true, render: b => money(b.stake)},
        {num: true, render: b => money(b.payout)},
        {num: true, render: b => money(b.profit)}
      ], 'No settled sports bets yet.');
      renderAnalytics(sports, crypto);
      const sportsLogAnalytics = (sports || {}).log_analytics || {};
      const oddsProjection = sportsLogAnalytics.odds_monthly_projection || {};
      const latestOddsUsage = sportsLogAnalytics.latest_usage || {};
      const oddsAccounts = ((sports || {}).report || {}).odds_api_accounts || {};
      const usedCredits = Number(oddsProjection.used ?? latestOddsUsage.monthly_used ?? 0);
      const hardLimit = Number(oddsProjection.hard_limit || 5000000);
      const monitoringTarget = Number(oddsProjection.monitoring_target || oddsProjection.limit || 4500000);
      const pacingTarget = Number(oddsProjection.pacing_target || 4750000);
      const providerRemaining = Number(oddsAccounts.provider_credits_remaining_authoritative
        ?? latestOddsUsage.quota_remaining ?? Math.max(0, hardLimit - usedCredits));
      const projectedCredits = Number(oddsProjection.projected || usedCredits);
      const usagePct = hardLimit > 0 ? Math.max(0, Math.min(100, usedCredits / hardLimit * 100)) : 0;
      setText('odds-api-used', usedCredits.toLocaleString());
      setText('odds-api-remaining', providerRemaining.toLocaleString());
      setText('odds-api-projected', projectedCredits.toLocaleString());
      setText('odds-api-daily-rate', `${Number(oddsProjection.daily_rate || 0).toLocaleString()}/day`);
      const oddsUsageBar = document.getElementById('odds-api-usage-bar');
      if (oddsUsageBar) {
        oddsUsageBar.style.width = `${usagePct.toFixed(1)}%`;
        oddsUsageBar.style.background = projectedCredits >= hardLimit
          ? 'var(--bad)'
          : projectedCredits >= pacingTarget ? 'var(--amber)' : 'var(--good)';
      }
      const pacingStatus = String(oddsProjection.status || 'on_track').replaceAll('_', ' ');
      const pressureMode = String(oddsProjection.pressure_mode || 'normal').replaceAll('_', ' ');
      const recentTraffic = oddsProjection.last_hour_credits == null ? '' : ` · Last hour: ${Number(oddsProjection.last_hour_credits).toLocaleString()} credits / ${Number(oddsProjection.last_hour_calls || 0).toLocaleString()} calls`;
      setText('odds-api-usage-status', `Usage status: ${pacingStatus} · governor ${pressureMode}${recentTraffic}`);
      setText(
        'odds-api-usage-detail',
        `${usagePct.toFixed(1)}% of ${hardLimit.toLocaleString()} provider credits · monitoring target ${monitoringTarget.toLocaleString()} · soft pacing ${pacingTarget.toLocaleString()} · execution reserve ${Number(oddsProjection.provider_reserve || 0).toLocaleString()} · projection based on ${String(oddsProjection.projection_basis || 'provider cycle').replaceAll('_', ' ')} · broad refresh pacing ${Number(oddsProjection.paid_refresh_multiplier || 1).toFixed(2)}x · paid shadows ${oddsProjection.shadow_paid_calls_allowed === false ? 'paused' : 'available'}`
      );
      const keyRows = Array.isArray(oddsAccounts.accounts) ? oddsAccounts.accounts : [];
      setText(
        'odds-api-key-detail',
        keyRows.length
          ? keyRows.map(row => `${row.label}: ${Number(row.provider_used || 0).toLocaleString()} used, ${Number(row.provider_remaining || 0).toLocaleString()} remaining${row.exhausted ? ' (exhausted)' : ''}`).join(' · ')
          : `Usage source: ${String(oddsProjection.usage_source || 'provider reported').replaceAll('_', ' ')}`
      );
      if (!cryptoCandidateHistoryLoaded) renderCryptoCandidateLog(crypto.candidate_log || []);
      const cryptoTextLog = document.getElementById('logs-crypto-text');
      if (cryptoTextLog) {
        cryptoTextLog.innerHTML = (crypto.logs || []).length
          ? crypto.logs.map(line => `<div class="logline">${esc(centralizeTextTimes(line))}</div>`).join('')
          : '<div class="logline">No crypto log lines yet.</div>';
      }
      document.getElementById('sports-logs').innerHTML = sports.logs.length
        ? sports.logs.map(line => `<div class="logline">${esc(centralizeTextTimes(line))}</div>`).join('')
        : '<div class="logline">No sports log lines yet.</div>';
      const notices = [
        ...auditWarnings.map(message => ({source:'Sports audit', message})),
        ...[['Sports', sports.logs], ['Crypto', crypto.logs]]
          .flatMap(([source, lines]) => (lines || []).filter(line => /warn|error|fail|exception|blocked/i.test(line))
            .slice(0, 8).map(message => ({source, message})))
      ];
      document.getElementById('logs-notices').innerHTML = notices.length
        ? notices.map(row => `<div class="logline"><strong>${esc(row.source)}</strong> · ${esc(centralizeTextTimes(row.message))}</div>`).join('')
        : '<div class="logline">No errors or warnings in the recent log window.</div>';
      installTableScrollHelpers();
      setTimeout(updateTableScrollHelpers, 0);
      } catch (error) {
        setText('generated', `Refresh failed · ${error.message || error}`, 'sub negative');
        setText('system-health', 'Dashboard data unavailable', 'health-chip bad');
      } finally {
        refreshInFlight = false;
      }
    }

    organizeControlCenter();
    organizeCryptoControl();
    installControlSearch();
    installTableScrollHelpers();
    [
      'crypto-log-min-price', 'crypto-log-max-price',
      'crypto-log-min-edge', 'crypto-log-max-edge',
      'crypto-log-min-confidence', 'crypto-log-max-confidence',
      'crypto-log-search'
    ].forEach(id => {
      document.getElementById(id)?.addEventListener('input', () => renderCryptoCandidateLog());
    });
    ['crypto-log-asset', 'crypto-log-side', 'crypto-log-decision'].forEach(id => {
      document.getElementById(id)?.addEventListener('change', () => renderCryptoCandidateLog());
    });
    const cryptoTimeRange = document.getElementById('crypto-log-time-range');
    const savedCryptoTimeRange = localStorage.getItem('cryptoLogTimeRange');
    if (
      savedCryptoTimeRange
      && cryptoTimeRange
      && [...cryptoTimeRange.options].some(option => option.value === savedCryptoTimeRange)
    ) {
      cryptoTimeRange.value = savedCryptoTimeRange;
    }
    cryptoTimeRange?.addEventListener('change', () => {
      localStorage.setItem('cryptoLogTimeRange', cryptoTimeRange.value);
      loadCryptoCandidateHistory();
    });
    ['sports-log-min-price', 'sports-log-max-price', 'sports-log-min-edge', 'sports-log-max-edge', 'sports-log-search'].forEach(id => {
      document.getElementById(id)?.addEventListener('input', scheduleSportsCandidateHistory);
    });
    ['sports-log-decision', 'sports-log-sport', 'sports-log-market'].forEach(id => {
      document.getElementById(id)?.addEventListener('change', loadSportsCandidateHistory);
    });
    const sportsTimeRange = document.getElementById('sports-log-time-range');
    const savedSportsTimeRange = localStorage.getItem('sportsLogTimeRange');
    if (savedSportsTimeRange && sportsTimeRange && [...sportsTimeRange.options].some(option => option.value === savedSportsTimeRange)) {
      sportsTimeRange.value = savedSportsTimeRange;
    }
    sportsTimeRange?.addEventListener('change', () => {
      localStorage.setItem('sportsLogTimeRange', sportsTimeRange.value);
      loadSportsCandidateHistory();
    });
    const savedView = localStorage.getItem('dashboardView');
    const visibleViews = new Set(['overview', 'sports', 'crypto', 'analytics', 'crypto-analytics', 'perps-shadow', 'control', 'crypto-control', 'logs']);
    showView(visibleViews.has(savedView) ? savedView : 'overview');
    document.querySelectorAll('[data-analytics-tab]').forEach(btn => btn.classList.toggle('active', btn.dataset.analyticsTab === (localStorage.getItem('analyticsBot') || 'sports')));
    showSportsPanel(localStorage.getItem('sportsPanel') || 'open');
    showCryptoPanel(localStorage.getItem('cryptoPanel') || 'open');
    showLogPanel(localStorage.getItem('logPanel') || 'sports');
    refresh();
    function refreshCandidateHistories() {
      if (document.hidden || !document.querySelector('[data-view="logs"].active')) return;
      const selected = localStorage.getItem('logPanel') || 'sports';
      if (selected === 'crypto') loadCryptoCandidateHistory();
      if (selected === 'sports') loadSportsCandidateHistory();
    }
    setInterval(() => { if (!document.hidden) refresh(); }, 5000);
    setInterval(refreshCandidateHistories, 15000);
  </script>
</body>
</html>
"""


HTML = expand_controls(HTML).replace("/* DASHBOARD_STYLES */", Path(__file__).with_name("dashboard.css").read_text(encoding="utf-8"))


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        request = urlparse(self.path)
        path = request.path
        if path == "/":
            self.respond(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            payload = json.dumps(
                compact_dashboard_state(build_state()),
                separators=(",", ":"),
            ).encode("utf-8")
            self.respond(payload, "application/json")
            return
        if path == "/api/sports/candidate-log":
            query = parse_qs(request.query)
            try:
                minutes = float((query.get("minutes") or ["360"])[0])
            except (TypeError, ValueError):
                minutes = 360.0
            def query_float(name):
                try:
                    return float((query.get(name) or [""])[0])
                except (TypeError, ValueError):
                    return None
            payload = json.dumps(sports_candidate_history(
                SPORTS_LOG_FILE,
                portfolio=read_json(SPORTS_PORTFOLIO_FILE, {}),
                minutes=minutes,
                min_price=query_float("min_price"),
                max_price=query_float("max_price"),
                min_edge=query_float("min_edge"),
                max_edge=query_float("max_edge"),
                decision=str((query.get("decision") or [""])[0]),
                sport=str((query.get("sport") or [""])[0]),
                market=str((query.get("market") or [""])[0]),
                search=str((query.get("search") or [""])[0]),
            )).encode("utf-8")
            self.respond(payload, "application/json")
            return
        if path == "/api/crypto/candidate-log":
            query = parse_qs(request.query)
            try:
                minutes = float((query.get("minutes") or ["60"])[0])
            except (TypeError, ValueError):
                minutes = 60.0
            settings = public_crypto_settings()
            mode = str(settings.get("CRYPTO_EXECUTION_MODE") or "paper").lower()
            events_file = (
                CRYPTO_LIVE_EVENTS_FILE
                if mode == "live"
                else CRYPTO_PAPER_EVENTS_FILE
            )
            history = (
                intelligence_history(
                    CRYPTO_SCAN_INTELLIGENCE_FILE,
                    minutes=minutes,
                )
                if CRYPTO_SCAN_INTELLIGENCE_FILE.exists()
                else crypto_candidate_history(events_file, minutes=minutes)
            )
            payload = json.dumps(history).encode("utf-8")
            self.respond(payload, "application/json")
            return
        self.send_error(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.read_json_body()
            if path == "/api/crypto/funding":
                origin = urlparse(self.headers.get("Origin") or "")
                if self.headers.get("X-Crypto-Funding-Action") != "1" or (
                    origin.netloc and origin.netloc != self.headers.get("Host")
                ):
                    raise ValueError("Open this control from the local dashboard.")
                self.respond_json(control_crypto_funding(payload.get("action")))
                return
            if path == "/api/crypto/patient":
                origin = urlparse(self.headers.get("Origin") or "")
                if self.headers.get("X-Crypto-Patient-Action") != "1" or (
                    origin.netloc and origin.netloc != self.headers.get("Host")
                ):
                    raise ValueError("Open this control from the local dashboard.")
                self.respond_json(control_patient_crypto(payload.get("action")))
                return
            if path == "/api/settings":
                settings = save_settings(payload)
                self.respond_json({"ok": True, "settings": public_settings(), "saved_keys": [key for key in SENSITIVE_KEYS if settings.get(key)]})
                return
            if path == "/api/settings/reveal":
                key = str(payload.get("key", ""))
                self.respond_json({"ok": True, "key": key, "value": reveal_setting(key)})
                return
            if path == "/api/crypto/settings":
                settings = save_crypto_settings(payload)
                self.respond_json({"ok": True, "settings": public_crypto_settings(), "saved_keys": [key for key in CRYPTO_SENSITIVE_KEYS if settings.get(key)]})
                return
            if path == "/api/crypto/settings/reveal":
                key = str(payload.get("key", ""))
                self.respond_json({"ok": True, "key": key, "value": reveal_crypto_setting(key)})
                return
            if path == "/api/crypto/campaign/reset":
                result = reset_crypto_campaign_bot(payload.get("bot_number"))
                self.respond_json({"ok": True, **result})
                return
            if path == "/api/sports/campaign/reset":
                result = reset_sports_campaign_bot(payload.get("bot_number"))
                self.respond_json({"ok": True, **result})
                return
            if path == "/api/process/start":
                status = start_process(str(payload.get("name", "")))
                self.respond_json({"ok": True, "process": status})
                return
            if path == "/api/process/stop":
                status = stop_process(str(payload.get("name", "")))
                self.respond_json({"ok": True, "process": status})
                return
            if path == "/api/storage/cleanup":
                self.respond_json(run_maintenance(force=True))
                return
            if path == "/api/sports/rerun-bot-picks":
                result = rerun_bot_picks_once()
                self.respond_json({"ok": True, **result})
                return
            if path == "/api/sports/bet-more/quote":
                self.respond_json(sports_bet_more_quote(payload))
                return
            if path == "/api/sports/bet-more/order":
                self.respond_json(sports_bet_more_order(payload))
                return
            if path == "/api/odds/reset":
                removed = reset_odds_state()
                self.respond_json({"ok": True, "removed": removed})
                return
        except Exception as exc:
            body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, "Not found")

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body or "{}")

    def respond_json(self, data):
        self.respond(json.dumps(data).encode("utf-8"), "application/json")

    def respond(self, payload: bytes, content_type: str):
        use_gzip = (
            len(payload) >= 1024
            and "gzip" in str(self.headers.get("Accept-Encoding") or "").lower()
        )
        if use_gzip:
            payload = gzip.compress(payload, compresslevel=5)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Dashboard running at http://{HOST}:{PORT}")
    server.serve_forever()
