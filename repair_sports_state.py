"""Rebuild sports state after JSON corruption using append-only events and Kalshi.

This tool is intentionally conservative: settled analytics come from durable
events, while currently open positions and cash come from Kalshi. Disposable
caches are reset and repopulate naturally on the next healthy scan.
"""

import argparse
import gzip
import json
from datetime import datetime
from pathlib import Path

import sports_paper_bettor as sports
from kalshi_common import KALSHI_BASE_URL, get_json, write_json


EVENTS_FILE = Path("sports_paper_events.jsonl")
PORTFOLIO_FILE = Path("sports_paper_portfolio.json")
TICKETS_FILE = Path("sports_capper_tickets.json")
UNIT_BANKROLL_BASE = 3616.72
UNIT_SIZE_PCT = 2.0
UNIT_SIZE = 72.33

SETTLEMENT_TYPES = {
    "sports_bet_settled",
    "sports_bet_settled_score_cache",
    "sports_bet_early_dead_loss_closed",
    "sports_user_cashout_detected",
}
DISPOSABLE_STATE_DEFAULTS = {
    "sports_favorite_watchlist.json": {},
    "sports_game_odds_cache.json": {},
    "sports_line_history.json": {},
    "sports_odds_cache.json": {},
    "sports_schedule_cache.json": {},
    "sports_watchlist.json": {},
}


def event_rows():
    archive_dir = Path("archives/storage_retention/analytics")
    sources = sorted(archive_dir.glob(f"{EVENTS_FILE.name}.*")) if archive_dir.exists() else []
    sources.append(EVENTS_FILE)
    for source in sources:
        if not source.exists():
            continue
        opener = gzip.open if source.suffix == ".gz" else open
        with opener(source, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row


def ticker_of(row):
    return str(row.get("kalshi_ticker") or row.get("ticker") or "")


def history_key(row):
    if row.get("id"):
        return f"id:{row['id']}"
    return "|".join((
        ticker_of(row),
        str(row.get("placed_at") or ""),
        str(row.get("strategy_owner") or row.get("source") or ""),
    ))


def clean_event(row):
    cleaned = dict(row)
    cleaned.pop("type", None)
    cleaned.pop("ts", None)
    return cleaned


def infer_market_type(ticker):
    ticker = ticker.upper()
    if "SPREAD" in ticker:
        return "spread"
    if "TOTAL" in ticker:
        return "total"
    return "moneyline"


def infer_sport(ticker):
    value = ticker.upper()
    mappings = (
        ("KXMLB", "baseball_mlb", "MLB"),
        ("KXWNBA", "basketball_wnba", "WNBA"),
        ("KXNBA", "basketball_nba", "NBA"),
        ("KXNCAAF", "americanfootball_ncaaf", "NCAAF"),
        ("KXNCAAB", "basketball_ncaab", "NCAAB"),
        ("KXNFL", "americanfootball_nfl", "NFL"),
        ("KXNHL", "icehockey_nhl", "NHL"),
        ("KXATP", "tennis", "Tennis"),
        ("KXWTA", "tennis", "Tennis"),
        ("KXUFC", "mma", "MMA/UFC"),
    )
    return next(((key, label) for prefix, key, label in mappings if value.startswith(prefix)), ("sports", "Sports"))


def is_sports_ticker(ticker):
    key, _label = infer_sport(ticker)
    return key != "sports"


def market_details(ticker):
    try:
        payload = get_json(f"{KALSHI_BASE_URL}/markets/{ticker}")
        return payload.get("market") or payload
    except Exception:
        return {}


def rebuild_history(rows):
    history = {}
    placements_by_ticker = {}
    invalid_closes = []
    missed_history = {}
    capper_events = []
    for row in rows:
        event_type = row.get("type")
        ticker = ticker_of(row)
        if event_type in {"live_bet_placed", "paper_bot_pick_placed", "manual_live_test_order_placed", "sports_user_live_bet_imported"}:
            placements_by_ticker.setdefault(ticker, []).append(clean_event(row))
        if event_type in SETTLEMENT_TYPES:
            settled = clean_event(row)
            settled["status"] = "settled"
            history[history_key(settled)] = settled
        elif event_type == "invalid_market_position_closed":
            invalid_closes.append(row)
        elif event_type == "sports_missed_fill_settled":
            settled = clean_event(row)
            missed_history[history_key(settled)] = settled
        elif event_type == "trusted_capper_pick_placed":
            capper_events.append(row)

    # The invalid-MMA liquidation events are compact; merge them with their
    # original placements so the prior analytics remain complete.
    for close in invalid_closes:
        ticker = ticker_of(close)
        placement = dict((placements_by_ticker.get(ticker) or [{}])[-1])
        placement.update({
            "kalshi_ticker": ticker,
            "ticker": ticker,
            "status": "settled",
            "result": "WIN" if float(close.get("profit") or 0) > 0 else "LOSS",
            "profit": round(float(close.get("profit") or 0), 2),
            "fee": float(close.get("fee") or 0),
            "settled_at": close.get("ts"),
            "settlement_reason": close.get("reason"),
            "exit_price": close.get("exit"),
        })
        history[history_key(placement)] = placement

    ordered = sorted(history.values(), key=lambda row: str(row.get("settled_at") or row.get("settlement_ts") or ""))
    missed = sorted(missed_history.values(), key=lambda row: str(row.get("settled_at") or ""))
    return ordered, missed, capper_events


def rebuild_open_positions(snapshot, capper_events):
    latest_capper = {ticker_of(row): row for row in capper_events}
    open_bets = []
    for remote in snapshot.get("positions_sample") or []:
        ticker = ticker_of(remote)
        try:
            position = float(remote.get("position_fp") or 0)
        except (TypeError, ValueError):
            position = 0.0
        if not ticker or not is_sports_ticker(ticker) or position == 0:
            continue
        contracts = abs(position)
        stake = round(float(remote.get("market_exposure_dollars") or 0), 2)
        entry_price = round(stake / contracts * 100.0, 3) if contracts else 0.0
        order_side = "yes" if position > 0 else "no"
        details = market_details(ticker)
        capper = latest_capper.get(ticker)
        sport_key, sport_title = infer_sport(ticker)
        selection = (
            (capper or {}).get("selection")
            or details.get("yes_sub_title")
            or details.get("title")
            or ticker
        )
        placed_at = (capper or {}).get("ts") or remote.get("last_updated_ts") or datetime.now().isoformat(timespec="seconds")
        bet = {
            "id": f"capper-recovered-{capper['ticket_id']}" if capper else f"user-bet-recovered-{ticker}",
            "ticker": ticker,
            "kalshi_ticker": ticker,
            "selected_team": selection,
            "market_title": details.get("title") or selection,
            "market_type": infer_market_type(ticker),
            "sport_key": sport_key,
            "sport_title": sport_title,
            "side": "Yes" if order_side == "yes" else "No",
            "order_side": order_side,
            "kalshi_order_side": order_side,
            "entry_price": entry_price,
            "stake": stake,
            "contracts": contracts,
            "fee": round(float(remote.get("fees_paid_dollars") or 0), 4),
            "mode": "live",
            "status": "open",
            "result": None,
            "placed_at": placed_at,
            "remote_position_recovered": True,
            "remote_last_updated_ts": remote.get("last_updated_ts"),
        }
        if capper:
            bet.update({
                "source": "trusted_capper",
                "strategy_owner": "trusted_capper",
                "capper_source": capper.get("source") or "InfluencedBets",
                "trusted_capper_ticket_id": capper.get("ticket_id"),
                "trusted_capper_component_id": capper.get("component_id"),
                "capper_posted_odds": capper.get("posted_odds"),
                "capper_units": capper.get("capper_units"),
                "edge": capper.get("edge"),
                "unit_count": capper.get("placed_units"),
                "unit_size": UNIT_SIZE,
                "unit_size_pct": UNIT_SIZE_PCT,
                "unit_bankroll_base": UNIT_BANKROLL_BASE,
                "unit_target_units": capper.get("placed_units"),
            })
        else:
            bet.update({
                "source": "user_manual",
                "strategy_owner": "user_bet",
                "user_bet": True,
                "manual_bet": True,
            })
        open_bets.append(bet)
    return open_bets


def ticket_store(capper_events, open_bets, history):
    open_tickets = {row.get("trusted_capper_ticket_id") for row in open_bets if row.get("trusted_capper_ticket_id")}
    settled_by_ticket = {
        row.get("trusted_capper_ticket_id"): row
        for row in history
        if row.get("trusted_capper_ticket_id")
    }
    tickets = []
    for event in capper_events:
        ticket_id = event.get("ticket_id")
        settled = settled_by_ticket.get(ticket_id)
        status = "placed" if ticket_id in open_tickets else "settled" if settled else "placed"
        selection = event.get("selection") or event.get("ticker")
        market_type = infer_market_type(event.get("ticker") or "")
        sport_key, sport_label = infer_sport(event.get("ticker") or "")
        tickets.append({
            "ticket_id": ticket_id,
            "source": event.get("source") or "InfluencedBets",
            "sport_key": sport_key,
            "sport_label": sport_label,
            "selection": selection,
            "market_type": market_type,
            "market_line": None,
            "posted_odds": event.get("posted_odds"),
            "capper_units": event.get("capper_units"),
            "pick_type": "straight",
            "components": [{
                "component_id": event.get("component_id"),
                "selection": selection,
                "market_type": market_type,
                "market_line": None,
                "risk_fraction": 1.0,
            }],
            "legs": [],
            "executable": True,
            "status": status,
            "status_reason": "recovered_from_append_only_event_ledger",
            "placed_at": event.get("ts"),
            "placed_units": event.get("placed_units"),
            "target_units": event.get("placed_units"),
            "fills": [{
                "ticker": event.get("ticker"),
                "entry_price": event.get("entry_price"),
                "stake": event.get("stake"),
                "placed_units": event.get("placed_units"),
            }],
            "created_at": event.get("ts"),
            "updated_at": event.get("ts"),
            "settled_at": (settled or {}).get("settled_at"),
            "warnings": ["Recovered after restart corruption; unplaced queue tickets must be re-imported."],
        })
    return {
        "version": 2,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tickets": tickets,
        "recovery_warning": "The pre-crash unplaced trusted-capper queue could not be recovered; paste and approve those picks again.",
    }


def build_reconciliation(snapshot, open_bets):
    account_positions = snapshot.get("positions_sample") or []
    sports_tickers = {ticker_of(row) for row in open_bets}
    excluded = [ticker_of(row) for row in account_positions if ticker_of(row) not in sports_tickers]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ok": True,
        "reconciliation_scope": "sports_only",
        "account": snapshot,
        "remote_sports_tickers": sorted(sports_tickers),
        "remote_open_count": len(sports_tickers),
        "remote_account_open_count": len(account_positions),
        "remote_excluded_non_sports_count": len(excluded),
        "remote_excluded_non_sports_tickers": sorted(excluded),
        "unmatched_local_tickers": [],
        "unmatched_remote_tickers": [],
        "position_mismatches": [],
        "repaired_from_live_account": True,
    }


def repair(apply=False):
    rows = list(event_rows())
    history, missed_history, capper_events = rebuild_history(rows)
    snapshot = sports.fetch_live_account_snapshot()
    if not snapshot.get("ok") or snapshot.get("cash_balance") is None:
        raise RuntimeError(f"Kalshi account snapshot failed: {snapshot}")
    open_bets = rebuild_open_positions(snapshot, capper_events)
    now = datetime.now().astimezone()
    portfolio = {
        "mode": "live",
        "starting_balance": 300.0,
        "balance": round(float(snapshot.get("cash_balance") or 0), 2),
        "bets": open_bets,
        "history": history,
        "missed_fills": [],
        "missed_fill_history": missed_history,
        "sports_unit_daily_snapshot": {
            "session_key": f"{now.date().isoformat()}|daily_open",
            "local_date": now.date().isoformat(),
            "reset_at": None,
            "bankroll_base": UNIT_BANKROLL_BASE,
            "captured_at": f"{now.date().isoformat()}T00:00:00{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]}",
            "unit_size_pct_at_capture": UNIT_SIZE_PCT,
            "unit_size_at_capture": UNIT_SIZE,
        },
        "repair_metadata": {
            "repaired_at": now.isoformat(timespec="seconds"),
            "source": "sports_paper_events.jsonl_plus_live_kalshi",
            "settled_rows": len(history),
            "open_rows": len(open_bets),
            "event_rows": len(rows),
        },
    }
    result = {
        "apply": apply,
        "cash_balance": portfolio["balance"],
        "open_bets": [{"ticker": ticker_of(row), "stake": row.get("stake"), "owner": row.get("strategy_owner")} for row in open_bets],
        "settled_history": len(history),
        "missed_fill_history": len(missed_history),
        "unit_snapshot": portfolio["sports_unit_daily_snapshot"],
        "capper_tickets_recovered": len(capper_events),
    }
    if apply:
        write_json(PORTFOLIO_FILE, portfolio)
        write_json(TICKETS_FILE, ticket_store(capper_events, open_bets, history))
        write_json("sports_live_reconciliation.json", build_reconciliation(snapshot, open_bets))
        for path, default in DISPOSABLE_STATE_DEFAULTS.items():
            write_json(path, default)
        # Provider credit accounting will be re-established from authoritative
        # response headers; retain an explicit recovery marker until then.
        write_json("sports_odds_budget.json", {"entries": [], "recovered_after_restart": True})
        write_json("sports_odds_key_state.json", {"keys": {}, "recovered_after_restart": True})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write the reconstructed state files")
    arguments = parser.parse_args()
    print(json.dumps(repair(apply=arguments.apply), indent=2, sort_keys=True))
