"""Filled sports risk in units, separate from the strategy's requested tier."""

import math


def filled_units(row):
    """Use the entry's captured dollar unit; never today's bankroll unit."""
    try:
        size = float(row.get("unit_size") or (row.get("sports_units") or {}).get("unit_size") or 0)
        stake = float(row["stake"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(size) or not math.isfinite(stake) or size <= 0 or stake < 0:
        return None
    return stake / size


def record_filled_units(row):
    """Update accounting fields only, preserving requested/target sizing."""
    units = filled_units(row)
    if units is None:
        return False
    state = dict(row.get("sports_units") or {})
    state.setdefault("requested_units", state.get("additional_units"))
    state.update(placed_units=units, filled_stake=row["stake"])
    row.update(unit_count=units, unit_size=float(row.get("unit_size") or state["unit_size"]), sports_units=state)
    return True


def reserved_units(row):
    """Reserve at least actual risk and the requested tier for sizing guards."""
    state = row.get("sports_units") or row.get("sports_game_odds_units") or {}
    values = [filled_units(row) or 0]
    for value in (state.get("additional_units"), state.get("placed_units"), row.get("unit_count")):
        try:
            value = float(value)
            if math.isfinite(value):
                values.append(value)
        except (TypeError, ValueError):
            pass
    return max(values)


def reconcile_scanner_units(portfolio, corrected_at):
    """Repair retained scanner metadata; leave orders and financials intact."""
    changes = []
    for collection in ("bets", "history"):
        for row in portfolio.get(collection) or []:
            if row.get("source") != "edge_scanner" or row.get("strategy_owner") != "live_campaign":
                continue
            units = filled_units(row)
            state = row.get("sports_units") or {}
            if units is None or (row.get("unit_count") == units and state.get("placed_units") == units):
                continue
            correction = {
                "corrected_at": corrected_at,
                "basis": "filled_stake_divided_by_entry_unit_size",
                "previous_unit_count": row.get("unit_count"),
                "previous_placed_units": state.get("placed_units"),
                "actual_units": units,
            }
            row.setdefault("unit_accounting_correction", correction)
            record_filled_units(row)
            changes.append({"collection": collection, "kalshi_ticker": row.get("kalshi_ticker"),
                            "placed_at": row.get("placed_at"), **correction})
    return changes
