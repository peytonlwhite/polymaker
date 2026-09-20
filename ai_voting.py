"""Low-cost AI voting helpers shared by the crypto and sports bots.

Deterministic strategy and risk rules remain authoritative.  These helpers only
produce compact risk reviews and never place orders.
"""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


OPENAI_NANO_INPUT_USD_PER_MILLION = 0.20
OPENAI_NANO_OUTPUT_USD_PER_MILLION = 1.25


SPORTS_RISK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "veto": {"type": "boolean"},
        "reasons": {
            "type": "array",
            "items": {"type": "string", "maxLength": 120},
            "maxItems": 3,
        },
        "needs_live_search": {"type": "boolean"},
        "facts": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "maxLength": 40},
                    "status": {"type": "string", "maxLength": 120},
                    "source": {"type": "string", "maxLength": 80},
                    "freshness_minutes": {
                        "anyOf": [
                            {"type": "number", "minimum": 0},
                            {"type": "null"},
                        ]
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
                "required": [
                    "type",
                    "status",
                    "source",
                    "freshness_minutes",
                    "confidence",
                ],
            },
        },
        "source_notes": {"type": "string", "maxLength": 180},
    },
    "required": [
        "risk_level",
        "veto",
        "reasons",
        "needs_live_search",
        "facts",
        "source_notes",
    ],
}


def _json_request(url, method="GET", body=None, headers=None, timeout=30):
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=encoded, method=method, headers=headers or {})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"connection_error: {str(exc.reason)[:200]}") from exc


def _response_text(payload):
    if payload.get("output_text"):
        return str(payload["output_text"])
    chunks = []
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text") or content.get("content")
            if text:
                chunks.append(str(text))
    return "\n".join(chunks)


def parse_vote(content, provider="unknown", model=""):
    raw = str(content or "").strip()
    vote = {
        "enabled": True,
        "ok": False,
        "provider": provider,
        "model": model,
        "raw": raw[:2000],
        "risk_level": "unknown",
        "veto": False,
        "reasons": [],
        "needs_live_search": False,
        "facts": [],
        "source_notes": "",
    }
    if not raw:
        vote["error"] = "empty_response"
        return vote
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        risk_match = re.search(
            r'"risk_level"\s*:\s*"(low|medium|high|unknown)"',
            raw,
            re.IGNORECASE,
        )
        veto_match = re.search(
            r'"veto"\s*:\s*(true|false|"true"|"false")',
            raw,
            re.IGNORECASE,
        )
        if raw.startswith("{") and risk_match and veto_match:
            needs_search_match = re.search(
                r'"needs_live_search"\s*:\s*(true|false|"true"|"false")',
                raw,
                re.IGNORECASE,
            )
            vote.update({
                "ok": True,
                "partial": True,
                "error": "truncated_json_salvaged",
                "risk_level": risk_match.group(1).lower(),
                "veto": veto_match.group(1).strip('"').lower() == "true",
                "needs_live_search": bool(
                    needs_search_match
                    and needs_search_match.group(1).strip('"').lower() == "true"
                ),
            })
            return vote
        vote["error"] = "json_not_found"
        return vote
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        vote["error"] = "invalid_json"
        return vote
    if not isinstance(parsed, dict):
        vote["error"] = "json_not_object"
        return vote
    risk_level = str(parsed.get("risk_level") or "unknown").strip().lower()
    if risk_level not in {"low", "medium", "high", "unknown"}:
        risk_level = "unknown"
    veto_value = parsed.get("veto", False)
    if isinstance(veto_value, str):
        veto_value = veto_value.strip().lower() in {"1", "true", "yes", "y", "on"}
    reasons = parsed.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    needs_search = parsed.get("needs_live_search", False)
    if isinstance(needs_search, str):
        needs_search = needs_search.strip().lower() in {"1", "true", "yes", "y", "on"}
    parsed_facts = parsed.get("facts") or []
    if not isinstance(parsed_facts, list):
        parsed_facts = []
    facts = []
    for item in parsed_facts[:3]:
        if not isinstance(item, dict):
            continue
        freshness = item.get("freshness_minutes")
        try:
            freshness = max(0.0, float(freshness)) if freshness is not None else None
        except (TypeError, ValueError):
            freshness = None
        confidence = str(item.get("confidence") or "low").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
        facts.append(
            {
                "type": str(item.get("type") or "")[:40],
                "status": str(item.get("status") or "")[:120],
                "source": str(item.get("source") or "")[:80],
                "freshness_minutes": freshness,
                "confidence": confidence,
            }
        )
    vote.update({
        "ok": True,
        "parsed": parsed,
        "risk_level": risk_level,
        "veto": bool(veto_value),
        "reasons": [str(item)[:240] for item in reasons[:6]],
        "needs_live_search": bool(needs_search),
        "facts": facts,
        "source_notes": str(parsed.get("source_notes") or "")[:180],
    })
    return vote


def review_prompt(candidate, context, allow_live_search=False):
    evidence_rule = (
        "You may use web search for current game state or breaking news. Treat search snippets as provisional: "
        "cite the named source and freshness in facts, and do not invent missing inning/clock/set details. "
        if allow_live_search
        else "Only supplied fields count as facts; unsupported claims must trigger needs_live_search instead of being invented. "
    )
    return (
        "Review the candidate only for risk, contradictory context, stale inputs, or an obviously invalid edge. "
        "Do not estimate a new win probability, calculate a new stake, or approve a trade merely because a model score is high. "
        f"{evidence_rule}"
        "IMPORTANT TIMING RULE: minutes_since_start is elapsed real-world wall time since scheduled tipoff/start. "
        "It includes timeouts, fouls, free throws, reviews, breaks, delays, and other stoppages. Never compare it "
        "with regulation playing minutes and never infer the period, inning, game clock, or time remaining from it. "
        "Follow candidate.timing_semantics exactly for the sport's authoritative progress fields. Football uses quarter and game clock; "
        "basketball uses quarter/period and game clock; baseball uses inning/half/outs; hockey uses period and game clock; "
        "soccer uses half/match minute/stoppage; tennis uses set/game/point score and server; combat sports use round and round clock. "
        "Never demand a progress field that does not exist for that sport. If the correct sport-specific fields are absent, mark timing "
        "uncertainty or request live search; do not veto merely because wall-clock elapsed time exceeds normal game or match duration. "
        "A team that is not currently covering a spread is not a contradiction when authoritative time remaining is unknown. "
        "Field semantics: kalshi_liquidity=0 can mean the API omitted a liquidity estimate and is not itself a veto when "
        "the candidate has a positive-volume, tight executable bid/ask and fill-or-kill protection. book_line=null is normal "
        "for moneylines. favorite_rebound.ok=false only means an optional strategy did not trigger. kalshi_market_start=null "
        "is not a schedule defect when commence_time is present. Deterministic identity, date, side, quote, edge, fee, and "
        "sportsbook-family gates are enforced outside this review; do not second-guess them without a concrete contradictory fact. "
        "Return one compact JSON object with keys risk_level (low|medium|high), veto (boolean), "
        "reasons (at most three short strings), needs_live_search (boolean), facts (zero to three objects with "
        "type, status, source, freshness_minutes, confidence), and source_notes. "
        "Set needs_live_search only when "
        "current web/news/social information is material and absent from the supplied data. Never invent facts.\n\n"
        f"Context: {context}\nCandidate JSON: {json.dumps(candidate, default=str, sort_keys=True)[:8000]}"
    )


def ollama_vote(base_url, model, candidate, context, timeout=90, max_output_tokens=220):
    if not model:
        return {"enabled": True, "ok": False, "provider": "ollama", "error": "missing_model"}
    url = urljoin(str(base_url or "http://127.0.0.1:11434").rstrip("/") + "/", "api/chat")
    try:
        payload = _json_request(
            url,
            method="POST",
            body={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a cautious betting risk reviewer. Return JSON only."},
                    {"role": "user", "content": review_prompt(candidate, context)},
                ],
                "stream": False,
                "format": "json",
                "think": "low",
                "options": {"temperature": 0.1, "num_predict": max(256, int(max_output_tokens))},
            },
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        message = payload.get("message") or {}
        vote = parse_vote(message.get("content"), "ollama", model)
        vote["usage"] = {
            "input_tokens": payload.get("prompt_eval_count"),
            "output_tokens": payload.get("eval_count"),
            "duration_ms": round(float(payload.get("total_duration") or 0) / 1_000_000, 1),
            "cost_usd": 0.0,
        }
        return vote
    except Exception as exc:
        return {
            "enabled": True,
            "ok": False,
            "provider": "ollama",
            "model": model,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


def openai_vote(api_key, model, candidate, context, timeout=45, max_output_tokens=220, enable_web_search=False):
    if not api_key:
        return {"enabled": True, "ok": False, "provider": "openai", "error": "missing_api_key"}
    try:
        request_body = {
                "model": model or "gpt-5.4-nano",
                "input": [
                    {"role": "system", "content": "You are a cautious betting risk reviewer. Return JSON only."},
                    {"role": "user", "content": review_prompt(candidate, context, allow_live_search=enable_web_search)},
                ],
                # This is a compact classification task. Reasoning tokens can
                # exhaust the small output budget before the JSON is emitted.
                "reasoning": {"effort": "none"},
                "max_output_tokens": max(80, int(max_output_tokens)),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "sports_risk_vote",
                        "description": "A risk-only review of a sports market candidate.",
                        "strict": True,
                        "schema": SPORTS_RISK_SCHEMA,
                    }
                },
            }
        if enable_web_search:
            request_body["tools"] = [{"type": "web_search"}]
            request_body["tool_choice"] = "auto"
        payload = _json_request(
            "https://api.openai.com/v1/responses",
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            body=request_body,
            timeout=timeout,
        )
        vote = parse_vote(_response_text(payload), "openai", model or "gpt-5.4-nano")
        vote["web_search_enabled"] = bool(enable_web_search)
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        estimated_cost = (
            input_tokens * OPENAI_NANO_INPUT_USD_PER_MILLION
            + output_tokens * OPENAI_NANO_OUTPUT_USD_PER_MILLION
        ) / 1_000_000
        vote["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": usage.get("total_tokens"),
            "estimated_cost_usd": round(estimated_cost, 8),
        }
        return vote
    except Exception as exc:
        return {
            "enabled": True,
            "ok": False,
            "provider": "openai",
            "model": model or "gpt-5.4-nano",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


def votes_disagree(left, right):
    if not left.get("ok") or not right.get("ok"):
        return False
    return bool(left.get("veto")) != bool(right.get("veto")) or (
        left.get("risk_level") == "high"
    ) != (right.get("risk_level") == "high")


def ollama_health(base_url="http://127.0.0.1:11434", model=""):
    url = urljoin(str(base_url).rstrip("/") + "/", "api/tags")
    try:
        payload = _json_request(url, timeout=2)
        models = [str(row.get("name") or "") for row in (payload.get("models") or [])]
        installed = not model or any(name == model or name.startswith(f"{model}:") for name in models)
        return {
            "running": True,
            "healthy": installed,
            "model": model,
            "model_installed": installed,
            "available_models": models,
        }
    except Exception as exc:
        return {
            "running": False,
            "healthy": False,
            "model": model,
            "model_installed": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
