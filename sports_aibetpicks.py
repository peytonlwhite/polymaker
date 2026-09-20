"""Authenticated AIBetPicks import and exact-contract execution lane.

This module never submits an order itself. The Sports executor remains the
owner of cash reservations, reconciliation, order intents and fill accounting.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import sports_modest_recovery

CHICAGO = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parent
FEED_FILE = ROOT / "sports_aibetpicks_feed.json"
STATE_FILE = ROOT / "sports_aibetpicks_state.json"
TOKEN_FILE = Path.home() / ".codex" / "secrets" / "polymaker" / "aibetpicks.dpapi"
FEED_URL = "https://aibetpicks.ai/polymaker-picks-feed.php"
VERSION = "aibetpicks-v4-modest-recovery"
PARSER_VERSION = "aibetpicks-kalshi-source-v2"
# Supported full-game contract identities. Execution uses Kalshi settlement.
SERIES = {
    "baseball_mlb": ("KXMLB",),
    "americanfootball_nfl": ("KXNFL",),
    "americanfootball_ncaaf": ("KXNCAAF", "KXCFB"),
    "basketball_nba": ("KXNBA",),
    "basketball_ncaab": ("KXNCAAMB", "KXNCAAB"),
}
SUFFIX = {"moneyline": "GAME", "spread": "SPREAD", "total": "TOTAL"}
TERMINAL = {"filled", "duplicate", "expired", "invalid", "ambiguous"}


def tennis_family(pick):
    key = str(pick.get("sport_key") or "")
    match = re.fullmatch(r"tennis_(atp|wta)(?:_[a-z0-9_]+)?", key)
    return match[1] if match and "doubles" not in key else None


def supported_series(pick):
    kind = pick.get("bet_type") or pick.get("type")
    family = tennis_family(pick)
    if family:
        # Full-match singles moneylines only; GAME can mean a single game.
        return ("KX" + family.upper() + "MATCH",) if kind == "moneyline" else ()
    if not isinstance(kind, str) or kind not in SUFFIX:
        return ()
    return tuple(prefix + SUFFIX[kind] for prefix in SERIES.get(pick.get("sport_key"), ()))


def now_local():
    return datetime.now(CHICAGO)


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {} if default is None else default


def save_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, allow_nan=False))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                # Windows readers can briefly hold the destination open.
                # Retry the atomic rename, never truncate the live state.
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def enabled(engine):
    return str(engine.settings_file_value("SPORTS_AIBETPICKS_ENABLED", "true")).lower() == "true"


def polling_slot(now):
    local = now.astimezone(CHICAGO)
    return local.strftime("%Y-%m-%dT%H") if 10 <= local.hour <= 17 else None


def next_poll(state, now):
    local = now.astimezone(CHICAGO)
    retry = timestamp(state.get("retry_after"))
    if polling_slot(local) and state.get("last_slot") != polling_slot(local):
        due = max(local, retry.astimezone(CHICAGO)) if retry else local
        if polling_slot(due):
            return due
        return next_poll({}, due)
    if local.hour < 10:
        return local.replace(hour=10, minute=0, second=0, microsecond=0)
    if local.hour < 17:
        return local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return (local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)


def validate_feed(feed, now):
    if not isinstance(feed, dict):
        raise ValueError("unsupported_feed_schema")
    generated = timestamp(feed.get("generated_at"))
    if feed.get("schema_version") != 1 or not isinstance(feed.get("bots"), list):
        raise ValueError("unsupported_feed_schema")
    if not generated or not -60 <= (now - generated).total_seconds() <= 300:
        raise ValueError("stale_feed_response")
    if feed.get("date") != now.astimezone(CHICAGO).date().isoformat():
        raise ValueError("feed_date_mismatch")
    identifiers = [row.get("bot_id") for row in feed["bots"] if isinstance(row, dict)]
    if (len(identifiers) != len(feed["bots"])
            or not all(isinstance(value, str) and value.strip() for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError("invalid_bot_identifiers")
    if len(identifiers) > 100:
        raise ValueError("unexpected_bot_count")
    for bot in feed["bots"]:
        # PHP encodes an empty associative history as []; new source bots
        # must not block every other bot's picks before their first result.
        # Only the empty list is equivalent to an empty history mapping.
        if bot.get("history") == []:
            bot["history"] = {}
        if not isinstance(bot.get("history", {}), dict) or bot.get("today") is not None and not isinstance(bot["today"], dict):
            raise ValueError("invalid_bot_pick_payload")
    return feed


def fetch_feed(now=None):
    from secure_settings import read_secure_settings
    current = now or now_local()
    token = read_secure_settings(TOKEN_FILE).get("token")
    if not token:
        raise ValueError("feed_token_unavailable")
    # Never forward authentication to a redirected destination or log it.
    with requests.get(FEED_URL, headers={"X-Polymaker-Token": token},
                      timeout=(10, 25), allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise ValueError(f"feed_http_{response.status_code}")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 4_000_000:
                raise ValueError("feed_response_too_large")
            chunks.append(chunk)
        return validate_feed(json.loads(b"".join(chunks)), current)


def american_probability(odds):
    odds = number(odds)
    if odds is None or abs(odds) < 100:
        return None
    return -odds / (-odds + 100) if odds < 0 else 100 / (odds + 100)


def validate_pick(pick, now, *, history=False):
    if not isinstance(pick, dict):
        return "no_pick"
    required = ("pick_id", "event_id", "sport_key", "home_team", "away_team", "selection", "commence_time")
    if not all(isinstance(pick.get(key), str) and pick[key].strip() for key in required):
        return "missing_structured_identity"
    if not supported_series(pick) or pick.get("market_scope") != "full_game":
        return "unsupported_sport_or_period"
    expected_rules = "tennis_full_match_games_v1" if tennis_family(pick) else "two_way_including_overtime"
    if history and pick.get("settlement_rules") != expected_rules:
        return "unsupported_settlement_rules"
    kind = pick.get("bet_type") or pick.get("type")
    if not isinstance(kind, str) or kind not in SUFFIX or (pick.get("type") and pick["type"] != kind):
        return "unsupported_market_type"
    if pick.get("manual_override") or pick.get("result_needs_review"):
        return "source_requires_review"
    if history and not american_probability(pick.get("odds")):
        return "missing_posted_odds"
    if pick["home_team"] == pick["away_team"]:
        return "ambiguous_teams"
    if kind == "total":
        if pick["selection"].lower() not in {"over", "under"} or str(pick.get("direction") or "").lower() != pick["selection"].lower():
            return "total_direction_mismatch"
    else:
        if pick["selection"] not in {pick["home_team"], pick["away_team"]}:
            return "team_selection_mismatch"
        expected = "home" if pick["selection"] == pick["home_team"] else "away"
        if pick.get("direction") != expected:
            return "team_direction_mismatch"
    if kind != "moneyline":
        line = number(pick.get("line"))
        # Integer lines can push at a bookmaker; binary Kalshi contracts cannot.
        if line is None or abs(line * 2 - round(line * 2)) > 1e-9 or line == int(line):
            return "push_line_not_equivalent"
        if kind == "total" and line <= 0:
            return "invalid_total_line"
    elif pick.get("line") is not None:
        return "unexpected_moneyline_line"
    start = timestamp(pick["commence_time"])
    if not start:
        return "invalid_start_time"
    if not history:
        if pick.get("date") != now.astimezone(CHICAGO).date().isoformat():
            return "pick_date_mismatch"
        if pick.get("status") != "pending":
            return "source_not_pending"
        if start <= now + timedelta(minutes=5):
            return "entry_window_closed"
        if start > now + timedelta(days=2):
            return "start_too_far_away"
    return ""


def performance(bot, now):
    """Priced, verified returns; a favorite-heavy win rate is not an edge."""
    values, seen = [], set()
    risked_units = profit_units = 0.0
    missing_units = 0
    wins = losses = pushes = 0
    for pick in (bot.get("history") or {}).values():
        if validate_pick(pick, now, history=True):
            continue
        start = timestamp(pick.get("commence_time"))
        if not start or not timedelta(0) <= now - start <= timedelta(days=90):
            continue
        if pick["pick_id"] in seen or not pick.get("result_source"):
            continue
        seen.add(pick["pick_id"])
        outcome = pick.get("status")
        odds = float(pick["odds"])
        units = number(pick.get("stake_units"))
        if outcome not in {"win", "loss", "push"}:
            continue
        if units is None or units <= 0:
            missing_units += 1
            continue
        risked_units += units
        if outcome == "win":
            wins += 1
            value = odds / 100 if odds > 0 else 100 / -odds
            values.append(value)
            profit_units += units * value
        elif outcome == "loss":
            losses += 1
            values.append(-1.0)
            profit_units -= units
        elif outcome == "push":
            pushes += 1
    count = len(values)
    roi = profit_units / risked_units if risked_units else 0.0
    # Retain the legacy flat-stake diagnostic separately from unit-weighted ROI.
    flat_roi = statistics.mean(values) if values else 0.0
    lower = flat_roi - 1.96 * statistics.stdev(values) / math.sqrt(count) if count > 1 else -1.0
    return {"wins": wins, "losses": losses, "pushes": pushes, "sample": count,
            "roi_pct": round(100 * roi, 2), "roi_lower_pct": round(100 * lower, 2),
            "risked_units": risked_units, "profit_units": profit_units, "missing_unit_records": missing_units,
            "roi_lower_basis": "flat_1u_diagnostic_only",
            "basis": "verified_priced_full_game_last_90_days"}


def sizing(pick, maximum_units=None):
    """Use the published stake, never infer it from our price or track record."""
    units = number((pick or {}).get("stake_units"))
    error = ""
    if units is None or units <= 0:
        error = "aibetpicks_source_units_missing_or_invalid"
    elif maximum_units is not None:
        maximum = number(maximum_units)
        if maximum is None or maximum <= 0 or units > maximum:
            error = "aibetpicks_source_units_above_limit"
    return {"ok": not error, "error": error or None, "target_units": units,
            "published_units": units, "basis": "published_stake_units",
            "reason": error or "Published AIBetPicks unit size",
            "local_edge_required": False, "local_history_required": False}


def execution_sizing(engine, portfolio, candidate, *, now=None, unit_size=None):
    """Published base plus an independently capped, optional unit addition."""
    pick = (candidate.get("aibetpicks") or {}).get("pick") or {}
    units = sizing(pick, engine.SPORTS_UNIT_MAX_PER_MARKET)
    if not units["ok"]:
        return units
    base = units["target_units"]
    unit = unit_size if unit_size is not None else engine.effective_sports_unit_size(portfolio)
    recovery = sports_modest_recovery.review(
        portfolio, candidate, lane="aibetpicks", base_units=base,
        maximum_units=engine.SPORTS_UNIT_MAX_PER_MARKET, now=now,
        is_enabled=sports_modest_recovery.enabled(engine))
    target = recovery["target_units"]
    # Optional additions yield first when the full enlarged stake cannot fit.
    # The published base continues to require full depth and all risk checks.
    while target > base:
        price = number(candidate.get("entry_price")) or 0
        depth = number(candidate.get("executable_contracts_at_ask")) or 0
        needed = math.floor(round(target * unit, 2) * 100 / price + 1e-9) if price > 0 and unit > 0 else 0
        capacity = math.floor(depth * engine.SPORTS_FOK_TOP_DEPTH_UTILIZATION)
        issue = "recovery_depth_unavailable" if needed < 1 or capacity < needed else lane_preflight(engine, portfolio, candidate, round(target * unit, 2))
        if not issue:
            break
        target = max(base, target - 0.5)
        recovery = sports_modest_recovery.resize(recovery, target, "addition_reduced_" + issue)
    return {**units, "base_units": base, "target_units": target,
            "recovery": recovery, "reason": recovery["reason"] if target > base else units["reason"]}


def pick_fingerprint(pick):
    fields = ("sport_key", "event_id", "home_team", "away_team", "commence_time", "selection", "line", "direction")
    payload = {key: pick.get(key) for key in fields}
    payload["market_type"] = pick.get("bet_type") or pick.get("type")
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def contract_terms(market, kind):
    """Use the strike/explicit line label; never read a number in a team name."""
    if kind == "moneyline":
        return {"type": kind}
    labels = " ".join(str(market.get(key) or "") for key in ("yes_sub_title", "title"))
    match = re.search(r"\b(over|under|more than|less than)\s+(\d+(?:\.\d+)?)", labels, re.I)
    if not match:
        return {}
    direction = "Under" if match[1].lower() in {"under", "less than"} else "Over"
    point = float(match[2])
    strike = number(market.get("cap_strike" if direction == "Under" else "floor_strike"))
    if strike is not None and strike != point:
        return {}
    if market.get("strike_type") and market["strike_type"] != ("less" if direction == "Under" else "greater"):
        return {}
    if kind == "spread" and direction != "Over":
        return {}
    return {"type": kind, "point": -point if kind == "spread" else point, "side": direction}


def exact_candidate(engine, pick, market, now):
    reason = validate_pick(pick, now)
    if reason:
        return None, reason
    kind = pick.get("bet_type") or pick["type"]
    series = engine.inferred_market_series_ticker(market)
    if series not in supported_series(pick):
        return None, "different_sport_or_period"
    if market.get("status") not in {"open", "active"}:
        return None, "market_not_open"
    if not engine.market_date_matches_game(pick, market):
        return None, "event_date_mismatch"
    if tennis_family(pick) and engine.kalshi_market_date(market) != engine.game_date(pick):
        return None, "tennis_exact_event_date_required"
    start = engine.kalshi_market_start_datetime(market)
    if start is None and pick["sport_key"] != "baseball_mlb":
        # Football/basketball event tickers commonly encode only the date.
        # Exact teams, league and date still identify the full-game event.
        start = timestamp(pick["commence_time"])
    if not start or abs((start - timestamp(pick["commence_time"])).total_seconds()) > 30 * 60:
        return None, "event_start_mismatch_or_missing"
    score, identity = engine.match_game_to_market(pick, market)
    if score < 2:
        return None, "event_teams_mismatch"
    if pick["sport_key"] in engine.COLLEGE_SPORT_KEYS:
        college = engine.college_market_identity_review(pick, market)
        if not college.get("detected") or not college.get("matched"):
            return None, "college_event_identity_unverified"
    classified = contract_terms(market, kind)
    if classified.get("type") != kind:
        return None, "market_type_mismatch"
    point = number(classified.get("point"))
    if kind == "total":
        if point != number(pick["line"]):
            return None, "exact_line_unavailable"
        yes_selection = str(classified.get("side") or "Over").lower()
        order_side = "yes" if yes_selection == pick["selection"].lower() else "no"
    else:
        yes_team = engine.infer_market_side(pick, market)
        teams = {engine.normalize_text(pick["home_team"]), engine.normalize_text(pick["away_team"])}
        if not yes_team or yes_team not in teams:
            return None, "selected_team_unverified"
        if tennis_family(pick) and engine.normalize_text(market.get("yes_sub_title") or "") != yes_team:
            return None, "tennis_full_player_name_required"
        order_side = "yes" if yes_team == engine.normalize_text(pick["selection"]) else "no"
        if kind == "spread" and (point if order_side == "yes" else -point if point is not None else None) != number(pick["line"]):
            return None, "exact_line_unavailable"
    ticker = market.get("ticker")
    result = {**pick, "aibetpicks": {"active": True, "pick": dict(pick), "version": VERSION},
              "source": "aibetpicks", "strategy_owner": "aibetpicks", "kalshi_ticker": ticker,
              "ticker": ticker, "kalshi_title": market.get("title"), "event_ticker": market.get("event_ticker"),
              "market_type": kind, "market_line": pick.get("line"), "selected_team": pick["selection"],
              "order_side": order_side, "game_key": engine.row_game_key({**pick, "id": pick["event_id"]}), "game": pick.get("game"),
              "game_started": False, "game_completed": False, "pregame_eligible": True,
              "kalshi_market_start": start.isoformat(), "aibetpicks_identity": identity,
              "aibetpicks_fingerprint": pick_fingerprint(pick),
              "feature_score": 0, "kalshi_volume": market.get("volume", 0)}
    # User-authorized source following: the selection and units determine the
    # wager. Retain the actual venue rules without requiring book equivalence.
    result["aibetpicks_settlement_review"] = {
        "execution_compatible": True, "basis": "kalshi_contract_rules",
        "source_bookmaker": pick.get("bookmaker"),
        "source_rules": pick.get("settlement_rules"),
        "kalshi_rules_primary": market.get("rules_primary"),
        "kalshi_rules_secondary": market.get("rules_secondary"),
    }
    if pick["sport_key"] in engine.COLLEGE_SPORT_KEYS:
        review = engine.college_market_side_identity_review(result, market)
        if review.get("applies") and (not review.get("detected") or not review.get("matched")):
            return None, "college_side_identity_unverified"
    from sports_build import strategy_fields
    result.update(strategy_fields())
    identity_function = getattr(engine, "sports_strategy_identity", None)
    if identity_function:
        identity = identity_function()
        result.update(strategy_version=identity["version"], strategy_config_hash=identity["config_hash"])
    return result, ""


def executable_book(data, side):
    """Kalshi publishes bids; the opposite best bid is this side's ask."""
    fixed = data.get("orderbook_fp")
    book = fixed if isinstance(fixed, dict) else data.get("orderbook") or {}
    def levels(which):
        rows = book.get(which + "_dollars") if fixed is not None else book.get(which)
        valid = []
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            price, quantity = number(row[0]), number(row[1])
            if price is not None and quantity is not None and quantity > 0:
                price = price * 100 if fixed is not None else price
                if 0 < price < 100:
                    valid.append((price, quantity))
        return sorted(valid, reverse=True)
    own, opposite = levels(side), levels("no" if side == "yes" else "yes")
    if not own or not opposite:
        return None, None, None
    ask = round(100 - opposite[0][0], 4)
    # The shared executor accepts integral-cent prices. Fail closed on a
    # fractional-cent contract until its executor supports that price grid.
    if abs(ask - round(ask)) > 1e-7:
        return None, None, None
    return own[0][0], ask, opposite[0][1]


def order_guard(engine, candidate, now=None):
    current = now or now_local()
    pick = (candidate.get("aibetpicks") or {}).get("pick") or {}
    market = engine.fetch_kalshi_market_by_ticker(candidate.get("kalshi_ticker")) or {}
    exact, reason = exact_candidate(engine, pick, market, current)
    if not exact or exact["order_side"] != candidate.get("order_side"):
        return {"ok": False, "error": "aibetpicks_" + (reason or "side_changed")}
    side = exact["order_side"]
    try:
        from urllib.parse import quote
        url = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
        book, _headers = engine.get_json(f"{url}/markets/{quote(candidate['kalshi_ticker'], safe='')}/orderbook")
        bid, ask, depth = executable_book(book, side)
    except Exception:
        return {"ok": False, "error": "aibetpicks_orderbook_unavailable"}
    if bid is None or ask is None or not 0 < bid <= ask < 100:
        return {"ok": False, "error": "aibetpicks_quote_unavailable"}
    if ask - bid > engine.SPORTS_MAX_SPREAD_CENTS:
        return {"ok": False, "error": "aibetpicks_quote_spread_too_wide"}
    fee_pp = engine.estimated_taker_fee_per_contract(ask, engine.SPORTS_SHADOW_FEE_RATE) * 100
    if ask > engine.SPORTS_LIVE_MAX_PRICE_CENTS:
        return {"ok": False, "error": "live_price_above_max"}
    # Optional legacy diagnostics never approve, block or size a source pick.
    fair = number(candidate.get("aibetpicks_fair_probability"))
    evidence_expiry = timestamp(candidate.get("aibetpicks_evidence_valid_until"))
    if not evidence_expiry or current > evidence_expiry:
        fair = None
    edge = round(fair * 100 - ask - fee_pp, 3) if fair is not None else None
    updates = {"entry_price": ask, "entry_bid": bid, "kalshi_spread": ask - bid,
               "entry_american_odds": engine.american_from_kalshi_cents(ask),
               "edge": edge, "net_edge": edge, "estimated_fee_edge_pp": fee_pp,
               "executable_contracts_at_ask": depth, "orderbook_depth_valid": depth is not None and depth > 0,
               "aibetpicks_fair_probability": fair, "model_prob": fair * 100 if fair is not None else None,
               "aibetpicks_settlement_review": exact["aibetpicks_settlement_review"]}
    return {"ok": True, "candidate_updates": updates, "source": "kalshi_rest_exact_contract"}


def current_book_evidence(engine, pick, now, *, details=None):
    """One bounded event request, using shared quota/key rotation accounting."""
    kind = pick.get("bet_type") or pick["type"]
    markets = {"moneyline": "h2h", "spread": "spreads,alternate_spreads", "total": "totals,alternate_totals"}[kind]
    regions = "us,us2,eu"
    cost = engine.estimated_credit_cost(markets, regions)
    if not engine.can_spend_odds_credits(cost)[0]:
        return None, 0, "odds_budget_unavailable"
    try:
        game, headers = engine.odds_api_get_json(
            f"https://api.the-odds-api.com/v4/sports/{pick['sport_key']}/events/{pick['event_id']}/odds",
            params={"regions": regions, "markets": markets, "oddsFormat": "american", "dateFormat": "iso",
                    "includeLinks": "true", "includeSids": "true"})
        engine.record_odds_spend(pick["sport_key"], markets, cost,
            engine.parse_int_header(headers, "x-requests-remaining"),
            engine.parse_int_header(headers, "x-requests-last"),
            key_index=headers.get("_polymaker_odds_key_index"), regions=regions,
            event_id=pick["event_id"], purpose="aibetpicks_exact_line_evidence")
    except Exception:
        return None, 0, "book_evidence_unavailable"
    if not isinstance(game, dict) or game.get("id") != pick["event_id"]:
        return None, 0, "provider_event_mismatch"
    if any(game.get(key) != pick.get(key) for key in ("home_team", "away_team")) or not timestamp(game.get("commence_time")) or abs((timestamp(game["commence_time"]) - timestamp(pick["commence_time"])).total_seconds()) > 300:
        return None, 0, "provider_event_mismatch"
    families, expiries = {}, []
    for book in game.get("bookmakers") or []:
        for market in book.get("markets") or []:
            if market.get("key") not in markets.split(","):
                continue
            updated = timestamp(market.get("last_update") or book.get("last_update"))
            if not updated or not -60 <= (now - updated).total_seconds() <= 180:
                continue
            outcomes = market.get("outcomes") or []
            selected, other = None, None
            for outcome in outcomes:
                line = number(outcome.get("point"))
                if outcome.get("name") == pick["selection"] and (kind == "moneyline" or line == number(pick["line"])):
                    selected = american_probability(outcome.get("price"))
                opposite_line = -number(pick["line"]) if kind == "spread" else number(pick.get("line"))
                expected_other = ({pick["home_team"], pick["away_team"]} - {pick["selection"]}) if kind != "total" else ({"Over", "Under"} - {pick["selection"]})
                if outcome.get("name") in expected_other and (kind == "moneyline" or line == opposite_line):
                    other = american_probability(outcome.get("price"))
            if selected and other and 0.98 <= selected + other <= 1.20:
                from sports_probability import BOOK_FAMILIES
                book_key = engine.normalize_text(book.get("key"))
                if not book_key:
                    continue
                family = BOOK_FAMILIES.get(book_key, book_key)
                families.setdefault(family, []).append(selected / (selected + other))
                expiries.append(updated + timedelta(seconds=180))
    values = [statistics.median(rows) for rows in families.values()]
    if details is not None and expiries:
        details["valid_until"] = min(expiries).isoformat()
    return (statistics.median(values) if values else None), len(values), "fresh_exact_line_consensus" if values else "exact_line_books_unavailable"


def select_executable_candidate(engine, matches, portfolio, now, *, target_units=1):
    """Compare equivalent listings that can fill the published source stake."""
    available, reason = [], "exact_contract_unavailable"
    unit = engine.effective_sports_unit_size(portfolio)
    unique = {row["kalshi_ticker"]: row for row in matches}
    for candidate in list(unique.values())[:8]:
        guard = order_guard(engine, candidate, now)
        if not guard.get("ok"):
            reason = guard.get("error") or "aibetpicks_quote_unavailable"
            continue
        candidate.update(guard["candidate_updates"])
        price = candidate["entry_price"]
        if price > engine.SPORTS_LIVE_MAX_PRICE_CENTS:
            reason = "live_price_above_max"
            continue
        usable = math.floor(candidate["executable_contracts_at_ask"] * engine.SPORTS_FOK_TOP_DEPTH_UTILIZATION)
        required_stake = round(unit * target_units, 2)
        required_contracts = math.floor(required_stake * 100 / price + 1e-9)
        if unit <= 0 or required_contracts < 1 or usable < required_contracts:
            reason = "aibetpicks_below_source_unit_capacity"
            continue
        available.append(candidate)
    if not available:
        return None, reason
    return min(available, key=lambda row: (row["entry_price"], -row["executable_contracts_at_ask"], row["kalshi_ticker"])), ""


def discover_source_candidates(engine, pick, now):
    """A broad catalog page can omit an older event that is still open.

    Search only the source's supported full-game series, with bounded paging;
    every returned candidate still passes the exact identity/line validator.
    """
    from urllib.parse import urlencode
    matches, pages, errors, truncated = [], 0, [], False
    kind = pick.get("bet_type") or pick.get("type")
    if validate_pick(pick, now):
        return [], {"requested_pages": 0, "errors": [], "truncated": False}
    base = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
    for series in supported_series(pick):
        cursor, seen = None, set()
        for _ in range(3):
            params = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                pages += 1
                data, _headers = engine.get_json(f"{base}/markets?{urlencode(params)}")
                if not isinstance(data, dict) or not isinstance(data.get("markets"), list):
                    raise ValueError("invalid_market_catalog")
            except Exception as exc:
                errors.append(type(exc).__name__)
                break
            for market in data["markets"]:
                if not isinstance(market, dict):
                    continue
                candidate, _reason = exact_candidate(engine, pick, market, now)
                if candidate:
                    matches.append(candidate)
            cursor = data.get("cursor")
            if matches or not cursor:
                break
            if cursor in seen:
                truncated = True
                break
            seen.add(cursor)
        else:
            truncated = bool(cursor)
    return matches, {"requested_pages": pages, "errors": errors, "truncated": truncated}


def existing_duplicate(portfolio, candidate, engine=None):
    for row in [*(portfolio.get("bets") or []), *(portfolio.get("history") or [])]:
        if row.get("mode") != "live":
            continue
        same_pick = row.get("aibetpicks_fingerprint") == candidate["aibetpicks_fingerprint"]
        same_contract = (row.get("kalshi_ticker") or row.get("ticker")) == candidate["kalshi_ticker"]
        if same_pick or same_contract:
            return True
        if engine is None or engine.row_game_key(row) != engine.row_game_key(candidate):
            continue
        # A normal scanner position can express the same bet using NO on
        # the opponent's ticker. Compare the selected outcome, not YES/NO.
        kind = candidate.get("market_type")
        contract = {"ticker": row.get("kalshi_ticker") or row.get("ticker")}
        series = engine.inferred_market_series_ticker(contract)
        if series not in supported_series({**candidate, "bet_type": kind}):
            continue
        row_start = timestamp(row.get("commence_time") or row.get("kalshi_market_start")) or engine.kalshi_market_start_datetime(contract)
        pick_start = timestamp(candidate.get("commence_time"))
        if row_start and pick_start and abs((row_start - pick_start).total_seconds()) > 1800:
            continue  # Preserve distinct games in a doubleheader.
        a, b = engine.row_position(candidate), engine.row_position(row)
        if a["market_type"] == b["market_type"] and a["side"] == b["side"] and (kind == "moneyline" or a["line"] == b["line"]):
            return True
    return False


def lane_preflight(engine, portfolio, candidate, stake):
    if engine.EXECUTION_MODE != "live" or not engine.live_trading_ready():
        return "live_execution_unavailable"
    if not engine.SPORTS_LIVE_EDGE_ORDER_ENABLED:
        return "live_orders_disabled"
    if existing_duplicate(portfolio, candidate, engine):
        return "duplicate"
    if any(engine.positions_are_direct_opposites(candidate, row)
           for row in portfolio.get("bets", [])
           if row.get("mode") == "live" and row.get("status") in {"open", "pending", "resting"}):
        return "opposite_position_already_open"
    if len(engine.sports_bot_open_positions(portfolio)) >= engine.SPORTS_LIVE_CAMPAIGN_MAX_OPEN:
        return "sports_open_slots_full"
    unit = engine.effective_sports_unit_size(portfolio)
    lane_exposure = sum(float(row.get("stake") or 0) for row in portfolio.get("bets", [])
                        if row.get("mode") == "live" and row.get("status") in {"open", "pending", "resting"}
                        and (row.get("source") == "aibetpicks" or row.get("strategy_owner") == "aibetpicks"))
    if lane_exposure + stake > 10 * unit + 0.009:
        return "aibetpicks_open_10u_cap"
    daily_cap = engine.effective_live_daily_loss_cap()
    if daily_cap > 0 and engine.live_daily_loss(portfolio) + stake > daily_cap + 0.009:
        return "live_daily_loss_remaining"
    if engine.SPORTS_LIVE_CAMPAIGN_ENABLED:
        campaign = engine.sports_live_campaign_context(portfolio)
        if campaign.get("daily_loss_cap_hit") or (float(campaign.get("global_daily_loss_cap") or campaign.get("daily_loss_cap") or 0) > 0 and stake > float(campaign.get("daily_loss_remaining") or 0) + 0.009):
            return "campaign_daily_loss_cap"
    return engine.live_execution_preflight_skip_reason(portfolio, candidate, stake)


def record_fill(engine, portfolio, candidate, order):
    candidate.update(order.get("candidate_updates") or {})
    stake = float(order.get("actual_stake") or 0)
    unit = float(candidate["sports_units"]["unit_size"])
    filled_at = now_local().isoformat(timespec="seconds")
    bet = {**candidate, "id": "aibetpicks-" + uuid.uuid4().hex, "source": "aibetpicks",
           "strategy_owner": "aibetpicks", "live_order": order, "stake": stake, "mode": "live",
           "placed_at": filled_at, "status": "open", "unit_size": unit, "unit_count": round(stake / unit, 3),
           "contracts": float(order.get("contracts") or 0), "kalshi_order_side": candidate["order_side"],
           "fee": round(float(candidate.get("exact_order_fee") or engine.order_response_fee(order.get("response") or {})
                              or engine.rounded_taker_fee(order.get("contracts"), candidate.get("entry_price"))), 4)}
    bet["sports_units"] = {**candidate["sports_units"], "placed_units": bet["unit_count"]}
    bet["clv_identity"] = engine.clv_market_identity(bet)
    portfolio["balance"] = round(float(portfolio.get("balance") or 0) - stake, 2)
    portfolio.setdefault("bets", []).append(bet)
    engine.save_portfolio(portfolio)
    client_id = (order.get("request") or {}).get("client_order_id")
    if client_id:
        engine.upsert_live_order_intent(client_id, status="portfolio_committed", portfolio_bet_id=bet["id"], committed_at=filled_at)
    reservation = (order.get("shared_bankroll") or {}).get("reservation_id")
    if reservation and engine.finalize_reservation:
        engine.finalize_reservation(reservation, "filled", stake)
    engine.append_jsonl(engine.EVENTS_FILE, {"type": "aibetpicks_pick_filled", "at": filled_at,
        "pick_id": candidate["pick_id"], "bot_id": candidate["aibetpicks_bot_id"], "ticker": candidate["kalshi_ticker"],
        "side": candidate["order_side"], "units": bet["unit_count"], "stake": stake})
    return bet


def sync_recovered_decisions(state, portfolio, engine):
    """Only a persisted fill or a definitive intent outcome resolves a submission."""
    changed = False
    for decision in state.get("decisions", {}).values():
        if decision.get("status") != "ambiguous":
            continue
        def matches(candidate):
            return (bool(decision.get("bot_id") and decision.get("pick_id"))
                    and candidate.get("aibetpicks_bot_id") == decision.get("bot_id")
                    and candidate.get("pick_id") == decision.get("pick_id"))
        fills = [row for row in [*(portfolio.get("bets") or []), *(portfolio.get("history") or [])]
                 if row.get("mode") == "live" and matches(row) and float(row.get("stake") or 0) > 0]
        if fills:
            bet = fills[0]
            unit = number(bet.get("unit_size") or (bet.get("sports_units") or {}).get("unit_size"))
            decision.update(status="filled", reason="recovered_fill", bet_id=bet.get("id"),
                            actual_stake=bet["stake"], actual_units=round(bet["stake"] / unit, 3) if unit else None,
                            recovery=(bet.get("sports_units") or {}).get("recovery"))
            changed = True
            continue
        intents = [row for row in engine.load_live_order_intents().get("intents", []) if matches(row.get("candidate") or {})]
        if intents and all(row.get("status") in {"rejected_no_order", "not_filled", "order_not_found"} for row in intents):
            decision.update(status="waiting", reason="recovered_without_fill", retry_after=None)
            changed = True
    return changed


def process(engine, portfolio, markets, *, now=None, allow_poll=True):
    current = now or now_local()
    state = read_json(STATE_FILE)
    state.setdefault("decisions", {})
    if sync_recovered_decisions(state, portfolio, engine):
        save_json(STATE_FILE, state)
    placed = []
    if not enabled(engine):
        return {"enabled": False, "placed": []}
    slot = polling_slot(current)
    retry = timestamp(state.get("retry_after"))
    if allow_poll and slot and (state.get("last_slot") != slot or state.get("source_policy_version") != VERSION) and (not retry or current >= retry):
        state["last_attempt"] = current.isoformat()
        try:
            feed = fetch_feed(current)
            save_json(FEED_FILE, feed)
            state.update(last_poll=current.isoformat(), last_slot=slot, error=None, retry_after=None, source_policy_version=VERSION)
        except Exception as exc:
            # Exception types only: request internals can contain secrets.
            state.update(error=str(exc) if isinstance(exc, ValueError) and str(exc).startswith(("feed_", "stale_", "unsupported_", "invalid_", "unexpected_")) else type(exc).__name__,
                         retry_after=(current + timedelta(minutes=5)).isoformat())
        save_json(STATE_FILE, state)
    feed = read_json(FEED_FILE)
    fresh = timestamp(state.get("last_poll"))
    # Never trade a cached feed after an outage or a new-day restart.
    if not slot or state.get("error") or not fresh or not -60 <= (current - fresh).total_seconds() <= 3900 or feed.get("date") != current.astimezone(CHICAGO).date().isoformat():
        return {**dashboard_summary(current), "placed": placed}
    for bot in feed.get("bots") or []:
        pick = bot.get("today")
        if not isinstance(pick, dict):
            continue
        key = str(bot["bot_id"]) + ":" + str(pick.get("date"))
        previous = state["decisions"].get(key) or {}
        signature = hashlib.sha256(json.dumps(pick, sort_keys=True).encode()).hexdigest()
        if previous.get("status") in TERMINAL:
            recheck_source_policy = (
                previous.get("status") == "invalid"
                and previous.get("reason") in {"unsupported_sport_or_period", "unsupported_settlement_rules",
                                               "missing_posted_odds", "nfl_moneyline_tie_payout_not_equivalent"}
                and previous.get("parser_version") != PARSER_VERSION
                and supported_series(pick)
            )
            # A corrected/replaced unpublished pick may become executable.
            # Filled, duplicate and uncertain submissions never reopen.
            if not recheck_source_policy and (previous.get("status") not in {"invalid", "expired"} or previous.get("source_signature") == signature):
                continue
        due = timestamp(previous.get("retry_after"))
        if due and current < due and previous.get("parser_version") == PARSER_VERSION:
            continue
        decision = {"bot_id": bot["bot_id"], "bot_name": bot.get("name"), "pick_id": pick.get("pick_id"),
                    "source_signature": signature, "parser_version": PARSER_VERSION,
                    "pick": pick.get("pick"), "game": pick.get("game"), "date": pick.get("date"),
                    "updated_at": current.isoformat(), "status": "waiting", "reason": "exact_contract_unavailable"}
        state["decisions"][key] = decision
        issue = validate_pick(pick, current)
        if issue:
            decision.update(status="expired" if issue == "entry_window_closed" else "invalid", reason=issue)
            save_json(STATE_FILE, state)
            continue
        units = sizing(pick, engine.SPORTS_UNIT_MAX_PER_MARKET)
        unit = engine.effective_sports_unit_size(portfolio)
        decision.update(units=units["target_units"], published_units=units["published_units"],
                        unit_size=unit, sizing=units)
        if not units["ok"] or not number(unit) or unit <= 0:
            decision.update(status="waiting", reason=units["error"] or "aibetpicks_unit_value_unavailable",
                            retry_after=(current + timedelta(minutes=2)).isoformat())
            save_json(STATE_FILE, state)
            continue
        stake = round(units["target_units"] * unit, 2)
        decision["planned_stake"] = stake
        matches = []
        for market in markets:
            candidate, _reason = exact_candidate(engine, pick, market, current)
            if candidate:
                matches.append(candidate)
        if not matches:
            matches, discovery = discover_source_candidates(engine, pick, current)
            decision["source_market_discovery"] = discovery
        # Equivalent YES/NO listings compete on executable price and depth;
        # the pick is never split or accumulated across contracts.
        matches.sort(key=lambda row: (row["order_side"] != "yes", row["kalshi_ticker"]))
        if not matches:
            decision["retry_after"] = (current + timedelta(minutes=2)).isoformat()
            save_json(STATE_FILE, state)
            continue
        duplicate = next((candidate for candidate in matches if existing_duplicate(portfolio, candidate, engine)), None)
        if duplicate:
            decision.update(status="duplicate", reason="existing_pick_or_contract",
                            ticker=duplicate["kalshi_ticker"], side=duplicate["order_side"])
            save_json(STATE_FILE, state)
            continue
        candidate, quote_issue = select_executable_candidate(engine, matches, portfolio, current, target_units=units["target_units"])
        if candidate is None:
            decision.update(reason=quote_issue, retry_after=(current + timedelta(minutes=2)).isoformat())
            save_json(STATE_FILE, state)
            continue
        candidate["aibetpicks_bot_id"] = bot["bot_id"]
        candidate["aibetpicks_bot_name"] = bot.get("name")
        units = execution_sizing(engine, portfolio, candidate, now=current, unit_size=unit)
        stake = round(units["target_units"] * unit, 2)
        decision.update(units=units["target_units"], planned_stake=stake, sizing=units,
                        recovery=units["recovery"])
        decision.update(ticker=candidate["kalshi_ticker"], side=candidate["order_side"])
        candidate.update(edge=None, net_edge=None, model_prob=None,
                         aibetpicks_fair_probability=None,
                         sports_units={**units, "unit_size": unit, "enabled": True, "source": VERSION})
        decision["book_evidence"] = "not_required_for_source_sizing"
        issue = lane_preflight(engine, portfolio, candidate, stake)
        if issue:
            decision.update(reason=issue, retry_after=(current + timedelta(minutes=2)).isoformat())
            save_json(STATE_FILE, state)
            continue
        # Write the source identity before entering the durable exchange order
        # protocol. A crash here fails closed; recovery never blindly retries.
        decision.update(status="ambiguous", reason="submission_in_progress")
        save_json(STATE_FILE, state)
        order = engine.place_live_kalshi_order(candidate, stake, portfolio=portfolio)
        if order.get("ok") and float(order.get("contracts") or 0) > 0:
            bet = record_fill(engine, portfolio, candidate, order)
            placed.append(bet)
            decision.update(status="filled", reason="filled", bet_id=bet["id"], actual_stake=bet["stake"], actual_units=bet["unit_count"])
            decision["recovery"] = (bet.get("sports_units") or {}).get("recovery")
        else:
            unresolved = any(row.get("status") in {"prepared", "ambiguous", "response_received", "response_recovered", "filled_pending_portfolio"}
                             for row in engine.load_live_order_intents().get("intents", []))
            decision.update(status="ambiguous" if unresolved else "waiting", reason=order.get("error") or "not_filled",
                            retry_after=(current + timedelta(minutes=5)).isoformat())
        save_json(STATE_FILE, state)
    return {**dashboard_summary(current), "placed": placed}


def dashboard_summary(now=None, *, is_enabled=None):
    current = now or now_local()
    state, feed = read_json(STATE_FILE), read_json(FEED_FILE)
    if is_enabled is None:
        is_enabled = str(read_json(ROOT / "bot_settings.json").get("SPORTS_AIBETPICKS_ENABLED", "true")).lower() == "true"
    today = current.astimezone(CHICAGO).date().isoformat()
    rows = []
    for bot in feed.get("bots") or []:
        pick = bot.get("today") if feed.get("date") == today else None
        decision = (state.get("decisions") or {}).get(str(bot["bot_id"]) + ":" + today) or {}
        rows.append({"bot_id": bot["bot_id"], "name": bot.get("name"), "pick": pick,
                     "performance": performance(bot, current), "decision": decision})
    last_poll = timestamp(state.get("last_poll"))
    if not is_enabled:
        health = "disabled"
    elif state.get("error"):
        health = "error"
    elif not polling_slot(current):
        health = "scheduled"
    elif not last_poll:
        health = "waiting"
    elif not -60 <= (current - last_poll).total_seconds() <= 3900 or feed.get("date") != today:
        health = "stale"
    else:
        health = "healthy"
    return {"enabled": is_enabled, "health": health, "version": VERSION, "timezone": "America/Chicago",
            "schedule": "Hourly, 10 AM–5 PM America/Chicago", "last_poll": state.get("last_poll"),
            "next_poll": next_poll(state, current).isoformat() if is_enabled else None, "error": state.get("error"),
            "feed_date": feed.get("date"), "bots": rows,
            "sizing_policy": "Published AIBetPicks units remain the base. When modest recovery is enabled, eligible new bets can add 0.5U, 1U or at most 2U within daily/open budgets. Kalshi rules apply; no sportsbook odds, vig or independent edge requirement. Shared execution safeguards still apply."}
