from copy import deepcopy
from datetime import datetime, timedelta, timezone
import http.client
import json
import threading
import unittest
from unittest.mock import Mock, patch

import dashboard
import crypto_funding as funding

NOW = datetime(2026, 9, 17, 16, tzinfo=timezone.utc)


def report():
    return {"generated_at": NOW.isoformat(), "top_candidates": [
        {"asset": "ETH", "market_lane": "crypto_15m", "exchange_index": 2}]}


class CryptoFundingTests(unittest.TestCase):
    def test_full_allocation_is_verified_without_claiming_cash_arrived(self):
        request = Mock(side_effect=[{"allocations": [{"exchange_index": 3, "percent": 100}]}, {}, funding.TARGET])
        saved = []
        result = funding.apply_full_crypto(request, lambda row: saved.append(deepcopy(row)), report(), NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(saved[0]["status"], "requesting")
        self.assertEqual(saved[-1]["status"], "allocation_verified")
        self.assertIn("pending", result["funding"]["message"])
        request.assert_any_call(funding.PATH, method="POST", body={"allocations": [{"exchange_index": 2, "percent": 100}]})
        self.assertEqual(len(request.call_args_list), 3)

    def test_existing_target_is_read_only(self):
        request = Mock(return_value=funding.TARGET)
        self.assertTrue(funding.apply_full_crypto(request, Mock(), report(), NOW)["ok"])
        self.assertTrue(all(not c.kwargs for c in request.call_args_list))

    def test_uncertain_write_reads_back_without_retry(self):
        request = Mock(side_effect=[{"allocations": []}, TimeoutError(), funding.TARGET])
        self.assertTrue(funding.apply_full_crypto(request, Mock(), report(), NOW)["ok"])
        self.assertEqual(sum(c.kwargs.get("method") == "POST" for c in request.call_args_list), 1)

    def test_failed_verification_cannot_report_success(self):
        request = Mock(side_effect=[{"allocations": []}, {}, TimeoutError()])
        result = funding.apply_full_crypto(request, Mock(), report(), NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["funding"]["status"], "verification_needed")

    def test_unverified_destination_prevents_any_mutation(self):
        for mutation in (lambda r: r.update(generated_at=(NOW-timedelta(minutes=10)).isoformat()),
                         lambda r: r["top_candidates"][0].update(exchange_index=3),
                         lambda r: r.update(top_candidates=[])):
            r = report(); mutation(r)
            request = Mock()
            with self.assertRaises(ValueError):
                funding.apply_full_crypto(request, Mock(), r, NOW)
            request.assert_not_called()

    def test_audit_failure_prevents_post(self):
        request = Mock(return_value={"allocations": []})
        with self.assertRaises(OSError):
            funding.apply_full_crypto(request, Mock(side_effect=OSError()), report(), NOW)
        request.assert_called_once_with(funding.PATH)

    def test_summary_reports_actual_cash_separately_from_target(self):
        result = funding.summary({"account": {"balance_raw": {"balance_dollars": "174.47",
                                  "balance_breakdown": [{"exchange_index": 2, "balance": "0.0000"}]}}},
                                 {"status": "allocation_verified"})
        self.assertEqual(result["account_cash"], 174.47)
        self.assertEqual(result["crypto_cash"], 0)
        self.assertTrue(result["full_crypto_target_verified"])
        self.assertIsNone(funding.dollars("NaN"))
        self.assertIsNone(funding.dollars(-1))

    def test_invalid_allocation_does_not_pass_as_full_crypto(self):
        for rows in ([], [{"exchange_index": 2, "percent": 50}],
                     [{"exchange_index": 2, "percent": 100}, {"exchange_index": 3, "percent": 50}],
                     [{"exchange_index": 2, "percent": 100}, {"exchange_index": 2, "percent": 0}]):
            self.assertFalse(funding.full_crypto({"allocations": rows}))

    def test_funding_endpoint_requires_explicit_same_origin_action(self):
        server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch.object(dashboard, "control_crypto_funding", return_value={"ok": True}) as control:
                for headers, expected in (({}, 400),
                    ({"X-Crypto-Funding-Action": "1", "Origin": "https://example.com"}, 400),
                    ({"X-Crypto-Funding-Action": "1"}, 200)):
                    conn = http.client.HTTPConnection(*server.server_address)
                    conn.request("POST", "/api/crypto/funding", json.dumps({"action": "full_crypto"}),
                                 {"Content-Type": "application/json", **headers})
                    response = conn.getresponse(); self.assertEqual(response.status, expected)
                    response.read(); conn.close()
                control.assert_called_once_with("full_crypto")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
