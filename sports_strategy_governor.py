"""Evaluate and optionally apply bounded sports strategy overrides."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sports_adaptive_strategy import (
    atomic_write_json,
    evaluate_state,
    load_state,
    public_summary,
)


PORTFOLIO_FILE = Path("sports_paper_portfolio.json")
CANDIDATE_REGISTRY_FILE = Path("sports_candidate_registry.json")
STATE_FILE = Path("sports_adaptive_overrides.json")
REPORT_FILE = Path("sports_strategy_governor_report.json")
SETTINGS_FILE = Path("bot_settings.json")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def run(*, apply=False, generated_at=None):
    portfolio = read_json(PORTFOLIO_FILE, {})
    registry = read_json(CANDIDATE_REGISTRY_FILE, {})
    previous = load_state(STATE_FILE)
    settings = read_json(SETTINGS_FILE, {})
    forced_active_segments = sorted({
        value.strip()
        for value in str(settings.get("SPORTS_ADAPTIVE_FORCE_ACTIVE_SEGMENTS") or "").split(",")
        if value.strip()
    })
    evaluated, transitions = evaluate_state(
        portfolio,
        registry,
        previous,
        generated_at=generated_at,
        policy_overrides={"forced_active_segments": forced_active_segments},
    )
    report = {
        **public_summary(evaluated, limit=100),
        "mode": "apply" if apply else "dry_run",
        "transitions": transitions,
        "baseline_protected": True,
        "risk_increases_allowed": False,
        "user_authorized_forced_active_segments": forced_active_segments,
    }
    if apply:
        atomic_write_json(STATE_FILE, evaluated)
        atomic_write_json(REPORT_FILE, report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--generated-at")
    args = parser.parse_args()
    print(json.dumps(run(apply=args.apply, generated_at=args.generated_at), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
