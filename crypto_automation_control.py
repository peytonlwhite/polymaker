"""Deterministic health and adaptive-lane controls for the crypto bot.

The scheduled Codex tasks use this module as their evidence source.  Runtime
strategy changes are deliberately limited to a small lane state file; API
credentials, settlement, fees, bankroll limits, and recovery settings are not
mutable here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path


VERSION = "crypto-adaptive-control-v2"
STRATEGY_VERSION = "settlement-edge-v3-residual-maturity"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "crypto_adaptive_governor.json"
HEALTH_FILE = ROOT / "crypto_automation_health.json"
SETTINGS_FILE = ROOT / "crypto_settings.json"
PORTFOLIO_FILE = ROOT / "crypto_live_portfolio.json"
REPORT_FILE = ROOT / "crypto_live_report.json"
MODEL_STATE_FILE = ROOT / "crypto_15m_model_state.json"
REJECTION_SHADOW_FILE = ROOT / "crypto_rejection_shadow.json"
LOG_FILE = ROOT / "crypto_live_log.txt"

PROTECTED_RECOVERY_SETTINGS = {
    "CRYPTO_15M_UNIT_RECOVERY_ENABLED": "false",
    "CRYPTO_15M_POOLED_RECOVERY_ENABLED": "false",
    "CRYPTO_15M_RECOVERY_MAX_OVERLAYS_PER_EXPIRY": "1",
    "CRYPTO_15M_UNIT_RECOVERY_MIN_BASE_UNITS": "3",
    "CRYPTO_15M_UNIT_RECOVERY_MIN_PROBABILITY_UNITS": "3",
    "CRYPTO_15M_UNIT_RECOVERY_TARGET_DRAWDOWN_FRACTION": "0.33",
    "CRYPTO_15M_UNIT_RECOVERY_MAX_BONUS_UNITS": "2",
    "CRYPTO_15M_UNIT_RECOVERY_MAX_TOTAL_UNITS": "5",
    "CRYPTO_SHARED_RECOVERY_ENABLED": "false",
}

PROTECTED_UNIT_RISK_SETTINGS = {
    "CRYPTO_15M_UNIT_STAKING_ENABLED": "false",
    "CRYPTO_15M_PARTIAL_RECOVERY_ENABLED": "false",
    "CRYPTO_15M_CONTINUOUS_OPPORTUNITY_MODE": "true",
    "CRYPTO_15M_CAMPAIGN_BOT_COUNT": "3",
    "CRYPTO_15M_UNIT_SIZE_PCT": "0.75",
    "CRYPTO_15M_UNIT_MAX_PER_MARKET": "5",
    "CRYPTO_15M_UNIT_MIN_DATA_QUALITY": "0.95",
    "CRYPTO_15M_UNIT_WIN_PROB_MODEL_WEIGHT": "0.35",
    "CRYPTO_15M_UNIT_WIN_PROB_SHADOW_MAX_WEIGHT": "0.10",
    "CRYPTO_15M_UNIT_MIN_CALIBRATION_RESERVE_PP": "0.50",
    "CRYPTO_15M_UNIT_KELLY_FRACTION": "0.25",
    "CRYPTO_15M_GLOBAL_DAILY_LOSS_UNITS": "6",
    "CRYPTO_15M_MODEL_MATURITY_CAP_ENABLED": "true",
    "CRYPTO_15M_MODEL_UNQUALIFIED_MAX_UNITS": "1",
    "CRYPTO_15M_MODEL_TIER_2_MIN_MARKETS": "50",
    "CRYPTO_15M_MODEL_TIER_3_MIN_MARKETS": "100",
    "CRYPTO_15M_MODEL_TIER_4_MIN_MARKETS": "150",
    "CRYPTO_15M_MODEL_TIER_5_MIN_MARKETS": "250",
    "CRYPTO_15M_MODEL_TIER_MAX_CALIBRATION_GAP": "0.05",
    "CRYPTO_15M_LEARNING_ENABLED": "true",
    "CRYPTO_15M_ML_FORCE_SHADOW": "false",
    "CRYPTO_15M_ML_MIN_INTERVAL_MARKETS": "100",
    "CRYPTO_15M_ML_MIN_INTERVAL_COVERAGE": "0.80",
    "CRYPTO_15M_MIN_ENTRY_PRICE_CENTS": "35",
    "CRYPTO_15M_MIN_EDGE": "3",
    "CRYPTO_15M_MID_PRICE_MIN_CENTS": "35",
    "CRYPTO_15M_SIMPLE_MIN_EDGE": "3",
    "CRYPTO_15M_SIMPLE_MIN_PRICE_CENTS": "35",
    "CRYPTO_15M_TIERED_MIN_EDGE": "3",
    "CRYPTO_15M_TIERED_MIN_PRICE_CENTS": "35",
    "CRYPTO_15M_MAX_UNCONFIRMED_MARKET_GAP": "8",
    "CRYPTO_15M_FLOW_CAN_OVERRIDE_MARKET_GAP": "false",
    "CRYPTO_15M_FLOW_CONFIRMATION_MIN_STRENGTH": "0.22",
    "CRYPTO_15M_FLOW_CONFIRMATION_MIN_SOURCES": "3",
    "CRYPTO_15M_DAILY_LOSS_CAP": "400",
    "CRYPTO_15M_CROSS_BOT_CAP_RECOVERY_ENABLED": "false",
    "CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_ENABLED": "true",
    "CRYPTO_LIVE_UNIT_DEPTH_DOWNSHIFT_MIN_UNITS": "1",
    "CRYPTO_LIVE_QUOTE_REFRESH_MAX_ADVERSE_CENTS": "6",
    "CRYPTO_LIVE_QUOTE_REFRESH_PRIORITY_MAX_ADVERSE_CENTS": "6",
    "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_ENABLED": "true",
    "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_SECONDS": "15",
    "CRYPTO_LIVE_SLIPPAGE_RECONFIRM_MAX_MOVE_CENTS": "2",
    "SHARED_CRYPTO_DAILY_LOSS_CAP": "0",
    "SHARED_CRYPTO_DAILY_LOSS_PCT": "0.05",
    "SHARED_CRYPTO_RISK_CAP_BUFFER": "0",
    "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_CAP": "0",
    "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_PCT": "0",
    "CRYPTO_SCAN_INTELLIGENCE_ENABLED": "true",
    "CRYPTO_SCAN_INTELLIGENCE_MAX_ACTIVE": "10000",
    "CRYPTO_DYNAMIC_QUALIFICATION_ENABLED": "true",
    "CRYPTO_DYNAMIC_AUTO_PROMOTE_ENABLED": "false",
    "CRYPTO_15M_SPRINT_ENABLED": "true",
    "CRYPTO_15M_SPRINT_SHADOW_ONLY": "true",
    "CRYPTO_CYCLE_SHADOW_ENABLED": "false",
    "CRYPTO_LIVE_ORDER_ENABLED": "false",
    "CRYPTO_LIVE_DRY_RUN": "false",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED": "false",
    "CRYPTO_15M_LEARNING_ACTIVE_ENABLED": "false",
    "CRYPTO_PROSPECTIVE_SHADOW_ENABLED": "true",
    "CRYPTO_LEGACY_SHADOW_COLLECTION_ENABLED": "false",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_BANKROLL": "0",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_SIZING_BANKROLL_CAP": "5000",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_STAKE_PCT": "0.30",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_CONTRACTS": "5",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_ENABLED": "true",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MIN_PRICE_CENTS": "35",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MAX_PRICE_CENTS": "80",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MIN_MINUTES": "5",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MAX_MINUTES": "13",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MIN_STRENGTH_BPS": "10",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MAX_CONTRACTS": "3",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_MINUTES": "2",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_MINUTES": "13",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_ADVERSE_MOVE_CENTS": "1",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_DAILY_ENTRY_CAP": "25",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_DAILY_LOSS_PCT": "0.90",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_OPEN_EXPOSURE_PCT": "0.90",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_OPEN": "4",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_OPEN_PER_EXPIRY": "2",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_REVIEW_SETTLED_CAP": "100",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_REQUIRE_TOURNAMENT_GATE": "true",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_REQUIRE_FORWARD_GATE": "true",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_FORWARD_MARKETS": "100",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_FORWARD_DAYS": "3",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_PAIR_DELTA_SECONDS": "2",
    "CRYPTO_DYNAMIC_MIN_SETTLED_MARKETS": "100",
    "CRYPTO_DYNAMIC_RECENT_VALIDATION_MARKETS": "40",
    "CRYPTO_DYNAMIC_MAX_CALIBRATION_GAP": "0.05",
    "CRYPTO_DYNAMIC_MAX_RECENT_CALIBRATION_GAP": "0.075",
    "CRYPTO_DYNAMIC_HARD_MIN_DATA_QUALITY": "0.95",
    "CRYPTO_DYNAMIC_MAX_KALSHI_AGE_SECONDS": "2",
    "CRYPTO_DYNAMIC_MIN_HEALTHY_SOURCES": "2",
    "CRYPTO_DYNAMIC_MAX_MODEL_DISAGREEMENT_PP": "15",
    "CRYPTO_DYNAMIC_HALF_MIN_EDGE_CENTS": "1",
    "CRYPTO_DYNAMIC_HALF_MIN_MARGIN_CENTS": "0.25",
    "CRYPTO_DYNAMIC_HALF_MIN_EDGE_PROBABILITY": "85",
    "CRYPTO_DYNAMIC_HALF_MIN_DATA_QUALITY": "0.98",
    "CRYPTO_DYNAMIC_HALF_MIN_ALIGNED_SOURCES": "2",
    "CRYPTO_DYNAMIC_HALF_MAX_SPREAD_CENTS": "2",
    "CRYPTO_DYNAMIC_HALF_MAX_MARKET_GAP_PP": "4",
    "CRYPTO_DYNAMIC_ONE_MIN_EDGE_CENTS": "2",
    "CRYPTO_DYNAMIC_ONE_MIN_MARGIN_CENTS": "0.50",
    "CRYPTO_DYNAMIC_ONE_MIN_EDGE_PROBABILITY": "88",
    "CRYPTO_DYNAMIC_ONE_MIN_DATA_QUALITY": "0.96",
    "CRYPTO_DYNAMIC_ONE_MIN_ALIGNED_SOURCES": "2",
    "CRYPTO_DYNAMIC_ONE_MAX_SPREAD_CENTS": "3",
    "CRYPTO_DYNAMIC_ONE_MAX_MARKET_GAP_PP": "8",
}

LANE_BANDS = ("35-44c", "45-62c", "63-70c")
LANE_SIDES = ("YES", "NO")
MODE_MULTIPLIERS = {
    "live_full": 1.0,
    "live_reduced": 0.5,
    "probation": 0.25,
    "shadow": 0.0,
}

_SECRET_MARKERS = (
    "API_KEY",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
)
_NON_STRATEGY_KEYS = {
    "CRYPTO_EXECUTION_MODE",
    "CRYPTO_RUN_LOOP",
    "CRYPTO_LOG_MAX_MB",
    "CRYPTO_EVENTS_MAX_MB",
    "CRYPTO_LOG_ROTATION_BACKUPS",
    "CRYPTO_LOG_TOP_CANDIDATES",
    "CRYPTO_EVENT_TOP_CANDIDATES",
}
_CODE_FILES = (
    "crypto_automation_control.py",
    "crypto_paper_bettor.py",
    "crypto_pricing.py",
    "crypto_microstructure.py",
    "crypto_market_stream.py",
    "crypto_live_campaign.py",
    "crypto_15m_learning.py",
    "crypto_scan_intelligence.py",
    "crypto_dynamic_qualification.py",
    "crypto_btc_15m_sprint.py",
    "crypto_spot_flow_live_pilot.py",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now(now: datetime | None = None) -> str:
    return (now or utc_now()).astimezone(timezone.utc).isoformat()


def parse_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _integer(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _boolean(value, default=False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def price_band(value) -> str:
    price = _number(value, -1)
    if price < 0:
        return "no_price"
    if price < 20:
        return "<20c"
    if price < 35:
        return "20-34c"
    if price < 45:
        return "35-44c"
    if price < 63:
        return "45-62c"
    if price <= 70:
        return "63-70c"
    if price <= 80:
        return "71-80c"
    return "80c+"


def lane_key(row) -> str:
    side = str((row or {}).get("side") or "").strip().upper()
    return f"{price_band(executed_entry_price(row))}:{side}"


def executed_entry_price(row) -> float:
    live_order = (row or {}).get("live_order") or {}
    executed = _number(live_order.get("executed_price"))
    return executed if executed > 0 else _number((row or {}).get("entry_price"))


def _loaded_code_hash(root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    for name in _CODE_FILES:
        path = root / name
        digest.update(name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()[:16]


LOADED_CODE_HASH = _loaded_code_hash()


def strategy_identity(settings, learned_state=None) -> dict:
    selected = {}
    for raw_key, raw_value in sorted((settings or {}).items()):
        key = str(raw_key)
        if not key.startswith(("CRYPTO_", "MULTI_MARKET_")):
            continue
        if key in _NON_STRATEGY_KEYS or any(marker in key for marker in _SECRET_MARKERS):
            continue
        if key.endswith("CAMPAIGN_ID") or "RESET_AT" in key:
            continue
        selected[key] = str(raw_value)
    feature_schema = str(
        (learned_state or {}).get("feature_schema_version") or "unavailable"
    )
    payload = {
        "strategy_version": STRATEGY_VERSION,
        "code_hash": LOADED_CODE_HASH,
        "feature_schema_version": feature_schema,
        "settings": selected,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "strategy_version": STRATEGY_VERSION,
        "control_version": VERSION,
        "code_hash": LOADED_CODE_HASH,
        "config_hash": hashlib.sha256(
            json.dumps(selected, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16],
        "feature_schema_version": feature_schema,
        "fingerprint": fingerprint,
    }


def identity_matches(row, identity) -> bool:
    row_identity = (row or {}).get("strategy_identity") or {}
    return bool(
        identity.get("fingerprint")
        and row_identity.get("fingerprint") == identity.get("fingerprint")
    )


def is_recovery_candidate(candidate) -> bool:
    """Recovery entries remain outside the governor's mutation authority."""
    campaign = (candidate or {}).get("crypto_live_campaign") or {}
    if not campaign:
        return False
    bounded = campaign.get("bounded_unit_recovery") or {}
    high_water = campaign.get("high_water_drawdown") or {}
    outstanding_drawdown = _number(
        campaign.get("outstanding_drawdown")
        if campaign.get("outstanding_drawdown") is not None
        else high_water.get("outstanding_drawdown")
    )
    cycle_goal = _number(campaign.get("cycle_goal"))
    full_target = _number(campaign.get("full_recovery_target_profit"))
    realized = _number(campaign.get("cycle_realized_profit"))
    recovery_mode = str(campaign.get("recovery_mode") or "").strip().lower()
    if bounded and bounded.get("enabled") is False and recovery_mode in {
        "",
        "unit_flat",
    }:
        return False
    return bool(
        outstanding_drawdown > 0.009
        or bounded.get("drawdown_active")
        or campaign.get("partial_recovery_active")
        or realized < -0.009
        or full_target > cycle_goal + 0.009
        or recovery_mode in {
            "partial",
            "support",
            "directional_cooldown",
            "bounded_unit_recovery",
            "bounded_unit_recovery_base_only",
        }
    )


def empty_governor_state(identity=None, now=None) -> dict:
    stamp = iso_now(now)
    return {
        "version": VERSION,
        "updated_at": stamp,
        "strategy_identity": identity or {},
        "execution_mode": "report_only",
        "bootstrap": {},
        "lanes": {},
        "history": [],
        "protected_controls": {
            "recovery_settings": list(PROTECTED_RECOVERY_SETTINGS),
            "settlement": True,
            "fees": True,
            "api_credentials": True,
            "bankroll_limits": True,
            "strong_opposing_flow_veto": True,
        },
    }


def load_governor_state(path: Path = STATE_FILE) -> dict:
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def _normal_probability_positive(mean, sample_sd, count) -> float:
    if count <= 1 or sample_sd <= 1e-12:
        if mean > 0:
            return 1.0
        if mean < 0:
            return 0.0
        return 0.5
    standard_error = sample_sd / math.sqrt(count)
    z_score = mean / max(standard_error, 1e-12)
    return max(0.0, min(1.0, 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))))


def _market_metrics(rows, horizon=75) -> dict:
    ordered = sorted(rows, key=lambda row: str(row.get("settled_at") or ""))
    recent = ordered[-max(1, int(horizon)) :]
    stake = sum(_number(row.get("stake")) for row in recent)
    profit = sum(_number(row.get("profit")) for row in recent)
    returns = [
        _number(row.get("profit")) / _number(row.get("stake"))
        for row in recent
        if _number(row.get("stake")) > 0
    ]
    mean_return = statistics.fmean(returns) if returns else 0.0
    sample_sd = statistics.stdev(returns) if len(returns) > 1 else 0.0
    standard_error = sample_sd / math.sqrt(len(returns)) if returns else 0.0
    probability = _normal_probability_positive(mean_return, sample_sd, len(returns))
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for row in recent:
        cumulative += _number(row.get("profit"))
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return {
        "independent_markets": len(recent),
        "stake": round(stake, 4),
        "profit": round(profit, 4),
        "roi_pct": round(100.0 * profit / stake, 4) if stake else 0.0,
        "constant_risk_mean_return_pct": round(100.0 * mean_return, 4),
        "return_lower_90_pct": round(100.0 * (mean_return - 1.645 * standard_error), 4),
        "return_upper_90_pct": round(100.0 * (mean_return + 1.645 * standard_error), 4),
        "probability_positive_roi": round(probability, 6),
        "maximum_drawdown": round(drawdown, 4),
        "average_stake": round(stake / len(recent), 4) if recent else 0.0,
        "first_settled_at": recent[0].get("settled_at") if recent else None,
        "last_settled_at": recent[-1].get("settled_at") if recent else None,
    }


def _deduplicated_live_rows(portfolio, identity) -> dict:
    grouped = {}
    for bet in (portfolio or {}).get("bets") or []:
        if bet.get("status") != "settled" or not identity_matches(bet, identity):
            continue
        if str(bet.get("market_lane") or "") != "crypto_15m":
            continue
        owner = str(bet.get("strategy_owner") or "").lower()
        if "user" in owner or bet.get("excluded_from_bot_analytics"):
            continue
        lane = lane_key(bet)
        if lane.split(":", 1)[0] not in LANE_BANDS:
            continue
        ticker = str(bet.get("ticker") or bet.get("id") or "")
        key = (lane, ticker)
        row = grouped.setdefault(
            key,
            {
                "lane": lane,
                "ticker": ticker,
                "stake": 0.0,
                "profit": 0.0,
                "settled_at": bet.get("settled_at") or bet.get("close_time"),
            },
        )
        row["stake"] += _number(bet.get("stake"))
        row["profit"] += _number(bet.get("profit"))
        if str(bet.get("settled_at") or "") > str(row.get("settled_at") or ""):
            row["settled_at"] = bet.get("settled_at")
    by_lane = {f"{band}:{side}": [] for band in LANE_BANDS for side in LANE_SIDES}
    for row in grouped.values():
        by_lane.setdefault(row["lane"], []).append(row)
    return by_lane


def _deduplicated_shadow_rows(ledger, identity, lane, changed_at=None) -> list:
    grouped = {}
    cutoff = parse_time(changed_at)
    for row in (ledger or {}).get("records") or []:
        if row.get("status") != "settled" or row.get("cohort") != "adaptive_lane_shadow":
            continue
        if not identity_matches(row, identity) or lane_key(row) != lane:
            continue
        settled_at = parse_time(row.get("settled_at"))
        if cutoff and (not settled_at or settled_at <= cutoff):
            continue
        ticker = str(row.get("ticker") or row.get("id") or "")
        grouped.setdefault(
            ticker,
            {
                "lane": lane,
                "ticker": ticker,
                "stake": _number(row.get("virtual_stake")),
                "profit": _number(row.get("virtual_profit")),
                "settled_at": row.get("settled_at"),
            },
        )
    return list(grouped.values())


def _count_since(rows, changed_at) -> int:
    cutoff = parse_time(changed_at)
    if not cutoff:
        return len(rows)
    return sum(
        1
        for row in rows
        if parse_time(row.get("settled_at")) and parse_time(row.get("settled_at")) > cutoff
    )


def _transition(state, lane_name, lane, target, reason, now) -> None:
    prior = lane.get("mode") or "live_full"
    if prior == target:
        return
    lane.update(
        {
            "mode": target,
            "stake_multiplier": MODE_MULTIPLIERS[target],
            "changed_at": iso_now(now),
            "change_reason": reason,
        }
    )
    state.setdefault("history", []).append(
        {
            "changed_at": iso_now(now),
            "lane": lane_name,
            "from": prior,
            "to": target,
            "reason": reason,
        }
    )


def evaluate_governor(
    settings,
    portfolio,
    learned_state=None,
    rejection_shadow=None,
    prior_state=None,
    *,
    apply=False,
    now=None,
) -> dict:
    current = now or utc_now()
    identity = strategy_identity(settings, learned_state)
    state = prior_state if isinstance(prior_state, dict) else {}
    if state.get("strategy_identity", {}).get("fingerprint") != identity["fingerprint"]:
        old_identity = state.get("strategy_identity") or {}
        old_history = list(state.get("history") or [])[-100:]
        state = empty_governor_state(identity, current)
        state["history"] = old_history
        if old_identity:
            state["history"].append(
                {
                    "changed_at": iso_now(current),
                    "reason": "strategy_identity_changed",
                    "from_fingerprint": old_identity.get("fingerprint"),
                    "to_fingerprint": identity.get("fingerprint"),
                }
            )
    state["strategy_identity"] = identity
    live_rows = _deduplicated_live_rows(portfolio, identity)
    unique_markets = {row["ticker"] for rows in live_rows.values() for row in rows}
    bootstrap_minimum = max(
        1,
        _integer(settings.get("CRYPTO_ADAPTIVE_BOOTSTRAP_MIN_MARKETS"), 100),
    )
    enabled = _boolean(settings.get("CRYPTO_ADAPTIVE_GOVERNOR_ENABLED"), False)
    bootstrap_ready = len(unique_markets) >= bootstrap_minimum
    state["execution_mode"] = "adaptive" if enabled and bootstrap_ready else "report_only"
    state["bootstrap"] = {
        "enabled": enabled,
        "minimum_independent_markets": bootstrap_minimum,
        "version_matched_independent_markets": len(unique_markets),
        "ready": bootstrap_ready,
        "reason": "ready" if bootstrap_ready else "collecting_version_matched_markets",
    }

    demotion_markets = max(
        25, _integer(settings.get("CRYPTO_ADAPTIVE_DEMOTION_MIN_MARKETS"), 75)
    )
    transition_markets = max(
        10, _integer(settings.get("CRYPTO_ADAPTIVE_TRANSITION_MIN_MARKETS"), 25)
    )
    full_promotion_markets = max(
        transition_markets,
        _integer(settings.get("CRYPTO_ADAPTIVE_FULL_PROMOTION_MIN_MARKETS"), 50),
    )
    shadow_markets = max(
        25, _integer(settings.get("CRYPTO_ADAPTIVE_SHADOW_PROMOTION_MIN_MARKETS"), 100)
    )
    drawdown_limit = abs(_number(settings.get("CRYPTO_ADAPTIVE_DRAWDOWN_DOLLARS"), 300))
    negative_roi = _number(settings.get("CRYPTO_ADAPTIVE_NEGATIVE_ROI_PCT"), -10)
    demotion_probability = _number(
        settings.get("CRYPTO_ADAPTIVE_DEMOTION_MAX_PROBABILITY"), 0.20
    )
    promotion_probability = _number(
        settings.get("CRYPTO_ADAPTIVE_PROMOTION_MIN_PROBABILITY"), 0.90
    )
    recovery_fraction = max(
        0.0,
        min(1.0, _number(settings.get("CRYPTO_ADAPTIVE_DEFICIT_RECOVERY_FRACTION"), 2 / 3)),
    )

    for band in LANE_BANDS:
        for side in LANE_SIDES:
            lane_name = f"{band}:{side}"
            rows = live_rows.get(lane_name) or []
            all_metrics = _market_metrics(rows, max(len(rows), 1))
            recent_metrics = _market_metrics(rows, demotion_markets)
            lane = state.setdefault("lanes", {}).setdefault(
                lane_name,
                {
                    "mode": "live_full",
                    "stake_multiplier": 1.0,
                    "changed_at": iso_now(current),
                    "change_reason": "initial_state",
                },
            )
            lane["metrics"] = {"all": all_metrics, "recent": recent_metrics}
            lane["settled_since_change"] = _count_since(rows, lane.get("changed_at"))
            lane["recovery_exempt"] = True
            lane["last_evaluated_at"] = iso_now(current)
            if not (apply and state["execution_mode"] == "adaptive"):
                continue

            mode = lane.get("mode") or "live_full"
            probability = _number(recent_metrics.get("probability_positive_roi"), 0.5)
            roi = _number(recent_metrics.get("roi_pct"))
            profit = _number(recent_metrics.get("profit"))
            settled_since = lane["settled_since_change"]
            negative_evidence = probability <= demotion_probability and (
                profit <= -drawdown_limit or roi <= negative_roi
            )

            if mode == "live_full" and recent_metrics["independent_markets"] >= demotion_markets and negative_evidence:
                lane["demotion_deficit"] = round(min(profit, -drawdown_limit), 4)
                lane["reference_stake"] = max(0.01, _number(recent_metrics.get("average_stake"), 1))
                lane["promotion_path"] = False
                _transition(state, lane_name, lane, "live_reduced", "negative_lane_evidence", current)
            elif mode == "live_reduced" and settled_since >= transition_markets:
                if negative_evidence:
                    lane["demotion_deficit"] = round(
                        min(_number(lane.get("demotion_deficit")), profit, -drawdown_limit),
                        4,
                    )
                    _transition(state, lane_name, lane, "shadow", "negative_evidence_persisted", current)
                elif lane.get("promotion_path") and settled_since >= full_promotion_markets and probability >= 0.75 and roi > 0:
                    lane["promotion_path"] = False
                    _transition(state, lane_name, lane, "live_full", "reduced_lane_validation_passed", current)
                elif not lane.get("promotion_path") and probability >= 0.75 and roi > 0:
                    _transition(state, lane_name, lane, "live_full", "lane_recovered_before_shadow", current)
            elif mode == "probation" and settled_since >= transition_markets:
                if probability <= demotion_probability or roi <= negative_roi:
                    _transition(state, lane_name, lane, "shadow", "probation_failed", current)
                elif probability >= 0.75 and roi > 0:
                    lane["promotion_path"] = True
                    _transition(state, lane_name, lane, "live_reduced", "probation_passed", current)
            elif mode == "shadow":
                shadow_rows = _deduplicated_shadow_rows(
                    rejection_shadow or {}, identity, lane_name, lane.get("changed_at")
                )
                shadow_metrics = _market_metrics(shadow_rows, max(len(shadow_rows), 1))
                reference_stake = max(0.01, _number(lane.get("reference_stake"), 1))
                normalized_recovery = sum(
                    (_number(row.get("profit")) / max(_number(row.get("stake")), 1e-9))
                    * reference_stake
                    for row in shadow_rows
                )
                deficit = min(-0.01, _number(lane.get("demotion_deficit"), -drawdown_limit))
                remaining_deficit = deficit + normalized_recovery
                recovery_target = deficit * (1.0 - recovery_fraction)
                lane["shadow_metrics"] = shadow_metrics
                lane["counterfactual_remaining_deficit"] = round(remaining_deficit, 4)
                if (
                    shadow_metrics["independent_markets"] >= shadow_markets
                    and _number(shadow_metrics.get("probability_positive_roi")) >= promotion_probability
                    and _number(shadow_metrics.get("roi_pct")) > 0
                    and remaining_deficit >= recovery_target
                ):
                    lane["promotion_path"] = True
                    _transition(state, lane_name, lane, "probation", "shadow_recovery_evidence_passed", current)

    state["history"] = list(state.get("history") or [])[-200:]
    state["updated_at"] = iso_now(current)
    return state


def candidate_control(settings, candidate, state=None) -> dict:
    control = {
        "version": VERSION,
        "enabled": _boolean((settings or {}).get("CRYPTO_ADAPTIVE_GOVERNOR_ENABLED"), False),
        "execution_mode": "report_only",
        "lane": lane_key(candidate),
        "mode": "live_full",
        "stake_multiplier": 1.0,
        "shadow": False,
        "recovery_exempt": False,
        "reason": "disabled",
    }
    if not control["enabled"] or str((candidate or {}).get("market_lane") or "") != "crypto_15m":
        return control
    state = state if isinstance(state, dict) else load_governor_state()
    identity = (candidate or {}).get("strategy_identity") or {}
    if not identity.get("fingerprint") or state.get("strategy_identity", {}).get("fingerprint") != identity.get("fingerprint"):
        control["reason"] = "strategy_identity_mismatch"
        return control
    control["execution_mode"] = str(state.get("execution_mode") or "report_only")
    lane = (state.get("lanes") or {}).get(control["lane"]) or {}
    control["mode"] = str(lane.get("mode") or "live_full")
    control["stake_multiplier"] = _number(
        lane.get("stake_multiplier"), MODE_MULTIPLIERS.get(control["mode"], 1.0)
    )
    if control["execution_mode"] != "adaptive":
        control.update({"mode": "live_full", "stake_multiplier": 1.0, "reason": "bootstrap_report_only"})
        return control
    if is_recovery_candidate(candidate):
        control.update(
            {
                "mode": "live_full",
                "stake_multiplier": 1.0,
                "recovery_exempt": True,
                "reason": "recovery_protected",
            }
        )
        return control
    control["shadow"] = control["mode"] == "shadow"
    control["reason"] = "adaptive_lane_state"
    return control


def apply_candidate_stake_multiplier(candidate, stake) -> float:
    control = (candidate or {}).get("adaptive_control") or {}
    if control.get("recovery_exempt") or control.get("execution_mode") != "adaptive":
        return round(max(0.0, _number(stake)), 2)
    multiplier = max(0.0, min(1.0, _number(control.get("stake_multiplier"), 1.0)))
    return round(max(0.0, _number(stake)) * multiplier, 2)


def _pid_exists(pid) -> bool:
    from process_supervision import native_process
    return native_process(pid).get("running") is True


def _age_seconds(value, now) -> float | None:
    parsed = parse_time(value)
    return max(0.0, (now - parsed).total_seconds()) if parsed else None


def build_health_report(root: Path = ROOT, now=None) -> dict:
    current = (now or utc_now()).astimezone(timezone.utc)
    settings = read_json(root / "crypto_settings.json", {})
    report = read_json(root / "crypto_live_report.json", {})
    portfolio = read_json(root / "crypto_live_portfolio.json", {"bets": []})
    pid_file = read_json(root / ".crypto_bot.pid", {})
    process_file = read_json(root / "bot_processes.json", {})
    checks = []
    patient = report.get("eth_patient_promotion") or {}
    patient_selected = bool(
        _boolean(settings.get("CRYPTO_ETH_PATIENT_ONLY_LIVE"))
        and patient.get("owner") == "crypto_eth_patient_recovery"
        and patient.get("version") == "eth-patient-bankroll-v1"
    )
    legacy_checks = {"btc_15m_sprint_shadow_collection", "continuous_opportunity_workflow",
                     "campaign_bot_availability", "continuous_high_water_ledgers", "pooled_high_water_recovery_ledger",
                     "spot_flow_live_pilot_isolation", "spot_flow_live_pilot_fail_closed_bankroll",
                     "spot_flow_live_pilot_validation", "live_readiness"}

    def add(name, ok, severity, detail=None):
        checks.append(
            {
                "name": name,
                "ok": bool(ok),
                "severity": severity,
                "detail": detail,
                "applicable": not (patient_selected and name in legacy_checks),
            }
        )

    if patient_selected:
        patient_enabled = _boolean(settings.get("CRYPTO_ETH_PATIENT_ENABLED"))
        required_true = ("CRYPTO_ETH_PATIENT_ONLY_LIVE", "CRYPTO_15M_SPRINT_SHADOW_ONLY",
                         "CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL", "CRYPTO_RECONCILE_LIVE_ON_SCAN",
                         "CRYPTO_LIVE_DEPTH_PREFLIGHT_ENABLED")
        required_false = ("CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED", "CRYPTO_LIVE_CORE_ENABLED",
                          "MULTI_MARKET_LIVE_ENABLED", "MULTI_MARKET_COMMODITY_LIVE_ENABLED")
        failures = [key for key in required_true if not _boolean(settings.get(key))]
        failures += [key for key in required_false if _boolean(settings.get(key))]
        if abs(_number(settings.get("CRYPTO_ETH_PATIENT_BASE_STAKE_PCT")) - 1) > 1e-12:
            failures.append("CRYPTO_ETH_PATIENT_BASE_STAKE_PCT")
        if patient_enabled:
            if not _boolean(settings.get("CRYPTO_LIVE_ORDER_ENABLED")) or _boolean(settings.get("CRYPTO_LIVE_DRY_RUN")):
                failures.append("patient_live_switches")
        add("patient_execution_controls", not failures, "critical", {"failures": failures, "entries_enabled": patient_enabled})
        add("patient_worker_acknowledged", (patient.get("status") != "disabled") == patient_enabled,
            "warning", {"runtime_status": patient.get("status"), "entries_enabled": patient_enabled})
        unresolved = [ticker for ticker, row in ((portfolio.get("eth_patient_promotion") or {}).get("records") or {}).items()
                      if row.get("status") in {"submitting", "order_uncertain"}]
        add("patient_order_intents", not unresolved, "critical", unresolved)
        patient_account = ((report.get("live_reconciliation") or {}).get("account") or {})
        add("patient_account_complete", all(patient_account.get(key) is True for key in ("ok", "orders_complete", "positions_complete")),
            "critical", {key: patient_account.get(key) for key in ("ok", "orders_complete", "positions_complete")})

    add("crypto_worker_process", _pid_exists(pid_file.get("pid")), "critical", {"pid": pid_file.get("pid")})
    crypto_process = (process_file or {}).get("crypto") or {}
    worker_alive = _pid_exists(pid_file.get("pid"))
    registered_alive = _pid_exists(crypto_process.get("pid"))
    same_worker = str(crypto_process.get("pid")) == str(pid_file.get("pid"))
    add("crypto_wrapper_process", registered_alive, "warning" if worker_alive else "critical",
        {"pid": crypto_process.get("pid"), "role": "worker" if same_worker else "wrapper"})
    add("crypto_process_registry_consistent", bool(crypto_process.get("running")) == registered_alive,
        "warning", {"registered_running": bool(crypto_process.get("running")), "process_exists": registered_alive})
    report_age = _age_seconds(report.get("generated_at"), current)
    add(
        "live_report_fresh",
        report_age is not None and report_age <= 600,
        "critical",
        {"age_seconds": round(report_age, 3) if report_age is not None else None},
    )
    add("execution_mode_live", report.get("mode") == "live", "critical", {"mode": report.get("mode")})

    campaign = report.get("crypto_15m_campaign") or {}
    sprint = report.get("btc_15m_sprint") or {}
    sprint_freeze = sprint.get("execution_freeze") or {}
    sprint_status = str(sprint.get("status") or "")
    prospective = report.get("prospective_shadow") or {}
    intentional_research_freeze = bool(
        prospective.get("mode") == "shadow_only"
        and prospective.get("automatic_promotion") is False
        and parse_time(prospective.get("registered_at"))
        and not _boolean(settings.get("CRYPTO_LIVE_ORDER_ENABLED"))
        and not _boolean(settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED"))
    )
    sprint_freeze_active = bool(
        sprint.get("mode") == "paper_shadow_only"
        and sprint.get("affects_execution") is False
        and sprint.get("automatic_promotion") is False
        and sprint_status
        in {"COLLECTING", "FINALIZING", "PASS", "FAIL", "INCONCLUSIVE"}
        and sprint_freeze.get("healthy_intentional_state") is True
    ) or intentional_research_freeze
    add(
        "btc_15m_sprint_shadow_collection",
        sprint_freeze_active,
        "critical",
        {
            "status": sprint_status,
            "policy_hash": (sprint.get("configuration") or {}).get("policy_hash"),
            "prospective_started_at": sprint.get("prospective_started_at"),
            "prospective_ends_at": sprint.get("prospective_ends_at"),
            "freeze": sprint_freeze,
        },
    )
    configured_bot_count = _integer(
        campaign.get("configured_bot_count"),
        _integer(settings.get("CRYPTO_15M_CAMPAIGN_BOT_COUNT"), 0),
    )
    continuous_detail = {
        "continuous_opportunity_mode": campaign.get("continuous_opportunity_mode"),
        "progression_mode": campaign.get("progression_mode"),
        "cycle_workflow_enabled": campaign.get("cycle_workflow_enabled"),
        "cycle_tracking_only": campaign.get("cycle_tracking_only"),
        "cycle_completion_blocks": campaign.get("cycle_completion_blocks"),
        "target_remaining": campaign.get("target_remaining"),
        "configured_bot_count": configured_bot_count,
        "max_open_per_bot": campaign.get("max_open_per_bot"),
    }
    add(
        "continuous_opportunity_workflow",
        sprint_freeze_active
        or (
            campaign.get("continuous_opportunity_mode") is True
            and campaign.get("progression_mode") == "continuous_opportunity_units"
            and campaign.get("cycle_workflow_enabled") is False
            and campaign.get("cycle_tracking_only") is False
            and campaign.get("cycle_completion_blocks") is False
            and abs(_number(campaign.get("target_remaining"))) < 0.005
            and configured_bot_count == 3
            and _integer(campaign.get("max_open_per_bot"), 0) == 1
        ),
        "critical",
        continuous_detail,
    )
    availability_reported = "available_bot_count" in campaign
    available_bot_count = _integer(campaign.get("available_bot_count"), 0)
    campaign_open_count = _integer(campaign.get("bot_live_open_count"), 0)
    add(
        "campaign_bot_availability",
        not availability_reported
        or (
            configured_bot_count > 0
            and (available_bot_count > 0 or campaign_open_count > 0)
        ),
        "warning",
        {
            "configured_bot_count": configured_bot_count,
            "availability_reported": availability_reported,
            "available_bot_count": available_bot_count,
            "open_count": campaign_open_count,
            "status": campaign.get("status"),
            "unit_capacity_limited_bot_numbers": campaign.get(
                "unit_capacity_limited_bot_numbers"
            ) or [],
        },
    )

    ledgers = portfolio.get("crypto_15m_high_water_ledgers") or {}
    ledger_issues = []
    for bot_number in range(1, configured_bot_count + 1):
        ledger = ledgers.get(str(bot_number)) or {}
        current_profit = _number(ledger.get("current_profit"))
        high_water_profit = _number(ledger.get("high_water_profit"))
        outstanding_drawdown = _number(ledger.get("outstanding_drawdown"))
        expected_drawdown = max(0.0, high_water_profit - current_profit)
        if (
            ledger.get("mode") != "continuous_high_water_drawdown"
            or not parse_time(ledger.get("migration_cutoff"))
            or outstanding_drawdown < -0.005
            or abs(outstanding_drawdown - expected_drawdown) > 0.011
        ):
            ledger_issues.append({
                "bot_number": bot_number,
                "mode": ledger.get("mode"),
                "migration_cutoff": ledger.get("migration_cutoff"),
                "current_profit": current_profit,
                "high_water_profit": high_water_profit,
                "outstanding_drawdown": outstanding_drawdown,
                "expected_drawdown": round(expected_drawdown, 2),
            })
    add(
        "continuous_high_water_ledgers",
        sprint_freeze_active or (configured_bot_count == 3 and not ledger_issues),
        "critical",
        ledger_issues,
    )
    pooled_ledger = portfolio.get("crypto_15m_pooled_high_water_ledger") or {}
    pooled_current_profit = _number(pooled_ledger.get("current_profit"))
    pooled_high_water_profit = _number(pooled_ledger.get("high_water_profit"))
    pooled_drawdown = _number(pooled_ledger.get("outstanding_drawdown"))
    expected_pooled_drawdown = max(
        0.0,
        pooled_high_water_profit - pooled_current_profit,
    )
    pooled_detail = {
        "mode": pooled_ledger.get("mode"),
        "migration_cutoff": pooled_ledger.get("migration_cutoff"),
        "current_profit": pooled_current_profit,
        "high_water_profit": pooled_high_water_profit,
        "outstanding_drawdown": pooled_drawdown,
        "expected_drawdown": round(expected_pooled_drawdown, 2),
        "report_pooled_recovery_enabled": campaign.get("pooled_recovery_enabled"),
        "open_overlay_count": ((campaign.get("recovery_reservations") or {}).get("open_overlay_count")),
    }
    add(
        "pooled_high_water_recovery_ledger",
        sprint_freeze_active
        or (
            campaign.get("pooled_recovery_enabled") is True
            and pooled_ledger.get("mode") == "pooled_continuous_high_water_drawdown"
            and bool(parse_time(pooled_ledger.get("migration_cutoff")))
            and pooled_drawdown >= -0.005
            and abs(pooled_drawdown - expected_pooled_drawdown) <= 0.011
        ),
        "critical",
        pooled_detail,
    )

    micro = report.get("crypto_15m_microstructure") or {}
    add("coinbase_stream", micro.get("coinbase_stream_connected") is True, "warning", micro.get("coinbase_stream_error"))
    add("kraken_stream", micro.get("kraken_stream_connected") is True, "warning", micro.get("kraken_stream_error"))
    kalshi_stream = micro.get("kalshi_stream") or {}
    kalshi_message_age = _age_seconds(kalshi_stream.get("last_message_at"), current)
    add(
        "kalshi_stream",
        kalshi_stream.get("connected") is True
        and kalshi_stream.get("thread_alive") is True
        and kalshi_message_age is not None
        and kalshi_message_age <= 180,
        "critical",
        {
            "last_message_age_seconds": round(kalshi_message_age, 3) if kalshi_message_age is not None else None,
            "last_error": kalshi_stream.get("last_error"),
            "sequence_gap_count": kalshi_stream.get("sequence_gap_count"),
        },
    )
    add(
        "coinbase_sequence_gaps",
        _integer(micro.get("coinbase_trade_gap_count")) <= 3,
        "warning",
        {
            "gaps": _integer(micro.get("coinbase_trade_gap_count")),
            "backfills": _integer(micro.get("coinbase_trade_backfill_count")),
        },
    )
    reconciliation = report.get("live_reconciliation") or {}
    add("account_reconciliation", (reconciliation.get("account") or {}).get("ok") is True, "critical")
    pilot = report.get("spot_flow_live_pilot") or {}
    pilot_config = pilot.get("configuration") or {}
    pilot_risk = pilot.get("risk") or {}
    pilot_bankroll = pilot_risk.get("bankroll") or {}
    pilot_cash = _number(pilot_bankroll.get("cash_balance"))
    pilot_minimum = _number(pilot_config.get("minimum_bankroll"), 0)
    prospective = report.get("prospective_shadow") or {}
    intentional_research_freeze = bool(
        prospective.get("mode") == "shadow_only"
        and prospective.get("automatic_promotion") is False
        and not _boolean(settings.get("CRYPTO_LIVE_ORDER_ENABLED"))
        and not _boolean(settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED"))
    )
    add(
        "spot_flow_live_pilot_isolation",
        (
            (pilot_config.get("enabled") is True or intentional_research_freeze)
            and abs(_number(pilot_config.get("stake_fraction")) - 0.003) < 1e-12
            and abs(_number(pilot_config.get("daily_loss_fraction")) - 0.009) < 1e-12
            and abs(_number(pilot_config.get("open_exposure_fraction")) - 0.009) < 1e-12
            and _integer(pilot_config.get("maximum_contracts")) == 5
            and _number(pilot_config.get("maximum_adverse_move_cents")) <= 1
            and _number(
                pilot_config.get("maximum_pair_capture_delta_seconds"), 999
            ) <= 2
            and _integer(pilot_config.get("maximum_open_per_expiry")) <= 2
        ),
        "critical",
        {
            "mode": pilot.get("mode"),
            "configuration": pilot_config,
            "risk": pilot_risk,
        },
    )
    add(
        "spot_flow_live_pilot_fail_closed_bankroll",
        pilot_cash >= pilot_minimum
        or _integer(pilot.get("eligible_candidates")) == 0,
        "critical",
        {
            "cash_balance": pilot_cash,
            "minimum_bankroll": pilot_minimum,
            "eligible_candidates": pilot.get("eligible_candidates"),
            "blocking": pilot_risk.get("reasons") or [],
        },
    )
    add(
        "spot_flow_live_pilot_validation",
        bool((pilot.get("validation") or {}).get("eligible")) or intentional_research_freeze,
        "warning",
        (pilot.get("validation") or {}).get("gates") or {},
    )
    readiness = reconciliation.get("readiness") or {}
    readiness_blocking = set(readiness.get("blocking") or [])
    intentional_freeze_blocking = {
        "live_order_enabled",
        "sprint_shadow_only_disabled",
        "dry_run_disabled",
    }
    add(
        "live_readiness",
        readiness.get("ok") is True
        or (
            sprint_freeze_active
            and readiness_blocking
            and readiness_blocking.issubset(intentional_freeze_blocking)
        ),
        "critical",
        {
            "blocking": sorted(readiness_blocking),
            "intentional_sprint_freeze": sprint_freeze_active,
        },
    )
    add("unmatched_local_positions", not (reconciliation.get("unmatched_local_tickers") or []), "critical", reconciliation.get("unmatched_local_tickers") or [])
    market_fetch = report.get("kalshi_market_fetch") or {}
    add("kalshi_market_fetch", not market_fetch.get("outage") and not market_fetch.get("network_failure"), "critical", market_fetch.get("errors") or [])

    market_regime = read_json(root / "crypto_market_regime.json", {})
    regime_latest = market_regime.get("latest") or {}
    regime_age = _age_seconds(regime_latest.get("generated_at"), current)
    regime_global = regime_latest.get("global") or {}
    regime_horizons = (market_regime.get("forecast_evaluation") or {}).get("horizons") or {}
    add(
        "market_regime_forecasts",
        market_regime.get("version") == "market-regime-v3"
        and regime_age is not None
        and regime_age <= 900
        and all(key in regime_horizons for key in ("1h", "4h", "24h"))
        and all(f"direction_{key}" in regime_global for key in ("1h", "4h", "24h")),
        "warning",
        {
            "version": market_regime.get("version"),
            "age_seconds": round(regime_age, 3) if regime_age is not None else None,
            "forecast_records": len(market_regime.get("forecast_records") or []),
            "horizons": sorted(regime_horizons),
        },
    )
    regime_report = report.get("market_regime") or {}
    add(
        "market_regime_execution_validation_lock",
        not regime_report.get("affects_execution")
        or regime_report.get("execution_validation_qualified") is True,
        "critical",
        {
            "affects_execution": bool(regime_report.get("affects_execution")),
            "execution_requested": bool(regime_report.get("execution_requested")),
            "execution_validation_qualified": bool(regime_report.get("execution_validation_qualified")),
        },
    )

    scan_intelligence = report.get("scan_intelligence") or {}
    intelligence_enabled = _boolean(
        settings.get("CRYPTO_SCAN_INTELLIGENCE_ENABLED"),
        True,
    )
    intelligence_validation = scan_intelligence.get("validation") or {}
    dynamic_policy = scan_intelligence.get("dynamic_policy") or {}
    dynamic_status = str(dynamic_policy.get("status") or "shadow")
    dynamic_checks = dynamic_policy.get("activation_checks") or {}
    dynamic_minimum = _integer(dynamic_policy.get("minimum_independent_markets"), 100)
    dynamic_markets = _integer(dynamic_policy.get("independent_markets"), 0)
    dynamic_live_safe = bool(
        dynamic_status != "active"
        or (
            dynamic_policy.get("qualified_for_activation") is True
            and dynamic_policy.get("affects_execution") is True
            and dynamic_markets >= dynamic_minimum
            and dynamic_checks
            and all(bool(value) for value in dynamic_checks.values())
        )
    )
    add(
        "scan_intelligence_collection",
        not intelligence_enabled
        or (
            scan_intelligence.get("schema_version") == 1
            and not scan_intelligence.get("error")
            and isinstance(scan_intelligence.get("stats"), dict)
            and intelligence_validation.get("affects_live_probability") is False
            and (
                intelligence_validation.get("affects_live_entry_rules") is False
                or dynamic_live_safe
            )
        ),
        "warning",
        {
            "enabled": intelligence_enabled,
            "schema_version": scan_intelligence.get("schema_version"),
            "candidate_records": scan_intelligence.get("candidate_records"),
            "active_records": scan_intelligence.get("active_records"),
            "validation": intelligence_validation,
            "error": scan_intelligence.get("error"),
        },
    )
    add(
        "dynamic_qualification_policy",
        not intelligence_enabled
        or (
            dynamic_policy.get("version") == "dynamic-qualification-v1"
            and dynamic_status in {"shadow", "active"}
            and dynamic_live_safe
        ),
        "critical",
        {
            "status": dynamic_status,
            "qualified_for_activation": dynamic_policy.get("qualified_for_activation"),
            "affects_execution": dynamic_policy.get("affects_execution"),
            "independent_markets": dynamic_markets,
            "minimum_independent_markets": dynamic_minimum,
            "activation_checks": dynamic_checks,
        },
    )

    recovery = {key: str(settings.get(key, "")) for key in PROTECTED_RECOVERY_SETTINGS}
    normalized_recovery = {
        key: value.strip().lower() for key, value in recovery.items()
    }
    expected_recovery = {
        key: value.strip().lower() for key, value in PROTECTED_RECOVERY_SETTINGS.items()
    }
    add("protected_recovery_settings", normalized_recovery == expected_recovery, "critical", recovery)

    unit_risk = {
        key: str(settings.get(key, ""))
        for key in PROTECTED_UNIT_RISK_SETTINGS
    }
    normalized_unit_risk = {
        key: value.strip().lower() for key, value in unit_risk.items()
    }
    expected_unit_risk = {
        key: value.strip().lower()
        for key, value in PROTECTED_UNIT_RISK_SETTINGS.items()
    }
    if patient_selected and normalized_unit_risk.get("CRYPTO_LIVE_ORDER_ENABLED") in {"true", "false"}:
        # The selected, separately checked patient profile owns the global
        # live switch. Every other protected value still compares exactly.
        expected_unit_risk["CRYPTO_LIVE_ORDER_ENABLED"] = normalized_unit_risk["CRYPTO_LIVE_ORDER_ENABLED"]
    add(
        "protected_unit_risk_settings",
        normalized_unit_risk == expected_unit_risk,
        "critical",
        unit_risk,
    )

    log_path = root / "crypto_live_log.txt"
    log_age = max(0.0, current.timestamp() - log_path.stat().st_mtime) if log_path.exists() else None
    add("live_log_fresh", log_age is not None and log_age <= 600, "critical", {"age_seconds": round(log_age, 3) if log_age is not None else None})
    recent_issues = []
    if log_path.exists():
        try:
            with log_path.open("rb") as handle:
                handle.seek(max(0, log_path.stat().st_size - 250_000))
                tail = handle.read().decode("utf-8", errors="replace")
            for line in tail.splitlines():
                match = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
                if not match:
                    continue
                try:
                    local_zone = datetime.now().astimezone().tzinfo
                    stamp = datetime.strptime(
                        match.group(1), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=local_zone).astimezone(timezone.utc)
                except ValueError:
                    stamp = None
                if stamp and (current - stamp).total_seconds() <= 75 * 60 and re.search(
                    r"\b(traceback|exception|fatal|error)\b", line, flags=re.IGNORECASE
                ):
                    recent_issues.append(line[-500:])
        except OSError:
            recent_issues.append("unable_to_read_log")
    add("recent_runtime_errors", not recent_issues, "warning", recent_issues[-20:])

    critical = [row for row in checks if row["applicable"] and not row["ok"] and row["severity"] == "critical"]
    warnings = [row for row in checks if row["applicable"] and not row["ok"] and row["severity"] == "warning"]
    status = "critical" if critical else "degraded" if warnings else "healthy"
    return {
        "version": VERSION,
        "generated_at": iso_now(current),
        "status": status,
        "active_profile": "patient_eth" if patient_selected else "legacy",
        "entries_enabled": _boolean(settings.get("CRYPTO_ETH_PATIENT_ENABLED")) if patient_selected else _boolean(settings.get("CRYPTO_LIVE_ORDER_ENABLED")),
        "critical_failures": [row["name"] for row in critical],
        "warnings": [row["name"] for row in warnings],
        "checks": checks,
        "recovery_protected": normalized_recovery == expected_recovery,
        "unit_risk_protected": normalized_unit_risk == expected_unit_risk,
    }


def run_health(write=True) -> dict:
    report = build_health_report()
    if write:
        atomic_write_json(HEALTH_FILE, report)
    return report


def run_governor(apply=False, write=True) -> dict:
    # Import lazily so the standalone governor uses the exact same defaults,
    # encrypted settings, and environment overrides as the live bot without
    # creating an import cycle when the bot imports this module.
    try:
        from crypto_paper_bettor import load_settings

        settings = load_settings()
    except ImportError:
        settings = read_json(SETTINGS_FILE, {})
    portfolio = read_json(PORTFOLIO_FILE, {"bets": []})
    learned = read_json(MODEL_STATE_FILE, {})
    rejection = read_json(REJECTION_SHADOW_FILE, {"records": []})
    state = evaluate_governor(
        settings,
        portfolio,
        learned,
        rejection,
        load_governor_state(),
        apply=apply,
    )
    if write:
        atomic_write_json(STATE_FILE, state)
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    health_parser = subparsers.add_parser("health")
    health_parser.add_argument("--no-write", action="store_true")
    governor_parser = subparsers.add_parser("govern")
    governor_parser.add_argument("--apply", action="store_true")
    governor_parser.add_argument("--no-write", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.command == "health":
        result = run_health(write=not arguments.no_write)
    else:
        result = run_governor(apply=arguments.apply, write=not arguments.no_write)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
