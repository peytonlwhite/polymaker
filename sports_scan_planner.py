"""Bounded round-robin quote refreshes between complete sports discovery scans."""

from collections import defaultdict
from datetime import datetime, timezone
from sports_audit_support import CHICAGO, number, parse_timestamp


def make_plan(candidates, *, now=None, previous=None):
    now = parse_timestamp(now or datetime.now(timezone.utc))
    events = defaultdict(list)
    seen = set()
    for row in candidates or []:
        ticker, sport = row.get("kalshi_ticker"), row.get("sport_key")
        start = parse_timestamp(row.get("commence_time") or row.get("kalshi_market_start"))
        if not ticker or not sport or ticker in seen or row.get("game_completed") or not start:
            continue
        minutes = (start - now).total_seconds() / 60
        if minutes > 190 or minutes < -480:
            continue
        seen.add(ticker)
        events[str(row.get("event_id") or row.get("game_key") or ticker)].append({
            "ticker": ticker, "sport_key": sport, "start": start.isoformat(),
            "edge": number(row.get("edge")),
        })
    # Group provider requests, interleaving events within each provider so one
    # match's many alternate lines cannot monopolize its first batch.
    groups = sorted(events.values(), key=lambda group: (group[0]["start"], group[0]["ticker"]))
    for group in groups:
        group.sort(key=lambda row: -(row["edge"] if row["edge"] is not None else -999))
    sports = list(dict.fromkeys(group[0]["sport_key"] for group in groups))
    targets = [group[index] for sport in sports
               for index in range(max(map(len, groups), default=0))
               for group in groups if group[0]["sport_key"] == sport and index < len(group)]
    cursor = 0
    old = (previous or {}).get("targets") or []
    if old:
        old_cursor = int((previous or {}).get("cursor") or 0) % len(old)
        indices = {row["ticker"]: index for index, row in enumerate(targets)}
        for offset in range(len(old)):
            ticker = old[(old_cursor + offset) % len(old)].get("ticker")
            if ticker in indices:
                cursor = indices[ticker]
                break
    return {"generated_at": now.astimezone(CHICAGO).isoformat(), "targets": targets,
            "cursor": cursor, "target_count": len(targets)}


def focused_route(plan, *, now=None, max_tickers=16, max_sports=4):
    now = parse_timestamp(now or datetime.now(timezone.utc))
    targets = (plan or {}).get("targets") or []
    created = parse_timestamp((plan or {}).get("generated_at"))
    empty = {"enabled": False, "tickers": [], "sport_keys": [], "next_cursor": 0}
    if not targets or not created or not 0 <= (now - created).total_seconds() <= 15 * 60:
        return empty
    cursor = int((plan or {}).get("cursor") or 0) % len(targets)
    chosen, sports, consumed = [], [], 0
    for offset in range(len(targets)):
        row = targets[(cursor + offset) % len(targets)]
        start = parse_timestamp(row.get("start"))
        if not start or not -480 <= (start - now).total_seconds() / 60 <= 180:
            consumed += 1
            continue
        sport = row["sport_key"]
        if sport not in sports and len(sports) >= max_sports:
            break
        chosen.append(row)
        if sport not in sports:
            sports.append(sport)
        consumed += 1
        if len(chosen) >= max_tickers:
            break
    live = any(parse_timestamp(row["start"]) <= now for row in chosen)
    return {"enabled": bool(chosen), "tickers": [row["ticker"] for row in chosen],
            "sport_keys": sports, "next_cursor": (cursor + consumed) % len(targets),
            "target_interval_seconds": 40 if live else 60, "live": live,
            "universe_count": len(targets)}


def cadence_sleep(report, route, pacing_multiplier=1):
    """Start-to-start cadence, with a short yield when a scan exceeds its budget."""
    duration = max(0, number((report or {}).get("scan_duration_seconds")) or 0)
    interval = route["target_interval_seconds"] * max(1, pacing_multiplier)
    return max(5.0, interval - duration)
