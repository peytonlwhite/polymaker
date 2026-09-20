"""Passive sports audit metrics. No execution decisions or network requests."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import random

from sports_audit_support import AUDIT_REVISION, CHICAGO, integrity_valid, number, parse_timestamp, strategy_lane


def event_key(row, index=0):
    return str(row.get("game_key") or row.get("event_id") or row.get("kalshi_event_ticker") or row.get("kalshi_ticker") or f"unknown-{index}")


def performance(rows, *, bootstrap_samples=0):
    rows = list(rows)
    stake = sum(number(row.get("stake")) or 0.0 for row in rows)
    profit = sum(number(row.get("profit")) or 0.0 for row in rows)
    groups = defaultdict(lambda: [0.0, 0.0])
    unit_rows = [row for row in rows if (number(row.get("unit_size")) or 0) > 0]
    for index, row in enumerate(rows):
        group = groups[event_key(row, index)]
        group[0] += number(row.get("stake")) or 0.0
        group[1] += number(row.get("profit")) or 0.0
    result = {"records": len(rows), "effective_events": len(groups), "stake": round(stake, 2), "after_fee_profit": round(profit, 2), "roi_pct": round(100 * profit / stake, 3) if stake else None, "net_units": round(sum((number(row.get("profit")) or 0.0) / float(row["unit_size"]) for row in unit_rows), 3), "unit_records": len(unit_rows), "wins": sum(row.get("result") == "WIN" for row in rows), "sample_warning": "exploratory_multiple_segments_not_adjusted", "affects_execution": False}
    result["missing_unit_records"] = len(rows) - len(unit_rows)
    if bootstrap_samples and len(groups) >= 2:
        rng = random.Random(193)
        values = list(groups.values())
        draws = []
        for _ in range(min(10000, int(bootstrap_samples))):
            sample = rng.choices(values, k=len(values))
            denominator = sum(value[0] for value in sample)
            if denominator > 0:
                draws.append(100 * sum(value[1] for value in sample) / denominator)
        draws.sort()
        result["event_bootstrap_roi_95"] = [round(draws[int((len(draws) - 1) * q)], 3) for q in (0.025, 0.975)] if draws else None
    return result


def _band(value, width):
    value = number(value)
    if value is None:
        return "missing"
    low = (value // width) * width
    return f"{low:g}_to_{low + width:g}"


def segment_dimensions(row):
    state = row.get("game_state_features") or {}
    phase = state.get("inning") if state.get("inning") is not None else state.get("current_set")
    phase_label = "inning" if state.get("inning") is not None else "set"
    return {
        "sport_league": str(row.get("sport_key") or "unknown"),
        "market_type": str(row.get("market_type") or "unknown"),
        "timing": str(row.get("bet_timing_bucket") or row.get("timing_bucket") or "unknown"),
        "game_phase": f"{phase_label}_{phase}" if phase is not None else "missing",
        "price_band": _band(row.get("entry_price"), 10),
        "edge_band": _band(row.get("net_edge") if row.get("net_edge") is not None else row.get("edge"), 5),
        "confidence_band": _band(row.get("confidence_score"), 10),
        "unit_band": _band(row.get("unit_count"), 0.5),
        "source": str(row.get("capper_source") or row.get("odds_source") or row.get("source") or "unknown"),
        "execution_mode": str(row.get("mode") or "unknown"),
        "strategy_revision": str(row.get("strategy_version") or "legacy_unknown") + ":" + str(row.get("strategy_config_hash") or "unknown"),
        "strategy_build": str(row.get("strategy_build_id") or "legacy_unknown") + ":" + str(row.get("strategy_config_hash") or "unknown"),
    }


def source_performance_report(rows, *, now=None, bootstrap_samples=0):
    now = parse_timestamp(now or datetime.now(timezone.utc))
    settled = [row for row in rows if row.get("result") in {"WIN", "LOSS", "VOID"} and parse_timestamp(row.get("settled_at")) is not None]
    report = {"revision": AUDIT_REVISION, "as_of": now.astimezone(CHICAGO).isoformat(), "attribution": "explicit_source_only", "roi_basis": "after_fee_profit_over_stake", "unit_basis": "recorded_unit_value_at_entry", "lanes": {}}
    for lane in sorted({strategy_lane(row) for row in settled}):
        windows = {}
        for days in (7, 30):
            financial = [row for row in settled if strategy_lane(row) == lane and now - timedelta(days=days) <= parse_timestamp(row["settled_at"]) <= now]
            intended = [row for row in financial if integrity_valid(row)]
            dimensions = defaultdict(lambda: defaultdict(list))
            for row in intended:
                for dimension, key in segment_dimensions(row).items():
                    dimensions[dimension][key].append(row)
            windows[f"{days}d"] = {"financial": performance(financial, bootstrap_samples=bootstrap_samples), "intended_strategy": performance(intended, bootstrap_samples=bootstrap_samples), "integrity_excluded": performance([row for row in financial if not integrity_valid(row)]), "segments": {dimension: {key: performance(group) for key, group in sorted(groups.items())} for dimension, groups in dimensions.items()}}
            modes = defaultdict(list)
            for row in financial:
                modes[str(row.get("mode") or "unknown")].append(row)
            windows[f"{days}d"]["execution_modes"] = {
                mode: {
                    "financial": performance(group),
                    "intended_strategy": performance([row for row in group if integrity_valid(row)]),
                    "integrity_excluded": performance([row for row in group if not integrity_valid(row)]),
                }
                for mode, group in sorted(modes.items())
            }
            builds = defaultdict(list)
            for row in intended:
                builds[(str(row.get("mode") or "unknown"), segment_dimensions(row)["strategy_build"])].append(row)
            windows[f"{days}d"]["build_cohorts"] = [
                {"execution_mode": mode, "build": build, **performance(group)}
                for (mode, build), group in sorted(builds.items())
            ]
        report["lanes"][lane] = windows
    return report


def capper_funnel(tickets, *, now=None):
    now = parse_timestamp(now or datetime.now(timezone.utc))
    output = {"mode": "shadow", "affects_execution": False, "windows": {}}
    for days in (7, 30):
        rows = [row for row in tickets if (stamp := parse_timestamp(row.get("approved_at") or row.get("created_at"))) is not None and now - timedelta(days=days) <= stamp <= now]
        eligible = [row for row in rows if row.get("executable", True) and row.get("pick_type") not in {"parlay", "ladder"} and row.get("status") != "cancelled" and not row.get("coverage_excluded")]
        stages = Counter(parsed=len(rows), eligible=len(eligible), parent_containers=sum(row.get("pick_type") in {"parlay", "ladder"} for row in rows))
        blockers = Counter()
        segments = defaultdict(Counter)
        for row in eligible:
            snapshot = row.get("match_snapshot") or {}
            fills = row.get("fills") or []
            existing = "existing_position" in str(row.get("status_reason") or "")
            flags = {
                "scanned": (number(row.get("scan_count")) or 0) > 0 or bool(row.get("last_scan_at") or row.get("last_scanned_at")),
                "matched": bool(row.get("matched_at") or row.get("first_match_snapshot") or snapshot.get("components") or (number(snapshot.get("matched_components")) or 0) > 0),
                "live_watched": bool(snapshot.get("game_started") or row.get("live_watch_seen") or any(component.get("game_started") for component in snapshot.get("components") or [])),
                "acceptable_price": bool(snapshot.get("price_acceptable") or row.get("price_acceptable_seen") or (snapshot.get("price_review") or {}).get("ok")),
                "has_fill": bool(fills),
                "complete": bool(fills and row.get("status") in {"placed", "settled"}),
                "partial_fill": bool(fills and row.get("status") not in {"placed", "settled"}),
                "existing_position_satisfied": existing,
                "manual_position_satisfied": row.get("manual_position_satisfied") is True,
                "expired": row.get("status") == "expired",
            }
            stages.update(key for key, value in flags.items() if value)
            key = "|".join(str(row.get(field) or "unknown") for field in ("source", "sport_key", "market_type", "line_unit", "pick_type"))
            segments[key].update(eligible=1)
            segments[key].update(name for name, value in flags.items() if value)
            if not fills:
                blockers[str(row.get("last_actionable_status_reason") or row.get("status_reason") or "unknown")] += 1
        output["windows"][f"{days}d"] = {"stages": dict(stages), "fill_coverage_pct": round(100 * stages["has_fill"] / len(eligible), 3) if eligible else None, "last_blockers": dict(blockers), "segments": {key: dict(value) for key, value in sorted(segments.items())}, "missing_live_or_price_evidence_is_unknown": True}
    return output


def api_value_shadow(calls, *, candidates=None):
    """Estimate redundant refreshes without suppressing any request."""
    groups = defaultdict(lambda: {"calls": 0, "credits": 0.0, "same_update_interval_calls": 0, "shadow_redundant_credits": 0.0})
    previous = {}
    ordered = sorted(calls, key=lambda row: parse_timestamp(row.get("at")) or datetime.min.replace(tzinfo=timezone.utc))
    for row in ordered:
        sport, markets = str(row.get("sport_key") or "unknown"), str(row.get("markets") or "")
        bucket = groups[f"{sport}|{markets}"]
        cost = number(row.get("actual_cost"))
        cost = number(row.get("estimated_cost")) or 0.0 if cost is None else cost
        bucket["calls"] += 1
        bucket["credits"] += cost
        at = parse_timestamp(row.get("at"))
        signature = (sport, row.get("event_id"), tuple(sorted(markets.split(","))), str(row.get("regions") or ""))
        interval = 60 if "alternate" in markets or "scores" in markets else 40
        if at and signature in previous and (at - previous[signature]).total_seconds() < interval:
            bucket["same_update_interval_calls"] += 1
            bucket["shadow_redundant_credits"] += cost
        if at:
            previous[signature] = at
    opportunities = defaultdict(lambda: {"candidates": 0, "events": set(), "provider_updates": set(), "confirmed_candidates": 0, "skip_reasons": Counter()})
    for row in candidates or []:
        key = "|".join(str(row.get(field) or "unknown") for field in ("sport_key", "market_type"))
        bucket = opportunities[key]
        bucket["candidates"] += 1
        bucket["events"].add(event_key(row))
        token = ((row.get("pricing_v2") or {}).get("consensus") or {}).get("update_token")
        if token:
            bucket["provider_updates"].add(str(token))
        bucket["confirmed_candidates"] += int(bool((row.get("edge_confirmation") or {}).get("ok")))
        bucket["skip_reasons"].update(set(row.get("skip_reasons") or []))
    latest_scan = {key: {**value, "events": len(value["events"]), "provider_updates": len(value["provider_updates"]), "skip_reasons": dict(value["skip_reasons"])} for key, value in opportunities.items()}
    purposes = Counter(str(row.get("purpose") or "legacy_unknown") for row in calls)
    return {"mode": "shadow", "requests_suppressed": 0, "affects_execution": False, "groups": dict(groups), "calls_by_purpose": dict(purposes), "latest_scan_opportunities": latest_scan, "limitation": "Retained-call cadence is an upper bound on possible savings, not measured waste. Latest scan opportunities are a separate cohort. No request is suppressed."}


def archive_continuity(paths, *, now=None, days=30):
    """Stream raw or compressed JSONL; report corruption instead of hiding it."""
    now = parse_timestamp(now or datetime.now(timezone.utc))
    counts = Counter()
    malformed = 0
    unreadable = []
    for path in paths:
        path = Path(path)
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8-sig") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        stamp = parse_timestamp(row.get("generated_at") or row.get("ts") or row.get("at"))
                    except (ValueError, AttributeError):
                        malformed += 1
                        continue
                    if stamp and now - timedelta(days=days) <= stamp <= now:
                        counts[stamp.astimezone(CHICAGO).date().isoformat()] += 1
        except (OSError, EOFError, UnicodeError):
            unreadable.append(path.name)
    calendar = [(now.astimezone(CHICAGO).date() - timedelta(days=index)).isoformat() for index in range(days)]
    return {"mode": "passive", "days": days, "daily_records": dict(sorted(counts.items())), "days_without_retained_records": sorted(day for day in calendar if not counts[day]), "malformed_records": malformed, "unreadable_files": unreadable, "absence_is_not_proof_of_no_scanning": True}


def reconciliation_transition(previous, audit, *, now=None):
    now = parse_timestamp(now or datetime.now(timezone.utc))
    previous = dict(previous or {})
    ok = audit.get("ok") is True
    started = parse_timestamp(previous.get("incident_started_at"))
    if not ok and started is None:
        started = now
    return {"ok": ok, "checked_at": now.astimezone(CHICAGO).isoformat(), "warnings": list(audit.get("warnings") or []), "incident_started_at": None if ok else started.isoformat(), "resolved_at": now.isoformat() if ok and started else previous.get("resolved_at"), "last_incident_duration_seconds": round((now - started).total_seconds(), 3) if ok and started else previous.get("last_incident_duration_seconds"), "incident_age_seconds": round((now - started).total_seconds(), 3) if started and not ok else 0.0}
