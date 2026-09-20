"""Opt-in cash routing for the existing Sports and patient ETH live workers.

No fixed allocation percentages. Import/status are read-only. Only the user's
enable command changes the exchange target; routing starts after worker reload.
Transfers are asynchronous and never retried after an ambiguous submission.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from threading import local
from urllib.parse import quote
from zoneinfo import ZoneInfo
import argparse
import json
import uuid

from kalshi_common import write_json
import shared_bankroll as bankroll

VERSION = "shared-cash-pool-v1"
CONTROL = Path("shared_cash_pool_control.json")
LEDGER = Path("shared_cash_pool_transfers.json")
ALLOCATION_PATH = "/portfolio/target_balance_allocation"
TRANSFER_PATH = "/portfolio/intra_exchange_instance_transfer"
TRANSFERS_PATH = "/portfolio/intra_exchange_instance_transfers"
EXCHANGES = {"sports": 0, "crypto": 2}
_session = local()


def timestamp():
    return datetime.now(ZoneInfo("America/Chicago")).isoformat(timespec="seconds")


def read_object(path, *, optional=False):
    if optional and not Path(path).exists():
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("shared_cash_state_invalid")
    return value


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("shared_cash_amount_invalid") from None
    if not result.is_finite() or result < 0:
        raise ValueError("shared_cash_amount_invalid")
    return result


def enabled():
    record = read_object(CONTROL, optional=True)
    if not record:
        return False
    if record.get("version") != VERSION or not isinstance(record.get("enabled"), bool):
        raise ValueError("shared_cash_control_invalid")
    return record["enabled"]


def balances(raw):
    rows = raw.get("balance_breakdown")
    if not isinstance(rows, list):
        raise ValueError("shared_cash_balances_unverified")
    result = {}
    for row in rows:
        index = row.get("exchange_index")
        if type(index) is not int or not 0 <= index <= 100 or index in result:
            raise ValueError("shared_cash_balances_invalid")
        result[index] = amount(row.get("balance"))
    if not set(EXCHANGES.values()).issubset(result):
        raise ValueError("shared_cash_balances_incomplete")
    if abs(sum(result.values()) - amount(raw.get("balance_dollars"))) > Decimal("0.0001"):
        raise ValueError("shared_cash_balance_total_mismatch")
    return result


def reserved_by_exchange(state):
    """Do not release an unresolved in-flight order merely because its TTL aged."""
    rows = state.get("reservations")
    if not isinstance(rows, list):
        raise ValueError("shared_cash_reservations_unverified")
    reserved = {index: Decimal(0) for index in EXCHANGES.values()}
    for row in rows:
        if row.get("status") != "active":
            continue
        index = (row.get("metadata") or {}).get("exchange_index")
        if type(index) is not int or index not in reserved:
            raise ValueError("shared_cash_reservation_destination_unverified")
        reserved[index] += amount(row.get("stake"))
    return reserved


def require_reservations():
    settings = bankroll.load_settings()
    if not all(bankroll.setting_bool(settings, key) for key in
               ("SHARED_BANKROLL_ENABLED", "SHARED_BANKROLL_RESERVATIONS_ENABLED")):
        raise ValueError("Shared bankroll and order reservations must remain enabled.")


def validate_ledger(ledger):
    if ledger.get("version") != VERSION or "pending" not in ledger or not isinstance(ledger.get("history"), list):
        raise ValueError("shared_cash_transfer_ledger_invalid")
    pending = ledger["pending"]
    if pending is not None and (
        not isinstance(pending, dict)
        or not all(key in pending for key in ("operation_id", "requested_at", "request", "status", "destination_balance_before"))
        or not isinstance(pending["request"], dict)
    ):
        raise ValueError("shared_cash_pending_transfer_invalid")


def transfer_matches(transfer, record):
    body = record["request"]
    return (
        transfer.get("transfer_id") == record.get("transfer_id")
        and transfer.get("source") == transfer.get("destination") == "event_contract"
        and transfer.get("source_exchange_shard") == body["source_exchange_shard"]
        and transfer.get("destination_exchange_shard") == body["destination_exchange_shard"]
        and amount(transfer.get("amount")) == Decimal(body["amount"]) / 10000
    )


def reconcile_transfer(request, ledger):
    pending = ledger.get("pending")
    if not pending:
        return None
    if not pending.get("transfer_id"):
        return {"ok": False, "error": "shared_cash_transfer_uncertain", "action_required": True}
    response = request(TRANSFERS_PATH + "/" + quote(pending["transfer_id"], safe=""))
    transfer = response.get("transfer") or {}
    if not transfer_matches(transfer, pending):
        raise ValueError("shared_cash_transfer_identity_mismatch")
    if transfer.get("status") != "complete":
        return {"ok": False, "error": "shared_cash_transfer_" + str(transfer.get("status") or "unknown"),
                "action_required": transfer.get("status") != "pending"}
    fresh = balances(request("/portfolio/balance"))
    destination = pending["request"]["destination_exchange_shard"]
    expected = amount(pending["destination_balance_before"]) + Decimal(pending["request"]["amount"]) / 10000
    if fresh[destination] < expected:
        # An acknowledged transfer with a lagging balance is NOT a new deficit
        # to fund. Keep its durable fence until arrival is observed.
        return {"ok": False, "error": "shared_cash_transfer_balance_unconfirmed"}
    pending.update(status="complete", verified_at=timestamp(), response=transfer)
    ledger.setdefault("history", []).append(pending)
    ledger["pending"] = None
    write_json(LEDGER, ledger)
    return None


def prepare_cash(request, strategy, ticker, budget):
    """Caller holds the interprocess shared bankroll lock through order entry."""
    if strategy not in EXCHANGES or not ticker:
        raise ValueError("shared_cash_destination_invalid")
    require_reservations()
    allocation = request(ALLOCATION_PATH)
    if allocation.get("allocations") != []:
        return {"ok": False, "error": "shared_cash_fixed_allocation_present", "action_required": True}
    ledger = read_object(LEDGER)
    validate_ledger(ledger)
    unresolved = reconcile_transfer(request, ledger)
    if unresolved:
        return unresolved
    market = request("/markets/" + quote(ticker, safe="")).get("market") or {}
    destination = market.get("exchange_index")
    if type(destination) is not int or destination != EXCHANGES[strategy] or market.get("ticker") != ticker:
        raise ValueError("shared_cash_market_destination_unverified")
    raw = request("/portfolio/balance")
    cash = balances(raw)
    reserved = reserved_by_exchange(read_object(bankroll.STATE_FILE))
    free = {index: max(Decimal(0), cash[index] - reserved[index]) for index in EXCHANGES.values()}
    requested = amount(budget(raw) if callable(budget) else budget)
    if requested <= 0:
        raise ValueError("shared_cash_budget_invalid")
    # This is an affordability ceiling, never an increased strategy stake.
    required = min(requested, sum(free.values())).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
    if required <= 0:
        return {"ok": False, "error": "shared_cash_unavailable"}
    deficit = required - free[destination]
    if deficit <= 0:
        return {"ok": True, "enabled": True, "strategy": strategy, "ticker": ticker,
                "exchange_index": destination, "capacity": float(required), "balance": raw}
    source = next(index for index in EXCHANGES.values() if index != destination)
    if free[source] < deficit:
        return {"ok": False, "error": "shared_cash_unavailable"}
    body = {"source": "event_contract", "destination": "event_contract",
            "source_exchange_shard": source, "destination_exchange_shard": destination,
            "source_subaccount": 0, "destination_subaccount": 0,
            "amount": int((deficit * 10000).to_integral_value(rounding=ROUND_CEILING))}
    pending = {"operation_id": str(uuid.uuid4()), "requested_at": timestamp(),
               "strategy": strategy, "ticker": ticker, "request": body, "status": "submitting",
               "destination_balance_before": str(cash[destination])}
    ledger["pending"] = pending
    write_json(LEDGER, ledger)  # Durable intent BEFORE the only transfer POST.
    try:
        response = request(TRANSFER_PATH, method="POST", body=body)
        transfer_id = response.get("transfer_id")
        if not isinstance(transfer_id, str) or not transfer_id.strip():
            raise ValueError("missing_transfer_id")
        pending.update(status="pending", transfer_id=transfer_id)
    except Exception as exc:
        # Neither a timeout nor a generic server error proves no transfer exists.
        pending.update(status="uncertain", error_type=type(exc).__name__)
    write_json(LEDGER, ledger)
    return {"ok": False, "error": "shared_cash_transfer_" + pending["status"],
            "action_required": pending["status"] == "uncertain"}


@contextmanager
def cash_session(strategy, ticker, budget, request):
    """Funding precedes fresh quotes; the common lock stays held through entry.

    A transferred dollar is not spendable until status AND a fresh balance
    confirm it. A transfer attempt always defers that order attempt.
    """
    try:
        active = enabled()
    except Exception as exc:
        yield {"ok": False, "error": str(exc)}
        return
    if not active:
        yield {"ok": True, "enabled": False}
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(bankroll.shared_state_lock())
            review = prepare_cash(request, strategy, ticker, budget)
        except Exception as exc:
            review = {"ok": False, "error": "shared_cash_preflight_failed", "detail": type(exc).__name__ + ": " + str(exc)}
        previous = getattr(_session, "review", None)
        _session.review = review if review.get("ok") else None
        try:
            yield review
        finally:
            _session.review = previous


def capacity_review(strategy, ticker, cost):
    """Fast final check; performs no I/O that could age a confirmed order quote."""
    if not enabled():
        return None
    row = getattr(_session, "review", None) or {}
    if not row.get("ok") or row.get("strategy") != strategy or row.get("ticker") != ticker:
        return {"ok": False, "error": "shared_cash_session_required"}
    if amount(cost) > amount(row["capacity"]):
        return {"ok": False, "error": "shared_cash_capacity_changed"}
    return None


def patient_budget(raw, settings):
    """Upper bound of the existing five signal tiers plus one recovery contract."""
    value = amount(raw["portfolio_value_dollars"]) if "portfolio_value_dollars" in raw else amount(raw["portfolio_value"]) / 100
    equity = amount(raw["balance_dollars"]) + value
    fraction = amount(settings.get("CRYPTO_ETH_PATIENT_BASE_STAKE_PCT", 1)) / 100
    if not 0 < fraction <= 1:
        raise ValueError("invalid_stake_fraction")
    return equity * fraction * Decimal(5) / 3 + 1


def sports_budget(stake):
    return amount(stake) * Decimal("1.07") + Decimal("0.01")


def display_summary():
    try:
        control = read_object(CONTROL, optional=True)
        ledger = read_object(LEDGER, optional=True)
        pending = ledger.get("pending")
        return {"shared_cash_enabled": control.get("enabled", False),
                "shared_cash_pending_transfer": pending,
                "full_crypto_target_verified": False,
                "message": ("Cash transfer " + str(pending.get("status")) + "; entries wait for verification."
                            if pending else control.get("message") or
                            "Shared cash routing is prepared but not enabled. Enable it, then reload both live workers.")}
    except Exception:
        return {"shared_cash_enabled": False, "message": "Shared cash state cannot be read; entries fail closed."}


def bind_transfer(request, transfer_id):
    """Operator supplies the ID from Kalshi history after an ambiguous POST.

    This only reads the exchange and binds an exact matching local journal row;
    it cannot create, retry, clear or mark a transfer complete without evidence.
    """
    with bankroll.shared_state_lock():
        ledger = read_object(LEDGER)
        pending = ledger.get("pending")
        if not pending or pending.get("transfer_id"):
            raise ValueError("No unidentified transfer is awaiting resolution.")
        transfer = request(TRANSFERS_PATH + "/" + quote(transfer_id, safe="")).get("transfer") or {}
        candidate = {**pending, "transfer_id": transfer_id}
        if not transfer_matches(transfer, candidate):
            raise ValueError("Transfer does not match the pending source, destination and amount.")
        requested = datetime.fromisoformat(pending["requested_at"]).timestamp()
        created = float(transfer.get("created_ts", 0))
        if not requested - 5 <= created <= requested + 120:
            raise ValueError("Transfer timestamp does not match the pending attempt.")
        candidate.update(bound_at=timestamp(), status="pending")
        ledger["pending"] = candidate
        write_json(LEDGER, ledger)
        return reconcile_transfer(request, ledger) or {"ok": True, "status": "complete"}


def activate(request):
    """User-operated account change. Does not restart workers or transfer cash."""
    with bankroll.shared_state_lock():
        require_reservations()
        if LEDGER.exists():
            ledger = read_object(LEDGER)
            validate_ledger(ledger)
            if ledger.get("pending") or ledger.get("version") != VERSION:
                raise ValueError("Resolve the existing transfer journal before enabling shared cash.")
        else:
            write_json(LEDGER, {"version": VERSION, "pending": None, "history": []})
        before = request(ALLOCATION_PATH)
        if not isinstance(before.get("allocations"), list):
            raise ValueError("Current exchange allocation could not be verified.")
        record = {"version": VERSION, "enabled": False, "status": "requesting",
                  "requested_at": timestamp(), "previous_allocation": before,
                  "message": "Shared cash activation requires verification."}
        write_json(CONTROL, record)
        if before["allocations"]:
            body = {"allocations": [], "resting_margin_reservation": before.get("resting_margin_reservation", "sum")}
            try:
                request(ALLOCATION_PATH, method="POST", body=body)
            except Exception:
                pass  # Read back; never blindly repeat the mutation.
        try:
            verified = request(ALLOCATION_PATH).get("allocations") == []
        except Exception:
            verified = False
        record.update(enabled=verified, status="enabled" if verified else "verification_needed",
                      verified_at=timestamp() if verified else None,
                      message=("Shared cash routing enabled with no percentage allocation. Reload both live workers to load this version."
                               if verified else "Shared cash target is unverified. Routing remains disabled; check Kalshi before retrying."))
        write_json(CONTROL, record)
        return {"ok": verified, "funding": record, "error": None if verified else record["message"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "enable", "resolve"])
    parser.add_argument("--transfer-id")
    args = parser.parse_args()
    from crypto_paper_bettor import kalshi_credentials, load_settings
    from kalshi_common import kalshi_private_request
    credentials = kalshi_credentials(load_settings())
    def request(path, method="GET", body=None):
        return kalshi_private_request(path, method=method, body=body, **credentials)[0]
    if args.action == "enable":
        print(json.dumps(activate(request), indent=2))
    elif args.action == "resolve":
        if not args.transfer_id:
            parser.error("resolve requires --transfer-id from Kalshi transfer history")
        print(json.dumps(bind_transfer(request, args.transfer_id), indent=2))
    else:
        print(json.dumps({"checked_at": timestamp(), "control": read_object(CONTROL, optional=True),
                          "allocation": request(ALLOCATION_PATH), "balance": request("/portfolio/balance"),
                          "transfers": read_object(LEDGER, optional=True)}, indent=2))


if __name__ == "__main__":
    main()
