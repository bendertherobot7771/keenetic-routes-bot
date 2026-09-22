from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from keenetic_routes_bot.app import BotApp
from keenetic_routes_bot.config import Config
from keenetic_routes_bot.models import DnsRoute, FqdnGroup, Interface, Ipv4Route
from keenetic_routes_bot.telegram import TelegramError


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, object]] = []
        self.sent_messages: list[tuple[int, str, object]] = []
        self.edited_messages: list[tuple[int, int, str, object]] = []
        self.answers: list[tuple[str, str, bool]] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self.delete_error: TelegramError | None = None
        self.edit_error: TelegramError | None = None
        self.next_message_id = 100

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup=None,
        parse_mode="HTML",
        disable_web_page_preview=True,
    ):
        message = (chat_id, text, reply_markup)
        self.messages.append(message)
        self.sent_messages.append(message)
        self.next_message_id += 1
        return {"message_id": self.next_message_id, "chat": {"id": chat_id}}

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup=None,
        parse_mode="HTML",
        disable_web_page_preview=True,
    ):
        if self.edit_error is not None:
            raise self.edit_error
        self.messages.append((chat_id, text, reply_markup))
        self.edited_messages.append((chat_id, message_id, text, reply_markup))
        return {"message_id": message_id, "chat": {"id": chat_id}}

    def answer_callback_query(
        self, callback_query_id: str, *, text: str = "", show_alert: bool = False
    ) -> None:
        self.answers.append((callback_query_id, text, show_alert))

    def delete_message(self, chat_id: int, message_id: int) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted_messages.append((chat_id, message_id))


class FakeRouter:
    def __init__(self) -> None:
        self.groups = [FqdnGroup("openai", "openai", ("openai.com",))]
        self.rules: list[DnsRoute] = []
        self.ipv4_routes: list[Ipv4Route] = []
        self.saved_groups: list[FqdnGroup] = []
        self.saved_dns_routes: list[DnsRoute] = []
        self.saved_ipv4_routes: list[Ipv4Route] = []
        self.deleted_ipv4_indices: list[str] = []

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

    def create_group_with_dns_route(self, group, interface):
        self.save_group(group, replace=False)
        self.save_dns_route(
            DnsRoute(
                index=str(len(self.rules) + 1),
                group=group.name,
                interface=interface,
                auto=True,
                enabled=True,
            )
        )

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

    def set_dns_route_enabled(self, index, enabled):
        self.set_dns_routes_enabled([index], enabled)

    def set_dns_routes_enabled(self, indices, enabled):
        selected = set(indices)
        self.rules = [
            replace(route, enabled=enabled) if route.index in selected else route
            for route in self.rules
        ]

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

    def delete_ipv4_route(self, index):
        self.delete_ipv4_routes([index])

    def delete_ipv4_routes(self, indices):
        values = list(indices)
        self.deleted_ipv4_indices.extend(values)
        selected = set(values)
        self.ipv4_routes = [
            route for route in self.ipv4_routes if route.index not in selected
        ]

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
                "message_id": 20,
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
                "message": {
                    "message_id": 10,
                    "chat": {"id": user_id, "type": "private"},
                },
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
        self.assertIsNone(self.router.get_group("AI subdomains"))
        self.app.handle_update(self.callback_update("gnewif:1"))
        created = self.router.get_group("AI subdomains")
        self.assertIsNotNone(created)
        self.assertEqual(created.entries, ("api.openai.com",))
        self.assertTrue(self.router.rules[-1].enabled)
        self.assertEqual(self.router.rules[-1].interface, "Wireguard3")

    def test_new_group_requires_interface_and_creates_enabled_dns_rule(self) -> None:
        self.app.handle_update(self.callback_update("group_new"))
        self.app.handle_update(self.message_update("steam"))
        self.app.handle_update(self.message_update("store.steampowered.com"))

        self.assertIsNone(self.router.get_group("steam"))
        self.assertIn("Выберите интерфейс", self.telegram.messages[-1][1])
        self.app.handle_update(self.callback_update("gnewif:0"))

        self.assertIsNotNone(self.router.get_group("steam"))
        self.assertEqual(len(self.router.rules), 1)
        self.assertTrue(self.router.rules[0].enabled)
        self.assertEqual(self.router.rules[0].interface, "u1Host")
        self.assertIn("Маршрутизация включена", self.telegram.messages[-1][1])

    def test_new_group_reactivates_rule_if_router_created_it_disabled(self) -> None:
        original_create = self.router.create_group_with_dns_route

        def create_disabled(group, interface):
            original_create(group, interface)
            self.router.rules = [
                replace(route, enabled=False) for route in self.router.rules
            ]

        self.router.create_group_with_dns_route = create_disabled
        self.app.handle_update(self.callback_update("group_new"))
        self.app.handle_update(self.message_update("steam"))
        self.app.handle_update(self.message_update("store.steampowered.com"))
        self.app.handle_update(self.callback_update("gnewif:0"))

        self.assertTrue(self.router.rules[0].enabled)

    def test_group_can_toggle_all_dns_routes_without_deleting(self) -> None:
        self.router.rules = [
            DnsRoute("1", "openai", interface="u1Host", enabled=True),
            DnsRoute("2", "openai", interface="Wireguard3", enabled=True),
        ]
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_toggle"))

        self.assertTrue(all(not route.enabled for route in self.router.rules))
        self.assertEqual(len(self.router.rules), 2)
        self.app.handle_update(self.callback_update("g_toggle"))
        self.assertTrue(all(route.enabled for route in self.router.rules))

    def test_group_without_rule_requires_route_before_toggle(self) -> None:
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_toggle"))

        self.assertIn("Сначала создайте правило", self.telegram.messages[-1][1])

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
            "⚠️ Мой настоящий список · 1",
        )
        labels = [row[0]["text"] for row in keyboard["inline_keyboard"]]
        self.assertIn("🔎 Найти правило по домену", labels)
        self.assertIn("🔄 Сменить интерфейс выборочно", labels)
        self.assertIn("🔄 Сменить интерфейс во всех списках", labels)
        self.assertIn("🗑 Удалить домены из списков", labels)
        self.assertIn("🧹 Убрать дубликаты", labels)

    def test_group_domain_buttons_have_explicit_labels(self) -> None:
        keyboard = self.app._group_keyboard()["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "📄 Показать домены")
        self.assertEqual(keyboard[1][0]["text"], "➕ Добавить домен")
        self.assertEqual(keyboard[1][1]["text"], "➖ Удалить домен")
        self.assertIn(
            "⏯ Включить/выключить маршрутизацию",
            [row[0]["text"] for row in keyboard],
        )

    def test_group_list_marks_enabled_disabled_and_missing_rules(self) -> None:
        self.router.groups = [
            FqdnGroup("a", "Active"),
            FqdnGroup("b", "Disabled"),
            FqdnGroup("c", "Missing"),
        ]
        self.router.rules = [
            DnsRoute("1", "a", interface="u1Host", enabled=True),
            DnsRoute("2", "b", interface="u1Host", enabled=False),
        ]
        self.app.handle_update(self.callback_update("groups"))
        labels = [
            row[0]["text"]
            for row in self.telegram.messages[-1][2]["inline_keyboard"][:3]
        ]
        self.assertEqual(
            labels,
            ["🟢 Active · 0", "⚪ Disabled · 0", "⚠️ Missing · 0"],
        )

    def test_rejects_nonexistent_interface_when_attaching_group(self) -> None:
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_attach"))
        self.app.handle_update(self.message_update("UnknownInterface"))

        self.assertFalse(self.router.saved_dns_routes)
        self.assertFalse(self.telegram.deleted_messages)
        self.assertIn("не найден", self.telegram.sent_messages[-1][1])

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

    def test_bulk_changes_dns_interfaces_for_all_groups(self) -> None:
        self.router.groups = [
            FqdnGroup("domain-list0", "Первый", ("one.example",)),
            FqdnGroup("domain-list1", "Второй", ("two.example",)),
            FqdnGroup("domain-list2", "Без правила", ("three.example",)),
        ]
        self.router.rules = [
            DnsRoute("1", "domain-list0", interface="u1Host"),
            DnsRoute("2", "domain-list1", interface="u1Host", reject=True),
        ]

        self.app.handle_update(self.callback_update("groups_interfaces_all"))
        self.assertIn(
            "для всех DNS-списков",
            self.telegram.messages[-1][1],
        )
        self.app.handle_update(self.callback_update("dgbif:1"))
        self.assertIn("Списков: <b>2</b>", self.telegram.messages[-1][1])
        self.assertFalse(self.router.saved_dns_routes)
        self.app.handle_update(self.callback_update("dgb_apply"))

        self.assertEqual(len(self.router.saved_dns_routes), 2)
        self.assertEqual(
            {route.group for route in self.router.saved_dns_routes},
            {"domain-list0", "domain-list1"},
        )
        self.assertTrue(
            all(
                route.interface == "Wireguard3"
                for route in self.router.saved_dns_routes
            )
        )

    def test_callback_navigation_edits_one_bot_message(self) -> None:
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_show"))

        self.assertFalse(self.telegram.sent_messages)
        self.assertEqual(len(self.telegram.edited_messages), 3)
        self.assertTrue(
            all(
                message_id == 10
                for _, message_id, _, _ in self.telegram.edited_messages
            )
        )

    def test_message_response_edits_the_active_bot_message(self) -> None:
        self.app.handle_update(self.message_update("/start"))
        sent_message_id = self.app.active_messages[42]
        self.app.handle_update(self.callback_update("group_new"))
        self.app.handle_update(self.message_update("Новый список"))

        self.assertEqual(len(self.telegram.sent_messages), 1)
        self.assertEqual(self.telegram.edited_messages[-1][1], 10)
        self.assertNotEqual(sent_message_id, 0)
        self.assertEqual(self.telegram.deleted_messages, [(42, 20), (42, 20)])

    def test_invalid_user_input_is_kept_and_reported_in_new_message(self) -> None:
        self.app.handle_update(self.callback_update("group_new"))
        sent_before = len(self.telegram.sent_messages)
        self.app.handle_update(self.message_update(""))

        self.assertFalse(self.telegram.deleted_messages)
        self.assertEqual(len(self.telegram.sent_messages), sent_before + 1)
        self.assertIn("❌", self.telegram.sent_messages[-1][1])

    def test_delete_failure_reports_error_without_repeating_action(self) -> None:
        self.telegram.delete_error = TelegramError("Telegram API: denied")
        self.app.handle_update(self.message_update("/start"))

        self.assertFalse(self.telegram.deleted_messages)
        self.assertEqual(len(self.telegram.sent_messages), 2)
        self.assertIn("Данные обработаны", self.telegram.sent_messages[-1][1])

    def test_active_message_survives_bot_restart(self) -> None:
        with TemporaryDirectory() as directory:
            config = replace(
                self.config,
                ui_state_file=str(Path(directory) / "ui_state.json"),
            )
            first = BotApp(config, self.telegram, self.router)
            first.handle_update(self.message_update("/start"))
            message_id = first.active_messages[42]

            second = BotApp(config, self.telegram, self.router)
            second.handle_update(self.message_update("/help"))

        self.assertEqual(len(self.telegram.sent_messages), 1)
        self.assertEqual(self.telegram.edited_messages[-1][1], message_id)

    def test_transient_edit_error_does_not_create_replacement_panel(self) -> None:
        self.app.handle_update(self.message_update("/start"))
        self.telegram.edit_error = TelegramError("Telegram API недоступен")

        with self.assertRaises(TelegramError):
            self.app.handle_update(self.callback_update("groups"))

        self.assertEqual(len(self.telegram.sent_messages), 1)

    def test_long_output_uses_pages_in_the_same_message(self) -> None:
        self.router.groups = [
            FqdnGroup(
                "large",
                "Большой список",
                tuple(f"domain-{index:03d}.example.com" for index in range(300)),
            )
        ]
        self.app.handle_update(self.callback_update("groups"))
        self.app.handle_update(self.callback_update("g:0"))
        self.app.handle_update(self.callback_update("g_show"))

        keyboard = self.telegram.messages[-1][2]
        self.assertEqual(
            keyboard["inline_keyboard"][0][-1]["callback_data"],
            "page:1",
        )
        self.app.handle_update(self.callback_update("page:1"))

        self.assertFalse(self.telegram.sent_messages)
        self.assertEqual(self.telegram.edited_messages[-1][1], 10)
        self.assertIn("(2/", self.telegram.edited_messages[-1][2])

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

    def test_bulk_deletes_ipv4_routes_by_description_after_confirmation(self) -> None:
        self.router.ipv4_routes = [
            Ipv4Route("1", "149.154.160.0/20", interface="u1Host", comment="telegram"),
            Ipv4Route("2", "91.108.4.0/22", interface="u1Host", comment="telegram"),
            Ipv4Route("3", "31.13.64.0/18", interface="u1Host", comment="social"),
        ]

        self.app.handle_update(self.callback_update("routes"))
        labels = [
            row[0]["text"]
            for row in self.telegram.messages[-1][2]["inline_keyboard"]
        ]
        self.assertIn("🗑 Удалить маршруты по описанию", labels)

        self.app.handle_update(self.callback_update("routes_delete_descriptions"))
        self.app.handle_update(self.callback_update("ipdd:1"))
        self.assertFalse(self.router.deleted_ipv4_indices)
        self.assertIn("Удалить IPv4-маршруты: <b>2</b>", self.telegram.messages[-1][1])

        self.app.handle_update(self.callback_update("ipdd_apply"))
        self.assertEqual(self.router.deleted_ipv4_indices, ["1", "2"])
        self.assertEqual([route.index for route in self.router.ipv4_routes], ["3"])
        self.assertIn("Удалено IPv4-маршрутов: <b>2</b>", self.telegram.messages[-1][1])


if __name__ == "__main__":
    unittest.main()
