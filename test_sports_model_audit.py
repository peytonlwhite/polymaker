import unittest
from unittest.mock import patch

import sports_model_audit as audit


class SportsModelAuditTests(unittest.TestCase):
    def test_dry_run_does_not_call_api(self):
        with patch.object(audit, "build_packet", return_value={"scope": "weekly"}), patch.object(
            audit, "atomic_write_json"
        ), patch.object(audit, "_configuration", return_value={
            "api_key": "configured",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "max_output_tokens": 1000,
            "web_search": False,
        }), patch.object(audit, "_request") as request, patch.object(
            audit.Path, "stat", return_value=type("Stat", (), {"st_size": 100})()
        ):
            result = audit.run("weekly", execute=False)
        request.assert_not_called()
        self.assertEqual("dry_run", result["mode"])

    def test_weekly_and_monthly_model_routing(self):
        settings = {
            "SPORTS_OPENAI_API_KEY": "configured",
            "SPORTS_AUTOMATION_WEEKLY_MODEL": "gpt-5.6-sol",
            "SPORTS_AUTOMATION_WEEKLY_REASONING": "high",
            "SPORTS_AUTOMATION_MONTHLY_MODEL": "gpt-5.6-sol",
            "SPORTS_AUTOMATION_MONTHLY_REASONING": "xhigh",
        }
        with patch.object(audit, "read_json", return_value=settings):
            weekly = audit._configuration("weekly")
            monthly = audit._configuration("monthly")
        self.assertEqual("high", weekly["reasoning_effort"])
        self.assertEqual("xhigh", monthly["reasoning_effort"])
        self.assertFalse(weekly["web_search"])
        self.assertTrue(monthly["web_search"])

    def test_request_retries_empty_output_with_lower_reasoning(self):
        responses = [
            {"id": "first", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            {"id": "second", "status": "completed", "output_text": "Useful findings"},
        ]
        config = {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "max_output_tokens": 1000,
            "web_search": False,
            "api_key": "configured",
        }
        with patch.object(audit, "_request", side_effect=responses) as request:
            response, findings, attempts = audit._request_with_retries(
                "weekly", {"scope": "weekly"}, config, sleep=lambda _seconds: None
            )
        self.assertEqual("second", response["id"])
        self.assertEqual("Useful findings", findings)
        self.assertEqual(2, len(attempts))
        self.assertEqual("xhigh", request.call_args_list[0].args[2]["reasoning_effort"])
        self.assertEqual("high", request.call_args_list[1].args[2]["reasoning_effort"])

    def test_retry_delay_honors_provider_hint(self):
        self.assertEqual(2.93, audit._retry_delay_seconds("retry after 2.93s", 1))


if __name__ == "__main__":
    unittest.main()
