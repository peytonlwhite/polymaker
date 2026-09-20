"""Credential setup must preserve secrets and never echo them."""
import getpass
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings

import configure_crypto_keys as setup
from secure_settings import read_secure_settings


class ConfigureCryptoKeysTests(unittest.TestCase):
    def test_saved_values_are_encrypted_and_other_keys_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / "keys.dpapi"
            with patch.object(setup, "read_secure_settings", return_value={"CRYPTO_GROK_API_KEY": "existing-placeholder"}):
                saved = setup.configure(["CRYPTO_OPENAI_API_KEY"], store=store, prompt=lambda name: "new-placeholder")
            self.assertEqual(["CRYPTO_OPENAI_API_KEY"], saved)
            self.assertNotIn(b"new-placeholder", store.read_bytes())
            self.assertNotIn(b"existing-placeholder", store.read_bytes())
            self.assertEqual({"CRYPTO_GROK_API_KEY": "existing-placeholder", "CRYPTO_OPENAI_API_KEY": "new-placeholder"}, read_secure_settings(store))

    def test_blank_input_preserves_store_without_rewriting(self):
        with patch.object(setup, "read_secure_settings", return_value={"CRYPTO_OPENAI_API_KEY": "saved-placeholder"}), patch.object(setup, "write_secure_settings") as write:
            self.assertEqual([], setup.configure(["CRYPTO_OPENAI_API_KEY"], prompt=lambda name: ""))
            write.assert_not_called()

    def test_interrupted_second_prompt_does_not_partially_save(self):
        with patch.object(setup, "read_secure_settings", return_value={}), patch.object(setup, "write_secure_settings") as write:
            answers = iter(["first-placeholder"])
            with self.assertRaises(StopIteration):
                setup.configure(["CRYPTO_OPENAI_API_KEY", "CRYPTO_GROK_API_KEY"], prompt=lambda name: next(answers))
            write.assert_not_called()

    def test_rejects_non_secret_settings_before_reading_store(self):
        with patch.object(setup, "read_secure_settings") as read:
            with self.assertRaises(ValueError):
                setup.configure(["CRYPTO_EXECUTION_MODE"])
            read.assert_not_called()

    def test_getpass_echo_fallback_is_an_error(self):
        def fallback(*args):
            warnings.warn("Cannot hide input", getpass.GetPassWarning)
            self.fail("A plaintext input fallback was allowed")
        with patch.object(setup.getpass, "getpass", side_effect=fallback):
            with self.assertRaises(getpass.GetPassWarning):
                setup.hidden_prompt("CRYPTO_OPENAI_API_KEY")

    def test_cli_rejects_piped_input_before_prompting(self):
        with patch.object(setup.sys.stdin, "isatty", return_value=False), patch.object(setup, "configure") as configure, patch.object(setup.sys, "stderr"):
            with self.assertRaises(SystemExit):
                setup.main(["CRYPTO_OPENAI_API_KEY"])
            configure.assert_not_called()
