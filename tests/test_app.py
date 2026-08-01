from __future__ import annotations

import unittest

from keenetic_routes_bot.app import BotApp
from keenetic_routes_bot.config import Config
from keenetic_routes_bot.models import DnsRoute, FqdnGroup, Interface


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, object]] = []
        self.answers: list[tuple[str, str, bool]] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup=None,
        parse_mode="HTML",
        disable_web_page_preview=True,
    ):
        self.messages.append((chat_id, text, reply_markup))
        return {}

    def answer_callback_query(
        self, callback_query_id: str, *, text: str = "", show_alert: bool = False
    ) -> None:
        self.answers.append((callback_query_id, text, show_alert))


class FakeRouter:
    def __init__(self) -> None:
        self.groups = [FqdnGroup("openai", "openai", ("openai.com",))]
        self.rules: list[DnsRoute] = []
        self.saved_groups: list[FqdnGroup] = []

    def version(self):
        return {"release": "5.1.1"}

    def list_groups(self):
        return list(self.groups)

    def get_group(self, name):
        return next((item for item in self.groups if item.name == name), None)

    def save_group(self, group, replace=True):
        self.saved_groups.append(group)
        self.groups = [item for item in self.groups if item.name != group.name]
        self.groups.append(group)

    def delete_group(self, name):
        self.groups = [item for item in self.groups if item.name != name]

    def list_dns_routes(self):
        return list(self.rules)

    def list_ipv4_routes(self):
        return []

    def list_interfaces(self):
        return [Interface("u1Host", "WireGuard", True)]


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.telegram = FakeTelegram()
        self.router = FakeRouter()
        self.config = Config(
            bot_token="123:abc",
            allowed_users=frozenset({42}),
            default_interface="u1Host",
        )
        self.app = BotApp(
            self.config,
            self.telegram,  # type: ignore[arg-type]
            self.router,  # type: ignore[arg-type]
        )

    def message_update(self, text: str, *, user_id: int = 42):
        return {
            "update_id": 1,
            "message": {
                "from": {"id": user_id},
                "chat": {"id": user_id, "type": "private"},
                "text": text,
            },
        }

    def callback_update(self, data: str, *, user_id: int = 42):
        return {
            "update_id": 2,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": user_id},
                "data": data,
                "message": {"chat": {"id": user_id, "type": "private"}},
            },
        }

    def test_denies_unknown_user(self) -> None:
        self.app.handle_update(self.message_update("/start", user_id=99))
        self.assertIn("Доступ запрещён", self.telegram.messages[-1][1])

    def test_adds_entries_to_selected_group(self) -> None:
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_add"))
        self.app.handle_update(self.message_update("chatgpt.com\nOPENAI.COM"))
        saved = self.router.saved_groups[-1]
        self.assertEqual(saved.entries, ("openai.com", "chatgpt.com"))
        self.assertIn("Добавлено: 1", self.telegram.messages[-1][1])

    def test_refuses_group_delete_when_rule_uses_it(self) -> None:
        self.router.rules = [DnsRoute("1", "openai", interface="u1Host", auto=True)]
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_delete"))
        self.assertIn("Сначала удалите", self.telegram.messages[-1][1])

    def test_status_includes_total_group_entries(self) -> None:
        self.router.groups = [
            FqdnGroup("Domain list 0", "Social networks", ("x.com", "reddit.com")),
            FqdnGroup("Domain list 1", "Video", ("youtube.com",)),
        ]
        self.app.handle_update(self.callback_update("status"))
        text = self.telegram.messages[-1][1]
        self.assertIn("DNS-списков: <b>2</b> (сайтов: <b>3</b>)", text)

    def test_group_menu_uses_description_instead_of_internal_name(self) -> None:
        self.router.groups = [
            FqdnGroup("Domain list 0", "Мой настоящий список", ("example.com",))
        ]
        self.app.handle_update(self.callback_update("groups"))
        keyboard = self.telegram.messages[-1][2]
        self.assertEqual(
            keyboard["inline_keyboard"][0][0]["text"],
            "🌐 Мой настоящий список · 1",
        )


if __name__ == "__main__":
    unittest.main()
