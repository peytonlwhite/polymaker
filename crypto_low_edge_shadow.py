from datetime import datetime, timedelta, timezone


VERSION = "low-edge-65-v1"


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _iso(value=None):
    return _now(value).isoformat()


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def empty_ledger():
    return {
        "version": VERSION,
        "mode": "paper_shadow_only",
        "updated_at": None,
        "records": [],
    }


def normalize_ledger(value):
    ledger = value if isinstance(value, dict) else {}
    records = ledger.get("records")
    ledger["version"] = VERSION
    ledger["mode"] = "paper_shadow_only"
    ledger["records"] = records if isinstance(records, list) else []
    return ledger


def edge_cohort(edge):
    edge = _number(edge, -999)
    if 1.0 <= edge < 2.0:
        return "1-2%"
    if 2.0 <= edge < 3.0:
        return "2-3%"
    if 3.0 <= edge < 4.0:
        return "3-4%"
    return None


def capture_candidates(
    ledger,
    candidates,
    *,
    now=None,
    min_confidence=65.0,
    min_price=35.0,
    max_price=70.0,
    min_minutes=2.0,
    max_minutes=15.0,
    virtual_stake=1.0,
    max_records=5000,
):
    ledger = normalize_ledger(ledger)
    now_iso = _iso(now)
    known_tickers = {
        str(row.get("ticker") or "")
        for row in ledger["records"]
        if row.get("ticker")
    }
    added = []
    for candidate in candidates or []:
        ticker = str(candidate.get("ticker") or "")
        edge = _number(candidate.get("edge"), -999)
        confidence = _number(candidate.get("confidence"))
        entry_price = _number(candidate.get("entry_price"))
        minutes = _number(candidate.get("minutes_to_close"), -999)
        cohort = edge_cohort(edge)
        if (
            not ticker
            or ticker in known_tickers
            or not candidate.get("is_15m_market")
            or not candidate.get("low_edge_shadow_quality_ok")
            or cohort is None
            or confidence < min_confidence
            or entry_price < min_price
            or entry_price > max_price
            or minutes < min_minutes
            or minutes > max_minutes
        ):
            continue
        stake = max(0.01, _number(virtual_stake, 1.0))
        contracts = stake / max(0.01, entry_price / 100.0)
        row = {
            "id": f"{ticker}:{str(candidate.get('side') or '').lower()}",
            "status": "open",
            "mode": "paper_shadow_only",
            "strategy_owner": "crypto_low_edge_shadow",
            "cohort": cohort,
            "captured_at": now_iso,
            "ticker": ticker,
            "event_ticker": candidate.get("event_ticker"),
            "series_ticker": candidate.get("series_ticker"),
            "asset": candidate.get("asset"),
            "title": candidate.get("title"),
            "market_kind": candidate.get("market_kind"),
            "side": str(candidate.get("side") or "").lower(),
            "entry_price": round(entry_price, 4),
            "fee_per_contract": round(
                max(0.0, _number(candidate.get("exact_fee_cents"))) / 100.0,
                6,
            ) if "exact_fee_cents" in candidate else None,
            "virtual_stake": round(stake, 4),
            "virtual_contracts": round(contracts, 6),
            "raw_edge": candidate.get("raw_edge"),
            "estimated_fee_edge_pp": candidate.get("estimated_fee_edge_pp"),
            "net_edge": round(edge, 4),
            "confidence": round(confidence, 4),
            "model_prob_yes": candidate.get("model_prob_yes"),
            "close_time": candidate.get("close_time"),
            "minutes_to_close": round(minutes, 4),
            "initial_price_source": candidate.get("initial_price_source"),
            "market_quote_fetched_at": candidate.get("market_quote_fetched_at"),
            "quality_controls": candidate.get("low_edge_shadow_quality_controls") or {},
            "result": None,
            "market_result": None,
            "virtual_fee": None,
            "virtual_payout": None,
            "virtual_profit": None,
        }
        ledger["records"].append(row)
        known_tickers.add(ticker)
        added.append(row)
    if max_records > 0 and len(ledger["records"]) > max_records:
        ledger["records"] = ledger["records"][-max_records:]
    ledger["updated_at"] = now_iso
    return added


def settle_records(
    ledger,
    fetch_market,
    *,
    fee_per_contract,
    now=None,
    grace_minutes=2.0,
    retry_minutes=5.0,
    max_checks=20,
):
    ledger = normalize_ledger(ledger)
    current = _now(now)
    settled = []
    checks = 0
    for row in ledger["records"]:
        if row.get("status") != "open" or checks >= max(0, int(max_checks)):
            continue
        close_time = row.get("close_time")
        try:
            due = _now(close_time) + timedelta(minutes=max(0.0, grace_minutes))
        except (TypeError, ValueError):
            continue
        if current < due:
            continue
        last_check = row.get("last_settlement_check_at")
        if last_check:
            try:
                if current < _now(last_check) + timedelta(minutes=max(0.0, retry_minutes)):
                    continue
            except (TypeError, ValueError):
                pass
        checks += 1
        row["last_settlement_check_at"] = current.isoformat()
        row["settlement_check_count"] = int(row.get("settlement_check_count") or 0) + 1
        try:
            market = fetch_market(row.get("ticker")) or {}
        except Exception as exc:
            row["last_settlement_error"] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        result = str(market.get("result") or market.get("market_result") or "").lower()
        status = str(market.get("status") or "").lower()
        if status != "finalized" or result not in {"yes", "no"}:
            row["last_settlement_status"] = status or "not_finalized"
            continue
        side = str(row.get("side") or "").lower()
        contracts = _number(row.get("virtual_contracts"))
        stake = _number(row.get("virtual_stake"))
        price = _number(row.get("entry_price"))
        stored_fee = row.get("fee_per_contract")
        fee = max(
            0.0,
            _number(stored_fee)
            if stored_fee is not None
            else _number(fee_per_contract(price)),
        ) * contracts
        payout = contracts if side == result else 0.0
        profit = payout - stake - fee
        row.update({
            "status": "settled",
            "settled_at": market.get("settlement_ts") or market.get("settled_time") or current.isoformat(),
            "settlement_source": "kalshi_finalized_market",
            "market_result": result,
            "result": "WIN" if side == result else "LOSS",
            "virtual_fee": round(fee, 6),
            "virtual_payout": round(payout, 6),
            "virtual_profit": round(profit, 6),
            "last_settlement_error": None,
            "last_settlement_status": "finalized",
        })
        settled.append(row)
    ledger["updated_at"] = current.isoformat()
    return settled


def _cohort_summary(records, label):
    rows = [row for row in records if row.get("cohort") == label]
    settled = [row for row in rows if row.get("status") == "settled"]
    wins = sum(1 for row in settled if row.get("result") == "WIN")
    losses = sum(1 for row in settled if row.get("result") == "LOSS")
    stake = sum(_number(row.get("virtual_stake")) for row in settled)
    profit = sum(_number(row.get("virtual_profit")) for row in settled)
    return {
        "cohort": label,
        "tracked": len(rows),
        "open": sum(1 for row in rows if row.get("status") == "open"),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(settled) * 100.0, 2) if settled else 0.0,
        "virtual_stake": round(stake, 4),
        "virtual_profit": round(profit, 4),
        "roi": round(profit / stake * 100.0, 2) if stake else 0.0,
        "average_edge": round(
            sum(_number(row.get("net_edge")) for row in settled) / len(settled),
            3,
        ) if settled else 0.0,
        "average_confidence": round(
            sum(_number(row.get("confidence")) for row in settled) / len(settled),
            3,
        ) if settled else 0.0,
        "average_entry_price": round(
            sum(_number(row.get("entry_price")) for row in settled) / len(settled),
            3,
        ) if settled else 0.0,
    }


def summarize(ledger):
    ledger = normalize_ledger(ledger)
    records = ledger["records"]
    cohorts = [
        _cohort_summary(records, "1-2%"),
        _cohort_summary(records, "2-3%"),
        _cohort_summary(records, "3-4%"),
    ]
    settled = [row for row in records if row.get("status") == "settled"]
    wins = sum(1 for row in settled if row.get("result") == "WIN")
    profit = sum(_number(row.get("virtual_profit")) for row in settled)
    stake = sum(_number(row.get("virtual_stake")) for row in settled)
    recent = sorted(
        records,
        key=lambda row: row.get("settled_at") or row.get("captured_at") or "",
        reverse=True,
    )[:100]
    return {
        "version": VERSION,
        "mode": "paper_shadow_only",
        "affects_execution": False,
        "tracked": len(records),
        "open": sum(1 for row in records if row.get("status") == "open"),
        "settled": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": round(wins / len(settled) * 100.0, 2) if settled else 0.0,
        "virtual_profit": round(profit, 4),
        "roi": round(profit / stake * 100.0, 2) if stake else 0.0,
        "cohorts": cohorts,
        "recent_records": recent,
        "updated_at": ledger.get("updated_at"),
    }
