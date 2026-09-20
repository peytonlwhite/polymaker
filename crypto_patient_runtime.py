"""Optional patient promotion, called by the existing single portfolio writer.

Importing this module never starts a worker or enables execution. All external
operations are provided by the existing bot adapter and mocked in tests.
"""
import time
import shared_cash_pool

from crypto_patient_promotion import (
    FIELD, OWNER, VERSION, PatientPromotion, enabled, number, parsed, risk_context, signal_review,
)


RUNTIME_VERSION = "eth-patient-shared-cash-v4"


def run_patient_scan(bot, settings, portfolio, candidates, *, sleep=time.sleep, monotonic=time.monotonic):
    report = {"status": "disabled", "owner": OWNER, "version": VERSION,
              "runtime_version": RUNTIME_VERSION, "placed": 0, "decisions": [], "rejections": {}}
    placed = []
    if not enabled(settings) or bot.execution_mode(settings) != "live":
        return placed, report
    readiness = bot.crypto_patient_readiness(settings)
    if not readiness["ok"]:
        report.update(status="blocked", reasons=readiness["blocking"])
        return placed, report
    engine = PatientPromotion(portfolio)
    # A crash/timeout after submitting is never evidence of an unfilled order.
    # Reconciliation and an explicit operator resolution are required to resume.
    unresolved = [r for r in engine.state["records"].values() if r["status"] in {"submitting", "order_uncertain"}]
    if unresolved:
        report.update(status="blocked", reasons=["unresolved_order_intent"], unresolved=[r["ticker"] for r in unresolved])
        return placed, report
    # Venue history belongs to the continuously running ETH feeds, not each
    # rotating contract. Evaluate fresh signals immediately on a new ticker.
    selected = [row for row in candidates
                if row.get("asset") == "ETH" and row.get("market_lane") == "crypto_15m"]
    report.update(status="watching", candidate_count=len(selected))
    if not selected:
        return placed, report
    runtime = bot.build_execution_shadow_runtime(settings, independent=True)

    def refresh(row):
        fresh = runtime["fresh_snapshot"](row)
        stream = bot.KALSHI_CRYPTO_STREAM
        book = stream.snapshot(row["ticker"], max_age_seconds=1) if stream else {}
        if book.get("sequence_valid") is not True:
            fresh["data_quality"] = {"score": 0}
        return fresh

    def quotes(row, side):
        payload, fetched_at = bot.fetch_fresh_kalshi_orderbook(settings, row["ticker"], force_rest=True)
        def quote_for(count):
            return {**bot.kalshi_orderbook_depth_summary(payload, side, required_contracts=count),
                    "fetched_at": fetched_at, "source": "kalshi_rest_orderbook",
                    "sequence_valid": payload.get("sequence_valid", True)}
        return quote_for

    def funding_request(path, method="GET", body=None):
        return bot.kalshi_private_request(path, method=method, body=body,
                                         **bot.kalshi_credentials(bot.load_settings()))[0]

    report["status"] = "watching"
    for original in selected:
        if original["ticker"] in engine.state["records"]:
            continue
        fresh = refresh(original)
        side, reasons = signal_review(fresh, bot.utc_now())
        if reasons:
            for reason in reasons:
                report["rejections"][reason] = report["rejections"].get(reason, 0) + 1
            continue
        quote_for = quotes(fresh, side)
        # Recompute signal after the independent confirmation request.
        fresh = refresh(original)
        confirmed_side, reasons = signal_review(fresh, bot.utc_now())
        if reasons or confirmed_side != side:
            continue
        engine.observe(fresh, quote_for, bot.utc_now())
    bot.save_portfolio(portfolio)
    scan_deadline = monotonic() + 60
    reconciliation = None
    while monotonic() <= scan_deadline:
        waiting = [r for r in selected if (engine.state["records"].get(r["ticker"]) or {}).get("status") == "waiting"]
        if not waiting:
            break
        current_settings = bot.load_settings()
        if not enabled(current_settings) or not bot.crypto_patient_readiness(current_settings)["ok"]:
            report.update(status="blocked", reasons=["execution_controls_changed"])
            break
        for original in waiting:
            if len(placed) >= bot.effective_scan_bet_limit(current_settings):
                break
            record = engine.state["records"][original["ticker"]]
            if bot.utc_now() > parsed(record["deadline"]):
                record.update(status="expired", reason="patient_deadline_elapsed")
                continue
            fresh = refresh(original)
            if number(fresh.get(record["side"] + "_ask"), 100) > record["anchor_cents"] - 1:
                continue
            # The cash lock covers funding, new quotes, submission and portfolio
            # persistence. Pending transfers leave this intent in waiting state.
            with shared_cash_pool.cash_session(
                "crypto", original["ticker"],
                lambda raw: shared_cash_pool.patient_budget(raw, current_settings),
                funding_request,
            ) as funding:
                if not funding.get("ok"):
                    record["reason"] = funding["error"]
                    report["funding"] = funding
                    if funding.get("action_required"):
                        report.update(status="blocked", reasons=[funding["error"]])
                        bot.save_portfolio(portfolio)
                        return placed, report
                    continue
                # Fresh account state precedes price confirmation, so a slow account
                # request cannot turn a one-second quote into a stale submission.
                reconciliation = bot.reconcile_live_account(portfolio, current_settings)
                quote_for = quotes(fresh, record["side"])
                fresh = refresh(original)
                now = bot.utc_now()
                risk = risk_context(portfolio, reconciliation, fresh, current_settings, now)
                proposal = engine.prepare(fresh, quote_for, risk, current_settings, now)
                if not proposal:
                    continue
                # Persist the exact intent before calling any order-capable adapter.
                record.update(status="submitting", client_order_id=proposal[FIELD]["client_order_id"], proposal=proposal[FIELD])
                bot.save_portfolio(portfolio)
                try:
                    result = bot.place_live_kalshi_order(current_settings, portfolio, proposal, proposal[FIELD]["principal_stake"])
                    if result.get("ok"):
                        bet = bot.record_live_bet(portfolio, proposal, result)
                        placed.append(bet)
                        record.update(status="filled", bet_id=bet["id"])
                    elif result.get("error") == "live_order_exception":
                        record.update(status="order_uncertain", reason=result["error"])
                    else:
                        record.update(status="not_filled", reason=result.get("error") or "order_failed")
                    quality = bot.build_execution_quality_record(proposal, "filled" if result.get("ok") else "order_failed", live_order=result, reason=result.get("error") or "", settings=current_settings)
                    bot.persist_execution_quality_record(current_settings, quality)
                    bot.event_line({"type": "eth_patient_order_result", "ticker": original["ticker"], "intent": record, "result": result})
                    report["decisions"].append({"ticker": original["ticker"], "status": record["status"], "sizing": proposal[FIELD]})
                except Exception:
                    record.update(status="order_uncertain", reason="adapter_exception")
                    bot.save_portfolio(portfolio)
                    raise
                bot.save_portfolio(portfolio)
                if record["status"] == "order_uncertain":
                    report.update(status="blocked", reasons=["unresolved_order_intent"])
                    report["placed"] = len(placed)
                    return placed, report
        if len(placed) >= bot.effective_scan_bet_limit(current_settings):
            break
        if all((engine.state["records"].get(r["ticker"]) or {}).get("status") != "waiting" for r in selected):
            break
        remaining = scan_deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(2, remaining))
    for row in engine.state["records"].values():
        if row["status"] == "waiting" and bot.utc_now() >= parsed(row["deadline"]):
            row.update(status="expired", reason="patient_deadline_elapsed")
    bot.save_portfolio(portfolio)
    report.update(placed=len(placed), records={ticker: {k: row.get(k) for k in ("status", "reason", "deadline", "anchor_cents")} for ticker, row in engine.state["records"].items()})
    return placed, report
