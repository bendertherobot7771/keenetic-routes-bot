from __future__ import annotations

import html
import logging
import shlex
import time
from collections import defaultdict, deque
from dataclasses import replace
from typing import Any

from .config import Config
from .models import DnsRoute, FqdnGroup, Ipv4Route
from .rci import KeeneticRciClient, RciError
from .telegram import TelegramClient, TelegramError, inline_keyboard
from .validation import (
    ValidationError,
    domain_covers,
    is_domain_entry,
    merge_entries,
    normalize_domain_search_query,
    normalize_group_name,
    normalize_interface,
    parse_entries,
    parse_ipv4_routes,
    remove_entries,
)


class BotApp:
    def __init__(
        self,
        config: Config,
        telegram: TelegramClient,
        router: KeeneticRciClient,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.telegram = telegram
        self.router = router
        self.logger = logger or logging.getLogger(__name__)
        self.sessions: dict[int, dict[str, Any]] = defaultdict(dict)
        self.request_times: dict[int, deque[float]] = defaultdict(deque)

    def run(self) -> None:
        offset: int | None = None
        retry_delay = 2
        self.logger.info("Bot polling started")
        while True:
            try:
                updates = self.telegram.get_updates(
                    offset=offset, timeout=self.config.poll_timeout
                )
                retry_delay = 2
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    offset = max(offset or 0, update_id + 1)
                    try:
                        self.handle_update(update)
                    except Exception:
                        self.logger.exception(
                            "Unhandled update error, update_id=%s", update_id
                        )
                        chat_id = _chat_id(update)
                        if chat_id is not None:
                            self._send(
                                chat_id,
                                "❌ Внутренняя ошибка. Подробности записаны в журнал.",
                                keyboard=self._home_keyboard(),
                            )
            except TelegramError as exc:
                self.logger.warning("%s; retry in %s seconds", exc, retry_delay)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        message = update.get("message")
        actor = (callback or message or {}).get("from") or {}
        user_id = int(actor.get("id", 0))
        chat = (
            (callback or {}).get("message", {}).get("chat")
            if callback
            else (message or {}).get("chat")
        ) or {}
        chat_id = int(chat.get("id", 0))
        if user_id not in self.config.allowed_users:
            self.logger.warning("Access denied for Telegram user_id=%s", user_id)
            if callback:
                self.telegram.answer_callback_query(
                    str(callback.get("id", "")),
                    text="Доступ запрещён.",
                    show_alert=True,
                )
            elif chat_id:
                self._send(chat_id, "⛔ Доступ запрещён.")
            return
        if self.config.private_chats_only and chat.get("type") != "private":
            if callback:
                self.telegram.answer_callback_query(
                    str(callback.get("id", "")),
                    text="Бот работает только в личном чате.",
                    show_alert=True,
                )
            elif chat_id:
                self._send(chat_id, "⛔ Используйте личный чат с ботом.")
            return
        if not self._rate_limit_ok(user_id):
            if callback:
                self.telegram.answer_callback_query(
                    str(callback.get("id", "")),
                    text="Слишком много команд. Повторите через минуту.",
                    show_alert=True,
                )
            else:
                self._send(chat_id, "⏳ Слишком много команд. Повторите через минуту.")
            return
        if callback:
            callback_id = str(callback.get("id", ""))
            self.telegram.answer_callback_query(callback_id)
            self._handle_callback(user_id, chat_id, str(callback.get("data", "")))
            return
        if message:
            text = str(message.get("text", "")).strip()
            if text:
                self._handle_message(user_id, chat_id, text)

    def _handle_message(self, user_id: int, chat_id: int, text: str) -> None:
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command in {"/start", "/menu"}:
            self.sessions[user_id].clear()
            self._send(
                chat_id,
                "<b>Keenetic Routes Bot</b>\n\n"
                "Управляет штатными DNS-списками и IPv4-маршрутами Keenetic.",
                keyboard=self._home_keyboard(),
            )
            return
        if command == "/help":
            self._send_help(chat_id)
            return
        if command == "/cancel":
            self.sessions[user_id].clear()
            self._send(chat_id, "Операция отменена.", keyboard=self._home_keyboard())
            return
        command_callbacks = {
            "/status": "status",
            "/lists": "groups",
            "/rules": "rules",
            "/routes": "routes",
            "/interfaces": "interfaces",
        }
        if command in command_callbacks:
            self._handle_callback(user_id, chat_id, command_callbacks[command])
            return
        action = str(self.sessions[user_id].get("action", ""))
        if not action:
            self._send(
                chat_id,
                "Выберите действие в меню или используйте /help.",
                keyboard=self._home_keyboard(),
            )
            return
        try:
            handler = getattr(self, f"_state_{action}")
            handler(user_id, chat_id, text)
        except AttributeError:
            self.sessions[user_id].clear()
            self._send(chat_id, "Состояние диалога сброшено. Откройте меню заново.")
        except (ValidationError, RciError) as exc:
            self._send(chat_id, f"❌ {html.escape(str(exc))}")

    def _handle_callback(self, user_id: int, chat_id: int, callback_data: str) -> None:
        try:
            if callback_data == "home":
                self.sessions[user_id].clear()
                self._send(
                    chat_id,
                    "<b>Главное меню</b>",
                    keyboard=self._home_keyboard(),
                )
            elif callback_data == "status":
                self._show_status(chat_id)
            elif callback_data == "interfaces":
                self._show_interfaces(chat_id)
            elif callback_data == "groups":
                self._show_groups(user_id, chat_id)
            elif callback_data == "group_new":
                self.sessions[user_id] = {"action": "create_group_name"}
                self._send(
                    chat_id,
                    "Введите имя нового списка (до 64 символов).\n\n/cancel — отмена",
                )
            elif callback_data == "groups_dedupe":
                self._prepare_groups_deduplicate(user_id, chat_id)
            elif callback_data == "groups_dedupe_yes":
                self._confirm_groups_deduplicate(user_id, chat_id)
            elif callback_data == "groups_interfaces":
                self._start_dns_bulk_interface_selection(user_id, chat_id)
            elif callback_data.startswith("dgb:"):
                self._toggle_dns_bulk_group(
                    user_id, chat_id, int(callback_data.split(":", 1)[1])
                )
            elif callback_data == "dgb_all":
                self._select_all_dns_bulk_groups(user_id, chat_id)
            elif callback_data == "dgb_done":
                self._prepare_dns_bulk_interface(user_id, chat_id)
            elif callback_data.startswith("dgbif:"):
                self._prepare_dns_bulk_interface_confirmation(
                    user_id, chat_id, int(callback_data.split(":", 1)[1])
                )
            elif callback_data == "dgb_apply":
                self._apply_dns_bulk_interface(user_id, chat_id)
            elif callback_data == "groups_remove":
                self.sessions[user_id] = {"action": "remove_entries_global"}
                self._send(
                    chat_id,
                    self._bulk_entries_prompt("remove", domains_only=True),
                )
            elif callback_data == "groups_search":
                self.sessions[user_id] = {"action": "search_domain_query"}
                self._send(
                    chat_id,
                    "Введите полный домен или его часть.\n\n"
                    "Примеры: <code>ya.ru</code>, <code>ya</code>\n\n"
                    "/cancel — отмена",
                )
            elif callback_data == "groups_search_exact":
                self._search_domains(user_id, chat_id, exact=True)
            elif callback_data == "groups_search_partial":
                self._search_domains(user_id, chat_id, exact=False)
            elif callback_data.startswith("g:"):
                self._select_group(
                    user_id, chat_id, int(callback_data.split(":", 1)[1])
                )
            elif callback_data == "g_show":
                self._show_group_entries(user_id, chat_id)
            elif callback_data == "g_add":
                self.sessions[user_id]["action"] = "add_group_entries"
                self._send(
                    chat_id,
                    self._bulk_entries_prompt("add"),
                )
            elif callback_data == "g_add_yes":
                self._confirm_group_add(user_id, chat_id)
            elif callback_data == "g_add_cancel":
                self._cancel_group_add(user_id, chat_id)
            elif callback_data == "g_create_yes":
                self._confirm_group_create(user_id, chat_id)
            elif callback_data == "g_create_cancel":
                self.sessions[user_id].clear()
                self._send(
                    chat_id,
                    "Создание списка отменено.",
                    keyboard=inline_keyboard(
                        [[("DNS-списки", "groups")], [("← Меню", "home")]]
                    ),
                )
            elif callback_data == "g_remove":
                self.sessions[user_id]["action"] = "remove_group_entries"
                self._send(
                    chat_id,
                    self._bulk_entries_prompt("remove"),
                )
            elif callback_data == "g_attach":
                self.sessions[user_id]["action"] = "attach_group"
                interface_names = self._interface_names()
                default_interface = self._format_interface(
                    self.config.default_interface, interface_names
                )
                default_hint = (
                    f"\nПо умолчанию: <code>{html.escape(default_interface)}</code>"
                    if self.config.default_interface
                    else ""
                )
                self._send(
                    chat_id,
                    "Введите системный ID интерфейса и необязательный режим "
                    "<code>exclusive</code>.\n"
                    "Пример: <code>u1Host exclusive</code>"
                    f"{default_hint}\n\n/interfaces — показать интерфейсы",
                )
            elif callback_data == "g_delete":
                self._prepare_group_delete(user_id, chat_id)
            elif callback_data == "g_delete_yes":
                self._confirm_group_delete(user_id, chat_id)
            elif callback_data == "rules":
                self._show_rules(user_id, chat_id)
            elif callback_data.startswith("r:"):
                self._select_rule(user_id, chat_id, int(callback_data.split(":", 1)[1]))
            elif callback_data == "r_toggle":
                self._toggle_rule(user_id, chat_id)
            elif callback_data == "r_interface":
                self._show_interface_choices(
                    user_id,
                    chat_id,
                    title="Выберите новый интерфейс для DNS-правила",
                    callback_prefix="rif",
                    cancel_callback="rules",
                )
            elif callback_data.startswith("rif:"):
                self._change_rule_interface(
                    user_id, chat_id, int(callback_data.split(":", 1)[1])
                )
            elif callback_data == "r_delete":
                self._prepare_rule_delete(user_id, chat_id)
            elif callback_data == "r_delete_yes":
                self._confirm_rule_delete(user_id, chat_id)
            elif callback_data == "routes":
                self._show_ipv4_routes(user_id, chat_id)
            elif callback_data == "routes_interfaces":
                self._show_ipv4_description_choices(user_id, chat_id)
            elif callback_data.startswith("ipd:"):
                self._select_ipv4_bulk_description(
                    user_id, chat_id, int(callback_data.split(":", 1)[1])
                )
            elif callback_data.startswith("ipbif:"):
                self._prepare_ipv4_bulk_interface_confirmation(
                    user_id, chat_id, int(callback_data.split(":", 1)[1])
                )
            elif callback_data == "ipbi_apply":
                self._apply_ipv4_bulk_interface(user_id, chat_id)
            elif callback_data == "route_add":
                self.sessions[user_id] = {"action": "add_ipv4_routes"}
                default_interface = self._format_interface(
                    self.config.default_interface, self._interface_names()
                )
                default_hint = (
                    f"\nИнтерфейс по умолчанию: "
                    f"<code>{html.escape(default_interface)}</code>"
                    if self.config.default_interface
                    else ""
                )
                self._send(
                    chat_id,
                    "Отправьте маршруты по одному в строке:\n"
                    "<code>CIDR INTERFACE описание</code>\n\n"
                    "Пример:\n"
                    "<code>149.154.160.0/20 u1Host telegram</code>"
                    f"{default_hint}",
                )
            elif callback_data.startswith("ip:"):
                self._select_ipv4_route(
                    user_id, chat_id, int(callback_data.split(":", 1)[1])
                )
            elif callback_data == "ip_toggle":
                self._toggle_ipv4_route(user_id, chat_id)
            elif callback_data == "ip_interface":
                self._show_interface_choices(
                    user_id,
                    chat_id,
                    title="Выберите новый интерфейс для IPv4-маршрута",
                    callback_prefix="ipif",
                    cancel_callback="routes",
                )
            elif callback_data.startswith("ipif:"):
                self._change_ipv4_route_interface(
                    user_id, chat_id, int(callback_data.split(":", 1)[1])
                )
            elif callback_data == "ip_delete":
                self._prepare_ipv4_delete(user_id, chat_id)
            elif callback_data == "ip_delete_yes":
                self._confirm_ipv4_delete(user_id, chat_id)
            else:
                self._send(
                    chat_id,
                    "Кнопка устарела. Откройте нужный раздел заново.",
                    keyboard=self._home_keyboard(),
                )
        except (ValidationError, RciError) as exc:
            self.logger.warning("Operation failed: %s", exc)
            self._send(
                chat_id,
                f"❌ {html.escape(str(exc))}",
                keyboard=self._home_keyboard(),
            )
        except (ValueError, IndexError):
            self._send(
                chat_id,
                "Список изменился. Откройте раздел заново.",
                keyboard=self._home_keyboard(),
            )

    def _state_create_group_name(self, user_id: int, chat_id: int, text: str) -> None:
        name = normalize_group_name(text)
        if any(group.name == name for group in self.router.list_groups()):
            raise ValidationError("Список с таким именем уже существует.")
        self.sessions[user_id] = {
            "action": "create_group_entries",
            "pending_group": name,
        }
        self._send(
            chat_id,
            f"Список <b>{html.escape(name)}</b>.\n" + self._bulk_entries_prompt("add"),
        )

    def _state_create_group_entries(
        self, user_id: int, chat_id: int, text: str
    ) -> None:
        name = str(self.sessions[user_id]["pending_group"])
        entries = parse_entries(text)
        self._validate_group_size(entries)
        conflicts = self._find_domain_conflicts(entries)
        if conflicts:
            self.sessions[user_id].update({"action": "", "pending_entries": entries})
            self._send_domain_conflict_warning(
                chat_id,
                conflicts,
                confirm_callback="g_create_yes",
                cancel_callback="g_create_cancel",
            )
            return
        self._create_group(user_id, chat_id, name, entries)

    def _create_group(
        self,
        user_id: int,
        chat_id: int,
        name: str,
        entries: tuple[str, ...],
    ) -> None:
        self.router.save_group(
            FqdnGroup(name=name, description=name, entries=entries),
            replace=False,
        )
        self.sessions[user_id] = {"current_group": name}
        self.logger.info(
            "Telegram user_id=%s created FQDN group=%r entries=%s",
            user_id,
            name,
            len(entries),
        )
        self._send(
            chat_id,
            f"✅ Список <b>{html.escape(name)}</b> создан: {len(entries)} записей.",
            keyboard=self._group_keyboard(),
        )

    def _confirm_group_create(self, user_id: int, chat_id: int) -> None:
        name = str(self.sessions[user_id].get("pending_group", ""))
        entries = tuple(self.sessions[user_id].get("pending_entries", ()))
        if not name or not entries:
            raise ValidationError("Подтверждение устарело.")
        if self.router.get_group(name) is not None:
            raise ValidationError("Список с таким именем уже существует.")
        self._create_group(user_id, chat_id, name, entries)

    def _state_add_group_entries(self, user_id: int, chat_id: int, text: str) -> None:
        group = self._current_group(user_id)
        additions = parse_entries(text)
        conflicts = self._find_domain_conflicts(additions)
        if conflicts:
            self.sessions[user_id].update(
                {"action": "", "pending_additions": additions}
            )
            self._send_domain_conflict_warning(
                chat_id,
                conflicts,
                confirm_callback="g_add_yes",
                cancel_callback="g_add_cancel",
            )
            return
        self._add_group_entries(user_id, chat_id, group, additions)

    def _add_group_entries(
        self,
        user_id: int,
        chat_id: int,
        group: FqdnGroup,
        additions: tuple[str, ...],
    ) -> None:
        entries = merge_entries(group.entries, additions)
        self._validate_group_size(entries)
        added_count = len(entries) - len(group.entries)
        self.router.save_group(replace(group, entries=entries))
        self.sessions[user_id] = {"current_group": group.name}
        self.logger.info(
            "Telegram user_id=%s added entries to group=%r count=%s",
            user_id,
            group.name,
            added_count,
        )
        self._send(
            chat_id,
            f"✅ Добавлено: {added_count}. Всего: {len(entries)}.",
            keyboard=self._group_keyboard(),
        )

    def _confirm_group_add(self, user_id: int, chat_id: int) -> None:
        additions = tuple(self.sessions[user_id].get("pending_additions", ()))
        if not additions:
            raise ValidationError("Подтверждение устарело.")
        self._add_group_entries(
            user_id, chat_id, self._current_group(user_id), additions
        )

    def _cancel_group_add(self, user_id: int, chat_id: int) -> None:
        group = self._current_group(user_id)
        self.sessions[user_id] = {"current_group": group.name}
        self._send(
            chat_id,
            "Добавление отменено.",
            keyboard=self._group_keyboard(),
        )

    def _state_remove_group_entries(
        self, user_id: int, chat_id: int, text: str
    ) -> None:
        group = self._current_group(user_id)
        removals = parse_entries(text)
        entries, missing = remove_entries(group.entries, removals)
        removed_count = len(group.entries) - len(entries)
        if removed_count == 0:
            raise ValidationError("Ни одна из указанных записей не найдена.")
        self.router.save_group(replace(group, entries=entries))
        self.sessions[user_id] = {"current_group": group.name}
        suffix = f" Не найдено: {len(missing)}." if missing else ""
        self.logger.info(
            "Telegram user_id=%s removed entries from group=%r count=%s",
            user_id,
            group.name,
            removed_count,
        )
        self._send(
            chat_id,
            f"✅ Удалено: {removed_count}. Осталось: {len(entries)}.{suffix}",
            keyboard=self._group_keyboard(),
        )

    def _state_remove_entries_global(
        self, user_id: int, chat_id: int, text: str
    ) -> None:
        removals = parse_entries(text)
        non_domains = tuple(entry for entry in removals if not is_domain_entry(entry))
        if non_domains:
            raise ValidationError(
                "Для глобального удаления укажите только доменные имена."
            )
        removal_keys = {entry.casefold().rstrip(".") for entry in removals}
        removed_by_group: list[tuple[str, tuple[str, ...]]] = []
        found_keys: set[str] = set()
        for group in self.router.list_groups():
            matched = tuple(
                entry
                for entry in group.entries
                if is_domain_entry(entry)
                and entry.casefold().rstrip(".") in removal_keys
            )
            if not matched:
                continue
            matched_keys = {entry.casefold().rstrip(".") for entry in matched}
            remaining = tuple(
                entry
                for entry in group.entries
                if entry.casefold().rstrip(".") not in matched_keys
            )
            self.router.save_group(replace(group, entries=remaining))
            removed_by_group.append((group.description or group.name, matched))
            found_keys.update(matched_keys)
        if not removed_by_group:
            raise ValidationError("Указанные домены не найдены ни в одном DNS-списке.")
        missing = tuple(
            entry
            for entry in removals
            if entry.casefold().rstrip(".") not in found_keys
        )
        removed_count = sum(len(entries) for _, entries in removed_by_group)
        lines: list[str] = []
        for group_name, entries in removed_by_group:
            lines.append(f"<b>{html.escape(group_name)}</b>")
            lines.extend(f"• <code>{html.escape(entry)}</code>" for entry in entries)
            lines.append("")
        if missing:
            lines.append("<b>Не найдены</b>")
            lines.extend(f"• <code>{html.escape(entry)}</code>" for entry in missing)
        self.sessions[user_id].clear()
        self.logger.info(
            "Telegram user_id=%s globally removed FQDN entries groups=%s count=%s missing=%s",
            user_id,
            len(removed_by_group),
            removed_count,
            len(missing),
        )
        self._send_paginated_lines(
            chat_id,
            lines,
            title=(
                f"✅ Удалено доменов: <b>{removed_count}</b> "
                f"из списков: <b>{len(removed_by_group)}</b>"
            ),
            final_keyboard=inline_keyboard(
                [[("DNS-списки", "groups")], [("← Меню", "home")]]
            ),
        )

    def _state_search_domain_query(self, user_id: int, chat_id: int, text: str) -> None:
        query = normalize_domain_search_query(text)
        self.sessions[user_id] = {"search_domain_query": query}
        self._send(
            chat_id,
            f"Как искать <code>{html.escape(query)}</code>?\n\n"
            "Точное совпадение найдёт только идентичный домен.\n"
            "Частичное совпадение найдёт все домены, содержащие этот текст.",
            keyboard=inline_keyboard(
                [
                    [("🎯 Точное совпадение", "groups_search_exact")],
                    [("🔎 Частичное совпадение", "groups_search_partial")],
                    [("Отмена", "groups")],
                ]
            ),
        )

    def _search_domains(self, user_id: int, chat_id: int, *, exact: bool) -> None:
        query = str(self.sessions[user_id].get("search_domain_query", ""))
        if not query:
            raise ValidationError("Поисковый запрос устарел.")
        groups = self.router.list_groups()
        routes = self.router.list_dns_routes()
        interface_names = self._interface_names()
        matches: list[tuple[FqdnGroup, str]] = []
        for group in groups:
            for entry in group.entries:
                if not is_domain_entry(entry):
                    continue
                entry_key = entry.casefold().rstrip(".")
                is_match = entry_key == query if exact else query in entry_key
                if is_match:
                    matches.append((group, entry))
        self.sessions[user_id].clear()
        mode = "точное" if exact else "частичное"
        self.logger.info(
            "Telegram user_id=%s searched FQDN query=%r exact=%s matches=%s",
            user_id,
            query,
            exact,
            len(matches),
        )
        if not matches:
            self._send(
                chat_id,
                f"По запросу <code>{html.escape(query)}</code> "
                f"({mode} совпадение) ничего не найдено.",
                keyboard=inline_keyboard(
                    [
                        [("🔎 Новый поиск", "groups_search")],
                        [("← К спискам", "groups")],
                    ]
                ),
            )
            return
        lines: list[str] = []
        for group, entry in matches:
            display_name = group.description or group.name
            linked = [route for route in routes if route.group == group.name]
            lines.extend(
                [
                    f"<code>{html.escape(entry)}</code>",
                    f"Список: <b>{html.escape(display_name)}</b>",
                ]
            )
            if linked:
                lines.append("Правила:")
                for route in linked:
                    target = self._format_route_target(route, interface_names)
                    lines.append(
                        f"• {'🟢' if route.enabled else '⚪'} "
                        f"<code>{html.escape(target)}</code>"
                        f"{' exclusive' if route.reject else ''}"
                    )
            else:
                lines.append("Правила: нет")
            lines.append("")
        self._send_paginated_lines(
            chat_id,
            lines,
            title=(
                f"🔎 <b>Найдено: {len(matches)}</b>\n"
                f"Запрос: <code>{html.escape(query)}</code> · режим: {mode}"
            ),
            final_keyboard=inline_keyboard(
                [
                    [("🔎 Новый поиск", "groups_search")],
                    [("← К спискам", "groups")],
                ]
            ),
        )

    def _state_attach_group(self, user_id: int, chat_id: int, text: str) -> None:
        group = self._current_group(user_id)
        parts = shlex.split(text)
        interface_value = parts[0] if parts else self.config.default_interface
        if interface_value in {".", "-"}:
            interface_value = self.config.default_interface
        interface = normalize_interface(interface_value)
        exclusive = any(
            part.casefold() in {"exclusive", "эксклюзивный", "reject"}
            for part in parts[1:]
        )
        existing = next(
            (
                route
                for route in self.router.list_dns_routes()
                if route.group == group.name and route.interface == interface
            ),
            None,
        )
        route = DnsRoute(
            index=existing.index if existing else "",
            group=group.name,
            interface=interface,
            auto=True,
            reject=exclusive,
            enabled=True,
        )
        self.router.save_dns_route(route)
        self.sessions[user_id] = {"current_group": group.name}
        self.logger.info(
            "Telegram user_id=%s attached group=%r interface=%r exclusive=%s",
            user_id,
            group.name,
            interface,
            exclusive,
        )
        group_label = self._format_group_name(group.name, {group.name: group})
        interface_label = self._format_interface(
            interface, self._interface_names()
        )
        self._send(
            chat_id,
            f"✅ Список <b>{html.escape(group_label)}</b> направлен через "
            f"<code>{html.escape(interface_label)}</code>.\n"
            f"Эксклюзивный маршрут: {'да' if exclusive else 'нет'}.",
            keyboard=self._group_keyboard(),
        )

    def _state_add_ipv4_routes(self, user_id: int, chat_id: int, text: str) -> None:
        routes = parse_ipv4_routes(
            text, default_interface=self.config.default_interface
        )
        existing_destinations = {
            (route.destination, route.interface)
            for route in self.router.list_ipv4_routes()
        }
        new_routes = tuple(
            route
            for route in routes
            if (route.destination, route.interface) not in existing_destinations
        )
        if not new_routes:
            raise ValidationError("Все указанные маршруты уже существуют.")
        self.router.add_ipv4_routes(new_routes)
        self.sessions[user_id].clear()
        self.logger.info(
            "Telegram user_id=%s added IPv4 routes count=%s",
            user_id,
            len(new_routes),
        )
        self._send(
            chat_id,
            f"✅ Добавлено IPv4-маршрутов: {len(new_routes)}.",
            keyboard=self._routes_keyboard(),
        )

    def _show_status(self, chat_id: int) -> None:
        version = self.router.version()
        release = (
            version.get("release")
            or version.get("version")
            or version.get("title")
            or "неизвестно"
        )
        groups = self.router.list_groups()
        rules = self.router.list_dns_routes()
        routes = self.router.list_ipv4_routes()
        group_entries = sum(len(group.entries) for group in groups)
        self._send(
            chat_id,
            "<b>Статус</b>\n\n"
            f"KeeneticOS: <code>{html.escape(str(release))}</code>\n"
            f"DNS-списков: <b>{len(groups)}</b> (сайтов: <b>{group_entries}</b>)\n"
            f"Правил DNS: <b>{len(rules)}</b>\n"
            f"IPv4-маршрутов: <b>{len(routes)}</b>",
            keyboard=inline_keyboard(
                [[("🔄 Обновить", "status")], [("← Меню", "home")]]
            ),
        )

    def _show_interfaces(self, chat_id: int) -> None:
        interfaces = self.router.list_interfaces()
        if not interfaces:
            self._send(
                chat_id,
                "Интерфейсы не найдены.",
                keyboard=inline_keyboard([[("← Меню", "home")]]),
            )
            return
        lines = ["<b>Системные ID интерфейсов</b>", ""]
        for interface in interfaces[:80]:
            marker = "🟢" if interface.connected else "⚪"
            label = self._format_interface(
                interface.ident, {interface.ident: interface.description}
            )
            lines.append(
                f"{marker} <code>{html.escape(label)}</code>"
            )
        if len(interfaces) > 80:
            lines.append(f"\n…ещё {len(interfaces) - 80}")
        self._send_paginated_lines(
            chat_id,
            lines,
            final_keyboard=inline_keyboard([[("← Меню", "home")]]),
        )

    def _show_groups(self, user_id: int, chat_id: int) -> None:
        groups = self.router.list_groups()
        self.sessions[user_id].clear()
        rows: list[list[tuple[str, str]]] = []
        for index, group in enumerate(groups):
            display_name = group.description or group.name
            rows.append(
                [
                    (
                        f"🌐 {display_name} · {len(group.entries)}",
                        f"g:{index}",
                    )
                ]
            )
        rows.extend(
            [
                [("➕ Новый список", "group_new")],
                [("🔎 Найти правило по домену", "groups_search")],
                [("🔄 Массово сменить интерфейс", "groups_interfaces")],
                [("🗑 Удалить домены из списков", "groups_remove")],
                [("🧹 Убрать дубликаты", "groups_dedupe")],
                [("← Меню", "home")],
            ]
        )
        self._send(
            chat_id,
            f"<b>DNS-списки</b>\n\nВсего: {len(groups)}",
            keyboard=inline_keyboard(rows),
        )

    def _prepare_groups_deduplicate(self, user_id: int, chat_id: int) -> None:
        updates, removed = self._deduplicate_groups(self.router.list_groups())
        if not removed:
            self._send(
                chat_id,
                "✅ Дубликаты и избыточные поддомены не найдены.",
                keyboard=inline_keyboard(
                    [[("← К спискам", "groups")], [("← Меню", "home")]]
                ),
            )
            return
        self.sessions[user_id] = {"confirm_groups_deduplicate": True}
        preview = "\n".join(
            f"• <code>{html.escape(entry)}</code> — {html.escape(group_name)}"
            for group_name, entry in removed[:10]
        )
        suffix = f"\n…ещё {len(removed) - 10}" if len(removed) > 10 else ""
        self._send(
            chat_id,
            f"Найдено избыточных доменов: <b>{len(removed)}</b> "
            f"в {len(updates)} списках.\n\n{preview}{suffix}\n\n"
            "Удалить их? IP-адреса и CIDR не изменятся.",
            keyboard=inline_keyboard(
                [
                    [("🧹 Да, убрать", "groups_dedupe_yes")],
                    [("Отмена", "groups")],
                ]
            ),
        )

    def _confirm_groups_deduplicate(self, user_id: int, chat_id: int) -> None:
        if not self.sessions[user_id].get("confirm_groups_deduplicate"):
            raise ValidationError("Подтверждение устарело.")
        updates, removed = self._deduplicate_groups(self.router.list_groups())
        for group in updates:
            self.router.save_group(group)
        self.sessions[user_id].clear()
        self.logger.info(
            "Telegram user_id=%s deduplicated FQDN groups changed=%s removed=%s",
            user_id,
            len(updates),
            len(removed),
        )
        self._send(
            chat_id,
            f"✅ Удалено избыточных доменов: {len(removed)}. "
            f"Обновлено списков: {len(updates)}.",
            keyboard=inline_keyboard(
                [[("DNS-списки", "groups")], [("← Меню", "home")]]
            ),
        )

    def _select_group(self, user_id: int, chat_id: int, index: int) -> None:
        group = self.router.list_groups()[index]
        self.sessions[user_id] = {"current_group": group.name}
        display_name = group.description or group.name
        linked = [
            route
            for route in self.router.list_dns_routes()
            if route.group == group.name
        ]
        interface_names = self._interface_names()
        links = (
            ", ".join(
                f"<code>{html.escape(self._format_route_target(route, interface_names))}</code>"
                for route in linked
            )
            or "нет"
        )
        self._send(
            chat_id,
            f"<b>{html.escape(display_name)}</b>\n\n"
            f"Записей: <b>{len(group.entries)}</b>\n"
            f"Правила: {links}",
            keyboard=self._group_keyboard(),
        )

    def _show_group_entries(self, user_id: int, chat_id: int) -> None:
        group = self._current_group(user_id)
        if not group.entries:
            self._send(
                chat_id,
                f"Список <b>{html.escape(group.name)}</b> пуст.",
                keyboard=self._group_keyboard(),
            )
            return
        lines = [f"<code>{html.escape(entry)}</code>" for entry in group.entries]
        self._send_paginated_lines(
            chat_id,
            lines,
            title=f"<b>{html.escape(group.name)}</b>",
            final_keyboard=self._group_keyboard(),
        )

    def _prepare_group_delete(self, user_id: int, chat_id: int) -> None:
        group = self._current_group(user_id)
        linked = [
            route
            for route in self.router.list_dns_routes()
            if route.group == group.name
        ]
        if linked:
            raise ValidationError(
                f"Список используется в {len(linked)} правилах. "
                "Сначала удалите связанные правила DNS."
            )
        self.sessions[user_id]["confirm_group_delete"] = group.name
        self._send(
            chat_id,
            f"Удалить список <b>{html.escape(group.name)}</b> и все "
            f"{len(group.entries)} записей?",
            keyboard=inline_keyboard(
                [
                    [("🗑 Да, удалить", "g_delete_yes")],
                    [("Отмена", "groups")],
                ]
            ),
        )

    def _confirm_group_delete(self, user_id: int, chat_id: int) -> None:
        name = str(self.sessions[user_id].get("confirm_group_delete", ""))
        if not name:
            raise ValidationError("Подтверждение устарело.")
        self.router.delete_group(name)
        self.sessions[user_id].clear()
        self.logger.info("Telegram user_id=%s deleted FQDN group=%r", user_id, name)
        self._send(
            chat_id,
            f"✅ Список <b>{html.escape(name)}</b> удалён.",
            keyboard=inline_keyboard(
                [[("DNS-списки", "groups")], [("← Меню", "home")]]
            ),
        )

    def _show_rules(self, user_id: int, chat_id: int) -> None:
        rules = self.router.list_dns_routes()
        groups = {group.name: group for group in self.router.list_groups()}
        interface_names = self._interface_names()
        self.sessions[user_id].clear()
        rows: list[list[tuple[str, str]]] = []
        for position, route in enumerate(rules):
            marker = "🟢" if route.enabled else "⚪"
            target = self._format_route_target(route, interface_names)
            group_label = self._format_group_name(route.group, groups)
            rows.append(
                [
                    (
                        f"{marker} {group_label} → {target}",
                        f"r:{position}",
                    )
                ]
            )
        rows.extend([[("DNS-списки", "groups")], [("← Меню", "home")]])
        self._send(
            chat_id,
            f"<b>Правила DNS-маршрутизации</b>\n\nВсего: {len(rules)}",
            keyboard=inline_keyboard(rows),
        )

    def _select_rule(self, user_id: int, chat_id: int, position: int) -> None:
        route = self.router.list_dns_routes()[position]
        groups = {group.name: group for group in self.router.list_groups()}
        self.sessions[user_id] = {
            "current_rule": route.index,
            "current_rule_group": route.group,
        }
        target = self._format_route_target(route, self._interface_names())
        group_label = self._format_group_name(route.group, groups)
        self._send(
            chat_id,
            f"<b>{html.escape(group_label)}</b>\n\n"
            f"Назначение: <code>{html.escape(target)}</code>\n"
            f"Включено: {'да' if route.enabled else 'нет'}\n"
            f"Автоматически: {'да' if route.auto else 'нет'}\n"
            f"Эксклюзивный: {'да' if route.reject else 'нет'}",
            keyboard=self._rule_keyboard(route.enabled),
        )

    def _toggle_rule(self, user_id: int, chat_id: int) -> None:
        route = self._current_rule(user_id)
        self.router.set_dns_route_enabled(route.index, not route.enabled)
        self.logger.info(
            "Telegram user_id=%s toggled DNS rule index=%r enabled=%s",
            user_id,
            route.index,
            not route.enabled,
        )
        self._send(
            chat_id,
            f"✅ Правило {'включено' if not route.enabled else 'выключено'}.",
            keyboard=inline_keyboard(
                [[("← К правилам", "rules")], [("← Меню", "home")]]
            ),
        )

    def _prepare_rule_delete(self, user_id: int, chat_id: int) -> None:
        route = self._current_rule(user_id)
        groups = {group.name: group for group in self.router.list_groups()}
        group_label = self._format_group_name(route.group, groups)
        self.sessions[user_id]["confirm_rule_delete"] = route.index
        self._send(
            chat_id,
            f"Удалить правило для списка <b>{html.escape(group_label)}</b>?",
            keyboard=inline_keyboard(
                [
                    [("🗑 Да, удалить", "r_delete_yes")],
                    [("Отмена", "rules")],
                ]
            ),
        )

    def _confirm_rule_delete(self, user_id: int, chat_id: int) -> None:
        index = str(self.sessions[user_id].get("confirm_rule_delete", ""))
        if not index:
            raise ValidationError("Подтверждение устарело.")
        self.router.delete_dns_route(index)
        self.sessions[user_id].clear()
        self.logger.info(
            "Telegram user_id=%s deleted DNS rule index=%r", user_id, index
        )
        self._send(
            chat_id,
            "✅ Правило DNS удалено.",
            keyboard=inline_keyboard(
                [[("← К правилам", "rules")], [("← Меню", "home")]]
            ),
        )

    def _show_ipv4_routes(self, user_id: int, chat_id: int) -> None:
        routes = self.router.list_ipv4_routes()
        interface_names = self._interface_names()
        self.sessions[user_id].clear()
        rows: list[list[tuple[str, str]]] = []
        for position, route in enumerate(routes[:90]):
            marker = "🟢" if route.enabled else "⚪"
            target = self._format_route_target(route, interface_names)
            description = route.comment or "без описания"
            rows.append(
                [
                    (
                        f"{marker} {route.destination} → {target} · {description}",
                        f"ip:{position}",
                    )
                ]
            )
        rows.extend(
            [
                [("➕ Добавить", "route_add")],
                [("🔄 Сменить интерфейс по описанию", "routes_interfaces")],
                [("← Меню", "home")],
            ]
        )
        suffix = "\nПоказаны первые 90." if len(routes) > 90 else ""
        self._send(
            chat_id,
            f"<b>Пользовательские IPv4-маршруты</b>\n\nВсего: {len(routes)}{suffix}",
            keyboard=inline_keyboard(rows),
        )

    def _select_ipv4_route(self, user_id: int, chat_id: int, position: int) -> None:
        route = self.router.list_ipv4_routes()[position]
        self.sessions[user_id] = {"current_ipv4_route": route.index}
        target = self._format_route_target(route, self._interface_names())
        self._send(
            chat_id,
            f"<b>{html.escape(route.destination)}</b>\n\n"
            f"Назначение: <code>{html.escape(target)}</code>\n"
            f"Описание: {html.escape(route.comment or '—')}\n"
            f"Включено: {'да' if route.enabled else 'нет'}\n"
            f"Автоматически: {'да' if route.auto else 'нет'}\n"
            f"Эксклюзивный: {'да' if route.reject else 'нет'}",
            keyboard=self._ipv4_route_keyboard(route.enabled),
        )

    def _toggle_ipv4_route(self, user_id: int, chat_id: int) -> None:
        route = self._current_ipv4_route(user_id)
        self.router.set_ipv4_route_enabled(route.index, not route.enabled)
        self.logger.info(
            "Telegram user_id=%s toggled IPv4 route index=%r enabled=%s",
            user_id,
            route.index,
            not route.enabled,
        )
        self._send(
            chat_id,
            f"✅ Маршрут {'включён' if not route.enabled else 'выключен'}.",
            keyboard=self._routes_keyboard(),
        )

    def _prepare_ipv4_delete(self, user_id: int, chat_id: int) -> None:
        route = self._current_ipv4_route(user_id)
        self.sessions[user_id]["confirm_ipv4_delete"] = route.index
        self._send(
            chat_id,
            f"Удалить маршрут <code>{html.escape(route.destination)}</code>?",
            keyboard=inline_keyboard(
                [
                    [("🗑 Да, удалить", "ip_delete_yes")],
                    [("Отмена", "routes")],
                ]
            ),
        )

    def _confirm_ipv4_delete(self, user_id: int, chat_id: int) -> None:
        index = str(self.sessions[user_id].get("confirm_ipv4_delete", ""))
        if not index:
            raise ValidationError("Подтверждение устарело.")
        self.router.delete_ipv4_route(index)
        self.sessions[user_id].clear()
        self.logger.info(
            "Telegram user_id=%s deleted IPv4 route index=%r", user_id, index
        )
        self._send(
            chat_id,
            "✅ IPv4-маршрут удалён.",
            keyboard=self._routes_keyboard(),
        )

    def _show_interface_choices(
        self,
        user_id: int,
        chat_id: int,
        *,
        title: str,
        callback_prefix: str,
        cancel_callback: str,
    ) -> None:
        interfaces = self.router.list_interfaces()
        if not interfaces:
            raise ValidationError("Интерфейсы не найдены.")
        self.sessions[user_id]["interface_choices"] = tuple(
            interface.ident for interface in interfaces
        )
        rows: list[list[tuple[str, str]]] = []
        for position, interface in enumerate(interfaces):
            label = self._format_interface(
                interface.ident, {interface.ident: interface.description}
            )
            rows.append(
                [
                    (
                        f"{'🟢' if interface.connected else '⚪'} {label}",
                        f"{callback_prefix}:{position}",
                    )
                ]
            )
        rows.append([("Отмена", cancel_callback)])
        self._send(
            chat_id,
            f"<b>{html.escape(title)}</b>",
            keyboard=inline_keyboard(rows),
        )

    def _selected_interface(self, user_id: int, position: int) -> str:
        choices = tuple(self.sessions[user_id].get("interface_choices", ()))
        if position < 0 or position >= len(choices):
            raise ValidationError("Список интерфейсов устарел.")
        interface = str(choices[position])
        available = {item.ident for item in self.router.list_interfaces()}
        if interface not in available:
            raise ValidationError("Выбранный интерфейс больше не существует.")
        return interface

    def _change_rule_interface(
        self, user_id: int, chat_id: int, position: int
    ) -> None:
        route = self._current_rule(user_id)
        interface = self._selected_interface(user_id, position)
        if route.interface == interface and not route.gateway:
            raise ValidationError("У правила уже установлен этот интерфейс.")
        self.router.save_dns_route(replace(route, interface=interface, gateway=""))
        self.sessions[user_id].clear()
        interface_label = self._format_interface(interface, self._interface_names())
        self.logger.info(
            "Telegram user_id=%s changed DNS rule index=%r interface=%r",
            user_id,
            route.index,
            interface,
        )
        self._send(
            chat_id,
            f"✅ Интерфейс DNS-правила изменён на "
            f"<code>{html.escape(interface_label)}</code>.",
            keyboard=inline_keyboard(
                [[("← К правилам", "rules")], [("← Меню", "home")]]
            ),
        )

    def _change_ipv4_route_interface(
        self, user_id: int, chat_id: int, position: int
    ) -> None:
        route = self._current_ipv4_route(user_id)
        interface = self._selected_interface(user_id, position)
        if route.interface == interface and not route.gateway:
            raise ValidationError("У маршрута уже установлен этот интерфейс.")
        self.router.save_ipv4_route(replace(route, interface=interface, gateway=""))
        self.sessions[user_id].clear()
        interface_label = self._format_interface(interface, self._interface_names())
        self.logger.info(
            "Telegram user_id=%s changed IPv4 route index=%r interface=%r",
            user_id,
            route.index,
            interface,
        )
        self._send(
            chat_id,
            f"✅ Интерфейс IPv4-маршрута изменён на "
            f"<code>{html.escape(interface_label)}</code>.",
            keyboard=self._routes_keyboard(),
        )

    def _start_dns_bulk_interface_selection(
        self, user_id: int, chat_id: int
    ) -> None:
        routed_groups = {route.group for route in self.router.list_dns_routes()}
        group_names = tuple(
            group.name
            for group in self.router.list_groups()
            if group.name in routed_groups
        )
        if not group_names:
            raise ValidationError("Нет DNS-списков со связанными правилами.")
        self.sessions[user_id] = {
            "dns_bulk_groups": group_names,
            "dns_bulk_selected": [],
        }
        self._show_dns_bulk_group_selection(user_id, chat_id)

    def _show_dns_bulk_group_selection(self, user_id: int, chat_id: int) -> None:
        group_names = tuple(self.sessions[user_id].get("dns_bulk_groups", ()))
        selected = set(self.sessions[user_id].get("dns_bulk_selected", ()))
        groups = {group.name: group for group in self.router.list_groups()}
        rows: list[list[tuple[str, str]]] = []
        for position, name in enumerate(group_names):
            marker = "☑️" if name in selected else "⬜"
            rows.append(
                [
                    (
                        f"{marker} {self._format_group_name(name, groups)}",
                        f"dgb:{position}",
                    )
                ]
            )
        rows.extend(
            [
                [("Выбрать все", "dgb_all")],
                [(f"Продолжить ({len(selected)})", "dgb_done")],
                [("Отмена", "groups")],
            ]
        )
        self._send(
            chat_id,
            "<b>Массовая смена интерфейса DNS</b>\n\n"
            "Выберите списки, для правил которых нужно сменить интерфейс.",
            keyboard=inline_keyboard(rows),
        )

    def _toggle_dns_bulk_group(
        self, user_id: int, chat_id: int, position: int
    ) -> None:
        group_names = tuple(self.sessions[user_id].get("dns_bulk_groups", ()))
        if position < 0 or position >= len(group_names):
            raise ValidationError("Список DNS-групп устарел.")
        selected = set(self.sessions[user_id].get("dns_bulk_selected", ()))
        name = group_names[position]
        if name in selected:
            selected.remove(name)
        else:
            selected.add(name)
        self.sessions[user_id]["dns_bulk_selected"] = list(selected)
        self._show_dns_bulk_group_selection(user_id, chat_id)

    def _select_all_dns_bulk_groups(self, user_id: int, chat_id: int) -> None:
        group_names = tuple(self.sessions[user_id].get("dns_bulk_groups", ()))
        if not group_names:
            raise ValidationError("Список DNS-групп устарел.")
        self.sessions[user_id]["dns_bulk_selected"] = list(group_names)
        self._show_dns_bulk_group_selection(user_id, chat_id)

    def _prepare_dns_bulk_interface(self, user_id: int, chat_id: int) -> None:
        selected = tuple(self.sessions[user_id].get("dns_bulk_selected", ()))
        if not selected:
            raise ValidationError("Выберите хотя бы один DNS-список.")
        self._show_interface_choices(
            user_id,
            chat_id,
            title="Выберите новый интерфейс для выбранных DNS-списков",
            callback_prefix="dgbif",
            cancel_callback="groups",
        )

    def _prepare_dns_bulk_interface_confirmation(
        self, user_id: int, chat_id: int, position: int
    ) -> None:
        interface = self._selected_interface(user_id, position)
        selected = set(self.sessions[user_id].get("dns_bulk_selected", ()))
        routes = [
            replace(route, interface=interface, gateway="")
            for route in self.router.list_dns_routes()
            if route.group in selected
            and (route.interface != interface or bool(route.gateway))
        ]
        if not routes:
            raise ValidationError("У выбранных правил уже установлен этот интерфейс.")
        self.sessions[user_id]["pending_dns_bulk_interface"] = interface
        interface_label = self._format_interface(interface, self._interface_names())
        self._send(
            chat_id,
            f"Сменить интерфейс у DNS-правил: <b>{len(routes)}</b>?\n"
            f"Списков: <b>{len({route.group for route in routes})}</b>.\n"
            f"Новый интерфейс: <code>{html.escape(interface_label)}</code>.",
            keyboard=inline_keyboard(
                [
                    [("✅ Сменить", "dgb_apply")],
                    [("Отмена", "groups")],
                ]
            ),
        )

    def _apply_dns_bulk_interface(self, user_id: int, chat_id: int) -> None:
        interface = str(
            self.sessions[user_id].get("pending_dns_bulk_interface", "")
        )
        available = {item.ident for item in self.router.list_interfaces()}
        if not interface or interface not in available:
            raise ValidationError("Выбранный интерфейс больше не существует.")
        selected = set(self.sessions[user_id].get("dns_bulk_selected", ()))
        routes = [
            replace(route, interface=interface, gateway="")
            for route in self.router.list_dns_routes()
            if route.group in selected
            and (route.interface != interface or bool(route.gateway))
        ]
        if not routes:
            raise ValidationError("Правила уже изменены или больше не существуют.")
        self.router.save_dns_routes(routes)
        self.sessions[user_id].clear()
        interface_label = self._format_interface(interface, self._interface_names())
        changed_groups = {route.group for route in routes}
        self.logger.info(
            "Telegram user_id=%s bulk changed DNS interfaces groups=%s rules=%s interface=%r",
            user_id,
            len(changed_groups),
            len(routes),
            interface,
        )
        self._send(
            chat_id,
            f"✅ Интерфейс изменён у правил: <b>{len(routes)}</b>; "
            f"DNS-списков: <b>{len(changed_groups)}</b>.\n"
            f"Новый интерфейс: <code>{html.escape(interface_label)}</code>.",
            keyboard=inline_keyboard(
                [[("DNS-списки", "groups")], [("← Меню", "home")]]
            ),
        )

    def _show_ipv4_description_choices(self, user_id: int, chat_id: int) -> None:
        routes = self.router.list_ipv4_routes()
        descriptions = tuple(
            sorted({route.comment for route in routes if route.comment}, key=str.casefold)
        )
        if not descriptions:
            raise ValidationError("Нет IPv4-маршрутов с заполненным описанием.")
        counts = {
            description: sum(route.comment == description for route in routes)
            for description in descriptions
        }
        self.sessions[user_id] = {"ipv4_bulk_descriptions": descriptions}
        rows = [
            [
                (
                    f"{description} · {counts[description]}",
                    f"ipd:{position}",
                )
            ]
            for position, description in enumerate(descriptions)
        ]
        rows.append([("Отмена", "routes")])
        self._send(
            chat_id,
            "<b>Массовая смена интерфейса IPv4</b>\n\n"
            "Выберите описание маршрутов.",
            keyboard=inline_keyboard(rows),
        )

    def _select_ipv4_bulk_description(
        self, user_id: int, chat_id: int, position: int
    ) -> None:
        descriptions = tuple(
            self.sessions[user_id].get("ipv4_bulk_descriptions", ())
        )
        if position < 0 or position >= len(descriptions):
            raise ValidationError("Список описаний устарел.")
        description = str(descriptions[position])
        self.sessions[user_id]["ipv4_bulk_description"] = description
        self._show_interface_choices(
            user_id,
            chat_id,
            title=f"Новый интерфейс для маршрутов «{description}»",
            callback_prefix="ipbif",
            cancel_callback="routes",
        )

    def _prepare_ipv4_bulk_interface_confirmation(
        self, user_id: int, chat_id: int, position: int
    ) -> None:
        interface = self._selected_interface(user_id, position)
        description = str(
            self.sessions[user_id].get("ipv4_bulk_description", "")
        )
        if not description:
            raise ValidationError("Выбранное описание устарело.")
        routes = [
            replace(route, interface=interface, gateway="")
            for route in self.router.list_ipv4_routes()
            if route.comment == description
            and (route.interface != interface or bool(route.gateway))
        ]
        if not routes:
            raise ValidationError("У этих маршрутов уже установлен этот интерфейс.")
        self.sessions[user_id]["pending_ipv4_bulk_interface"] = interface
        interface_label = self._format_interface(interface, self._interface_names())
        self._send(
            chat_id,
            f"Сменить интерфейс у IPv4-маршрутов: <b>{len(routes)}</b>?\n"
            f"Описание: <b>{html.escape(description)}</b>.\n"
            f"Новый интерфейс: <code>{html.escape(interface_label)}</code>.",
            keyboard=inline_keyboard(
                [
                    [("✅ Сменить", "ipbi_apply")],
                    [("Отмена", "routes")],
                ]
            ),
        )

    def _apply_ipv4_bulk_interface(self, user_id: int, chat_id: int) -> None:
        interface = str(
            self.sessions[user_id].get("pending_ipv4_bulk_interface", "")
        )
        available = {item.ident for item in self.router.list_interfaces()}
        if not interface or interface not in available:
            raise ValidationError("Выбранный интерфейс больше не существует.")
        description = str(
            self.sessions[user_id].get("ipv4_bulk_description", "")
        )
        if not description:
            raise ValidationError("Выбранное описание устарело.")
        routes = [
            replace(route, interface=interface, gateway="")
            for route in self.router.list_ipv4_routes()
            if route.comment == description
            and (route.interface != interface or bool(route.gateway))
        ]
        if not routes:
            raise ValidationError("Маршруты уже изменены или больше не существуют.")
        self.router.save_ipv4_routes(routes)
        self.sessions[user_id].clear()
        interface_label = self._format_interface(interface, self._interface_names())
        self.logger.info(
            "Telegram user_id=%s bulk changed IPv4 interfaces "
            "description=%r routes=%s interface=%r",
            user_id,
            description,
            len(routes),
            interface,
        )
        self._send(
            chat_id,
            f"✅ Интерфейс изменён у IPv4-маршрутов: <b>{len(routes)}</b>.\n"
            f"Описание: <b>{html.escape(description)}</b>.\n"
            f"Новый интерфейс: <code>{html.escape(interface_label)}</code>.",
            keyboard=self._routes_keyboard(),
        )

    def _current_group(self, user_id: int) -> FqdnGroup:
        name = str(self.sessions[user_id].get("current_group", ""))
        group = self.router.get_group(name)
        if group is None:
            raise ValidationError("Список больше не существует.")
        return group

    def _current_rule(self, user_id: int) -> DnsRoute:
        index = str(self.sessions[user_id].get("current_rule", ""))
        route = next(
            (item for item in self.router.list_dns_routes() if item.index == index),
            None,
        )
        if route is None:
            raise ValidationError("Правило больше не существует.")
        return route

    def _current_ipv4_route(self, user_id: int) -> Ipv4Route:
        index = str(self.sessions[user_id].get("current_ipv4_route", ""))
        route = next(
            (item for item in self.router.list_ipv4_routes() if item.index == index),
            None,
        )
        if route is None:
            raise ValidationError("Маршрут больше не существует.")
        return route

    def _send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: dict[str, Any] | None = None,
    ) -> None:
        self.telegram.send_message(chat_id, text, reply_markup=keyboard)

    def _send_paginated_lines(
        self,
        chat_id: int,
        lines: list[str],
        *,
        title: str = "",
        final_keyboard: dict[str, Any] | None = None,
    ) -> None:
        pages: list[list[str]] = []
        current: list[str] = []
        current_length = len(title) + 32
        for line in lines:
            extra = len(line) + 1
            if current and current_length + extra > 3500:
                pages.append(current)
                current = []
                current_length = len(title) + 32
            current.append(line)
            current_length += extra
        if current or not pages:
            pages.append(current)
        for page_number, page in enumerate(pages, start=1):
            page_title = title
            if title and len(pages) > 1:
                page_title += f" ({page_number}/{len(pages)})"
            prefix = f"{page_title}\n\n" if page_title else ""
            keyboard = final_keyboard if page_number == len(pages) else None
            self._send(
                chat_id,
                prefix + "\n".join(page),
                keyboard=keyboard,
            )

    def _send_help(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "<b>Команды</b>\n\n"
            "/lists — DNS-списки\n"
            "/rules — правила DNS-маршрутизации\n"
            "/routes — IPv4-маршруты\n"
            "/interfaces — системные ID интерфейсов\n"
            "/status — состояние подключения\n"
            "/cancel — отменить ввод\n\n"
            "Домены можно отправлять пачкой: столбиком, через пробел, "
            "запятую или <code>;</code>.\n\n"
            "Все изменения попадают в штатную конфигурацию Keenetic и видны "
            "в веб-панели.",
            keyboard=self._home_keyboard(),
        )

    @staticmethod
    def _bulk_entries_prompt(action: str, *, domains_only: bool = False) -> str:
        operation = "добавления" if action == "add" else "удаления"
        entries = "домены" if domains_only else "домены, IP-адреса или CIDR"
        wildcard_hint = (
            "\nПоддомены учитываются автоматически; <code>*.</code> не нужен."
            if action == "add"
            else ""
        )
        return (
            f"Отправьте {entries} для {operation}. Можно вставить сразу несколько:\n"
            "• столбиком;\n"
            "• через пробел, запятую или <code>;</code>.\n\n"
            "Например, столбиком:\n"
            "<code>ya.ru\n"
            "yandex.ru\n"
            "yandex.com\n"
            "yandex.by\n"
            "yandex.kz\n"
            "yandex.com.tr</code>\n\n"
            "Или одной строкой:\n"
            "<code>ya.ru yandex.ru yandex.com</code>"
            f"{wildcard_hint}\n\n/cancel — отмена"
        )

    @staticmethod
    def _home_keyboard() -> dict[str, Any]:
        return inline_keyboard(
            [
                [("🌐 DNS-списки", "groups"), ("🧭 Правила DNS", "rules")],
                [("🌍 IPv4-маршруты", "routes")],
                [("🔌 Интерфейсы", "interfaces"), ("ℹ️ Статус", "status")],
            ]
        )

    @staticmethod
    def _group_keyboard() -> dict[str, Any]:
        return inline_keyboard(
            [
                [("📄 Показать домены", "g_show")],
                [
                    ("➕ Добавить домен", "g_add"),
                    ("➖ Удалить домен", "g_remove"),
                ],
                [("🧹 Убрать дубликаты", "groups_dedupe")],
                [("🔗 Создать правило", "g_attach")],
                [("🗑 Удалить список", "g_delete")],
                [("← К спискам", "groups"), ("← Меню", "home")],
            ]
        )

    @staticmethod
    def _rule_keyboard(enabled: bool) -> dict[str, Any]:
        return inline_keyboard(
            [
                [(("⏸ Выключить" if enabled else "▶️ Включить"), "r_toggle")],
                [("🔄 Сменить интерфейс", "r_interface")],
                [("🗑 Удалить правило", "r_delete")],
                [("← К правилам", "rules"), ("← Меню", "home")],
            ]
        )

    @staticmethod
    def _routes_keyboard() -> dict[str, Any]:
        return inline_keyboard([[("← К маршрутам", "routes")], [("← Меню", "home")]])

    @staticmethod
    def _ipv4_route_keyboard(enabled: bool) -> dict[str, Any]:
        return inline_keyboard(
            [
                [(("⏸ Выключить" if enabled else "▶️ Включить"), "ip_toggle")],
                [("🔄 Сменить интерфейс", "ip_interface")],
                [("🗑 Удалить маршрут", "ip_delete")],
                [("← К маршрутам", "routes"), ("← Меню", "home")],
            ]
        )

    def _interface_names(self) -> dict[str, str]:
        return {
            interface.ident: interface.description
            for interface in self.router.list_interfaces()
        }

    @staticmethod
    def _format_interface(interface: str, names: dict[str, str]) -> str:
        if not interface:
            return "любой интерфейс"
        description = names.get(interface, "").strip()
        if description and description.casefold() != interface.casefold():
            return f"{interface} ({description})"
        return interface

    @classmethod
    def _format_route_target(
        cls,
        route: DnsRoute | Ipv4Route,
        interface_names: dict[str, str],
    ) -> str:
        if route.interface:
            return cls._format_interface(route.interface, interface_names)
        return route.gateway or "любой интерфейс"

    @staticmethod
    def _format_group_name(
        name: str, groups: dict[str, FqdnGroup]
    ) -> str:
        group = groups.get(name)
        description = (group.description if group else "").strip()
        if description and description.casefold() != name.casefold():
            return f"{name} ({description})"
        return name

    def _find_domain_conflicts(
        self, additions: tuple[str, ...]
    ) -> list[tuple[str, str, str, str]]:
        conflicts: list[tuple[str, str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        groups = self.router.list_groups()
        for addition in additions:
            if not is_domain_entry(addition):
                continue
            for group in groups:
                display_name = group.description or group.name
                for existing in group.entries:
                    if not is_domain_entry(existing):
                        continue
                    if domain_covers(existing, addition):
                        relation = "covered"
                    elif domain_covers(addition, existing):
                        relation = "covers"
                    else:
                        continue
                    key = (addition, existing, group.name)
                    if key not in seen:
                        conflicts.append((addition, existing, display_name, relation))
                        seen.add(key)
        return conflicts

    def _send_domain_conflict_warning(
        self,
        chat_id: int,
        conflicts: list[tuple[str, str, str, str]],
        *,
        confirm_callback: str,
        cancel_callback: str,
    ) -> None:
        lines: list[str] = []
        for addition, existing, group_name, relation in conflicts[:10]:
            action = "уже покрывается" if relation == "covered" else "покрывает"
            lines.append(
                f"• <code>{html.escape(addition)}</code> {action} "
                f"<code>{html.escape(existing)}</code> "
                f"(список «{html.escape(group_name)}»)"
            )
        suffix = (
            f"\n…ещё совпадений: {len(conflicts) - 10}" if len(conflicts) > 10 else ""
        )
        self._send(
            chat_id,
            "⚠️ Похожие домены уже есть в DNS-списках:\n\n"
            + "\n".join(lines)
            + suffix
            + "\n\nДобавлять записи всё равно?",
            keyboard=inline_keyboard(
                [
                    [("Продолжить", confirm_callback)],
                    [("Отменить", cancel_callback)],
                ]
            ),
        )

    @staticmethod
    def _deduplicate_groups(
        groups: list[FqdnGroup],
    ) -> tuple[list[FqdnGroup], list[tuple[str, str]]]:
        all_domains = [
            entry.casefold().rstrip(".")
            for group in groups
            for entry in group.entries
            if is_domain_entry(entry)
        ]
        seen: set[str] = set()
        updates: list[FqdnGroup] = []
        removed: list[tuple[str, str]] = []
        for group in groups:
            kept: list[str] = []
            for entry in group.entries:
                redundant = False
                if is_domain_entry(entry):
                    entry_key = entry.casefold().rstrip(".")
                    redundant = entry_key in seen or any(
                        parent != entry_key and domain_covers(parent, entry_key)
                        for parent in all_domains
                    )
                    if not redundant:
                        seen.add(entry_key)
                if redundant:
                    removed.append((group.description or group.name, entry))
                else:
                    kept.append(entry)
            if tuple(kept) != group.entries:
                updates.append(replace(group, entries=tuple(kept)))
        return updates, removed

    def _rate_limit_ok(self, user_id: int) -> bool:
        now = time.monotonic()
        times = self.request_times[user_id]
        while times and now - times[0] > 60:
            times.popleft()
        if len(times) >= 30:
            return False
        times.append(now)
        return True

    def _validate_group_size(self, entries: tuple[str, ...]) -> None:
        if len(entries) > self.config.max_group_entries:
            raise ValidationError(
                f"В списке получилось {len(entries)} записей, а лимит "
                f"настроен на {self.config.max_group_entries}. Разделите их "
                "на несколько DNS-списков."
            )


def _chat_id(update: dict[str, Any]) -> int | None:
    if "message" in update:
        value = update["message"].get("chat", {}).get("id")
    else:
        value = (
            update.get("callback_query", {})
            .get("message", {})
            .get("chat", {})
            .get("id")
        )
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
