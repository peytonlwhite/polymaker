"""User-operated dashboard controls for the optional ETH patient strategy.

Status is read-only. Only an explicit dashboard POST calls ``set_control``;
tests provide fake settings writers and process adapters.
"""
from datetime import datetime, timezone
import json
from pathlib import Path

from crypto_patient_promotion import ENABLED, FIELD, VERSION, parsed
from crypto_patient_runtime import RUNTIME_VERSION


def truth(value):
    return str(value).lower() == "true"


LIVE_PROFILE = {
    ENABLED: "true", "CRYPTO_ETH_PATIENT_BASE_STAKE_PCT": "1",
    "CRYPTO_ETH_PATIENT_ONLY_LIVE": "true", "CRYPTO_LIVE_ORDER_ENABLED": "true",
    "CRYPTO_RUN_LOOP": "true", "CRYPTO_15M_SPRINT_SHADOW_ONLY": "true",
    "CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED": "false", "CRYPTO_LIVE_CORE_ENABLED": "false",
    "MULTI_MARKET_LIVE_ENABLED": "false", "MULTI_MARKET_COMMODITY_LIVE_ENABLED": "false",
}


def activation_blocks(settings, global_settings, installed=True):
    checks = (
        (installed, "Strategy update is not installed."),
        (str(settings.get("CRYPTO_EXECUTION_MODE")) == "live", "Crypto must be in live mode."),
        (truth(global_settings.get("ALLOW_LIVE_TRADING")), "The account live-trading switch is off."),
        (not truth(settings.get("CRYPTO_LIVE_DRY_RUN", "true")), "Crypto dry run is enabled."),
        (not truth(settings.get("CRYPTO_LIVE_REQUIRE_CONFIRMATION", "true")), "Per-order confirmation is enabled."),
        (truth(settings.get("CRYPTO_LIVE_REQUIRE_SHARED_BANKROLL")), "Shared bankroll protection must be enabled."),
        (truth(settings.get("CRYPTO_RECONCILE_LIVE_ON_SCAN")), "Account reconciliation must be enabled."),
        (truth(settings.get("CRYPTO_LIVE_DEPTH_PREFLIGHT_ENABLED")), "Fresh order-depth checks must be enabled."),
        (truth(settings.get("CRYPTO_15M_SPRINT_SHADOW_ONLY")), "Keep the legacy Crypto strategies in shadow mode."),
        (not truth(settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED")), "Pause the other Crypto live pilot first."),
    )
    return [message for ok, message in checks if not ok]


def installed(root):
    root = Path(root)
    try:
        return all((root / name).is_file() for name in ("crypto_patient_promotion.py", "crypto_patient_runtime.py")) and "def crypto_patient_readiness(" in (root / "crypto_paper_bettor.py").read_text(encoding="utf-8")
    except OSError:
        return False


def summary(settings, global_settings, report, *, code_installed=True, now=None):
    now = now or datetime.now(timezone.utc)
    on = truth(settings.get(ENABLED))
    requested = parsed(settings.get("CRYPTO_ETH_PATIENT_REQUESTED_AT"))
    generated = parsed(report.get("generated_at"))
    fresh = bool(generated and 0 <= (now-generated).total_seconds() <= 180)
    runtime = report.get(FIELD) or {}
    acknowledged = bool(fresh and runtime.get("version") == VERSION and requested and generated >= requested)
    blocks = activation_blocks(settings, global_settings, code_installed)
    status, label = "paused", "PAUSED"
    detail = "Patient ETH entries with capped recovery. Start from a 1% bankroll base; size changes with signal strength."
    if on:
        if blocks or not truth(settings.get("CRYPTO_LIVE_ORDER_ENABLED")):
            status, label = "blocked", "ACTIVATION BLOCKED"
            detail = " ".join(blocks) or "The Crypto live-order switch is off."
        elif not acknowledged:
            status, label = "pending", "ACTIVATION PENDING"
            detail = "Waiting for the Crypto worker to acknowledge activation."
        elif runtime.get("status") == "source_history_warmup":
            status, label = "warming_up", "LIVE ENABLED · WARMING UP"
            detail = "Collecting five minutes of fresh ETH history before considering entries."
        elif runtime.get("status") == "blocked":
            status, label = "blocked", "LIVE ENABLED · BLOCKED"
            detail = ", ".join(runtime.get("reasons") or ["Execution checks are blocking new entries."]).replace("_", " ")
        elif runtime.get("status") == "watching":
            status, label = "active", "LIVE ENABLED · WATCHING"
            detail = "Watching ETH for a qualifying signal and a one-cent better entry within 60 seconds."
    elif blocks:
        detail = " ".join(blocks)
    update_available = bool(acknowledged and runtime.get("runtime_version") != RUNTIME_VERSION)
    profile_selected = all(str(settings.get(key)) == value for key, value in LIVE_PROFILE.items())
    ready_blocks = activation_blocks({**settings, **LIVE_PROFILE}, global_settings, code_installed)
    pending = bool(on and requested and (not generated or generated < requested))
    ready = bool(acknowledged and profile_selected and runtime.get("runtime_version") == RUNTIME_VERSION)
    if not ready and not pending and not ready_blocks:
        status, label = "ready", "READY TO MAKE LIVE"
        detail = "Patient ETH capped recovery only · 1% bankroll base · no startup or research waiting gate. Click Make live to load it."
    elif ready and runtime.get("status") == "watching":
        detail = "Patient ETH capped recovery is the only live strategy. Watching for its entry signal; no warmup."
    return {"enabled": on, "status": status, "label": label, "detail": detail,
            "can_make_live": bool(not ready_blocks and not ready and not pending),
            "can_reload": bool(on and update_available and not blocks and truth(settings.get("CRYPTO_LIVE_ORDER_ENABLED"))),
            "can_activate": not on and not blocks, "blocking": blocks,
            "base_stake_pct": settings.get("CRYPTO_ETH_PATIENT_BASE_STAKE_PCT", "1"),
            "worker_acknowledged": acknowledged, "requested_at": settings.get("CRYPTO_ETH_PATIENT_REQUESTED_AT"),
            "worker_report_at": report.get("generated_at"), "code_installed": code_installed}


def set_control(action, settings, global_settings, report, *, save, reload_worker, portfolio, code_installed=True, now=None):
    if action not in {"activate", "pause", "reload", "make_live"}:
        raise ValueError("Choose make_live or pause.")
    now = now or datetime.now(timezone.utc)
    if action == "pause":
        updated = save({ENABLED: "false"})
        return {"ok": True, "control": summary(updated, global_settings, report, code_installed=code_installed, now=now)}
    if action == "make_live":
        selected = {**settings, **LIVE_PROFILE}
        blocks = activation_blocks(selected, global_settings, code_installed)
        if blocks:
            raise ValueError(" ".join(blocks))
        if not isinstance(portfolio, dict) or not isinstance(portfolio.get("bets"), list):
            raise ValueError("Crypto portfolio is unreadable; activation was not changed.")
        records = (portfolio.get(FIELD) or {}).get("records") or {}
        if any(row.get("status") in {"submitting", "order_uncertain"} for row in records.values()):
            raise ValueError("An earlier patient order needs reconciliation before activation.")
        current = summary(settings, global_settings, report, code_installed=code_installed, now=now)
        if not current["can_make_live"]:
            return {"ok": True, "control": current}
        changes = {**LIVE_PROFILE, "CRYPTO_ETH_PATIENT_REQUESTED_AT": now.isoformat()}
        updated = save(changes)
        try:
            worker = reload_worker(needs_reload=(report.get(FIELD) or {}).get("runtime_version") != RUNTIME_VERSION,
                                   open_positions=sum(row.get("status") == "open" for row in portfolio["bets"]))
        except Exception:
            save({key: settings.get(key, "false" if key != "CRYPTO_ETH_PATIENT_REQUESTED_AT" else "") for key in changes})
            raise
        return {"ok": True, "worker": worker,
                "control": summary(updated, global_settings, report, code_installed=code_installed, now=now)}
    blocks = activation_blocks(settings, global_settings, code_installed)
    if blocks:
        raise ValueError(" ".join(blocks))
    if action == "activate" and truth(settings.get(ENABLED)):
        # Duplicate clicks never cause a second restart or reset warmup.
        return {"ok": True, "control": summary(settings, global_settings, report, code_installed=code_installed, now=now)}
    if not isinstance(portfolio, dict) or not isinstance(portfolio.get("bets"), list):
        raise ValueError("Crypto portfolio is unreadable; activation was not changed.")
    unresolved = (portfolio.get(FIELD) or {}).get("records") or {}
    if any(row.get("status") in {"submitting", "order_uncertain"} for row in unresolved.values()):
        raise ValueError("An earlier patient order needs reconciliation before activation.")
    runtime = report.get(FIELD) or {}
    if action == "reload":
        current = summary(settings, global_settings, report, code_installed=code_installed, now=now)
        if not current["can_reload"]:
            if current["worker_acknowledged"] and runtime.get("runtime_version") == RUNTIME_VERSION:
                return {"ok": True, "control": current}
            raise ValueError("A fresh, enabled Crypto worker is required to apply this update.")
        # User-requested reload preserves strategy selection and sizing.
        updated = save({"CRYPTO_ETH_PATIENT_REQUESTED_AT": now.isoformat()})
        try:
            worker = reload_worker(needs_reload=True,
                                   open_positions=sum(row.get("status") == "open" for row in portfolio["bets"]))
        except Exception:
            save({"CRYPTO_ETH_PATIENT_REQUESTED_AT": settings.get("CRYPTO_ETH_PATIENT_REQUESTED_AT", "")})
            raise
        return {"ok": True, "worker": worker,
                "control": summary(updated, global_settings, report, code_installed=code_installed, now=now)}
    needs_reload = runtime.get("version") != VERSION
    if needs_reload and truth(settings.get("CRYPTO_LIVE_ORDER_ENABLED")):
        raise ValueError("Turn off Crypto live orders before loading the new worker.")
    changes = {ENABLED: "true", "CRYPTO_ETH_PATIENT_BASE_STAKE_PCT": "1",
               "CRYPTO_LIVE_ORDER_ENABLED": "true", "CRYPTO_RUN_LOOP": "true",
               "CRYPTO_ETH_PATIENT_REQUESTED_AT": now.isoformat()}
    updated = save(changes)
    try:
        # The adapter reloads only the Crypto trader, preserving companion
        # research workers. It inspects the saved positions before restarting.
        worker = reload_worker(needs_reload=needs_reload,
                               open_positions=sum(row.get("status") == "open" for row in portfolio["bets"]))
    except Exception:
        save({key: settings.get(key, "false" if key == ENABLED else "") for key in changes})
        raise
    return {"ok": True, "worker": worker,
            "control": summary(updated, global_settings, report, code_installed=code_installed, now=now)}


def load_portfolio(path):
    # Do not treat corrupt account state as an empty account during activation.
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))
