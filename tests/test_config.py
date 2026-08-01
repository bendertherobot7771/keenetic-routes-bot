from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keenetic_routes_bot.config import Config, ConfigError, load_env_file


class ConfigTests(unittest.TestCase):
    def test_loads_required_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BOT_TOKEN": "1234567890:abcdefghijklmnopqrstuvwxyzABCDEFGHI",
                "ALLOWED_USERS": "10,20",
                "DEFAULT_INTERFACE": "u1Host",
            },
            clear=True,
        ):
            config = Config.from_env()
        self.assertEqual(config.allowed_users, frozenset({10, 20}))
        self.assertEqual(config.default_interface, "u1Host")

    def test_rejects_non_loopback_rci_by_default(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BOT_TOKEN": "123:abc",
                "ALLOWED_USERS": "10",
                "RCI_URL": "http://192.168.1.1/rci",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "loopback"):
                Config.from_env()

    def test_env_file_does_not_override_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.env"
            path.write_text(
                'BOT_TOKEN="file:token"\nALLOWED_USERS="5"\n',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"BOT_TOKEN": "environment:token"},
                clear=True,
            ):
                load_env_file(path)
                self.assertEqual(os.environ["BOT_TOKEN"], "environment:token")
                self.assertEqual(os.environ["ALLOWED_USERS"], "5")

    def test_rejects_invalid_default_interface(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BOT_TOKEN": "123:abc",
                "ALLOWED_USERS": "10",
                "DEFAULT_INTERFACE": "bad interface",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigError, "DEFAULT_INTERFACE"):
                Config.from_env()


if __name__ == "__main__":
    unittest.main()
