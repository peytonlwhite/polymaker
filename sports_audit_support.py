"""Pure record semantics shared by sports execution, training and reporting."""

from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
AUDIT_REVISION = "sports-audit-remediation-v1"
STRATEGY_VERSION = "sports-price-quality-v5-audit"


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def parse_timestamp(value, *, naive_tz=CHICAGO):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=naive_tz)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def strategy_lane(row):
    source = str(row.get("source") or "").strip()
    owner = str(row.get("strategy_owner") or "").strip()
    if source in {"manual", "user_manual", "manual_live_test"} or owner == "user_manual" or row.get("user_manual"):
        return "manual"
    if source == "trusted_capper" or owner == "trusted_capper" or row.get("trusted_capper") or row.get("trusted_capper_ticket_id"):
        return "capper"
    if source == "aibetpicks" or owner == "aibetpicks":
        return "aibetpicks"
    if (row.get("sports_game_odds_strategy") or {}).get("active"):
        return "sports_game_odds"
    if source in {"edge_scanner", "bot_pick"}:
        return "autonomous"
    if source == "candidate_observation":
        return "candidate_observation"
    return "unattributed"


def integrity_valid(row):
    return not row.get("strategy_analytics_excluded") and (row.get("strategy_integrity") or {}).get("valid") is not False


def binary_outcome(row):
    """Contract outcome, never the sign of a cashout or scalar payout's P&L."""
    if not integrity_valid(row):
        return None
    if row.get("cashout") or row.get("cashed_out") or row.get("cashout_at") or str(row.get("settlement_source") or "").lower() == "cashout":
        return None
    value = number(row.get("settlement_side_value"))
    if value is not None:
        return value / 100.0 if value in {0.0, 100.0} else None
    result = str(row.get("result") or "").upper()
    return {"WIN": 1.0, "LOSS": 0.0}.get(result)


def observation_age_minutes(row, *, now=None):
    """Measure the oldest contributing quote, not the time it was downloaded."""
    now = parse_timestamp(now or datetime.now(timezone.utc))
    stamps = row.get("contributing_updates")
    if stamps is None:
        stamps = [row.get("last_update")]
    parsed = [parse_timestamp(value, naive_tz=timezone.utc) for value in stamps]
    if not parsed or any(value is None or value > now for value in parsed):
        return None
    return max((now - value).total_seconds() / 60.0 for value in parsed)


def fill_edge(candidate, fill_price, fee_cents):
    """Record the probability basis actually available; missing is not zero."""
    if candidate.get("capper_odds_api_exhausted_fallback"):
        return {"edge": None, "net_edge": None, "fill_edge_basis": "unavailable"}
    pricing = candidate.get("pricing_v2") or {}
    basis = str(pricing.get("edge_basis") or "conservative_net")
    field = "model_prob" if basis in {"guarded_nominal_net", "nominal_net"} else "model_prob_lower"
    probability = number(candidate.get(field))
    if probability is None:
        return {"edge": None, "net_edge": None, "fill_edge_basis": "probability_unavailable", "fill_reference_probability": number(candidate.get("model_prob")), "fill_reference_type": candidate.get("pricing_basis") or "unspecified"}
    edge = round(probability - float(fill_price) - float(fee_cents), 3)
    return {"edge": edge, "net_edge": edge, "fill_edge_basis": basis}


def book_horizon_alignment(mark, *, observed_at, provider_updates, max_delay_minutes=2.0):
    captured = parse_timestamp(mark.get("captured_at"))
    observed = parse_timestamp(observed_at)
    updates = [parse_timestamp(value, naive_tz=timezone.utc) for value in provider_updates]
    valid_stamps = bool(captured and observed and updates and all(updates))
    delay = (observed - captured).total_seconds() / 60.0 if captured and observed else None
    valid = bool(valid_stamps and abs(delay) <= max_delay_minutes and all(abs((value - captured).total_seconds()) <= max_delay_minutes * 60 for value in updates))
    return {"book_time_valid": valid, "book_observed_at": observed.isoformat() if observed else None, "book_attachment_delay_minutes": round(delay, 3) if delay is not None else None, "book_provider_updates": list(provider_updates), "book_time_reason": "aligned" if valid else "missing_or_outside_horizon"}


def capper_parent_exposure_review(portfolio, commitments, metadata, stake):
    """Conserve lifetime parent units, deduplicating persisted reservation fills."""
    parent = metadata.get("parent_capper_ticket_id")
    if not parent:
        return {"ok": True, "active": False}
    cap = number(metadata.get("parlay_parent_total_unit_cap"))
    unit = number(metadata.get("unit_size"))
    if cap is None or cap <= 0 or unit is None or unit <= 0:
        return {"ok": False, "error": "capper_parent_budget_metadata_missing"}
    used = 0.0
    persisted = set()
    for row in [*(portfolio.get("bets") or []), *(portfolio.get("history") or [])]:
        if row.get("parent_capper_ticket_id") != parent:
            continue
        row_unit = number(row.get("unit_size"))
        row_stake = number(row.get("stake"))
        if row_unit is None or row_unit <= 0 or row_stake is None or row_stake < 0:
            return {"ok": False, "error": "capper_parent_historical_units_unknown"}
        used += row_stake / row_unit
        reservation_id = ((row.get("live_order") or {}).get("shared_bankroll") or {}).get("reservation_id")
        if reservation_id:
            persisted.add(reservation_id)
    for reservation_id, row in commitments.items():
        if not isinstance(row, dict):
            return {"ok": False, "error": "capper_parent_commitment_units_unknown"}
        if row.get("parent_capper_ticket_id") == parent and reservation_id not in persisted:
            units = number(row.get("units"))
            if units is None or units < 0:
                return {"ok": False, "error": "capper_parent_commitment_units_unknown"}
            used += units
    requested = max(0.0, float(stake)) / unit
    ok = used + requested <= cap + 1e-9
    return {"ok": ok, "active": True, "error": None if ok else "capper_parent_unit_cap", "used_units": round(used, 6), "requested_units": round(requested, 6), "cap_units": cap}
