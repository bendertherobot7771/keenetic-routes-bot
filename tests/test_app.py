from __future__ import annotations

import unittest

from keenetic_routes_bot.app import BotApp
from keenetic_routes_bot.config import Config
from keenetic_routes_bot.models import DnsRoute, FqdnGroup, Interface, Ipv4Route


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
        self.ipv4_routes: list[Ipv4Route] = []
        self.saved_groups: list[FqdnGroup] = []
        self.saved_dns_routes: list[DnsRoute] = []
        self.saved_ipv4_routes: list[Ipv4Route] = []

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

    def save_dns_route(self, route):
        self.save_dns_routes([route])

    def save_dns_routes(self, routes):
        for route in routes:
            self.saved_dns_routes.append(route)
            self.rules = [item for item in self.rules if item.index != route.index]
            self.rules.append(route)

    def list_ipv4_routes(self):
        return list(self.ipv4_routes)

    def save_ipv4_route(self, route):
        self.save_ipv4_routes([route])

    def save_ipv4_routes(self, routes):
        for route in routes:
            self.saved_ipv4_routes.append(route)
            self.ipv4_routes = [
                item for item in self.ipv4_routes if item.index != route.index
            ]
            self.ipv4_routes.append(route)

    def list_interfaces(self):
        return [
            Interface("u1Host", "WireGuard", True),
            Interface("Wireguard3", "fastVPS_Estonia", True),
        ]


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
        self.assertFalse(self.router.saved_groups)
        self.assertIn("уже есть", self.telegram.messages[-1][1])
        self.app.handle_update(self.callback_update("g_add_yes"))
        saved = self.router.saved_groups[-1]
        self.assertEqual(saved.entries, ("openai.com", "chatgpt.com"))
        self.assertIn("Добавлено: 1", self.telegram.messages[-1][1])

    def test_warns_when_parent_domain_already_exists_and_can_cancel(self) -> None:
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_add"))
        self.app.handle_update(self.message_update("api.openai.com"))

        text = self.telegram.messages[-1][1]
        self.assertIn("api.openai.com", text)
        self.assertIn("openai.com", text)
        self.assertFalse(self.router.saved_groups)

        self.app.handle_update(self.callback_update("g_add_cancel"))
        self.assertFalse(self.router.saved_groups)
        self.assertIn("Добавление отменено", self.telegram.messages[-1][1])

    def test_add_prompt_explains_bulk_input_formats(self) -> None:
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_add"))

        text = self.telegram.messages[-1][1]
        self.assertIn("столбиком", text)
        self.assertIn("через пробел", text)
        self.assertIn("ya.ru yandex.ru yandex.com", text)

    def test_removes_multiple_entries_from_selected_group(self) -> None:
        self.router.groups = [
            FqdnGroup(
                "yandex",
                "Yandex",
                ("ya.ru", "yandex.ru", "yandex.com", "keep.example"),
            )
        ]
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_remove"))
        self.app.handle_update(self.message_update("ya.ru yandex.ru\nyandex.com"))

        self.assertEqual(
            self.router.get_group("yandex").entries,
            ("keep.example",),
        )
        self.assertIn("Удалено: 3", self.telegram.messages[-1][1])

    def test_globally_removes_domains_and_reports_each_list(self) -> None:
        self.router.groups = [
            FqdnGroup(
                "first",
                "First list",
                ("ya.ru", "yandex.ru", "keep.example"),
            ),
            FqdnGroup(
                "second",
                "Second list",
                ("yandex.com", "ya.ru", "192.0.2.0/24"),
            ),
        ]
        self.app.handle_update(self.callback_update("groups_remove"))
        self.assertIn("через пробел", self.telegram.messages[-1][1])
        self.app.handle_update(
            self.message_update("ya.ru yandex.ru\nyandex.com missing.example")
        )

        self.assertEqual(
            self.router.get_group("first").entries,
            ("keep.example",),
        )
        self.assertEqual(
            self.router.get_group("second").entries,
            ("192.0.2.0/24",),
        )
        text = self.telegram.messages[-1][1]
        self.assertIn("Удалено доменов: <b>4</b>", text)
        self.assertIn("First list", text)
        self.assertIn("Second list", text)
        self.assertIn("missing.example", text)

    def test_global_remove_rejects_ip_entries(self) -> None:
        self.app.handle_update(self.callback_update("groups_remove"))
        self.app.handle_update(self.message_update("192.0.2.1"))
        self.assertIn("только доменные имена", self.telegram.messages[-1][1])
        self.assertFalse(self.router.saved_groups)

    def test_partial_domain_search_finds_entries_and_linked_rules(self) -> None:
        self.router.groups = [
            FqdnGroup(
                "yandex-main",
                "Yandex main",
                ("ya.ru", "yandex.ru", "example.com"),
            ),
            FqdnGroup("yandex-global", "Yandex global", ("yandex.com",)),
        ]
        self.router.rules = [
            DnsRoute("1", "yandex-main", interface="u1Host", enabled=True),
            DnsRoute(
                "2",
                "yandex-global",
                interface="Wireguard0",
                reject=True,
                enabled=False,
            ),
        ]

        self.app.handle_update(self.callback_update("groups_search"))
        self.app.handle_update(self.message_update("ya"))
        choice_text = self.telegram.messages[-1][1]
        choice_keyboard = self.telegram.messages[-1][2]
        self.assertIn("Как искать", choice_text)
        self.assertEqual(
            choice_keyboard["inline_keyboard"][1][0]["text"],
            "🔎 Частичное совпадение",
        )

        self.app.handle_update(self.callback_update("groups_search_partial"))
        text = self.telegram.messages[-1][1]
        self.assertIn("Найдено: 3", text)
        self.assertIn("ya.ru", text)
        self.assertIn("yandex.ru", text)
        self.assertIn("yandex.com", text)
        self.assertIn("Yandex main", text)
        self.assertIn("u1Host", text)
        self.assertIn("Wireguard0", text)
        self.assertIn("exclusive", text)

    def test_exact_domain_search_returns_only_identical_domain(self) -> None:
        self.router.groups = [
            FqdnGroup("yandex", "Yandex", ("ya.ru", "yandex.ru", "notya.ru"))
        ]
        self.app.handle_update(self.callback_update("groups_search"))
        self.app.handle_update(self.message_update("YA.RU."))
        self.app.handle_update(self.callback_update("groups_search_exact"))

        text = self.telegram.messages[-1][1]
        self.assertIn("Найдено: 1", text)
        self.assertIn("<code>ya.ru</code>", text)
        self.assertNotIn("yandex.ru", text)
        self.assertNotIn("notya.ru", text)
        self.assertIn("Правила: нет", text)

    def test_domain_search_reports_no_matches(self) -> None:
        self.app.handle_update(self.callback_update("groups_search"))
        self.app.handle_update(self.message_update("missing"))
        self.app.handle_update(self.callback_update("groups_search_partial"))
        self.assertIn("ничего не найдено", self.telegram.messages[-1][1])

    def test_warns_when_new_parent_covers_an_existing_subdomain(self) -> None:
        self.router.groups = [FqdnGroup("search", "Search", ("search.yandex.ru",))]
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_add"))
        self.app.handle_update(self.message_update("yandex.ru"))

        text = self.telegram.messages[-1][1]
        self.assertIn("yandex.ru", text)
        self.assertIn("покрывает", text)
        self.assertFalse(self.router.saved_groups)

    def test_warns_before_creating_group_with_covered_domain(self) -> None:
        self.app.handle_update(self.callback_update("group_new"))
        self.app.handle_update(self.message_update("AI subdomains"))
        self.app.handle_update(self.message_update("api.openai.com"))

        self.assertIn("уже есть", self.telegram.messages[-1][1])
        self.assertIsNone(self.router.get_group("AI subdomains"))

        self.app.handle_update(self.callback_update("g_create_yes"))
        created = self.router.get_group("AI subdomains")
        self.assertIsNotNone(created)
        self.assertEqual(created.entries, ("api.openai.com",))

    def test_deduplicates_domains_across_all_groups(self) -> None:
        self.router.groups = [
            FqdnGroup(
                "first",
                "First",
                ("search.yandex.ru", "192.0.2.0/24"),
            ),
            FqdnGroup(
                "second",
                "Second",
                ("yandex.ru", "openai.com"),
            ),
            FqdnGroup(
                "third",
                "Third",
                ("openai.com", "mail.yandex.ru", "192.0.2.0/24"),
            ),
        ]

        self.app.handle_update(self.callback_update("groups_dedupe"))
        self.assertIn("избыточных доменов: <b>3</b>", self.telegram.messages[-1][1])
        self.assertFalse(self.router.saved_groups)

        self.app.handle_update(self.callback_update("groups_dedupe_yes"))
        self.assertEqual(
            self.router.get_group("first").entries,
            ("192.0.2.0/24",),
        )
        self.assertEqual(
            self.router.get_group("second").entries,
            ("yandex.ru", "openai.com"),
        )
        self.assertEqual(
            self.router.get_group("third").entries,
            ("192.0.2.0/24",),
        )
        self.assertIn("Удалено избыточных доменов: 3", self.telegram.messages[-1][1])

    def test_deduplicate_reports_when_nothing_to_remove(self) -> None:
        self.app.handle_update(self.callback_update("groups_dedupe"))
        self.assertIn("не найдены", self.telegram.messages[-1][1])
        self.assertFalse(self.router.saved_groups)

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
        labels = [row[0]["text"] for row in keyboard["inline_keyboard"]]
        self.assertIn("🔎 Найти правило по домену", labels)
        self.assertIn("🔄 Массово сменить интерфейс", labels)
        self.assertIn("🗑 Удалить домены из списков", labels)
        self.assertIn("🧹 Убрать дубликаты", labels)

    def test_group_domain_buttons_have_explicit_labels(self) -> None:
        keyboard = self.app._group_keyboard()["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "📄 Показать домены")
        self.assertEqual(keyboard[1][0]["text"], "➕ Добавить домен")
        self.assertEqual(keyboard[1][1]["text"], "➖ Удалить домен")

    def test_rules_show_group_and_interface_descriptions(self) -> None:
        self.router.groups = [
            FqdnGroup("domain-list0", "Социальные сети", ("example.com",))
        ]
        self.router.rules = [
            DnsRoute("1", "domain-list0", interface="Wireguard3")
        ]

        self.app.handle_update(self.callback_update("rules"))

        label = self.telegram.messages[-1][2]["inline_keyboard"][0][0]["text"]
        self.assertIn("domain-list0 (Социальные сети)", label)
        self.assertIn("Wireguard3 (fastVPS_Estonia)", label)

    def test_ipv4_routes_show_description_and_interface_name(self) -> None:
        self.router.ipv4_routes = [
            Ipv4Route(
                "7",
                "149.154.160.0/20",
                interface="Wireguard3",
                comment="telegram",
            )
        ]

        self.app.handle_update(self.callback_update("routes"))

        label = self.telegram.messages[-1][2]["inline_keyboard"][0][0]["text"]
        self.assertIn("Wireguard3 (fastVPS_Estonia)", label)
        self.assertIn("telegram", label)

    def test_changes_interface_for_one_dns_rule(self) -> None:
        self.router.rules = [DnsRoute("1", "openai", interface="u1Host")]
        self.app.handle_update(self.callback_update("rules"))
        self.app.handle_update(self.callback_update("r:0"))
        self.app.handle_update(self.callback_update("r_interface"))
        self.app.handle_update(self.callback_update("rif:1"))

        self.assertEqual(self.router.saved_dns_routes[-1].interface, "Wireguard3")
        self.assertIn("fastVPS_Estonia", self.telegram.messages[-1][1])

    def test_changes_interface_for_one_ipv4_route(self) -> None:
        self.router.ipv4_routes = [
            Ipv4Route("7", "149.154.160.0/20", interface="u1Host", comment="telegram")
        ]
        self.app.handle_update(self.callback_update("routes"))
        self.app.handle_update(self.callback_update("ip:0"))
        self.app.handle_update(self.callback_update("ip_interface"))
        self.app.handle_update(self.callback_update("ipif:1"))

        self.assertEqual(self.router.saved_ipv4_routes[-1].interface, "Wireguard3")

    def test_bulk_changes_dns_interfaces_for_selected_groups(self) -> None:
        self.router.groups = [
            FqdnGroup("domain-list0", "Первый", ("one.example",)),
            FqdnGroup("domain-list1", "Второй", ("two.example",)),
        ]
        self.router.rules = [
            DnsRoute("1", "domain-list0", interface="u1Host"),
            DnsRoute("2", "domain-list1", interface="u1Host", reject=True),
        ]
        self.app.handle_update(self.callback_update("groups_interfaces"))
        self.app.handle_update(self.callback_update("dgb:1"))
        self.app.handle_update(self.callback_update("dgb_done"))
        self.app.handle_update(self.callback_update("dgbif:1"))
        self.assertFalse(self.router.saved_dns_routes)
        self.app.handle_update(self.callback_update("dgb_apply"))

        self.assertEqual(len(self.router.saved_dns_routes), 1)
        saved = self.router.saved_dns_routes[0]
        self.assertEqual(saved.group, "domain-list1")
        self.assertEqual(saved.interface, "Wireguard3")
        self.assertTrue(saved.reject)

    def test_bulk_changes_ipv4_interfaces_by_description(self) -> None:
        self.router.ipv4_routes = [
            Ipv4Route("1", "149.154.160.0/20", interface="u1Host", comment="telegram"),
            Ipv4Route("2", "91.108.4.0/22", interface="u1Host", comment="telegram"),
            Ipv4Route("3", "31.13.64.0/18", interface="u1Host", comment="social"),
        ]
        self.app.handle_update(self.callback_update("routes_interfaces"))
        self.app.handle_update(self.callback_update("ipd:1"))
        self.app.handle_update(self.callback_update("ipbif:1"))
        self.assertFalse(self.router.saved_ipv4_routes)
        self.app.handle_update(self.callback_update("ipbi_apply"))

        self.assertEqual(len(self.router.saved_ipv4_routes), 2)
        self.assertEqual(
            {route.comment for route in self.router.saved_ipv4_routes}, {"telegram"}
        )
        self.assertTrue(
            all(
                route.interface == "Wireguard3"
                for route in self.router.saved_ipv4_routes
            )
        )


if __name__ == "__main__":
    unittest.main()
