import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kalshi_common import get_json, log_line, normalize_text, write_json


BOT_PICKS_FILE = "bot_picks_report.json"
BOT_PICKS_LOG_FILE = "bot_picks_log.txt"
SETTINGS_FILE = "bot_settings.json"


def settings_file_value(name, default=""):
    try:
        settings = json.loads(Path(SETTINGS_FILE).read_text(encoding="utf-8-sig"))
        value = settings.get(name, default)
        return default if value is None else str(value)
    except Exception:
        return default

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4")
BOT_PICKS_GROK_SEARCH_ENABLED = os.getenv("BOT_PICKS_GROK_SEARCH_ENABLED", os.getenv("SPORTS_GROK_SEARCH_ENABLED", "true")).lower() == "true"
BOT_PICKS_GROK_WEB_SEARCH_ENABLED = os.getenv("BOT_PICKS_GROK_WEB_SEARCH_ENABLED", os.getenv("SPORTS_GROK_WEB_SEARCH_ENABLED", "true")).lower() == "true"
BOT_PICKS_GROK_X_SEARCH_ENABLED = os.getenv("BOT_PICKS_GROK_X_SEARCH_ENABLED", os.getenv("SPORTS_GROK_X_SEARCH_ENABLED", "true")).lower() == "true"
XAI_SEARCH_MODEL = os.getenv("XAI_SEARCH_MODEL", "grok-4.3")
BOT_PICKS_ENABLED = os.getenv("BOT_PICKS_ENABLED", settings_file_value("BOT_PICKS_ENABLED", "true")).lower() == "true"
BOT_PICKS_ACTIVE_KEYS = {
    key.strip()
    for key in os.getenv("BOT_PICKS_ACTIVE_KEYS", settings_file_value("BOT_PICKS_ACTIVE_KEYS", "turbo,season,fade_public,mlb,nba")).split(",")
    if key.strip()
}
BOT_PICKS_REFRESH_HOURS = float(os.getenv("BOT_PICKS_REFRESH_HOURS", settings_file_value("BOT_PICKS_REFRESH_HOURS", "12")))
BOT_PICKS_MAX_GAMES = int(float(os.getenv("BOT_PICKS_MAX_GAMES", settings_file_value("BOT_PICKS_MAX_GAMES", "20"))))
BOT_PICKS_SCHEDULE_ENABLED = os.getenv("BOT_PICKS_SCHEDULE_ENABLED", settings_file_value("BOT_PICKS_SCHEDULE_ENABLED", "true")).lower() == "true"
BOT_PICKS_MORNING_HOUR = int(float(os.getenv("BOT_PICKS_MORNING_HOUR", settings_file_value("BOT_PICKS_MORNING_HOUR", "10"))))
BOT_PICKS_AFTERNOON_HOUR = int(float(os.getenv("BOT_PICKS_AFTERNOON_HOUR", settings_file_value("BOT_PICKS_AFTERNOON_HOUR", "14"))))
BOT_PICKS_EVENING_HOUR = int(float(os.getenv("BOT_PICKS_EVENING_HOUR", settings_file_value("BOT_PICKS_EVENING_HOUR", "18"))))
BOT_PICKS_STAGGER_MINUTES = int(float(os.getenv("BOT_PICKS_STAGGER_MINUTES", settings_file_value("BOT_PICKS_STAGGER_MINUTES", "10"))))
BOT_PICKS_LOG_RESPONSE_CHARS = int(float(os.getenv("BOT_PICKS_LOG_RESPONSE_CHARS", settings_file_value("BOT_PICKS_LOG_RESPONSE_CHARS", "1200"))))
BOT_PICKS_FORCE_DAILY_PICK = os.getenv("BOT_PICKS_FORCE_DAILY_PICK", settings_file_value("BOT_PICKS_FORCE_DAILY_PICK", "true")).lower() == "true"
BOT_PICKS_PUBLIC_FADE_MIN_PCT = float(os.getenv("BOT_PICKS_PUBLIC_FADE_MIN_PCT", settings_file_value("BOT_PICKS_PUBLIC_FADE_MIN_PCT", "95")))
SHARP_BOOKS = {"pinnacle", "draftkings", "fanduel", "circa", "betonlineag", "bovada"}


def load_chicago_tz():
    try:
        return ZoneInfo("America/Chicago")
    except ZoneInfoNotFoundError:
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is not None:
            return local_tz
        return timezone(timedelta(hours=-5), "Central")


CHICAGO_TZ = load_chicago_tz()

ACTIVE_BOTS = {
    "turbo": {
        "name": "Turbo Bot",
        "sports": ["basketball_ncaab", "basketball_nba", "americanfootball_nfl", "americanfootball_ncaaf", "baseball_mlb"],
        "style": "fast, aggressive, high-volume +EV hunter. Prefer strongest edge with model agreement and line value.",
    },
    "season": {
        "name": "Season Ender Bot",
        "sports": ["basketball_nba", "americanfootball_ncaaf", "americanfootball_nfl", "basketball_ncaab", "baseball_mlb"],
        "style": "conservative, low-variance specialist. Require multiple confirming factors. Avoid marginal spots.",
    },
    "fade_public": {
        "name": "Fade The Public Bot",
        "sports": ["basketball_ncaab", "basketball_nba", "americanfootball_nfl", "americanfootball_ncaaf", "baseball_mlb"],
        "style": "strict contrarian public-fade specialist. Only fade verified extreme public bet percentage spots. Never fade blindly.",
    },
    "mlb": {
        "name": "MLB Bot",
        "sports": ["baseball_mlb"],
        "style": "baseball specialist. Focus on pitching, bullpen, lineup, travel, weather, and market value.",
    },
    "nba": {
        "name": "NBA Bot",
        "sports": ["basketball_nba"],
        "style": "basketball specialist. Focus on pace, efficiency, injury impact, rest, and sharp line movement.",
    },
}


def active_bot_items():
    return [(key, bot) for key, bot in ACTIVE_BOTS.items() if key in BOT_PICKS_ACTIVE_KEYS]


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def report_is_fresh(report):
    errors = report.get("errors") or []
    if XAI_API_KEY and any(error.get("error") == "missing_xai_key" for error in errors):
        return False
    if XAI_API_KEY and errors and not report.get("picks"):
        return False
    generated_at = report.get("generated_at")
    if not generated_at:
        return False
    try:
        then = datetime.fromisoformat(generated_at)
    except ValueError:
        return False
    if then.tzinfo is not None:
        then = then.replace(tzinfo=None)
    return datetime.now() - then < timedelta(hours=BOT_PICKS_REFRESH_HOURS)


def american_price_text(value):
    if value is None:
        return ""
    value = int(value)
    return f"+{value}" if value > 0 else str(value)


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def chicago_date(value):
    dt = parse_iso_datetime(value)
    if not dt:
        return ""
    return dt.astimezone(CHICAGO_TZ).date().isoformat()


def chicago_time(value):
    dt = parse_iso_datetime(value)
    if not dt:
        return ""
    return dt.astimezone(CHICAGO_TZ).strftime("%H:%M %Z")


def today_chicago():
    return datetime.now(CHICAGO_TZ).date().isoformat()


def now_chicago():
    return datetime.now(CHICAGO_TZ)


def normalize_team_key(value):
    return normalize_text(value)


def game_id(game):
    return f"{normalize_team_key(game.get('home_team', ''))}|{normalize_team_key(game.get('away_team', ''))}"


def build_used_from_picks(picks):
    used = {"games": set(), "teams_by_sport": {}}
    for pick in picks:
        home = normalize_team_key(pick.get("home_team", ""))
        away = normalize_team_key(pick.get("away_team", ""))
        sport_key = normalize_text(pick.get("sport_key", ""))
        if home and away:
            used["games"].add(f"{home}|{away}")
        if sport_key:
            used["teams_by_sport"].setdefault(sport_key, set())
            for value in (home, away, normalize_team_key(pick.get("selection", ""))):
                if value:
                    used["teams_by_sport"][sport_key].add(value)
    return used


def empty_report():
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date": today_chicago(),
        "enabled": BOT_PICKS_ENABLED,
        "xai_model": XAI_MODEL,
        "picks": [],
        "errors": [],
        "attempts": {},
        "selected_games_by_bot": {},
        "used_today": {"games": [], "teams_by_sport": {}},
        "schedule": {
            "enabled": BOT_PICKS_SCHEDULE_ENABLED,
            "morning_hour": BOT_PICKS_MORNING_HOUR,
            "afternoon_hour": BOT_PICKS_AFTERNOON_HOUR,
            "evening_hour": BOT_PICKS_EVENING_HOUR,
            "stagger_minutes": BOT_PICKS_STAGGER_MINUTES,
            "timezone": str(CHICAGO_TZ),
        },
    }


def daily_report(existing):
    if existing.get("date") == today_chicago():
        report = {**empty_report(), **existing}
        report.setdefault("picks", [])
        report.setdefault("errors", [])
        report.setdefault("attempts", {})
        report.setdefault("selected_games_by_bot", {})
        report.setdefault("used_today", {"games": [], "teams_by_sport": {}})
        report["enabled"] = BOT_PICKS_ENABLED
        report["xai_model"] = XAI_MODEL
        report["schedule"] = empty_report()["schedule"]
        return report
    return empty_report()


def bot_has_pick(report, bot_key):
    return any(pick.get("bot_key") == bot_key for pick in report.get("picks", []))


def bot_attempted_slot(report, bot_key, slot):
    return any(attempt.get("slot") == slot for attempt in report.get("attempts", {}).get(bot_key, []))


def scheduled_time_for(bot_index, slot):
    if slot == "morning":
        hour = BOT_PICKS_MORNING_HOUR
    elif slot == "afternoon":
        hour = BOT_PICKS_AFTERNOON_HOUR
    else:
        hour = BOT_PICKS_EVENING_HOUR
    base = now_chicago().replace(hour=hour, minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=bot_index * BOT_PICKS_STAGGER_MINUTES)


def due_slot_for_bot(report, bot_key, bot_index, force=False):
    if force or not BOT_PICKS_SCHEDULE_ENABLED:
        return "forced" if force else "unscheduled"
    now = now_chicago()
    morning_due = scheduled_time_for(bot_index, "morning")
    afternoon_due = scheduled_time_for(bot_index, "afternoon")
    evening_due = scheduled_time_for(bot_index, "evening")

    if now >= morning_due and not bot_attempted_slot(report, bot_key, "morning"):
        return "morning"
    if bot_has_pick(report, bot_key):
        return ""
    if now >= afternoon_due and not bot_attempted_slot(report, bot_key, "afternoon"):
        return "afternoon"
    if now >= evening_due and not bot_attempted_slot(report, bot_key, "evening"):
        return "evening"
    return ""


def record_attempt(report, bot_key, bot, slot, status, **extra):
    attempt = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "slot": slot,
        "status": status,
        **extra,
    }
    report.setdefault("attempts", {}).setdefault(bot_key, []).append(attempt)
    if status in ("error", "no_pick", "invalid_pick"):
        report.setdefault("errors", []).append(
            {"bot_key": bot_key, "bot_name": bot["name"], "slot": slot, "error": extra.get("error", status)}
        )
    return attempt


def get_game_market_features(game):
    features = {
        "book_count": 0,
        "sharp_book_count": 0,
        "spread_disagreement": None,
        "total_disagreement": None,
        "feature_score": 0,
        "best_prices": [],
    }
    bookmakers = game.get("bookmakers") or []
    features["book_count"] = len(bookmakers)
    spread_lines = []
    total_lines = []

    for book in bookmakers:
        book_key = normalize_text(book.get("key", ""))
        if book_key in SHARP_BOOKS:
            features["sharp_book_count"] += 1
        for market in book.get("markets") or []:
            market_key = market.get("key")
            if market_key not in ("h2h", "spreads", "totals"):
                continue
            for outcome in market.get("outcomes") or []:
                name = outcome.get("name", "")
                price = outcome.get("price")
                if price is None:
                    continue
                features["best_prices"].append(
                    {
                        "market": market_key,
                        "name": name,
                        "point": outcome.get("point"),
                        "price": int(price),
                        "book": book_key,
                    }
                )
                if market_key == "spreads" and "point" in outcome:
                    spread_lines.append(float(outcome["point"]))
                if market_key == "totals" and "point" in outcome:
                    total_lines.append(float(outcome["point"]))

    if spread_lines:
        features["spread_disagreement"] = round(max(spread_lines) - min(spread_lines), 2)
    if total_lines:
        features["total_disagreement"] = round(max(total_lines) - min(total_lines), 2)

    features["best_prices"].sort(key=lambda row: row["price"], reverse=True)
    features["feature_score"] = (
        features["book_count"] * 2
        + features["sharp_book_count"] * 4
        + float(features["spread_disagreement"] or 0)
        + float(features["total_disagreement"] or 0)
    )
    return features


def summarize_game(game):
    features = get_game_market_features(game)
    lines = [
        f"- {game.get('home_team')} vs {game.get('away_team')} ({game.get('sport_title') or game.get('sport_key')})",
        f"  Start: {chicago_time(game.get('commence_time'))}",
        "  Books used: " + ", ".join(sorted(SHARP_BOOKS)),
        (
            f"  Market quality: {features['book_count']} books, "
            f"{features['sharp_book_count']} sharp/reference books"
            + (f", spread disagreement {features['spread_disagreement']}" if features["spread_disagreement"] is not None else "")
            + (f", total disagreement {features['total_disagreement']}" if features["total_disagreement"] is not None else "")
        ),
    ]
    spread_lines = []
    total_lines = []
    for bookmaker in (game.get("bookmakers") or []):
        book = bookmaker.get("key") or bookmaker.get("title") or "book"
        if normalize_text(book) not in SHARP_BOOKS:
            continue
        for market in bookmaker.get("markets") or []:
            key = market.get("key")
            if key not in ("h2h", "spreads", "totals"):
                continue
            outcomes = []
            for outcome in market.get("outcomes") or []:
                name = outcome.get("name")
                price = american_price_text(outcome.get("price"))
                point = outcome.get("point")
                if key == "spreads" and point is not None and name == game.get("home_team"):
                    spread_lines.append(float(point))
                if key == "totals" and point is not None:
                    total_lines.append(float(point))
                if point is None:
                    outcomes.append(f"{name} {price}")
                else:
                    point_text = f"+{point}" if float(point) >= 0 else str(point)
                    outcomes.append(f"{name} {point_text} @ {price}")
            if outcomes:
                lines.append(f"  {book} {key}: " + " | ".join(outcomes[:4]))
    if spread_lines:
        lines.append(f"  Consensus spread: {round(sum(spread_lines) / len(spread_lines), 2)}")
    if total_lines:
        lines.append(f"  Consensus total: {round(sum(total_lines) / len(total_lines), 2)}")
    if features["best_prices"]:
        best = features["best_prices"][:8]
        bits = []
        for price in best:
            point = "" if price["point"] is None else f" {price['point']}"
            bits.append(f"{price['market']} {price['name']}{point} @ {price['price']} ({price['book']})")
        lines.append("  Best current prices: " + " | ".join(bits))
    return "\n".join(lines)


def pick_game_pool(games, bot, used_today):
    allowed = set(bot.get("sports") or [])
    today = today_chicago()
    filtered = []
    for game in games:
        sport_key = normalize_text(game.get("sport_key", ""))
        if game.get("sport_key") not in allowed:
            continue
        if chicago_date(game.get("commence_time")) != today:
            continue
        home = normalize_team_key(game.get("home_team", ""))
        away = normalize_team_key(game.get("away_team", ""))
        if game_id(game) in used_today["games"]:
            continue
        used_teams = used_today["teams_by_sport"].get(sport_key, set())
        if home in used_teams or away in used_teams:
            continue
        filtered.append(game)

    filtered.sort(
        key=lambda game: (
            -get_game_market_features(game)["feature_score"],
            game.get("commence_time") or "",
        )
    )
    return filtered[:BOT_PICKS_MAX_GAMES]


def parse_pick(content):
    content = content.strip()
    if not content:
        raise ValueError("empty_grok_content")
    if "```" in content:
        content = content.replace("```json", "").replace("```", "").strip()
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end >= start:
        content = content[start : end + 1]
    data = json.loads(content)
    if "pick" not in data:
        raise ValueError("missing_pick_field")
    return data.get("pick")


def response_preview(content):
    return " ".join(str(content).split())[:BOT_PICKS_LOG_RESPONSE_CHARS]


def game_log_sample(games, count=5):
    return [
        f"{game.get('sport_key')}:{game.get('away_team')}@{game.get('home_team')}:{game.get('commence_time')}"
        for game in games[:count]
    ]


def xai_search_tools():
    tools = []
    if BOT_PICKS_GROK_WEB_SEARCH_ENABLED:
        tools.append({"type": "web_search"})
    if BOT_PICKS_GROK_X_SEARCH_ENABLED:
        tools.append({"type": "x_search"})
    return tools


def extract_response_text(data):
    if data.get("output_text"):
        return str(data.get("output_text") or "").strip()
    pieces = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text") or content.get("output_text")
            if text:
                pieces.append(str(text))
    return "\n".join(pieces).strip()


def call_grok_chat_completions(payload):
    data, _headers = get_json(
        "https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
        method="POST",
        body=payload,
    )
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    return content, data.get("usage") or {}, "chat_completions", {}, []


def call_grok_for_pick(bot_key, bot, games, used_today):
    if not XAI_API_KEY:
        return None, "missing_xai_key", {}
    if not games:
        return None, "no_games_for_bot", {}

    game_summary = "\n\n".join(summarize_game(game) for game in games)
    schema_hint = {
        "pick": {
            "sport_key": "baseball_mlb",
            "event_id": "Odds API event id",
            "home_team": "official home team",
            "away_team": "official away team",
            "game": "Away @ Home",
            "pick": "Team +1.5 or Over 8.5 or Team ML",
            "bet_type": "moneyline|spread|total",
            "selection": "team name or Over/Under",
            "line": 1.5,
            "direction": "home|away|over|under|null",
            "odds": "-110",
            "confidence": 0.72,
            "actionable": True,
            "estimated_edge_pct": 3.2,
            "public_bet_pct": 95,
            "public_side": "Team or Over/Under the public is on",
            "fade_side": "Team or Over/Under being bet against the public",
            "public_source": "named source or search summary for public bet percentage",
            "reason_summary": "one concise sentence",
            "analysis": "short reasoning summary",
            "description": "full bet description",
        }
    }
    used_games = sorted(used_today["games"])
    used_teams = {
        sport: sorted(teams)
        for sport, teams in used_today["teams_by_sport"].items()
    }
    pick_rule = (
        "4. For paper tracking, choose exactly one strongest available pick from the supplied games. Set actionable=false if the edge is not strong enough for a real bet.\n"
        if BOT_PICKS_FORCE_DAILY_PICK
        else "4. Choose exactly one strongest +EV pick, or return {\"pick\": null} if there is no strong edge.\n"
    )
    fade_rules = ""
    if bot_key == "fade_public":
        pick_rule = (
            "4. Choose exactly one fade-the-public pick ONLY IF a named source or live web/X search verifies "
            f"public_bet_pct >= {BOT_PICKS_PUBLIC_FADE_MIN_PCT:.0f}% on the opposite side. If no verified "
            "95%+ public side exists, return {\"pick\": null}.\n"
        )
        fade_rules = (
            "FADE THE PUBLIC STRICT RULES:\n"
            f"- public_bet_pct must be >= {BOT_PICKS_PUBLIC_FADE_MIN_PCT:.0f}; this is percent of bets/tickets, not your confidence.\n"
            "- The pick must be AGAINST public_side. If public is 95%+ on Team A, selection must be Team B or the opposite total side.\n"
            "- public_source must name where the percentage came from or summarize the web/X search result. Do not invent it.\n"
            "- Prefer moneyline/spread/total only when Kalshi can plausibly match it. Avoid props.\n"
            "- If the public percentage is not verified, return null even if the matchup looks good.\n"
        )
    prompt = (
        f"You are {bot['name']}: {bot['style']}\n"
        "MANDATORY RULES:\n"
        "1. Use the supplied Odds API data as schedule/line ground truth.\n"
        "2. Use web_search/x_search only for latest injuries, weather, lineups, pitchers, rest/travel, and named-source public/sharp data if your model supports it.\n"
        "3. Do not invent public %, handle %, reverse line movement, steam, or sharp money.\n"
        f"{pick_rule}"
        "5. Prefer moneyline, spread, or total. Avoid player props.\n"
        "6. home_team and away_team must exactly match one supplied game.\n"
        "7. Do not choose games/teams already used by earlier bots today.\n"
        "8. actionable=true only when estimated_edge_pct >= 3.0 and confidence >= 0.70. Otherwise return the best paper pick with actionable=false.\n"
        f"{fade_rules}"
        f"Already used game ids: {used_games}\n"
        f"Already used teams by sport: {used_teams}\n"
        "Output valid JSON only with this shape:\n"
        f"{json.dumps(schema_hint)}\n\n"
        "REAL-TIME ODDS DATA (today only, sport-specific):\n"
        "Use as ground truth for teams, schedule, books, current lines, and prices.\n"
        "Prefer candidates with sharper books, book disagreement, stale prices, matchup/news/weather/rest edges, and clear value. "
        "For real-money actionability require edge >= 3%; for paper tracking still return the strongest pick and mark actionable=false if edge is weaker.\n\n"
        f"Available games:\n{game_summary}"
    )
    chat_payload = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": "Return JSON only. Do not include markdown or commentary."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 800,
    }
    search_tools = xai_search_tools()
    search_payload = {
        "model": XAI_SEARCH_MODEL or XAI_MODEL,
        "input": [
            {"role": "system", "content": "Return JSON only. Do not include markdown or commentary."},
            {"role": "user", "content": prompt},
        ],
        "tools": search_tools,
        "temperature": 0.1,
        "max_output_tokens": 900,
    }
    log_line(
        BOT_PICKS_LOG_FILE,
        (
            f"Grok request: bot={bot['name']} model={XAI_MODEL} games={len(games)} "
            f"search={BOT_PICKS_GROK_SEARCH_ENABLED and bool(search_tools)} search_model={XAI_SEARCH_MODEL or XAI_MODEL} "
            f"prompt_chars={len(prompt)} used_games={len(used_games)} used_team_sports={len(used_teams)} "
            f"samples={game_log_sample(games)}"
        ),
    )
    search_error = ""
    if BOT_PICKS_GROK_SEARCH_ENABLED and search_tools:
        try:
            data, _headers = get_json(
                "https://api.x.ai/v1/responses",
                headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
                method="POST",
                body=search_payload,
            )
            content = extract_response_text(data)
            usage = data.get("usage") or {}
            api_used = "responses"
            tool_usage = data.get("server_side_tool_usage") or {}
            citations = data.get("citations") or []
        except Exception as exc:
            search_error = str(exc)
            content, usage, api_used, tool_usage, citations = call_grok_chat_completions(chat_payload)
    else:
        content, usage, api_used, tool_usage, citations = call_grok_chat_completions(chat_payload)
    log_line(
        BOT_PICKS_LOG_FILE,
        (
            f"Grok response: bot={bot['name']} api={api_used} content_chars={len(content)} "
            f"usage={usage} tool_usage={tool_usage} citations={len(citations)} "
            f"search_error={search_error[:180]} preview={response_preview(content)}"
        ),
    )
    try:
        pick = parse_pick(content)
    except Exception as exc:
        return None, f"parse_error:{exc}", {"content_preview": response_preview(content), "usage": usage, "api": api_used, "search_error": search_error}
    if pick is None:
        return None, "grok_returned_null", {"content_preview": response_preview(content), "usage": usage, "api": api_used, "search_error": search_error}
    return pick, "", {"content_preview": response_preview(content), "usage": usage, "api": api_used, "search_error": search_error, "tool_usage": tool_usage, "citations": citations[:5]}


def attach_event_from_games(pick, games):
    if not pick:
        return pick
    pick_home = normalize_text(pick.get("home_team", ""))
    pick_away = normalize_text(pick.get("away_team", ""))
    for game in games:
        game_home = normalize_text(game.get("home_team", ""))
        game_away = normalize_text(game.get("away_team", ""))
        if game_home == pick_home and game_away == pick_away:
            pick["event_id"] = game.get("id")
            pick["sport_key"] = game.get("sport_key")
            pick["commence_time"] = game.get("commence_time")
            pick["home_team"] = game.get("home_team")
            pick["away_team"] = game.get("away_team")
            return pick
        if game_home == pick_away and game_away == pick_home:
            pick["event_id"] = game.get("id")
            pick["sport_key"] = game.get("sport_key")
            pick["commence_time"] = game.get("commence_time")
            pick["home_team"] = game.get("home_team")
            pick["away_team"] = game.get("away_team")
            return pick
    return pick


def normalize_pick_for_storage(pick):
    if not pick:
        return None
    return {
        "date": today_chicago(),
        "status": pick.get("status", "pending"),
        "sport": pick.get("sport", "Unknown"),
        "sport_key": pick.get("sport_key", ""),
        "event_id": pick.get("event_id", ""),
        "commence_time": pick.get("commence_time", ""),
        "home_team": pick.get("home_team", ""),
        "away_team": pick.get("away_team", ""),
        "game": pick.get("game", ""),
        "pick": pick.get("pick", ""),
        "odds": pick.get("odds", "-110"),
        "type": pick.get("type", pick.get("bet_type", "Unknown")),
        "bet_type": pick.get("bet_type", "spread"),
        "selection": pick.get("selection", ""),
        "line": float(pick["line"]) if pick.get("line") not in (None, "") else None,
        "direction": pick.get("direction"),
        "player_name": pick.get("player_name"),
        "prop_stat": pick.get("prop_stat"),
        "confidence": float(pick["confidence"]) if pick.get("confidence") not in (None, "") else None,
        "actionable": bool(pick.get("actionable", True)),
        "estimated_edge_pct": float(pick["estimated_edge_pct"]) if pick.get("estimated_edge_pct") not in (None, "") else None,
        "public_bet_pct": float(pick["public_bet_pct"]) if pick.get("public_bet_pct") not in (None, "") else None,
        "public_side": pick.get("public_side", ""),
        "fade_side": pick.get("fade_side", ""),
        "public_source": pick.get("public_source", ""),
        "analysis": pick.get("analysis", ""),
        "reason_summary": pick.get("reason_summary", ""),
        "description": pick.get("description", ""),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def validate_fade_public_pick(pick):
    if pick.get("bot_key") != "fade_public":
        return True, ""
    public_pct = pick.get("public_bet_pct")
    if public_pct is None:
        return False, "fade_public_missing_public_pct"
    if float(public_pct) < BOT_PICKS_PUBLIC_FADE_MIN_PCT:
        return False, f"fade_public_pct_below_{BOT_PICKS_PUBLIC_FADE_MIN_PCT:.0f}"
    if not str(pick.get("public_source") or "").strip():
        return False, "fade_public_missing_public_source"
    public_side = normalize_text(pick.get("public_side", ""))
    selection = normalize_text(pick.get("selection", ""))
    fade_side = normalize_text(pick.get("fade_side", ""))
    if public_side and (public_side == selection or (fade_side and public_side == fade_side)):
        return False, "fade_public_pick_matches_public_side"
    return True, ""


def validate_pick_against_pool(pick, pool, bot_key=""):
    if not pick:
        return None
    pick = normalize_pick_for_storage(pick)
    pick["bot_key"] = bot_key or pick.get("bot_key", "")
    attached = attach_event_from_games(pick, pool)
    if not attached.get("event_id"):
        return None
    if attached.get("bet_type") not in ("moneyline", "spread", "total"):
        return None
    ok, reason = validate_fade_public_pick(attached)
    if not ok:
        attached["validation_error"] = reason
        return None
    return attached


def mark_pick_used(used_today, pick):
    home = normalize_team_key(pick.get("home_team", ""))
    away = normalize_team_key(pick.get("away_team", ""))
    sport_key = normalize_text(pick.get("sport_key", ""))
    if home and away:
        used_today["games"].add(f"{home}|{away}")
    if sport_key:
        used_today["teams_by_sport"].setdefault(sport_key, set())
        for value in (home, away, normalize_team_key(pick.get("selection", ""))):
            if value:
                used_today["teams_by_sport"][sport_key].add(value)


def generate_bot_picks(games, force=False):
    existing = read_json(BOT_PICKS_FILE, {})
    report = daily_report(existing)

    if not BOT_PICKS_ENABLED:
        write_json(BOT_PICKS_FILE, report)
        return report

    due_bots = []
    for bot_index, bot_key in enumerate([key for key, _bot in active_bot_items()]):
        slot = due_slot_for_bot(report, bot_key, bot_index, force=force)
        if slot:
            due_bots.append((bot_index, bot_key, slot))

    if not due_bots and not force and report_is_fresh(report):
        log_line(
            BOT_PICKS_LOG_FILE,
            (
                f"Bot picks fresh/no due attempts: picks={len(report.get('picks', []))} "
                f"errors={len(report.get('errors', []))} next_schedule=10:00/14:00/18:00 CT stagger={BOT_PICKS_STAGGER_MINUTES}m"
            ),
        )
        write_json(BOT_PICKS_FILE, report)
        return report

    if not due_bots and BOT_PICKS_SCHEDULE_ENABLED and not force:
        log_line(
            BOT_PICKS_LOG_FILE,
            (
                f"Bot picks not due yet: now={now_chicago().strftime('%H:%M %Z')} "
                f"schedule=10:00/14:00/18:00 CT stagger={BOT_PICKS_STAGGER_MINUTES}m"
            ),
        )
        write_json(BOT_PICKS_FILE, report)
        return report

    report["generated_at"] = datetime.now().isoformat(timespec="seconds")
    due_bot_keys = {bot_key for _index, bot_key, _slot in due_bots}
    report["errors"] = [
        error
        for error in report.get("errors", [])
        if error.get("slot") and error.get("bot_key") not in due_bot_keys
    ]

    used_today = build_used_from_picks(report.get("picks", []))
    for bot_index, bot_key, slot in due_bots:
        bot = ACTIVE_BOTS[bot_key]
        if bot_has_pick(report, bot_key) and slot != "forced":
            record_attempt(report, bot_key, bot, slot, "skipped", reason="already_has_pick")
            log_line(BOT_PICKS_LOG_FILE, f"Bot skipped: {bot['name']} slot={slot} reason=already_has_pick")
            continue

        pool = pick_game_pool(games, bot, used_today)
        report["selected_games_by_bot"][bot_key] = [
            {
                "event_id": game.get("id"),
                "sport_key": game.get("sport_key"),
                "home_team": game.get("home_team"),
                "away_team": game.get("away_team"),
                "commence_time": game.get("commence_time"),
                "feature_score": get_game_market_features(game)["feature_score"],
            }
            for game in pool
        ]
        log_line(
            BOT_PICKS_LOG_FILE,
            (
                f"Bot attempt start: {bot['name']} slot={slot} index={bot_index} "
                f"pool_games={len(pool)} sports={bot.get('sports')} samples={game_log_sample(pool)}"
            ),
        )
        try:
            pick, error, debug = call_grok_for_pick(bot_key, bot, pool, used_today)
        except Exception as exc:
            pick, error, debug = None, str(exc), {}

        raw_pick = pick
        if pick:
            pick = validate_pick_against_pool(pick, pool, bot_key=bot_key)
            if raw_pick and not pick:
                error = "invalid_pick_not_in_pool_or_type"
                if bot_key == "fade_public":
                    normalized = normalize_pick_for_storage(raw_pick)
                    normalized["bot_key"] = bot_key
                    ok, reason = validate_fade_public_pick(normalized)
                    if not ok:
                        error = reason
        if pick:
            pick["bot_key"] = bot_key
            pick["bot_name"] = bot["name"]
            report["picks"].append(pick)
            mark_pick_used(used_today, pick)
            record_attempt(
                report,
                bot_key,
                bot,
                slot,
                "pick",
                pick=pick.get("pick"),
                game=pick.get("game"),
                confidence=pick.get("confidence"),
                usage=debug.get("usage"),
            )
            log_line(BOT_PICKS_LOG_FILE, f"Bot pick: {bot['name']} slot={slot} -> {pick.get('pick')} | {pick.get('game')} | conf={pick.get('confidence')}")
        else:
            status = "invalid_pick" if error == "invalid_pick_not_in_pool_or_type" else "no_pick"
            record_attempt(
                report,
                bot_key,
                bot,
                slot,
                status,
                error=error or "no_pick",
                pool_games=len(pool),
                usage=debug.get("usage"),
                response_preview=debug.get("content_preview"),
            )
            log_line(BOT_PICKS_LOG_FILE, f"Bot no-pick/error: {bot['name']} slot={slot} -> {error or 'no_pick'}")

    report["used_today"] = {
        "games": sorted(used_today["games"]),
        "teams_by_sport": {sport: sorted(teams) for sport, teams in used_today["teams_by_sport"].items()},
    }
    write_json(BOT_PICKS_FILE, report)
    return report


def load_bot_picks():
    return read_json(BOT_PICKS_FILE, {"picks": [], "errors": []})
