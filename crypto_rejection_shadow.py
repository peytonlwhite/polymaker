"""Persistent counterfactual outcomes for selected crypto campaign rejections."""

from datetime import datetime, timedelta, timezone


VERSION = "crypto-rejection-shadow-v3"
COHORT_REASONS = {
    "directional_confirmation": "campaign_directional_confirmation",
    "directional_opposition": "directional_opposition",
    "directional_flow_too_weak": "directional_flow_too_weak",
    "directional_insufficient_consensus": "directional_insufficient_consensus",
    "directional_data_unavailable": "directional_data_unavailable",
    "one_source_shadow": "directional_insufficient_consensus",
    "mid_price_6_9_edge": "campaign_mid_price_quality_filter",
    "commodity_live_shadow": "commodity_live_shadow",
    "adaptive_lane_shadow": "adaptive_lane_shadow",
}


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def empty_ledger():
    return {"version": VERSION, "mode": "paper_shadow_only", "updated_at": None, "records": []}


def normalize_ledger(value):
    ledger = value if isinstance(value, dict) else {}
    ledger["version"] = VERSION
    ledger["mode"] = "paper_shadow_only"
    ledger["records"] = ledger.get("records") if isinstance(ledger.get("records"), list) else []
    return ledger


def capture_candidates(ledger, candidates, *, now=None, virtual_stake=1.0, max_records=5000):
    ledger = normalize_ledger(ledger)
    current = _now(now)
    captured_at = current.isoformat()
    time_bucket = int(current.timestamp() // 60)
    known = {str(row.get("id") or "") for row in ledger["records"]}
    added = []
    for candidate in candidates or []:
        lane = candidate.get("market_lane")
        if lane not in {"crypto_15m", "commodity_15m"}:
            continue
        reasons = set(candidate.get("skip_reasons") or [])
        ticker = str(candidate.get("ticker") or "")
        side = str(candidate.get("side") or "").lower()
        for cohort, reason in COHORT_REASONS.items():
            if reason not in reasons or not ticker or side not in {"yes", "no"}:
                continue
            if cohort == "commodity_live_shadow":
                if lane != "commodity_15m" or reasons != {"commodity_live_shadow"}:
                    continue
            elif cohort == "adaptive_lane_shadow":
                if lane != "crypto_15m" or reasons != {"adaptive_lane_shadow"}:
                    continue
            elif lane != "crypto_15m":
                continue
            if cohort == "one_source_shadow" and not bool(
                (candidate.get("directional_confirmation") or {}).get(
                    "one_source_shadow_eligible"
                )
            ):
                continue
            if cohort == "directional_insufficient_consensus" and bool(
                (candidate.get("directional_confirmation") or {}).get(
                    "one_source_shadow_eligible"
                )
            ):
                continue
            record_id = f"{cohort}:{ticker}:{side}:{time_bucket}"
            if record_id in known:
                continue
            price = _number(candidate.get("entry_price"))
            if not 0 < price < 100:
                continue
            stake = max(0.01, _number(virtual_stake, 1.0))
            row = {
                "id": record_id,
                "status": "open",
                "mode": "paper_shadow_only",
                "strategy_owner": "crypto_rejection_shadow",
                "cohort": cohort,
                "rejection_reason": reason,
                "captured_at": captured_at,
                "capture_minute_bucket": time_bucket,
                "ticker": ticker,
                "event_ticker": candidate.get("event_ticker"),
                "series_ticker": candidate.get("series_ticker"),
                "asset": candidate.get("asset"),
                "market_lane": lane,
                "side": side,
                "entry_price": round(price, 4),
                "fee_per_contract": round(
                    max(0.0, _number(candidate.get("exact_fee_cents"))) / 100.0,
                    6,
                ) if "exact_fee_cents" in candidate else None,
                "virtual_stake": round(stake, 4),
                "virtual_contracts": round(stake / (price / 100.0), 6),
                "edge": round(_number(candidate.get("edge")), 4),
                "confidence": round(_number(candidate.get("confidence")), 4),
                "edge_low": candidate.get("edge_low"),
                "probability_net_edge_positive": candidate.get(
                    "probability_net_edge_positive"
                ),
                "data_quality": candidate.get("data_quality"),
                "adaptive_control": candidate.get("adaptive_control"),
                "strategy_identity": candidate.get("strategy_identity"),
                "strategy_version": candidate.get("strategy_version"),
                "strategy_config_hash": candidate.get("strategy_config_hash"),
                "feature_schema_version": candidate.get("feature_schema_version"),
                "model_prob_yes": candidate.get("model_prob_yes"),
                "minutes_to_close": candidate.get("minutes_to_close"),
                "close_time": candidate.get("close_time"),
                "skip_reasons": list(candidate.get("skip_reasons") or []),
                "directional_confirmation": candidate.get("directional_confirmation"),
                "result": None,
                "market_result": None,
                "virtual_profit": None,
            }
            ledger["records"].append(row)
            known.add(record_id)
            added.append(row)
    if max_records > 0 and len(ledger["records"]) > int(max_records):
        ledger["records"] = ledger["records"][-int(max_records):]
    ledger["updated_at"] = captured_at
    return added


def settle_records(ledger, fetch_market, *, fee_per_contract, now=None, grace_minutes=2, retry_minutes=5, max_checks=20):
    ledger = normalize_ledger(ledger)
    current = _now(now)
    settled = []
    checks = 0
    for row in ledger["records"]:
        if row.get("status") != "open" or checks >= max(0, int(max_checks)):
            continue
        try:
            if current < _now(row.get("close_time")) + timedelta(minutes=max(0, grace_minutes)):
                continue
        except (TypeError, ValueError):
            continue
        last_check = row.get("last_settlement_check_at")
        if last_check:
            try:
                if current < _now(last_check) + timedelta(minutes=max(0, retry_minutes)):
                    continue
            except (TypeError, ValueError):
                pass
        checks += 1
        row["last_settlement_check_at"] = current.isoformat()
        try:
            market = fetch_market(row.get("ticker")) or {}
        except Exception as exc:
            row["last_settlement_error"] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        result = str(market.get("result") or market.get("market_result") or "").lower()
        if str(market.get("status") or "").lower() != "finalized" or result not in {"yes", "no"}:
            continue
        contracts = _number(row.get("virtual_contracts"))
        stake = _number(row.get("virtual_stake"))
        stored_fee = row.get("fee_per_contract")
        fee = max(
            0.0,
            _number(stored_fee)
            if stored_fee is not None
            else _number(fee_per_contract(_number(row.get("entry_price")))),
        ) * contracts
        payout = contracts if row.get("side") == result else 0.0
        profit = payout - stake - fee
        row.update({
            "status": "settled",
            "settled_at": market.get("settlement_ts") or market.get("settled_time") or current.isoformat(),
            "settlement_source": "kalshi_finalized_market",
            "market_result": result,
            "result": "WIN" if row.get("side") == result else "LOSS",
            "virtual_fee": round(fee, 6),
            "virtual_payout": round(payout, 6),
            "virtual_profit": round(profit, 6),
            "last_settlement_error": None,
        })
        settled.append(row)
    ledger["updated_at"] = current.isoformat()
    return settled


def _summary_rows(records, cohort):
    tracked = [row for row in records if row.get("cohort") == cohort]
    settled = [row for row in tracked if row.get("status") == "settled"]
    wins = sum(row.get("result") == "WIN" for row in settled)
    stake = sum(_number(row.get("virtual_stake")) for row in settled)
    profit = sum(_number(row.get("virtual_profit")) for row in settled)
    independent = {}
    for row in sorted(settled, key=lambda value: str(value.get("captured_at") or "")):
        independent.setdefault(str(row.get("ticker") or ""), row)
    independent_rows = list(independent.values())
    independent_stake = sum(_number(row.get("virtual_stake")) for row in independent_rows)
    independent_profit = sum(_number(row.get("virtual_profit")) for row in independent_rows)
    return {
        "cohort": cohort,
        "tracked": len(tracked),
        "open": sum(row.get("status") == "open" for row in tracked),
        "settled": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": round(100.0 * wins / len(settled), 2) if settled else 0.0,
        "virtual_profit": round(profit, 4),
        "roi": round(100.0 * profit / stake, 2) if stake else 0.0,
        "independent_settled_markets": len(independent_rows),
        "market_weighted_profit": round(independent_profit, 4),
        "market_weighted_roi": (
            round(100.0 * independent_profit / independent_stake, 2)
            if independent_stake
            else 0.0
        ),
    }


def summarize(ledger):
    ledger = normalize_ledger(ledger)
    records = ledger["records"]
    cohorts = [_summary_rows(records, cohort) for cohort in COHORT_REASONS]
    return {
        "version": VERSION,
        "mode": "paper_shadow_only",
        "affects_execution": False,
        "tracked": len(records),
        "open": sum(row.get("status") == "open" for row in records),
        "settled": sum(row.get("status") == "settled" for row in records),
        "cohorts": cohorts,
        "recent_records": sorted(
            records,
            key=lambda row: row.get("settled_at") or row.get("captured_at") or "",
            reverse=True,
        )[:100],
        "updated_at": ledger.get("updated_at"),
    }
