"""Scheduled governor entrypoint with model review only on state transitions."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ai_voting import _json_request, _response_text
from sports_model_audit import atomic_write_text
from sports_strategy_governor import run as run_governor


SETTINGS_FILE = Path("bot_settings.json")
FINDINGS_FILE = Path("sports_strategy_governor_findings.md")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def run():
    governor = run_governor(apply=True)
    transitions = governor.get("transitions") or []
    result = {
        "ok": True,
        "generated_at": governor.get("generated_at"),
        "transition_count": len(transitions),
        "active_override_count": len(governor.get("active_overrides") or []),
        "model_review": "not_needed_no_transition",
    }
    if not transitions:
        return result
    settings = read_json(SETTINGS_FILE, {})
    api_key = os.getenv("SPORTS_OPENAI_API_KEY") or str(settings.get("SPORTS_OPENAI_API_KEY") or "")
    if not api_key:
        return {**result, "model_review": "missing_api_key"}
    model = str(settings.get("SPORTS_AUTOMATION_GOVERNOR_MODEL") or "gpt-5.6-terra")
    effort = str(settings.get("SPORTS_AUTOMATION_GOVERNOR_REASONING") or "medium")
    prompt = (
        "Review these deterministic sports-strategy state transitions for consistency with their supplied evidence. "
        "The changes have already been bounded to shadow, one-unit probation, or restoration to the checked-in baseline. "
        "Do not propose an exposure increase, order action, new API activation, or loss-chasing. Return concise Markdown "
        "with validity assessment, evidence caveats, monitoring requirements, and any item needing user review.\n\n"
        + json.dumps({
            "transitions": transitions,
            "active_overrides": governor.get("active_overrides") or [],
            "guardrails": {
                "risk_increases_allowed": False,
                "restore_above_baseline_allowed": False,
            },
        }, sort_keys=True)
    )
    response = _json_request(
        "https://api.openai.com/v1/responses",
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        body={
            "model": model,
            "input": [
                {"role": "system", "content": "You are a cautious sports strategy control reviewer."},
                {"role": "user", "content": prompt},
            ],
            "reasoning": {"effort": effort},
            "max_output_tokens": 1800,
        },
        timeout=180,
    )
    text = _response_text(response).strip()
    if text:
        atomic_write_text(
            FINDINGS_FILE,
            (
                f"<!-- generated {datetime.now().astimezone().isoformat(timespec='seconds')} "
                f"model={model} effort={effort} -->\n\n{text}"
            ),
        )
    return {
        **result,
        "model_review": "completed" if text else "empty_response",
        "model": model,
        "reasoning_effort": effort,
        "usage": response.get("usage") or {},
    }


if __name__ == "__main__":
    try:
        print(json.dumps(run(), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}, sort_keys=True))
        raise SystemExit(1)

