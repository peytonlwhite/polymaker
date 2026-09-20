import json
from pathlib import Path

from kalshi_common import KALSHI_BASE_URL, KALSHI_ORDER_PATH, kalshi_event_order_body, kalshi_private_request, write_json


SETTINGS_FILE = Path("bot_settings.json")
REPORT_FILE = Path("sports_live_preflight.json")


def load_settings():
    if not SETTINGS_FILE.exists():
        return {}
    return json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))


def main():
    settings = load_settings()
    api_key = settings.get("KALSHI_API_KEY", "")
    private_key_path = settings.get("KALSHI_PRIVATE_KEY_PATH", "")
    private_key_pem = settings.get("KALSHI_API_SECRET", "")

    report = {
        "ok": False,
        "base_url": KALSHI_BASE_URL,
        "order_path": KALSHI_ORDER_PATH,
        "checks": {
            "api_key_saved": bool(api_key),
            "private_key_saved": bool(private_key_path or private_key_pem),
        },
        "sample_order_body_no_submit": None,
    }

    try:
        report["sample_order_body_no_submit"] = kalshi_event_order_body(
            ticker="EXAMPLE-TICKER",
            order_side="yes",
            price_cents=50,
            count=1,
            client_order_id="preflight-no-submit",
        )
    except Exception as exc:
        report["sample_order_body_error"] = str(exc)

    if not api_key or not (private_key_path or private_key_pem):
        report["error"] = "missing_kalshi_credentials"
        write_json(REPORT_FILE, report)
        print(json.dumps(report, indent=2))
        return

    try:
        balance, _headers = kalshi_private_request(
            "/portfolio/balance",
            api_key_id=api_key,
            private_key_path=private_key_path,
            private_key_pem=private_key_pem,
        )
        report["balance_ok"] = True
        report["balance_response"] = balance
    except Exception as exc:
        report["balance_ok"] = False
        report["balance_error"] = str(exc)

    try:
        positions, _headers = kalshi_private_request(
            "/portfolio/positions",
            api_key_id=api_key,
            private_key_path=private_key_path,
            private_key_pem=private_key_pem,
        )
        report["positions_ok"] = True
        report["positions_count"] = len(positions.get("market_positions") or positions.get("positions") or [])
    except Exception as exc:
        report["positions_ok"] = False
        report["positions_error"] = str(exc)

    report["ok"] = bool(report.get("balance_ok") and report.get("positions_ok"))
    write_json(REPORT_FILE, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
