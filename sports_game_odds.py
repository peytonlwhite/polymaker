"""SportsGameOdds ingestion, adaptive quota pacing, and schema normalization.

The provider is used server-side as an independent validation source.  Its
events are normalized to the same compact game/bookmaker shape consumed by the
existing sports pricing engine; it never submits wagers itself.
"""

from __future__ import annotations

import calendar
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_common import get_json, normalize_text, write_json


API_BASE = "https://api.sportsgameodds.com/v2"
MAIN_ODD_IDS = (
    "points-home-game-ml-home",
    "points-away-game-ml-away",
    "points-home-game-sp-home",
    "points-away-game-sp-away",
    "points-all-game-ou-over",
    "points-all-game-ou-under",
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_minutes(value):
    parsed = _parse_iso(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 60.0)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _american(value):
    number = _number(value)
    return int(round(number)) if number not in (None, 0) else None


def _team_name(team):
    names = (team or {}).get("names") or {}
    return str(names.get("long") or names.get("medium") or names.get("short") or (team or {}).get("teamID") or "").strip()


def _name_tokens(value):
    ignored = {"fc", "cf", "the", "club", "team"}
    return {part for part in normalize_text(value).split() if len(part) > 1 and part not in ignored}


def _name_matches(left, right):
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return False
    if a == b or (len(a) >= 5 and a in b) or (len(b) >= 5 and b in a):
        return True
    tokens_a = _name_tokens(a)
    tokens_b = _name_tokens(b)
    overlap = tokens_a & tokens_b
    return bool(overlap) and len(overlap) >= min(2, len(tokens_a), len(tokens_b))


def provider_filter_for_sport(sport_key):
    key = str(sport_key or "").lower()
    mappings = (
        ("baseball_mlb", ("leagueID", "MLB")),
        ("basketball_wnba", ("leagueID", "WNBA")),
        ("basketball_nba", ("leagueID", "NBA")),
        ("americanfootball_ncaaf", ("leagueID", "NCAAF")),
        ("americanfootball_nfl", ("leagueID", "NFL")),
        ("basketball_ncaab", ("leagueID", "NCAAB")),
        ("icehockey_nhl", ("leagueID", "NHL")),
        ("soccer_epl", ("leagueID", "EPL")),
        ("soccer_uefa_champs", ("leagueID", "UEFA_CHAMPIONS_LEAGUE")),
    )
    for prefix, provider_filter in mappings:
        if key.startswith(prefix):
            return provider_filter
    if key.startswith("tennis_") and "itf" not in key:
        return "sportID", "TENNIS"
    if key.startswith("soccer_"):
        return "sportID", "SOCCER"
    return None


def sport_key_for_event(event, requested_sport_key=""):
    requested = str(requested_sport_key or "")
    league = str(event.get("leagueID") or "").upper()
    direct = {
        "MLB": "baseball_mlb",
        "WNBA": "basketball_wnba",
        "NBA": "basketball_nba",
        "NFL": "americanfootball_nfl",
        "NCAAF": "americanfootball_ncaaf",
        "NCAAB": "basketball_ncaab",
        "NHL": "icehockey_nhl",
        "EPL": "soccer_epl",
        "UEFA_CHAMPIONS_LEAGUE": "soccer_uefa_champs_league",
    }
    if league in direct:
        return direct[league]
    if str(event.get("sportID") or "").upper() == "TENNIS" and requested.startswith("tennis_"):
        return requested
    return requested or league.lower()


def event_matches_candidate(event, candidate, max_start_difference_minutes=180.0):
    teams = event.get("teams") or {}
    event_home = _team_name(teams.get("home"))
    event_away = _team_name(teams.get("away"))
    candidate_home = str(candidate.get("home_team") or "")
    candidate_away = str(candidate.get("away_team") or "")
    direct = _name_matches(event_home, candidate_home) and _name_matches(event_away, candidate_away)
    reversed_pair = _name_matches(event_home, candidate_away) and _name_matches(event_away, candidate_home)
    if not (direct or reversed_pair):
        return False
    provider_start = _parse_iso((event.get("status") or {}).get("startsAt"))
    candidate_start = _parse_iso(candidate.get("commence_time"))
    if not provider_start or not candidate_start:
        return True
    difference = abs((provider_start - candidate_start).total_seconds()) / 60.0
    return difference <= max(1.0, float(max_start_difference_minutes))


def _latest_update(event):
    latest = None
    for odd in (event.get("odds") or {}).values():
        for quote in (odd.get("byBookmaker") or {}).values():
            parsed = _parse_iso(quote.get("lastUpdatedAt"))
            if parsed and (latest is None or parsed > latest):
                latest = parsed
    return latest.isoformat(timespec="milliseconds") if latest else None


def normalize_event(event, fetched_at=None, requested_sport_key="", tier="unknown"):
    """Convert one SportsGameOdds event to the existing Odds API game shape."""
    fetched_at = fetched_at or _now_iso()
    teams = event.get("teams") or {}
    home_team = _team_name(teams.get("home"))
    away_team = _team_name(teams.get("away"))
    if not home_team or not away_team:
        return None
    status = event.get("status") or {}
    bookmaker_markets = {}
    fair_anchors = {}
    for odd_id, odd in (event.get("odds") or {}).items():
        if str(odd.get("statID") or "") != "points" or str(odd.get("periodID") or "") != "game":
            continue
        bet_type = str(odd.get("betTypeID") or "")
        side = str(odd.get("sideID") or "").lower()
        if bet_type not in {"ml", "sp", "ou"}:
            continue
        fair_anchors[str(odd_id)] = {
            "fair_odds": _american(odd.get("fairOdds")),
            "fair_odds_available": bool(odd.get("fairOddsAvailable")),
            "fair_spread": _number(odd.get("fairSpread")),
            "fair_over_under": _number(odd.get("fairOverUnder")),
            "book_odds": _american(odd.get("bookOdds")),
            "book_spread": _number(odd.get("bookSpread")),
            "book_over_under": _number(odd.get("bookOverUnder")),
            "open_fair_odds": _american(odd.get("openFairOdds")),
            "open_fair_spread": _number(odd.get("openFairSpread")),
            "open_fair_over_under": _number(odd.get("openFairOverUnder")),
            "open_book_odds": _american(odd.get("openBookOdds")),
            "open_book_spread": _number(odd.get("openBookSpread")),
            "open_book_over_under": _number(odd.get("openBookOverUnder")),
            "close_fair_odds": _american(odd.get("closeFairOdds")),
            "close_fair_spread": _number(odd.get("closeFairSpread")),
            "close_fair_over_under": _number(odd.get("closeFairOverUnder")),
            "close_book_odds": _american(odd.get("closeBookOdds")),
            "close_book_spread": _number(odd.get("closeBookSpread")),
            "close_book_over_under": _number(odd.get("closeBookOverUnder")),
            "market_name": odd.get("marketName"),
            "opposing_odd_id": odd.get("opposingOddID"),
            "score": _number(odd.get("score")),
            "scoring_supported": bool(odd.get("scoringSupported")),
            "started": bool(odd.get("started")),
            "ended": bool(odd.get("ended")),
            "cancelled": bool(odd.get("cancelled")),
        }
        for bookmaker_id, quote in (odd.get("byBookmaker") or {}).items():
            if not quote.get("available"):
                continue
            price = _american(quote.get("odds"))
            if price is None:
                continue
            if bet_type == "ml" and side in {"home", "away"}:
                market_key = "h2h"
                outcome = {"name": home_team if side == "home" else away_team, "price": price}
            elif bet_type == "sp" and side in {"home", "away"}:
                point = _number(quote.get("spread"))
                if point is None:
                    continue
                market_key = "spreads"
                outcome = {
                    "name": home_team if side == "home" else away_team,
                    "price": price,
                    "point": point,
                }
            elif bet_type == "ou" and side in {"over", "under"}:
                point = _number(quote.get("overUnder"))
                if point is None:
                    continue
                market_key = "totals"
                outcome = {"name": side.title(), "price": price, "point": point}
            else:
                continue
            book = bookmaker_markets.setdefault(str(bookmaker_id), {})
            market = book.setdefault(market_key, {"key": market_key, "outcomes": [], "last_update": None})
            market["outcomes"].append(outcome)
            quote_update = quote.get("lastUpdatedAt")
            current_update = _parse_iso(market.get("last_update"))
            parsed_update = _parse_iso(quote_update)
            if parsed_update and (current_update is None or parsed_update > current_update):
                market["last_update"] = quote_update

    bookmakers = []
    for bookmaker_id, markets in bookmaker_markets.items():
        market_rows = list(markets.values())
        last_updates = [_parse_iso(row.get("last_update")) for row in market_rows]
        latest = max((value for value in last_updates if value), default=None)
        bookmakers.append({
            "key": bookmaker_id,
            "title": bookmaker_id,
            "last_update": latest.isoformat(timespec="milliseconds") if latest else None,
            "markets": market_rows,
        })

    scores = [
        {"name": away_team, "score": (teams.get("away") or {}).get("score")},
        {"name": home_team, "score": (teams.get("home") or {}).get("score")},
    ]
    scores = [row for row in scores if row.get("score") not in (None, "")]
    live_context = {
        "source": "sports_game_odds",
        "scores": scores,
        "period": status.get("displayLong") or status.get("displayShort") or status.get("currentPeriodID"),
        "completed": bool(status.get("completed") or status.get("ended") or status.get("finalized")),
        "status": "final" if status.get("completed") or status.get("ended") or status.get("finalized") else "live" if status.get("started") else "scheduled",
        "last_update": _latest_update(event) or fetched_at,
    }
    period_id = str(status.get("currentPeriodID") or "").lower()
    period_number = _number(re.search(r"\d+", period_id).group(0)) if re.search(r"\d+", period_id) else None
    sport_id = str(event.get("sportID") or "").upper()
    if sport_id == "BASEBALL" and period_number is not None:
        live_context["inning"] = period_number
    elif sport_id in {"BASKETBALL", "FOOTBALL", "HOCKEY"} and period_number is not None:
        live_context["period"] = period_number
    elif sport_id == "TENNIS" and period_number is not None:
        live_context["current_set"] = period_number

    return {
        "id": f"sgo:{event.get('eventID')}",
        "sport_key": sport_key_for_event(event, requested_sport_key=requested_sport_key),
        "sport_title": str(event.get("leagueID") or event.get("sportID") or "SportsGameOdds"),
        "commence_time": status.get("startsAt"),
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": bookmakers,
        "scores": scores,
        "completed": live_context["completed"],
        "live_score_context": live_context,
        "premium_live_state": {
            "provider": "sports_game_odds",
            "provider_game_id": event.get("eventID"),
            "fetched_at": fetched_at,
            "home_score": (teams.get("home") or {}).get("score"),
            "away_score": (teams.get("away") or {}).get("score"),
            "status": live_context["status"],
            "period": live_context.get("period"),
            "inning": live_context.get("inning"),
            "current_set": live_context.get("current_set"),
            "in_break": bool(status.get("inBreak")),
            "delayed": bool(status.get("delayed")),
            "hard_start": bool(status.get("hardStart")),
            "odds_available": bool(status.get("oddsAvailable")),
            "odds_present": bool(status.get("oddsPresent")),
            "previous_period": status.get("previousPeriodID"),
            "periods": status.get("periods") or {},
        },
        "_odds_source": "sports_game_odds",
        "_odds_fetched_at": fetched_at,
        "_odds_cache_age_minutes": round(_age_minutes(fetched_at) or 0.0, 2),
        "_sports_game_odds": {
            "event_id": event.get("eventID"),
            "tier": tier,
            "fair_anchors": fair_anchors,
            "status": status,
        },
    }


def fair_anchor_for_candidate(game, candidate):
    """Return the provider's no-vig fair probability for the exact candidate."""
    metadata = (game.get("_sports_game_odds") or {})
    anchors = metadata.get("fair_anchors") or {}
    market_type = str(candidate.get("market_type") or "").lower()
    selected = normalize_text(candidate.get("selected_team") or "")
    home = normalize_text(game.get("home_team") or "")
    away = normalize_text(game.get("away_team") or "")
    odd_id = ""
    if market_type == "moneyline":
        side = "home" if selected == home else "away" if selected == away else ""
        odd_id = f"points-{side}-game-ml-{side}" if side else ""
    elif market_type == "spread":
        side = "home" if selected == home else "away" if selected == away else ""
        odd_id = f"points-{side}-game-sp-{side}" if side else ""
    elif market_type == "total":
        side = normalize_text(candidate.get("total_side") or "")
        odd_id = f"points-all-game-ou-{side}" if side in {"over", "under"} else ""
    anchor = anchors.get(odd_id) or {}
    fair_odds = anchor.get("fair_odds")
    if not anchor.get("fair_odds_available") or fair_odds in (None, 0):
        return {"ok": False, "reason": "fair_odds_unavailable", "odd_id": odd_id}
    if market_type == "spread":
        fair_line = anchor.get("fair_spread")
        market_line = candidate.get("market_line")
        if fair_line is None or market_line is None or abs(float(fair_line) - float(market_line)) > 0.01:
            return {"ok": False, "reason": "fair_spread_line_mismatch", "odd_id": odd_id}
    if market_type == "total":
        fair_line = anchor.get("fair_over_under")
        market_line = candidate.get("market_line")
        if fair_line is None or market_line is None or abs(float(fair_line) - float(market_line)) > 0.01:
            return {"ok": False, "reason": "fair_total_line_mismatch", "odd_id": odd_id}
    probability = american_to_probability(fair_odds)
    if probability is None:
        return {"ok": False, "reason": "fair_odds_invalid", "odd_id": odd_id}
    open_probability = american_to_probability(anchor.get("open_fair_odds"))
    close_probability = american_to_probability(anchor.get("close_fair_odds"))
    return {
        "ok": True,
        "odd_id": odd_id,
        "fair_odds": int(fair_odds),
        "fair_probability": round(probability * 100.0, 3),
        "open_fair_odds": anchor.get("open_fair_odds"),
        "open_fair_probability": round(open_probability * 100.0, 3) if open_probability is not None else None,
        "close_fair_odds": anchor.get("close_fair_odds"),
        "close_fair_probability": round(close_probability * 100.0, 3) if close_probability is not None else None,
        "fair_probability_move_from_open_pp": round((probability - open_probability) * 100.0, 3) if open_probability is not None else None,
        "book_odds": anchor.get("book_odds"),
        "fair_line": anchor.get("fair_spread") if market_type == "spread" else anchor.get("fair_over_under") if market_type == "total" else None,
        "book_line": anchor.get("book_spread") if market_type == "spread" else anchor.get("book_over_under") if market_type == "total" else None,
        "score": anchor.get("score"),
        "scoring_supported": anchor.get("scoring_supported"),
        "started": anchor.get("started"),
        "ended": anchor.get("ended"),
        "cancelled": anchor.get("cancelled"),
    }


def american_to_probability(odds):
    odds = _number(odds)
    if odds in (None, 0):
        return None
    return abs(odds) / (abs(odds) + 100.0) if odds < 0 else 100.0 / (odds + 100.0)


class SportsGameOddsFeed:
    def __init__(
        self,
        api_key,
        *,
        cache_file="sports_game_odds_cache.json",
        usage_refresh_minutes=360.0,
        discovery_cache_minutes=180.0,
        live_cache_minutes=10.0,
        pregame_cache_minutes=30.0,
        discovery_limit=12,
        max_events_per_scan=5,
        max_discovery_groups_per_scan=1,
        utilization_pct=0.92,
        failure_threshold=3,
        cooldown_minutes=15.0,
    ):
        self.api_key = str(api_key or "").strip()
        self.cache_file = Path(cache_file)
        self.usage_refresh_minutes = max(5.0, float(usage_refresh_minutes))
        self.discovery_cache_minutes = max(10.0, float(discovery_cache_minutes))
        self.live_cache_minutes = max(1.0, float(live_cache_minutes))
        self.pregame_cache_minutes = max(1.0, float(pregame_cache_minutes))
        self.discovery_limit = max(1, int(discovery_limit))
        self.max_events_per_scan = max(1, int(max_events_per_scan))
        self.max_discovery_groups_per_scan = max(0, int(max_discovery_groups_per_scan))
        self.utilization_pct = min(1.0, max(0.1, float(utilization_pct)))
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_minutes = max(1.0, float(cooldown_minutes))
        self.state = self._load_state()
        self.scan_status = {
            "enabled": bool(self.api_key),
            "requests": 0,
            "entities": 0,
            "cache_hits": 0,
            "discoveries": 0,
            "direct_refreshes": 0,
            "errors": [],
        }

    def _load_state(self):
        try:
            import json
            state = json.loads(self.cache_file.read_text(encoding="utf-8")) if self.cache_file.exists() else {}
        except Exception:
            state = {}
        state.setdefault("events", {})
        state.setdefault("discoveries", {})
        state.setdefault("daily_spend", {})
        state.setdefault("circuit", {"failures": 0, "open_until": None})
        return state

    def _save(self):
        self.state["updated_at"] = _now_iso()
        write_json(self.cache_file, self.state)

    def _circuit_open(self):
        open_until = _parse_iso((self.state.get("circuit") or {}).get("open_until"))
        return bool(open_until and datetime.now(timezone.utc) < open_until)

    def _success(self):
        self.state["circuit"] = {"failures": 0, "open_until": None}

    def _failure(self, error):
        circuit = self.state.setdefault("circuit", {"failures": 0, "open_until": None})
        circuit["failures"] = int(circuit.get("failures") or 0) + 1
        if circuit["failures"] >= self.failure_threshold:
            circuit["open_until"] = datetime.fromtimestamp(
                time.time() + self.cooldown_minutes * 60.0,
                tz=timezone.utc,
            ).isoformat(timespec="seconds")
        self.scan_status["errors"].append(str(error)[:300])
        self._save()

    def _get(self, path, params=None):
        if not self.api_key:
            raise RuntimeError("sportsgameodds_api_key_missing")
        if self._circuit_open():
            raise RuntimeError("sportsgameodds_circuit_open")
        last_error = None
        for attempt in range(2):
            try:
                payload, headers = get_json(
                    f"{API_BASE}{path}",
                    params=params,
                    headers={
                        "x-api-key": self.api_key,
                        "Accept": "application/json",
                        # Cloudflare blocks urllib's default signature even for
                        # authenticated API traffic. Identify this server-side
                        # integration with a normal, stable HTTP user agent.
                        "User-Agent": "PolymakerSports/1.0 (+https://sportsgameodds.com)",
                    },
                )
                if not isinstance(payload, dict) or payload.get("success") is False or payload.get("error"):
                    raise RuntimeError(str((payload or {}).get("error") or "sportsgameodds_invalid_response"))
                self.scan_status["requests"] += 1
                self._success()
                return payload, headers
            except Exception as exc:
                last_error = exc
                if attempt == 0 and "HTTP 5" in str(exc):
                    time.sleep(0.25)
                    continue
                break
        self._failure(last_error)
        raise last_error

    def refresh_usage(self, force=False):
        cached = self.state.get("usage") or {}
        if not force and _age_minutes(cached.get("fetched_at")) is not None and _age_minutes(cached.get("fetched_at")) < self.usage_refresh_minutes:
            return cached
        try:
            payload, _headers = self._get("/account/usage")
        except Exception:
            return cached
        data = payload.get("data") or {}
        monthly = ((data.get("rateLimits") or {}).get("per-month") or {})
        cached = {
            "fetched_at": _now_iso(),
            "tier": str(data.get("tier") or "unknown").lower(),
            "monthly_max_entities": monthly.get("max-entities"),
            "monthly_current_entities": monthly.get("current-entities"),
            "rate_limits": data.get("rateLimits") or {},
        }
        self.state["usage"] = cached
        self._save()
        return cached

    def _budget_status(self, estimated_entities=1):
        usage = self.refresh_usage()
        maximum = _number(usage.get("monthly_max_entities"))
        current = _number(usage.get("monthly_current_entities")) or 0.0
        if maximum is None:
            return {"allowed": True, "tier": usage.get("tier", "unknown"), "reason": "unlimited"}
        target = math.floor(maximum * self.utilization_pct)
        now = datetime.now(timezone.utc)
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        days_remaining = max(1, days_in_month - now.day + 1)
        remaining = max(0.0, target - current)
        daily_allowance = max(1, math.floor(remaining / days_remaining))
        day_key = now.date().isoformat()
        spent_today = int((self.state.get("daily_spend") or {}).get(day_key) or 0)
        allowed = remaining >= estimated_entities and spent_today + estimated_entities <= daily_allowance
        return {
            "allowed": allowed,
            "reason": "ok" if allowed else "adaptive_monthly_pacing",
            "tier": usage.get("tier", "unknown"),
            "monthly_max_entities": int(maximum),
            "monthly_current_entities": int(current),
            "monthly_target_entities": int(target),
            "monthly_remaining_to_target": int(remaining),
            "daily_allowance": int(daily_allowance),
            "spent_today": spent_today,
        }

    def _record_entities(self, count):
        count = max(0, int(count or 0))
        day_key = datetime.now(timezone.utc).date().isoformat()
        daily = self.state.setdefault("daily_spend", {})
        daily[day_key] = int(daily.get(day_key) or 0) + count
        self.scan_status["entities"] += count
        usage = self.state.get("usage") or {}
        current = _number(usage.get("monthly_current_entities"))
        if current is not None:
            usage["monthly_current_entities"] = int(current) + count
        self._save()

    def _event_entry(self, event_id):
        return (self.state.get("events") or {}).get(str(event_id)) or {}

    def _store_events(self, events, requested_sport_key=""):
        fetched_at = _now_iso()
        for event in events:
            event_id = str(event.get("eventID") or "")
            if not event_id:
                continue
            self.state.setdefault("events", {})[event_id] = {
                "fetched_at": fetched_at,
                "requested_sport_key": requested_sport_key,
                "event": event,
            }
        self._record_entities(len(events))

    def _fetch_events(self, params, estimated_entities, requested_sport_key=""):
        budget = self._budget_status(estimated_entities)
        self.scan_status["budget"] = budget
        if not budget.get("allowed"):
            return []
        payload, _headers = self._get("/events", params=params)
        events = list(payload.get("data") or [])
        self._store_events(events, requested_sport_key=requested_sport_key)
        self.scan_status["budget"] = self._budget_status(1)
        return events

    def _discover(self, provider_filter, requested_sport_key):
        filter_name, filter_value = provider_filter
        discovery_key = f"{filter_name}:{filter_value}"
        cached = (self.state.get("discoveries") or {}).get(discovery_key) or {}
        if _age_minutes(cached.get("fetched_at")) is not None and _age_minutes(cached.get("fetched_at")) < self.discovery_cache_minutes:
            return []
        params = {
            filter_name: filter_value,
            "oddsAvailable": "true",
            "oddID": ",".join(MAIN_ODD_IDS),
            "includeOpposingOdds": "true",
            "limit": self.discovery_limit,
        }
        events = self._fetch_events(params, self.discovery_limit, requested_sport_key=requested_sport_key)
        if events:
            self.state.setdefault("discoveries", {})[discovery_key] = {
                "fetched_at": _now_iso(),
                "event_ids": [event.get("eventID") for event in events if event.get("eventID")],
            }
            self.scan_status["discoveries"] += 1
            self._save()
        return events

    def _matching_entry(self, candidate):
        matches = []
        for event_id, entry in (self.state.get("events") or {}).items():
            event = entry.get("event") or {}
            if not event_matches_candidate(event, candidate):
                continue
            provider_start = _parse_iso((event.get("status") or {}).get("startsAt"))
            candidate_start = _parse_iso(candidate.get("commence_time"))
            difference = abs((provider_start - candidate_start).total_seconds()) if provider_start and candidate_start else 0.0
            matches.append((difference, event_id, entry))
        return min(matches, default=(None, None, None), key=lambda row: row[0] if row[0] is not None else 0)[2]

    def _refresh_entry(self, entry, candidate):
        if not entry:
            return None
        age = _age_minutes(
            _latest_update(entry.get("event") or {})
            if candidate.get("game_started")
            else entry.get("fetched_at")
        )
        max_age = self.live_cache_minutes if candidate.get("game_started") else self.pregame_cache_minutes
        if age is not None and age <= max_age:
            self.scan_status["cache_hits"] += 1
            return entry
        event_id = str((entry.get("event") or {}).get("eventID") or "")
        if not event_id:
            return entry
        events = self._fetch_events(
            {
                "eventID": event_id,
                "oddID": ",".join(MAIN_ODD_IDS),
                "includeOpposingOdds": "true",
            },
            1,
            requested_sport_key=str(candidate.get("sport_key") or ""),
        )
        if events:
            self.scan_status["direct_refreshes"] += 1
            return self._event_entry(event_id)
        return entry

    def games_for_candidates(self, candidates):
        self.scan_status.update({
            "requests": 0,
            "entities": 0,
            "cache_hits": 0,
            "discoveries": 0,
            "direct_refreshes": 0,
            "errors": [],
        })
        if not self.api_key:
            self.scan_status["reason"] = "api_key_missing"
            return [], self.status()
        self.refresh_usage()
        prioritized = sorted(
            list(candidates or []),
            key=lambda row: (
                1 if row.get("game_started") else 0,
                float(row.get("edge") if row.get("edge") is not None else -999),
                float(row.get("kalshi_volume") or 0),
            ),
            reverse=True,
        )
        unique_candidates = []
        seen_games = set()
        for candidate in prioritized:
            game_key = str(candidate.get("game_key") or candidate.get("event_id") or "")
            if not game_key or game_key in seen_games or not provider_filter_for_sport(candidate.get("sport_key")):
                continue
            seen_games.add(game_key)
            unique_candidates.append(candidate)
            if len(unique_candidates) >= self.max_events_per_scan:
                break

        discovery_groups_used = set()
        entries = []
        for candidate in unique_candidates:
            entry = self._matching_entry(candidate)
            provider_filter = provider_filter_for_sport(candidate.get("sport_key"))
            if not entry and provider_filter and len(discovery_groups_used) < self.max_discovery_groups_per_scan:
                group_key = ":".join(provider_filter)
                if group_key not in discovery_groups_used:
                    discovery_groups_used.add(group_key)
                    self._discover(provider_filter, str(candidate.get("sport_key") or ""))
                entry = self._matching_entry(candidate)
            if not entry:
                continue
            entry = self._refresh_entry(entry, candidate)
            if entry:
                entries.append(entry)

        games = []
        seen_event_ids = set()
        tier = (self.state.get("usage") or {}).get("tier", "unknown")
        for entry in entries:
            event = entry.get("event") or {}
            event_id = str(event.get("eventID") or "")
            if not event_id or event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            game = normalize_event(
                event,
                fetched_at=entry.get("fetched_at"),
                requested_sport_key=entry.get("requested_sport_key") or "",
                tier=tier,
            )
            if game:
                games.append(game)
        self._save()
        return games, self.status()

    def status(self):
        usage = self.state.get("usage") or {}
        budget = (self.scan_status.get("budget") or self._budget_status(1)) if self.api_key else {}
        return {
            **self.scan_status,
            "tier": usage.get("tier", "unknown"),
            "usage": usage,
            "budget": budget,
            "cached_events": len(self.state.get("events") or {}),
            "circuit": self.state.get("circuit") or {},
        }
