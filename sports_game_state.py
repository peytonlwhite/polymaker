"""Sport-specific live-state feature extraction and shadow probabilities."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from kalshi_common import normalize_text


GAME_STATE_MODEL_VERSION = "sport-state-shadow-v2"
GAME_STATE_PROVIDER_MAX_AGE_SECONDS = 180.0


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _logit(probability):
    probability = max(0.001, min(0.999, float(probability)))
    return math.log(probability / (1.0 - probability))


def _logistic(value):
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))


def _score_map(game):
    context = game.get("live_score_context") or {}
    scores = context.get("scores") if isinstance(context, dict) else None
    scores = scores or game.get("scores") or []
    result = {}
    for row in scores:
        if not isinstance(row, dict):
            continue
        score = _number(row.get("score"))
        name = normalize_text(row.get("name"))
        if name and score is not None:
            result[name] = score
    return result


def _clock_minutes(value):
    if value is None:
        return None
    text = str(value)
    match = re.search(r"(?P<minutes>\d{1,2}):(?P<seconds>\d{2})", text)
    if not match:
        return _number(value)
    return int(match.group("minutes")) + int(match.group("seconds")) / 60.0


def _selected_score_diff(game, candidate, provider_state):
    selected = normalize_text(candidate.get("selected_team"))
    home = normalize_text(game.get("home_team"))
    away = normalize_text(game.get("away_team"))
    scores = _score_map(game)
    home_score = _number(provider_state.get("home_score"), scores.get(home))
    away_score = _number(provider_state.get("away_score"), scores.get(away))
    if home_score is None or away_score is None:
        return None
    if selected == home:
        return home_score - away_score
    if selected == away:
        return away_score - home_score
    return None


def _game_score_values(game, provider_state):
    """Return both team scores even when the candidate is a total market."""
    home = normalize_text(game.get("home_team"))
    away = normalize_text(game.get("away_team"))
    scores = _score_map(game)
    home_score = _number(provider_state.get("home_score"), scores.get(home))
    away_score = _number(provider_state.get("away_score"), scores.get(away))
    return home_score, away_score


def _provider_age_seconds(provider):
    value = provider.get("fetched_at") if isinstance(provider, dict) else None
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def revalidate_cached_game_state(features, *, elapsed_seconds=0.0, now=None):
    """Advance cached provider/score ages before a fast execution recheck."""
    row = dict(features or {})
    if not row:
        return row
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    def advanced_age(timestamp_key, age_key):
        timestamp_age = None
        timestamp = row.get(timestamp_key)
        if timestamp:
            try:
                parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                timestamp_age = max(
                    0.0,
                    (
                        now.astimezone(timezone.utc)
                        - parsed.astimezone(timezone.utc)
                    ).total_seconds(),
                )
            except (TypeError, ValueError):
                pass
        try:
            prior = float(row.get(age_key))
        except (TypeError, ValueError):
            return timestamp_age
        elapsed_age = max(0.0, prior + max(0.0, float(elapsed_seconds or 0.0)))
        return max(timestamp_age, elapsed_age) if timestamp_age is not None else elapsed_age

    provider_age = advanced_age("provider_fetched_at", "provider_age_seconds")
    score_age = advanced_age("score_context_fetched_at", "score_context_age_seconds")
    provider_fresh = bool(
        provider_age is not None
        and provider_age <= GAME_STATE_PROVIDER_MAX_AGE_SECONDS
    )
    score_fresh = bool(
        score_age is not None
        and score_age <= GAME_STATE_PROVIDER_MAX_AGE_SECONDS
    )
    row["provider_age_seconds"] = round(provider_age, 1) if provider_age is not None else None
    row["score_context_age_seconds"] = round(score_age, 1) if score_age is not None else None
    row["provider_fresh"] = provider_fresh
    row["score_context_fresh"] = score_fresh
    row["authoritative_progress"] = bool(
        row.get("authoritative_progress")
        and row.get("progress_ready")
        and provider_fresh
    )
    row["live_state_fresh"] = bool(row["authoritative_progress"] or score_fresh)
    row["state_quality"] = (
        "authoritative_progress"
        if row["authoritative_progress"]
        else "score_only"
        if row.get("score_available")
        else "unavailable"
    )
    row["freshness_revalidated_at"] = now.astimezone().isoformat(timespec="seconds")
    return {key: value for key, value in row.items() if value is not None}


def _selected_tennis_diffs(game, candidate, provider):
    selected = normalize_text(candidate.get("selected_team"))
    home = normalize_text(game.get("home_team"))
    away = normalize_text(game.get("away_team"))
    if selected == home:
        sign = 1.0
    elif selected == away:
        sign = -1.0
    else:
        return None, None
    home_sets = _number(provider.get("home_sets_won"))
    away_sets = _number(provider.get("away_sets_won"))
    home_games = _number(provider.get("home_current_games"))
    away_games = _number(provider.get("away_current_games"))
    sets_diff = sign * (home_sets - away_sets) if home_sets is not None and away_sets is not None else None
    games_diff = sign * (home_games - away_games) if home_games is not None and away_games is not None else None
    return sets_diff, games_diff


def _selected_tennis_values(game, candidate, home_value, away_value):
    selected = normalize_text(candidate.get("selected_team"))
    if selected == normalize_text(game.get("home_team")):
        return home_value, away_value
    if selected == normalize_text(game.get("away_team")):
        return away_value, home_value
    return None, None


def _tennis_point_value(value):
    text = normalize_text(value)
    mapping = {
        "love": 0.0,
        "0": 0.0,
        "15": 1.0,
        "30": 2.0,
        "40": 3.0,
        "ad": 4.0,
        "adv": 4.0,
        "advantage": 4.0,
    }
    if text in mapping:
        return mapping[text]
    return _number(value)


def game_state_features(game, candidate):
    provider = game.get("premium_live_state") if isinstance(game.get("premium_live_state"), dict) else {}
    live_context = game.get("live_score_context") if isinstance(game.get("live_score_context"), dict) else {}
    sport_key = str(game.get("sport_key") or "")
    score_diff = _selected_score_diff(game, candidate, provider)
    home_score, away_score = _game_score_values(game, provider)
    score_available = home_score is not None and away_score is not None
    provider_age_seconds = _provider_age_seconds(provider)
    score_context_age_seconds = _provider_age_seconds(live_context)

    def state_value(*keys):
        for source in (provider, live_context, game):
            for key in keys:
                value = source.get(key)
                if value not in (None, ""):
                    return value
        return None

    features = {
        "version": GAME_STATE_MODEL_VERSION,
        "sport_key": sport_key,
        "provider": provider.get("provider") or (game.get("live_score_context") or {}).get("source"),
        "provider_game_id": provider.get("provider_game_id"),
        "provider_fetched_at": provider.get("fetched_at"),
        "provider_age_seconds": round(provider_age_seconds, 1) if provider_age_seconds is not None else None,
        "score_context_fetched_at": live_context.get("fetched_at"),
        "score_context_age_seconds": (
            round(score_context_age_seconds, 1)
            if score_context_age_seconds is not None
            else None
        ),
        "score_diff_selected": score_diff,
        "home_score": home_score,
        "away_score": away_score,
        "score_total": home_score + away_score if score_available else None,
        "minutes_since_start": candidate.get("minutes_since_start"),
        "score_available": score_available,
        "premium_state_available": bool(provider),
    }
    if sport_key.startswith("basketball"):
        period = _number(state_value("period", "quarter"))
        clock = _clock_minutes(state_value("clock", "time_remaining"))
        regulation_periods = 4.0
        period_length = 10.0 if "wnba" in sport_key else 12.0
        remaining = None
        if period is not None and clock is not None:
            remaining = max(0.0, (regulation_periods - min(period, regulation_periods)) * period_length + clock)
        features.update(
            {
                "period": period,
                "clock_minutes": clock,
                "regulation_minutes_remaining": remaining,
                "possession": provider.get("possession"),
                "home_timeouts": _number(provider.get("home_timeouts")),
                "away_timeouts": _number(provider.get("away_timeouts")),
                "home_fouls": _number(provider.get("home_fouls")),
                "away_fouls": _number(provider.get("away_fouls")),
                "model_ready": score_available and remaining is not None,
            }
        )
    elif sport_key.startswith("baseball"):
        inning = _number(state_value("inning"))
        raw_half = state_value("inning_half", "half")
        half = normalize_text(raw_half) if raw_half not in (None, "") else ""
        outs = _number(state_value("outs"))
        progress = None
        if inning is not None:
            progress = max(0.0, min(1.0, ((inning - 1.0) * 2.0 + (1.0 if half in {"bottom", "bot"} else 0.0)) / 18.0))
        features.update(
            {
                "inning": inning,
                "inning_half": half or None,
                "outs": outs,
                "balls": _number(provider.get("balls")),
                "strikes": _number(provider.get("strikes")),
                "pitch_count": _number(provider.get("pitch_count")),
                "game_progress": progress,
                "model_ready": score_available and progress is not None,
            }
        )
    elif sport_key.startswith("tennis"):
        derived_sets_diff, derived_games_diff = _selected_tennis_diffs(game, candidate, provider)
        selected_sets_diff = _number(state_value("selected_sets_diff"), derived_sets_diff)
        selected_games_diff = _number(state_value("selected_games_diff"), derived_games_diff)
        server = state_value("server", "serving")
        selected_serving = None
        if server not in (None, ""):
            selected_serving = normalize_text(server) == normalize_text(candidate.get("selected_team"))
        selected_point_score, opponent_point_score = _selected_tennis_values(
            game,
            candidate,
            state_value("home_point_score", "home_game_score"),
            state_value("away_point_score", "away_game_score"),
        )
        home_current_games = _number(state_value("home_current_games"))
        away_current_games = _number(state_value("away_current_games"))
        inferred_tiebreak = bool(
            home_current_games is not None
            and away_current_games is not None
            and home_current_games == away_current_games
            and home_current_games >= 6
        )
        features.update(
            {
                "server": server,
                "selected_serving": selected_serving,
                "selected_sets_diff": selected_sets_diff,
                "selected_games_diff": selected_games_diff,
                "current_set": _number(state_value("current_set", "set")),
                "set_score": state_value("set_score", "sets"),
                "game_score": state_value("game_score", "games", "point_score"),
                "selected_point_score": selected_point_score,
                "opponent_point_score": opponent_point_score,
                "selected_point_value": _tennis_point_value(selected_point_score),
                "opponent_point_value": _tennis_point_value(opponent_point_score),
                "break_point": bool(state_value("break_point", "is_break_point")),
                "tiebreak": bool(state_value("tiebreak", "is_tiebreak")) or inferred_tiebreak,
                "surface": state_value("surface", "court_surface"),
                "best_of": _number(state_value("best_of", "bestof")),
                "match_format": state_value("match_format", "format"),
                "match_status": state_value("match_status", "status", "state"),
                "model_ready": bool(
                    server
                    and selected_sets_diff is not None
                    and _number(state_value("current_set", "set")) is not None
                ),
            }
        )
    elif sport_key.startswith("americanfootball"):
        quarter = _number(state_value("quarter", "period"))
        clock = _clock_minutes(state_value("clock", "time_remaining"))
        remaining = None
        if quarter is not None and clock is not None:
            remaining = max(0.0, (4.0 - min(quarter, 4.0)) * 15.0 + clock)
        features.update(
            {
                "quarter": quarter,
                "clock_minutes": clock,
                "regulation_minutes_remaining": remaining,
                "possession": state_value("possession"),
                "down": _number(state_value("down")),
                "distance": _number(state_value("distance")),
                "model_ready": False,
            }
        )
    elif sport_key.startswith("icehockey"):
        period = _number(state_value("period"))
        clock = _clock_minutes(state_value("clock", "time_remaining"))
        remaining = None
        if period is not None and clock is not None:
            remaining = max(0.0, (3.0 - min(period, 3.0)) * 20.0 + clock)
        features.update(
            {
                "period": period,
                "clock_minutes": clock,
                "regulation_minutes_remaining": remaining,
                "strength": state_value("strength"),
                "model_ready": False,
            }
        )
    elif sport_key.startswith("soccer"):
        features.update(
            {
                "half": _number(state_value("half", "period")),
                "match_minute": _number(state_value("match_minute", "minute", "elapsed")),
                "stoppage_time": _number(state_value("stoppage_time", "added_time")),
                "match_status": state_value("match_status", "status", "state"),
                "model_ready": False,
            }
        )
    elif sport_key.startswith(("mma_", "boxing_")):
        features.update(
            {
                "round": _number(state_value("round", "period")),
                "round_clock_minutes": _clock_minutes(state_value("round_clock", "clock", "time_remaining")),
                "fight_status": state_value("fight_status", "status", "state"),
                "model_ready": False,
            }
        )
    elif sport_key.startswith(("rugby", "aussierules", "lacrosse")):
        features.update(
            {
                "period": _number(state_value("period", "quarter", "half")),
                "clock_minutes": _clock_minutes(state_value("clock", "time_remaining")),
                "model_ready": False,
            }
        )
    elif sport_key.startswith("cricket"):
        features.update(
            {
                "innings": _number(state_value("innings", "inning")),
                "overs": _number(state_value("overs")),
                "wickets": _number(state_value("wickets")),
                "model_ready": False,
            }
        )
    else:
        features["model_ready"] = False
    # Execution sizing distinguishes an authoritative progress feed from a
    # score-only fallback.  Any licensed or official adapter should populate
    # ``premium_live_state``; this keeps the sizing policy provider-agnostic.
    progress_ready = False
    if sport_key.startswith("basketball"):
        progress_ready = features.get("period") is not None and features.get("clock_minutes") is not None
    elif sport_key.startswith("baseball"):
        progress_ready = features.get("inning") is not None and bool(features.get("inning_half"))
    elif sport_key.startswith("tennis"):
        progress_ready = bool(features.get("server")) and features.get("current_set") is not None
    elif sport_key.startswith("americanfootball"):
        progress_ready = features.get("quarter") is not None and features.get("clock_minutes") is not None
    elif sport_key.startswith("icehockey"):
        progress_ready = features.get("period") is not None and features.get("clock_minutes") is not None
    elif sport_key.startswith("soccer"):
        progress_ready = features.get("match_minute") is not None
    elif sport_key.startswith(("mma_", "boxing_")):
        progress_ready = features.get("round") is not None and features.get("round_clock_minutes") is not None
    elif sport_key.startswith(("rugby", "aussierules", "lacrosse")):
        progress_ready = features.get("period") is not None and features.get("clock_minutes") is not None
    elif sport_key.startswith("cricket"):
        progress_ready = features.get("innings") is not None and features.get("overs") is not None
    provider_fresh = (
        provider_age_seconds is None
        or provider_age_seconds <= GAME_STATE_PROVIDER_MAX_AGE_SECONDS
    )
    score_context_fresh = bool(
        live_context
        and (
            score_context_age_seconds is None
            or score_context_age_seconds <= GAME_STATE_PROVIDER_MAX_AGE_SECONDS
        )
    )
    verified_source = provider.get("verified_progress_source") is not False
    features["progress_ready"] = bool(progress_ready)
    features["provider_fresh"] = bool(provider_fresh)
    features["score_context_fresh"] = score_context_fresh
    features["authoritative_progress"] = bool(provider and progress_ready and provider_fresh and verified_source)
    features["live_state_fresh"] = bool(features["authoritative_progress"] or score_context_fresh)
    features["state_quality"] = (
        "authoritative_progress"
        if features["authoritative_progress"]
        else "score_only"
        if features.get("score_available")
        else "unavailable"
    )
    return {key: value for key, value in features.items() if value is not None}


def shadow_game_state_probability(candidate, features):
    """Return a transparent heuristic shadow value, never an execution value."""
    pricing = candidate.get("pricing_v2") or {}
    prior_pct = _number(
        candidate.get("pregame_probability"),
        _number(candidate.get("prematch_probability")),
    )
    prematch_prior_available = prior_pct is not None
    if prior_pct is None:
        # A live book already embeds score, clock, inning, and server. Applying
        # the state heuristic on top would double count that information.
        prior_pct = _number(pricing.get("book_probability"), _number(candidate.get("model_prob"), 50.0))
    prior = max(0.01, min(0.99, prior_pct / 100.0))
    result = {
        "version": GAME_STATE_MODEL_VERSION,
        "mode": "shadow_only",
        "affects_execution": False,
        "prior_probability": round(prior * 100.0, 3),
        "prior_source": "prematch" if prematch_prior_available else "live_fallback_no_adjustment",
        "model_ready": bool(features.get("model_ready")),
        "features": features,
    }
    if not prematch_prior_available:
        result.update(
            {
                "probability": round(prior * 100.0, 3),
                "reason": "prematch_prior_unavailable_prevent_double_count",
            }
        )
        return result
    if not features.get("model_ready"):
        result.update(
            {
                "probability": round(prior * 100.0, 3),
                "reason": "insufficient_sport_state",
            }
        )
        return result

    sport_key = str(features.get("sport_key") or "")
    score_diff = float(features.get("score_diff_selected") or 0)
    adjustment = 0.0
    if sport_key.startswith("basketball"):
        remaining = max(0.0, float(features.get("regulation_minutes_remaining") or 0))
        late_multiplier = 1.0 + max(0.0, 24.0 - remaining) / 12.0
        adjustment = score_diff * 0.075 * late_multiplier
        reason = "basketball_score_clock_update"
    elif sport_key.startswith("baseball"):
        progress = max(0.0, min(1.0, float(features.get("game_progress") or 0)))
        adjustment = score_diff * (0.45 + 1.10 * progress)
        reason = "baseball_score_inning_update"
    elif sport_key.startswith("tennis"):
        sets_diff = float(features.get("selected_sets_diff") or 0)
        games_diff = float(features.get("selected_games_diff") or 0)
        best_of = int(float(features.get("best_of") or 3))
        set_scale = 0.85 if best_of >= 5 else 1.0
        surface = normalize_text(features.get("surface"))
        serve_scale = 1.25 if "grass" in surface else 0.8 if "clay" in surface else 1.0
        tiebreak = bool(features.get("tiebreak"))
        selected_serving = features.get("selected_serving")
        serve_adjustment = 0.0
        if selected_serving is True:
            serve_adjustment = 0.035 * serve_scale
        elif selected_serving is False:
            serve_adjustment = -0.035 * serve_scale
        if tiebreak:
            serve_adjustment *= 0.35
        selected_point = _number(features.get("selected_point_value"))
        opponent_point = _number(features.get("opponent_point_value"))
        point_adjustment = 0.0
        if selected_point is not None and opponent_point is not None:
            point_adjustment = max(-2.0, min(2.0, selected_point - opponent_point)) * 0.045
        break_adjustment = 0.0
        if features.get("break_point"):
            break_adjustment = -0.10 if selected_serving is True else 0.10
        adjustment = (
            sets_diff * 1.15 * set_scale
            + games_diff * 0.12
            + serve_adjustment
            + point_adjustment
            + break_adjustment
        )
        reason = "tennis_set_game_server_update"
    else:
        reason = "unsupported_sport_state"
    probability = _logistic(_logit(prior) + adjustment)
    result.update(
        {
            "probability": round(probability * 100.0, 3),
            "logit_adjustment": round(adjustment, 6),
            "reason": reason,
        }
    )
    if sport_key.startswith("tennis"):
        result["tennis_adjustment_components"] = {
            "sets": round(sets_diff * 1.15 * set_scale, 6),
            "games": round(games_diff * 0.12, 6),
            "server": round(serve_adjustment, 6),
            "point_score": round(point_adjustment, 6),
            "break_point": round(break_adjustment, 6),
            "surface": features.get("surface"),
            "best_of": best_of,
            "tiebreak": tiebreak,
        }
    return result
