# Local API credentials

Real API keys belong on each user's computer, never in source code, commits,
shared analytics, or logs. The checked-in CI settings contain dummy values only.

## Set up crypto keys on Windows

From the repository root in an interactive PowerShell terminal, run:

```powershell
python configure_crypto_keys.py CRYPTO_OPENAI_API_KEY CRYPTO_GROK_API_KEY
```

Enter each value at its hidden prompt. Only key names appear in the command and
shell history. Blank input preserves an existing key. Nothing is saved if a
prompt is interrupted, and the command refuses terminals that would echo input.

The command stores credentials in `crypto_secrets.dpapi`, encrypted with Windows
DPAPI for the current Windows user. The crypto runtime already reads that store.
It is excluded from Git, as are local settings, environment files, and private
keys. Each person who clones the repository must configure their own credentials.
Copying another user's encrypted store is not a credential setup method.

Use `python configure_crypto_keys.py --help` for the supported key names. The
command accepts key names only, never values as command-line arguments. Run the
bots from the repository root so they find their normal local configuration.

Existing `CRYPTO_*` and `MULTI_MARKET_*` environment variables take precedence over
the encrypted store. Avoid keeping a stale override when changing a saved key.

## Other existing local settings

Sports and shared-provider credentials already load from environment variables
and the ignored `bot_settings.json`; they are not embedded in source code. That
JSON file is local plaintext, so keep it private and out of shared exports.
Kalshi also supports a local private-key file through `KALSHI_PRIVATE_KEY_PATH`.
Keep private keys outside the checkout, or use an ignored `.pem` or `.key` file.

The local crypto dashboard currently saves keys to ignored `crypto_settings.json`.
Use the encrypted setup command above for new or updated crypto keys; an existing
encrypted value takes precedence over a plaintext JSON value. This setup command
does not restart a bot, change trading controls, or modify live runtime state.
