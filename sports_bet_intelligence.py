"""Shadow decision intelligence for every scanned sports candidate.

This module deliberately explains and records new signals before they are
allowed to change execution.  The existing calibrated pricing and unit engine
remain authoritative while these features accumulate settled outcomes and CLV.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median

from kalshi_common import normalize_text


INTELLIGENCE_VERSION = "sports-bet-intelligence-v1"
STATE_VERSION = "sports-intelligence-history-v1"


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _probability(value):
    number = _number(value)
    return max(0.01, min(99.99, number)) if number is not None else None


def _parse_time(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).strip().replace(".", "", 1).isdigit():
        try:
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value, now):
    parsed = _parse_time(value)
    return max(0.0, (now - parsed).total_seconds()) if parsed else None


def _mean(values):
    rows = [float(value) for value in values if _number(value) is not None]
    return sum(rows) / len(rows) if rows else None


def _stdev(values):
    rows = [float(value) for value in values if _number(value) is not None]
    if len(rows) < 2:
        return 0.0 if rows else None
    center = sum(rows) / len(rows)
    return math.sqrt(sum((value - center) ** 2 for value in rows) / len(rows))


def _candidate_key(candidate):
    ticker = str(candidate.get("kalshi_ticker") or "").strip()
    side = "no" if normalize_text(candidate.get("order_side")) == "no" else "yes"
    return f"{ticker}|{side}" if ticker else ""


def _candidate_probability(candidate):
    pricing = candidate.get("pricing_v2") or {}
    distribution = pricing.get("probability_distribution") or {}
    for value in (
        distribution.get("median_probability"),
        pricing.get("fair_probability"),
        candidate.get("model_prob"),
    ):
        if _probability(value) is not None:
            return _probability(value)
    return None


def _side_levels(snapshot, side):
    side = "no" if normalize_text(side) == "no" else "yes"
    asks = list(snapshot.get(f"{side}_ask_levels") or [])
    bids = list(snapshot.get(f"{side}_bid_levels") or [])
    ask = _number(snapshot.get(f"{side}_ask"))
    bid = _number(snapshot.get(f"{side}_bid"))
    ask_size = _number(
        snapshot.get("yes_ask_size") if side == "yes" else snapshot.get("yes_bid_size")
    )
    bid_size = _number(
        snapshot.get("yes_bid_size") if side == "yes" else snapshot.get("yes_ask_size")
    )
    if not asks and ask is not None:
        asks = [{"price": ask, "size": ask_size}]
    if not bids and bid is not None:
        bids = [{"price": bid, "size": bid_size}]
    return bids, asks


def _depth_within(levels, cents, *, ask):
    usable = [row for row in levels if _number(row.get("price")) is not None]
    if not usable:
        return None
    top = min(_number(row.get("price")) for row in usable) if ask else max(
        _number(row.get("price")) for row in usable
    )
    total = 0.0
    known = False
    for row in usable:
        price = _number(row.get("price"))
        size = _number(row.get("size"))
        if size is None:
            continue
        distance = price - top if ask else top - price
        if distance <= cents + 1e-9:
            total += max(0.0, size)
            known = True
    return round(total, 3) if known else None


def _walk_asks(levels, contracts):
    remaining = max(0.0, float(_number(contracts, 0.0)))
    if remaining <= 0:
        return {"contracts": 0.0, "filled_contracts": 0.0, "fill_ratio": 1.0}
    cost = 0.0
    filled = 0.0
    for row in sorted(levels, key=lambda item: float(_number(item.get("price"), 999))):
        price = _number(row.get("price"))
        size = _number(row.get("size"))
        if price is None or size is None or size <= 0:
            continue
        take = min(remaining, size)
        filled += take
        cost += take * price
        remaining -= take
        if remaining <= 1e-9:
            break
    top = min((_number(row.get("price")) for row in levels if _number(row.get("price")) is not None), default=None)
    vwap = cost / filled if filled > 0 else None
    return {
        "contracts": round(float(contracts), 3),
        "filled_contracts": round(filled, 3),
        "fill_ratio": round(filled / float(contracts), 4) if contracts > 0 else 1.0,
        "vwap_cents": round(vwap, 3) if vwap is not None else None,
        "slippage_cents": round(vwap - top, 3) if vwap is not None and top is not None else None,
    }


def market_microstructure(candidate):
    snapshot = candidate.get("stream_snapshot") or {}
    side = candidate.get("order_side") or "yes"
    bids, asks = _side_levels(snapshot, side)
    bid = _number(candidate.get("entry_bid"))
    ask = _number(candidate.get("entry_price"))
    spread = ask - bid if ask is not None and bid is not None else None
    bid_depth_2c = _depth_within(bids, 2.0, ask=False)
    ask_depth_2c = _depth_within(asks, 2.0, ask=True)
    denominator = (bid_depth_2c or 0.0) + (ask_depth_2c or 0.0)
    imbalance = (
        ((bid_depth_2c or 0.0) - (ask_depth_2c or 0.0)) / denominator
        if denominator > 0 else None
    )
    quote_age = _number(snapshot.get("age_seconds"))
    quality = 100.0
    quality -= min(40.0, max(0.0, float(spread or 10.0) - 1.0) * 8.0)
    if snapshot.get("orderbook_valid") is False:
        quality -= 35.0
    if not snapshot.get("fresh"):
        quality -= 25.0
    if quote_age is not None:
        quality -= min(15.0, max(0.0, quote_age - 3.0) * 1.5)
    if ask_depth_2c is not None and ask_depth_2c < 10:
        quality -= 12.0
    quality = max(0.0, min(100.0, quality))
    return {
        "source": snapshot.get("source") or "kalshi_rest_or_unavailable",
        "order_side": "no" if normalize_text(side) == "no" else "yes",
        "quote_fresh": bool(snapshot.get("fresh")),
        "quote_age_seconds": quote_age,
        "exchange_latency_ms": snapshot.get("exchange_latency_ms"),
        "bid_cents": bid,
        "ask_cents": ask,
        "spread_cents": round(spread, 3) if spread is not None else None,
        "orderbook_valid": snapshot.get("orderbook_valid"),
        "bid_depth_1c": _depth_within(bids, 1.0, ask=False),
        "bid_depth_2c": bid_depth_2c,
        "bid_depth_5c": _depth_within(bids, 5.0, ask=False),
        "ask_depth_1c": _depth_within(asks, 1.0, ask=True),
        "ask_depth_2c": ask_depth_2c,
        "ask_depth_5c": _depth_within(asks, 5.0, ask=True),
        "depth_imbalance": round(imbalance, 4) if imbalance is not None else None,
        "price_velocity_30s_pp": snapshot.get("mid_velocity_30s_cents"),
        "price_velocity_2m_pp": snapshot.get("mid_velocity_120s_cents"),
        "price_velocity_5m_pp": snapshot.get("mid_velocity_300s_cents"),
        "quote_changes_60s": snapshot.get("quote_changes_60s"),
        "recent_trade_count_60s": snapshot.get("recent_trade_count_60s"),
        "recent_trade_contracts_60s": snapshot.get("recent_trade_contracts_60s"),
        "recent_yes_taker_contracts_60s": snapshot.get("recent_yes_taker_contracts_60s"),
        "recent_no_taker_contracts_60s": snapshot.get("recent_no_taker_contracts_60s"),
        "last_trade_price": snapshot.get("last_trade_price"),
        "last_trade_size": snapshot.get("last_trade_size") or snapshot.get("last_trade_count"),
        "last_trade_age_seconds": snapshot.get("last_trade_age_seconds"),
        "volume": snapshot.get("volume", candidate.get("kalshi_volume")),
        "open_interest": snapshot.get("open_interest"),
        "dollar_volume": snapshot.get("dollar_volume"),
        "dollar_open_interest": snapshot.get("dollar_open_interest"),
        "fill_scenarios": {
            str(contracts): _walk_asks(asks, contracts)
            for contracts in (1, 10, 25, 50)
        },
        "quality_score": round(quality, 1),
        "mode": "shadow_only",
        "affects_execution": False,
    }


def source_synchronization(candidate, now):
    pricing = candidate.get("pricing_v2") or {}
    observations = (pricing.get("consensus") or {}).get("observations") or []
    source_times = []
    for row in observations:
        parsed = _parse_time(row.get("last_update"))
        if parsed:
            source_times.append((f"book:{row.get('family')}", parsed))
    snapshot = candidate.get("stream_snapshot") or {}
    for label, value in (
        ("kalshi_quote", snapshot.get("quote_received_at")),
        ("kalshi_exchange", snapshot.get("exchange_ts_ms")),
        ("odds_fetch", candidate.get("odds_fetched_at")),
        ("score", candidate.get("score_last_update")),
    ):
        parsed = _parse_time(value)
        if parsed:
            source_times.append((label, parsed))
    timestamps = [value for _label, value in source_times]
    skew = (max(timestamps) - min(timestamps)).total_seconds() if len(timestamps) >= 2 else None
    book_ages = [_age_seconds(row.get("last_update"), now) for row in observations]
    book_ages = [value for value in book_ages if value is not None]
    quote_age = _age_seconds(snapshot.get("quote_received_at"), now)
    score_age = _age_seconds(candidate.get("score_last_update"), now)
    grade = 100.0
    if skew is None:
        grade -= 20.0
    else:
        skew_tolerance = 30.0 if candidate.get("game_started") else 180.0
        skew_scale = 6.0 if candidate.get("game_started") else 15.0
        grade -= min(35.0, max(0.0, skew - skew_tolerance) / skew_scale)
    if quote_age is None:
        grade -= 15.0
    elif quote_age > 8.0:
        grade -= min(25.0, (quote_age - 8.0) * 2.0)
    if candidate.get("game_started") and score_age is None:
        grade -= 20.0
    if book_ages and max(book_ages) > 360.0:
        grade -= 15.0
    grade = max(0.0, min(100.0, grade))
    status = "synchronized" if grade >= 85 else "usable" if grade >= 65 else "degraded" if grade >= 40 else "poor"
    return {
        "status": status,
        "score": round(grade, 1),
        "source_count": len(source_times),
        "book_timestamp_count": len(book_ages),
        "source_skew_seconds": round(skew, 3) if skew is not None else None,
        "freshest_source": max(source_times, key=lambda row: row[1])[0] if source_times else None,
        "oldest_source": min(source_times, key=lambda row: row[1])[0] if source_times else None,
        "maximum_book_age_seconds": round(max(book_ages), 3) if book_ages else None,
        "median_book_age_seconds": round(median(book_ages), 3) if book_ages else None,
        "kalshi_quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
        "score_age_seconds": round(score_age, 3) if score_age is not None else None,
        "live_score_required": bool(candidate.get("game_started")),
        "mode": "shadow_only",
        "affects_execution": False,
    }


def sportsbook_market_intelligence(candidate, previous, sharp_books):
    consensus = ((candidate.get("pricing_v2") or {}).get("consensus") or {})
    observations = consensus.get("observations") or []
    sharp = {normalize_text(value) for value in sharp_books or []}
    current = {}
    sharp_probabilities = []
    retail_probabilities = []
    for row in observations:
        family = str(row.get("family") or "")
        probability = _probability(row.get("probability"))
        if not family or probability is None:
            continue
        current[family] = probability
        books = {normalize_text(value) for value in row.get("books") or []}
        (sharp_probabilities if books & sharp else retail_probabilities).append(probability)
    prior_families = (previous or {}).get("families") or {}
    movements = []
    for family, probability in current.items():
        prior = _number(prior_families.get(family))
        if prior is None:
            continue
        movements.append({
            "family": family,
            "move_pp": round(probability - prior, 3),
            "probability": round(probability, 3),
        })
    meaningful = [row for row in movements if abs(row["move_pp"]) >= 0.25]
    directions = Counter("up" if row["move_pp"] > 0 else "down" for row in meaningful)
    steam = "up" if directions["up"] >= 2 and directions["up"] > directions["down"] else "down" if directions["down"] >= 2 and directions["down"] > directions["up"] else "mixed_or_flat"
    sharp_mean = _mean(sharp_probabilities)
    retail_mean = _mean(retail_probabilities)
    consensus_probability = _number(consensus.get("probability"))
    previous_consensus = _number((previous or {}).get("consensus_probability"))
    consensus_move = (
        consensus_probability - previous_consensus
        if consensus_probability is not None and previous_consensus is not None else None
    )
    sharp_edge = sharp_mean - retail_mean if sharp_mean is not None and retail_mean is not None else None
    signal = (
        "sharp_higher" if sharp_edge is not None and sharp_edge >= 1.0
        else "sharp_lower" if sharp_edge is not None and sharp_edge <= -1.0
        else "sharp_retail_aligned" if sharp_edge is not None
        else "insufficient_sharp_retail_split"
    )
    return {
        "family_count": len(current),
        "sharp_probability": round(sharp_mean, 3) if sharp_mean is not None else None,
        "retail_probability": round(retail_mean, 3) if retail_mean is not None else None,
        "sharp_retail_difference_pp": round(sharp_edge, 3) if sharp_edge is not None else None,
        "sharp_signal": signal,
        "consensus_move_since_prior_scan_pp": round(consensus_move, 3) if consensus_move is not None else None,
        "steam_direction": steam,
        "moving_family_count": len(meaningful),
        "up_family_count": directions["up"],
        "down_family_count": directions["down"],
        "largest_moves": sorted(movements, key=lambda row: abs(row["move_pp"]), reverse=True)[:5],
        "family_probability_range_pp": consensus.get("family_probability_range_pp"),
        "outlier_family_count": consensus.get("outlier_book_family_count"),
        "average_age_minutes": consensus.get("average_age_minutes"),
        "line_ladder_interpolated_family_count": consensus.get("line_ladder_interpolated_family_count"),
        "mode": "shadow_only",
        "affects_execution": False,
    }


def model_disagreement(candidate):
    challengers = dict(candidate.get("outcome_probability_challengers") or {})
    provider = candidate.get("provider_fair_anchor") or {}
    if provider.get("ok") and provider.get("fair_probability") is not None:
        challengers["sports_game_odds_fair_anchor"] = provider.get("fair_probability")
    values = {key: _probability(value) for key, value in challengers.items()}
    values = {key: value for key, value in values.items() if value is not None}
    ordered = sorted(values.items(), key=lambda row: row[1])
    probability_range = ordered[-1][1] - ordered[0][1] if len(ordered) >= 2 else None
    deviation = _stdev(values.values())
    level = (
        "insufficient_sources" if len(values) < 2
        else "aligned" if probability_range <= 4
        else "mild" if probability_range <= 8
        else "material" if probability_range <= 15
        else "severe"
    )
    all_in = _number(((candidate.get("pricing_v2") or {}).get("probability_distribution") or {}).get("all_in_price_cents"))
    source_edges = {
        key: round(value - all_in, 3) if all_in is not None else None
        for key, value in values.items()
    }
    positive = sum(1 for value in source_edges.values() if value is not None and value > 0)
    return {
        "classification": level,
        "source_count": len(values),
        "probabilities": {key: round(value, 3) for key, value in values.items()},
        "range_pp": round(probability_range, 3) if probability_range is not None else None,
        "standard_deviation_pp": round(deviation, 3) if deviation is not None else None,
        "lowest_source": ordered[0][0] if ordered else None,
        "highest_source": ordered[-1][0] if ordered else None,
        "source_net_edges_pp": source_edges,
        "positive_edge_sources": positive,
        "negative_or_flat_sources": max(0, len(values) - positive),
        "mode": "shadow_only",
        "affects_execution": False,
    }


def _price_quality(candidate):
    pricing = candidate.get("pricing_v2") or {}
    distribution = pricing.get("probability_distribution") or {}
    entry = _number(candidate.get("entry_price"))
    fee = _number(candidate.get("estimated_fee_edge_pp"), 0.0)
    slippage = _number(distribution.get("estimated_slippage_cents"), 0.0)
    robust = _number(distribution.get("robust_probability"))
    all_in = _number(distribution.get("all_in_price_cents"))
    if all_in is None and entry is not None:
        all_in = entry + fee + slippage
    positive_ev_ceiling = robust - fee - slippage if robust is not None else None
    execution_probability = _number(pricing.get("fair_probability"), _number(candidate.get("model_prob")))
    return {
        "entry_bid_cents": candidate.get("entry_bid"),
        "entry_ask_cents": entry,
        "all_in_price_cents": round(all_in, 3) if all_in is not None else None,
        "fee_cents_per_contract": round(fee, 3),
        "assumed_slippage_cents": round(slippage, 3),
        "execution_fair_probability": execution_probability,
        "robust_probability": robust,
        "positive_ev_price_ceiling_cents": round(positive_ev_ceiling, 3) if positive_ev_ceiling is not None else None,
        "price_room_to_positive_ev_ceiling_cents": round(positive_ev_ceiling - entry, 3) if positive_ev_ceiling is not None and entry is not None else None,
        "nominal_net_edge_pp": pricing.get("net_edge_pp", candidate.get("edge")),
        "conservative_net_edge_pp": pricing.get("net_conservative_edge_pp"),
        "posterior_p_edge_positive": distribution.get("posterior_p_edge_positive"),
    }


def _cross_market_reviews(candidates):
    grouped = defaultdict(list)
    for candidate in candidates:
        grouped[str(candidate.get("game_key") or candidate.get("event_id") or "")].append(candidate)
    output = {}
    for rows in grouped.values():
        for candidate in rows:
            checks = []
            probability = _candidate_probability(candidate)
            market_type = normalize_text(candidate.get("market_type"))
            selected = normalize_text(candidate.get("selected_team"))
            line = _number(candidate.get("market_line"))
            for sibling in rows:
                if sibling is candidate:
                    continue
                sibling_probability = _candidate_probability(sibling)
                if probability is None or sibling_probability is None:
                    continue
                sibling_type = normalize_text(sibling.get("market_type"))
                sibling_selected = normalize_text(sibling.get("selected_team"))
                sibling_line = _number(sibling.get("market_line"))
                violation = False
                rule = ""
                gap = None
                if market_type == "spread" and sibling_type == "moneyline" and selected == sibling_selected and line is not None:
                    rule = "spread_vs_moneyline_order"
                    violation = probability + 3.0 < sibling_probability if line > 0 else probability > sibling_probability + 3.0 if line < 0 else False
                    gap = probability - sibling_probability
                elif market_type == sibling_type == "spread" and selected == sibling_selected and line is not None and sibling_line is not None and line != sibling_line:
                    rule = "spread_ladder_monotonicity"
                    violation = (line > sibling_line and probability + 2.0 < sibling_probability) or (line < sibling_line and probability > sibling_probability + 2.0)
                    gap = probability - sibling_probability
                elif market_type == sibling_type == "total" and normalize_text(candidate.get("total_side")) == normalize_text(sibling.get("total_side")) and line is not None and sibling_line is not None and line != sibling_line:
                    side = normalize_text(candidate.get("total_side"))
                    rule = "total_ladder_monotonicity"
                    expected_increasing = side == "under"
                    violation = (line > sibling_line and ((probability + 2.0 < sibling_probability) if expected_increasing else (probability > sibling_probability + 2.0))) or (line < sibling_line and ((probability > sibling_probability + 2.0) if expected_increasing else (probability + 2.0 < sibling_probability)))
                    gap = probability - sibling_probability
                if rule:
                    checks.append({
                        "rule": rule,
                        "sibling_ticker": sibling.get("kalshi_ticker"),
                        "sibling_market_type": sibling.get("market_type"),
                        "probability_gap_pp": round(gap, 3),
                        "violation": bool(violation),
                    })
            violations = [row for row in checks if row["violation"]]
            output[id(candidate)] = {
                "status": "coherent" if checks and not violations else "contradiction_detected" if violations else "insufficient_sibling_markets",
                "checks": len(checks),
                "violations": len(violations),
                "consistency_score": max(0.0, round(100.0 - 20.0 * len(violations), 1)) if checks else None,
                "details": checks[:12],
                "mode": "shadow_only",
                "affects_execution": False,
            }
    return output


def _pair_correlation(left, right):
    if str(left.get("game_key") or left.get("event_id")) != str(right.get("game_key") or right.get("event_id")):
        return 0.0
    left_type = normalize_text(left.get("market_type"))
    right_type = normalize_text(right.get("market_type"))
    left_selected = normalize_text(left.get("selected_team"))
    right_selected = normalize_text(right.get("selected_team"))
    if left_type == right_type == "moneyline":
        return 0.95 if left_selected == right_selected else -0.90
    if left_type == right_type == "total":
        return 0.90 if normalize_text(left.get("total_side")) == normalize_text(right.get("total_side")) else -0.88
    if left_type == right_type == "spread":
        return 0.90 if left_selected == right_selected else -0.86
    if {left_type, right_type} == {"moneyline", "spread"}:
        return 0.82 if left_selected == right_selected else -0.78
    if "total" in {left_type, right_type} and ({left_type, right_type} & {"moneyline", "spread"}):
        return 0.15
    return 0.05


def _normal_cdf(value):
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _joint_pair(left, right, draws=512):
    left_probability = _candidate_probability(left)
    right_probability = _candidate_probability(right)
    if left_probability is None or right_probability is None:
        return None
    rho = max(-0.97, min(0.97, _pair_correlation(left, right)))
    seed_text = "|".join(sorted((_candidate_key(left), _candidate_key(right))))
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    wins = losses = split = 0
    for _ in range(max(128, int(draws))):
        z1 = rng.gauss(0.0, 1.0)
        z2 = rho * z1 + math.sqrt(max(1e-9, 1.0 - rho * rho)) * rng.gauss(0.0, 1.0)
        hit_left = _normal_cdf(z1) <= left_probability / 100.0
        hit_right = _normal_cdf(z2) <= right_probability / 100.0
        if hit_left and hit_right:
            wins += 1
        elif not hit_left and not hit_right:
            losses += 1
        else:
            split += 1
    total = wins + losses + split
    return {
        "sibling_ticker": right.get("kalshi_ticker"),
        "assumed_correlation": round(rho, 3),
        "joint_win_probability": round(wins / total, 4),
        "joint_loss_probability": round(losses / total, 4),
        "split_probability": round(split / total, 4),
        "draws": total,
    }


def _joint_scenarios(candidate, siblings):
    pairs = [
        review for sibling in siblings
        if sibling is not candidate and (review := _joint_pair(candidate, sibling)) is not None
    ]
    pairs.sort(key=lambda row: (row["assumed_correlation"], row["joint_loss_probability"]), reverse=True)
    return {
        "pair_count": len(pairs),
        "highest_positive_correlation": max((row["assumed_correlation"] for row in pairs), default=None),
        "highest_joint_loss_probability": max((row["joint_loss_probability"] for row in pairs), default=None),
        "pairs": pairs[:8],
        "method": "deterministic_gaussian_copula_shadow",
        "mode": "shadow_only",
        "affects_execution": False,
    }


def _row_units(row):
    units = _number(
        (row.get("sports_units") or {}).get("placed_units")
        or (row.get("sports_units") or {}).get("additional_units")
        or row.get("unit_count")
    )
    if units is not None:
        return max(0.0, units)
    unit_size = _number(row.get("unit_size"))
    return max(0.0, _number(row.get("stake"), 0.0) / unit_size) if unit_size else 0.0


def _marginal_portfolio_risk(candidate, portfolio):
    open_rows = [
        row for row in (portfolio or {}).get("bets") or []
        if normalize_text(row.get("status")) in {"open", "pending", "resting"}
    ]
    related = []
    positive_correlation_units = 0.0
    for row in open_rows:
        correlation = _pair_correlation(candidate, row)
        if abs(correlation) < 0.05:
            continue
        units = _row_units(row)
        related.append({
            "ticker": row.get("kalshi_ticker"),
            "market_type": row.get("market_type"),
            "units": round(units, 3),
            "assumed_correlation": round(correlation, 3),
        })
        positive_correlation_units += max(0.0, correlation) * units
    shadow_multiplier = max(0.35, 1.0 / (1.0 + 0.20 * positive_correlation_units))
    return {
        "open_position_count": len(open_rows),
        "related_position_count": len(related),
        "positive_correlation_weighted_units": round(positive_correlation_units, 3),
        "shadow_stake_multiplier": round(shadow_multiplier, 4),
        "related_positions": sorted(related, key=lambda row: abs(row["assumed_correlation"]), reverse=True)[:10],
        "mode": "shadow_only",
        "affects_execution": False,
    }


def _failure_scenarios(candidate, card):
    price = card.get("price_quality") or {}
    sync = card.get("source_synchronization") or {}
    micro = card.get("market_microstructure") or {}
    disagreement = card.get("model_disagreement") or {}
    edge = _number(price.get("nominal_net_edge_pp"))
    scenarios = []
    if edge is not None:
        scenarios.append({
            "scenario": "fair_probability_falls",
            "break_even_move_pp": round(max(0.0, edge), 3),
            "meaning": "A probability drop of this size removes the current nominal edge.",
        })
        scenarios.append({
            "scenario": "kalshi_price_rises",
            "break_even_move_cents": round(max(0.0, edge), 3),
            "meaning": "An adverse ask move of this size removes the current nominal edge before extra slippage.",
        })
    if sync.get("status") in {"degraded", "poor"}:
        scenarios.append({"scenario": "source_time_skew", "severity": sync.get("status")})
    if disagreement.get("classification") in {"material", "severe"}:
        scenarios.append({"scenario": "model_source_disagreement", "severity": disagreement.get("classification")})
    if _number(micro.get("ask_depth_2c"), 999) < 10:
        scenarios.append({"scenario": "thin_orderbook_price_impact", "severity": "material"})
    return scenarios


def _provider_inventory(candidate):
    anchor = candidate.get("provider_fair_anchor") or {}
    return {
        "the_odds_api": {
            "available": candidate.get("odds_source") not in {None, "", "unknown"},
            "source": candidate.get("odds_source"),
            "market_level_timestamps_used": True,
            "individual_book_probabilities_used": bool(((candidate.get("pricing_v2") or {}).get("consensus") or {}).get("observations")),
        },
        "kalshi_websocket": {
            "available": bool(candidate.get("stream_snapshot")),
            "depth_used": bool((candidate.get("stream_snapshot") or {}).get("orderbook_valid")),
            "trades_used": (candidate.get("stream_snapshot") or {}).get("recent_trade_count_60s") is not None,
            "volume_open_interest_used": True,
            "exchange_timestamp_used": (candidate.get("stream_snapshot") or {}).get("exchange_ts_ms") is not None,
        },
        "sports_game_odds": {
            "available": bool(candidate.get("provider_signal_inventory")),
            "fair_anchor_available": bool(anchor.get("ok")),
            "opening_price_used_shadow": anchor.get("open_fair_probability") is not None,
            "closing_reference_used_shadow": anchor.get("close_fair_probability") is not None,
            "live_status_flags_used": bool((candidate.get("provider_signal_inventory") or {}).get("status")),
        },
    }


def empty_state():
    return {"version": STATE_VERSION, "generated_at": None, "candidates": {}}


def enrich_candidates(candidates, *, previous_state=None, portfolio=None, sharp_books=(), now=None):
    """Attach the intelligence card and return a bounded lead/lag history state."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    previous_rows = (previous_state or {}).get("candidates") or {}
    cross_reviews = _cross_market_reviews(candidates)
    groups = defaultdict(list)
    for candidate in candidates:
        groups[str(candidate.get("game_key") or candidate.get("event_id") or "")].append(candidate)
    new_rows = {}
    for candidate in candidates:
        key = _candidate_key(candidate)
        previous = previous_rows.get(key) or {}
        card = {
            "version": INTELLIGENCE_VERSION,
            "generated_at": now.isoformat(timespec="milliseconds"),
            "mode": "shadow_observability",
            "affects_execution": False,
            "outcome_probabilities": dict(candidate.get("outcome_probability_challengers") or {}),
            "probability_distribution": ((candidate.get("pricing_v2") or {}).get("probability_distribution") or {}),
            "price_quality": _price_quality(candidate),
            "market_microstructure": market_microstructure(candidate),
            "source_synchronization": source_synchronization(candidate, now),
            "sportsbook_market": sportsbook_market_intelligence(candidate, previous, sharp_books),
            "model_disagreement": model_disagreement(candidate),
            "cross_market_consistency": cross_reviews.get(id(candidate)) or {},
            "joint_scenarios": _joint_scenarios(candidate, groups[str(candidate.get("game_key") or candidate.get("event_id") or "")]),
            "game_state": {
                "features": candidate.get("game_state_features") or {},
                "shadow_probability": candidate.get("game_state_shadow") or {},
            },
            "marginal_portfolio_risk": _marginal_portfolio_risk(candidate, portfolio),
            "provider_signal_inventory": _provider_inventory(candidate),
        }
        card["failure_scenarios"] = _failure_scenarios(candidate, card)
        candidate["bet_intelligence"] = card
        consensus = ((candidate.get("pricing_v2") or {}).get("consensus") or {})
        family_probabilities = {
            str(row.get("family")): row.get("probability")
            for row in consensus.get("observations") or []
            if row.get("family") and _number(row.get("probability")) is not None
        }
        history = list(previous.get("history") or [])
        snapshot = {
            "at": now.isoformat(timespec="milliseconds"),
            "consensus_probability": consensus.get("probability"),
            "kalshi_probability": ((candidate.get("pricing_v2") or {}).get("kalshi_market_probability")),
            "entry_price": candidate.get("entry_price"),
            "update_token": consensus.get("update_token"),
        }
        if not history or history[-1].get("update_token") != snapshot.get("update_token") or history[-1].get("entry_price") != snapshot.get("entry_price"):
            history.append(snapshot)
        new_rows[key] = {
            "last_seen": snapshot["at"],
            "consensus_probability": consensus.get("probability"),
            "kalshi_probability": snapshot["kalshi_probability"],
            "entry_price": snapshot["entry_price"],
            "families": family_probabilities,
            "history": history[-60:],
        }
    # Keep recently seen inactive tickers so short schedule gaps do not erase
    # the reference point used for steam/lead-lag calculations.
    for key, row in previous_rows.items():
        if key in new_rows:
            continue
        age = _age_seconds(row.get("last_seen"), now)
        if age is not None and age <= 48 * 3600 and len(new_rows) < 5000:
            new_rows[key] = row
    state = {
        "version": STATE_VERSION,
        "generated_at": now.isoformat(timespec="milliseconds"),
        "candidates": new_rows,
    }
    return state, summarize(candidates)


def update_unit_explanation(candidate, unit_review=None):
    card = candidate.get("bet_intelligence") or {}
    review = unit_review or candidate.get("sports_units") or candidate.get("sports_game_odds_units") or {}
    metrics = review.get("metrics") or {}
    requirements = review.get("requirements") or {}
    target = float(_number(review.get("target_units"), 0) or 0)
    raw_target = float(_number(review.get("raw_target_units"), 0) or 0)
    probability_target = float(_number(review.get("probability_target_units"), target) or 0)
    requirement_units = sorted(
        number for value in requirements
        if (number := _number(value)) is not None
    )
    max_units = float(_number(
        review.get("max_units_per_market"),
        max(requirement_units or [5]),
    ) or 0)
    next_units = next((units for units in requirement_units if units > target + 1e-9), None)
    blockers = []
    next_key = str(int(next_units)) if next_units is not None and float(next_units).is_integer() else str(next_units)
    next_tier = requirements.get(next_key) or requirements.get(next_units) or {}
    metric_map = {
        "min_edge": "qualification_edge",
        "min_confidence": "confidence",
        "min_pro_score": "pro_score",
        "min_final_score": "final_score",
        "min_book_families": "independent_book_families",
    }
    for requirement, metric in metric_map.items():
        required = _number(next_tier.get(requirement))
        actual = _number(metrics.get(metric))
        if required is not None and (actual is None or actual + 1e-9 < required):
            blockers.append({
                "metric": metric,
                "actual": actual,
                "required": required,
                "shortfall": round(required - (actual or 0.0), 3),
            })
    probability = review.get("probability_sizing") or candidate.get("probability_sizing") or {}
    unit = {
        "reason": review.get("reason"),
        "raw_quality_units": raw_target,
        "probability_target_units": probability_target,
        "final_target_units": target,
        "additional_units": review.get("additional_units", review.get("placed_units")),
        "existing_units": review.get("existing_units"),
        "unit_size": review.get("unit_size"),
        "requested_stake": review.get("requested_stake"),
        "next_unit_tier": next_units,
        "next_unit_blockers": blockers,
        "risk_caps": review.get("unit_caps") or [],
        "probability_reason": probability.get("reason"),
        "kelly_units": probability.get("kelly_units"),
        "sizing_probability": probability.get("sizing_probability"),
        "posterior_p_edge_positive": (probability.get("probability_distribution") or {}).get("posterior_p_edge_positive"),
        "walk_forward_promotion_validated": probability.get("walk_forward_promotion_validated"),
    }
    card["unit_decision"] = unit
    micro = card.get("market_microstructure") or {}
    stake = _number(review.get("requested_stake"))
    entry = _number(candidate.get("entry_price"))
    if stake is not None and entry and entry > 0:
        _bids, asks = _side_levels(candidate.get("stream_snapshot") or {}, candidate.get("order_side"))
        micro["requested_stake_fill"] = _walk_asks(asks, stake / (entry / 100.0))
    card["market_microstructure"] = micro
    candidate["bet_intelligence"] = card
    return unit


def compact_intelligence(candidate):
    card = candidate.get("bet_intelligence") or {}
    micro = card.get("market_microstructure") or {}
    sportsbook = card.get("sportsbook_market") or {}
    cross = card.get("cross_market_consistency") or {}
    joint = card.get("joint_scenarios") or {}
    marginal = card.get("marginal_portfolio_risk") or {}
    return {
        "version": card.get("version"),
        "mode": card.get("mode"),
        "price_quality": card.get("price_quality") or {},
        "market_microstructure": {
            key: micro.get(key)
            for key in (
                "source", "quote_fresh", "quote_age_seconds", "exchange_latency_ms",
                "spread_cents", "orderbook_valid", "bid_depth_2c", "ask_depth_2c",
                "depth_imbalance", "price_velocity_30s_pp", "price_velocity_2m_pp",
                "quote_changes_60s", "recent_trade_count_60s",
                "recent_trade_contracts_60s", "quality_score",
            )
        },
        "source_synchronization": card.get("source_synchronization") or {},
        "sportsbook_market": {
            key: sportsbook.get(key)
            for key in (
                "family_count", "sharp_probability", "retail_probability",
                "sharp_retail_difference_pp", "sharp_signal",
                "consensus_move_since_prior_scan_pp", "steam_direction",
                "moving_family_count", "family_probability_range_pp",
            )
        },
        "model_disagreement": card.get("model_disagreement") or {},
        "cross_market_consistency": {
            key: cross.get(key)
            for key in ("status", "checks", "violations", "consistency_score")
        },
        "joint_scenarios": {
            key: joint.get(key)
            for key in (
                "pair_count", "highest_positive_correlation",
                "highest_joint_loss_probability", "method",
            )
        },
        "marginal_portfolio_risk": {
            key: marginal.get(key)
            for key in (
                "open_position_count", "related_position_count",
                "positive_correlation_weighted_units", "shadow_stake_multiplier",
            )
        },
        "unit_decision": card.get("unit_decision") or {},
        "failure_scenarios": card.get("failure_scenarios") or [],
        "provider_signal_inventory": card.get("provider_signal_inventory") or {},
    }


def summarize(candidates):
    rows = [row.get("bet_intelligence") or {} for row in candidates or []]
    sync = Counter((row.get("source_synchronization") or {}).get("status") or "unknown" for row in rows)
    disagreement = Counter((row.get("model_disagreement") or {}).get("classification") or "unknown" for row in rows)
    cross = Counter((row.get("cross_market_consistency") or {}).get("status") or "unknown" for row in rows)
    micro_scores = [
        _number((row.get("market_microstructure") or {}).get("quality_score"))
        for row in rows
    ]
    micro_scores = [value for value in micro_scores if value is not None]
    return {
        "version": INTELLIGENCE_VERSION,
        "mode": "shadow_observability",
        "affects_execution": False,
        "candidate_count": len(rows),
        "source_synchronization": dict(sync),
        "model_disagreement": dict(disagreement),
        "cross_market_consistency": dict(cross),
        "average_microstructure_quality": round(sum(micro_scores) / len(micro_scores), 2) if micro_scores else None,
        "depth_available_candidates": sum(1 for row in rows if (row.get("market_microstructure") or {}).get("orderbook_valid") is True),
        "trade_flow_available_candidates": sum(1 for row in rows if (row.get("market_microstructure") or {}).get("recent_trade_count_60s") is not None),
    }
