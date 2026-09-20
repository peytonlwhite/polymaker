"""User-operated Kalshi cash allocation; importing or displaying never moves funds."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

PATH = "/portfolio/target_balance_allocation"
TARGET = {"allocations": [{"exchange_index": 2, "percent": 100}]}


def dollars(value):
    try:
        result = Decimal(str(value))
        return float(result) if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def full_crypto(data):
    rows = data.get("allocations") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return False
    seen = set()
    total = Decimal(0)
    for row in rows:
        index = row.get("exchange_index")
        percent = dollars(row.get("percent"))
        if isinstance(index, bool) or not isinstance(index, int) or index in seen or percent is None:
            return False
        seen.add(index)
        if index == 2 and percent != 100 or index != 2 and percent != 0:
            return False
        total += Decimal(str(percent))
    return 2 in seen and total == 100


def summary(reconciliation, record):
    account = reconciliation.get("account") or {}
    raw = account.get("balance_raw") or {}
    crypto_cash = None
    for row in raw.get("balance_breakdown") or []:
        if row.get("exchange_index") == 2:
            crypto_cash = dollars(row.get("balance"))
    cash = dollars(raw.get("balance_dollars"))
    verified = record.get("status") == "allocation_verified"
    return {"crypto_cash": crypto_cash, "account_cash": cash,
            "snapshot_at": reconciliation.get("generated_at"),
            "allocation_verified_at": record.get("verified_at"),
            "full_crypto_target_verified": verified,
            "status": record.get("status", "not_requested"),
            "message": record.get("message", "Allocate 100% of available cash to Crypto. Entry sizing remains 1% of total bankroll.")}


def apply_full_crypto(request, save, report, now=None):
    """Called only by a user's funding POST, with an authenticated API adapter.

    Set the exchange's allocation target, never fabricate spendable shard cash.
    Verification distinguishes target acceptance from funds arriving.
    """
    now = now or datetime.now(timezone.utc)
    try:
        generated = datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
        fresh = 0 <= (now - generated).total_seconds() <= 180
    except (KeyError, TypeError, ValueError):
        fresh = False
    eth = [r for r in report.get("top_candidates", [])
           if r.get("asset") == "ETH" and r.get("market_lane") == "crypto_15m"]
    if not fresh or not eth or any(r.get("exchange_index") != 2 for r in eth):
        raise ValueError("The ETH exchange destination needs a fresh market snapshot before funding.")
    before = request(PATH)
    if not isinstance(before, dict) or not isinstance(before.get("allocations"), list):
        raise ValueError("Kalshi's current balance allocation could not be verified.")
    record = {"requested_at": now.isoformat(), "target": TARGET,
              "previous_allocations": before["allocations"], "status": "requesting"}
    # Durable audit precedes the only mutation. GET/status paths never call it.
    save(record)
    if not full_crypto(before):
        try:
            request(PATH, method="POST", body=TARGET)
        except Exception:
            # The server may have applied a timed-out request; read before any
            # future user retry rather than issuing a second mutation here.
            record["status"] = "verification_needed"
    try:
        after = request(PATH)
        verified = full_crypto(after)
    except Exception:
        verified = False
    if verified:
        record.update(status="allocation_verified", verified_at=datetime.now(timezone.utc).isoformat(),
                      message="Kalshi confirms a 100% Crypto cash target. Balance movement is pending until the Crypto cash snapshot updates.")
    else:
        record.update(status="verification_needed",
                      message="The 100% Crypto allocation could not be confirmed. Check Kalshi before retrying; no transfer is assumed complete.")
    save(record)
    return {"ok": verified, "funding": record, "error": None if verified else record["message"]}
