"""Read-only daily results for scanner and AIBetPicks, separately and combined.

This report produces no stake recommendations and does not change bot state.
The retained portfolio ledger supports histories trimmed into compressed archives.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from sports_modest_recovery import CHICAGO, LANES, entries, number, stamp


def totals(rows):
    values = [number(row.get("profit_units")) for row in rows]
    known = [value for value in values if value is not None]
    missing = len(values) - len(known)
    net = round(sum(known), 6)
    return {
        "net_units": None if missing else net,
        "known_net_units": net,
        "settled_count": len(values),
        "wins": sum(value > 0 for value in known),
        "losses": sum(value < 0 for value in known),
        "breakeven": sum(value == 0 for value in known),
        "missing_accounting_count": missing,
    }


def report(portfolio, now=None):
    now = now or datetime.now(CHICAGO)
    now = (now if now.tzinfo else now.replace(tzinfo=CHICAGO)).astimezone(CHICAGO)
    records = list(entries(portfolio, now).values())
    days = []
    for day in ((now - timedelta(days=1)).date(), now.date()):
        settled = [row for row in records if not row.get("open")
                   and stamp(row.get("settled_at"))
                   and stamp(row["settled_at"]).date() == day]
        days.append({
            "date": day.isoformat(),
            "lanes": {lane: totals([row for row in settled if row["lane"] == lane])
                      for lane in LANES},
            "combined": totals(settled),
        })
    return {"as_of": now.isoformat(timespec="seconds"),
            "timezone": "America/Chicago", "days": days}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("portfolio", type=Path, help="Portfolio JSON to read; never modified")
    args = parser.parse_args()
    portfolio = json.loads(args.portfolio.read_text(encoding="utf-8-sig"))
    print(json.dumps(report(portfolio), indent=2))


if __name__ == "__main__":
    main()
