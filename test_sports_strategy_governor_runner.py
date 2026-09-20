import unittest
from unittest.mock import patch

import sports_strategy_governor_runner as runner


class SportsStrategyGovernorRunnerTests(unittest.TestCase):
    def test_no_transition_uses_no_model_quota(self):
        with patch.object(runner, "run_governor", return_value={
            "generated_at": "now",
            "transitions": [],
            "active_overrides": [],
        }), patch.object(runner, "_json_request") as request:
            result = runner.run()
        request.assert_not_called()
        self.assertEqual("not_needed_no_transition", result["model_review"])


if __name__ == "__main__":
    unittest.main()
