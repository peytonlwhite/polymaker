"""Build a compact, sanitized evidence packet for scheduled sports audits."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sports_adaptive_strategy import atomic_write_json, load_state, parse_time, public_summary
from sports_analytics import pricing_analytics
from sports_operations_guardian import HEALTH_FILE
from sports_probability import build_calibration_state
from sports_audit_support import AUDIT_REVISION, CHICAGO, integrity_valid, parse_timestamp, strategy_lane
from sports_audit_analytics import archive_continuity, api_value_shadow, capper_funnel, source_performance_report
from sports_spread_audit import spread_report


PORTFOLIO_FILE = Path("sports_paper_portfolio.json")
CANDIDATE_REGISTRY_FILE = Path("sports_candidate_registry.json")
ADAPTIVE_STATE_FILE = Path("sports_adaptive_overrides.json")
REPORT_FILE = Path("sports_paper_report.json")
SETTINGS_FILE = Path("bot_settings.json")
QUALIFICATION_V2_STATE_FILE = Path("sports_qualification_v2_state.json")

RELEVANT_SETTINGS = (
    "SPORTS_PRICING_V2_DEFAULT_BOOK_WEIGHT",
    "SPORTS_PRICING_V2_PRIOR_STRENGTH",
    "SPORTS_P_EDGE_POSITIVE_MIN",
    "SPORTS_P_EDGE_POSITIVE_MAX_UNITS",
    "SPORTS_WATCHLIST_CONFIRM_SCANS",
    "SPORTS_WATCHLIST_REQUIRE_DISTINCT_UPDATES",
    "SPORTS_MAX_GAME_EXPOSURE_PCT",
    "SPORTS_MAX_TEAM_EXPOSURE_PCT",
    "SPORTS_MAX_SPORT_EXPOSURE_PCT",
    "SPORTS_UNIT_SIZE_PCT",
    "SPORTS_UNIT_MAX_PER_MARKET",
    "LIVE_MAX_DAILY_LOSS_PCT",
    "LIVE_MAX_DAILY_LOSS_CAP",
    "LIVE_MAX_OPEN_EXPOSURE_PCT",
    "LIVE_MAX_OPEN_EXPOSURE_CAP",
    "SPORTS_ADAPTIVE_STRATEGY_ENABLED",
    "SPORTS_QUALIFICATION_V2_ENABLED",
    "SPORTS_QUALIFICATION_V2_AUTO_PROMOTE",
    "SPORTS_QUALIFICATION_V2_MIN_TRAIN_EVENTS",
    "SPORTS_QUALIFICATION_V2_MIN_VALIDATION_EVENTS",
)

CORE_FILES = (
    "sports_paper_bettor.py",
    "sports_probability.py",
    "sports_analytics.py",
    "sports_market_stream.py",
    "sports_candidate_tracking.py",
    "sports_adaptive_strategy.py",
    "sports_strategy_governor.py",
    "sports_operations_guardian.py",
    "sports_bet_intelligence.py",
    "sports_qualification_v2.py",
)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _settled_rows(portfolio):
    return [
        row for row in (portfolio.get("history") or [])
        if str(row.get("result") or "").upper() in {"WIN", "LOSS"}
        and strategy_lane(row) == "autonomous" and integrity_valid(row)
    ]


def performance_summary(rows, *, since=None):
    if since:
        rows = [
            row for row in rows
            if (parse_timestamp(row.get("settled_at") or row.get("placed_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since
        ]
    stake = sum(_number(row.get("stake")) for row in rows)
    profit = sum(_number(row.get("profit")) for row in rows)
    return {
        "count": len(rows),
        "wins": sum(str(row.get("result") or "").upper() == "WIN" for row in rows),
        "stake": round(stake, 2),
        "profit": round(profit, 2),
        "roi_pct": round(profit / stake * 100.0, 2) if stake > 0 else None,
    }


def segment_performance(rows):
    grouped = defaultdict(list)
    for row in rows:
        price = _number(row.get("entry_price"), -1)
        price_bucket = f"{int(price // 10) * 10:02d}-{int(price // 10) * 10 + 9:02d}c" if 0 < price < 100 else "unknown"
        key = "|".join((
            str(row.get("sport_key") or "unknown"),
            str(row.get("market_type") or "unknown"),
            str(row.get("bet_timing_bucket") or "unknown"),
            price_bucket,
        ))
        grouped[key].append(row)
    result = []
    for key, selected in grouped.items():
        summary = performance_summary(selected)
        if summary["count"] >= 5:
            result.append({"segment": key, **summary})
    return sorted(result, key=lambda row: (row.get("roi_pct") or 0, -row["count"]))


def file_fingerprints():
    rows = []
    for filename in CORE_FILES:
        path = Path(filename)
        if not path.exists():
            rows.append({"path": filename, "missing": True})
            continue
        data = path.read_bytes()
        rows.append({
            "path": filename,
            "sha256": hashlib.sha256(data).hexdigest()[:16],
            "bytes": len(data),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        })
    return rows


def _metric_subset(row):
    row = row if isinstance(row, dict) else {}
    keys = (
        "count", "event_count", "effective_event_count", "wins", "losses",
        "stake", "profit", "profit_units", "roi_pct", "win_rate_pct",
        "brier", "log_loss", "expected_calibration_error",
        "expected_calibration_error_pp", "calibration_bias_pp",
        "average_clv_5m_cents", "clv_5m_count", "max_drawdown_units",
        "book_weight", "promotion_validated", "validated", "reason",
        "train_event_count", "validation_event_count",
        "brier_improvement_vs_kalshi", "log_loss_improvement_vs_kalshi",
    )
    return {key: row.get(key) for key in keys if row.get(key) is not None}


def compact_calibration_packet(calibration):
    calibration = calibration if isinstance(calibration, dict) else {}

    def compact_profiles(profiles, limit):
        ranked = sorted(
            (profiles or {}).items(),
            key=lambda item: _number((item[1] or {}).get("effective_event_count")),
            reverse=True,
        )[:limit]
        return {
            key: {
                **_metric_subset(value),
                "walk_forward_validation": _metric_subset(
                    (value or {}).get("walk_forward_validation") or {}
                ),
            }
            for key, value in ranked
        }

    global_row = calibration.get("global") or {}
    return {
        "version": calibration.get("version"),
        "generated_at": calibration.get("generated_at"),
        "global": {
            **_metric_subset(global_row),
            "walk_forward_validation": _metric_subset(
                global_row.get("walk_forward_validation") or {}
            ),
        },
        "sports": compact_profiles(calibration.get("sports"), 20),
        "sport_markets": compact_profiles(calibration.get("sport_markets"), 30),
        "largest_segments": compact_profiles(calibration.get("segments"), 30),
    }


def compact_qualification_packet(state):
    state = state if isinstance(state, dict) else {}
    segments = {}
    for key, row in (state.get("segments") or {}).items():
        gates = row.get("unit_gates") or {}
        selected_gate = None
        selected_unit = None
        for unit, gate in sorted(gates.items(), key=lambda item: _number(item[0]), reverse=True):
            if gate.get("passed") or selected_gate is None:
                selected_unit = unit
                selected_gate = gate
            if gate.get("passed"):
                break
        selected_gate = selected_gate or {}
        segments[key] = {
            "status": row.get("status"),
            "eligible_max_units": row.get("eligible_max_units"),
            "promoted_max_units": row.get("promoted_max_units"),
            "demotion_reason": row.get("demotion_reason"),
            "updated_at": row.get("updated_at"),
            "representative_unit": selected_unit,
            "representative_gate_passed": bool(selected_gate.get("passed")),
            "representative_blockers": list(selected_gate.get("blockers") or [])[:8],
            "representative_evidence": _metric_subset(
                selected_gate.get("all_selected") or {}
            ),
            "rolling_demotion_review": _metric_subset(
                row.get("rolling_demotion_review") or {}
            ),
        }
    return {
        "version": state.get("version"),
        "qualification_version": state.get("qualification_version"),
        "config_hash": state.get("config_hash"),
        "enabled": state.get("enabled"),
        "auto_promote": state.get("auto_promote"),
        "eligible_resolved_observations": state.get("eligible_resolved_observations", 0),
        "segments": segments,
        "recent_transitions": (state.get("transitions") or [])[-20:],
    }


def compact_live_qualification(summary):
    summary = summary if isinstance(summary, dict) else {}
    return {
        key: summary.get(key)
        for key in (
            "version", "mode", "candidate_count", "shadow_qualified_count",
            "shadow_rescue_count", "execution_active_count", "promoted_segments",
            "tracked_segments", "hard_blockers", "config_hash",
        )
        if summary.get(key) is not None
    }


def compact_pricing_packet(summary):
    summary = summary if isinstance(summary, dict) else {}
    compact = {
        key: value
        for key, value in summary.items()
        if key not in {"segments", "quality_scores"}
    }
    compact["quality_scores"] = {
        key: _metric_subset(value)
        for key, value in (summary.get("quality_scores") or {}).items()
    }
    compact["segments"] = {
        segment: {
            source: _metric_subset(metrics)
            for source, metrics in (sources or {}).items()
        }
        for segment, sources in (summary.get("segments") or {}).items()
    }
    return compact


def compact_live_readiness(summary):
    summary = summary if isinstance(summary, dict) else {}
    return {
        key: summary.get(key)
        for key in (
            "ready", "execution_mode", "account_ok", "cash_balance",
            "remote_open_count", "local_open_count", "unmatched_remote_count",
            "unmatched_local_count", "cash_difference", "warnings", "blockers",
            "last_reconciled_at", "reason",
        )
        if summary.get(key) is not None
    }


def build_packet(scope="weekly", now=None):
    now = parse_timestamp(now or datetime.now(timezone.utc))
    portfolio = read_json(PORTFOLIO_FILE, {})
    registry = read_json(CANDIDATE_REGISTRY_FILE, {})
    live_report = read_json(REPORT_FILE, {})
    settings = read_json(SETTINGS_FILE, {})
    rows = _settled_rows(portfolio)
    pricing_rows = [row for row in rows if isinstance(row.get("pricing_v2"), dict)]
    calibration = build_calibration_state(
        pricing_rows,
        default_weight=_number(settings.get("SPORTS_PRICING_V2_DEFAULT_BOOK_WEIGHT"), 0.5),
        prior_strength=_number(settings.get("SPORTS_PRICING_V2_PRIOR_STRENGTH"), 25),
        generated_at=now.astimezone().isoformat(timespec="seconds"),
    )
    lookback_days = 30 if scope == "monthly" else 7
    cutoff = now - timedelta(days=lookback_days)
    window_rows = [row for row in rows if (stamp := parse_timestamp(row.get("settled_at"))) is not None and cutoff <= stamp <= now]
    decision_paths = sorted(Path("archives/storage_retention/analytics").rglob("sports_decision_snapshots.jsonl*"))
    if Path("sports_decision_snapshots.jsonl").exists():
        decision_paths.append(Path("sports_decision_snapshots.jsonl"))
    adaptive = load_state(ADAPTIVE_STATE_FILE)
    qualification_v2 = read_json(QUALIFICATION_V2_STATE_FILE, {})
    return {
        "version": "sports-strategy-audit-packet-v3-source-time",
        "audit_revision": AUDIT_REVISION,
        "scope": scope,
        "generated_at": now.astimezone(CHICAGO).isoformat(timespec="seconds"),
        "window": {"days": lookback_days, "start": cutoff.astimezone(CHICAGO).isoformat(), "end": now.astimezone(CHICAGO).isoformat()},
        "source_performance": source_performance_report(portfolio.get("history") or [], now=now, bootstrap_samples=1000),
        "spread_analysis": spread_report(portfolio.get("history") or [], now=now, build_id=(live_report.get("strategy_identity") or {}).get("build_id"), bootstrap_samples=1000),
        "capper_funnel_shadow": capper_funnel((read_json(Path("sports_capper_tickets.json"), {}) or {}).get("tickets") or [], now=now),
        "api_value_shadow": api_value_shadow((read_json(Path("sports_odds_usage.json"), {}) or {}).get("calls") or []),
        "retained_decision_continuity": archive_continuity(decision_paths, now=now),
        "guardrails": {
            "may_reduce_risk": True,
            "may_restore_only_to_baseline": True,
            "may_increase_risk_above_baseline": False,
            "may_activate_new_api_or_sport": False,
            "material_live_code_changes_require_user_approval": True,
        },
        "strategy_identity": live_report.get("strategy_identity"),
        "operations_health": read_json(HEALTH_FILE, {}),
        "live_status": {
            "report_generated_at": live_report.get("generated_at"),
            "execution_mode": live_report.get("execution_mode"),
            "candidate_count": live_report.get("candidate_count"),
            "placed_count": live_report.get("placed_count"),
            "no_bet_reason": live_report.get("no_bet_reason"),
            "live_data": live_report.get("live_data") or {},
            "kalshi_stream": live_report.get("kalshi_stream") or {},
            "live_readiness": compact_live_readiness(
                live_report.get("live_readiness") or {}
            ),
            "qualification_v2": compact_live_qualification(
                live_report.get("qualification_v2") or {}
            ),
        },
        "performance": {
            "lifetime": performance_summary(rows),
            f"last_{lookback_days}_days": performance_summary(window_rows),
            "weakest_segments": segment_performance(window_rows)[:30],
            "strongest_segments": list(reversed(segment_performance(window_rows)[-30:])),
        },
        "pricing_analytics": compact_pricing_packet(
            pricing_analytics(window_rows)
        ),
        "calibration": compact_calibration_packet(calibration),
        "adaptive_strategy": adaptive_strategy_packet(adaptive),
        "qualification_v2": compact_qualification_packet(qualification_v2),
        "candidate_population": {
            "pending": len((registry.get("pending") or {})),
            "resolved_observations": len(registry.get("resolved_observations") or []),
            "resolved_tickers": len((registry.get("resolved_tickers") or {})),
        },
        "settings": {key: settings.get(key) for key in RELEVANT_SETTINGS},
        "source_fingerprints": file_fingerprints(),
    }


def adaptive_strategy_packet(state):
    summary = public_summary(state, limit=30)
    return {
        **summary,
        "recent_transitions": (state.get("audit_log") or [])[-20:],
        "policy": state.get("policy") or {},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("weekly", "monthly"), default="weekly")
    parser.add_argument("--output")
    args = parser.parse_args()
    output = Path(args.output or f"sports_{args.scope}_strategy_audit.json")
    packet = build_packet(args.scope)
    atomic_write_json(output, packet)
    print(json.dumps({
        "ok": True,
        "scope": args.scope,
        "output": str(output),
        "generated_at": packet["generated_at"],
        "settled_count": packet["performance"]["lifetime"]["count"],
        "adaptive_overrides": len(packet["adaptive_strategy"]["active_overrides"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
