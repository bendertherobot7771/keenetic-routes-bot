from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(RuntimeError):
    pass


def load_env_file(path: str | os.PathLike[str]) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{env_path}:{line_number}: ожидалась строка KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_users: frozenset[int]
    rci_url: str = "http://127.0.0.1:79/rci"
    rci_token: str = ""
    rci_token_header: str = "Authorization"
    rci_token_prefix: str = "Bearer "
    allow_non_loopback_rci: bool = False
    default_interface: str = ""
    log_level: int = logging.INFO
    log_file: str = ""
    private_chats_only: bool = True
    poll_timeout: int = 25
    request_timeout: int = 15
    max_group_entries: int = 300

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "Config":
        if env_file:
            load_env_file(env_file)
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token or ":" not in token:
            raise ConfigError("BOT_TOKEN отсутствует или имеет неверный формат.")
        allowed_users = _parse_user_ids(os.getenv("ALLOWED_USERS", ""))
        rci_url = os.getenv("RCI_URL", "http://127.0.0.1:79/rci").rstrip("/")
        allow_non_loopback = _parse_bool(os.getenv("ALLOW_NON_LOOPBACK_RCI", "false"))
        _validate_rci_url(rci_url, allow_non_loopback=allow_non_loopback)
        log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, log_level_name, None)
        if not isinstance(log_level, int):
            raise ConfigError(f"Неизвестный LOG_LEVEL: {log_level_name}")
        default_interface = os.getenv("DEFAULT_INTERFACE", "").strip()
        if default_interface and not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,64}", default_interface
        ):
            raise ConfigError("DEFAULT_INTERFACE имеет неверный формат.")
        token_header = os.getenv("RCI_TOKEN_HEADER", "Authorization").strip()
        if not token_header or not re.fullmatch(
            r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", token_header
        ):
            raise ConfigError("RCI_TOKEN_HEADER имеет неверный формат.")
        return cls(
            bot_token=token,
            allowed_users=allowed_users,
            rci_url=rci_url,
            rci_token=os.getenv("RCI_TOKEN", "").strip(),
            rci_token_header=token_header,
            rci_token_prefix=os.getenv("RCI_TOKEN_PREFIX", "Bearer "),
            allow_non_loopback_rci=allow_non_loopback,
            default_interface=default_interface,
            log_level=log_level,
            log_file=os.getenv("LOG_FILE", "").strip(),
            private_chats_only=_parse_bool(os.getenv("PRIVATE_CHATS_ONLY", "true")),
            poll_timeout=_parse_int("POLL_TIMEOUT", 25, minimum=5, maximum=50),
            request_timeout=_parse_int("REQUEST_TIMEOUT", 15, minimum=3, maximum=120),
            max_group_entries=_parse_int(
                "MAX_GROUP_ENTRIES", 300, minimum=1, maximum=10000
            ),
        )


def _parse_user_ids(value: str) -> frozenset[int]:
    try:
        result = frozenset(
            int(item.strip()) for item in value.split(",") if item.strip()
        )
    except ValueError as exc:
        raise ConfigError("ALLOWED_USERS должен содержать числовые ID.") from exc
    if not result:
        raise ConfigError("ALLOWED_USERS не должен быть пустым.")
    return result


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Некорректное логическое значение: {value}")


def _parse_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом.") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} должен быть от {minimum} до {maximum}.")
    return value


def _validate_rci_url(value: str, *, allow_non_loopback: bool) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ConfigError("RCI_URL должен быть локальным HTTP URL.")
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.hostname not in loopback_hosts and not allow_non_loopback:
        raise ConfigError(
            "RCI_URL должен указывать на loopback. Для удалённого RCI явно "
            "установите ALLOW_NON_LOOPBACK_RCI=true."
        )
    if not parsed.path.endswith("/rci"):
        raise ConfigError("RCI_URL должен оканчиваться на /rci.")
