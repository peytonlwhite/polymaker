# Monthly major sports research audit

Run in the local `polymaker` project on the first day of each month.

1. Run `python sports_strategy_audit.py --scope monthly`.
2. Inspect `sports_monthly_strategy_audit.json`, all sports strategy/analytics modules, adaptive history, candidate outcomes, provider health, and the prior weekly findings.
3. Use current primary/official documentation when researching sportsbook, exchange, data-provider, or OpenAI capabilities. Analyze regime changes, walk-forward performance, calibration, price bands, sport/market/timing interactions, CLV, fees, fill quality, and alternative algorithms/APIs.
4. Run experiments and tests in shadow/read-only form. You may add research code, tests, or reports, but do not deploy material live strategy changes, increase risk, activate an API/sport, restart the live bot, or place/cancel an order.
5. Write `sports_monthly_research_findings.md` with ranked recommendations, expected benefit, evidence quality, implementation cost, risks, and a clear approval list.
