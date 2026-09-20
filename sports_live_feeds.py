"""Optional low-latency play-by-play enrichment.

The paid Sportradar adapter is retained, but it is no longer the only way to
obtain sport-specific progress.  ``PublicScoreboardFeed`` uses cached public
scoreboards for just the actionable games.  The public adapter deliberately
does not scrape rendered web pages: structured scoreboards are faster, easier
to validate, and much less likely to turn page text into a false game state.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re
import time

from kalshi_common import get_json, normalize_text


SUPPORTED_SPORTRADAR_LEAGUES = {
    "baseball_mlb": "mlb",
    "basketball_nba": "nba",
    "basketball_nba_summer_league": "nba",
    "basketball_wnba": "wnba",
}

# Sportradar versions and endpoint names are league-specific.  Keep these
# explicit so a global MLB upgrade cannot silently break NBA/WNBA requests.
SPORTRADAR_LEAGUE_API = {
    "mlb": {"version": "v8", "play_by_play": "play_by_play"},
    "nba": {"version": "v7", "play_by_play": "pbp"},
    "wnba": {"version": "v8", "play_by_play": "pbp"},
}


# ESPN exposes the same state shown on its public scoreboards.  This is an
# undocumented public endpoint, so every request is cached and protected by a
# circuit breaker.  Adding a sport here is fail-safe: a row is accepted only
# after both competitors match the Odds API event.
PUBLIC_SCOREBOARD_LEAGUES = {
    "baseball_mlb": ("baseball", "mlb"),
    "basketball_nba": ("basketball", "nba"),
    "basketball_nba_summer_league": ("basketball", "nba-summer-league"),
    "basketball_wnba": ("basketball", "wnba"),
    "basketball_ncaab": ("basketball", "mens-college-basketball"),
    "americanfootball_nfl": ("football", "nfl"),
    "americanfootball_nfl_preseason": ("football", "nfl"),
    "americanfootball_ncaaf": ("football", "college-football"),
    "icehockey_nhl": ("hockey", "nhl"),
    "tennis_atp": ("tennis", "atp"),
    "tennis_wta": ("tennis", "wta"),
    "soccer_epl": ("soccer", "eng.1"),
    "soccer_usa_mls": ("soccer", "usa.1"),
    "soccer_uefa_champs_league": ("soccer", "uefa.champions"),
    "soccer_brazil_campeonato": ("soccer", "bra.1"),
    "soccer_brazil_serie_b": ("soccer", "bra.2"),
    "soccer_china_superleague": ("soccer", "chn.1"),
    "soccer_finland_veikkausliiga": ("soccer", "fin.1"),
    "soccer_korea_kleague1": ("soccer", "kor.1"),
    "soccer_mexico_ligamx": ("soccer", "mex.1"),
    "soccer_norway_eliteserien": ("soccer", "nor.1"),
    "soccer_sweden_allsvenskan": ("soccer", "swe.1"),
}

PUBLIC_SCOREBOARD_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.espn.com/",
    "User-Agent": "Mozilla/5.0 (compatible; PolymakerLiveState/1.0)",
}


def public_scoreboard_route(sport_key):
    key = str(sport_key or "")
    if key.startswith("tennis_atp"):
        return "tennis", "atp"
    if key.startswith("tennis_wta"):
        return "tennis", "wta"
    if key.startswith("americanfootball_nfl"):
        return "football", "nfl"
    return PUBLIC_SCOREBOARD_LEAGUES.get(key)


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _team_label(row):
    if not isinstance(row, dict):
        return ""
    return " ".join(
        str(row.get(key) or "").strip()
        for key in ("market", "name")
        if str(row.get(key) or "").strip()
    ) or str(row.get("alias") or "")


def _schedule_games(payload):
    if not isinstance(payload, dict):
        return []
    for path in (
        payload.get("games"),
        (payload.get("schedule") or {}).get("games") if isinstance(payload.get("schedule"), dict) else None,
    ):
        if isinstance(path, list):
            return path
    return []


def _teams_match(provider_game, game):
    provider_home = normalize_text(_team_label(provider_game.get("home") or {}))
    provider_away = normalize_text(_team_label(provider_game.get("away") or {}))
    home = normalize_text(game.get("home_team"))
    away = normalize_text(game.get("away_team"))

    def similar(left, right):
        return bool(left and right and (left == right or left in right or right in left))

    return similar(provider_home, home) and similar(provider_away, away)


def _last_row(value):
    return value[-1] if isinstance(value, list) and value else {}


def extract_provider_state(payload, sport_key):
    """Reduce a large play-by-play response to model-ready, stable fields."""
    root = payload.get("game") if isinstance(payload, dict) and isinstance(payload.get("game"), dict) else payload
    root = root if isinstance(root, dict) else {}
    home = root.get("home") if isinstance(root.get("home"), dict) else {}
    away = root.get("away") if isinstance(root.get("away"), dict) else {}
    state = {
        "provider": "sportradar",
        "sport_key": sport_key,
        "provider_game_id": root.get("id"),
        "status": root.get("status"),
        "clock": root.get("clock"),
        "period": root.get("quarter") or root.get("period"),
        "home_score": home.get("points") if home.get("points") is not None else home.get("runs"),
        "away_score": away.get("points") if away.get("points") is not None else away.get("runs"),
        "updated_at": root.get("updated") or root.get("updated_at"),
    }
    if str(sport_key).startswith("baseball"):
        innings = root.get("innings") if isinstance(root.get("innings"), list) else []
        latest_inning = _last_row(innings)
        events = root.get("events") if isinstance(root.get("events"), list) else []
        latest_event = _last_row(events)
        at_bat = latest_event.get("at_bat") if isinstance(latest_event, dict) and isinstance(latest_event.get("at_bat"), dict) else {}
        state.update(
            {
                "inning": root.get("inning") or latest_inning.get("number") or at_bat.get("inning"),
                "inning_half": root.get("inning_half") or latest_inning.get("half") or at_bat.get("half"),
                "outs": root.get("outs") if root.get("outs") is not None else at_bat.get("outs"),
                "balls": at_bat.get("balls"),
                "strikes": at_bat.get("strikes"),
                "pitch_count": (
                    ((latest_event.get("pitch") or {}).get("pitcher") or {}).get("pitch_count")
                    if isinstance(latest_event, dict)
                    else None
                ),
                "latest_event_id": latest_event.get("id") if isinstance(latest_event, dict) else None,
                "latest_event_type": latest_event.get("type") if isinstance(latest_event, dict) else None,
            }
        )
    elif str(sport_key).startswith("basketball"):
        periods = root.get("quarters") if isinstance(root.get("quarters"), list) else root.get("periods")
        latest_period = _last_row(periods)
        plays = root.get("plays") if isinstance(root.get("plays"), list) else []
        latest_play = _last_row(plays)
        state.update(
            {
                "period": state.get("period") or latest_period.get("number"),
                "clock": state.get("clock") or latest_play.get("clock"),
                "possession": root.get("possession") or latest_play.get("possession"),
                "home_timeouts": home.get("remaining_timeouts") or home.get("timeouts"),
                "away_timeouts": away.get("remaining_timeouts") or away.get("timeouts"),
                "home_fouls": home.get("team_fouls") or home.get("fouls"),
                "away_fouls": away.get("team_fouls") or away.get("fouls"),
                "latest_play_id": latest_play.get("id") if isinstance(latest_play, dict) else None,
                "latest_play_type": latest_play.get("event_type") if isinstance(latest_play, dict) else None,
            }
        )
    elif str(sport_key).startswith(("americanfootball", "aussierules", "lacrosse")):
        periods = root.get("quarters") if isinstance(root.get("quarters"), list) else root.get("periods")
        latest_period = _last_row(periods)
        plays = root.get("plays") if isinstance(root.get("plays"), list) else []
        latest_play = _last_row(plays)
        state.update(
            {
                "quarter": root.get("quarter") or state.get("period") or latest_period.get("number"),
                "clock": state.get("clock") or latest_play.get("clock"),
                "possession": root.get("possession") or latest_play.get("possession"),
                "down": root.get("down") or latest_play.get("down"),
                "distance": root.get("distance") or latest_play.get("distance"),
            }
        )
    elif str(sport_key).startswith("icehockey"):
        periods = root.get("periods") if isinstance(root.get("periods"), list) else []
        latest_period = _last_row(periods)
        state.update(
            {
                "period": state.get("period") or latest_period.get("number"),
                "clock": state.get("clock") or root.get("time_remaining"),
                "strength": root.get("strength"),
            }
        )
    elif str(sport_key).startswith("soccer"):
        state.update(
            {
                "half": root.get("half") or root.get("period"),
                "match_minute": root.get("match_minute") or root.get("minute") or root.get("elapsed"),
                "stoppage_time": root.get("stoppage_time") or root.get("added_time"),
            }
        )
    elif str(sport_key).startswith(("mma_", "boxing_")):
        state.update(
            {
                "round": root.get("round") or root.get("period"),
                "round_clock": root.get("round_clock") or root.get("clock"),
            }
        )
    return {key: value for key, value in state.items() if value is not None}


def _espn_competitions(payload):
    """Flatten normal team events and tennis tournament groupings."""
    rows = []
    for event in (payload or {}).get("events") or []:
        for competition in event.get("competitions") or []:
            rows.append((event, competition))
        for grouping in event.get("groupings") or []:
            for competition in grouping.get("competitions") or []:
                rows.append((event, competition))
    return rows


def _espn_competitor_name(row):
    if not isinstance(row, dict):
        return ""
    entity = row.get("team") or row.get("athlete") or row.get("roster") or {}
    return str(
        entity.get("displayName")
        or entity.get("fullName")
        or entity.get("shortDisplayName")
        or entity.get("name")
        or ""
    )


def _similar_team(left, right):
    left = normalize_text(left)
    right = normalize_text(right)
    return bool(left and right and (left == right or left in right or right in left))


def _match_espn_competition(game, competition):
    competitors = competition.get("competitors") or []
    if len(competitors) != 2:
        return None
    wanted_home = game.get("home_team")
    wanted_away = game.get("away_team")
    matched = {}
    for competitor in competitors:
        label = _espn_competitor_name(competitor)
        if _similar_team(label, wanted_home):
            matched["home"] = competitor
        elif _similar_team(label, wanted_away):
            matched["away"] = competitor
    if set(matched) != {"home", "away"}:
        return None
    return matched


def _score_value(row):
    value = row.get("score") if isinstance(row, dict) else None
    if isinstance(value, dict):
        value = value.get("value") or value.get("displayValue")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def _status_type(status):
    return status.get("type") if isinstance(status, dict) and isinstance(status.get("type"), dict) else {}


def _set_values(competitor):
    values = []
    for row in competitor.get("linescores") or []:
        try:
            values.append(int(float(row.get("value"))))
        except (TypeError, ValueError):
            values.append(None)
    return values


def _sets_won(left, right, current_set, completed):
    won = 0
    count = max(len(left), len(right))
    for index in range(count):
        if not completed and current_set and index == int(current_set) - 1:
            continue
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if a is not None and b is not None and a > b:
            won += 1
    return won


def extract_espn_state(event, competition, matched, sport_key, fetched_at=None):
    """Convert one verified ESPN competition to the bot's compact schema."""
    status = competition.get("status") or event.get("status") or {}
    status_type = _status_type(status)
    situation = competition.get("situation") or {}
    period = status.get("period")
    detail = str(status_type.get("detail") or status_type.get("shortDetail") or "")
    home = matched["home"]
    away = matched["away"]
    state = {
        "provider": "espn_scoreboard",
        "provider_game_id": competition.get("id") or event.get("id"),
        "sport_key": sport_key,
        "status": status_type.get("state") or status_type.get("description"),
        "completed": bool(status_type.get("completed")),
        "home_score": _score_value(home),
        "away_score": _score_value(away),
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        # The event/team match and explicit provider status have been checked.
        # game_state_features still applies freshness and field-completeness.
        "verified_progress_source": True,
    }
    if sport_key.startswith("baseball"):
        detail_lower = detail.lower()
        half = (
            "top"
            if "top" in detail_lower
            else "bottom"
            if any(token in detail_lower for token in ("bot", "mid", "middle", "end"))
            else ""
        )
        state.update({
            "inning": period,
            "inning_half": half,
            "outs": situation.get("outs"),
            "balls": situation.get("balls"),
            "strikes": situation.get("strikes"),
            "latest_event_id": (situation.get("lastPlay") or {}).get("id"),
            "latest_event_type": ((situation.get("lastPlay") or {}).get("type") or {}).get("text"),
        })
    elif sport_key.startswith("basketball"):
        possessing = next((row for row in (home, away) if row.get("possession")), None)
        state.update({
            "period": period,
            "clock": status.get("displayClock"),
            "possession": _espn_competitor_name(possessing) if possessing else situation.get("possession"),
        })
    elif sport_key.startswith("americanfootball"):
        possessing = next((row for row in (home, away) if row.get("possession")), None)
        state.update({
            "quarter": period,
            "clock": status.get("displayClock"),
            "possession": _espn_competitor_name(possessing) if possessing else situation.get("possession"),
            "down": situation.get("down"),
            "distance": situation.get("distance"),
        })
    elif sport_key.startswith("icehockey"):
        state.update({"period": period, "clock": status.get("displayClock")})
    elif sport_key.startswith("soccer"):
        display_clock = str(status.get("displayClock") or detail)
        minute_match = re.search(r"(\d{1,3})", display_clock)
        state.update({
            "half": period,
            "match_minute": int(minute_match.group(1)) if minute_match else None,
        })
    elif sport_key.startswith("tennis"):
        home_sets = _set_values(home)
        away_sets = _set_values(away)
        current_set = int(period) if period not in (None, "") else max(len(home_sets), len(away_sets))
        completed = bool(status_type.get("completed"))
        server = next((
            _espn_competitor_name(row)
            for row in (home, away)
            if row.get("possession") is True
        ), "")
        format_row = competition.get("format") if isinstance(competition.get("format"), dict) else {}
        tournament = event.get("tournament") if isinstance(event.get("tournament"), dict) else {}
        venue = competition.get("venue") if isinstance(competition.get("venue"), dict) else {}
        home_point_score = (
            home.get("gameScore")
            or home.get("pointScore")
            or situation.get("homePointScore")
            or situation.get("homeScore")
        )
        away_point_score = (
            away.get("gameScore")
            or away.get("pointScore")
            or situation.get("awayPointScore")
            or situation.get("awayScore")
        )
        current_home_games = home_sets[current_set - 1] if current_set and len(home_sets) >= current_set else None
        current_away_games = away_sets[current_set - 1] if current_set and len(away_sets) >= current_set else None
        tiebreak = bool(
            situation.get("tiebreak")
            or situation.get("isTiebreak")
            or "tiebreak" in detail.lower()
            or (
                current_home_games is not None
                and current_home_games == current_away_games
                and current_home_games >= 6
            )
        )
        state.update({
            "current_set": current_set,
            "server": server,
            "home_set_scores": home_sets,
            "away_set_scores": away_sets,
            "home_sets_won": _sets_won(home_sets, away_sets, current_set, completed),
            "away_sets_won": _sets_won(away_sets, home_sets, current_set, completed),
            "home_current_games": current_home_games,
            "away_current_games": current_away_games,
            "home_point_score": home_point_score,
            "away_point_score": away_point_score,
            "break_point": situation.get("breakPoint") or situation.get("isBreakPoint"),
            "tiebreak": tiebreak,
            "surface": (
                tournament.get("surface")
                or venue.get("surface")
                or competition.get("surface")
            ),
            "best_of": (
                format_row.get("bestOf")
                or format_row.get("best_of")
                or competition.get("bestOf")
            ),
            "match_format": (
                format_row.get("name")
                or format_row.get("displayName")
                or competition.get("type")
            ),
            "set_score": f"{home_sets}-{away_sets}",
            "match_status": status_type.get("detail") or status_type.get("description"),
        })
    return {key: value for key, value in state.items() if value not in (None, "")}


class PublicScoreboardFeed:
    """Cached, no-key live state for actionable games.

    One scoreboard request covers every actionable game in a sport.  Failed or
    structurally ambiguous matches return no enrichment, allowing the existing
    Odds API score fallback to remain in control.
    """

    def __init__(self, *, cache_seconds=20, failure_threshold=3, circuit_cooldown_seconds=300):
        self.api_key = "public"
        self.cache_seconds = max(5.0, float(cache_seconds))
        self.failure_threshold = max(1, int(failure_threshold))
        self.circuit_cooldown_seconds = max(1.0, float(circuit_cooldown_seconds))
        self._cache = {}
        self._request_count = 0
        self._error_count = 0
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._last_error = ""

    def circuit_open(self):
        if self._circuit_open_until and time.monotonic() < self._circuit_open_until:
            return True
        if self._circuit_open_until:
            self._circuit_open_until = 0.0
            self._consecutive_failures = 0
        return False

    def _record_failure(self, exc):
        self._error_count += 1
        self._consecutive_failures += 1
        self._last_error = f"{type(exc).__name__}: {exc}"[:300]
        if self._consecutive_failures >= self.failure_threshold:
            self._circuit_open_until = time.monotonic() + self.circuit_cooldown_seconds

    def _record_success(self):
        self._consecutive_failures = 0
        self._last_error = ""

    def status(self):
        circuit_open = self.circuit_open()
        return {
            "provider": "public_scoreboards",
            "enabled": True,
            "requires_api_key": False,
            "supported_sports": sorted(PUBLIC_SCOREBOARD_LEAGUES),
            "request_count": self._request_count,
            "error_count": self._error_count,
            "cache_entries": len(self._cache),
            "cache_seconds": self.cache_seconds,
            "consecutive_failures": self._consecutive_failures,
            "circuit_open": circuit_open,
            "circuit_retry_after_seconds": round(max(0.0, self._circuit_open_until - time.monotonic()), 1) if circuit_open else 0.0,
            "last_error": self._last_error,
        }

    def supports(self, sport_key):
        return bool(public_scoreboard_route(sport_key))

    def _scoreboard(self, sport_key, day):
        route = public_scoreboard_route(sport_key)
        if not route or self.circuit_open():
            return {}
        key = (route, day.strftime("%Y%m%d"))
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] <= self.cache_seconds:
            return cached[1]
        url = (
            f"https://site.web.api.espn.com/apis/site/v2/sports/{route[0]}/{route[1]}/"
            f"scoreboard?dates={day:%Y%m%d}&limit=1000"
        )
        try:
            payload, _headers = get_json(url, headers=PUBLIC_SCOREBOARD_HEADERS)
            self._request_count += 1
            self._record_success()
            self._cache[key] = (time.monotonic(), payload)
            return payload
        except Exception as exc:
            self._request_count += 1
            self._record_failure(exc)
            raise

    def play_by_play(self, game):
        sport_key = str(game.get("sport_key") or "")
        commence = _parse_time(game.get("commence_time")) or datetime.now(timezone.utc)
        # Tennis scoreboards are tournament-oriented and can span many days;
        # querying today's board returns the active tournament groupings.
        day = datetime.now(timezone.utc).date() if sport_key.startswith("tennis") else commence.date()
        payload = self._scoreboard(sport_key, day)
        matches = []
        for event, competition in _espn_competitions(payload):
            matched = _match_espn_competition(game, competition)
            if not matched:
                continue
            scheduled = _parse_time(competition.get("date") or event.get("date"))
            distance = abs((scheduled - commence).total_seconds()) if scheduled else float("inf")
            matches.append((distance, event, competition, matched))
        if not matches:
            return {}
        _distance, event, competition, matched = min(matches, key=lambda row: row[0])
        return extract_espn_state(event, competition, matched, sport_key)


class SportradarFeed:
    def __init__(
        self,
        api_key,
        *,
        access_level="trial",
        version="v8",
        base_url="https://api.sportradar.com",
        min_request_interval_seconds=None,
        failure_threshold=3,
        circuit_cooldown_seconds=900,
    ):
        self.api_key = str(api_key or "")
        self.access_level = str(access_level or "trial")
        self.version = str(version or "v8")
        self.base_url = str(base_url or "https://api.sportradar.com").rstrip("/")
        self.min_request_interval_seconds = (
            max(0.0, float(min_request_interval_seconds))
            if min_request_interval_seconds is not None
            else (2.0 if self.access_level == "trial" else 0.0)
        )
        self._schedule_cache = {}
        self._event_ids = {}
        self._last_request_monotonic = 0.0
        self._request_count = 0
        self._error_count = 0
        self._rate_limit_count = 0
        self.failure_threshold = max(1, int(failure_threshold))
        self.circuit_cooldown_seconds = max(1.0, float(circuit_cooldown_seconds))
        self._consecutive_failures = 0
        self._circuit_open_until_monotonic = 0.0
        self._circuit_opened_at = ""
        self._last_error = ""

    def _league_api(self, league):
        configured = dict(SPORTRADAR_LEAGUE_API.get(league) or {})
        configured.setdefault("version", self.version)
        configured.setdefault("play_by_play", "pbp")
        return configured

    def circuit_open(self):
        if self._circuit_open_until_monotonic <= 0:
            return False
        if time.monotonic() < self._circuit_open_until_monotonic:
            return True
        # Cooldown expiry permits one provider probe. A successful response
        # closes the circuit; another failure opens it for a fresh cooldown.
        self._circuit_open_until_monotonic = 0.0
        self._consecutive_failures = 0
        return False

    def _record_failure(self, exc):
        self._error_count += 1
        self._consecutive_failures += 1
        self._last_error = f"{type(exc).__name__}: {exc}"[:300]
        if self._consecutive_failures >= self.failure_threshold:
            self._circuit_open_until_monotonic = (
                time.monotonic() + self.circuit_cooldown_seconds
            )
            self._circuit_opened_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )

    def _record_success(self):
        self._consecutive_failures = 0
        self._circuit_open_until_monotonic = 0.0
        self._last_error = ""

    def status(self):
        circuit_open = self.circuit_open()
        remaining = (
            max(0.0, self._circuit_open_until_monotonic - time.monotonic())
            if circuit_open
            else 0.0
        )
        return {
            "provider": "sportradar",
            "enabled": bool(self.api_key),
            "requires_api_key": not bool(self.api_key),
            "supported_sports": sorted(SUPPORTED_SPORTRADAR_LEAGUES),
            "league_api": SPORTRADAR_LEAGUE_API,
            "mapped_events": len(self._event_ids),
            "schedule_cache_entries": len(self._schedule_cache),
            "request_count": self._request_count,
            "error_count": self._error_count,
            "rate_limit_count": self._rate_limit_count,
            "min_request_interval_seconds": self.min_request_interval_seconds,
            "failure_threshold": self.failure_threshold,
            "consecutive_failures": self._consecutive_failures,
            "circuit_open": circuit_open,
            "circuit_opened_at": self._circuit_opened_at,
            "circuit_retry_after_seconds": round(remaining, 1),
            "circuit_cooldown_seconds": self.circuit_cooldown_seconds,
            "last_error": self._last_error,
        }

    def _get(self, url):
        if self.circuit_open():
            raise RuntimeError("sportradar_circuit_open")
        for attempt in range(2):
            elapsed = time.monotonic() - self._last_request_monotonic
            remaining = self.min_request_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
            try:
                payload, headers = get_json(
                    url,
                    headers={
                        "Accept": "application/json",
                        "x-api-key": self.api_key,
                    },
                )
            except Exception as exc:
                self._last_request_monotonic = time.monotonic()
                self._request_count += 1
                if "HTTP 429" in str(exc) and attempt == 0:
                    self._rate_limit_count += 1
                    time.sleep(max(2.0, self.min_request_interval_seconds * 1.5))
                    continue
                self._record_failure(exc)
                raise
            self._last_request_monotonic = time.monotonic()
            self._request_count += 1
            self._record_success()
            return payload, headers
        raise RuntimeError("sportradar_request_retry_exhausted")

    def _schedule(self, league, day):
        key = (league, day.isoformat())
        if key in self._schedule_cache:
            return self._schedule_cache[key]
        api = self._league_api(league)
        url = (
            f"{self.base_url}/{league}/{self.access_level}/{api['version']}/en/"
            f"games/{day.year:04d}/{day.month:02d}/{day.day:02d}/schedule.json"
        )
        payload, _headers = self._get(url)
        rows = _schedule_games(payload)
        self._schedule_cache[key] = rows
        return rows

    def event_id(self, game):
        local_id = str(game.get("id") or "")
        if local_id in self._event_ids:
            return self._event_ids[local_id]
        league = SUPPORTED_SPORTRADAR_LEAGUES.get(str(game.get("sport_key") or ""))
        commence = _parse_time(game.get("commence_time"))
        if not league or not commence:
            return ""
        matches = []
        for day_delta in (-1, 0, 1):
            day = commence.date() + timedelta(days=day_delta)
            for row in self._schedule(league, day):
                if _teams_match(row, game):
                    event_id = str(row.get("id") or "")
                    if event_id:
                        scheduled = _parse_time(
                            row.get("scheduled")
                            or row.get("commence_time")
                            or row.get("start_time")
                        )
                        distance = (
                            abs((scheduled - commence).total_seconds())
                            if scheduled is not None
                            else float("inf")
                        )
                        matches.append((distance, event_id))
        if matches:
            event_id = min(matches, key=lambda row: row[0])[1]
            self._event_ids[local_id] = event_id
            return event_id
        return ""

    def play_by_play(self, game):
        league = SUPPORTED_SPORTRADAR_LEAGUES.get(str(game.get("sport_key") or ""))
        event_id = self.event_id(game)
        if not league or not event_id:
            return {}
        api = self._league_api(league)
        url = (
            f"{self.base_url}/{league}/{self.access_level}/{api['version']}/en/"
            f"games/{event_id}/{api['play_by_play']}.json"
        )
        payload, _headers = self._get(url)
        state = extract_provider_state(payload, game.get("sport_key"))
        state["provider_game_id"] = event_id
        state["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        return state


def enrich_games_with_live_feed(games, feed, *, max_games=20):
    if not feed or not feed.api_key:
        return list(games or []), {
            **(feed.status() if feed else {"provider": "disabled", "enabled": False}),
            "requested_games": 0,
            "enriched_games": 0,
            "errors": [],
        }
    if feed.circuit_open():
        return list(games or []), {
            **feed.status(),
            "requested_games": 0,
            "enriched_games": 0,
            "errors": [],
            "fallback_reason": f"{feed.status().get('provider', 'live_data')}_circuit_open",
        }
    enriched = []
    errors = []
    requested = 0
    enriched_count = 0
    for game in games or []:
        row = dict(game)
        commence = _parse_time(row.get("commence_time"))
        started = bool(commence and commence <= datetime.now(timezone.utc))
        supported_sports = set(feed.status().get("supported_sports") or [])
        supports = getattr(feed, "supports", lambda sport_key: sport_key in supported_sports)
        if (
            started
            and requested < max(1, int(max_games))
            and supports(row.get("sport_key"))
            and not feed.circuit_open()
        ):
            requested += 1
            try:
                state = feed.play_by_play(row)
                if state:
                    row["premium_live_state"] = state
                    enriched_count += 1
            except Exception as exc:
                errors.append(
                    {
                        "event_id": row.get("id"),
                        "sport_key": row.get("sport_key"),
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
        enriched.append(row)
    return enriched, {
        **feed.status(),
        "requested_games": requested,
        "enriched_games": enriched_count,
        "errors": errors[:20],
    }
