"""Usage-bounded OpenAI model review for weekly and monthly sports audits."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ai_voting import _json_request, _response_text
from sports_adaptive_strategy import atomic_write_json
from sports_strategy_audit import build_packet


SETTINGS_FILE = Path("bot_settings.json")
WEEKLY_PACKET = Path("sports_weekly_strategy_audit.json")
MONTHLY_PACKET = Path("sports_monthly_strategy_audit.json")
WEEKLY_FINDINGS = Path("sports_weekly_audit_findings.md")
MONTHLY_FINDINGS = Path("sports_monthly_research_findings.md")
MODEL_AUDIT_STATE = Path("sports_model_audit_state.json")
AUDIT_MAX_ATTEMPTS = 3
AUDIT_RETRY_BASE_SECONDS = 3.0


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _configuration(scope):
    settings = read_json(SETTINGS_FILE, {})
    if scope == "monthly":
        return {
            "api_key": os.getenv("SPORTS_OPENAI_API_KEY") or str(settings.get("SPORTS_OPENAI_API_KEY") or ""),
            "model": str(settings.get("SPORTS_AUTOMATION_MONTHLY_MODEL") or "gpt-5.6-sol"),
            "reasoning_effort": str(settings.get("SPORTS_AUTOMATION_MONTHLY_REASONING") or "xhigh"),
            "max_output_tokens": int(float(settings.get("SPORTS_AUTOMATION_MONTHLY_MAX_OUTPUT_TOKENS") or 7000)),
            "web_search": str(settings.get("SPORTS_AUTOMATION_MONTHLY_WEB_SEARCH") or "true").lower() == "true",
        }
    return {
        "api_key": os.getenv("SPORTS_OPENAI_API_KEY") or str(settings.get("SPORTS_OPENAI_API_KEY") or ""),
        "model": str(settings.get("SPORTS_AUTOMATION_WEEKLY_MODEL") or "gpt-5.6-sol"),
        "reasoning_effort": str(settings.get("SPORTS_AUTOMATION_WEEKLY_REASONING") or "high"),
        "max_output_tokens": int(float(settings.get("SPORTS_AUTOMATION_WEEKLY_MAX_OUTPUT_TOKENS") or 5000)),
        "web_search": False,
    }


def _prompt(scope, packet):
    if scope == "monthly":
        role = (
            "Perform a deep monthly research audit. Identify regime changes, calibration or price-quality problems, "
            "execution weaknesses, alternative algorithms, and useful current APIs. Use primary or official sources "
            "for current external claims and include their links."
        )
        output = (
            "Return Markdown with: Executive verdict; performance/regime evidence; calibration and execution findings; "
            "ranked research or API opportunities; experiments; approval-required changes; and next-month monitoring plan."
        )
    else:
        role = (
            "Perform a weekly deep sports strategy audit. Find actionable bugs, data-health issues, calibration drift, "
            "CLV deterioration, selection bias, execution slippage, and narrow segments that warrant probation or shadow."
        )
        output = (
            "Return Markdown with: Executive verdict; system health; performance evidence; adaptive-segment review; "
            "bugs or data risks; ranked recommendations; and approval-required items."
        )
    return (
        f"{role}\n\n"
        "The checked-in baseline and high-volume approach are intentional. Never recommend increasing risk merely to "
        "recover losses. The automation may reduce a narrow segment or restore it only to baseline; new APIs/sports, "
        "risk increases, live order changes, process restarts, and material live code changes require user approval. "
        "Distinguish independent games from correlated tickets and use after-fee results. Do not invent missing data.\n\n"
        f"{output}\n\nSanitized audit packet JSON:\n{json.dumps(packet, sort_keys=True, default=str)}"
    )


def _request(scope, packet, config):
    body = {
        "model": config["model"],
        "input": [
            {
                "role": "system",
                "content": (
                    "You are an evidence-first sports strategy and software reliability auditor. "
                    "You analyze and recommend; you cannot place trades or change live state."
                ),
            },
            {"role": "user", "content": _prompt(scope, packet)},
        ],
        "reasoning": {"effort": config["reasoning_effort"]},
        "max_output_tokens": config["max_output_tokens"],
    }
    if config["web_search"]:
        body["tools"] = [{"type": "web_search"}]
        body["tool_choice"] = "auto"
    return _json_request(
        "https://api.openai.com/v1/responses",
        method="POST",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        body=body,
        timeout=300,
    )


def _reduced_reasoning_effort(effort, attempt):
    levels = ["low", "medium", "high", "xhigh"]
    try:
        index = levels.index(str(effort or "high").lower())
    except ValueError:
        index = 2
    return levels[max(0, index - max(0, int(attempt) - 1))]


def _retry_delay_seconds(error, attempt):
    match = re.search(
        r"(?:retry(?:\s+after|\s+in)?)[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*s",
        str(error or ""),
        flags=re.I,
    )
    if match:
        return min(30.0, max(0.25, float(match.group(1))))
    return min(30.0, AUDIT_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))


def _request_with_retries(scope, packet, config, *, sleep=time.sleep):
    attempts = []
    last_error = ""
    for attempt in range(1, AUDIT_MAX_ATTEMPTS + 1):
        attempt_config = dict(config)
        attempt_config["reasoning_effort"] = _reduced_reasoning_effort(
            config.get("reasoning_effort"), attempt
        )
        try:
            response = _request(scope, packet, attempt_config)
            findings = _response_text(response).strip()
            status = str(response.get("status") or "unknown")
            incomplete = response.get("incomplete_details") or {}
            attempts.append({
                "attempt": attempt,
                "reasoning_effort": attempt_config["reasoning_effort"],
                "status": status,
                "response_id": response.get("id"),
                "incomplete_details": incomplete,
                "output_text": bool(findings),
            })
            if findings:
                return response, findings, attempts
            last_error = (
                f"OpenAI audit returned no text status={status} "
                f"incomplete_details={json.dumps(incomplete, sort_keys=True)}"
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            attempts.append({
                "attempt": attempt,
                "reasoning_effort": attempt_config["reasoning_effort"],
                "error": last_error[:1000],
            })
            retryable = any(
                token in str(exc).lower()
                for token in (
                    "http 408", "http 409", "http 429", "http 500",
                    "http 502", "http 503", "http 504", "connection_error",
                    "timed out", "timeout",
                )
            )
            if not retryable:
                raise
        if attempt < AUDIT_MAX_ATTEMPTS:
            delay = _retry_delay_seconds(last_error, attempt)
            sleep(delay + random.uniform(0.0, min(1.0, delay * 0.2)))
    raise RuntimeError(last_error or "OpenAI audit failed after retries")


def _record_run_state(payload):
    state = read_json(MODEL_AUDIT_STATE, {})
    state.setdefault("runs", []).append(payload)
    state["runs"] = state["runs"][-100:]
    atomic_write_json(MODEL_AUDIT_STATE, state)


def run(scope="weekly", *, execute=False):
    config = _configuration(scope)
    packet = build_packet(scope)
    packet_path = MONTHLY_PACKET if scope == "monthly" else WEEKLY_PACKET
    findings_path = MONTHLY_FINDINGS if scope == "monthly" else WEEKLY_FINDINGS
    atomic_write_json(packet_path, packet)
    validation = {
        "ok": bool(config["api_key"] and config["model"] and config["reasoning_effort"]),
        "scope": scope,
        "mode": "run" if execute else "dry_run",
        "model": config["model"],
        "reasoning_effort": config["reasoning_effort"],
        "web_search": config["web_search"],
        "packet": str(packet_path),
        "packet_bytes": packet_path.stat().st_size,
        "findings": str(findings_path),
    }
    if not execute:
        return validation
    if not config["api_key"]:
        raise RuntimeError("SPORTS_OPENAI_API_KEY is not configured")
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        response, findings, attempts = _request_with_retries(scope, packet, config)
    except Exception as exc:
        _record_run_state({
            "at": started_at,
            "scope": scope,
            "model": config["model"],
            "reasoning_effort": config["reasoning_effort"],
            "web_search": config["web_search"],
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}"[:1000],
            "packet_bytes": validation["packet_bytes"],
        })
        raise
    header = (
        f"<!-- generated {datetime.now().astimezone().isoformat(timespec='seconds')} "
        f"model={config['model']} effort={config['reasoning_effort']} -->\n\n"
    )
    atomic_write_text(findings_path, header + findings)
    usage = response.get("usage") or {}
    _record_run_state({
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": scope,
        "model": config["model"],
        "reasoning_effort": config["reasoning_effort"],
        "web_search": config["web_search"],
        "usage": usage,
        "response_id": response.get("id"),
        "findings": str(findings_path),
        "status": "completed",
        "attempts": attempts,
        "packet_bytes": validation["packet_bytes"],
    })
    return {
        **validation,
        "response_id": response.get("id"),
        "usage": usage,
        "attempts": attempts,
        "output_bytes": len(findings.encode("utf-8")),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("weekly", "monthly"), default="weekly")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.scope, execute=args.run), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "scope": args.scope, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}, sort_keys=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
