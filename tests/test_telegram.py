from __future__ import annotations

import unittest
from unittest.mock import Mock

from keenetic_routes_bot.telegram import TelegramClient


class TelegramClientTests(unittest.TestCase):
    def test_deletes_message_by_chat_and_message_id(self) -> None:
        client = TelegramClient("123:abc")
        client.call = Mock(return_value=True)  # type: ignore[method-assign]

        client.delete_message(42, 123)

        client.call.assert_called_once_with(
            "deleteMessage", {"chat_id": 42, "message_id": 123}
        )


if __name__ == "__main__":
    unittest.main()
