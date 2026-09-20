# Sports strategy governor

Run in the local `polymaker` project three times daily.

1. Run `python sports_strategy_governor.py --apply`.
2. Read `sports_strategy_governor_report.json` and `sports_operations_health.json`.
3. Verify the governor changed only `sports_adaptive_overrides.json` and its report/backup files.
4. Report transitions, evidence, current shadow/probation segments, and health warnings concisely.
5. Do not edit source code or `bot_settings.json`, restart a process, place/cancel an order, increase risk, activate an API, or enable a sport. If the deterministic command fails or proposes something outside these boundaries, make no strategy change and report the failure.

The baseline strategy and high-volume approach are intentional. The governor may reduce a narrow segment, move it to shadow, return it to one-unit probation, or restore it only to its checked-in baseline after its evidence thresholds pass.
