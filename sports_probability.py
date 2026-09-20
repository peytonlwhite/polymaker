"""Calibrated, same-book sports probability construction.

The legacy scanner selected the best price for each outcome across unrelated
books and then removed vig from that synthetic market.  That can manufacture
edge.  This module instead removes vig inside each bookmaker/line first,
collapses correlated book families, and only then builds a robust consensus.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import NormalDist, median

from kalshi_common import american_to_prob, normalize_text
from sports_audit_support import binary_outcome, integrity_valid, parse_timestamp, strategy_lane


PRICING_VERSION = "same-book-consensus-v3"
VALIDATION_VERSION = "settlement-purged-hierarchy-v1"

_BOOK_FAMILY_ALIASES = {
    "betonlineag": "betonline_lowvig",
    "lowvig": "betonline_lowvig",
    "betrivers": "kambi_us",
    "unibet_us": "kambi_us",
    "twinspires": "kambi_us",
    "williamhill_us": "caesars",
    "williamhill": "caesars",
    "fanatics": "fanatics_pointsbet",
    "pointsbetus": "fanatics_pointsbet",
    "barstool": "espnbet",
    "espnbet": "espnbet",
    "betfair_ex_eu": "betfair_exchange",
    "betfair_ex_uk": "betfair_exchange",
    "betfair_ex_au": "betfair_exchange",
    "matchbook": "matchbook_exchange",
}
BOOK_FAMILIES = {
    normalize_text(book): family
    for book, family in _BOOK_FAMILY_ALIASES.items()
}

MARKET_KEY_ALIASES = {
    "spreads": {"spreads", "alternate_spreads"},
    "totals": {"totals", "alternate_totals"},
}


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _parse_time(value):
    # Provider timestamps are UTC; application record timestamps are Chicago.
    return parse_timestamp(value, naive_tz=timezone.utc)


def remove_vig(raw_probabilities, method="power"):
    """Normalize mutually-exclusive raw probabilities within a single book."""
    raw = [max(1e-9, min(1.0, float(value))) for value in raw_probabilities]
    if not raw:
        return []
    if len(raw) == 1:
        return [raw[0]]
    total = sum(raw)
    if total <= 0:
        return [0.0 for _ in raw]
    if method != "power" or total <= 1.000001:
        return [value / total for value in raw]

    # Find exponent k where sum(p_i ** k) == 1.  With an overround, k > 1.
    low, high = 1.0, 10.0
    while sum(value**high for value in raw) > 1.0 and high < 100.0:
        high *= 2.0
    for _ in range(80):
        mid = (low + high) / 2.0
        if sum(value**mid for value in raw) > 1.0:
            low = mid
        else:
            high = mid
    exponent = (low + high) / 2.0
    adjusted = [value**exponent for value in raw]
    adjusted_total = sum(adjusted)
    return [value / adjusted_total for value in adjusted]


def _book_weight(book_key, updated_at, *, sharp_books, now, half_life_minutes):
    sharp_weight = 1.6 if normalize_text(book_key) in sharp_books else 1.0
    parsed = _parse_time(updated_at)
    if parsed is None:
        return sharp_weight * 0.45, None
    age_minutes = max(0.0, (now - parsed).total_seconds() / 60.0)
    decay = 0.5 ** (age_minutes / max(0.1, half_life_minutes))
    return sharp_weight * max(0.05, decay), age_minutes


def _book_markets(game, market_key):
    accepted_keys = MARKET_KEY_ALIASES.get(market_key, {market_key})
    for bookmaker in game.get("bookmakers") or []:
        book_key = normalize_text(bookmaker.get("key") or bookmaker.get("title") or "")
        if not book_key:
            continue
        for market in bookmaker.get("markets") or []:
            if str(market.get("key") or "").lower() not in accepted_keys:
                continue
            yield book_key, bookmaker, market


def _dedupe_book_observations(observations):
    """Keep one quote per book so featured and alternate feeds cannot double-weight it."""
    selected = {}
    for row in observations:
        book = row["book"]
        current = selected.get(book)
        row_time = _parse_time(row.get("last_update"))
        current_time = _parse_time((current or {}).get("last_update"))
        if current is None or (row_time is not None and (current_time is None or row_time > current_time)):
            selected[book] = row
    return list(selected.values())


def _isotonic_probabilities(points, *, increasing):
    """Return a monotone probability ladder using unit-weight PAVA."""
    if not points:
        return []
    signed = [float(row[1]) if increasing else -float(row[1]) for row in points]
    blocks = []
    for index, value in enumerate(signed):
        blocks.append({"start": index, "end": index, "sum": value, "weight": 1.0})
        while len(blocks) >= 2:
            left, right = blocks[-2], blocks[-1]
            left_mean = left["sum"] / left["weight"]
            right_mean = right["sum"] / right["weight"]
            if left_mean <= right_mean + 1e-12:
                break
            blocks[-2:] = [{
                "start": left["start"],
                "end": right["end"],
                "sum": left["sum"] + right["sum"],
                "weight": left["weight"] + right["weight"],
            }]
    fitted = [0.0] * len(points)
    for block in blocks:
        value = block["sum"] / block["weight"]
        if not increasing:
            value = -value
        for index in range(block["start"], block["end"] + 1):
            fitted[index] = max(1e-6, min(1.0 - 1e-6, value))
    return fitted


def _line_ladder_estimate(points, target, *, increasing, max_gap):
    """Interpolate only between independently de-vigged, monotone line quotes."""
    if target is None or not points:
        return None
    by_point = {}
    for row in points:
        point = _number(row.get("point"))
        probability = _number(row.get("probability"))
        if point is None or probability is None:
            continue
        current = by_point.get(point)
        row_time = _parse_time(row.get("last_update"))
        current_time = _parse_time((current or {}).get("last_update"))
        if current is None or (row_time is not None and (current_time is None or row_time > current_time)):
            by_point[point] = dict(row)
    ordered = [by_point[point] for point in sorted(by_point)]
    if not ordered:
        return None
    fitted = _isotonic_probabilities(
        [(float(row["point"]), float(row["probability"])) for row in ordered],
        increasing=increasing,
    )
    for row, probability in zip(ordered, fitted):
        row["fitted_probability"] = probability
    exact = next((row for row in ordered if abs(float(row["point"]) - float(target)) <= 0.011), None)
    if exact is not None:
        return {
            **exact,
            "probability": exact["fitted_probability"],
            "interpolated": False,
            "line_distance": 0.0,
            "bracket_width": 0.0,
            "ladder_points": len(ordered),
        }
    lower = max((row for row in ordered if float(row["point"]) < float(target)), key=lambda row: float(row["point"]), default=None)
    upper = min((row for row in ordered if float(row["point"]) > float(target)), key=lambda row: float(row["point"]), default=None)
    if lower is None or upper is None:
        return None
    lower_point = float(lower["point"])
    upper_point = float(upper["point"])
    gap = upper_point - lower_point
    if gap <= 0 or gap > max(0.01, float(max_gap)):
        return None
    fraction = (float(target) - lower_point) / gap
    probability = lower["fitted_probability"] + fraction * (
        upper["fitted_probability"] - lower["fitted_probability"]
    )
    nearest = min((lower, upper), key=lambda row: abs(float(row["point"]) - float(target)))
    return {
        **nearest,
        "point": float(target),
        "probability": max(1e-6, min(1.0 - 1e-6, probability)),
        "selected_price": None,
        "interpolated": True,
        "line_distance": round(min(abs(float(target) - lower_point), abs(upper_point - float(target))), 3),
        "bracket_width": round(gap, 3),
        "bracket_points": [lower_point, upper_point],
        "ladder_points": len(ordered),
    }


def _moneyline_observations(game, selected_team, method):
    selected_key = normalize_text(selected_team)
    observations = []
    for book_key, bookmaker, market in _book_markets(game, "h2h"):
        outcomes = [
            outcome
            for outcome in market.get("outcomes") or []
            if outcome.get("name") and _number(outcome.get("price")) is not None
        ]
        if str(game.get("sport_key") or "").startswith("soccer_"):
            # A two-way/draw-no-bet quote is conditional on the game not
            # drawing. It cannot price a regulation-time team-win contract.
            expected = {normalize_text(game.get("home_team")), normalize_text(game.get("away_team")), "draw"}
            names = [normalize_text(row.get("name")) for row in outcomes]
            if len(outcomes) != 3 or set(names) != expected:
                continue
        selected_index = next(
            (index for index, row in enumerate(outcomes) if normalize_text(row.get("name")) == selected_key),
            None,
        )
        if selected_index is None or len(outcomes) < 2:
            continue
        fair = remove_vig([american_to_prob(row["price"]) for row in outcomes], method)
        observations.append(
            {
                "book": book_key,
                "family": BOOK_FAMILIES.get(book_key, book_key),
                "probability": fair[selected_index],
                "selected_price": int(float(outcomes[selected_index]["price"])),
                "paired_outcomes": len(outcomes),
                "last_update": market.get("last_update") or bookmaker.get("last_update"),
                "source": bookmaker.get("_source") or game.get("_odds_source") or "odds_api",
            }
        )
    return observations


def _spread_observations(game, selected_team, target_point, method, *, ladder_enabled=True, ladder_max_gap=2.5):
    selected_key = normalize_text(selected_team)
    target = _number(target_point)
    if target is None:
        return []
    book_ladders = defaultdict(list)
    book_metadata = {}
    for book_key, bookmaker, market in _book_markets(game, "spreads"):
        outcomes = [
            outcome
            for outcome in market.get("outcomes") or []
            if outcome.get("name")
            and _number(outcome.get("price")) is not None
            and _number(outcome.get("point")) is not None
        ]
        last_update = market.get("last_update") or bookmaker.get("last_update")
        book_metadata[book_key] = {
            "book": book_key,
            "family": BOOK_FAMILIES.get(book_key, book_key),
            "last_update": last_update,
            "source": bookmaker.get("_source") or game.get("_odds_source") or "odds_api",
        }
        for selected in outcomes:
            if normalize_text(selected.get("name")) != selected_key:
                continue
            selected_point = float(selected["point"])
            opposite = next(
                (
                    row for row in outcomes
                    if normalize_text(row.get("name")) != selected_key
                    and abs(float(row["point"]) + selected_point) <= 0.011
                ),
                None,
            )
            if opposite is None:
                continue
            fair = remove_vig(
                [american_to_prob(selected["price"]), american_to_prob(opposite["price"])],
                method,
            )
            book_ladders[book_key].append({
                "point": selected_point,
                "probability": fair[0],
                "selected_price": int(float(selected["price"])),
                "opposite_point": float(opposite["point"]),
                "paired_outcomes": 2,
                "last_update": last_update,
            })
    observations = []
    for book_key, points in book_ladders.items():
        estimate = _line_ladder_estimate(
            points,
            target,
            increasing=True,
            max_gap=ladder_max_gap if ladder_enabled else 0.01,
        )
        if estimate is None:
            continue
        observations.append({
            **book_metadata[book_key],
            **estimate,
            "selected_point": float(target),
            "line_model": "monotone_interpolation" if estimate.get("interpolated") else "exact_line",
        })
    return observations


def _total_observations(game, side, target_point, method, *, ladder_enabled=True, ladder_max_gap=2.5):
    side_key = normalize_text(side)
    opposite_key = "under" if side_key == "over" else "over"
    target = _number(target_point)
    if target is None or side_key not in {"over", "under"}:
        return []
    book_ladders = defaultdict(list)
    book_metadata = {}
    for book_key, bookmaker, market in _book_markets(game, "totals"):
        outcomes = [
            outcome
            for outcome in market.get("outcomes") or []
            if _number(outcome.get("price")) is not None and _number(outcome.get("point")) is not None
        ]
        last_update = market.get("last_update") or bookmaker.get("last_update")
        book_metadata[book_key] = {
            "book": book_key,
            "family": BOOK_FAMILIES.get(book_key, book_key),
            "last_update": last_update,
            "source": bookmaker.get("_source") or game.get("_odds_source") or "odds_api",
        }
        for selected in outcomes:
            if normalize_text(selected.get("name")) != side_key:
                continue
            selected_point = float(selected["point"])
            opposite = next(
                (
                    row for row in outcomes
                    if normalize_text(row.get("name")) == opposite_key
                    and abs(float(row["point"]) - selected_point) <= 0.011
                ),
                None,
            )
            if opposite is None:
                continue
            fair = remove_vig(
                [american_to_prob(selected["price"]), american_to_prob(opposite["price"])],
                method,
            )
            book_ladders[book_key].append({
                "point": selected_point,
                "probability": fair[0],
                "selected_price": int(float(selected["price"])),
                "paired_outcomes": 2,
                "last_update": last_update,
            })
    observations = []
    for book_key, points in book_ladders.items():
        estimate = _line_ladder_estimate(
            points,
            target,
            increasing=side_key == "under",
            max_gap=ladder_max_gap if ladder_enabled else 0.01,
        )
        if estimate is None:
            continue
        observations.append({
            **book_metadata[book_key],
            **estimate,
            "selected_point": float(target),
            "line_model": "monotone_interpolation" if estimate.get("interpolated") else "exact_line",
        })
    return observations


def _collapse_families(observations):
    grouped = defaultdict(list)
    for row in observations:
        grouped[row["family"]].append(row)
    collapsed = []
    for family, rows in grouped.items():
        total_weight = sum(max(0.0, row["weight"]) for row in rows)
        if total_weight <= 0:
            continue
        representative = max(rows, key=lambda row: row["weight"])
        collapsed.append(
            {
                **representative,
                "family": family,
                "books": sorted({row["book"] for row in rows}),
                "probability": sum(row["probability"] * row["weight"] for row in rows) / total_weight,
                # A family gets at most its strongest member's influence.
                "weight": max(row["weight"] for row in rows),
                "family_book_count": len(rows),
                "contributing_updates": [row.get("last_update") for row in rows],
                "all_exact_lines": all(not row.get("interpolated") and row.get("line_model", "exact_line") == "exact_line" for row in rows),
            }
        )
    return collapsed


def _consensus_update_token(families):
    """Stable fingerprint of the actual bookmaker observations in a consensus."""
    parts = []
    for row in sorted(families, key=lambda item: str(item.get("family") or "")):
        parts.append(
            "|".join(
                (
                    str(row.get("family") or ""),
                    ",".join(sorted(str(book) for book in row.get("books") or [])),
                    str(row.get("last_update") or ""),
                    f"{float(row.get('probability') or 0):.8f}",
                    str(row.get("selected_price") or ""),
                )
            )
        )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:20] if parts else ""


def _weighted_mean(rows):
    total = sum(max(0.0, row["weight"]) for row in rows)
    if total <= 0:
        return 0.0
    return sum(row["probability"] * row["weight"] for row in rows) / total


def _effective_sample_size(rows):
    weights = [max(0.0, row["weight"]) for row in rows]
    total = sum(weights)
    squares = sum(weight * weight for weight in weights)
    return (total * total / squares) if squares > 0 else 0.0


def _robust_family_outlier_split(families):
    """Remove only isolated, extreme family prices from a deep consensus.

    The median/MAD gate deliberately does nothing for small samples or broad
    two-sided disagreement. It is intended for the one-family bad-line case,
    not for hiding a real market disagreement.
    """
    rows = list(families or [])
    if len(rows) < 5:
        return rows, []
    center = median(row["probability"] for row in rows)
    mad = median(abs(row["probability"] - center) for row in rows)
    threshold = max(0.06, 4.0 * mad)
    core = [row for row in rows if abs(row["probability"] - center) <= threshold]
    outliers = [row for row in rows if abs(row["probability"] - center) > threshold]
    max_outliers = max(1, int(len(rows) * 0.20))
    if len(core) < 4 or not outliers or len(outliers) > max_outliers:
        return rows, []
    return core, outliers


def build_consensus(
    game,
    *,
    market_type,
    selected_team="",
    side="",
    target_point=None,
    sharp_books=(),
    method="power",
    now=None,
    half_life_minutes=6.0,
    max_age_minutes=10.0,
    min_uncertainty_pp=2.0,
    require_timestamps=False,
    line_ladder_enabled=True,
    line_ladder_max_gap=2.5,
):
    """Return a same-book de-vigged consensus with an uncertainty interval."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    sharp = {normalize_text(book) for book in sharp_books}
    market_type = str(market_type or "").lower()
    if market_type == "moneyline":
        raw = _moneyline_observations(game, selected_team, method)
    elif market_type == "spread":
        raw = _spread_observations(
            game,
            selected_team,
            target_point,
            method,
            ladder_enabled=line_ladder_enabled,
            ladder_max_gap=line_ladder_max_gap,
        )
    elif market_type == "total":
        raw = _total_observations(
            game,
            side,
            target_point,
            method,
            ladder_enabled=line_ladder_enabled,
            ladder_max_gap=line_ladder_max_gap,
        )
    else:
        raw = []

    raw = _dedupe_book_observations(raw)
    kept = []
    stale = []
    missing_timestamp = []
    for row in raw:
        weight, age = _book_weight(
            row["book"],
            row.get("last_update"),
            sharp_books=sharp,
            now=now,
            half_life_minutes=half_life_minutes,
        )
        enriched = {**row, "weight": weight, "age_minutes": age}
        if age is None and require_timestamps:
            missing_timestamp.append(enriched)
        elif age is not None and age > max_age_minutes:
            stale.append(enriched)
        else:
            kept.append(enriched)
    all_families = _collapse_families(kept)
    if not all_families:
        return {
            "version": PRICING_VERSION,
            "ok": False,
            "reason": "same_book_consensus_unavailable",
            "market_type": market_type,
            "raw_book_count": len(raw),
            "stale_book_count": len(stale),
            "missing_timestamp_book_count": len(missing_timestamp),
            "failure_diagnostics": {
                "paired_exact_line_books": len(raw),
                "stale_books": len(stale),
                "missing_timestamp_books": len(missing_timestamp),
                "target_point": _number(target_point),
            },
            "observations": [],
        }
    families, outlier_families = _robust_family_outlier_split(all_families)

    probability = _weighted_mean(families)
    variance = _weighted_mean(
        [{**row, "probability": (row["probability"] - probability) ** 2} for row in families]
    )
    standard_deviation = math.sqrt(max(0.0, variance))
    effective_n = max(1.0, _effective_sample_size(families))
    sampling_half_width = 1.96 * standard_deviation / math.sqrt(effective_n)
    uncertainty = max(float(min_uncertainty_pp) / 100.0, sampling_half_width)
    ages = [row["age_minutes"] for row in families if row.get("age_minutes") is not None]
    average_age = sum(ages) / len(ages) if ages else None
    if average_age is not None:
        uncertainty += min(0.02, average_age * 0.0015)
    interpolated_families = [row for row in families if row.get("interpolated")]
    interpolation_distance = max(
        [float(row.get("line_distance") or 0.0) for row in interpolated_families]
        or [0.0]
    )
    interpolation_uncertainty = min(
        0.03,
        (0.0025 + 0.005 * interpolation_distance) if interpolated_families else 0.0,
    )
    uncertainty += interpolation_uncertainty
    uncertainty = min(0.20, uncertainty)

    representative_family = min(
        families,
        key=lambda row: abs(row["probability"] - probability),
    )
    selected_price = representative_family.get("selected_price")
    family_range = max(row["probability"] for row in families) - min(row["probability"] for row in families)
    outlier_books = {book for row in outlier_families for book in row.get("books") or []}

    def public_observation(row):
        return {
            "family": row["family"],
            "books": row["books"],
            "probability": round(row["probability"] * 100.0, 3),
            "weight": round(row["weight"], 5),
            "age_minutes": round(row["age_minutes"], 3) if row.get("age_minutes") is not None else None,
            "last_update": row.get("last_update"),
            "contributing_updates": row.get("contributing_updates") or [row.get("last_update")],
            "all_exact_lines": row.get("all_exact_lines", True),
            "source": row.get("source"),
            "selected_price": row.get("selected_price"),
            "paired_outcomes": row.get("paired_outcomes"),
            "line_model": row.get("line_model") or "exact_line",
            "interpolated": bool(row.get("interpolated")),
            "selected_point": row.get("selected_point"),
            "line_distance": row.get("line_distance"),
            "bracket_width": row.get("bracket_width"),
            "bracket_points": row.get("bracket_points"),
            "ladder_points": row.get("ladder_points"),
        }
    return {
        "version": PRICING_VERSION,
        "ok": True,
        "method": method,
        "market_type": market_type,
        "probability": round(probability * 100.0, 3),
        "lower_probability": round(max(0.0, probability - uncertainty) * 100.0, 3),
        "upper_probability": round(min(1.0, probability + uncertainty) * 100.0, 3),
        "uncertainty_pp": round(uncertainty * 100.0, 3),
        "raw_book_count": len(kept),
        "independent_family_count": len(families),
        "pre_filter_independent_family_count": len(all_families),
        "outlier_book_family_count": len(outlier_families),
        "outlier_observations": [public_observation(row) for row in sorted(outlier_families, key=lambda item: item["probability"])],
        "sharp_book_count": sum(
            1
            for row in families
            if any(book in sharp and book not in outlier_books for book in row.get("books") or [])
        ),
        "stale_book_count": len(stale),
        "missing_timestamp_book_count": len(missing_timestamp),
        "average_age_minutes": round(average_age, 3) if average_age is not None else None,
        "sampling_half_width_pp": round(sampling_half_width * 100.0, 3),
        "family_probability_range_pp": round(family_range * 100.0, 3),
        "line_ladder_enabled": bool(line_ladder_enabled),
        "line_ladder_interpolated_family_count": len(interpolated_families),
        "line_ladder_max_distance": round(interpolation_distance, 3),
        "line_ladder_uncertainty_pp": round(interpolation_uncertainty * 100.0, 3),
        "representative_selected_price": selected_price,
        "update_token": _consensus_update_token(families),
        "observations": [public_observation(row) for row in sorted(families, key=lambda item: item["probability"])],
    }


def segment_key(sport_key, market_type, timing_bucket):
    return "|".join(
        (
            normalize_text(sport_key) or "unknown",
            normalize_text(market_type) or "unknown",
            normalize_text(timing_bucket) or "unknown",
        )
    )


def probability_price_band(probability):
    value = max(0.0, min(100.0, float(_number(probability, 50.0))))
    if value < 25.0:
        return "under_25"
    if value < 40.0:
        return "25_to_40"
    if value < 60.0:
        return "40_to_60"
    if value < 75.0:
        return "60_to_75"
    return "75_plus"


def settlement_time_bucket(minutes_to_settlement):
    minutes = _number(minutes_to_settlement)
    if minutes is None:
        return "unknown"
    if minutes <= 10.0:
        return "final_10m"
    if minutes <= 30.0:
        return "10_to_30m"
    if minutes <= 120.0:
        return "30_to_120m"
    if minutes <= 360.0:
        return "2_to_6h"
    return "over_6h"


def conditional_segment_key(sport_key, market_type, timing_bucket, price_band, settlement_bucket):
    return "|".join(
        (
            segment_key(sport_key, market_type, timing_bucket),
            normalize_text(price_band) or "unknown price",
            normalize_text(settlement_bucket) or "unknown settlement",
        )
    )


def _sigmoid(value):
    value = max(-40.0, min(40.0, float(value)))
    return 1.0 / (1.0 + math.exp(-value))


def _logit(probability):
    value = max(1e-6, min(1.0 - 1e-6, float(probability)))
    return math.log(value / (1.0 - value))


def apply_beta_calibration(probability, coefficients):
    """Apply beta calibration; [1, 1, 0] is the identity mapping."""
    value = max(1e-6, min(1.0 - 1e-6, float(probability)))
    coefficients = list(coefficients or [1.0, 1.0, 0.0])
    if len(coefficients) != 3:
        coefficients = [1.0, 1.0, 0.0]
    score = (
        float(coefficients[0]) * math.log(value)
        + float(coefficients[1]) * -math.log(1.0 - value)
        + float(coefficients[2])
    )
    return max(1e-6, min(1.0 - 1e-6, _sigmoid(score)))


def calibration_weight(state, *, sport_key, market_type, timing_bucket, default_weight=0.25):
    state = state if isinstance(state, dict) else {}
    key = segment_key(sport_key, market_type, timing_bucket)
    segment = (state.get("segments") or {}).get(key) or {}
    segment_count = int(segment.get("effective_event_count") or segment.get("count") or 0)
    if segment_count >= 10:
        return max(0.0, min(1.0, float(segment.get("book_weight", default_weight)))), key
    global_row = state.get("global") or {}
    global_count = int(global_row.get("effective_event_count") or global_row.get("count") or 0)
    if global_count >= 40:
        return max(0.0, min(1.0, float(global_row.get("book_weight", default_weight)))), "global"
    return max(0.0, min(1.0, float(default_weight))), "default"


def calibration_diagnostics(state, source):
    state = state if isinstance(state, dict) else {}
    if source == "global":
        return dict(state.get("global") or {})
    if source == "default":
        return {}
    if ":" in str(source):
        group, key = str(source).split(":", 1)
        mapping = {
            "conditional": "conditional_segments",
            "segment": "segments",
            "sport_market": "sport_markets",
            "sport": "sports",
        }.get(group)
        if mapping:
            return dict((state.get(mapping) or {}).get(key) or {})
    return dict((state.get("segments") or {}).get(source) or {})


def calibration_profile(
    state,
    *,
    sport_key,
    market_type,
    timing_bucket,
    market_probability,
    settlement_bucket="unknown",
    default_weight=0.25,
):
    """Select the deepest adequately sampled hierarchical calibration profile."""
    state = state if isinstance(state, dict) else {}
    sport = normalize_text(sport_key) or "unknown"
    market = normalize_text(market_type) or "unknown"
    segment = segment_key(sport, market, timing_bucket)
    conditional = conditional_segment_key(
        sport,
        market,
        timing_bucket,
        probability_price_band(market_probability),
        settlement_bucket,
    )
    choices = (
        ("conditional", conditional, (state.get("conditional_segments") or {}).get(conditional), 20),
        ("segment", segment, (state.get("segments") or {}).get(segment), 10),
        ("sport_market", f"{sport}|{market}", (state.get("sport_markets") or {}).get(f"{sport}|{market}"), 20),
        ("sport", sport, (state.get("sports") or {}).get(sport), 30),
        ("global", "global", state.get("global") or {}, 40),
    )
    for group, key, profile, minimum in choices:
        count = int((profile or {}).get("effective_event_count") or (profile or {}).get("count") or 0)
        if count < minimum:
            continue
        source = "global" if group == "global" else f"{group}:{key}"
        return {
            "source": source,
            "profile": dict(profile),
            "book_weight": max(0.0, min(1.0, float(profile.get("book_weight", default_weight)))),
            "price_band": probability_price_band(market_probability),
            "settlement_bucket": settlement_bucket or "unknown",
        }
    return {
        "source": "default",
        "profile": {},
        "book_weight": max(0.0, min(1.0, float(default_weight))),
        "price_band": probability_price_band(market_probability),
        "settlement_bucket": settlement_bucket or "unknown",
    }


def calibrated_ensemble_probability(raw_probability, profile):
    beta = (profile or {}).get("beta_calibration") or {}
    coefficients = beta.get("coefficients") or [1.0, 1.0, 0.0]
    return apply_beta_calibration(float(raw_probability) / 100.0, coefficients) * 100.0


def blend_with_kalshi(
    consensus,
    *,
    bid_cents,
    ask_cents,
    book_weight,
    uncertainty_floor_pp=1.0,
    calibration_error_pp=0.0,
):
    if not consensus.get("ok"):
        return {
            "version": PRICING_VERSION,
            "ok": False,
            "reason": consensus.get("reason", "consensus_unavailable"),
            "consensus": consensus,
        }
    bid = max(0.0, min(100.0, float(bid_cents or 0)))
    ask = max(0.0, min(100.0, float(ask_cents or 0)))
    if bid <= 0 or ask <= 0 or ask < bid:
        return {"version": PRICING_VERSION, "ok": False, "reason": "invalid_kalshi_book"}
    market_mid = (bid + ask) / 2.0
    independent_books = int(consensus.get("independent_family_count") or 0)
    # Two independent sportsbook families are the execution minimum. Once that
    # minimum is met, do not dilute the configured sportsbook weight again; the
    # consensus uncertainty already accounts for sample size and disagreement.
    reliability = min(1.0, independent_books / 2.0)
    effective_book_weight = max(0.0, min(1.0, float(book_weight))) * reliability
    book_probability = float(consensus["probability"])
    fair_probability = (
        effective_book_weight * book_probability
        + (1.0 - effective_book_weight) * market_mid
    )
    book_uncertainty = float(consensus.get("uncertainty_pp") or 0)
    market_uncertainty = (ask - bid) / 2.0
    modeled_uncertainty = math.sqrt(
        (effective_book_weight * book_uncertainty) ** 2
        + ((1.0 - effective_book_weight) * market_uncertainty) ** 2
    )
    combined_uncertainty = max(float(uncertainty_floor_pp), modeled_uncertainty)
    empirical_uncertainty = max(
        combined_uncertainty,
        max(0.0, float(calibration_error_pp or 0.0)),
    )
    return {
        "version": PRICING_VERSION,
        "ok": True,
        "book_probability": round(book_probability, 3),
        "kalshi_mid_probability": round(market_mid, 3),
        "requested_book_weight": round(float(book_weight), 4),
        "effective_book_weight": round(effective_book_weight, 4),
        "fair_probability": round(fair_probability, 3),
        "uncertainty_pp": round(combined_uncertainty, 3),
        "empirical_uncertainty_pp": round(empirical_uncertainty, 3),
        "calibration_error_floor_pp": round(max(0.0, float(calibration_error_pp or 0.0)), 3),
        "lower_probability": round(max(0.0, fair_probability - combined_uncertainty), 3),
        "upper_probability": round(min(100.0, fair_probability + combined_uncertainty), 3),
        "lower_probability_90": round(max(0.0, fair_probability - 1.645 * empirical_uncertainty), 3),
        "upper_probability_90": round(min(100.0, fair_probability + 1.645 * empirical_uncertainty), 3),
        "raw_edge_pp": round(fair_probability - ask, 3),
        "conservative_edge_before_fee_pp": round(fair_probability - combined_uncertainty - ask, 3),
        "bid_cents": round(bid, 3),
        "ask_cents": round(ask, 3),
        "spread_cents": round(ask - bid, 3),
        "consensus": consensus,
    }


def probability_distribution(
    pricing,
    *,
    entry_price_cents,
    estimated_fee_cents=0.0,
    estimated_slippage_cents=0.0,
    draw_count=401,
    robust_quantile=0.35,
):
    """Build a deterministic logit-normal posterior over the true win probability."""
    pricing = pricing if isinstance(pricing, dict) else {}
    center = _number(
        pricing.get("calibrated_ensemble_probability"),
        _number(pricing.get("fair_probability")),
    )
    entry = _number(entry_price_cents)
    if center is None or entry is None or not 0.0 < center < 100.0 or not 0.0 < entry < 100.0:
        return {"version": "logit-normal-posterior-v1", "ok": False, "reason": "invalid_probability_or_price"}
    diagnostics = pricing.get("calibration_diagnostics") or {}
    uncertainty_pp = max(
        0.25,
        float(pricing.get("empirical_uncertainty_pp") or pricing.get("uncertainty_pp") or 0.0),
        float(diagnostics.get("expected_calibration_error_pp") or 0.0) * 0.5,
    )
    center_probability = max(1e-4, min(1.0 - 1e-4, center / 100.0))
    probability_sigma = min(0.30, uncertainty_pp / 100.0)
    logit_sigma = min(2.5, probability_sigma / max(0.03, center_probability * (1.0 - center_probability)))
    count = max(101, min(2001, int(draw_count or 401)))
    normal = NormalDist()
    draws = []
    for index in range(count):
        quantile = (index + 0.5) / count
        z_value = normal.inv_cdf(quantile)
        draws.append(_sigmoid(_logit(center_probability) + logit_sigma * z_value))
    draws.sort()

    def percentile(value):
        value = max(0.0, min(1.0, float(value)))
        index = min(len(draws) - 1, max(0, int(round(value * (len(draws) - 1)))))
        return draws[index]

    all_in_price = min(
        99.99,
        entry + max(0.0, float(estimated_fee_cents or 0.0)) + max(0.0, float(estimated_slippage_cents or 0.0)),
    )
    edge_threshold = all_in_price / 100.0
    p_edge_positive = sum(1 for probability in draws if probability > edge_threshold) / len(draws)
    robust_probability = percentile(robust_quantile)
    return {
        "version": "logit-normal-posterior-v1",
        "ok": True,
        "draw_count": len(draws),
        "center_probability": round(center, 3),
        "mean_probability": round(sum(draws) / len(draws) * 100.0, 3),
        "median_probability": round(percentile(0.5) * 100.0, 3),
        "lower_probability_90": round(percentile(0.05) * 100.0, 3),
        "upper_probability_90": round(percentile(0.95) * 100.0, 3),
        "downside_probability_25": round(percentile(0.25) * 100.0, 3),
        "upside_probability_75": round(percentile(0.75) * 100.0, 3),
        "robust_quantile": round(max(0.0, min(1.0, float(robust_quantile))), 4),
        "robust_probability": round(robust_probability * 100.0, 3),
        "uncertainty_pp": round(uncertainty_pp, 3),
        "logit_sigma": round(logit_sigma, 6),
        "entry_price_cents": round(entry, 3),
        "estimated_fee_cents": round(max(0.0, float(estimated_fee_cents or 0.0)), 3),
        "estimated_slippage_cents": round(max(0.0, float(estimated_slippage_cents or 0.0)), 3),
        "all_in_price_cents": round(all_in_price, 3),
        "posterior_p_edge_positive": round(p_edge_positive, 4),
        "median_net_edge_pp": round(percentile(0.5) * 100.0 - all_in_price, 3),
        "downside_net_edge_25_pp": round(percentile(0.25) * 100.0 - all_in_price, 3),
    }


def probability_aware_sizing(
    pricing,
    *,
    entry_price_cents,
    bankroll,
    unit_size,
    quality_units,
    quality_cap_units,
    max_units=5,
    enabled=True,
    kelly_fraction=0.25,
    interval_weight=0.25,
    calibration_bias_prior_events=50.0,
    max_promotion_units=2,
    promotion_min_p_edge_positive=0.80,
    promotion_min_calibration_events=10,
    longshot_probability_floor=25.0,
    longshot_probability_mid=35.0,
    longshot_floor_max_units=1,
    longshot_mid_max_units=2,
    estimated_fee_cents=0.0,
    estimated_slippage_cents=0.0,
    posterior_draw_count=401,
    robust_probability_quantile=0.35,
    require_walk_forward_validation=False,
    unit_increment=0.5,
):
    """Size a qualified bet from calibrated probability, price, and uncertainty.

    Entry qualification remains the caller's responsibility.  This review can
    reduce a quality tier, or promote it only when the non-edge quality gates,
    calibration sample, and probability-of-positive-edge test all agree.
    Existing qualified bets retain a half-unit floor so sizing does not
    silently turn the strategy into a lower-volume entry filter. When
    walk-forward validation is required, unvalidated segments cannot size
    above that floor.
    """
    unit_increment = max(0.001, float(unit_increment or 0.5))

    def quantize_units(value):
        value = max(0.0, float(value or 0.0))
        return round(math.floor((value + 1e-9) / unit_increment) * unit_increment, 3)

    quality_units = quantize_units(quality_units)
    quality_cap_units = quantize_units(quality_cap_units)
    max_units = quantize_units(max_units)
    diagnostics = pricing.get("calibration_diagnostics") or {} if isinstance(pricing, dict) else {}
    walk_forward = diagnostics.get("walk_forward_validation") or {}
    walk_forward_profile_validated = bool(
        walk_forward.get("promotion_validated")
        and walk_forward.get("validation_version") == VALIDATION_VERSION
        and walk_forward.get("promotion_enabled")
    )
    calibration_source = str(pricing.get("calibration_source") or "default") if isinstance(pricing, dict) else "default"
    segment_validation_source = calibration_source.startswith(
        ("conditional:", "segment:", "sport_market:")
    )
    walk_forward_validated = bool(
        walk_forward_profile_validated and segment_validation_source
    )
    walk_forward_size_cap = (
        max_units
        if not require_walk_forward_validation or walk_forward_validated
        else min(max_units, unit_increment)
    )
    fallback_target = min(
        quality_units,
        quality_cap_units,
        max_units,
        walk_forward_size_cap,
    )
    base = {
        "version": "probability-kelly-v4-walk-forward-size-gate",
        "enabled": bool(enabled),
        "active": False,
        "unit_increment": unit_increment,
        "quality_units": quality_units,
        "quality_cap_units": quality_cap_units,
        "target_units": fallback_target,
        "reason": "disabled" if not enabled else "pricing_unavailable",
        "walk_forward_promotion_required": bool(require_walk_forward_validation),
        "walk_forward_promotion_validated": walk_forward_validated,
        "walk_forward_profile_validated": walk_forward_profile_validated,
        "walk_forward_segment_source": segment_validation_source,
        "walk_forward_size_cap_units": walk_forward_size_cap,
        "walk_forward_validation": walk_forward,
    }
    if not enabled:
        return base
    if not isinstance(pricing, dict) or not pricing.get("ok"):
        return base
    try:
        raw_probability = float(pricing["fair_probability"])
        entry_price = float(entry_price_cents)
        bankroll = max(0.0, float(bankroll))
        unit_size = max(0.0, float(unit_size))
    except (KeyError, TypeError, ValueError):
        return base
    if not (0.0 < raw_probability < 100.0 and 0.0 < entry_price < 100.0):
        base["reason"] = "invalid_probability_or_price"
        return base
    if bankroll <= 0 or unit_size <= 0:
        base["reason"] = "invalid_bankroll_or_unit_size"
        return base

    calibration_events = max(
        0,
        int(diagnostics.get("effective_event_count") or diagnostics.get("count") or 0),
    )
    raw_bias_pp = float(diagnostics.get("calibration_bias_pp") or 0.0)
    bias_prior = max(0.0, float(calibration_bias_prior_events or 0.0))
    bias_reliability = (
        calibration_events / (calibration_events + bias_prior)
        if calibration_events + bias_prior > 0
        else 0.0
    )
    applied_bias_pp = raw_bias_pp * bias_reliability
    ensemble_probability = _number(pricing.get("calibrated_ensemble_probability"))
    beta_calibration_applied = bool(ensemble_probability is not None and walk_forward_profile_validated)
    if beta_calibration_applied:
        calibrated_probability = max(0.01, min(99.99, ensemble_probability))
        applied_bias_pp = raw_probability - calibrated_probability
    else:
        calibrated_probability = max(0.01, min(99.99, raw_probability - applied_bias_pp))
    empirical_uncertainty_pp = max(
        0.0,
        float(
            pricing.get("empirical_uncertainty_pp")
            or pricing.get("uncertainty_pp")
            or 0.0
        ),
    )
    distribution = (
        pricing.get("probability_distribution")
        if beta_calibration_applied
        else None
    ) or probability_distribution(
        {**pricing, "calibrated_ensemble_probability": calibrated_probability},
        entry_price_cents=entry_price,
        estimated_fee_cents=estimated_fee_cents,
        estimated_slippage_cents=estimated_slippage_cents,
        draw_count=posterior_draw_count,
        robust_quantile=robust_probability_quantile,
    )
    interval_half_width_90_pp = min(49.0, 1.645 * empirical_uncertainty_pp)
    lower_probability_90 = max(0.01, calibrated_probability - interval_half_width_90_pp)
    upper_probability_90 = min(99.99, calibrated_probability + interval_half_width_90_pp)
    if distribution.get("ok"):
        lower_probability_90 = float(distribution.get("lower_probability_90") or lower_probability_90)
        upper_probability_90 = float(distribution.get("upper_probability_90") or upper_probability_90)
    interval_weight = max(0.0, min(1.0, float(interval_weight or 0.0)))
    sizing_probability = (
        float(distribution.get("robust_probability"))
        if distribution.get("ok") and distribution.get("robust_probability") is not None
        else max(
            0.01,
            calibrated_probability
            - interval_weight * (calibrated_probability - lower_probability_90),
        )
    )

    effective_price = max(
        0.01,
        min(
            99.99,
            entry_price
            + max(0.0, float(estimated_fee_cents or 0.0))
            + max(0.0, float(estimated_slippage_cents or 0.0)),
        ),
    )
    probability = sizing_probability / 100.0
    net_odds = (100.0 - effective_price) / effective_price
    full_kelly_fraction = max(
        0.0,
        (net_odds * probability - (1.0 - probability)) / net_odds,
    ) if net_odds > 0 else 0.0
    applied_kelly_fraction = full_kelly_fraction * max(0.0, float(kelly_fraction or 0.0))
    kelly_stake = bankroll * applied_kelly_fraction
    raw_kelly_units = kelly_stake / unit_size
    kelly_units = min(max_units, quantize_units(raw_kelly_units))
    if quality_units > 0:
        # Probability-aware sizing changes stake, not the entry decision.  A
        # candidate that already passed the strategy remains a minimum 0.5U bet.
        kelly_units = max(unit_increment, kelly_units)

    p_edge_positive = float(
        distribution.get("posterior_p_edge_positive")
        if distribution.get("posterior_p_edge_positive") is not None
        else pricing.get("p_edge_positive")
        or 0.0
    )
    calibration_ready = calibration_events >= max(0, int(promotion_min_calibration_events or 0))
    promotion_ready = bool(
        quality_units > 0
        and calibration_ready
        and p_edge_positive >= float(promotion_min_p_edge_positive or 0.0)
        and (not require_walk_forward_validation or walk_forward_validated)
    )
    promotion_cap = quality_units
    if promotion_ready:
        promotion_cap = min(
            max_units,
            quality_cap_units,
            quality_units + max(0.0, float(max_promotion_units or 0)),
        )

    longshot_cap = max_units
    if calibrated_probability < float(longshot_probability_floor):
        longshot_cap = quantize_units(longshot_floor_max_units)
    elif calibrated_probability < float(longshot_probability_mid):
        longshot_cap = quantize_units(longshot_mid_max_units)

    target_units = min(
        max_units,
        quality_cap_units,
        kelly_units,
        promotion_cap,
        longshot_cap,
        walk_forward_size_cap,
    )
    target_units = quantize_units(target_units)
    if (
        require_walk_forward_validation
        and not walk_forward_validated
        and quality_units > walk_forward_size_cap
        and target_units <= walk_forward_size_cap
    ):
        reason = "reduced_by_walk_forward_validation"
    elif target_units < quality_units:
        reason = "reduced_by_probability_kelly"
    elif target_units > quality_units:
        reason = "promoted_by_probability_kelly"
    else:
        reason = "quality_units_confirmed"

    return {
        **base,
        "active": True,
        "reason": reason,
        "target_units": target_units,
        "raw_win_probability": round(raw_probability, 3),
        "calibrated_win_probability": round(calibrated_probability, 3),
        "lower_win_probability_90": round(lower_probability_90, 3),
        "upper_win_probability_90": round(upper_probability_90, 3),
        "sizing_probability": round(sizing_probability, 3),
        "empirical_uncertainty_pp": round(empirical_uncertainty_pp, 3),
        "calibration_source": pricing.get("calibration_source") or "default",
        "calibration_events": calibration_events,
        "raw_calibration_bias_pp": round(raw_bias_pp, 3),
        "calibration_bias_reliability": round(bias_reliability, 4),
        "applied_calibration_bias_pp": round(applied_bias_pp, 3),
        "beta_calibration_available": ensemble_probability is not None,
        "beta_calibration_applied": beta_calibration_applied,
        "entry_price_cents": round(entry_price, 3),
        "estimated_fee_cents": round(max(0.0, float(estimated_fee_cents or 0.0)), 3),
        "estimated_slippage_cents": round(max(0.0, float(estimated_slippage_cents or 0.0)), 3),
        "effective_price_cents": round(effective_price, 3),
        "full_kelly_fraction": round(full_kelly_fraction, 6),
        "configured_kelly_fraction": round(max(0.0, float(kelly_fraction or 0.0)), 4),
        "applied_bankroll_fraction": round(applied_kelly_fraction, 6),
        "kelly_stake": round(kelly_stake, 2),
        "raw_kelly_units": round(raw_kelly_units, 3),
        "kelly_units": kelly_units,
        "promotion_ready": promotion_ready,
        "walk_forward_promotion_required": bool(require_walk_forward_validation),
        "walk_forward_promotion_validated": walk_forward_validated,
        "walk_forward_profile_validated": walk_forward_profile_validated,
        "walk_forward_segment_source": segment_validation_source,
        "walk_forward_validation": walk_forward,
        "walk_forward_size_cap_units": walk_forward_size_cap,
        "promotion_cap_units": promotion_cap,
        "promotion_min_p_edge_positive": round(float(promotion_min_p_edge_positive or 0.0), 4),
        "p_edge_positive": round(p_edge_positive, 4),
        "longshot_cap_units": longshot_cap,
        "bankroll": round(bankroll, 2),
        "unit_size": round(unit_size, 2),
        "interval_weight": round(interval_weight, 4),
        "probability_distribution": distribution,
    }


def _row_prediction(row):
    pricing = row.get("pricing_v2") or {}
    try:
        book = float(pricing["book_probability"]) / 100.0
        market = float(pricing["kalshi_mid_probability"]) / 100.0
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in (book, market)):
        return None
    outcome = binary_outcome(row)
    if outcome is None:
        return None
    return book, market, outcome


def _calibration_row_eligible(row):
    """Keep external/capper strategy selection out of the scanner calibration."""
    return integrity_valid(row) and strategy_lane(row) in {"autonomous", "candidate_observation"}


def _event_group_key(row, index):
    return str(
        row.get("game_key")
        or row.get("event_id")
        or row.get("kalshi_event_ticker")
        or row.get("kalshi_ticker")
        or f"row-{index}"
    )


def _row_time(row):
    for field in ("generated_at", "placed_at", "settled_at", "commence_time"):
        parsed = parse_timestamp(row.get(field))
        if parsed is not None:
            return parsed
    return None


def _prediction_time(row):
    # Settlement/start times cannot stand in for when a prediction was made.
    return parse_timestamp(row.get("generated_at")) or parse_timestamp(row.get("placed_at"))


def _weighted_samples(rows, *, recency_half_life_days=60.0):
    raw_samples = [
        (row, sample, _event_group_key(row, index), _row_time(row))
        for index, row in enumerate(rows)
        if (sample := _row_prediction(row)) is not None
    ]
    group_counts = defaultdict(int)
    for _row, _sample, group, _timestamp in raw_samples:
        group_counts[group] += 1
    timestamps = [timestamp for _row, _sample, _group, timestamp in raw_samples if timestamp is not None]
    reference_time = max(timestamps) if timestamps else None
    half_life = max(0.0, float(recency_half_life_days or 0.0))
    samples = []
    for row, sample, group, timestamp in raw_samples:
        recency_weight = 1.0
        if reference_time is not None and timestamp is not None and half_life > 0:
            age_days = max(0.0, (reference_time - timestamp).total_seconds() / 86400.0)
            recency_weight = 0.5 ** (age_days / half_life)
        samples.append({
            "row": row,
            "book": sample[0],
            "market": sample[1],
            "outcome": sample[2],
            "group": group,
            "weight": recency_weight / max(1, group_counts[group]),
        })
    return samples, group_counts


def _solve_linear_3(matrix, vector):
    augmented = [
        [float(matrix[row][column]) for column in range(3)] + [float(vector[row])]
        for row in range(3)
    ]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-10:
            return [0.0, 0.0, 0.0]
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                augmented[row][index] - factor * augmented[column][index]
                for index in range(4)
            ]
    return [augmented[index][3] for index in range(3)]


def _probability_metrics(predictions):
    if not predictions:
        return {
            "count": 0,
            "brier": None,
            "log_loss": None,
            "calibration_bias_pp": None,
            "expected_calibration_error_pp": None,
        }
    total_weight = sum(float(row[2]) for row in predictions)
    if total_weight <= 0:
        return _probability_metrics([])
    brier = sum(weight * (probability - outcome) ** 2 for probability, outcome, weight in predictions) / total_weight
    log_loss = -sum(
        weight * (
            outcome * math.log(max(1e-9, probability))
            + (1.0 - outcome) * math.log(max(1e-9, 1.0 - probability))
        )
        for probability, outcome, weight in predictions
    ) / total_weight
    average_probability = sum(probability * weight for probability, _outcome, weight in predictions) / total_weight
    actual_rate = sum(outcome * weight for _probability, outcome, weight in predictions) / total_weight
    bins = defaultdict(list)
    for probability, outcome, weight in predictions:
        bins[min(9, int(probability * 10))].append((probability, outcome, weight))
    ece = 0.0
    for bucket in bins.values():
        bucket_weight = sum(weight for _probability, _outcome, weight in bucket)
        predicted = sum(probability * weight for probability, _outcome, weight in bucket) / bucket_weight
        actual = sum(outcome * weight for _probability, outcome, weight in bucket) / bucket_weight
        ece += bucket_weight / total_weight * abs(predicted - actual)
    return {
        "count": len(predictions),
        "brier": round(brier, 6),
        "log_loss": round(log_loss, 6),
        "calibration_bias_pp": round((average_probability - actual_rate) * 100.0, 3),
        "expected_calibration_error_pp": round(ece * 100.0, 3),
    }


def _fit_beta_calibration(
    rows,
    *,
    book_weight,
    prior_coefficients=(1.0, 1.0, 0.0),
    prior_strength=25.0,
    recency_half_life_days=60.0,
):
    samples, groups = _weighted_samples(rows, recency_half_life_days=recency_half_life_days)
    prior = [float(value) for value in prior_coefficients]
    if len(prior) != 3:
        prior = [1.0, 1.0, 0.0]
    coefficients = list(prior)
    regularization = max(0.01, float(prior_strength or 0.0))
    for _iteration in range(40):
        gradient = [-regularization * (coefficients[index] - prior[index]) for index in range(3)]
        information = [[0.0 for _column in range(3)] for _row in range(3)]
        for sample in samples:
            raw_probability = max(
                1e-6,
                min(
                    1.0 - 1e-6,
                    float(book_weight) * sample["book"] + (1.0 - float(book_weight)) * sample["market"],
                ),
            )
            features = [math.log(raw_probability), -math.log(1.0 - raw_probability), 1.0]
            prediction = apply_beta_calibration(raw_probability, coefficients)
            weight = float(sample["weight"])
            residual = sample["outcome"] - prediction
            for row in range(3):
                gradient[row] += weight * features[row] * residual
                for column in range(3):
                    information[row][column] += (
                        weight * prediction * (1.0 - prediction) * features[row] * features[column]
                    )
        for index in range(3):
            information[index][index] += regularization
        step = _solve_linear_3(information, gradient)
        coefficients = [coefficients[index] + step[index] for index in range(3)]
        if max(abs(value) for value in step) < 1e-7:
            break
    predictions = []
    raw_predictions = []
    for sample in samples:
        raw_probability = max(
            1e-6,
            min(1.0 - 1e-6, float(book_weight) * sample["book"] + (1.0 - float(book_weight)) * sample["market"]),
        )
        predictions.append((apply_beta_calibration(raw_probability, coefficients), sample["outcome"], sample["weight"]))
        raw_predictions.append((raw_probability, sample["outcome"], sample["weight"]))
    calibrated_metrics = _probability_metrics(predictions)
    raw_metrics = _probability_metrics(raw_predictions)
    return {
        "method": "hierarchical_beta",
        "coefficients": [round(value, 6) for value in coefficients],
        "prior_coefficients": [round(value, 6) for value in prior],
        "prior_strength": round(regularization, 3),
        "count": len(samples),
        "effective_event_count": len(groups),
        "brier": calibrated_metrics["brier"],
        "log_loss": calibrated_metrics["log_loss"],
        "calibration_bias_pp": calibrated_metrics["calibration_bias_pp"],
        "expected_calibration_error_pp": calibrated_metrics["expected_calibration_error_pp"],
        "uncalibrated_brier": raw_metrics["brier"],
        "uncalibrated_log_loss": raw_metrics["log_loss"],
        "recency_half_life_days": float(recency_half_life_days or 0.0),
    }


def _fit_weight(rows, default_weight, prior_strength, *, recency_half_life_days=60.0):
    weighted, group_counts = _weighted_samples(
        rows,
        recency_half_life_days=recency_half_life_days,
    )
    samples = [
        (row["book"], row["market"], row["outcome"], row["weight"])
        for row in weighted
    ]
    if not samples:
        return {
            "count": 0,
            "effective_event_count": 0,
            "book_weight": round(default_weight, 4),
            "raw_optimal_weight": round(default_weight, 4),
            "brier": None,
        }
    candidates = [index / 100.0 for index in range(101)]

    def score(weight):
        total_weight = sum(sample_weight for _book, _market, _outcome, sample_weight in samples)
        return sum(
            sample_weight * (weight * book + (1.0 - weight) * market - outcome) ** 2
            for book, market, outcome, sample_weight in samples
        ) / max(1e-12, total_weight)

    raw_weight = min(candidates, key=score)
    effective_events = len(group_counts)
    shrunk = (
        effective_events * raw_weight + max(0.0, prior_strength) * default_weight
    ) / (effective_events + max(0.0, prior_strength))
    weighted_total = sum(sample_weight for _book, _market, _outcome, sample_weight in samples)
    predicted_actual = [
        (shrunk * book + (1.0 - shrunk) * market, outcome, sample_weight)
        for book, market, outcome, sample_weight in samples
    ]
    average_prediction = sum(prediction * sample_weight for prediction, _outcome, sample_weight in predicted_actual) / weighted_total
    actual_rate = sum(outcome * sample_weight for _prediction, outcome, sample_weight in predicted_actual) / weighted_total
    bins = defaultdict(list)
    for prediction, outcome, sample_weight in predicted_actual:
        bins[min(9, int(prediction * 10))].append((prediction, outcome, sample_weight))
    ece = 0.0
    for bucket in bins.values():
        bucket_weight = sum(sample_weight for _prediction, _outcome, sample_weight in bucket)
        predicted = sum(prediction * sample_weight for prediction, _outcome, sample_weight in bucket) / bucket_weight
        actual = sum(outcome * sample_weight for _prediction, outcome, sample_weight in bucket) / bucket_weight
        ece += bucket_weight / weighted_total * abs(predicted - actual)
    return {
        "count": len(samples),
        "effective_event_count": effective_events,
        "book_weight": round(max(0.0, min(1.0, shrunk)), 4),
        "raw_optimal_weight": round(raw_weight, 4),
        "brier": round(score(shrunk), 6),
        "book_only_brier": round(score(1.0), 6),
        "kalshi_only_brier": round(score(0.0), 6),
        "calibration_bias_pp": round((average_prediction - actual_rate) * 100.0, 3),
        "expected_calibration_error_pp": round(ece * 100.0, 3),
        "systematic_error_pp": round(max(ece, abs(average_prediction - actual_rate)) * 100.0, 3),
        "recency_half_life_days": float(recency_half_life_days or 0.0),
    }


def _walk_forward_validation(
    rows,
    *,
    default_weight,
    prior_strength,
    recency_half_life_days,
    min_train_events=40,
    min_validation_events=20,
    max_ece_pp=10.0,
    min_brier_improvement=0.0,
    parent_populations=(),
    promotion_enabled=False,
):
    grouped = defaultdict(list)
    group_order = []
    for index, row in enumerate(rows or []):
        if _row_prediction(row) is None:
            continue
        key = _event_group_key(row, index)
        if key not in grouped:
            group_order.append(key)
        grouped[key].append(row)
    group_order.sort(
        key=lambda key: min(
            (_prediction_time(row) or datetime.min.replace(tzinfo=timezone.utc) for row in grouped[key]),
            default=datetime.min.replace(tzinfo=timezone.utc),
        )
    )
    train_count = max(int(min_train_events), int(math.ceil(len(group_order) * 0.60)))
    if len(group_order) < train_count + int(min_validation_events):
        return {
            "validation_version": VALIDATION_VERSION,
            "promotion_enabled": bool(promotion_enabled),
            "method": "expanding_event_walk_forward",
            "validated": False,
            "promotion_validated": False,
            "reason": "insufficient_walk_forward_events",
            "available_events": len(group_order),
            "required_train_events": int(min_train_events),
            "required_validation_events": int(min_validation_events),
            "validation_event_count": max(0, len(group_order) - train_count),
        }
    evaluation_groups = group_order[train_count:]
    batch_size = max(5, int(math.ceil(len(evaluation_groups) / 4.0)))
    calibrated_predictions = []
    market_predictions = []
    uncalibrated_predictions = []
    evaluated_groups = set()
    folds = []

    def available_rows(population, at):
        # A game's label is usable only once its settlement is known. Missing
        # historical availability remains useful for fitting, but not validation.
        grouped_population = defaultdict(list)
        for index, row in enumerate(population):
            grouped_population[_event_group_key(row, index)].append(row)
        result = []
        for group_rows in grouped_population.values():
            known = [parse_timestamp(row.get("settled_at") or row.get("resolved_at")) for row in group_rows]
            if known and all(value is not None and value < at for value in known):
                result.extend(row for row in group_rows if _prediction_time(row) is not None and _prediction_time(row) < at)
        return result

    for start in range(0, len(evaluation_groups), batch_size):
        test_groups = evaluation_groups[start:start + batch_size]
        cutoff = train_count + start
        test_rows = [row for key in test_groups for row in grouped[key]]
        times = [_prediction_time(row) for row in test_rows]
        if not times or any(value is None for value in times):
            continue
        as_of = min(times)
        train_rows = available_rows([row for key in group_order[:cutoff] for row in grouped[key]], as_of)
        train_events = {_event_group_key(row, index) for index, row in enumerate(train_rows)}
        if len(train_events) < int(min_train_events):
            continue
        fold_prior = float(default_weight)
        prior_coefficients = (1.0, 1.0, 0.0)
        for population in parent_populations:
            parent_rows = available_rows(population, as_of)
            parent_weight = _fit_weight(parent_rows, fold_prior, prior_strength, recency_half_life_days=recency_half_life_days)
            fold_prior = float(parent_weight.get("book_weight", fold_prior))
            parent_beta = _fit_beta_calibration(parent_rows, book_weight=fold_prior, prior_coefficients=prior_coefficients, prior_strength=max(5.0, float(prior_strength) / 2.0), recency_half_life_days=recency_half_life_days)
            prior_coefficients = parent_beta.get("coefficients") or prior_coefficients
        weight_fit = _fit_weight(
            train_rows,
            fold_prior,
            prior_strength,
            recency_half_life_days=recency_half_life_days,
        )
        book_weight = float(weight_fit.get("book_weight", default_weight))
        beta_fit = _fit_beta_calibration(
            train_rows,
            book_weight=book_weight,
            prior_coefficients=prior_coefficients,
            prior_strength=max(5.0, float(prior_strength) / 2.0),
            recency_half_life_days=recency_half_life_days,
        )
        coefficients = beta_fit.get("coefficients") or [1.0, 1.0, 0.0]
        folds.append({"as_of": as_of.isoformat(), "training_events": len(train_events), "test_events": len(test_groups), "book_weight": book_weight, "coefficients": list(coefficients)})
        evaluated_groups.update(test_groups)
        samples, _groups = _weighted_samples(test_rows, recency_half_life_days=0.0)
        for sample in samples:
            raw_probability = max(
                1e-6,
                min(1.0 - 1e-6, book_weight * sample["book"] + (1.0 - book_weight) * sample["market"]),
            )
            weight = float(sample["weight"])
            outcome = sample["outcome"]
            calibrated_predictions.append((apply_beta_calibration(raw_probability, coefficients), outcome, weight))
            uncalibrated_predictions.append((raw_probability, outcome, weight))
            market_predictions.append((sample["market"], outcome, weight))
    calibrated = _probability_metrics(calibrated_predictions)
    market = _probability_metrics(market_predictions)
    uncalibrated = _probability_metrics(uncalibrated_predictions)
    brier_improvement = (
        float(market["brier"]) - float(calibrated["brier"])
        if market.get("brier") is not None and calibrated.get("brier") is not None
        else None
    )
    log_loss_improvement = (
        float(market["log_loss"]) - float(calibrated["log_loss"])
        if market.get("log_loss") is not None and calibrated.get("log_loss") is not None
        else None
    )
    promotion_validated = bool(
        len(evaluated_groups) >= int(min_validation_events)
        and brier_improvement is not None
        and brier_improvement >= float(min_brier_improvement)
        and log_loss_improvement is not None
        and log_loss_improvement >= 0.0
        and calibrated.get("expected_calibration_error_pp") is not None
        and float(calibrated["expected_calibration_error_pp"]) <= float(max_ece_pp)
    )
    return {
        "method": "expanding_event_walk_forward",
        "validation_version": VALIDATION_VERSION,
        "promotion_enabled": bool(promotion_enabled),
        "activation_mode": "staged_active" if promotion_enabled else "shadow",
        "validated": len(evaluated_groups) >= int(min_validation_events),
        "promotion_validated": promotion_validated,
        "reason": "passed" if promotion_validated else "out_of_sample_quality_not_proven",
        "train_event_count": train_count,
        "validation_event_count": len(evaluated_groups),
        "folds": folds,
        "calibrated": calibrated,
        "uncalibrated_ensemble": uncalibrated,
        "kalshi_market": market,
        "brier_improvement_vs_kalshi": round(brier_improvement, 6) if brier_improvement is not None else None,
        "log_loss_improvement_vs_kalshi": round(log_loss_improvement, 6) if log_loss_improvement is not None else None,
        "maximum_ece_pp": float(max_ece_pp),
        "minimum_brier_improvement": float(min_brier_improvement),
    }


def _calibration_profile_fit(
    rows,
    *,
    default_weight,
    prior_coefficients,
    prior_strength,
    recency_half_life_days,
    walk_forward_min_train_events,
    walk_forward_min_validation_events,
    walk_forward_max_ece_pp,
    walk_forward_min_brier_improvement,
    validation_default_weight,
    validation_parent_populations=(),
    promotion_enabled=False,
):
    weight_fit = _fit_weight(
        rows,
        default_weight,
        prior_strength,
        recency_half_life_days=recency_half_life_days,
    )
    book_weight = float(weight_fit.get("book_weight", default_weight))
    beta_fit = _fit_beta_calibration(
        rows,
        book_weight=book_weight,
        prior_coefficients=prior_coefficients,
        prior_strength=max(5.0, float(prior_strength) / 2.0),
        recency_half_life_days=recency_half_life_days,
    )
    bias = float(beta_fit.get("calibration_bias_pp") or 0.0)
    ece = float(beta_fit.get("expected_calibration_error_pp") or 0.0)
    return {
        **weight_fit,
        "blend_brier": weight_fit.get("brier"),
        "brier": beta_fit.get("brier"),
        "log_loss": beta_fit.get("log_loss"),
        "calibration_bias_pp": round(bias, 3),
        "expected_calibration_error_pp": round(ece, 3),
        "systematic_error_pp": round(max(abs(bias), ece), 3),
        "beta_calibration": beta_fit,
        "walk_forward_validation": _walk_forward_validation(
            rows,
            default_weight=validation_default_weight,
            parent_populations=validation_parent_populations,
            promotion_enabled=promotion_enabled,
            prior_strength=prior_strength,
            recency_half_life_days=recency_half_life_days,
            min_train_events=walk_forward_min_train_events,
            min_validation_events=walk_forward_min_validation_events,
            max_ece_pp=walk_forward_max_ece_pp,
            min_brier_improvement=walk_forward_min_brier_improvement,
        ),
    }


def _row_price_band(row):
    pricing = row.get("pricing_v2") or {}
    value = pricing.get("kalshi_mid_probability")
    if value is None:
        value = row.get("entry_price")
    return probability_price_band(value)


def _row_settlement_bucket(row):
    return str(
        row.get("settlement_time_bucket")
        or (row.get("pricing_v2") or {}).get("settlement_time_bucket")
        or settlement_time_bucket(row.get("minutes_to_settlement"))
    )


def calibration_training_fingerprint(rows, options):
    """Hash every fitting/validation input, without copying large bet payloads."""
    digest = hashlib.sha256(json.dumps(
        {"cache_version": "semantic-calibration-cache-v1", "validation_version": VALIDATION_VERSION, "pricing_version": PRICING_VERSION, "options": options},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8"))
    # Use the same filtered row indices as build_calibration_state for events
    # without an identity; inserting an excluded/manual record is immaterial.
    eligible = [row for row in rows if _calibration_row_eligible(row) and _row_prediction(row) is not None]
    for index, row in enumerate(eligible):
        prediction_time = _prediction_time(row)
        recency_time = _row_time(row)
        availability_time = parse_timestamp(row.get("settled_at") or row.get("resolved_at"))
        payload = (
            _event_group_key(row, index), _row_prediction(row),
            normalize_text(row.get("sport_key")), normalize_text(row.get("market_type")),
            row.get("bet_timing_bucket") or row.get("timing_bucket"),
            _row_price_band(row), _row_settlement_bucket(row),
            prediction_time.isoformat() if prediction_time else None,
            recency_time.isoformat() if recency_time else None,
            availability_time.isoformat() if availability_time else None,
        )
        digest.update(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    return digest.hexdigest()


def build_calibration_state(
    rows,
    *,
    default_weight=0.25,
    prior_strength=50.0,
    generated_at=None,
    recency_half_life_days=60.0,
    walk_forward_min_train_events=40,
    walk_forward_min_validation_events=20,
    walk_forward_max_ece_pp=10.0,
    walk_forward_min_brier_improvement=0.0,
    promotion_enabled=False,
):
    settled = [
        row
        for row in rows or []
        if _calibration_row_eligible(row) and _row_prediction(row) is not None
    ]
    sports = defaultdict(list)
    sport_markets = defaultdict(list)
    grouped = defaultdict(list)
    conditional = defaultdict(list)
    for row in settled:
        sport = normalize_text(row.get("sport_key")) or "unknown"
        market = normalize_text(row.get("market_type")) or "unknown"
        timing = row.get("bet_timing_bucket") or row.get("timing_bucket")
        segment = segment_key(sport, market, timing)
        sports[sport].append(row)
        sport_markets[f"{sport}|{market}"].append(row)
        grouped[segment].append(row)
        conditional[
            conditional_segment_key(
                sport,
                market,
                timing,
                _row_price_band(row),
                _row_settlement_bucket(row),
            )
        ].append(row)
    fit_kwargs = {
        "validation_default_weight": float(default_weight),
        "promotion_enabled": bool(promotion_enabled),
        "prior_strength": float(prior_strength),
        "recency_half_life_days": float(recency_half_life_days),
        "walk_forward_min_train_events": int(walk_forward_min_train_events),
        "walk_forward_min_validation_events": int(walk_forward_min_validation_events),
        "walk_forward_max_ece_pp": float(walk_forward_max_ece_pp),
        "walk_forward_min_brier_improvement": float(walk_forward_min_brier_improvement),
    }
    global_fit = _calibration_profile_fit(
        settled,
        default_weight=float(default_weight),
        prior_coefficients=(1.0, 1.0, 0.0),
        **fit_kwargs,
    )
    learned_global_weight = float(global_fit.get("book_weight", default_weight))
    global_coefficients = (global_fit.get("beta_calibration") or {}).get("coefficients") or [1.0, 1.0, 0.0]
    sport_fits = {
        key: _calibration_profile_fit(
            values,
            default_weight=learned_global_weight,
            prior_coefficients=global_coefficients,
            validation_parent_populations=(settled,),
            **fit_kwargs,
        )
        for key, values in sorted(sports.items())
    }
    sport_market_fits = {}
    for key, values in sorted(sport_markets.items()):
        sport = key.split("|", 1)[0]
        parent = sport_fits.get(sport) or global_fit
        sport_market_fits[key] = _calibration_profile_fit(
            values,
            default_weight=float(parent.get("book_weight", learned_global_weight)),
            prior_coefficients=(parent.get("beta_calibration") or {}).get("coefficients") or global_coefficients,
            validation_parent_populations=(settled, sports[sport]),
            **fit_kwargs,
        )
    segment_fits = {}
    for key, values in sorted(grouped.items()):
        sport, market, _timing = key.split("|", 2)
        parent = sport_market_fits.get(f"{sport}|{market}") or sport_fits.get(sport) or global_fit
        segment_fits[key] = _calibration_profile_fit(
            values,
            default_weight=float(parent.get("book_weight", learned_global_weight)),
            prior_coefficients=(parent.get("beta_calibration") or {}).get("coefficients") or global_coefficients,
            validation_parent_populations=(settled, sports[sport], sport_markets[f"{sport}|{market}"]),
            **fit_kwargs,
        )
    conditional_fits = {}
    for key, values in sorted(conditional.items()):
        parent_key = "|".join(key.split("|")[:3])
        sport, market, _timing = parent_key.split("|", 2)
        parent = segment_fits.get(parent_key) or global_fit
        conditional_fits[key] = _calibration_profile_fit(
            values,
            default_weight=float(parent.get("book_weight", learned_global_weight)),
            prior_coefficients=(parent.get("beta_calibration") or {}).get("coefficients") or global_coefficients,
            validation_parent_populations=(settled, sports[sport], sport_markets[f"{sport}|{market}"], grouped[parent_key]),
            **fit_kwargs,
        )
    return {
        "version": PRICING_VERSION,
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "default_book_weight": round(float(default_weight), 4),
        "prior_strength": round(float(prior_strength), 2),
        "recency_half_life_days": round(float(recency_half_life_days), 2),
        "eligible_sources": ["edge_scanner", "bot_pick", "candidate_observation"],
        "excluded_strategy_lanes": ["trusted_capper", "sports_game_odds", "manual", "unattributed"],
        "global": global_fit,
        "sports": sport_fits,
        "sport_markets": sport_market_fits,
        "segments": segment_fits,
        "conditional_segments": conditional_fits,
        "profile_selection_min_events": {
            "global": 40,
            "sport": 30,
            "sport_market": 20,
            "segment": 10,
            "conditional": 20,
        },
    }
