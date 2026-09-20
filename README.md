# Polymaker

Local control board and automation services for the Sports and Crypto
bots. The dashboard includes live process health, bankroll analytics, strategy
controls, Central-time logs, and storage-retention monitoring.

## Safety

This repository intentionally excludes credentials, private keys, live account
state, portfolios, logs, model datasets, and analytics archives. Keep those
files local and never force-add them to Git.

The settings in `.github/test-fixtures/` contain dummy credentials and exist only
for automated tests. Do not use them as live bot configuration.

## Local verification

```powershell
python -m unittest discover -p "test_*.py"
```

## Dashboard

```powershell
.\start_dashboard.ps1
```

The default local dashboard address is `http://127.0.0.1:8765`.

## Git workflow

Enable the repository's local protection against accidental direct pushes to
`main` after cloning:

```powershell
git config core.hooksPath .githooks
```

Create a feature branch, push it, and merge through a pull request after the
`tests` GitHub Actions check passes.
