"""Manual trusted-capper pick ingestion and durable ticket queue.

The dashboard is the only supported ingestion path.  It accepts text the user
has permission to copy, produces a deterministic preview, and persists only
the picks the user explicitly approves.  This module intentionally contains no
Discord credentials, scraping, or automated access to third-party channels.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sports_audit_support import parse_timestamp
from sports_audit_analytics import capper_funnel


STORE_FILE = Path("sports_capper_tickets.json")
LOCK_FILE = Path("sports_capper_tickets.lock")
WAKE_FILE = Path("sports_capper_wake.flag")
STORE_VERSION = 3
PARSER_VERSION = 12
SOURCE_NAME = "InfluencedBets"
COMBAT_PROP_UNSUPPORTED_REASON = "combat_method_of_victory_requires_exact_supported_market"
SOCCER_DOUBLE_CHANCE_UNSUPPORTED_REASON = "soccer_double_chance_requires_exact_supported_market"
CAPPER_COVERAGE_TARGET_PCT = float(os.getenv("SPORTS_CAPPER_COVERAGE_TARGET_PCT", "80"))
try:
    LOCAL_TZ = ZoneInfo("America/Chicago")
except ZoneInfoNotFoundError:
    LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone(timedelta(hours=-5), "Central")

SPORT_HEADERS = {
    "mlb": "baseball_mlb",
    "baseball": "baseball_mlb",
    "tennis": "tennis",
    "soccer": "soccer",
    "uefa super cup": "soccer",
    "ufc": "mma",
    "mma": "mma",
    "nfl": "americanfootball_nfl",
    "cfb": "americanfootball_ncaaf",
    "college fb": "americanfootball_ncaaf",
    "college football": "americanfootball_ncaaf",
    "ncaa football": "americanfootball_ncaaf",
    "ncaaf": "americanfootball_ncaaf",
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "cbb": "basketball_ncaab",
    "ncaab": "basketball_ncaab",
    "college basketball": "basketball_ncaab",
    "ncaa basketball": "basketball_ncaab",
    "nhl": "icehockey_nhl",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local_now():
    return datetime.now(LOCAL_TZ)


def _parse_discord_date_hint(value, reference_date=None):
    """Return the local post date when a copied Discord timestamp exposes it."""
    text = _plain(value)
    lowered = text.lower()
    reference_date = reference_date or _local_now().date()
    if re.search(r"\byesterday(?:\s+at\b|\b)", lowered):
        return (reference_date - timedelta(days=1)).isoformat(), "discord_yesterday"
    if re.search(r"\btoday(?:\s+at\b|\b)", lowered):
        return reference_date.isoformat(), "discord_today"
    # A time-only Discord divider belongs to the current local day.
    if re.search(r"(?:^|\s)[—–-]?\s*\d{1,2}:\d{2}\s*(?:am|pm)\s*$", text, flags=re.I):
        return reference_date.isoformat(), "discord_time_today"
    for pattern, formats in (
        (r"\b(\d{4}-\d{1,2}-\d{1,2})\b", ("%Y-%m-%d",)),
        (r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", ("%m/%d/%Y", "%m/%d/%y")),
        (
            r"\b((?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?)\b",
            ("%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d"),
        ),
    ):
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        raw = match.group(1)
        for fmt in formats:
            try:
                parsed = datetime.strptime(raw, fmt)
                if "%Y" not in fmt and "%y" not in fmt:
                    parsed = parsed.replace(year=reference_date.year)
                return parsed.date().isoformat(), "discord_explicit_date"
            except ValueError:
                continue
    return None, None


def _valid_event_date(value):
    try:
        return date.fromisoformat(str(value or "").strip()).isoformat()
    except (TypeError, ValueError):
        return None


def _plain(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalized_pick_syntax(value):
    """Normalize common copied-post formatting without changing pick meaning."""
    text = _strip_prefix(value)
    text = text.replace("\u2212", "-").replace("\ufe63", "-").replace("\uff0d", "-")
    text = text.replace("\uff0b", "+")
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"\b(?:money\s*line|moneyline)\b", "ML", text, flags=re.I)
    text = re.sub(r"\b(?:units?|unit)\b", "u", text, flags=re.I)
    text = re.sub(r"\(\s*([+-]\d+)\s*\)", r"\1", text)
    text = re.sub(r"\s+@\s+(?=[+-]\d+\b)", " ", text)
    # Copied Markdown/email lines can retain their hard-line-break backslash.
    # It is formatting only when it trails the complete pick.
    text = re.sub(r"\s*\\+\s*$", "", text)
    return _plain(text)


def _strip_prefix(value):
    value = _plain(value)
    value = re.sub(r"^[^\w$+\-]+", "", value, flags=re.UNICODE)
    return value.strip()


def _inline_sport_hint(value):
    """Recognize sport emoji on pick lines that omit a standalone heading."""
    text = str(value or "").lower()
    markers = (
        (("🎾", "ðÿž¾", "ðŸŽ¾"), "tennis", "Tennis"),
        (("⚾", "âš¾"), "baseball_mlb", "MLB"),
        (("🏈",), "americanfootball_nfl", "NFL"),
        (("🏀",), "basketball_nba", "NBA"),
        (("🏒",), "icehockey_nhl", "NHL"),
        (("⚽", "âš½"), "soccer", "Soccer"),
        (("🥊",), "mma", "UFC"),
    )
    for aliases, sport_key, label in markers:
        if any(alias.lower() in text for alias in aliases):
            return sport_key, label
    return None, None


def _inline_sport_hints(value):
    """Return distinct inline sport hints in the order they appear."""
    text = str(value or "").lower()
    variants = [text]
    for encoding in ("cp1252", "latin1"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired not in variants:
            variants.append(repaired)
    markers = (
        (("\U0001f94a", "dwcs"), "mma", "MMA/DWCS"),
        (("\U0001f3be", "tennis"), "tennis", "Tennis"),
        (("\u26be",), "baseball_mlb", "MLB"),
        (("\U0001f3c8",), "americanfootball_nfl", "NFL"),
        (("\U0001f3c0",), "basketball_nba", "NBA"),
        (("\U0001f3d2",), "icehockey_nhl", "NHL"),
        (("\u26bd",), "soccer", "Soccer"),
    )
    found = []
    for aliases, sport_key, label in markers:
        positions = [candidate.find(alias) for candidate in variants for alias in aliases if candidate.find(alias) >= 0]
        if positions:
            found.append((min(positions), sport_key, label))
    found.sort(key=lambda row: row[0])
    result = []
    seen = set()
    for _position, sport_key, label in found:
        if sport_key not in seen:
            result.append((sport_key, label))
            seen.add(sport_key)
    return result


def _inline_sport_hint(value):
    """Return the first inline sport hint for legacy callers."""
    hints = _inline_sport_hints(value)
    return hints[0] if hints else (None, None)


def _pick_line_sport(current_sport, current_label, hints):
    """Apply an explicit pick-line sport while preserving a specific league heading."""
    if len(hints) != 1:
        return current_sport, current_label
    hinted_sport, hinted_label = hints[0]
    if hinted_sport == current_sport:
        return current_sport, current_label
    if hinted_sport == "basketball_nba" and str(current_sport).startswith("basketball_"):
        return current_sport, current_label
    if hinted_sport == "americanfootball_nfl" and str(current_sport).startswith("americanfootball_"):
        return current_sport, current_label
    return hinted_sport, hinted_label


def _parlay_bullet_text(value):
    """Return (is_bullet, clean_text) for Unicode and copied mojibake bullets."""
    text = _plain(value)
    bullet = re.match(r"^(?:•|·|▪|◦|â€¢|[-*])\s*(.+)$", text)
    if not bullet:
        return False, _strip_prefix(text)
    return True, _strip_prefix(bullet.group(1))


def _ladder_transition_line(value):
    text = _plain(value).lower().replace(",", "")
    arrow = r"(?:—>|–>|→|->|-->|â€”>|â€“>)"
    money = rf"^\$?\d+(?:\.\d+)?\s*{arrow}\s*\$?\d+(?:\.\d+)?$"
    units = rf"^\d+(?:\.\d+)?u\s*{arrow}\s*\d+(?:\.\d+)?u?$"
    return bool(re.match(money, text) or re.match(units, text))


def _sport_from_header(line):
    lowered = _strip_prefix(line).lstrip("\ufe0f ").lower()
    # Cappers commonly label a slate as ``Saturday CFB`` or ``Friday NCAAF``.
    # Preserve the weekday for event-date resolution while recognizing the
    # sport portion exactly instead of allowing the football emoji to default
    # the entire post to NFL.
    header_text = re.sub(
        r"^(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+",
        "",
        lowered,
    )
    for label, key in sorted(SPORT_HEADERS.items(), key=lambda row: len(row[0]), reverse=True):
        if re.match(rf"^{re.escape(label)}(?:\s+add|\s+picks?|\s*$)", header_text) or (
            label == "soccer" and re.match(r"^(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+soccer$", lowered)
        ):
            return key, label.upper() if label in {"mlb", "ufc", "nfl", "cfb", "ncaaf", "nba", "wnba", "cbb", "ncaab", "nhl"} else label.title()
    return None, None


def _mixed_header_sports(line):
    """Recognize each explicit sport in headings such as Tennis Add & CFB."""
    parts = re.split(r"\s*(?:&|\+|\band\b)\s*", line, flags=re.I)
    if len(parts) < 2:
        return []
    sports = [_sport_from_header(part) for part in parts]
    return sports if all(sport for sport, _label in sports) else []


def _sport_prefixed_pick(value):
    """Split a same-line sport label from the structured pick that follows it."""
    text = _plain(value)
    for label, key in sorted(SPORT_HEADERS.items(), key=lambda row: len(row[0]), reverse=True):
        match = re.match(
            rf"^{re.escape(label)}(?:\s*[:|—–-]\s*|\s+)(?P<pick>.+)$",
            text,
            flags=re.I,
        )
        if not match:
            continue
        pick = _plain(match.group("pick"))
        if not re.search(r"[+-]\d+\s+\d+(?:\.\d+)?\s*u$", pick, flags=re.I):
            continue
        display = label.upper() if label in {"mlb", "ufc", "nfl", "cfb", "ncaaf", "nba", "wnba", "cbb", "ncaab", "nhl"} else label.title()
        return key, display, pick
    return None, None, text


def _ignored_line(line):
    lowered = _plain(line).lower()
    return (
        not lowered
        or _ladder_transition_line(lowered)
        or lowered.startswith("@influencedbets")
        or lowered.startswith("by: influencedbets")
        or lowered == "influencedbets"
        or lowered == "app"
        or " — yesterday at " in lowered
        or re.search(r"\s—\s\d{1,2}:\d{2}\s*(?:am|pm)$", lowered) is not None
        or lowered.startswith("to make this bet")
        or lowered.startswith("half the wager")
        or lowered.startswith("let’s climb")
        or lowered.startswith("lets climb")
        or re.match(r"^\$?[\d,.]+\s*(?:—|->|-->)\s*\$?[\d,.]+$", lowered) is not None
        or re.match(r"^\d+(?:\.\d+)?u\s*(?:—|->|-->)\s*\d+(?:\.\d+)?u$", lowered) is not None
    )


def _base_draft(sport_key, sport_label, raw_text):
    return {
        "draft_id": uuid.uuid4().hex,
        "source": SOURCE_NAME,
        "sport_key": sport_key or "",
        "sport_label": sport_label or "Unknown",
        "selection": "",
        "market_type": "",
        "market_line": None,
        "posted_odds": None,
        "capper_units": None,
        "pick_type": "straight",
        "legs": [],
        "components": [],
        "executable": True,
        "parser_confidence": 1.0,
        "warnings": [],
        "raw_text": raw_text,
        "target_event_date": _local_now().date().isoformat(),
        "event_date_source": "import_date_default",
        "parser_version": PARSER_VERSION,
    }


def _component(selection, market_type, market_line=None, risk_fraction=1.0, line_unit=None):
    component = {
        "component_id": uuid.uuid4().hex[:12],
        "selection": _plain(selection),
        "market_type": market_type,
        "market_line": market_line,
        "risk_fraction": float(risk_fraction),
        "status": "watching",
        "placed_units": 0,
        "fills": [],
    }
    if line_unit:
        component["line_unit"] = str(line_unit)
    return component


def _combat_method_prop(value):
    """Parse an MMA method prop without ever degrading it to a moneyline."""
    text = _plain(value)
    patterns = (
        (r"(?P<selection>.+?)\s+(?:to\s+win\s+)?by\s+(?:ko\s*/\s*tko|tko\s*/\s*ko|ko\s+or\s+tko|tko\s+or\s+ko|ko|tko)$", "ko_tko"),
        (r"(?P<selection>.+?)\s+(?:to\s+win\s+)?by\s+(?:submission|sub)$", "submission"),
        (r"(?P<selection>.+?)\s+(?:to\s+win\s+)?by\s+(?:decision|points?)$", "decision"),
        (r"(?P<selection>.+?)\s+(?:to\s+win\s+)?(?:inside\s+the\s+distance|by\s+finish)$", "inside_distance"),
    )
    for pattern, method in patterns:
        match = re.match(pattern, text, flags=re.I)
        if match:
            return {
                "selection": _plain(match.group("selection")),
                "market_type": "method_of_victory",
                "market_line": None,
                "method": method,
                "executable": False,
                "unsupported_reason": COMBAT_PROP_UNSUPPORTED_REASON,
                "prop_selection": text,
            }
    return None


def _parlay_leg_market(value, sport_key=""):
    """Parse an unpriced parlay leg without degrading derivatives to moneyline."""
    text = _plain(value)
    combat_prop = _combat_method_prop(text) if sport_key == "mma" else None
    if combat_prop:
        return combat_prop

    if sport_key == "soccer":
        double_chance_match = re.match(
            r"(?:(?P<team_first>.+?)\s*(?:&|or)\s*draw|draw\s*(?:&|or)\s*(?P<draw_first_team>.+))$",
            text,
            flags=re.I,
        )
        if double_chance_match:
            team = _plain(
                double_chance_match.group("team_first")
                or double_chance_match.group("draw_first_team")
            )
            return {
                "selection": f"{team} or Draw",
                "market_type": "double_chance",
                "market_line": None,
                "double_chance_side": "team_or_draw",
                "event_hint": team,
                "executable": False,
                "unsupported_reason": SOCCER_DOUBLE_CHANCE_UNSUPPORTED_REASON,
            }

    btts_match = re.match(r"(?P<event>.+?)\s+BTTS\s+(?P<side>YES|NO)$", text, flags=re.I)
    if btts_match:
        return {
            "selection": _plain(btts_match.group("event")),
            "market_type": "btts",
            "market_line": None,
            "btts_side": btts_match.group("side").lower(),
        }

    total_match = re.match(
        r"(?:(?P<event>.+?)\s+)?(?P<side>over|under|o|u)\s*"
        r"(?P<line>\d+(?:\.\d+)?)(?:\s+(?P<unit>(?:match\s+)?goals?|games?|sets?))?$",
        text,
        flags=re.I,
    )
    if total_match:
        side_token = total_match.group("side").lower()
        side = "over" if side_token in {"over", "o"} else "under"
        line = float(total_match.group("line"))
        row = {
            "selection": f"{side.title()} {line:g}",
            "market_type": "total",
            "market_line": line,
            "total_side": side,
        }
        event_hint = _plain(total_match.group("event"))
        if event_hint:
            row["event_hint"] = event_hint
        normalize_unit = _plain(total_match.group("unit")).lower()
        if normalize_unit.startswith("game"):
            row["line_unit"] = "game"
        elif normalize_unit.startswith("set"):
            row["line_unit"] = "set"
        return row

    leg_match = re.match(
        r"(.+?)\s+(ML|[+-]\d+(?:\.\d+)?)(?:\s+(Sets?|Games?))?$",
        text,
        flags=re.I,
    )
    if leg_match:
        market_token = leg_match.group(2).upper()
        row = {
            "selection": _plain(leg_match.group(1)),
            "market_type": "moneyline" if market_token == "ML" else "spread",
            "market_line": None if market_token == "ML" else float(market_token),
        }
        if leg_match.group(3):
            row["line_unit"] = leg_match.group(3).lower().rstrip("s")
        return row
    return None


def _parlay_leg_label(row):
    """Keep derivative meaning visible in the tracked parent description."""
    market_type = row.get("market_type")
    selection = _plain(row.get("selection"))
    if market_type == "btts":
        return f"{selection} BTTS {str(row.get('btts_side') or '').upper()}".strip()
    if market_type == "total":
        event_hint = _plain(row.get("event_hint"))
        return f"{event_hint + ' ' if event_hint else ''}{selection}".strip()
    return selection


def _finish_straight(draft):
    if draft.get("sport_key") == "baseball_mlb" and draft["market_type"] == "spread" and abs(float(draft.get("market_line") or 0) + 1.0) < 1e-9:
        draft["pick_type"] = "synthetic_run_line"
        draft["components"] = [
            _component(draft["selection"], "moneyline", None, 0.5),
            _component(draft["selection"], "spread", -1.5, 0.5),
        ]
        draft["warnings"].append("MLB -1 will be synthesized as equal-risk ML and -1.5 positions; both must qualify before execution.")
    else:
        component = _component(
            draft["selection"],
            draft["market_type"],
            draft.get("market_line"),
            1.0,
            line_unit=draft.get("line_unit"),
        )
        if draft.get("first_inning_side"):
            component["first_inning_side"] = draft["first_inning_side"]
        if draft.get("btts_side"):
            component["btts_side"] = draft["btts_side"]
        for key in ("total_side", "event_hint", "team_total_team"):
            if draft.get(key):
                component[key] = draft[key]
        draft["components"] = [component]
    if not draft.get("sport_key"):
        draft["parser_confidence"] = 0.55
        draft["warnings"].append("Sport was not recognized; edit it before approval.")
    return draft


def parse_capper_text(text):
    """Parse copied structured posts into reviewable drafts without persisting."""
    raw = html.unescape(str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return {"drafts": [], "warnings": ["Paste at least one pick."], "source": SOURCE_NAME}

    lines = raw.split("\n")
    drafts = []
    current_sport = ""
    current_label = "Unknown"
    current_event_day_hint = ""
    reference_date = _local_now().date()
    current_target_event_date = reference_date.isoformat()
    current_event_date_source = "import_date_default"
    pending_mixed_sports = []
    league_context = {}
    ladder_mode = False
    index = 0
    while index < len(lines):
        original = lines[index]
        line = _strip_prefix(original)
        pick_line = _normalized_pick_syntax(original)
        inline_hints = _inline_sport_hints(original)
        inline_sport, inline_label = inline_hints[0] if inline_hints else (None, None)
        if inline_sport and not current_sport:
            current_sport, current_label = inline_sport, inline_label
        if len(inline_hints) > 1:
            pending_mixed_sports = inline_hints
        timestamp_date, timestamp_source = _parse_discord_date_hint(original, reference_date=reference_date)
        if timestamp_date:
            current_target_event_date = timestamp_date
            current_event_date_source = timestamp_source
            index += 1
            continue
        header_sports = _mixed_header_sports(line)
        sport_key, sport_label = header_sports[0] if header_sports else _sport_from_header(line)
        if sport_key:
            for key, label in header_sports or [(sport_key, sport_label)]:
                if key.startswith(("americanfootball_", "basketball_")):
                    league_context[key.split("_")[0]] = (key, label)
            if header_sports:
                pending_mixed_sports = header_sports
            current_sport, current_label = sport_key, sport_label
            day_match = re.match(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", line, flags=re.I)
            current_event_day_hint = day_match.group(1).title() if day_match else ""
            if current_sport in {"americanfootball_ncaaf", "basketball_ncaab"} and current_event_day_hint:
                weekday_index = {
                    "Monday": 0,
                    "Tuesday": 1,
                    "Wednesday": 2,
                    "Thursday": 3,
                    "Friday": 4,
                    "Saturday": 5,
                    "Sunday": 6,
                }[current_event_day_hint]
                days_ahead = (weekday_index - reference_date.weekday()) % 7
                current_target_event_date = (reference_date + timedelta(days=days_ahead)).isoformat()
                current_event_date_source = "header_weekday"
            ladder_mode = False
            index += 1
            continue
        prefixed_sport, prefixed_label, prefixed_pick = _sport_prefixed_pick(pick_line)
        if prefixed_sport:
            current_sport, current_label = prefixed_sport, prefixed_label
            if prefixed_sport.startswith(("americanfootball_", "basketball_")):
                league_context[prefixed_sport.split("_")[0]] = (prefixed_sport, prefixed_label)
            pick_line = prefixed_pick
        # Football/basketball emoji identify a family, not NFL/NBA. Retain an
        # explicit league even while another sport's picks intervene.
        inline_hints = [
            league_context.get(key.split("_")[0], (key, label))
            if key in {"americanfootball_nfl", "basketball_nba"} else (key, label)
            for key, label in inline_hints
        ]
        if re.match(r"^ladder\b", line, flags=re.I):
            ladder_mode = True
            index += 1
            continue
        parlay_match = re.search(
            r"(?P<legs>\d+)\s*(?:-\s*)?leg\s+parlay\s+(?P<odds>[+-]\d+)(?:\s+(?P<units>\d+(?:\.\d+)?)u)?",
            pick_line,
            flags=re.I,
        )
        if parlay_match:
            parlay_sports = inline_hints if len(inline_hints) > 1 else pending_mixed_sports
            parlay_sport, parlay_label = _pick_line_sport(
                current_sport,
                current_label,
                inline_hints,
            )
            leg_rows = []
            ladder_evidence = False
            cursor = index + 1
            while cursor < len(lines):
                is_bullet, next_line = _parlay_bullet_text(lines[cursor])
                next_pick_line = _normalized_pick_syntax(next_line)
                if not next_line:
                    cursor += 1
                    continue
                if _sport_from_header(next_line)[0] or re.match(r"^ladder\b", next_line, flags=re.I):
                    break
                if re.search(r"\d+\s*(?:-\s*)?leg\s+parlay", next_line, flags=re.I):
                    break
                leg_row = _parlay_leg_market(next_pick_line, parlay_sport)
                if leg_row:
                    leg_rows.append(leg_row)
                    cursor += 1
                    continue
                if (
                    is_bullet
                    and len(leg_rows) < int(parlay_match.group("legs"))
                    and not _ignored_line(next_line)
                ):
                    leg_rows.append({
                        "selection": _plain(next_line),
                        "market_type": "moneyline",
                        "market_line": None,
                        "market_inferred": True,
                    })
                    cursor += 1
                    continue
                if _ladder_transition_line(next_line):
                    ladder_evidence = True
                    cursor += 1
                    continue
                if _ignored_line(next_line):
                    cursor += 1
                    continue
                break
            matchup_hints = [
                row.get("selection")
                for row in leg_rows
                if row.get("market_type") == "btts" and "/" in str(row.get("selection") or "")
            ]
            if not matchup_hints and parlay_sport == "soccer":
                # A generic "O2.5 Match Goals" leg in a two-leg soccer post is
                # for the same game as its one named team leg. Preserve that
                # team as an event anchor; direction and line alone are unsafe
                # on a slate with several simultaneous matches.
                matchup_hints = list(dict.fromkeys(
                    _plain(row.get("event_hint") or row.get("selection"))
                    for row in leg_rows
                    if row.get("market_type") in {"moneyline", "spread", "double_chance"}
                    and _plain(row.get("event_hint") or row.get("selection"))
                ))
            if len(matchup_hints) == 1:
                for leg_row in leg_rows:
                    if leg_row.get("market_type") == "total" and not leg_row.get("event_hint"):
                        leg_row["event_hint"] = matchup_hints[0]
            draft = _base_draft(parlay_sport, parlay_label, "\n".join(lines[index:cursor]).strip())
            draft["event_day_hint"] = current_event_day_hint
            draft["target_event_date"] = current_target_event_date
            draft["event_date_source"] = current_event_date_source
            draft.update({
                "selection": " + ".join(_parlay_leg_label(row) for row in leg_rows),
                "market_type": "parlay",
                "posted_odds": int(parlay_match.group("odds")),
                "capper_units": float(parlay_match.group("units")) if parlay_match.group("units") else None,
                "pick_type": "ladder" if ladder_mode or ladder_evidence else "parlay",
                "legs": leg_rows,
                "components": [],
                "executable": False,
                "leg_watch_enabled": not (ladder_mode or ladder_evidence),
                "parser_confidence": 0.98 if leg_rows else 0.65,
                "warnings": [
                    "The combined parlay is tracked only; after approval, each leg becomes an independent value-qualified watch ticket."
                ],
            })
            if len(parlay_sports) == len(leg_rows):
                for leg_row, (leg_sport, leg_label) in zip(leg_rows, parlay_sports):
                    leg_row["sport_key"] = leg_sport
                    leg_row["sport_label"] = leg_label
                draft["warnings"].append("Mixed-sport parlay detected; each leg keeps its own sport.")
            elif len(inline_hints) == 1:
                for leg_row in leg_rows:
                    leg_row["sport_key"], leg_row["sport_label"] = inline_hints[0]
            else:
                for leg_row in leg_rows:
                    leg_row["sport_key"] = current_sport
                    leg_row["sport_label"] = current_label
            if ladder_mode or ladder_evidence:
                draft["warnings"] = ["Tracked only: ladder posts are never auto-executed."]
            elif any(not row.get("executable", True) for row in leg_rows):
                if any(
                    row.get("unsupported_reason") == SOCCER_DOUBLE_CHANCE_UNSUPPORTED_REASON
                    for row in leg_rows
                ):
                    draft["warnings"].append(
                        "Soccer double-chance legs are tracked exactly and never substituted with a team moneyline; use a manual sportsbook until exact independently priced execution is supported."
                    )
                if any(
                    row.get("unsupported_reason") == COMBAT_PROP_UNSUPPORTED_REASON
                    for row in leg_rows
                ):
                    draft["warnings"].append(
                        "Combat method props are tracked exactly and never substituted with a fighter moneyline; use a manual sportsbook until exact independently priced execution is supported."
                    )
            elif any(row.get("market_inferred") for row in leg_rows):
                draft["warnings"].append("A parlay leg without an explicit market was interpreted as moneyline; review before approval.")
            if len(leg_rows) != int(parlay_match.group("legs")):
                draft["warnings"].append(f"Expected {parlay_match.group('legs')} legs but parsed {len(leg_rows)}.")
                draft["parser_confidence"] = min(draft["parser_confidence"], 0.6)
            drafts.append(draft)
            pending_mixed_sports = []
            index = max(cursor, index + 1)
            continue

        first_inning_match = re.match(
            r"(.+?)\s+(YRFI|NRFI)\s+([+-]\d+)\s+(\d+(?:\.\d+)?)\s*u$",
            pick_line,
            flags=re.I,
        )
        ml_match = re.match(r"(.+?)\s+ML\s+([+-]\d+)\s+(\d+(?:\.\d+)?)\s*u$", pick_line, flags=re.I)
        ladder_ml_match = re.match(r"(.+?)\s+ML\s+([+-]\d+)(?:\s+(\d+(?:\.\d+)?)\s*u)?$", pick_line, flags=re.I) if ladder_mode else None
        spread_match = re.match(r"(.+?)\s+([+-]\d+(?:\.\d+)?)(?:\s+(Sets?|Games?))?\s+([+-]\d+)\s+(\d+(?:\.\d+)?)\s*u$", pick_line, flags=re.I)
        team_total_match = re.match(r"(.+?)\s+team\s+total\s+(over|under|o|u)\s*(\d+(?:\.\d+)?)\s+([+-]\d+)\s+(\d+(?:\.\d+)?)\s*u$", pick_line, flags=re.I)
        total_match = re.match(
            r"(?P<event>.+?)\s+(?:game\s+total\s+)?(?P<side>over|under|o|u)\s*"
            r"(?P<line>\d+(?:\.\d+)?)(?:\s+(?P<line_unit>(?:match\s+)?goals?|games?|sets?))?\s+"
            r"(?P<odds>[+-]\d+)\s+(?P<units>\d+(?:\.\d+)?)\s*u$",
            pick_line,
            flags=re.I,
        )
        btts_match = re.match(r"(.+?)\s+BTTS\s+(YES|NO)\s+([+-]\d+)\s+(\d+(?:\.\d+)?)\s*u$", pick_line, flags=re.I)
        pick_sport, pick_label = _pick_line_sport(current_sport, current_label, inline_hints)
        combat_prop_match = re.match(
            r"(?P<prop>.+?)\s+(?P<odds>[+-]\d+)\s+(?P<units>\d+(?:\.\d+)?)\s*u$",
            pick_line,
            flags=re.I,
        )
        combat_prop = (
            _combat_method_prop(combat_prop_match.group("prop"))
            if combat_prop_match and pick_sport == "mma"
            else None
        )
        if first_inning_match:
            first_inning_side = first_inning_match.group(2).upper()
            draft = _base_draft(pick_sport, pick_label, original.strip())
            draft["event_day_hint"] = current_event_day_hint
            draft["target_event_date"] = current_target_event_date
            draft["event_date_source"] = current_event_date_source
            draft.update(
                selection=f"{_plain(first_inning_match.group(1))} {first_inning_side}",
                market_type="first_inning_run",
                first_inning_side=first_inning_side.lower(),
                posted_odds=int(first_inning_match.group(3)),
                capper_units=float(first_inning_match.group(4)),
            )
            draft["warnings"].append(
                f"{first_inning_side} maps to Kalshi's {'YES' if first_inning_side == 'YRFI' else 'NO'} side and may execute only before first pitch."
            )
            drafts.append(_finish_straight(draft))
        elif btts_match:
            btts_side = btts_match.group(2).lower()
            draft = _base_draft(pick_sport, pick_label, original.strip())
            draft["event_day_hint"] = current_event_day_hint
            draft["target_event_date"] = current_target_event_date
            draft["event_date_source"] = current_event_date_source
            draft.update(
                selection=_plain(btts_match.group(1)),
                market_type="btts",
                btts_side=btts_side,
                posted_odds=int(btts_match.group(3)),
                capper_units=float(btts_match.group(4)),
            )
            draft["warnings"].append(
                f"BTTS {btts_side.upper()} will map only to the exact full-game Kalshi matchup contract."
            )
            drafts.append(_finish_straight(draft))
        elif combat_prop:
            draft = _base_draft(pick_sport, pick_label, original.strip())
            draft["event_day_hint"] = current_event_day_hint
            draft["target_event_date"] = current_target_event_date
            draft["event_date_source"] = current_event_date_source
            draft.update(
                selection=combat_prop["selection"],
                market_type=combat_prop["market_type"],
                method=combat_prop["method"],
                prop_selection=combat_prop["prop_selection"],
                posted_odds=int(combat_prop_match.group("odds")),
                capper_units=float(combat_prop_match.group("units")),
                executable=False,
                unsupported_reason=COMBAT_PROP_UNSUPPORTED_REASON,
            )
            draft["warnings"].append(
                "Tracked only: this exact combat method prop cannot fall back to moneyline execution. Use a manual sportsbook until an independently priced exact market is supported."
            )
            finished = _finish_straight(draft)
            finished["components"][0]["method"] = combat_prop["method"]
            finished["components"][0]["executable"] = False
            finished["components"][0]["unsupported_reason"] = COMBAT_PROP_UNSUPPORTED_REASON
            drafts.append(finished)
        elif ml_match:
            draft = _base_draft(pick_sport, pick_label, original.strip())
            draft["event_day_hint"] = current_event_day_hint
            draft["target_event_date"] = current_target_event_date
            draft["event_date_source"] = current_event_date_source
            draft.update(selection=_plain(ml_match.group(1)), market_type="moneyline", posted_odds=int(ml_match.group(2)), capper_units=float(ml_match.group(3)))
            drafts.append(_finish_straight(draft))
        elif ladder_ml_match:
            draft = _base_draft(pick_sport, pick_label, original.strip())
            draft["event_day_hint"] = current_event_day_hint
            draft["target_event_date"] = current_target_event_date
            draft["event_date_source"] = current_event_date_source
            draft.update(
                selection=_plain(ladder_ml_match.group(1)),
                market_type="moneyline",
                posted_odds=int(ladder_ml_match.group(2)),
                capper_units=float(ladder_ml_match.group(3)) if ladder_ml_match.group(3) else None,
                pick_type="ladder",
                executable=False,
                components=[],
                leg_watch_enabled=False,
                warnings=["Tracked only: ladder posts are never auto-executed."],
            )
            drafts.append(draft)
        elif team_total_match:
            team = _plain(team_total_match.group(1))
            direction = "Over" if team_total_match.group(2).lower() in {"over", "o"} else "Under"
            draft = _base_draft(pick_sport, pick_label, original.strip())
            draft["event_day_hint"] = current_event_day_hint
            draft["target_event_date"] = current_target_event_date
            draft["event_date_source"] = current_event_date_source
            draft.update(
                selection=f"{team} {direction} {team_total_match.group(3)}",
                market_type="team_total",
                market_line=float(team_total_match.group(3)),
                total_side=direction.lower(),
                team_total_team=team,
                event_hint=team,
                posted_odds=int(team_total_match.group(4)),
                capper_units=float(team_total_match.group(5)),
            )
            draft["warnings"].append(
                "Team total preserved as a distinct market and will never be substituted with a full-game total."
            )
            drafts.append(_finish_straight(draft))
        elif total_match:
            direction = "Over" if total_match.group("side").lower() in {"over", "o"} else "Under"
            event_hint = re.sub(r"\b(?:game\s+)?total\b", "", _plain(total_match.group("event")), flags=re.I).strip(" :-")
            raw_line_unit = (total_match.group("line_unit") or "").lower()
            # Soccer totals are already unambiguously goal totals. Keep the
            # generic full-match total unit empty so execution can match the
            # existing Kalshi soccer-total candidates; tennis still needs its
            # explicit game/set distinction.
            line_unit = raw_line_unit.rstrip("s") if raw_line_unit and "goal" not in raw_line_unit else None
            draft = _base_draft(pick_sport, pick_label, original.strip())
            draft["event_day_hint"] = current_event_day_hint
            draft["target_event_date"] = current_target_event_date
            draft["event_date_source"] = current_event_date_source
            draft.update(
                selection=f"{direction} {total_match.group('line')}",
                market_type="total",
                market_line=float(total_match.group("line")),
                total_side=direction.lower(),
                posted_odds=int(total_match.group("odds")),
                capper_units=float(total_match.group("units")),
            )
            if line_unit:
                draft["line_unit"] = line_unit
            if event_hint:
                draft["event_hint"] = event_hint
            drafts.append(_finish_straight(draft))
        elif spread_match:
            draft = _base_draft(pick_sport, pick_label, original.strip())
            draft["event_day_hint"] = current_event_day_hint
            draft["target_event_date"] = current_target_event_date
            draft["event_date_source"] = current_event_date_source
            draft.update(selection=_plain(spread_match.group(1)), market_type="spread", market_line=float(spread_match.group(2)), posted_odds=int(spread_match.group(4)), capper_units=float(spread_match.group(5)))
            if spread_match.group(3):
                line_unit = spread_match.group(3).lower().rstrip("s")
                draft["line_unit"] = line_unit
            drafts.append(_finish_straight(draft))
            if spread_match.group(3):
                draft["components"][0]["line_unit"] = draft["line_unit"]
        index += 1

    warnings = []
    if not drafts:
        warnings.append("No structured picks were recognized. Include the sport heading, selection, posted odds, and units.")
    for draft in drafts:
        target_date = _valid_event_date(draft.get("target_event_date"))
        if target_date and target_date < reference_date.isoformat():
            draft["warnings"].append("This copied post is dated in the past and cannot be approved for automatic execution.")
        day_hint = str(draft.get("event_day_hint") or "").strip()
        if target_date and day_hint and date.fromisoformat(target_date).strftime("%A").lower() != day_hint.lower():
            draft["warnings"].append("The copied weekday and target event date disagree; correct the date before approval.")
        draft["import_raw_text"] = raw.strip()
    return {"drafts": drafts, "warnings": warnings, "source": SOURCE_NAME}


def american_implied_probability(odds):
    odds = float(odds)
    if odds == 0:
        raise ValueError("American odds cannot be zero")
    return (100.0 / (odds + 100.0) if odds > 0 else -odds / (-odds + 100.0)) * 100.0


def mapped_base_units(capper_units):
    units = float(capper_units or 0)
    if units >= 5:
        return 4
    if units >= 4:
        return 3
    return 2


def dynamic_target_units(capper_units, edge, confidence, pro_score, final_score, *, live=False, authoritative_state=True):
    base = mapped_base_units(capper_units)
    edge = float(edge if edge is not None else -999)
    target = max(1, base - 1) if edge < 2.0 else base
    if edge >= 4.0 and float(confidence or 0) >= 82 and float(pro_score or 0) >= 95 and float(final_score or 0) >= 94:
        target = min(5, base + 1)
    if live and not authoritative_state:
        target = min(target, 2)
    return target


def posted_price_review(
    posted_odds,
    entry_price_cents,
    fee_edge_pp=0,
    *,
    minutes_until_start=None,
    live=False,
    tolerance_pp=2.0,
    base_tolerance_pp=0.0,
    final_window_minutes=15.0,
):
    posted_probability = american_implied_probability(posted_odds)
    effective_probability = float(entry_price_cents) + max(0.0, float(fee_edge_pp or 0))
    late_or_live = live or (
        minutes_until_start is not None
        and float(minutes_until_start) <= float(final_window_minutes)
    )
    tolerance = float(tolerance_pp) if late_or_live else float(base_tolerance_pp)
    difference = effective_probability - posted_probability
    return {
        "ok": difference <= tolerance + 1e-9,
        "posted_probability": round(posted_probability, 3),
        "effective_probability": round(effective_probability, 3),
        "difference_pp": round(difference, 3),
        "tolerance_pp": round(tolerance, 3),
        "reason": "price_eligible" if difference <= tolerance + 1e-9 else "waiting_for_posted_price",
    }


@contextmanager
def _store_lock(timeout=5.0):
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {_now()}".encode("utf-8"))
        except FileExistsError:
            try:
                if time.time() - LOCK_FILE.stat().st_mtime > 30:
                    LOCK_FILE.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("capper_ticket_store_lock_timeout")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            LOCK_FILE.unlink(missing_ok=True)


def _read_store():
    try:
        value = json.loads(STORE_FILE.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        value = {}
    return {
        "version": STORE_VERSION,
        "updated_at": value.get("updated_at"),
        "tickets": value.get("tickets") if isinstance(value.get("tickets"), list) else [],
    }


def _write_store(store):
    store["version"] = STORE_VERSION
    store["updated_at"] = _now()
    temporary = STORE_FILE.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(STORE_FILE)


def _unsupported_leg_warning(reason):
    if reason == SOCCER_DOUBLE_CHANCE_UNSUPPORTED_REASON:
        return "Tracked only: this exact soccer double-chance leg is never substituted with a team moneyline."
    return "Tracked only: the exact combat method prop is not substituted with a fighter moneyline."


def _unsupported_leg_manual_reason(reason):
    if reason == SOCCER_DOUBLE_CHANCE_UNSUPPORTED_REASON:
        return "exact_soccer_double_chance_market_not_supported_use_manual_sportsbook_if_desired"
    return "exact_method_of_victory_market_not_supported_use_manual_sportsbook_if_desired"


def _migrate_parser_metadata(store):
    """Repair active legacy tickets using the current deterministic parser."""
    changed = False
    active_statuses = {"watching", "unmatched", "waiting_price", "blocked", "ready"}

    def reparsed_draft(row):
        """Reparse a pick with its original slate heading when available.

        A single football-emoji pick line is inherently NFL-like without its
        surrounding ``CFB`` header.  Older migrations reparsed only
        ``raw_text`` and therefore could not repair the league.  Match the
        exact original line inside ``import_raw_text`` first, then retain the
        single-line fallback for legacy stores that predate full import text.
        """
        raw_text = str(row.get("raw_text") or "").strip()
        import_text = str(row.get("import_raw_text") or "").strip()
        if import_text and raw_text:
            contextual = [
                draft
                for draft in (parse_capper_text(import_text).get("drafts") or [])
                if draft.get("pick_type") == "straight"
                and _plain(draft.get("raw_text")).casefold() == _plain(raw_text).casefold()
            ]
            if len(contextual) == 1:
                return contextual[0]
        drafts = parse_capper_text(raw_text).get("drafts") or [] if raw_text else []
        if len(drafts) == 1 and drafts[0].get("pick_type") == "straight":
            return drafts[0]
        return None

    def reparsed_parlay_draft(row):
        raw_text = str(row.get("raw_text") or "").strip()
        import_text = str(row.get("import_raw_text") or "").strip()
        if import_text and raw_text:
            contextual = [
                draft
                for draft in (parse_capper_text(import_text).get("drafts") or [])
                if draft.get("pick_type") in {"parlay", "ladder"}
                and _plain(draft.get("raw_text")).casefold() == _plain(raw_text).casefold()
            ]
            if len(contextual) == 1:
                return contextual[0]
        drafts = parse_capper_text(raw_text).get("drafts") or [] if raw_text else []
        if len(drafts) == 1 and drafts[0].get("pick_type") in {"parlay", "ladder"}:
            return drafts[0]
        return None

    def recorded_units(row):
        values = []
        try:
            values.append(float(row.get("placed_units") or 0))
        except (TypeError, ValueError):
            pass
        filled = 0.0
        for fill in row.get("fills") or []:
            try:
                filled += max(0.0, float(fill.get("units") or 0))
            except (TypeError, ValueError):
                continue
        values.append(filled)
        return max(values or [0.0])

    def component_semantics(rows):
        keys = (
            "selection", "market_type", "market_line", "risk_fraction", "line_unit",
            "first_inning_side", "btts_side", "total_side", "event_hint",
            "team_total_team", "method", "executable", "unsupported_reason",
        )
        return [
            {key: row.get(key) for key in keys if key in row}
            for row in (rows or [])
        ]

    copied_fields = (
        "sport_key", "sport_label", "selection", "market_type", "market_line",
        "line_unit", "total_side", "event_hint", "team_total_team", "components",
        "parser_confidence", "warnings", "method", "prop_selection", "executable",
        "unsupported_reason",
    )
    for row in store.get("tickets") or []:
        if int(row.get("parser_version") or 0) >= PARSER_VERSION:
            continue
        if row.get("event_date_source") == "user_edited":
            # The copied post is evidence, not authority over a later explicit
            # correction (for example Inter -> Inter Milan).
            continue
        if row.get("status") not in active_statuses or row.get("pick_type") != "straight":
            continue
        repaired = reparsed_draft(row)
        if repaired is None:
            continue
        semantic_changed = False
        for key in copied_fields:
            if key in repaired:
                if key == "sport_key" and not repaired.get(key):
                    continue
                if key == "sport_label" and repaired.get(key) in {None, "", "Unknown"}:
                    continue
                if key == "components" and component_semantics(row.get(key)) == component_semantics(repaired[key]):
                    continue
                semantic_changed = semantic_changed or row.get(key) != repaired[key]
                row[key] = repaired[key]
            else:
                semantic_changed = semantic_changed or key in row
                row.pop(key, None)
        row["parser_version"] = PARSER_VERSION
        if semantic_changed:
            row["status"] = "watching" if row.get("executable", True) else "unsupported"
            row["status_reason"] = (
                f"parser_v{PARSER_VERSION}_repaired_awaiting_market_match"
                if row.get("executable", True)
                else row.get("unsupported_reason") or "tracked_non_executable_pick_type"
            )
            row["match_snapshot"] = {}
            row["schedule_snapshot"] = {}
            row["updated_at"] = _now()
            row["fingerprint"] = _fingerprint(row, row.get("target_event_date"))
            row.setdefault("history", []).append({
                "at": row["updated_at"],
                "status": row["status"],
                "reason": f"parser_v{PARSER_VERSION}_metadata_repair",
            })
        changed = True

    # Parent parlays were historically excluded from parser migrations because
    # their queue status is ``unsupported`` by design. They still contain the
    # source-of-truth semantics used to generate executable child watches, so a
    # parser correction must update both the parent and any unfilled children.
    reparsed_parents = {}
    for row in store.get("tickets") or []:
        if int(row.get("parser_version") or 0) >= PARSER_VERSION:
            if row.get("pick_type") in {"parlay", "ladder"}:
                reparsed_parents[row.get("ticket_id")] = row
            continue
        if row.get("pick_type") not in {"parlay", "ladder"}:
            continue
        if row.get("status") in {"placed", "settled", "cancelled", "expired"}:
            continue
        target_date = _valid_event_date(row.get("target_event_date"))
        if target_date and target_date < _local_now().date().isoformat():
            continue
        repaired = reparsed_parlay_draft(row)
        if repaired is None:
            continue
        parent_fields = (
            "sport_key", "sport_label", "selection", "market_type", "market_line",
            "posted_odds", "capper_units", "pick_type", "legs", "executable",
            "leg_watch_enabled", "parser_confidence", "warnings",
        )
        semantic_changed = any(row.get(key) != repaired.get(key) for key in parent_fields)
        for key in parent_fields:
            if key in repaired:
                row[key] = repaired[key]
            else:
                row.pop(key, None)
        row["parser_version"] = PARSER_VERSION
        row["fingerprint"] = _fingerprint(row, row.get("target_event_date"))
        if semantic_changed:
            row["status"] = "unsupported"
            row["status_reason"] = (
                "combined_parlay_tracked_individual_legs_watching"
                if row.get("pick_type") == "parlay" and row.get("leg_watch_enabled", True)
                else row.get("unsupported_reason") or "tracked_non_executable_pick_type"
            )
            row["match_snapshot"] = {}
            row["updated_at"] = _now()
            row.setdefault("history", []).append({
                "at": row["updated_at"],
                "status": row["status"],
                "reason": f"parser_v{PARSER_VERSION}_parlay_metadata_repair",
            })
        reparsed_parents[row.get("ticket_id")] = row
        changed = True

    child_terminal_statuses = {"placed", "settled", "cancelled", "expired"}
    child_semantic_keys = (
        "selection", "market_type", "market_line", "risk_fraction", "line_unit",
        "method", "first_inning_side", "btts_side", "double_chance_side",
        "total_side", "event_hint", "team_total_team", "executable",
        "unsupported_reason",
    )
    for child in store.get("tickets") or []:
        if child.get("pick_type") != "parlay_leg":
            continue
        parent = reparsed_parents.get(child.get("parent_ticket_id"))
        if not parent or child.get("status") in child_terminal_statuses or recorded_units(child) > 0:
            continue
        try:
            leg = list(parent.get("legs") or [])[int(child.get("parlay_leg_index") or 0) - 1]
        except (IndexError, TypeError, ValueError):
            continue
        expected_component = _component(
            leg.get("selection"),
            leg.get("market_type"),
            leg.get("market_line"),
            1.0,
            leg.get("line_unit"),
        )
        expected_component["component_id"] = (
            ((child.get("components") or [{}])[0]).get("component_id")
            or expected_component["component_id"]
        )
        leg_executable = bool(leg.get("executable", True))
        expected_component["executable"] = leg_executable
        for key in child_semantic_keys:
            if key in {"selection", "market_type", "market_line", "risk_fraction", "line_unit", "executable"}:
                continue
            if leg.get(key) is not None:
                expected_component[key] = leg[key]
        current_component = (child.get("components") or [{}])[0]
        current_semantics = {key: current_component.get(key) for key in child_semantic_keys}
        expected_semantics = {key: expected_component.get(key) for key in child_semantic_keys}
        child_fields = (
            "selection", "market_type", "market_line", "line_unit", "method",
            "first_inning_side", "btts_side", "double_chance_side", "total_side",
            "event_hint", "team_total_team", "unsupported_reason",
        )
        semantic_changed = current_semantics != expected_semantics or any(
            child.get(key) != leg.get(key) for key in child_fields
        ) or bool(child.get("executable", True)) != leg_executable
        for key in child_fields:
            if leg.get(key) is not None:
                child[key] = leg[key]
            else:
                child.pop(key, None)
        child["executable"] = leg_executable
        child_version_changed = int(child.get("parser_version") or 0) != PARSER_VERSION
        child["parser_version"] = PARSER_VERSION
        if semantic_changed:
            child["components"] = [expected_component]
            child["matched_at"] = None
            child["first_match_snapshot"] = None
            child["match_snapshot"] = {}
            child["target_units"] = None
            child["component_achieved_units"] = {}
            child["status"] = "watching" if leg_executable else "unsupported"
            child["status_reason"] = (
                f"parser_v{PARSER_VERSION}_repaired_awaiting_market_match"
                if leg_executable
                else leg.get("unsupported_reason") or "tracked_non_executable_pick_type"
            )
            child["manual_fallback_recommended"] = not leg_executable
            child["manual_fallback_reason"] = _unsupported_leg_manual_reason(
                leg.get("unsupported_reason")
            ) if not leg_executable else ""
            child["price_strategy"] = (
                "trusted_straight_current_value" if leg_executable else "manual_exact_market_only"
            )
            child["warnings"] = (
                ["Parlay leg is watched as a trusted straight pick using the current standalone price; the parent parlay odds are not applied to the leg."]
                if leg_executable
                else [_unsupported_leg_warning(leg.get("unsupported_reason"))]
            )
            child["updated_at"] = _now()
            child.setdefault("history", []).append({
                "at": child["updated_at"],
                "status": child["status"],
                "reason": f"parser_v{PARSER_VERSION}_parlay_leg_metadata_repair",
            })
        if semantic_changed or child_version_changed:
            changed = True

    # Normalize active straight-ticket fingerprints after JSON round-trips.
    # ``7`` and ``7.0`` represent the same betting line but historically
    # produced different hashes, which could hide an otherwise exact duplicate.
    for row in store.get("tickets") or []:
        if row.get("status") not in active_statuses or row.get("pick_type") != "straight":
            continue
        canonical_fingerprint = _fingerprint(row, row.get("target_event_date"))
        if row.get("fingerprint") != canonical_fingerprint:
            row["fingerprint"] = canonical_fingerprint
            changed = True

    # A corrected contextual reparse can converge on a newer, already-correct
    # ticket.  Keep exactly one active watcher for an identical fingerprint so
    # stale parser copies cannot inflate queue work or coverage.  Never retire
    # a row that has any recorded fill.
    duplicate_groups = {}
    for row in store.get("tickets") or []:
        fingerprint = row.get("fingerprint")
        if fingerprint and row.get("status") in active_statuses:
            duplicate_groups.setdefault(fingerprint, []).append(row)
    for rows in duplicate_groups.values():
        if len(rows) < 2:
            continue
        survivor = max(
            rows,
            key=lambda row: (
                recorded_units(row),
                str(row.get("created_at") or row.get("approved_at") or ""),
                str(row.get("ticket_id") or ""),
            ),
        )
        for row in rows:
            if row is survivor or recorded_units(row) > 0:
                continue
            row["status"] = "expired"
            row["status_reason"] = f"parser_v{PARSER_VERSION}_superseded_by_equivalent_ticket"
            row["superseded_by_ticket_id"] = survivor.get("ticket_id")
            row["coverage_excluded"] = True
            row["updated_at"] = _now()
            row.setdefault("history", []).append({
                "at": row["updated_at"],
                "status": "expired",
                "reason": row["status_reason"],
            })
            changed = True
    return changed


def _signal_scan_wake():
    try:
        WAKE_FILE.write_text(_now(), encoding="utf-8")
    except OSError:
        pass


def _fingerprint(draft, day):
    def number(value):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return format(numeric, ".12g")

    fields = [
        draft.get("source"), draft.get("sport_key"), _plain(draft.get("selection")).lower(),
        draft.get("market_type"), number(draft.get("market_line")), number(draft.get("posted_odds")),
        number(draft.get("capper_units")), draft.get("pick_type"), day,
    ]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode("utf-8")).hexdigest()


def _expand_parlay_leg_watch_tickets(store):
    """Create idempotent executable child watches for regular parlay legs."""
    created = []
    local_day = _local_now().date().isoformat()
    parents = list(store.get("tickets") or [])
    for parent in parents:
        if parent.get("pick_type") != "parlay" or not parent.get("leg_watch_enabled", True):
            continue
        legs = list(parent.get("legs") or [])
        target_date = _valid_event_date(parent.get("target_event_date"))
        try:
            parent_units = float(parent.get("capper_units") or 0)
        except (TypeError, ValueError):
            parent_units = 0.0
        if not legs or parent_units <= 0 or not target_date or target_date < local_day:
            continue
        parent_id = parent.get("ticket_id")
        existing = {
            int(row.get("parlay_leg_index") or 0): row
            for row in store.get("tickets") or []
            if row.get("parent_ticket_id") == parent_id and row.get("pick_type") == "parlay_leg"
        }
        total_unit_cap = mapped_base_units(parent_units)
        # Preserve established whole-unit splits when they fit. Use half-unit
        # allocations only where the old one-unit floor exceeded the parent.
        allocation_step = 1.0 if total_unit_cap >= len(legs) else 0.5
        base_leg_ticks, remainder_ticks = divmod(int(total_unit_cap / allocation_step), len(legs))
        leg_ids = []
        for index, leg in enumerate(legs, start=1):
            if index in existing:
                leg_ids.append(existing[index].get("ticket_id"))
                continue
            component = _component(
                leg.get("selection"),
                leg.get("market_type"),
                leg.get("market_line"),
                1.0,
                leg.get("line_unit"),
            )
            leg_executable = bool(leg.get("executable", True))
            for key in (
                "method", "unsupported_reason", "first_inning_side", "btts_side",
                "double_chance_side", "total_side", "event_hint", "team_total_team",
            ):
                if leg.get(key):
                    component[key] = leg[key]
            component["executable"] = leg_executable
            ticket_id = uuid.uuid4().hex
            fingerprint = hashlib.sha256(
                f"{parent_id}|parlay_leg|{index}|{target_date}".encode("utf-8")
            ).hexdigest()
            now = _now()
            leg_unit_cap = (base_leg_ticks + (1 if index <= remainder_ticks else 0)) * allocation_step
            leg_executable = leg_executable and leg_unit_cap >= 0.5
            child = {
                "ticket_id": ticket_id,
                "fingerprint": fingerprint,
                "source": parent.get("source") or SOURCE_NAME,
                "sport_key": leg.get("sport_key") or parent.get("sport_key"),
                "sport_label": leg.get("sport_label") or parent.get("sport_label"),
                "selection": _plain(leg.get("selection")),
                "market_type": leg.get("market_type"),
                "market_line": leg.get("market_line"),
                "line_unit": leg.get("line_unit"),
                "method": leg.get("method"),
                "prop_selection": leg.get("prop_selection"),
                "first_inning_side": leg.get("first_inning_side"),
                "btts_side": leg.get("btts_side"),
                "double_chance_side": leg.get("double_chance_side"),
                "total_side": leg.get("total_side"),
                "event_hint": leg.get("event_hint"),
                "team_total_team": leg.get("team_total_team"),
                # The parent parlay price is not a valid benchmark for a leg.
                "posted_odds": None,
                "parent_posted_odds": parent.get("posted_odds"),
                "capper_units": parent.get("capper_units"),
                "pick_type": "parlay_leg",
                "price_strategy": "trusted_straight_current_value",
                "legs": [],
                "components": [component],
                "executable": leg_executable,
                "parser_version": parent.get("parser_version") or PARSER_VERSION,
                "parser_confidence": parent.get("parser_confidence"),
                "warnings": (
                    ["Parlay leg is watched as a trusted straight pick using the current standalone price; the parent parlay odds are not applied to the leg."]
                    if leg_executable
                    else [_unsupported_leg_warning(leg.get("unsupported_reason"))]
                ),
                "raw_text": parent.get("raw_text"),
                "import_raw_text": parent.get("import_raw_text"),
                "target_event_date": target_date,
                "event_date_source": parent.get("event_date_source"),
                "event_day_hint": parent.get("event_day_hint"),
                "parent_ticket_id": parent_id,
                "parlay_leg_index": index,
                "parlay_leg_count": len(legs),
                "parlay_parent_total_unit_cap": total_unit_cap,
                "parlay_leg_max_units": leg_unit_cap,
                "created_at": now,
                "updated_at": now,
                "approved_at": parent.get("approved_at") or now,
                "approved_local_at": parent.get("approved_local_at"),
                "approved_local_date": parent.get("approved_local_date"),
                "status": "watching" if leg_executable else "unsupported",
                "status_reason": (
                    "awaiting_independently_qualified_parlay_leg_price"
                    if leg_executable
                    else "parent_budget_unallocated" if leg_unit_cap < 0.5 else leg.get("unsupported_reason") or "tracked_non_executable_pick_type"
                ),
                "unsupported_reason": leg.get("unsupported_reason"),
                "manual_fallback_recommended": not leg_executable,
                "manual_fallback_reason": (
                    _unsupported_leg_manual_reason(leg.get("unsupported_reason"))
                    if not leg_executable else ""
                ),
                "matched_at": None,
                "placed_at": None,
                "target_units": None,
                "placed_units": 0,
                "fills": [],
                "match_snapshot": {},
                "history": [{"at": now, "status": "approved", "reason": "generated_from_approved_parlay_leg"}],
            }
            store["tickets"].append(child)
            created.append(child)
            leg_ids.append(ticket_id)
        parent["leg_ticket_ids"] = [value for value in leg_ids if value]
        parent["status"] = "unsupported"
        parent["status_reason"] = "combined_parlay_tracked_individual_legs_watching"
        parent["updated_at"] = _now()
    return created


def _migrate_parlay_leg_straight_policy(store):
    """Bring previously approved parlay-leg watches onto the current policy."""
    changed = False
    warning = "Parlay leg is watched as a trusted straight pick using the current standalone price; the parent parlay odds are not applied to the leg."
    for row in store.get("tickets") or []:
        if row.get("pick_type") != "parlay_leg":
            continue
        if not row.get("executable", True):
            row_changed = False
            if row.get("price_strategy") != "manual_exact_market_only":
                row["price_strategy"] = "manual_exact_market_only"
                row_changed = True
            unsupported_warning = _unsupported_leg_warning(row.get("unsupported_reason"))
            if row.get("warnings") != [unsupported_warning]:
                row["warnings"] = [unsupported_warning]
                row_changed = True
            manual_reason = _unsupported_leg_manual_reason(row.get("unsupported_reason"))
            if row.get("manual_fallback_reason") != manual_reason:
                row["manual_fallback_reason"] = manual_reason
                row_changed = True
            if row.get("status") not in {"placed", "settled", "cancelled", "expired"}:
                reason = row.get("unsupported_reason") or COMBAT_PROP_UNSUPPORTED_REASON
                if row.get("status") != "unsupported" or row.get("status_reason") != reason:
                    row["status"] = "unsupported"
                    row["status_reason"] = reason
                    row_changed = True
            if row_changed:
                row["updated_at"] = _now()
                changed = True
            continue
        row_changed = False
        if row.get("price_strategy") != "trusted_straight_current_value":
            row["price_strategy"] = "trusted_straight_current_value"
            row_changed = True
        if row.get("warnings") != [warning]:
            row["warnings"] = [warning]
            row_changed = True
        if row.get("status") == "watching" and row.get("status_reason") == "awaiting_independently_qualified_parlay_leg_price":
            row["status_reason"] = "awaiting_trusted_straight_price"
            row_changed = True
        if row_changed:
            row["updated_at"] = _now()
            changed = True
    return changed


def approve_drafts(drafts):
    now = _now()
    local_now = _local_now()
    local_day = local_now.date().isoformat()
    approved, duplicates, rejected = [], [], []
    with _store_lock():
        store = _read_store()
        _migrate_parser_metadata(store)
        known = {row.get("fingerprint") for row in store["tickets"]}
        for incoming in drafts or []:
            draft = dict(incoming or {})
            if not draft.get("selection") or not draft.get("market_type") or draft.get("posted_odds") is None:
                rejected.append({"draft_id": draft.get("draft_id"), "reason": "missing_required_pick_fields"})
                continue
            if draft.get("executable", True) and float(draft.get("capper_units") or 0) <= 0:
                rejected.append({"draft_id": draft.get("draft_id"), "reason": "missing_capper_units"})
                continue
            target_event_date = _valid_event_date(draft.get("target_event_date") or local_day)
            if not target_event_date:
                rejected.append({"draft_id": draft.get("draft_id"), "reason": "invalid_target_event_date"})
                continue
            if draft.get("executable", True) and target_event_date < local_day:
                rejected.append({"draft_id": draft.get("draft_id"), "reason": "target_event_date_is_in_the_past"})
                continue
            day_hint = str(draft.get("event_day_hint") or "").strip()
            if day_hint and date.fromisoformat(target_event_date).strftime("%A").lower() != day_hint.lower():
                rejected.append({"draft_id": draft.get("draft_id"), "reason": "event_day_and_date_disagree"})
                continue
            draft["target_event_date"] = target_event_date
            draft["event_date_source"] = draft.get("event_date_source") or "approval_date_default"
            fingerprint = _fingerprint(draft, target_event_date)
            if fingerprint in known:
                duplicates.append({"draft_id": draft.get("draft_id"), "fingerprint": fingerprint})
                continue
            ticket = dict(draft)
            ticket.update({
                "ticket_id": uuid.uuid4().hex,
                "fingerprint": fingerprint,
                "created_at": now,
                "updated_at": now,
                "approved_at": now,
                "approved_local_at": local_now.isoformat(timespec="seconds"),
                "approved_local_date": local_day,
                "status": "watching" if ticket.get("executable", True) else "unsupported",
                "status_reason": (
                    "awaiting_market_match"
                    if ticket.get("executable", True)
                    else ticket.get("unsupported_reason") or "tracked_non_executable_pick_type"
                ),
                "matched_at": None,
                "placed_at": None,
                "target_units": None,
                "placed_units": 0,
                "fills": [],
                "match_snapshot": {},
                "history": [{"at": now, "status": "approved", "reason": "user_approved_import"}],
            })
            ticket.pop("draft_id", None)
            store["tickets"].append(ticket)
            known.add(fingerprint)
            approved.append(ticket)
        # The dashboard may have remained alive across a parser deployment and
        # submitted an older preview. Normalize it before child expansion so
        # newly approved legs inherit current exact market semantics.
        _migrate_parser_metadata(store)
        approved.extend(_expand_parlay_leg_watch_tickets(store))
        _migrate_parlay_leg_straight_policy(store)
        _write_store(store)
    if approved:
        _signal_scan_wake()
    return {"approved": approved, "duplicates": duplicates, "rejected": rejected}


def list_tickets(limit=100, include_terminal=True):
    expanded = []
    with _store_lock():
        store = _read_store()
        parser_migrated = _migrate_parser_metadata(store)
        expanded = _expand_parlay_leg_watch_tickets(store)
        migrated = _migrate_parlay_leg_straight_policy(store)
        if expanded or migrated or parser_migrated:
            _write_store(store)
        tickets = list(store["tickets"])
    if expanded:
        _signal_scan_wake()
    if not include_terminal:
        tickets = [row for row in tickets if row.get("status") not in {"placed", "settled", "cancelled", "expired", "unsupported", "manual_attention"}]
    tickets = sorted(tickets, key=lambda row: row.get("created_at") or "", reverse=True)
    return tickets if limit is None else tickets[: int(limit)]


def update_ticket(ticket_id, updates, history_reason=None):
    with _store_lock():
        store = _read_store()
        found = None
        for row in store["tickets"]:
            if row.get("ticket_id") != ticket_id:
                continue
            found = row
            old_status = row.get("status")
            old_status_reason = row.get("status_reason")
            generic_reasons = {"watching", "ready", "queued", "partial_fill_watching_remaining"}
            if old_status_reason and old_status_reason not in generic_reasons and old_status not in {"expired", "settled", "cancelled", "placed"}:
                row["last_actionable_status_reason"] = old_status_reason
            for key, value in (updates or {}).items():
                if key not in {"ticket_id", "fingerprint", "created_at", "approved_at"}:
                    row[key] = value
            row["updated_at"] = _now()
            new_status = row.get("status")
            new_status_reason = row.get("status_reason")
            if new_status_reason and new_status_reason not in generic_reasons and new_status not in {"expired", "settled", "cancelled", "placed"}:
                row["last_actionable_status_reason"] = new_status_reason
            if history_reason or new_status != old_status or new_status_reason != old_status_reason:
                row.setdefault("history", []).append({
                    "at": row["updated_at"],
                    "status": new_status,
                    "reason": history_reason or new_status_reason or "status_changed",
                })
            break
        if found is None:
            raise KeyError("capper_ticket_not_found")
        _write_store(store)
        return found


def cancel_ticket(ticket_id):
    ticket = update_ticket(ticket_id, {"status": "cancelled", "status_reason": "cancelled_by_user"}, "cancelled_by_user")
    if ticket.get("pick_type") == "parlay":
        with _store_lock():
            store = _read_store()
            changed = False
            for row in store["tickets"]:
                if row.get("parent_ticket_id") != ticket_id or row.get("status") in {"placed", "settled", "cancelled", "expired"}:
                    continue
                row["status"] = "cancelled"
                row["status_reason"] = "parent_parlay_cancelled_by_user"
                row["updated_at"] = _now()
                row.setdefault("history", []).append({
                    "at": row["updated_at"],
                    "status": "cancelled",
                    "reason": "parent_parlay_cancelled_by_user",
                })
                changed = True
            if changed:
                _write_store(store)
    _signal_scan_wake()
    return ticket


def edit_ticket(ticket_id, edits):
    with _store_lock():
        store = _read_store()
        found = None
        for row in store["tickets"]:
            if row.get("ticket_id") != ticket_id:
                continue
            if row.get("status") in {"placed", "settled", "cancelled", "expired", "manual_attention"}:
                raise ValueError("capper_ticket_no_longer_editable")
            selection = _plain((edits or {}).get("selection", row.get("selection")))
            try:
                posted_odds = int(float((edits or {}).get("posted_odds", row.get("posted_odds"))))
                capper_units = float((edits or {}).get("capper_units", row.get("capper_units"))) if row.get("executable", True) else row.get("capper_units")
            except (TypeError, ValueError):
                raise ValueError("invalid_capper_ticket_numbers")
            target_event_date = _valid_event_date((edits or {}).get("target_event_date", row.get("target_event_date")))
            if not target_event_date:
                raise ValueError("invalid_target_event_date")
            if row.get("executable", True) and target_event_date < _local_now().date().isoformat():
                raise ValueError("target_event_date_is_in_the_past")
            day_hint = str(row.get("event_day_hint") or "").strip()
            if day_hint and date.fromisoformat(target_event_date).strftime("%A").lower() != day_hint.lower():
                raise ValueError("event_day_and_date_disagree")
            if not selection or posted_odds == 0 or (row.get("executable", True) and capper_units <= 0):
                raise ValueError("invalid_capper_ticket_edit")
            row["selection"] = selection
            requested_sport = str((edits or {}).get("sport_key") or row.get("sport_key") or "").strip()
            if requested_sport:
                if requested_sport not in set(SPORT_HEADERS.values()):
                    raise ValueError("invalid_capper_ticket_sport")
                row["sport_key"] = requested_sport
                row["sport_label"] = _plain((edits or {}).get("sport_label") or row.get("sport_label") or requested_sport)
            row["posted_odds"] = posted_odds
            row["capper_units"] = capper_units
            row["target_event_date"] = target_event_date
            row["event_date_source"] = "user_edited"
            for component in row.get("components") or []:
                component["selection"] = selection
            row["status"] = "watching" if row.get("executable", True) else "unsupported"
            row["status_reason"] = "edited_by_user_awaiting_rematch" if row.get("executable", True) else "tracked_non_executable_pick_type"
            row["match_snapshot"] = {}
            row["schedule_snapshot"] = {}
            row["updated_at"] = _now()
            row["fingerprint"] = _fingerprint(row, target_event_date)
            if any(other is not row and other.get("fingerprint") == row["fingerprint"] for other in store["tickets"]):
                raise ValueError("duplicate_capper_ticket")
            row.setdefault("history", []).append({"at": row["updated_at"], "status": row["status"], "reason": "edited_by_user"})
            found = row
            break
        if found is None:
            raise KeyError("capper_ticket_not_found")
        _write_store(store)
        _signal_scan_wake()
        return found


def ticket_summary(limit=100):
    all_tickets = list_tickets(limit=None)
    tickets = all_tickets if limit is None else all_tickets[: int(limit)]
    counts = {}
    for ticket in tickets:
        counts[ticket.get("status", "unknown")] = counts.get(ticket.get("status", "unknown"), 0) + 1
    # The dashboard's display limit must not truncate its 7/30-day denominator.
    coverage = coverage_summary(all_tickets)
    return {"source": SOURCE_NAME, "counts": counts, "coverage": coverage, "tickets": tickets, "store_file": str(STORE_FILE)}


def _coverage_time(row):
    for key in ("approved_at", "created_at", "placed_at", "settled_at"):
        value = row.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        return parse_timestamp(parsed)
    return None


def _coverage_failure_bucket(row):
    text = " ".join((
        str(row.get("status") or ""),
        str(row.get("status_reason") or ""),
        " ".join(str(value) for value in (row.get("warnings") or [])),
    )).lower()
    if any(token in text for token in ("identity", "ambiguous", "mismatch", "wrong_event")):
        return "identity"
    if any(token in text for token in ("price", "odds_range", "posted_odds")):
        return "price"
    if any(token in text for token in ("book", "odds_api", "provider", "score_context", "data")):
        return "data"
    if any(token in text for token in ("no_approved", "unavailable_on_kalshi", "awaiting_exact", "market_match", "no_market")):
        return "market_availability"
    if any(token in text for token in ("edge", "confidence", "strategy", "risk", "quality")):
        return "strategy_quality"
    if any(token in text for token in ("liquidity", "depth", "exposure", "reconciliation", "order")):
        return "execution_safeguard"
    if row.get("status") == "expired":
        return "expired_unmatched"
    return "pending_or_other"


def _ticket_was_actionable(row):
    if row.get("status") in {"placed", "settled", "ready", "waiting_price", "blocked"}:
        return True
    snapshot = row.get("match_snapshot") or {}
    return bool(
        row.get("placed_at")
        or row.get("matched_at")
        or row.get("first_match_snapshot")
        or row.get("fills")
        or int(snapshot.get("matched_components") or 0) > 0
    )


def coverage_summary(tickets, *, target_pct=None, now=None):
    """Return lifetime compatibility metrics plus rolling actionable coverage."""
    target_pct = CAPPER_COVERAGE_TARGET_PCT if target_pct is None else float(target_pct)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    executable = [
        row for row in tickets
        if row.get("executable", True)
        and row.get("pick_type") not in {"parlay", "ladder"}
        and row.get("status") != "cancelled"
        and not row.get("coverage_excluded")
    ]
    placed = [row for row in executable if row.get("status") in {"placed", "settled"}]
    lifetime_rate = (100.0 * len(placed) / len(executable)) if executable else 0.0

    def cohort(days=None):
        cutoff = now - timedelta(days=days) if days else None
        selected = [
            row for row in executable
            if cutoff is None or ((_coverage_time(row) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff)
        ]
        actionable = [row for row in selected if _ticket_was_actionable(row)]
        placed_rows = [row for row in actionable if row.get("status") in {"placed", "settled"}]
        rate = 100.0 * len(placed_rows) / len(actionable) if actionable else 0.0
        return {
            "days": days,
            "eligible_tickets": len(selected),
            "actionable_tickets": len(actionable),
            "non_actionable_or_pending": len(selected) - len(actionable),
            "placed_or_settled": len(placed_rows),
            "placement_rate_pct": round(rate, 2),
            "target_met": bool(actionable and rate + 1e-9 >= target_pct),
            "failure_funnel": dict(Counter(
                _coverage_failure_bucket(row)
                for row in selected
                if row.get("status") not in {"placed", "settled"}
            )),
            "by_pick_type": dict(Counter(str(row.get("pick_type") or "unknown") for row in selected)),
            "pregame_fills": sum(
                not any(bool((fill or {}).get("game_started")) for fill in row.get("fills") or [])
                for row in placed_rows
            ),
            "live_recovery_fills": sum(
                any(bool((fill or {}).get("game_started")) for fill in row.get("fills") or [])
                for row in placed_rows
            ),
        }

    rolling = {f"{days}d": cohort(days) for days in (7, 14, 30)}
    primary = rolling["14d"]
    return {
        "target_pct": target_pct,
        "eligible_tickets": len(executable),
        "placed_or_settled": len(placed),
        "placement_rate_pct": round(lifetime_rate, 2),
        "gap_to_target_pp": round(max(0.0, target_pct - lifetime_rate), 2),
        "target_met": lifetime_rate + 1e-9 >= target_pct,
        "primary_window": "14d_actionable",
        "primary_eligible_tickets": primary["actionable_tickets"],
        "primary_placement_rate_pct": primary["placement_rate_pct"],
        "primary_gap_to_target_pp": round(max(0.0, target_pct - primary["placement_rate_pct"]), 2),
        "primary_target_met": primary["target_met"],
        "lifetime": cohort(),
        "rolling": rolling,
        "legacy_rate_basis": "status_based_not_fill_evidence",
        "audit_funnel_shadow": capper_funnel(tickets, now=now),
    }
