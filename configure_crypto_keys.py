"""Prompt for crypto API keys and save them to this Windows user's encrypted store."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import sys
import warnings

from secure_settings import read_secure_settings, write_secure_settings


STORE = Path(__file__).resolve().parent / "crypto_secrets.dpapi"
KEY_NAMES = (
    "CRYPTO_OPENAI_API_KEY",
    "CRYPTO_GROK_API_KEY",
    "CRYPTO_NEWS_API_KEY",
    "CRYPTO_CRYPTOPANIC_API_KEY",
    "MULTI_MARKET_PYTH_API_KEY",
    "MULTI_MARKET_TWELVE_DATA_API_KEY",
    "MULTI_MARKET_ALPHA_VANTAGE_API_KEY",
    "KALSHI_API_KEY",
)


def hidden_prompt(name):
    # getpass must never fall back to echoing a credential in an unsuitable terminal.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(f"{name} (blank keeps the saved value): ")


def configure(names, *, store=STORE, prompt=hidden_prompt):
    names = tuple(dict.fromkeys(names))
    if any(name not in KEY_NAMES for name in names):
        raise ValueError("Unsupported crypto API key name")
    current = read_secure_settings(store, {})
    updated = dict(current)
    saved = []
    for name in names:
        value = prompt(name).strip()
        if value and value != current.get(name):
            updated[name] = value
            saved.append(name)
    if saved:
        write_secure_settings(store, updated)
    return saved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="+", choices=KEY_NAMES, help="Key names only; values are entered at hidden prompts")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("Encrypted local storage requires Windows")
    if not sys.stdin.isatty():
        parser.error("Run this command in an interactive terminal; do not pipe key values")
    try:
        saved = configure(args.names)
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        print("Canceled: hidden input was unavailable or interrupted. No keys were saved.", file=sys.stderr)
        return 1
    if saved:
        print(f"Saved {len(saved)} key(s) to the encrypted local store: {STORE}")
    else:
        print("No changes to the encrypted local store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
