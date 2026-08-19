from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, *, timeout: int = 15) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def get_updates(
        self, *, offset: int | None = None, timeout: int = 25
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(
                ["message", "callback_query"], separators=(",", ":")
            ),
        }
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload, timeout=timeout + 10)
        return result if isinstance(result, list) else []

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": str(disable_web_page_preview).lower(),
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(
                reply_markup, ensure_ascii=False, separators=(",", ":")
            )
        result = self.call("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": str(disable_web_page_preview).lower(),
        }
        payload["reply_markup"] = json.dumps(
            reply_markup or {"inline_keyboard": []},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = self.call("editMessageText", payload)
        return result if isinstance(result, dict) else {}

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": str(show_alert).lower(),
        }
        if text:
            payload["text"] = text[:200]
        self.call("answerCallbackQuery", payload)

    def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> Any:
        body = urllib.parse.urlencode(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "keenetic-routes-bot/0.1",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise TelegramError(
                f"Telegram API вернул HTTP {exc.code} для {method}."
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TelegramError(
                f"Telegram API недоступен при вызове {method}: {reason}"
            ) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramError("Telegram API вернул некорректный JSON.") from exc
        if not decoded.get("ok"):
            description = str(decoded.get("description") or "неизвестная ошибка")
            raise TelegramError(f"Telegram API: {description}")
        return decoded.get("result")


def inline_keyboard(
    rows: list[list[tuple[str, str]]],
) -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {"text": text, "callback_data": callback_data}
                for text, callback_data in row
            ]
            for row in rows
        ]
    }
