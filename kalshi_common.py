import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import base64
import shutil
import unicodedata
from datetime import datetime
import sys
from pathlib import Path


KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_ORDER_PATH = os.getenv("KALSHI_ORDER_PATH", "/portfolio/events/orders")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def write_json(path, data):
    target = Path(path)
    payload = json.dumps(data, indent=2, sort_keys=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    lastgood = target.with_name(f"{target.name}.lastgood")
    critical = (
        target.name.endswith("_portfolio.json")
        or target.name.endswith("_reconciliation.json")
        or target.name.endswith("_order_intents.json")
        or target.name in {
            "bot_settings.json", "sports_capper_tickets.json",
            "sports_odds_budget.json", "sports_odds_key_state.json",
        }
    )

    def valid_json_file(candidate):
        try:
            text = candidate.read_text(encoding="utf-8-sig")
            return bool(text.strip()) and json.loads(text) is not None
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False

    def durable_write(candidate, text):
        with candidate.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

    last_error = None
    for attempt in range(3):
        try:
            durable_write(tmp, payload)
            if not valid_json_file(tmp):
                raise OSError(f"Refusing to install invalid JSON temp file for {target}")
            if critical and valid_json_file(target):
                backup_tmp = lastgood.with_name(f".{lastgood.name}.{os.getpid()}.tmp")
                shutil.copy2(target, backup_tmp)
                with backup_tmp.open("r+b") as handle:
                    os.fsync(handle.fileno())
                backup_tmp.replace(lastgood)
            os.replace(tmp, target)
            if critical and not valid_json_file(lastgood):
                backup_tmp = lastgood.with_name(f".{lastgood.name}.{os.getpid()}.tmp")
                shutil.copy2(target, backup_tmp)
                with backup_tmp.open("r+b") as handle:
                    os.fsync(handle.fileno())
                backup_tmp.replace(lastgood)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.15 * (attempt + 1))
    raise last_error


def append_jsonl(path, event):
    line = json.dumps({"ts": now_iso(), **event}, sort_keys=True) + "\n"
    try:
        from storage_maintenance import maybe_rotate_for_append
        maybe_rotate_for_append(path, len(line.encode("utf-8")))
    except (ImportError, OSError):
        pass
    with Path(path).open("a", encoding="utf-8") as file:
        file.write(line)


def log_line(path, message):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_line = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_line, flush=True)
    try:
        from storage_maintenance import maybe_rotate_for_append
        maybe_rotate_for_append(path, len((line + "\n").encode("utf-8")))
    except (ImportError, OSError):
        pass
    with Path(path).open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def get_json(url, params=None, headers=None, method=None, body=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = response.read().decode("utf-8")
            return json.loads(body), dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc


def kalshi_authenticated_headers(method, path, api_key_id, private_key_path="", private_key_pem=""):
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise RuntimeError("cryptography package is required for Kalshi live trading. Run: pip install cryptography") from exc

    key_bytes = b""
    pem_text = str(private_key_pem or "").strip()
    key_path = Path(str(private_key_path or "").strip()) if private_key_path else None
    if key_path and key_path.exists():
        key_bytes = key_path.read_bytes()
    elif pem_text and "BEGIN" in pem_text and "PRIVATE KEY" in pem_text:
        key_bytes = pem_text.encode("utf-8")
    elif pem_text and Path(pem_text).exists():
        key_bytes = Path(pem_text).read_bytes()
    elif key_path:
        raise RuntimeError(f"Kalshi private key file not found: {key_path}")
    elif pem_text:
        raise RuntimeError("Kalshi private key value is not a PEM key or readable key-file path.")
    else:
        raise RuntimeError("Kalshi private key missing. Set KALSHI_PRIVATE_KEY_PATH or KALSHI_API_SECRET.")

    private_key = serialization.load_pem_private_key(key_bytes, password=None, backend=default_backend())
    timestamp = str(int(time.time() * 1000))
    sign_path = urllib.parse.urlparse(KALSHI_BASE_URL + path).path.split("?")[0]
    message = f"{timestamp}{method.upper()}{sign_path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
    }


def kalshi_private_request(path, method="GET", body=None, api_key_id="", private_key_path="", private_key_pem=""):
    if not api_key_id:
        raise RuntimeError("KALSHI_API_KEY missing")
    headers = kalshi_authenticated_headers(method, path, api_key_id, private_key_path, private_key_pem)
    if body is not None:
        headers["Content-Type"] = "application/json"
    return get_json(f"{KALSHI_BASE_URL}{path}", headers=headers, method=method, body=body)


def kalshi_event_order_body(ticker, order_side, price_cents, count, client_order_id, time_in_force="immediate_or_cancel"):
    side = str(order_side or "yes").strip().lower()
    price_cents = int(round(float(price_cents or 0)))
    count = int(count)
    if not ticker:
        raise ValueError("ticker_required")
    if price_cents <= 0 or price_cents >= 100:
        raise ValueError("price_cents_must_be_1_to_99")
    if count < 1:
        raise ValueError("count_must_be_positive")

    if side in {"yes", "y", "bid"}:
        book_side = "bid"
        yes_price_cents = price_cents
    elif side in {"no", "n", "ask"}:
        book_side = "ask"
        yes_price_cents = 100 - price_cents
    else:
        raise ValueError("order_side_must_be_yes_or_no")

    return {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": book_side,
        "count": f"{count:.2f}",
        "price": f"{yes_price_cents / 100.0:.4f}",
        "time_in_force": time_in_force,
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": True,
        "reduce_only": False,
    }


def fetch_kalshi_markets(status="open", limit=1000, max_pages=20, extra_params=None, sleep_seconds=0.15):
    markets = []
    cursor = None
    params = {"status": status, "limit": limit}
    if extra_params:
        params.update(extra_params)

    for _ in range(max_pages):
        if cursor:
            params["cursor"] = cursor
        last_error = None
        for attempt in range(3):
            try:
                data, _headers = get_json(f"{KALSHI_BASE_URL}/markets", params=params)
                break
            except RuntimeError as exc:
                last_error = exc
                if "HTTP 429" not in str(exc):
                    raise
                time.sleep(float(os.getenv("KALSHI_429_SLEEP_SECONDS", "1.5")) * (attempt + 1))
        else:
            raise last_error
        batch = data.get("markets", [])
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(sleep_seconds)

    return markets


def cents_from_market(market, field):
    if field in market and market[field] is not None:
        return float(market[field])

    dollar_field = f"{field}_dollars"
    if dollar_field in market and market[dollar_field] not in (None, ""):
        return round(float(market[dollar_field]) * 100, 2)

    return 0.0


def market_prices(market):
    yes_bid = cents_from_market(market, "yes_bid")
    yes_ask = cents_from_market(market, "yes_ask")
    no_bid = cents_from_market(market, "no_bid")
    no_ask = cents_from_market(market, "no_ask")

    if no_bid <= 0 and yes_ask > 0:
        no_bid = max(0.0, 100.0 - yes_ask)
    if no_ask <= 0 and yes_bid > 0:
        no_ask = max(0.0, 100.0 - yes_bid)

    return {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask}


def market_volume(market):
    for field in ("volume", "volume_24h", "open_interest"):
        if field in market and market[field] not in (None, ""):
            return float(market[field])
    for field in ("volume_fp", "volume_24h_fp", "open_interest_fp"):
        if field in market and market[field] not in (None, ""):
            return float(market[field])
    return 0.0


def market_liquidity(market):
    for field in ("liquidity", "liquidity_dollars"):
        if field in market and market[field] not in (None, ""):
            value = float(market[field])
            return value if field.endswith("_dollars") else value / 100.0
    return 0.0


def american_to_prob(odds):
    odds = int(odds)
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def prob_to_price(probability):
    return round(max(0.0, min(1.0, probability)) * 100, 2)


def no_vig_two_way(prob_a, prob_b):
    total = prob_a + prob_b
    if total <= 0:
        return 0.0, 0.0
    return prob_a / total, prob_b / total


def normalize_text(value):
    value = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", str(value))
        if not unicodedata.combining(ch)
    )
    return " ".join(
        "".join(ch.lower() if ch.isalnum() else " " for ch in value).split()
    )
