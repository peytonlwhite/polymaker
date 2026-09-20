"""Versioned shadow comparison for the crypto directional confirmation veto.

The lane is observational only.  It records candidates that reached the final
directional gate, pairing strong-opposition rejections with normally confirmed
controls under the same strategy/configuration generation.  It has no order
placement surface and never promotes itself into live execution.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from crypto_pricing import kalshi_order_fee


VERSION = "crypto-directional-opposition-shadow-v2"
MODE = "paper_shadow_only"
ASSETS = ("BTC", "ETH", "SOL", "DOGE")
LANES = ("directional_opposition", "confirmation_control")
PAIRED_VERSION = "directional-opposition-contemporaneous-pair-v1"
PAIRED_EXECUTION_VERSION = "directional-pair-integer-contract-forward-v1"
ASSET_REPLICATION_VERSION = "directional-asset-replications-v1"
ONE_SIDED_90_Z = 1.2815515655446004
ONE_SIDED_975_Z = 1.959963984540054
CHICAGO = ZoneInfo("America/Chicago")


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _boolean(value, default=False):
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _observation_date(value):
    try:
        return _now(value).astimezone(CHICAGO).date().isoformat()
    except (TypeError, ValueError):
        return None


def configuration(settings=None):
    settings = settings or {}
    assets = tuple(
        asset.strip().upper()
        for asset in str(
            settings.get(
                "CRYPTO_DIRECTIONAL_OPPOSITION_V2_ASSETS", ",".join(ASSETS)
            )
        ).split(",")
        if asset.strip().upper() in ASSETS
    ) or ASSETS
    config = {
        "enabled": _boolean(
            settings.get("CRYPTO_DIRECTIONAL_OPPOSITION_V2_ENABLED", True), True
        ),
        "assets": list(assets),
        "virtual_stake_dollars": max(
            0.01,
            _number(
                settings.get(
                    "CRYPTO_DIRECTIONAL_OPPOSITION_V2_VIRTUAL_STAKE", 1.0
                ),
                1.0,
            ),
        ),
        "settlement_grace_minutes": max(
            0.0,
            _number(
                settings.get(
                    "CRYPTO_DIRECTIONAL_OPPOSITION_V2_SETTLEMENT_GRACE_MINUTES",
                    2,
                ),
                2,
            ),
        ),
        "settlement_retry_minutes": max(
            0.0,
            _number(
                settings.get(
                    "CRYPTO_DIRECTIONAL_OPPOSITION_V2_SETTLEMENT_RETRY_MINUTES",
                    5,
                ),
                5,
            ),
        ),
        "max_settlement_checks_per_scan": max(
            1,
            int(
                _number(
                    settings.get(
                        "CRYPTO_DIRECTIONAL_OPPOSITION_V2_MAX_SETTLEMENT_CHECKS_PER_SCAN",
                        20,
                    ),
                    20,
                )
            ),
        ),
        "max_records": max(
            100,
            int(
                _number(
                    settings.get("CRYPTO_DIRECTIONAL_OPPOSITION_V2_MAX_RECORDS", 5000),
                    5000,
                )
            ),
        ),
        "minimum_independent_markets": max(
            100,
            int(
                _number(
                    settings.get(
                        "CRYPTO_DIRECTIONAL_OPPOSITION_V2_MIN_INDEPENDENT_MARKETS",
                        200,
                    ),
                    200,
                )
            ),
        ),
        "minimum_data_quality": max(
            0.0,
            min(1.0, _number(settings.get("CRYPTO_DIRECTIONAL_OPPOSITION_V2_MIN_DATA_QUALITY", 0.90), 0.90)),
        ),
        "maximum_quote_age_seconds": max(
            0.1,
            _number(settings.get("CRYPTO_DIRECTIONAL_OPPOSITION_V2_MAX_QUOTE_AGE_SECONDS", 2), 2),
        ),
        "maximum_spread_cents": max(
            0.0,
            _number(settings.get("CRYPTO_DIRECTIONAL_OPPOSITION_V2_MAX_SPREAD_CENTS", 4), 4),
        ),
        "minimum_price_cents": max(
            1.0,
            _number(settings.get("CRYPTO_DIRECTIONAL_OPPOSITION_V2_MIN_PRICE_CENTS", 35), 35),
        ),
        "maximum_price_cents": min(
            99.0,
            _number(settings.get("CRYPTO_DIRECTIONAL_OPPOSITION_V2_MAX_PRICE_CENTS", 70), 70),
        ),
        "minimum_minutes": max(
            0.0,
            _number(settings.get("CRYPTO_DIRECTIONAL_OPPOSITION_V2_MIN_MINUTES", 2), 2),
        ),
        "maximum_minutes": max(
            0.0,
            _number(settings.get("CRYPTO_DIRECTIONAL_OPPOSITION_V2_MAX_MINUTES", 12), 12),
        ),
        "affects_execution": False,
        "automatic_promotion": False,
        "paired_forward": {
            "enabled": _boolean(
                settings.get("CRYPTO_DIRECTIONAL_PAIR_ENABLED", True), True
            ),
            "minimum_price_cents": max(
                1.0,
                _number(settings.get("CRYPTO_DIRECTIONAL_PAIR_MIN_PRICE_CENTS", 35), 35),
            ),
            "maximum_price_cents": min(
                99.0,
                _number(settings.get("CRYPTO_DIRECTIONAL_PAIR_MAX_PRICE_CENTS", 44), 44),
            ),
            "virtual_stake_dollars_per_arm": max(
                0.01,
                _number(settings.get("CRYPTO_DIRECTIONAL_PAIR_VIRTUAL_STAKE", 1), 1),
            ),
            "minimum_unique_markets": max(
                1,
                int(_number(settings.get("CRYPTO_DIRECTIONAL_PAIR_MIN_UNIQUE_MARKETS", 100), 100)),
            ),
            "minimum_expiry_windows": max(
                1,
                int(_number(settings.get("CRYPTO_DIRECTIONAL_PAIR_MIN_EXPIRY_WINDOWS", 50), 50)),
            ),
            "minimum_observation_days": max(
                1,
                int(
                    _number(
                        settings.get(
                            "CRYPTO_DIRECTIONAL_PAIR_MIN_OBSERVATION_DAYS", 30
                        ),
                        30,
                    )
                ),
            ),
            "confidence_level": 0.90,
            "affects_execution": False,
            "automatic_promotion": False,
        },
        "asset_replications": {
            "enabled": _boolean(
                settings.get("CRYPTO_DIRECTIONAL_REPLICATION_ENABLED", True), True
            ),
            "minimum_price_cents": max(
                1.0,
                _number(
                    settings.get("CRYPTO_DIRECTIONAL_REPLICATION_MIN_PRICE_CENTS", 35),
                    35,
                ),
            ),
            "maximum_price_cents": min(
                99.0,
                _number(
                    settings.get("CRYPTO_DIRECTIONAL_REPLICATION_MAX_PRICE_CENTS", 44),
                    44,
                ),
            ),
            "minimum_minutes": max(
                0.0,
                _number(
                    settings.get("CRYPTO_DIRECTIONAL_REPLICATION_MIN_MINUTES", 2),
                    2,
                ),
            ),
            "maximum_minutes": max(
                0.0,
                _number(
                    settings.get("CRYPTO_DIRECTIONAL_REPLICATION_MAX_MINUTES", 12),
                    12,
                ),
            ),
            "virtual_stake_dollars_per_arm": max(
                0.01,
                _number(
                    settings.get("CRYPTO_DIRECTIONAL_REPLICATION_VIRTUAL_STAKE", 1),
                    1,
                ),
            ),
            "minimum_unique_markets_per_hypothesis": max(
                1,
                int(
                    _number(
                        settings.get(
                            "CRYPTO_DIRECTIONAL_REPLICATION_MIN_UNIQUE_MARKETS", 100
                        ),
                        100,
                    )
                ),
            ),
            "minimum_observation_days": max(
                1,
                int(
                    _number(
                        settings.get(
                            "CRYPTO_DIRECTIONAL_REPLICATION_MIN_OBSERVATION_DAYS", 30
                        ),
                        30,
                    )
                ),
            ),
            "familywise_confidence_level": 0.95,
            "per_hypothesis_one_sided_confidence_level": 0.975,
            "hypotheses": {
                "doge_fade": {"asset": "DOGE", "primary_arm": "fade"},
                "eth_follow": {"asset": "ETH", "primary_arm": "follow"},
            },
            "affects_execution": False,
            "automatic_promotion": False,
        },
    }
    policy_fields = {
        key: config[key]
        for key in (
            "assets",
            "virtual_stake_dollars",
            "minimum_independent_markets",
            "minimum_data_quality",
            "maximum_quote_age_seconds",
            "maximum_spread_cents",
            "minimum_price_cents",
            "maximum_price_cents",
            "minimum_minutes",
            "maximum_minutes",
        )
    }
    config["policy_hash"] = hashlib.sha256(
        json.dumps(policy_fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    paired_policy = dict(config["paired_forward"])
    config["paired_forward"]["policy_hash"] = hashlib.sha256(
        json.dumps(paired_policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    replication_policy = dict(config["asset_replications"])
    config["asset_replications"]["policy_hash"] = hashlib.sha256(
        json.dumps(replication_policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return config


def empty_ledger(settings=None):
    current = _now().isoformat()
    return {
        "version": VERSION,
        "mode": MODE,
        "affects_execution": False,
        "automatic_promotion": False,
        "configuration": configuration(settings),
        "created_at": current,
        "updated_at": None,
        "records": [],
        "capture_funnel": {},
        "paired_experiment": {
            "version": PAIRED_VERSION,
            "registered_at": current,
            "records": [],
            "execution_revision": {
                "version": PAIRED_EXECUTION_VERSION,
                "registered_at": current,
            },
        },
        "asset_replication_experiment": {
            "version": ASSET_REPLICATION_VERSION,
            "registered_at": current,
            "records": [],
        },
    }


def normalize_ledger(value, settings=None):
    ledger = value if isinstance(value, dict) and value.get("version") == VERSION else None
    base = empty_ledger(settings)
    base.update(ledger or {})
    base["version"] = VERSION
    base["mode"] = MODE
    base["affects_execution"] = False
    base["automatic_promotion"] = False
    base["configuration"] = configuration(settings)
    base["records"] = list(base.get("records") or [])
    base["capture_funnel"] = dict(base.get("capture_funnel") or {})
    paired = dict(base.get("paired_experiment") or {})
    paired["version"] = PAIRED_VERSION
    paired["registered_at"] = paired.get("registered_at") or _now().isoformat()
    paired["records"] = list(paired.get("records") or [])
    execution_revision = dict(paired.get("execution_revision") or {})
    execution_revision["version"] = PAIRED_EXECUTION_VERSION
    execution_revision["registered_at"] = (
        execution_revision.get("registered_at") or _now().isoformat()
    )
    paired["execution_revision"] = execution_revision
    base["paired_experiment"] = paired
    replications = dict(base.get("asset_replication_experiment") or {})
    replications["version"] = ASSET_REPLICATION_VERSION
    replications["registered_at"] = replications.get("registered_at") or _now().isoformat()
    replications["records"] = list(replications.get("records") or [])
    base["asset_replication_experiment"] = replications
    if ledger is not None:
        ledger.clear()
        ledger.update(base)
        return ledger
    return base


def _paired_arm(side, price_cents, fee_schedule, stake):
    fee = kalshi_order_fee(
        price_cents,
        1,
        schedule=fee_schedule,
        liquidity_role="taker",
    )
    if not fee.get("exact"):
        return None
    fee_cents = 100.0 * _number(fee.get("fee_per_contract_dollars"))
    cost = (_number(price_cents) + fee_cents) / 100.0
    if cost <= 0:
        return None
    return {
        "side": side,
        "entry_price_cents": round(_number(price_cents), 6),
        "exact_fee_cents": round(fee_cents, 6),
        "fee_detail": fee,
        "virtual_stake": round(stake, 6),
        "virtual_contracts": round(stake / cost, 8),
        "result": None,
        "virtual_profit": None,
    }


def _integer_contract_arm(arm):
    price_cents = _number((arm or {}).get("entry_price_cents"))
    fee_cents = _number((arm or {}).get("exact_fee_cents"))
    return {
        "side": (arm or {}).get("side"),
        "contracts": 1,
        "entry_price_cents": round(price_cents, 6),
        "exact_fee_cents": round(fee_cents, 6),
        "total_cost_dollars": round((price_cents + fee_cents) / 100.0, 6),
        "result": None,
        "virtual_profit": None,
    }


def _paired_record(candidate, config, captured_at):
    paired_config = config["paired_forward"]
    if not paired_config["enabled"] or _lane(candidate) != "directional_opposition":
        return None, "not_paired_opposition"
    selected_side = str(candidate.get("side") or "").lower()
    selected_price = _number(candidate.get("entry_price"), -1.0)
    if not (
        paired_config["minimum_price_cents"]
        <= selected_price
        <= paired_config["maximum_price_cents"]
    ):
        return None, "paired_price_outside_registered_band"
    micro = candidate.get("kalshi_microstructure") or {}
    opposite_side = "no" if selected_side == "yes" else "yes"
    opposite_key = (
        "best_no_entry_price_cents"
        if opposite_side == "no"
        else "best_yes_entry_price_cents"
    )
    opposite_price = _number(micro.get(opposite_key), -1.0)
    if not 0 < opposite_price < 100:
        return None, "paired_opposite_executable_ask_unavailable"
    fee_schedule = candidate.get("fee_schedule") or {}
    stake = paired_config["virtual_stake_dollars_per_arm"]
    fade = _paired_arm(selected_side, selected_price, fee_schedule, stake)
    follow = _paired_arm(opposite_side, opposite_price, fee_schedule, stake)
    if not fade or not follow:
        return None, "paired_exact_fee_unavailable"
    review = dict(
        candidate.get("directional_shadow_review")
        or candidate.get("directional_confirmation")
        or {}
    )
    identity = dict(candidate.get("strategy_identity") or {})
    ticker = str(candidate.get("ticker") or "")
    if not ticker:
        return None, "paired_ticker_unavailable"
    row = {
        "id": f"{PAIRED_VERSION}:{ticker}",
        "version": PAIRED_VERSION,
        "policy_hash": paired_config["policy_hash"],
        "mode": MODE,
        "affects_execution": False,
        "automatic_promotion": False,
        "status": "open",
        "captured_at": captured_at,
        "ticker": ticker,
        "event_ticker": candidate.get("event_ticker"),
        "series_ticker": candidate.get("series_ticker"),
        "asset": str(candidate.get("asset") or "").upper(),
        "close_time": str(candidate.get("close_time") or ""),
        "expiry_key": str(candidate.get("close_time") or ""),
        "selected_price_band": _price_band(selected_price),
        "selected_no_diagnostic": selected_side == "no",
        "fade": fade,
        "follow": follow,
        "directional_confirmation": review,
        "flow_review": candidate.get("flow_review"),
        "data_quality": candidate.get("data_quality"),
        "shadow_quality": candidate.get("directional_shadow_quality"),
        "minutes_to_close": candidate.get("minutes_to_close"),
        "selected_side_probability": _selected_probability(candidate),
        "selected_side_probability_low": candidate.get("selected_side_probability_low"),
        "selected_side_probability_high": candidate.get("selected_side_probability_high"),
        "expected_edge": candidate.get("expected_edge"),
        "edge_low": candidate.get("edge_low"),
        "confidence": candidate.get("confidence"),
        "strategy_identity": identity,
        "strategy_version": candidate.get("strategy_version")
        or identity.get("strategy_version"),
        "strategy_config_hash": candidate.get("strategy_config_hash")
        or identity.get("config_hash"),
        "feature_schema_version": candidate.get("feature_schema_version")
        or identity.get("feature_schema_version"),
        "market_result": None,
        "profit_delta": None,
    }
    row["integer_contract_forward"] = {
        "version": PAIRED_EXECUTION_VERSION,
        "fade": _integer_contract_arm(fade),
        "follow": _integer_contract_arm(follow),
        "profit_delta": None,
    }
    return row, None


def _asset_replication_record(pair, config):
    replication_config = config["asset_replications"]
    if not replication_config["enabled"]:
        return None
    asset = str(pair.get("asset") or "").upper()
    hypothesis = None
    definition = None
    for name, value in replication_config["hypotheses"].items():
        if value.get("asset") == asset:
            hypothesis = name
            definition = value
            break
    if not hypothesis:
        return None
    fade_price = _number((pair.get("fade") or {}).get("entry_price_cents"))
    minutes = _number(pair.get("minutes_to_close"), -1.0)
    if not (
        replication_config["minimum_price_cents"]
        <= fade_price
        <= replication_config["maximum_price_cents"]
        and replication_config["minimum_minutes"]
        <= minutes
        <= replication_config["maximum_minutes"]
    ):
        return None
    primary_arm = definition["primary_arm"]
    comparator_arm = "follow" if primary_arm == "fade" else "fade"
    return {
        "id": f"{ASSET_REPLICATION_VERSION}:{hypothesis}:{pair.get('ticker')}",
        "version": ASSET_REPLICATION_VERSION,
        "policy_hash": replication_config["policy_hash"],
        "mode": MODE,
        "affects_execution": False,
        "automatic_promotion": False,
        "status": "open",
        "hypothesis": hypothesis,
        "primary_arm_name": primary_arm,
        "comparator_arm_name": comparator_arm,
        "captured_at": pair.get("captured_at"),
        "ticker": pair.get("ticker"),
        "asset": asset,
        "close_time": pair.get("close_time"),
        "expiry_key": pair.get("expiry_key"),
        "minutes_to_close": pair.get("minutes_to_close"),
        "fade": dict(pair.get("fade") or {}),
        "follow": dict(pair.get("follow") or {}),
        "integer_contract_forward": {
            "fade": dict(
                ((pair.get("integer_contract_forward") or {}).get("fade") or {})
            ),
            "follow": dict(
                ((pair.get("integer_contract_forward") or {}).get("follow") or {})
            ),
            "profit_delta": None,
        },
        "directional_confirmation": pair.get("directional_confirmation"),
        "data_quality": pair.get("data_quality"),
        "shadow_quality": pair.get("shadow_quality"),
        "strategy_version": pair.get("strategy_version"),
        "strategy_config_hash": pair.get("strategy_config_hash"),
        "market_result": None,
        "primary_profit": None,
        "comparator_profit": None,
        "profit_delta": None,
    }


def _lane(candidate):
    review = (
        candidate.get("directional_shadow_review")
        or candidate.get("directional_confirmation")
        or {}
    )
    if not review.get("enabled"):
        return None
    if review.get("ok"):
        return "confirmation_control"
    if review.get("reason") == "directional_opposition":
        return "directional_opposition"
    return None


def _quality(candidate):
    quality = candidate.get("data_quality") or {}
    score = _number(quality.get("score") if isinstance(quality, dict) else quality)
    return (
        _number(candidate.get("edge_low"), -999.0),
        _number(candidate.get("expected_edge"), -999.0),
        score,
        _number(candidate.get("confidence")),
    )


def _price_band(price):
    price = _number(price)
    if price < 40:
        return "35-39"
    if price < 45:
        return "40-44"
    if price < 55:
        return "45-54"
    if price < 65:
        return "55-64"
    return "65-70"


def _probability_band(probability):
    value = max(0.0, min(99.999, _number(probability)))
    lower = int(value // 10) * 10
    return f"{lower:02d}-{lower + 9:02d}"


def _selected_probability(candidate):
    value = candidate.get("selected_side_probability")
    if value is not None:
        return _number(value)
    yes_probability = _number(candidate.get("model_prob_yes"), 50.0)
    return yes_probability if candidate.get("side") == "yes" else 100.0 - yes_probability


def _shadow_quality_reasons(candidate, config):
    reasons = []
    quality = candidate.get("data_quality") or {}
    quality_score = _number(quality.get("score") if isinstance(quality, dict) else quality)
    micro = candidate.get("kalshi_microstructure") or {}
    quote_age = _number(
        micro.get("age_seconds")
        if micro.get("age_seconds") is not None
        else micro.get("orderbook_age_seconds"),
        999.0,
    )
    yes_bid = micro.get("best_yes_bid_cents")
    yes_ask = micro.get("best_yes_entry_price_cents")
    try:
        yes_bid = float(yes_bid)
        yes_ask = float(yes_ask)
    except (TypeError, ValueError):
        yes_bid = yes_ask = None
    spread = yes_ask - yes_bid if yes_bid is not None and yes_ask is not None else None
    price = _number(candidate.get("entry_price"))
    minutes = _number(candidate.get("minutes_to_close"), -1.0)
    if not candidate.get("fee_schedule_exact"):
        reasons.append("exact_fee_unavailable")
    if not candidate.get("strategy_version") or not candidate.get("strategy_config_hash"):
        reasons.append("current_strategy_identity_unavailable")
    if quality_score < config["minimum_data_quality"]:
        reasons.append("data_quality_below_shadow_floor")
    if quote_age > config["maximum_quote_age_seconds"]:
        reasons.append("quote_stale")
    if not micro.get("sequence_valid", True):
        reasons.append("orderbook_sequence_invalid")
    if micro.get("book_consistent") is False:
        reasons.append("orderbook_inconsistent")
    if yes_bid is None or yes_ask is None:
        reasons.append("executable_book_unavailable")
    elif yes_bid > yes_ask:
        reasons.append("orderbook_crossed")
    elif spread > config["maximum_spread_cents"]:
        reasons.append("spread_too_wide")
    if not config["minimum_price_cents"] <= price <= config["maximum_price_cents"]:
        reasons.append("unsupported_price")
    if not config["minimum_minutes"] <= minutes <= config["maximum_minutes"]:
        reasons.append("unsupported_time")
    if _lane(candidate) not in LANES:
        reasons.append("directional_lane_unavailable")
    return list(dict.fromkeys(reasons)), {
        "data_quality_score": quality_score,
        "quote_age_seconds": quote_age,
        "spread_cents": spread,
        "book_consistent": not any(
            reason in reasons
            for reason in (
                "orderbook_sequence_invalid",
                "orderbook_inconsistent",
                "executable_book_unavailable",
                "orderbook_crossed",
            )
        ),
    }


def capture_candidates(ledger, candidates, settings=None, now=None):
    ledger = normalize_ledger(ledger, settings)
    config = ledger["configuration"]
    if not config["enabled"]:
        return []
    captured_at = _now(now).isoformat()
    known = {str(row.get("id") or "") for row in ledger["records"]}
    paired_records = ledger["paired_experiment"]["records"]
    known_paired = {str(row.get("id") or "") for row in paired_records}
    replication_records = ledger["asset_replication_experiment"]["records"]
    known_replications = {
        str(row.get("id") or "") for row in replication_records
    }
    best = {}
    paired_best = {}
    funnel = Counter()
    paired_funnel = Counter()
    for candidate in candidates or []:
        funnel["reviewed"] += 1
        asset = str(candidate.get("asset") or "").upper()
        lane = _lane(candidate)
        close_time = str(candidate.get("close_time") or "")
        side = str(candidate.get("side") or "").lower()
        identity = candidate.get("strategy_identity") or {}
        strategy_version = str(
            candidate.get("strategy_version") or identity.get("strategy_version") or ""
        )
        config_hash = str(
            candidate.get("strategy_config_hash") or identity.get("config_hash") or ""
        )
        reasons = []
        if candidate.get("market_lane") != "crypto_15m":
            reasons.append("not_crypto_15m")
        if asset not in config["assets"]:
            reasons.append("unsupported_asset")
        if not close_time:
            reasons.append("close_time_unavailable")
        if side not in {"yes", "no"}:
            reasons.append("selected_side_unavailable")
        quality_reasons, shadow_quality = _shadow_quality_reasons(candidate, config)
        reasons.extend(quality_reasons)
        if reasons:
            funnel["rejected"] += 1
            for reason in dict.fromkeys(reasons):
                funnel[f"reason:{reason}"] += 1
            continue
        price = _number(candidate.get("entry_price"))
        fee_cents = _number(candidate.get("exact_fee_cents"), -1.0)
        if not 0 < price < 100 or fee_cents < 0:
            funnel["rejected"] += 1
            funnel["reason:pricing_unavailable"] += 1
            continue
        candidate = dict(candidate)
        candidate["directional_shadow_quality"] = shadow_quality
        funnel["eligible"] += 1
        key = (asset, close_time, lane)
        if key not in best or _quality(candidate) > _quality(best[key]):
            best[key] = candidate
        if lane == "directional_opposition":
            paired_funnel["reviewed_opposition"] += 1
            ticker = str(candidate.get("ticker") or "")
            if ticker and (
                ticker not in paired_best
                or _quality(candidate) > _quality(paired_best[ticker])
            ):
                paired_best[ticker] = candidate

    added = []
    for (asset, close_time, lane), candidate in sorted(best.items()):
        record_id = f"{VERSION}:{asset}:{close_time}:{lane}"
        if record_id in known:
            continue
        price = _number(candidate.get("entry_price"))
        fee_cents = _number(candidate.get("exact_fee_cents"))
        stake = config["virtual_stake_dollars"]
        all_in_cost_per_contract = (price + fee_cents) / 100.0
        contracts = stake / all_in_cost_per_contract
        review = dict(
            candidate.get("directional_shadow_review")
            or candidate.get("directional_confirmation")
            or {}
        )
        identity = dict(candidate.get("strategy_identity") or {})
        price_band = _price_band(price)
        probability_band = _probability_band(
            _selected_probability(candidate)
        )
        expiry_key = str(close_time)
        row = {
            "id": record_id,
            "version": VERSION,
            "policy_hash": config["policy_hash"],
            "mode": MODE,
            "affects_execution": False,
            "automatic_promotion": False,
            "status": "open",
            "lane": lane,
            "captured_at": captured_at,
            "ticker": candidate.get("ticker"),
            "event_ticker": candidate.get("event_ticker"),
            "series_ticker": candidate.get("series_ticker"),
            "asset": asset,
            "close_time": close_time,
            "side": str(candidate.get("side") or "").lower(),
            "entry_price_cents": round(price, 6),
            "exact_fee_cents": round(fee_cents, 6),
            "fee_schedule": candidate.get("fee_schedule"),
            "fee_detail": candidate.get("fee_detail"),
            "virtual_stake": round(stake, 6),
            "virtual_contracts": round(contracts, 8),
            "edge": candidate.get("edge"),
            "expected_edge": candidate.get("expected_edge"),
            "edge_low": candidate.get("edge_low"),
            "confidence": candidate.get("confidence"),
            "selected_side_probability": _selected_probability(candidate),
            "selected_side_probability_low": candidate.get(
                "selected_side_probability_low"
            ),
            "selected_side_probability_high": candidate.get(
                "selected_side_probability_high"
            ),
            "probability_net_edge_positive": candidate.get(
                "probability_net_edge_positive"
            ),
            "data_quality": candidate.get("data_quality"),
            "minutes_to_close": candidate.get("minutes_to_close"),
            "price_band": price_band,
            "probability_band": probability_band,
            "expiry_key": expiry_key,
            "comparison_stratum": "|".join((
                asset,
                expiry_key,
                price_band,
                probability_band,
                str(candidate.get("strategy_version") or identity.get("strategy_version") or ""),
                str(candidate.get("strategy_config_hash") or identity.get("config_hash") or ""),
            )),
            "shadow_quality": candidate.get("directional_shadow_quality"),
            "directional_confirmation": review,
            "flow_review": candidate.get("flow_review"),
            "baseline_skip_reasons": [
                reason
                for reason in (candidate.get("skip_reasons") or [])
                if reason != "directional_opposition"
            ],
            "strategy_identity": identity,
            "strategy_version": candidate.get("strategy_version")
            or identity.get("strategy_version"),
            "strategy_config_hash": candidate.get("strategy_config_hash")
            or identity.get("config_hash"),
            "feature_schema_version": candidate.get("feature_schema_version")
            or identity.get("feature_schema_version"),
            "result": None,
            "market_result": None,
            "virtual_profit": None,
        }
        ledger["records"].append(row)
        known.add(record_id)
        added.append(row)
    paired_added = []
    replication_added = []
    for _ticker, candidate in sorted(paired_best.items()):
        paired_row, paired_reason = _paired_record(candidate, config, captured_at)
        if paired_row is None:
            paired_funnel["rejected"] += 1
            paired_funnel[f"reason:{paired_reason}"] += 1
            continue
        if paired_row["id"] in known_paired:
            paired_funnel["already_tracked"] += 1
            continue
        paired_records.append(paired_row)
        known_paired.add(paired_row["id"])
        paired_added.append(paired_row)
        paired_funnel["captured"] += 1
        replication_row = _asset_replication_record(paired_row, config)
        if (
            replication_row is not None
            and replication_row["id"] not in known_replications
        ):
            replication_records.append(replication_row)
            known_replications.add(replication_row["id"])
            replication_added.append(replication_row)
    if len(ledger["records"]) > config["max_records"]:
        open_rows = [row for row in ledger["records"] if row.get("status") == "open"]
        closed_rows = [row for row in ledger["records"] if row.get("status") != "open"]
        keep = max(0, config["max_records"] - len(open_rows))
        ledger["records"] = (closed_rows[-keep:] if keep else []) + open_rows
    if len(paired_records) > config["max_records"]:
        open_rows = [row for row in paired_records if row.get("status") == "open"]
        closed_rows = [row for row in paired_records if row.get("status") != "open"]
        keep = max(0, config["max_records"] - len(open_rows))
        ledger["paired_experiment"]["records"] = (
            (closed_rows[-keep:] if keep else []) + open_rows
        )
    if len(replication_records) > config["max_records"]:
        open_rows = [
            row for row in replication_records if row.get("status") == "open"
        ]
        closed_rows = [
            row for row in replication_records if row.get("status") != "open"
        ]
        keep = max(0, config["max_records"] - len(open_rows))
        ledger["asset_replication_experiment"]["records"] = (
            (closed_rows[-keep:] if keep else []) + open_rows
        )
    totals = Counter(ledger.get("capture_funnel") or {})
    totals.update(funnel)
    ledger["capture_funnel"] = dict(totals)
    ledger["last_capture_funnel"] = dict(funnel)
    paired_totals = Counter(ledger.get("paired_capture_funnel") or {})
    paired_totals.update(paired_funnel)
    ledger["paired_capture_funnel"] = dict(paired_totals)
    ledger["last_paired_capture_funnel"] = dict(paired_funnel)
    ledger["last_paired_capture_ids"] = [row["id"] for row in paired_added]
    ledger["last_replication_capture_ids"] = [
        row["id"] for row in replication_added
    ]
    ledger["updated_at"] = captured_at
    return added


def _settle_comparison_arms(row, result):
    for arm_name in ("fade", "follow"):
        arm = row[arm_name]
        won = arm.get("side") == result
        payout = _number(arm.get("virtual_contracts")) if won else 0.0
        profit = payout - _number(arm.get("virtual_stake"))
        arm.update(
            {
                "result": "WIN" if won else "LOSS",
                "won": won,
                "virtual_payout": round(payout, 6),
                "virtual_profit": round(profit, 6),
            }
        )
    row["profit_delta"] = round(
        _number(row["fade"].get("virtual_profit"))
        - _number(row["follow"].get("virtual_profit")),
        6,
    )
    integer = row.get("integer_contract_forward") or {}
    if integer.get("fade") and integer.get("follow"):
        for arm_name in ("fade", "follow"):
            arm = integer[arm_name]
            won = arm.get("side") == result
            payout = _number(arm.get("contracts")) if won else 0.0
            arm.update(
                {
                    "result": "WIN" if won else "LOSS",
                    "won": won,
                    "virtual_payout": round(payout, 6),
                    "virtual_profit": round(
                        payout - _number(arm.get("total_cost_dollars")), 6
                    ),
                }
            )
        integer["profit_delta"] = round(
            _number(integer["fade"].get("virtual_profit"))
            - _number(integer["follow"].get("virtual_profit")),
            6,
        )
        row["integer_contract_forward"] = integer


def settle_records(ledger, fetch_market, settings=None, now=None):
    ledger = normalize_ledger(ledger, settings)
    config = ledger["configuration"]
    current = _now(now)
    settled = []
    payload_cache = {}
    checks = 0
    for row in ledger["records"]:
        if row.get("status") != "open" or checks >= config["max_settlement_checks_per_scan"]:
            continue
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
        checks += 1
        row["last_settlement_check_at"] = current.isoformat()
        ticker = str(row.get("ticker") or "")
        if ticker not in payload_cache:
            try:
                payload_cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                payload_cache[ticker] = {"error": f"{type(exc).__name__}: {exc}"}
        market = payload_cache[ticker]
        status = str(market.get("status") or "").lower()
        result = str(market.get("result") or market.get("market_result") or "").lower()
        if status != "finalized" or result not in {"yes", "no"}:
            continue
        won = row.get("side") == result
        payout = _number(row.get("virtual_contracts")) if won else 0.0
        profit = payout - _number(row.get("virtual_stake"))
        probability = _number(row.get("selected_side_probability")) / 100.0
        row.update(
            {
                "status": "settled",
                "settled_at": market.get("settlement_ts")
                or market.get("settled_time")
                or current.isoformat(),
                "settlement_source": "kalshi_finalized_market",
                "market_result": result,
                "result": "WIN" if won else "LOSS",
                "won": won,
                "virtual_payout": round(payout, 6),
                "virtual_profit": round(profit, 6),
                "brier_score": round(
                    (probability - (1.0 if won else 0.0)) ** 2, 8
                ),
            }
        )
        settled.append(row)
    paired_settled = []
    paired_checks = 0
    for row in ledger["paired_experiment"]["records"]:
        if (
            row.get("status") != "open"
            or paired_checks >= config["max_settlement_checks_per_scan"]
        ):
            continue
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
        paired_checks += 1
        row["last_settlement_check_at"] = current.isoformat()
        ticker = str(row.get("ticker") or "")
        if ticker not in payload_cache:
            try:
                payload_cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                payload_cache[ticker] = {"error": f"{type(exc).__name__}: {exc}"}
        market = payload_cache[ticker]
        status = str(market.get("status") or "").lower()
        result = str(market.get("result") or market.get("market_result") or "").lower()
        if status != "finalized" or result not in {"yes", "no"}:
            continue
        _settle_comparison_arms(row, result)
        row.update(
            {
                "status": "settled",
                "settled_at": market.get("settlement_ts")
                or market.get("settled_time")
                or current.isoformat(),
                "settlement_source": "kalshi_finalized_market",
                "market_result": result,
            }
        )
        paired_settled.append(row)
    ledger["last_paired_settled_ids"] = [row["id"] for row in paired_settled]
    replication_settled = []
    replication_checks = 0
    for row in ledger["asset_replication_experiment"]["records"]:
        if (
            row.get("status") != "open"
            or replication_checks >= config["max_settlement_checks_per_scan"]
        ):
            continue
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
        replication_checks += 1
        row["last_settlement_check_at"] = current.isoformat()
        ticker = str(row.get("ticker") or "")
        if ticker not in payload_cache:
            try:
                payload_cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                payload_cache[ticker] = {"error": f"{type(exc).__name__}: {exc}"}
        market = payload_cache[ticker]
        status = str(market.get("status") or "").lower()
        result = str(market.get("result") or market.get("market_result") or "").lower()
        if status != "finalized" or result not in {"yes", "no"}:
            continue
        _settle_comparison_arms(row, result)
        primary_name = row["primary_arm_name"]
        comparator_name = row["comparator_arm_name"]
        row.update(
            {
                "status": "settled",
                "settled_at": market.get("settlement_ts")
                or market.get("settled_time")
                or current.isoformat(),
                "settlement_source": "kalshi_finalized_market",
                "market_result": result,
                "primary_profit": row[primary_name].get("virtual_profit"),
                "comparator_profit": row[comparator_name].get("virtual_profit"),
                "profit_delta": round(
                    _number(row[primary_name].get("virtual_profit"))
                    - _number(row[comparator_name].get("virtual_profit")),
                    6,
                ),
            }
        )
        replication_settled.append(row)
    ledger["last_replication_settled_ids"] = [
        row["id"] for row in replication_settled
    ]
    ledger["updated_at"] = current.isoformat()
    return settled


def _row_summary(rows):
    settled = [row for row in rows if row.get("status") == "settled"]
    stake = sum(_number(row.get("virtual_stake")) for row in settled)
    profit = sum(_number(row.get("virtual_profit")) for row in settled)
    wins = sum(row.get("result") == "WIN" for row in settled)
    return {
        "tracked": len(rows),
        "open": sum(row.get("status") == "open" for row in rows),
        "settled": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": round(100.0 * wins / len(settled), 2) if settled else 0.0,
        "virtual_profit": round(profit, 4),
        "roi": round(100.0 * profit / stake, 2) if stake else 0.0,
        "average_brier": round(
            sum(_number(row.get("brier_score")) for row in settled) / len(settled), 6
        ) if settled else None,
    }


def _paired_arm_summary(rows, arm_name):
    settled = [row for row in rows if row.get("status") == "settled"]
    stake = sum(_number((row.get(arm_name) or {}).get("virtual_stake")) for row in settled)
    profit = sum(_number((row.get(arm_name) or {}).get("virtual_profit")) for row in settled)
    wins = sum((row.get(arm_name) or {}).get("result") == "WIN" for row in settled)
    return {
        "settled": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": round(100.0 * wins / len(settled), 2) if settled else 0.0,
        "virtual_stake": round(stake, 4),
        "virtual_profit": round(profit, 4),
        "roi": round(100.0 * profit / stake, 2) if stake else 0.0,
        "mean_profit_per_market": round(profit / len(settled), 6) if settled else None,
    }


def _cluster_lower_bound(
    rows,
    value_getter,
    *,
    cluster_getter=None,
    z_value=ONE_SIDED_90_Z,
    confidence_level=0.90,
):
    settled = [row for row in rows if row.get("status") == "settled"]
    values = [_number(value_getter(row)) for row in settled]
    clusters = {}
    for row, value in zip(settled, values):
        key = (
            cluster_getter(row)
            if cluster_getter
            else str(row.get("expiry_key") or row.get("close_time") or "")
        )
        if key:
            clusters.setdefault(str(key), []).append(value)
    cluster_count = len(clusters)
    if not values or cluster_count < 2:
        return {
            "point_estimate": round(sum(values) / len(values), 6) if values else None,
            "standard_error": None,
            "one_sided_90_lower": None,
            "lower_bound": None,
            "confidence_level": confidence_level,
            "clusters": cluster_count,
        }
    mean = sum(values) / len(values)
    cluster_scores = [sum(value - mean for value in cluster) for cluster in clusters.values()]
    variance = (cluster_count / (cluster_count - 1.0)) * sum(
        score * score for score in cluster_scores
    ) / (len(values) ** 2)
    standard_error = math.sqrt(max(0.0, variance))
    lower = round(mean - z_value * standard_error, 6)
    return {
        "point_estimate": round(mean, 6),
        "standard_error": round(standard_error, 6),
        "one_sided_90_lower": lower if confidence_level == 0.90 else None,
        "lower_bound": lower,
        "confidence_level": confidence_level,
        "clusters": cluster_count,
    }


def _integer_arm_summary(rows, arm_name):
    settled = [
        row
        for row in rows
        if row.get("status") == "settled"
        and ((row.get("integer_contract_forward") or {}).get(arm_name) or {}).get(
            "virtual_profit"
        )
        is not None
    ]
    arms = [
        (row.get("integer_contract_forward") or {}).get(arm_name) or {}
        for row in settled
    ]
    cost = sum(_number(arm.get("total_cost_dollars")) for arm in arms)
    profit = sum(_number(arm.get("virtual_profit")) for arm in arms)
    wins = sum(arm.get("result") == "WIN" for arm in arms)
    return {
        "settled": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "virtual_cost": round(cost, 4),
        "virtual_profit": round(profit, 4),
        "roi": round(100.0 * profit / cost, 2) if cost else 0.0,
        "mean_profit_per_market": round(profit / len(settled), 6)
        if settled
        else None,
    }


def _paired_forward_summary(ledger, config):
    experiment = ledger["paired_experiment"]
    records = list(experiment.get("records") or [])
    settled = sorted(
        [row for row in records if row.get("status") == "settled"],
        key=lambda row: str(row.get("captured_at") or ""),
    )
    split = len(settled) // 2
    fade = _paired_arm_summary(settled, "fade")
    follow = _paired_arm_summary(settled, "follow")
    fade["chronological_halves"] = {
        "first": _paired_arm_summary(settled[:split], "fade"),
        "second": _paired_arm_summary(settled[split:], "fade"),
    }
    follow["chronological_halves"] = {
        "first": _paired_arm_summary(settled[:split], "follow"),
        "second": _paired_arm_summary(settled[split:], "follow"),
    }
    fade_lower = _cluster_lower_bound(
        settled, lambda row: (row.get("fade") or {}).get("virtual_profit")
    )
    delta_lower = _cluster_lower_bound(settled, lambda row: row.get("profit_delta"))
    unique_markets = len({str(row.get("ticker") or "") for row in settled})
    expiry_windows = len(
        {str(row.get("expiry_key") or row.get("close_time") or "") for row in settled}
    )
    observation_days = len(
        {
            _observation_date(row.get("captured_at") or row.get("close_time"))
            for row in settled
            if _observation_date(row.get("captured_at") or row.get("close_time"))
        }
    )
    fade_day_lower = _cluster_lower_bound(
        settled,
        lambda row: (row.get("fade") or {}).get("virtual_profit"),
        cluster_getter=lambda row: _observation_date(
            row.get("captured_at") or row.get("close_time")
        ),
    )
    delta_day_lower = _cluster_lower_bound(
        settled,
        lambda row: row.get("profit_delta"),
        cluster_getter=lambda row: _observation_date(
            row.get("captured_at") or row.get("close_time")
        ),
    )
    integer_all = [
        row
        for row in records
        if (row.get("integer_contract_forward") or {}).get("version")
        == PAIRED_EXECUTION_VERSION
    ]
    integer_rows = [
        row
        for row in integer_all
        if row.get("status") == "settled"
    ]
    integer_fade = _integer_arm_summary(integer_rows, "fade")
    integer_follow = _integer_arm_summary(integer_rows, "follow")
    integer_delta_lower = _cluster_lower_bound(
        integer_rows,
        lambda row: (row.get("integer_contract_forward") or {}).get(
            "profit_delta"
        ),
        cluster_getter=lambda row: _observation_date(
            row.get("captured_at") or row.get("close_time")
        ),
    )
    paired_config = config["paired_forward"]
    first_half_positive = fade["chronological_halves"]["first"]["virtual_profit"] > 0
    second_half_positive = fade["chronological_halves"]["second"]["virtual_profit"] > 0
    gates = {
        "minimum_unique_markets": unique_markets
        >= paired_config["minimum_unique_markets"],
        "minimum_expiry_windows": expiry_windows
        >= paired_config["minimum_expiry_windows"],
        "minimum_observation_days": observation_days
        >= paired_config["minimum_observation_days"],
        "fade_profitable": fade["virtual_profit"] > 0,
        "fade_beats_follow": fade["virtual_profit"] > follow["virtual_profit"],
        "fade_cluster_lower_positive": fade_lower["one_sided_90_lower"] is not None
        and fade_lower["one_sided_90_lower"] > 0,
        "delta_cluster_lower_positive": delta_lower["one_sided_90_lower"] is not None
        and delta_lower["one_sided_90_lower"] > 0,
        "fade_day_cluster_lower_positive": fade_day_lower["lower_bound"] is not None
        and fade_day_lower["lower_bound"] > 0,
        "delta_day_cluster_lower_positive": delta_day_lower["lower_bound"] is not None
        and delta_day_lower["lower_bound"] > 0,
        "both_chronological_halves_profitable": first_half_positive
        and second_half_positive,
        "minimum_integer_contract_forward_markets": len(integer_rows)
        >= paired_config["minimum_unique_markets"],
        "integer_contract_fade_profitable": integer_fade["virtual_profit"] > 0,
        "integer_contract_delta_positive": integer_fade["virtual_profit"]
        > integer_follow["virtual_profit"],
        "integer_contract_day_lower_positive": integer_delta_lower["lower_bound"]
        is not None
        and integer_delta_lower["lower_bound"] > 0,
    }
    by_asset = []
    for asset in config["assets"]:
        rows = [row for row in settled if row.get("asset") == asset]
        by_asset.append(
            {
                "asset": asset,
                "fade": _paired_arm_summary(rows, "fade"),
                "follow": _paired_arm_summary(rows, "follow"),
                "profit_delta": round(
                    sum(_number(row.get("profit_delta")) for row in rows), 4
                ),
            }
        )
    selected_no = [row for row in settled if row.get("selected_no_diagnostic")]
    return {
        "version": PAIRED_VERSION,
        "mode": MODE if paired_config["enabled"] else "disabled",
        "registered_at": experiment.get("registered_at"),
        "affects_execution": False,
        "automatic_promotion": False,
        "definition": {
            "selected_entry_price_cents": [
                paired_config["minimum_price_cents"],
                paired_config["maximum_price_cents"],
            ],
            "virtual_stake_dollars_per_arm": paired_config[
                "virtual_stake_dollars_per_arm"
            ],
            "comparison": "same_ticker_same_snapshot_fade_vs_follow",
            "settlement": "kalshi_finalized_market_only",
            "minimum_unique_markets": paired_config["minimum_unique_markets"],
            "minimum_expiry_windows": paired_config["minimum_expiry_windows"],
            "minimum_observation_days": paired_config["minimum_observation_days"],
            "confidence_level": paired_config["confidence_level"],
            "policy_hash": paired_config["policy_hash"],
        },
        "tracked": len(records),
        "open": sum(row.get("status") == "open" for row in records),
        "settled": len(settled),
        "unique_settled_markets": unique_markets,
        "independent_expiry_windows": expiry_windows,
        "observation_days": observation_days,
        "fade": fade,
        "follow": follow,
        "paired_profit_delta": round(
            fade["virtual_profit"] - follow["virtual_profit"], 4
        ),
        "fade_cluster_inference": fade_lower,
        "delta_cluster_inference": delta_lower,
        "fade_day_cluster_inference": fade_day_lower,
        "delta_day_cluster_inference": delta_day_lower,
        "integer_contract_forward": {
            "version": PAIRED_EXECUTION_VERSION,
            "registered_at": (experiment.get("execution_revision") or {}).get(
                "registered_at"
            ),
            "tracked": len(integer_all),
            "open": sum(row.get("status") == "open" for row in integer_all),
            "settled": len(integer_rows),
            "fade": integer_fade,
            "follow": integer_follow,
            "profit_delta": round(
                integer_fade["virtual_profit"]
                - integer_follow["virtual_profit"],
                4,
            ),
            "delta_day_cluster_inference": integer_delta_lower,
            "historical_backfill": False,
        },
        "evaluation": {
            "gates": gates,
            "ready_for_manual_review": bool(gates) and all(gates.values()),
            "automatic_promotion": False,
        },
        "by_asset": by_asset,
        "exploratory_selected_no": {
            "post_hoc_diagnostic_only": True,
            "fade": _paired_arm_summary(selected_no, "fade"),
            "follow": _paired_arm_summary(selected_no, "follow"),
        },
        "capture_funnel": dict(ledger.get("paired_capture_funnel") or {}),
        "last_capture_funnel": dict(ledger.get("last_paired_capture_funnel") or {}),
        "recent_records": sorted(
            records,
            key=lambda row: str(row.get("settled_at") or row.get("captured_at") or ""),
            reverse=True,
        )[:100],
    }


def _asset_replication_summary(ledger, config):
    experiment = ledger["asset_replication_experiment"]
    records = list(experiment.get("records") or [])
    replication_config = config["asset_replications"]
    hypotheses = []
    for name, definition in replication_config["hypotheses"].items():
        rows = sorted(
            [row for row in records if row.get("hypothesis") == name],
            key=lambda row: str(row.get("captured_at") or ""),
        )
        settled = [row for row in rows if row.get("status") == "settled"]
        primary_name = definition["primary_arm"]
        comparator_name = "follow" if primary_name == "fade" else "fade"
        primary = _paired_arm_summary(settled, primary_name)
        comparator = _paired_arm_summary(settled, comparator_name)
        split = len(settled) // 2
        primary["chronological_halves"] = {
            "first": _paired_arm_summary(settled[:split], primary_name),
            "second": _paired_arm_summary(settled[split:], primary_name),
        }
        days = {
            _observation_date(row.get("captured_at") or row.get("close_time"))
            for row in settled
            if _observation_date(row.get("captured_at") or row.get("close_time"))
        }
        expiry_lower = _cluster_lower_bound(
            settled,
            lambda row: row.get("primary_profit"),
            z_value=ONE_SIDED_975_Z,
            confidence_level=0.975,
        )
        day_lower = _cluster_lower_bound(
            settled,
            lambda row: row.get("primary_profit"),
            cluster_getter=lambda row: _observation_date(
                row.get("captured_at") or row.get("close_time")
            ),
            z_value=ONE_SIDED_975_Z,
            confidence_level=0.975,
        )
        delta_day_lower = _cluster_lower_bound(
            settled,
            lambda row: row.get("profit_delta"),
            cluster_getter=lambda row: _observation_date(
                row.get("captured_at") or row.get("close_time")
            ),
            z_value=ONE_SIDED_975_Z,
            confidence_level=0.975,
        )
        integer_primary = _integer_arm_summary(settled, primary_name)
        integer_comparator = _integer_arm_summary(settled, comparator_name)
        integer_day_lower = _cluster_lower_bound(
            settled,
            lambda row: _number(
                (
                    (row.get("integer_contract_forward") or {}).get(primary_name)
                    or {}
                ).get("virtual_profit")
            )
            - _number(
                (
                    (row.get("integer_contract_forward") or {}).get(
                        comparator_name
                    )
                    or {}
                ).get("virtual_profit")
            ),
            cluster_getter=lambda row: _observation_date(
                row.get("captured_at") or row.get("close_time")
            ),
            z_value=ONE_SIDED_975_Z,
            confidence_level=0.975,
        )
        halves = primary["chronological_halves"]
        gates = {
            "minimum_unique_markets": len(settled)
            >= replication_config["minimum_unique_markets_per_hypothesis"],
            "minimum_observation_days": len(days)
            >= replication_config["minimum_observation_days"],
            "primary_profitable": primary["virtual_profit"] > 0,
            "primary_beats_comparator": primary["virtual_profit"]
            > comparator["virtual_profit"],
            "both_chronological_halves_profitable": bool(
                halves["first"]["settled"]
                and halves["second"]["settled"]
                and halves["first"]["virtual_profit"] > 0
                and halves["second"]["virtual_profit"] > 0
            ),
            "expiry_cluster_lower_positive": expiry_lower["lower_bound"]
            is not None
            and expiry_lower["lower_bound"] > 0,
            "day_cluster_lower_positive": day_lower["lower_bound"] is not None
            and day_lower["lower_bound"] > 0,
            "paired_delta_day_lower_positive": delta_day_lower["lower_bound"]
            is not None
            and delta_day_lower["lower_bound"] > 0,
            "integer_contract_primary_profitable": integer_primary[
                "virtual_profit"
            ]
            > 0,
            "integer_contract_delta_day_lower_positive": integer_day_lower[
                "lower_bound"
            ]
            is not None
            and integer_day_lower["lower_bound"] > 0,
        }
        hypotheses.append(
            {
                "hypothesis": name,
                "asset": definition["asset"],
                "primary_arm": primary_name,
                "comparator_arm": comparator_name,
                "tracked": len(rows),
                "open": sum(row.get("status") == "open" for row in rows),
                "settled": len(settled),
                "observation_days": len(days),
                "primary": primary,
                "comparator": comparator,
                "profit_delta": round(
                    primary["virtual_profit"] - comparator["virtual_profit"], 4
                ),
                "primary_expiry_cluster_inference": expiry_lower,
                "primary_day_cluster_inference": day_lower,
                "delta_day_cluster_inference": delta_day_lower,
                "integer_contract_forward": {
                    "primary": integer_primary,
                    "comparator": integer_comparator,
                    "delta_day_cluster_inference": integer_day_lower,
                },
                "evaluation": {
                    "gates": gates,
                    "ready_for_manual_review": bool(gates)
                    and all(gates.values()),
                    "automatic_promotion": False,
                },
            }
        )
    return {
        "version": ASSET_REPLICATION_VERSION,
        "mode": MODE if replication_config["enabled"] else "disabled",
        "registered_at": experiment.get("registered_at"),
        "affects_execution": False,
        "automatic_promotion": False,
        "historical_backfill": False,
        "familywise_confidence_level": replication_config[
            "familywise_confidence_level"
        ],
        "per_hypothesis_one_sided_confidence_level": replication_config[
            "per_hypothesis_one_sided_confidence_level"
        ],
        "minimum_unique_markets_per_hypothesis": replication_config[
            "minimum_unique_markets_per_hypothesis"
        ],
        "minimum_observation_days": replication_config[
            "minimum_observation_days"
        ],
        "hypotheses": hypotheses,
        "recent_records": sorted(
            records,
            key=lambda row: str(row.get("settled_at") or row.get("captured_at") or ""),
            reverse=True,
        )[:100],
    }


def summarize(ledger, settings=None):
    ledger = normalize_ledger(ledger, settings)
    config = ledger["configuration"]
    records = ledger["records"]
    lanes = []
    for lane in LANES:
        rows = [row for row in records if row.get("lane") == lane]
        lane_summary = {"lane": lane, **_row_summary(rows)}
        ordered = sorted(
            [row for row in rows if row.get("status") == "settled"],
            key=lambda row: str(row.get("close_time") or ""),
        )
        split = len(ordered) // 2
        lane_summary["chronological_halves"] = {
            "first": _row_summary(ordered[:split]),
            "second": _row_summary(ordered[split:]),
        }
        lane_summary["assets"] = [
            {"asset": asset, **_row_summary([row for row in rows if row.get("asset") == asset])}
            for asset in config["assets"]
        ]
        lanes.append(lane_summary)
    opposition = next(row for row in lanes if row["lane"] == "directional_opposition")
    control = next(row for row in lanes if row["lane"] == "confirmation_control")
    strata = {}
    for row in records:
        if row.get("status") != "settled":
            continue
        key = str(row.get("comparison_stratum") or "")
        if not key:
            continue
        strata.setdefault(key, {}).setdefault(str(row.get("lane") or ""), []).append(row)
    matched = []
    matched_pairs = []
    for key, lane_rows in strata.items():
        opposition_rows = sorted(
            lane_rows.get("directional_opposition") or [],
            key=lambda row: str(row.get("captured_at") or ""),
        )
        control_rows = sorted(
            lane_rows.get("confirmation_control") or [],
            key=lambda row: str(row.get("captured_at") or ""),
        )
        pairs = min(len(opposition_rows), len(control_rows))
        if not pairs:
            continue
        paired_opposition = opposition_rows[:pairs]
        paired_control = control_rows[:pairs]
        for opposition_row, control_row in zip(paired_opposition, paired_control):
            try:
                lag_seconds = abs(
                    (
                        _now(opposition_row.get("captured_at"))
                        - _now(control_row.get("captured_at"))
                    ).total_seconds()
                )
            except (TypeError, ValueError):
                lag_seconds = None
            matched_pairs.append(
                {
                    "opposition": opposition_row,
                    "control": control_row,
                    "same_ticker": opposition_row.get("ticker")
                    == control_row.get("ticker"),
                    "same_side": opposition_row.get("side")
                    == control_row.get("side"),
                    "capture_lag_seconds": lag_seconds,
                    "profit_delta": _number(opposition_row.get("virtual_profit"))
                    - _number(control_row.get("virtual_profit")),
                }
            )
        matched.append({
            "stratum": key,
            "pairs": pairs,
            "opposition": _row_summary(paired_opposition),
            "control": _row_summary(paired_control),
            "profit_delta": round(
                _row_summary(paired_opposition)["virtual_profit"]
                - _row_summary(paired_control)["virtual_profit"],
                4,
            ),
        })
    comparable = sum(row["pairs"] for row in matched)
    lags = sorted(
        row["capture_lag_seconds"]
        for row in matched_pairs
        if row["capture_lag_seconds"] is not None
    )
    same_side_pairs = [row for row in matched_pairs if row["same_side"]]
    flipped_side_pairs = [row for row in matched_pairs if not row["same_side"]]
    raw_unpaired_delta = round(
        opposition["virtual_profit"] - control["virtual_profit"], 4
    )
    matched_delta = round(
        sum(_number(row.get("profit_delta")) for row in matched_pairs), 4
    )
    return {
        "version": VERSION,
        "mode": MODE if config["enabled"] else "disabled",
        "affects_execution": False,
        "automatic_promotion": False,
        "updated_at": ledger.get("updated_at"),
        "configuration": config,
        "tracked": len(records),
        "open": sum(row.get("status") == "open" for row in records),
        "settled": sum(row.get("status") == "settled" for row in records),
        "capture_funnel": dict(ledger.get("capture_funnel") or {}),
        "last_capture_funnel": dict(ledger.get("last_capture_funnel") or {}),
        "lanes": lanes,
        "matched_strata": matched,
        "legacy_observational_only": True,
        "raw_unpaired_opposition_minus_control_profit": raw_unpaired_delta,
        "opposition_minus_control_profit": raw_unpaired_delta,
        "matched_observational_profit_delta": matched_delta,
        "matched_observational_diagnostics": {
            "pairs": len(matched_pairs),
            "same_ticker_pairs": sum(row["same_ticker"] for row in matched_pairs),
            "same_side_pairs": len(same_side_pairs),
            "same_side_profit_delta": round(
                sum(_number(row.get("profit_delta")) for row in same_side_pairs), 4
            ),
            "flipped_side_pairs": len(flipped_side_pairs),
            "flipped_side_profit_delta": round(
                sum(_number(row.get("profit_delta")) for row in flipped_side_pairs),
                4,
            ),
            "median_capture_lag_seconds": round(lags[len(lags) // 2], 3)
            if lags
            else None,
            "maximum_capture_lag_seconds": round(max(lags), 3) if lags else None,
            "contemporaneous": False,
        },
        "evaluation": {
            "paired_markets_available": comparable,
            "minimum_independent_markets": config["minimum_independent_markets"],
            "sample_threshold_met": comparable
            >= config["minimum_independent_markets"],
            "ready_for_manual_review": False,
            "reason": "legacy_observational_noncontemporaneous_comparison",
            "automatic_promotion": False,
        },
        "paired_forward": _paired_forward_summary(ledger, config),
        "asset_replications": _asset_replication_summary(ledger, config),
        "recent_records": sorted(
            records,
            key=lambda row: str(row.get("settled_at") or row.get("captured_at") or ""),
            reverse=True,
        )[:100],
    }
