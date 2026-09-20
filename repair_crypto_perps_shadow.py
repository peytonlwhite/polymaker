"""Audited paper-only recovery; preview by default, --apply with scanner stopped.

Invalid exits remain verbatim in history and are explicitly unscored. Recovery
uses known entry fees/funding, never a guessed replacement exit or refunded fee.
The exact input is retained as a gzip archive before the atomic state write.
"""
import argparse
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import crypto_perps_shadow as perps
from runtime_guard import acquire_single_instance


def recovery_preview(portfolio):
    if portfolio.get("mode") != "paper_shadow_only":
        raise ValueError("Recovery is restricted to the isolated paper ledger")
    invalid = [row for row in portfolio.get("history", [])
               if perps.invalid_settlement_reason(row)]
    if not invalid:
        return None
    if any(perps.invalid_settlement_reason(row) != "invalid_exit_price" for row in invalid):
        raise ValueError("Recovery requires valid entries and known pre-exit economics")
    ids = sorted(row["id"] for row in invalid)
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate invalid position IDs")
    if (portfolio.get("invalid_settlement_repair") or {}).get("unscored_ids") == ids:
        return None
    scored = [row for row in portfolio.get("history", [])
              if not perps.invalid_settlement_reason(row)]
    amounts = [float(portfolio["starting_balance"])]
    amounts.extend(float(row["profit"]) for row in scored)
    for row in list(portfolio.get("positions", [])) + invalid:
        fee, funding = float(row["entry_fee"]), float(row["funding_pnl"])
        if fee < 0:
            raise ValueError("Invalid known entry fee")
        amounts.extend([-fee, funding])
    if not all(math.isfinite(value) for value in amounts):
        raise ValueError("Non-finite known cash economics")
    return {
        "version": 1,
        "unscored_ids": ids,
        "balance_before": portfolio["balance"],
        "balance_after": round(math.fsum(amounts), 6),
        "valid_settled_count": len(scored),
        "valid_realized_profit": round(math.fsum(float(row["profit"]) for row in scored), 6),
        "method": "starting_cash_plus_valid_profit_plus_open_and_unscored_funding_minus_entry_fees",
        "note": "Original history retained verbatim; invalid exit outcomes remain unknown, not zero-profit trades",
    }


def apply_recovery(path, archive_dir):
    """Caller must own the scanner PID guard before reading or writing state."""
    path, archive_dir = Path(path), Path(archive_dir)
    original = path.read_bytes()
    portfolio = json.loads(original.decode("utf-8-sig"))
    preview = recovery_preview(portfolio)
    if preview is None:
        return {"changed": False}
    now = perps.now_utc().astimezone(ZoneInfo("America/Chicago"))
    digest = hashlib.sha256(original).hexdigest()
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"perps_before_invalid_exit_repair_{now:%Y%m%d_%H%M%S}_{digest[:12]}.json.gz"
    with gzip.open(archive, "xb") as handle:
        handle.write(original)
    if path.read_bytes() != original:
        raise RuntimeError("Portfolio changed during recovery; refusing overwrite")
    repaired = deepcopy(portfolio)
    audit = {**preview, "repaired_at": now.isoformat(), "source_sha256": digest,
             "backup_path": str(archive.resolve())}
    repaired["balance"] = preview["balance_after"]
    repaired["invalid_settlement_repair"] = audit
    repaired.setdefault("repair_history", []).append(audit)
    perps.write_json(path, repaired)
    return {"changed": True, **audit}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        acquire_single_instance(Path(".crypto_bot.pid"), "perps paper recovery")
        result = apply_recovery(perps.PORTFOLIO_FILE, Path("archives/perps_repairs"))
    else:
        result = recovery_preview(json.loads(perps.PORTFOLIO_FILE.read_text(encoding="utf-8-sig")))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
