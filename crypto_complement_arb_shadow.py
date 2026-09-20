"""Paper-only complementary YES/NO maker and taker-parity research.

The maker experiment posts simultaneous hypothetical one-contract YES and NO
bids and requires a public trade strictly through each limit before counting a
fill.  All unfilled and single-leg outcomes remain in the denominator.  The
taker parity diagnostic measures executable two-leg cost but never assumes a
crossed or internally inconsistent book is tradable.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo

from crypto_pricing import kalshi_order_fee


VERSION = "crypto-complement-arb-shadow-v1"
MODE = "paper_shadow_only"
CHICAGO = ZoneInfo("America/Chicago")
ONE_SIDED_95_Z = NormalDist().inv_cdf(0.95)


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _integer(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _boolean(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value=None):
    return _now(value).isoformat()


def _observation_date(value):
    try:
        return _now(value).astimezone(CHICAGO).date().isoformat()
    except (TypeError, ValueError):
        return None


def _policy_hash(config):
    payload = {key: value for key, value in config.items() if key != "policy_hash"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]


def configuration(settings=None):
    settings = settings or {}
    config = {
        "enabled": _boolean(
            settings.get("CRYPTO_COMPLEMENT_ARB_SHADOW_ENABLED", True), True
        ),
        "assets": sorted(
            {
                value.strip().upper()
                for value in str(
                    settings.get(
                        "CRYPTO_COMPLEMENT_ARB_SHADOW_ASSETS",
                        "BTC,ETH,SOL,DOGE,XRP",
                    )
                ).split(",")
                if value.strip()
            }
        ),
        "minimum_minutes": max(
            0.0,
            _number(
                settings.get("CRYPTO_COMPLEMENT_ARB_SHADOW_MIN_MINUTES", 6), 6
            ),
        ),
        "maximum_minutes": max(
            0.0,
            _number(
                settings.get("CRYPTO_COMPLEMENT_ARB_SHADOW_MAX_MINUTES", 13), 13
            ),
        ),
        "maximum_quote_age_seconds": max(
            0.1,
            _number(
                settings.get(
                    "CRYPTO_COMPLEMENT_ARB_SHADOW_MAX_QUOTE_AGE_SECONDS", 2
                ),
                2,
            ),
        ),
        "minimum_locked_profit_cents": max(
            0.0,
            _number(
                settings.get(
                    "CRYPTO_COMPLEMENT_ARB_SHADOW_MIN_LOCKED_PROFIT_CENTS", 1
                ),
                1,
            ),
        ),
        "minimum_unique_markets_interim": max(
            25,
            _integer(
                settings.get("CRYPTO_COMPLEMENT_ARB_SHADOW_INTERIM_MARKETS", 100),
                100,
            ),
        ),
        "minimum_observation_days_interim": max(
            3,
            _integer(
                settings.get("CRYPTO_COMPLEMENT_ARB_SHADOW_INTERIM_DAYS", 7), 7
            ),
        ),
        "minimum_unique_markets_final": max(
            100,
            _integer(
                settings.get("CRYPTO_COMPLEMENT_ARB_SHADOW_FINAL_MARKETS", 200),
                200,
            ),
        ),
        "minimum_observation_days_final": max(
            14,
            _integer(
                settings.get("CRYPTO_COMPLEMENT_ARB_SHADOW_FINAL_DAYS", 30), 30
            ),
        ),
        "settlement_grace_minutes": max(
            0.0,
            _number(
                settings.get(
                    "CRYPTO_COMPLEMENT_ARB_SHADOW_SETTLEMENT_GRACE_MINUTES", 2
                ),
                2,
            ),
        ),
        "settlement_retry_minutes": max(
            0.0,
            _number(
                settings.get(
                    "CRYPTO_COMPLEMENT_ARB_SHADOW_SETTLEMENT_RETRY_MINUTES", 5
                ),
                5,
            ),
        ),
        "maximum_settlement_checks_per_scan": max(
            1,
            _integer(
                settings.get(
                    "CRYPTO_COMPLEMENT_ARB_SHADOW_MAX_SETTLEMENT_CHECKS", 50
                ),
                50,
            ),
        ),
        "maximum_records": max(
            1000,
            _integer(
                settings.get("CRYPTO_COMPLEMENT_ARB_SHADOW_MAX_RECORDS", 10000),
                10000,
            ),
        ),
        "fill_model": "public_trade_strictly_through_each_limit",
        "queue_position_modeled": False,
        "historical_backfill": False,
        "affects_execution": False,
        "automatic_promotion": False,
    }
    config["policy_hash"] = _policy_hash(config)
    return config


def empty_ledger(settings=None, now=None):
    config = configuration(settings)
    registered = _iso(now)
    return {
        "version": VERSION,
        "mode": MODE,
        "registered_at": registered,
        "updated_at": registered,
        "configuration": config,
        "records": [],
        "parity_records": [],
        "last_capture_ids": [],
        "last_fill_ids": [],
        "last_settled_ids": [],
        "last_capture_funnel": {},
    }


def normalize_ledger(value, settings=None, now=None):
    ledger = value if isinstance(value, dict) else {}
    ledger["version"] = VERSION
    ledger["mode"] = MODE
    ledger.setdefault("registered_at", _iso(now))
    ledger["configuration"] = configuration(settings)
    ledger["records"] = (
        ledger.get("records") if isinstance(ledger.get("records"), list) else []
    )
    ledger["parity_records"] = (
        ledger.get("parity_records")
        if isinstance(ledger.get("parity_records"), list)
        else []
    )
    for key in (
        "last_capture_ids",
        "last_fill_ids",
        "last_settled_ids",
    ):
        ledger.setdefault(key, [])
    ledger.setdefault("last_capture_funnel", {})
    for row in ledger["records"]:
        if not isinstance(row, dict):
            continue
        row["affects_execution"] = False
        row["automatic_promotion"] = False
        row.setdefault("queue_position_modeled", False)
    return ledger


def _book_quality(candidate, config):
    reasons = []
    if candidate.get("market_lane") != "crypto_15m" or not candidate.get(
        "is_15m_market"
    ):
        reasons.append("unsupported_market_lane")
    if str(candidate.get("asset") or "").upper() not in config["assets"]:
        reasons.append("unsupported_asset")
    minutes = _number(candidate.get("minutes_to_close"), -1.0)
    if not config["minimum_minutes"] <= minutes <= config["maximum_minutes"]:
        reasons.append("outside_registered_time_window")
    book = candidate.get("kalshi_microstructure") or {}
    if not book.get("sequence_valid"):
        reasons.append("sequence_invalid")
    if not book.get("fresh"):
        reasons.append("book_stale")
    if not book.get("book_consistent"):
        reasons.append("book_inconsistent")
    if _number(book.get("age_seconds"), 999.0) > config[
        "maximum_quote_age_seconds"
    ]:
        reasons.append("quote_too_old")
    if not (candidate.get("fee_schedule") or {}).get("authoritative"):
        reasons.append("fee_schedule_not_exact")
    return {"ok": not reasons, "reasons": reasons}


def _maker_leg(candidate, side, price):
    fee = kalshi_order_fee(
        price,
        1,
        schedule=candidate.get("fee_schedule") or {},
        liquidity_role="maker",
    )
    return {
        "side": side,
        "limit_price_cents": round(price, 6),
        "contracts": 1,
        "fee_dollars": round(_number(fee.get("fee_dollars")), 6),
        "fee_schedule": fee,
        "status": "working",
        "filled_at": None,
        "fill_evidence": None,
        "virtual_profit": None,
    }


def _update_working_fills(ledger, candidates, now):
    by_ticker = {
        str(candidate.get("ticker") or ""): candidate
        for candidate in candidates or []
        if candidate.get("ticker")
    }
    updated = []
    for row in ledger["records"]:
        if row.get("status") != "working":
            continue
        candidate = by_ticker.get(str(row.get("ticker") or ""))
        if not candidate:
            continue
        book = candidate.get("kalshi_microstructure") or {}
        trade_ts = _number(book.get("last_trade_ts"))
        prior_ts = _number(row.get("last_observed_trade_ts"))
        trade_yes = book.get("last_trade_price_cents")
        if trade_yes is None or trade_ts <= prior_ts:
            continue
        trade_yes = _number(trade_yes)
        newly_filled = False
        for side, trade_side_price in (("yes", trade_yes), ("no", 100.0 - trade_yes)):
            leg = row.get(side) or {}
            if leg.get("status") != "working":
                continue
            if trade_side_price < _number(leg.get("limit_price_cents")) - 1e-9:
                leg.update(
                    {
                        "status": "filled",
                        "filled_at": now,
                        "fill_evidence": "public_trade_strictly_through_limit",
                        "fill_trade_yes_price_cents": round(trade_yes, 6),
                        "fill_trade_side_price_cents": round(trade_side_price, 6),
                    }
                )
                newly_filled = True
        row["last_observed_trade_ts"] = trade_ts
        if newly_filled:
            row["fill_count"] = sum(
                (row.get(side) or {}).get("status") == "filled"
                for side in ("yes", "no")
            )
            updated.append(row)
    ledger["last_fill_ids"] = [row["id"] for row in updated]
    return updated


def capture_candidates(ledger, candidates, settings=None, now=None):
    ledger = normalize_ledger(ledger, settings, now)
    config = ledger["configuration"]
    captured_at = _iso(now)
    fill_updates = _update_working_fills(ledger, candidates, captured_at)
    known = {str(row.get("id") or "") for row in ledger["records"]}
    parity_known = {
        str(row.get("id") or "") for row in ledger["parity_records"]
    }
    added = []
    funnel = Counter(reviewed=0, quality_eligible=0, captured=0)
    for candidate in candidates or []:
        funnel["reviewed"] += 1
        quality = _book_quality(candidate, config)
        if not quality["ok"]:
            for reason in quality["reasons"]:
                funnel[f"reason:{reason}"] += 1
            continue
        funnel["quality_eligible"] += 1
        ticker = str(candidate.get("ticker") or "")
        book = candidate.get("kalshi_microstructure") or {}
        yes_bid = _number(book.get("best_yes_bid_cents"), -1.0)
        no_bid = _number(book.get("best_no_bid_cents"), -1.0)
        yes_ask = _number(book.get("best_yes_entry_price_cents"), -1.0)
        no_ask = _number(book.get("best_no_entry_price_cents"), -1.0)
        if min(yes_bid, no_bid, yes_ask, no_ask) <= 0:
            funnel["reason:incomplete_two_sided_book"] += 1
            continue
        yes_taker = kalshi_order_fee(
            yes_ask,
            1,
            schedule=candidate.get("fee_schedule") or {},
            liquidity_role="taker",
        )
        no_taker = kalshi_order_fee(
            no_ask,
            1,
            schedule=candidate.get("fee_schedule") or {},
            liquidity_role="taker",
        )
        parity_id = f"{config['policy_hash']}:parity:{ticker}"
        if parity_id not in parity_known:
            taker_cost_cents = (
                yes_ask
                + no_ask
                + 100.0
                * (
                    _number(yes_taker.get("fee_dollars"))
                    + _number(no_taker.get("fee_dollars"))
                )
            )
            ledger["parity_records"].append(
                {
                    "id": parity_id,
                    "captured_at": captured_at,
                    "ticker": ticker,
                    "asset": candidate.get("asset"),
                    "yes_ask_cents": yes_ask,
                    "no_ask_cents": no_ask,
                    "two_leg_cost_cents": round(taker_cost_cents, 6),
                    "locked_profit_cents": round(100.0 - taker_cost_cents, 6),
                    "book_consistent": True,
                    "exact_fees": bool(
                        yes_taker.get("exact") and no_taker.get("exact")
                    ),
                    "executable_locked_profit": bool(
                        yes_taker.get("exact")
                        and no_taker.get("exact")
                        and taker_cost_cents < 100.0
                    ),
                }
            )
            parity_known.add(parity_id)
        record_id = f"{config['policy_hash']}:dual-maker:{ticker}"
        if record_id in known:
            funnel["already_tracked"] += 1
            continue
        yes_leg = _maker_leg(candidate, "yes", yes_bid)
        no_leg = _maker_leg(candidate, "no", no_bid)
        if not (yes_leg["fee_schedule"].get("exact") and no_leg["fee_schedule"].get("exact")):
            funnel["reason:maker_fee_not_exact"] += 1
            continue
        both_cost = (
            yes_bid / 100.0
            + no_bid / 100.0
            + _number(yes_leg.get("fee_dollars"))
            + _number(no_leg.get("fee_dollars"))
        )
        locked_profit = 1.0 - both_cost
        if locked_profit * 100.0 < config["minimum_locked_profit_cents"]:
            funnel["reason:maker_locked_profit_below_floor"] += 1
            continue
        row = {
            "id": record_id,
            "version": VERSION,
            "policy_hash": config["policy_hash"],
            "mode": MODE,
            "affects_execution": False,
            "automatic_promotion": False,
            "historical_backfill": False,
            "status": "working",
            "captured_at": captured_at,
            "ticker": ticker,
            "event_ticker": candidate.get("event_ticker"),
            "series_ticker": candidate.get("series_ticker"),
            "asset": str(candidate.get("asset") or "").upper(),
            "close_time": candidate.get("close_time"),
            "expiry_key": candidate.get("close_time"),
            "minutes_to_close": candidate.get("minutes_to_close"),
            "fill_model": config["fill_model"],
            "queue_position_modeled": False,
            "yes": yes_leg,
            "no": no_leg,
            "both_fill_cost_dollars": round(both_cost, 6),
            "both_fill_locked_profit_dollars": round(locked_profit, 6),
            "last_observed_trade_ts": _number(book.get("last_trade_ts")),
            "fill_count": 0,
            "market_result": None,
            "virtual_profit": None,
        }
        ledger["records"].append(row)
        known.add(record_id)
        added.append(row)
        funnel["captured"] += 1
    if len(ledger["records"]) > config["maximum_records"]:
        open_rows = [row for row in ledger["records"] if row.get("status") == "working"]
        closed = [row for row in ledger["records"] if row.get("status") != "working"]
        keep = max(0, config["maximum_records"] - len(open_rows))
        ledger["records"] = (closed[-keep:] if keep else []) + open_rows
    if len(ledger["parity_records"]) > config["maximum_records"]:
        ledger["parity_records"] = ledger["parity_records"][-config["maximum_records"] :]
    ledger["last_capture_ids"] = [row["id"] for row in added]
    ledger["last_capture_funnel"] = dict(funnel)
    ledger["updated_at"] = captured_at
    return added, fill_updates


def _market_result(payload):
    market = (payload or {}).get("market") if isinstance(payload, dict) else None
    market = market if isinstance(market, dict) else (payload or {})
    status = str(market.get("status") or "").lower()
    result = str(market.get("result") or "").lower()
    return (
        status == "finalized" and result in {"yes", "no"},
        result,
        market.get("settlement_ts") or market.get("settled_time"),
    )


def settle_records(ledger, fetch_market, settings=None, now=None):
    ledger = normalize_ledger(ledger, settings, now)
    config = ledger["configuration"]
    current = _now(now)
    cache = {}
    checks = 0
    settled = []
    for row in ledger["records"]:
        if row.get("status") != "working":
            continue
        if checks >= config["maximum_settlement_checks_per_scan"]:
            break
        try:
            close = _now(row.get("close_time"))
        except (TypeError, ValueError):
            continue
        if current < close + timedelta(minutes=config["settlement_grace_minutes"]):
            continue
        last_check = row.get("last_settlement_check_at")
        if last_check:
            try:
                if current < _now(last_check) + timedelta(
                    minutes=config["settlement_retry_minutes"]
                ):
                    continue
            except (TypeError, ValueError):
                pass
        ticker = str(row.get("ticker") or "")
        if ticker not in cache:
            try:
                cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                cache[ticker] = {"status": "error", "error": type(exc).__name__}
            checks += 1
        finalized, result, settled_at = _market_result(cache[ticker])
        row["last_settlement_check_at"] = current.isoformat()
        row["settlement_checks"] = _integer(row.get("settlement_checks")) + 1
        if not finalized:
            continue
        cost = 0.0
        payout = 0.0
        for side in ("yes", "no"):
            leg = row.get(side) or {}
            if leg.get("status") == "filled":
                cost += _number(leg.get("limit_price_cents")) / 100.0
                cost += _number(leg.get("fee_dollars"))
                won = side == result
                payout += 1.0 if won else 0.0
                leg["result"] = "WIN" if won else "LOSS"
                leg["virtual_profit"] = round(
                    (1.0 if won else 0.0)
                    - _number(leg.get("limit_price_cents")) / 100.0
                    - _number(leg.get("fee_dollars")),
                    6,
                )
            else:
                leg["status"] = "unfilled"
                leg["result"] = "NO_FILL"
                leg["virtual_profit"] = 0.0
        fill_count = sum(
            (row.get(side) or {}).get("status") == "filled"
            for side in ("yes", "no")
        )
        row.update(
            {
                "status": "settled",
                "settled_at": settled_at or current.isoformat(),
                "market_result": result,
                "fill_count": fill_count,
                "fill_outcome": {0: "no_fill", 1: "single_leg", 2: "both_legs"}[
                    fill_count
                ],
                "actual_filled_cost_dollars": round(cost, 6),
                "virtual_profit": round(payout - cost, 6),
            }
        )
        settled.append(row)
    ledger["last_settled_ids"] = [row["id"] for row in settled]
    if settled:
        ledger["updated_at"] = current.isoformat()
    return settled


def _cluster_lower(rows):
    clusters = {}
    for row in rows:
        day = _observation_date(row.get("captured_at"))
        if day:
            clusters[day] = clusters.get(day, 0.0) + _number(
                row.get("virtual_profit")
            )
    values = list(clusters.values())
    if len(values) < 2:
        return {"lower_bound": None, "clusters": len(values), "confidence_level": 0.95}
    mean = statistics.fmean(values)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    return {
        "mean_day_profit": round(mean, 6),
        "standard_error": round(standard_error, 6),
        "lower_bound": round(mean - ONE_SIDED_95_Z * standard_error, 6),
        "clusters": len(values),
        "confidence_level": 0.95,
    }


def summarize(ledger, settings=None):
    ledger = normalize_ledger(ledger, settings)
    config = ledger["configuration"]
    records = [
        row
        for row in ledger["records"]
        if row.get("policy_hash") == config["policy_hash"]
    ]
    settled = sorted(
        [row for row in records if row.get("status") == "settled"],
        key=lambda row: str(row.get("captured_at") or ""),
    )
    cost = sum(_number(row.get("actual_filled_cost_dollars")) for row in settled)
    profit = sum(_number(row.get("virtual_profit")) for row in settled)
    split = len(settled) // 2
    first_profit = sum(_number(row.get("virtual_profit")) for row in settled[:split])
    second_profit = sum(_number(row.get("virtual_profit")) for row in settled[split:])
    days = {
        _observation_date(row.get("captured_at"))
        for row in settled
        if _observation_date(row.get("captured_at"))
    }
    lower = _cluster_lower(settled)
    unique_markets = len({str(row.get("ticker") or "") for row in settled})
    evidence = {
        "profitable_after_all_single_leg_outcomes": profit > 0,
        "both_chronological_halves_profitable": bool(
            settled[:split]
            and settled[split:]
            and first_profit > 0
            and second_profit > 0
        ),
        "day_cluster_lower_positive": lower["lower_bound"] is not None
        and lower["lower_bound"] > 0,
    }
    interim_gates = {
        "minimum_unique_markets": unique_markets
        >= config["minimum_unique_markets_interim"],
        "minimum_observation_days": len(days)
        >= config["minimum_observation_days_interim"],
        **evidence,
    }
    final_gates = {
        "minimum_unique_markets": unique_markets
        >= config["minimum_unique_markets_final"],
        "minimum_observation_days": len(days)
        >= config["minimum_observation_days_final"],
        **evidence,
    }
    parity = [
        row
        for row in ledger["parity_records"]
        if str(row.get("id") or "").startswith(config["policy_hash"])
    ]
    minimum_parity_cost = min(
        (_number(row.get("two_leg_cost_cents"), 999.0) for row in parity),
        default=None,
    )
    return {
        "version": VERSION,
        "mode": MODE if config["enabled"] else "disabled",
        "affects_execution": False,
        "automatic_promotion": False,
        "historical_backfill": False,
        "registered_at": ledger.get("registered_at"),
        "updated_at": ledger.get("updated_at"),
        "configuration": config,
        "tracked": len(records),
        "working": sum(row.get("status") == "working" for row in records),
        "settled": len(settled),
        "unique_markets": unique_markets,
        "observation_days": len(days),
        "both_legs_filled": sum(row.get("fill_outcome") == "both_legs" for row in settled),
        "single_leg_filled": sum(row.get("fill_outcome") == "single_leg" for row in settled),
        "no_fill": sum(row.get("fill_outcome") == "no_fill" for row in settled),
        "filled_cost": round(cost, 4),
        "profit": round(profit, 4),
        "roi": round(100.0 * profit / cost, 2) if cost else 0.0,
        "chronological_halves": {
            "first_profit": round(first_profit, 4),
            "second_profit": round(second_profit, 4),
        },
        "day_cluster_inference": lower,
        "interim_review": {
            "eligible": bool(interim_gates) and all(interim_gates.values()),
            "gates": interim_gates,
            "automatic_promotion": False,
        },
        "final_review": {
            "eligible": bool(final_gates) and all(final_gates.values()),
            "gates": final_gates,
            "automatic_promotion": False,
        },
        "taker_parity_diagnostic": {
            "observations": len(parity),
            "minimum_two_leg_cost_cents": round(minimum_parity_cost, 6)
            if minimum_parity_cost is not None
            else None,
            "executable_locked_profit_observations": sum(
                bool(row.get("executable_locked_profit")) for row in parity
            ),
            "integrity_only": True,
        },
        "last_capture_funnel": dict(ledger.get("last_capture_funnel") or {}),
        "recent_records": sorted(
            records,
            key=lambda row: str(row.get("settled_at") or row.get("captured_at") or ""),
            reverse=True,
        )[:100],
    }

