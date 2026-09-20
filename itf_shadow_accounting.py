"""Read-only unit accounting for the frozen ITF experiment's dashboard.

The worker's cost, contract count and payout remain authoritative. Derived unit
columns use the experiment's frozen dollar unit, never today's Sports unit.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sports_itf_shadow import Experiment, dashboard_summary as worker_summary


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite accounting value")
    return result


def position_accounting(row, unit_dollars):
    unit, cost, count = map(decimal, (unit_dollars, row["cost"], row["contracts"]))
    if unit <= 0 or cost <= 0 or count <= 0:
        raise ValueError("Invalid unit, cost or contract count")
    errors = []
    fills = row.get("fills") or []
    premium = sum((decimal(f["price"]) * decimal(f["contracts"]) for f in fills), Decimal(0))
    fees = sum((decimal(f["fee"]) for f in fills), Decimal(0))
    if (not fills or sum(decimal(f["contracts"]) for f in fills) != count
            or premium != decimal(row["premium"]) or fees != decimal(row["fee"])
            or premium + fees != cost):
        errors.append("fill_cost_mismatch")
    # Full-precision ratios are returned; the UI alone rounds their display.
    result = {"staked_units": float(cost / unit), "win_profit": float(count - cost),
              "win_profit_units": float((count - cost) / unit),
              "payout_units": None, "profit_units": None}
    if row["status"] == "settled":
        payout, profit, value = map(decimal, (row["payout"], row["profit"], row["settlement_value"]))
        if not 0 <= value <= 1 or (count * value).quantize(Decimal(".0001")) != payout:
            errors.append("settlement_payout_mismatch")
        if payout - cost != profit:
            errors.append("net_profit_mismatch")
        result.update(payout_units=float(payout / unit), profit_units=float(profit / unit))
    return result, errors


def dashboard_summary(root=None, now=None):
    root = Path(root or Path(__file__).resolve().parent)
    report = worker_summary(root, now)
    if report.get("status") in {"not_started", "unreadable"}:
        return report
    try:
        experiment = Experiment(root / "sports_itf_shadow_state.json")
        state = experiment.state
        if (state["started_at"] != report.get("started_at")
                or state["rules_hash"] != report.get("rules_hash")
                or state["unit_dollars"] != report.get("unit_dollars")):
            raise ValueError("Ledger and report refer to different experiment accounting")
        # Derive both aggregates and all rows from one atomic ledger snapshot.
        view = {**report, **experiment.summary(now), "generated_at": report.get("generated_at")}
        unit = decimal(state["unit_dollars"])
        fields = ("id", "strategy", "placed_at", "selected", "opponent", "tournament", "phase",
                  "ticker", "side", "entry_price", "contracts", "premium", "fee", "cost",
                  "actual_units", "target_units", "status", "result", "payout", "profit",
                  "settlement_value", "settled_at")
        rows, errors = [], []
        amounts = defaultdict(lambda: {"gains": Decimal(0), "losses": Decimal(0),
                                       "payout": Decimal(0), "cost": Decimal(0)})
        for source in state["positions"].values():
            row = {k: source.get(k) for k in fields}
            accounting, issues = position_accounting(source, unit)
            row.update(accounting)
            errors.extend({"id": source["id"], "reason": reason} for reason in issues)
            if source["status"] == "settled":
                totals = amounts[source["strategy"]]
                profit = decimal(source["profit"])
                totals["gains"] += max(Decimal(0), profit)
                totals["losses"] += min(Decimal(0), profit)
                totals["payout"] += decimal(source["payout"])
                totals["cost"] += decimal(source["cost"])
            rows.append(row)
        for strategy in view["strategies"]:
            totals = amounts[strategy["id"]]
            strategy.update({"gains_units": float(totals["gains"] / unit),
                             "losses_units": float(totals["losses"] / unit),
                             "settled_payout_units": float(totals["payout"] / unit),
                             "settled_staked_units": float(totals["cost"] / unit)})
            if abs(decimal(strategy["profit"]) - totals["payout"] + totals["cost"]) > Decimal(".0001"):
                errors.append({"id": strategy["id"], "reason": "strategy_profit_mismatch"})
        view["positions"] = sorted(rows, key=lambda r: (r["placed_at"], r["id"]), reverse=True)
        view["accounting"] = {"ok": not errors, "checked_positions": len(rows),
                              "settled_positions": sum(r["status"] == "settled" for r in rows),
                              "unit_dollars": float(unit), "data_at": state.get("last_scan_at"),
                              "errors": errors, "formula": "Net profit units = (settlement payout - cost including fees) / frozen unit"}
        return view
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
        return {**report, "accounting": {"ok": False, "errors": [{"reason": "accounting_verification_unavailable"}]}}
