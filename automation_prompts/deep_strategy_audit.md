# Weekly deep sports strategy audit

Run in the local `polymaker` project once weekly.

1. Run `python sports_strategy_audit.py --scope weekly`.
2. Inspect `sports_weekly_strategy_audit.json`, the sports source files, recent logs, candidate registry, adaptive overrides, pricing analytics, execution quality, API health, and tests.
3. Run the relevant sports tests. Diagnose bugs, calibration drift, CLV deterioration, provider problems, execution slippage, selection bias, and narrow strategy segments that merit shadow/probation.
4. You may implement clear minor correctness, test, analytics, or observability fixes only when they do not increase live risk, change unit size, activate new APIs/sports, or materially change strategy behavior. Run the full test suite after any edit. Do not restart the live bot automatically.
5. Put material strategy/code changes, new APIs, risk increases, or live activation changes in a proposal for user approval. Never place, cancel, or alter a live order or position.
6. Write the concise findings and any verified changes to `sports_weekly_audit_findings.md`, including evidence, tests, and whether a controlled restart is required.
