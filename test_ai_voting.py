import unittest
from unittest.mock import patch

from ai_voting import SPORTS_RISK_SCHEMA, openai_vote, parse_vote, votes_disagree


class AiVotingTests(unittest.TestCase):
    def test_parse_compact_vote(self):
        vote = parse_vote(
            '{"risk_level":"medium","veto":false,"reasons":["thin context"],"needs_live_search":true}',
            "openai",
            "gpt-5.4-nano",
        )
        self.assertTrue(vote["ok"])
        self.assertFalse(vote["veto"])
        self.assertTrue(vote["needs_live_search"])
        self.assertEqual(vote["provider"], "openai")

    def test_local_cloud_disagreement_is_detected(self):
        self.assertTrue(votes_disagree(
            {"ok": True, "veto": True, "risk_level": "high"},
            {"ok": True, "veto": False, "risk_level": "low"},
        ))

    def test_invalid_response_fails_open_to_deterministic_rules(self):
        vote = parse_vote("not json", "ollama", "gpt-oss:20b")
        self.assertFalse(vote["ok"])
        self.assertFalse(vote["veto"])

    def test_truncated_json_preserves_explicit_veto(self):
        vote = parse_vote(
            '{"risk_level":"high","veto":true,"reasons":["missing live inning context"',
            "openai",
            "gpt-5.4-nano",
        )
        self.assertTrue(vote["ok"])
        self.assertTrue(vote["partial"])
        self.assertEqual(vote["error"], "truncated_json_salvaged")
        self.assertEqual(vote["risk_level"], "high")
        self.assertTrue(vote["veto"])

    def test_structured_facts_are_bounded_and_sanitized(self):
        vote = parse_vote(
            """{
              "risk_level": "medium",
              "veto": false,
              "reasons": ["lineup not confirmed"],
              "needs_live_search": true,
              "facts": [{
                "type": "lineup",
                "status": "unconfirmed",
                "source": "candidate",
                "freshness_minutes": "4.5",
                "confidence": "medium"
              }],
              "source_notes": "No outside source supplied"
            }""",
            "openai",
            "gpt-5.4-nano",
        )
        self.assertTrue(vote["ok"])
        self.assertEqual(vote["facts"][0]["freshness_minutes"], 4.5)
        self.assertEqual(vote["source_notes"], "No outside source supplied")

    def test_openai_schema_forbids_unstructured_extra_fields(self):
        self.assertFalse(SPORTS_RISK_SCHEMA["additionalProperties"])
        self.assertIn("facts", SPORTS_RISK_SCHEMA["required"])
        self.assertIn("source_notes", SPORTS_RISK_SCHEMA["required"])

    def test_openai_search_escalation_enables_web_tool(self):
        response = {
            "output_text": '{"risk_level":"low","veto":false,"reasons":[],"needs_live_search":false,"facts":[],"source_notes":"score verified"}',
            "usage": {},
        }
        with patch("ai_voting._json_request", return_value=response) as request:
            vote = openai_vote(
                "test-key",
                "gpt-5.4-nano",
                {"game": "Away at Home"},
                "live sports state",
                enable_web_search=True,
            )
        body = request.call_args.kwargs["body"]
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(body["tool_choice"], "auto")
        self.assertTrue(vote["web_search_enabled"])


if __name__ == "__main__":
    unittest.main()
