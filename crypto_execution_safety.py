"""Read-only API pagination and pure Crypto execution evidence checks."""

import math
from datetime import datetime, timezone
from urllib.parse import urlencode


def complete_pages(request, endpoint, field, *, params=None, maximum_pages=100):
    """Return a complete collection or raise; a partial page is never success."""
    values, seen = [], set()
    query = dict(params or {})
    for _ in range(maximum_pages):
        payload, _headers = request(endpoint + ("?" + urlencode(query) if query else ""))
        if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
            raise ValueError("invalid_portfolio_collection:" + field)
        values.extend(payload[field])
        cursor = payload.get("cursor")
        if not cursor:
            return values
        if cursor in seen:
            raise ValueError("repeated_portfolio_cursor:" + field)
        seen.add(cursor)
        query["cursor"] = cursor
    raise ValueError("incomplete_portfolio_pagination:" + field)


def shard_cash(account, exchange_index):
    """Unknown/missing shard is not total account cash. Breakdown is in dollars."""
    if isinstance(exchange_index, bool) or exchange_index is None:
        return None
    try:
        index = int(exchange_index)
        if index < 0 or str(exchange_index) != str(index):
            return None
    except (ValueError, TypeError):
        return None
    rows = (account.get("balance_raw") or {}).get("balance_breakdown")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if str(row.get("exchange_index")) == str(index):
            try:
                value = float(row.get("balance"))
                return max(0.0, value) if math.isfinite(value) else None
            except (TypeError, ValueError):
                return None
    return None


def confirmation_age(confirmation, now=None):
    current = now or datetime.now(timezone.utc)
    try:
        fetched = datetime.fromisoformat(str(confirmation.get("fetched_at") or "").replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            return None
        age = (current - fetched).total_seconds()
        return age if math.isfinite(age) and age >= -0.25 else None
    except (ValueError, TypeError):
        return None


def confirmation_reasons(confirmation, maximum_age=2.0, now=None):
    age = confirmation_age(confirmation, now)
    reasons = []
    if age is None:
        reasons.append("confirmation_timestamp_unavailable")
    elif age > maximum_age:
        reasons.append("confirmation_quote_stale")
    if confirmation.get("sequence_valid") is False:
        reasons.append("confirmation_sequence_invalid")
    return reasons
