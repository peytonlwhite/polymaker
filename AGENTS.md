# Polymaker repository guidance

## Scope and safety

- Treat Sports, Crypto, and Weather execution as live-money systems unless the
  current task explicitly says otherwise.
- Never place, cancel, or modify a live order merely to test a code path.
- Do not weaken bankroll, exposure, liquidity, reconciliation, audit, or
  confirmation safeguards without explicit user approval.
- Preserve user-created changes and live runtime state. Do not reset, delete,
  or reconstruct state unless the task explicitly requires it.

## Private and generated data

- Never commit credentials, private keys, settings containing secrets, account
  state, portfolios, logs, event ledgers, model datasets, temporary files, or
  retained archives.
- Respect `.gitignore`; never use `git add -f` for ignored runtime files.
- Use sanitized example files when configuration documentation is needed.

## Implementation conventions

- Display and interpret application timestamps in `America/Chicago`, including
  the correct CST/CDT daylight-saving offset.
- Prefer small, targeted changes and preserve unrelated worktree edits.
- For bot restarts, inspect open positions first and verify process health,
  reconciliation, audit warnings, and recent error logs afterward.
- Keep recovery tooling compatible with compressed analytics and event archives.

## Validation

- Run focused tests while developing.
- Before publishing a behavioral change, run the complete suite:

  ```powershell
  python -m unittest discover -p "test_*.py"
  ```

- Report any failing test by name; do not hide, delete, or weaken a valid test
  simply to make CI pass.

## Git delivery

- Do not commit or push unless the user asks for publishing or delivery.
- Use a `codex/` feature branch and a pull request for bot behavior, execution,
  bankroll, security, recovery, storage, or substantial dashboard changes.
- Direct updates to `main` are reserved for explicitly approved trivial
  documentation-only changes.
- Keep the repository-local pre-push guard enabled with
  `git config core.hooksPath .githooks`.
- Make commits around coherent completed work, not every intermediate edit.
- Before staging, confirm that no ignored runtime or credential file is included.
