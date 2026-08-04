from __future__ import annotations

import ipaddress
import re
import shlex
from collections.abc import Iterable

from .models import Ipv4Route


class ValidationError(ValueError):
    pass


_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def normalize_group_name(value: str) -> str:
    name = " ".join(value.strip().split())
    if not name:
        raise ValidationError("Имя списка не может быть пустым.")
    if len(name) > 64:
        raise ValidationError("Имя списка должно быть не длиннее 64 символов.")
    if any(ord(char) < 32 for char in name) or "/" in name:
        raise ValidationError("Имя списка содержит недопустимые символы.")
    return name


def normalize_interface(value: str) -> str:
    interface = value.strip()
    if not _INTERFACE_RE.fullmatch(interface):
        raise ValidationError(
            "Некорректный системный идентификатор интерфейса. Пример: u1Host."
        )
    return interface


def normalize_entry(value: str) -> str:
    entry = value.strip().rstrip(".")
    if not entry:
        raise ValidationError("Пустая строка.")
    if any(char.isspace() for char in entry):
        raise ValidationError(f"«{value}»: пробелы внутри записи недопустимы.")
    if "://" in entry or "/" in entry and not _looks_like_network(entry):
        raise ValidationError(
            f"«{value}»: укажите домен или IP/CIDR без протокола и пути."
        )
    try:
        return str(ipaddress.ip_network(entry, strict=False))
    except ValueError:
        pass
    try:
        return str(ipaddress.ip_address(entry))
    except ValueError:
        pass
    if entry.startswith("*."):
        entry = entry[2:]
    try:
        ascii_domain = entry.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValidationError(f"«{value}»: некорректное доменное имя.") from exc
    if len(ascii_domain) > 253 or "." not in ascii_domain:
        raise ValidationError(f"«{value}»: некорректное доменное имя.")
    labels = ascii_domain.split(".")
    if any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        raise ValidationError(f"«{value}»: некорректное доменное имя.")
    return ascii_domain


def parse_entries(text: str) -> tuple[str, ...]:
    raw_entries: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw_entries.extend(
            candidate for candidate in re.split(r"[\s,;]+", line) if candidate
        )
    if not raw_entries:
        raise ValidationError("Не найдено ни одной записи.")
    normalized: list[str] = []
    errors: list[str] = []
    for entry in raw_entries:
        try:
            normalized.append(normalize_entry(entry))
        except ValidationError as exc:
            errors.append(str(exc))
    if errors:
        preview = "\n".join(f"• {error}" for error in errors[:8])
        suffix = "\n…" if len(errors) > 8 else ""
        raise ValidationError(f"Исправьте записи:\n{preview}{suffix}")
    return tuple(dict.fromkeys(normalized))


def parse_ipv4_routes(
    text: str,
    *,
    default_interface: str = "",
) -> tuple[Ipv4Route, ...]:
    routes: list[Ipv4Route] = []
    errors: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            errors.append(f"строка {line_number}: {exc}")
            continue
        if not parts:
            continue
        try:
            network = ipaddress.ip_network(parts[0], strict=False)
            if network.version != 4:
                raise ValidationError("нужна IPv4-сеть")
            interface = parts[1] if len(parts) >= 2 else default_interface
            if not interface:
                raise ValidationError("не указан интерфейс")
            interface = normalize_interface(interface)
            comment = " ".join(parts[2:]) if len(parts) > 2 else ""
            if len(comment) > 64:
                raise ValidationError("описание длиннее 64 символов")
            routes.append(
                Ipv4Route(
                    index="",
                    destination=str(network),
                    interface=interface,
                    auto=True,
                    comment=comment,
                )
            )
        except (ValueError, ValidationError) as exc:
            errors.append(f"строка {line_number}: {exc}")
    if errors:
        raise ValidationError(
            "Не удалось разобрать маршруты:\n"
            + "\n".join(f"• {error}" for error in errors[:8])
        )
    if not routes:
        raise ValidationError("Не найдено ни одного IPv4-маршрута.")
    return tuple(routes)


def merge_entries(current: Iterable[str], additions: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*current, *additions]))


def remove_entries(
    current: Iterable[str], removals: Iterable[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    removal_set = set(removals)
    current_tuple = tuple(current)
    remaining = tuple(entry for entry in current_tuple if entry not in removal_set)
    missing = tuple(entry for entry in removal_set if entry not in current_tuple)
    return remaining, missing


def is_domain_entry(value: str) -> bool:
    """Return whether a normalized FQDN-group entry is a domain name."""
    try:
        ipaddress.ip_network(value, strict=False)
        return False
    except ValueError:
        return True


def domain_covers(parent: str, candidate: str) -> bool:
    """Return whether Keenetic's wildcard matching for parent covers candidate."""
    parent = parent.casefold().rstrip(".")
    candidate = candidate.casefold().rstrip(".")
    return candidate == parent or candidate.endswith(f".{parent}")


def _looks_like_network(value: str) -> bool:
    try:
        ipaddress.ip_network(value, strict=False)
        return True
    except ValueError:
        return False
