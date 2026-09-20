"""Explain realized spread results using evidence recorded at entry, never hindsight."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from sports_audit_support import CHICAGO, integrity_valid, number, parse_timestamp, strategy_lane
from sports_audit_analytics import performance, segment_dimensions


def entry_evidence(row):
    consensus = (row.get("pricing_v2") or {}).get("consensus") or {}
    observations = consensus.get("observations") or []
    line = number(row.get("market_line"))  # Already the SELECTED side's signed line.
    exact = bool(observations) and line is not None and all(
        item.get("all_exact_lines") is True and not item.get("interpolated")
        and number(item.get("selected_point")) == line for item in observations)
    ages = [number(item.get("age_minutes")) for item in observations]
    age = max(ages) if ages and all(value is not None and value >= 0 for value in ages) else None
    state = row.get("game_state_features") or {}
    phase = next((f"{key}_{state[key]:g}" for key in ("inning", "quarter", "period", "current_set")
                  if isinstance(state.get(key), (int, float))), "missing")
    families = number(consensus.get("independent_family_count"))
    agreement = number(consensus.get("family_probability_range_pp"))
    return {
        "exact_line": "verified" if exact else "interpolated" if any(item.get("interpolated") for item in observations) else "unverified",
        "signed_line": f"{line:+g}" if line is not None else "missing",
        "book_families": f"{families:g}" if families is not None else "missing",
        "book_agreement": "missing" if agreement is None else "0_to_3pp" if agreement <= 3 else "3_to_7pp" if agreement <= 7 else "over_7pp",
        "oldest_quote_age": "missing" if age is None else "under_1m" if age <= 1 else "1_to_2m" if age <= 2 else "over_2m",
        "game_phase": phase,
        "score_context": "fresh" if state.get("score_context_fresh") is True else "unverified",
    }


def spread_report(rows, *, now=None, build_id=None, bootstrap_samples=400):
    now = parse_timestamp(now or datetime.now(timezone.utc))
    eligible = [row for row in rows if row.get("mode") == "live" and strategy_lane(row) == "autonomous"
                and row.get("market_type") == "spread" and row.get("result") in {"WIN", "LOSS", "VOID"}
                and (stamp := parse_timestamp(row.get("settled_at"))) and stamp <= now]
    windows = {}
    for days in (7, 30):
        financial = [row for row in eligible if parse_timestamp(row["settled_at"]) >= now - timedelta(days=days)]
        intended = [row for row in financial if integrity_valid(row)]
        groups = defaultdict(lambda: defaultdict(list))
        for row in intended:
            dims = segment_dimensions(row)
            evidence = entry_evidence(row)
            dimensions = {**evidence, **{key: dims[key] for key in ("timing", "sport_league", "edge_band", "unit_band", "strategy_build")}}
            dimensions["league_line_timing"] = "|".join((dims["sport_league"], evidence["signed_line"], dims["timing"]))
            for name, key in dimensions.items():
                groups[name][key].append(row)
        windows[f"{days}d"] = {
            "financial": performance(financial, bootstrap_samples=bootstrap_samples),
            "intended_strategy": performance(intended, bootstrap_samples=bootstrap_samples),
            "integrity_excluded": performance([row for row in financial if not integrity_valid(row)]),
            "segments": {name: {key: performance(group) for key, group in sorted(buckets.items())}
                         for name, buckets in groups.items()},
            "current_build": performance([row for row in intended if build_id and row.get("strategy_build_id") == build_id]),
        }
    return {"version": "sports-spread-entry-audit-v1", "as_of": now.astimezone(CHICAGO).isoformat(),
            "affects_execution": False, "source": "live_autonomous_only", "current_build_id": build_id,
            "windows": windows,
            "limitations": ["Segments describe realized entries; they do not prove causation or validate a new threshold.",
                            "Missing line, quote, timing, or build evidence stays unverified; historical records are not backfilled.",
                            "Bootstrap resamples whole events; small and multiple segments remain exploratory."]}
