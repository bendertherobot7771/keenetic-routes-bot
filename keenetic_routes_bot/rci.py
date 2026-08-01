from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote

from .models import DnsRoute, FqdnGroup, Interface, Ipv4Route


class RciError(RuntimeError):
    pass


Transport = Callable[[str, str, bytes | None, dict[str, str], int], Any]


class KeeneticRciClient:
    """Client for the local Keenetic RCI used by the stock web interface."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:79/rci",
        *,
        token: str = "",
        token_header: str = "Authorization",
        token_prefix: str = "Bearer ",
        timeout: int = 15,
        transport: Transport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or _urllib_transport
        self.logger = logger or logging.getLogger(__name__)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "keenetic-routes-bot/0.1",
        }
        if token:
            self.headers[token_header] = f"{token_prefix}{token}"

    def version(self) -> dict[str, Any]:
        data = self._get("show.version")
        return data if isinstance(data, dict) else {}

    def list_groups(self) -> list[FqdnGroup]:
        data = self._get("show.sc.object-group.fqdn")
        if not isinstance(data, dict):
            return []
        return sorted(
            (FqdnGroup.from_rci(name, value) for name, value in data.items()),
            key=lambda group: group.name.casefold(),
        )

    def get_group(self, name: str) -> FqdnGroup | None:
        return next((group for group in self.list_groups() if group.name == name), None)

    def save_group(self, group: FqdnGroup, *, replace: bool = True) -> None:
        queries: list[dict[str, Any]] = []
        if replace and self.get_group(group.name) is not None:
            queries.append(
                {
                    "path": "object-group.fqdn",
                    "data": {group.name: {"include": {"no": True}}},
                }
            )
        queries.append(
            {
                "path": "object-group.fqdn",
                "data": {
                    group.name: {
                        "description": group.description or group.name,
                        "include": [{"address": address} for address in group.entries],
                    }
                },
            }
        )
        self._write(queries)

    def delete_group(self, name: str) -> None:
        self._write(
            [
                {
                    "path": "object-group.fqdn",
                    "data": {"name": name, "no": True},
                }
            ]
        )

    def list_dns_routes(self) -> list[DnsRoute]:
        raw = self._get("show.sc.dns-proxy.route")
        return [
            DnsRoute.from_rci(item) for item in _as_list(raw) if isinstance(item, dict)
        ]

    def save_dns_route(self, route: DnsRoute) -> None:
        queries: list[dict[str, Any]] = []
        if route.index:
            queries.append(
                {
                    "path": "dns-proxy.route",
                    "data": {
                        "disable": {
                            "index": route.index,
                            "no": route.enabled,
                        }
                    },
                }
            )
        queries.append(
            {
                "path": "dns-proxy.route",
                "data": route.to_rci(include_index=True),
            }
        )
        self._write(queries)

    def delete_dns_route(self, index: str) -> None:
        self._write(
            [
                {
                    "path": "dns-proxy.route",
                    "data": {"index": index, "no": True},
                }
            ]
        )

    def set_dns_route_enabled(self, index: str, enabled: bool) -> None:
        self._write(
            [
                {
                    "path": "dns-proxy.route",
                    "data": {"disable": {"index": index, "no": enabled}},
                }
            ]
        )

    def list_ipv4_routes(self) -> list[Ipv4Route]:
        raw = self._get("show.sc.ip.route")
        return [
            Ipv4Route.from_rci(item) for item in _as_list(raw) if isinstance(item, dict)
        ]

    def add_ipv4_routes(self, routes: Iterable[Ipv4Route]) -> None:
        queries = [
            {"path": "ip.route", "data": route.to_rci(include_index=False)}
            for route in routes
        ]
        if queries:
            self._write(queries)

    def delete_ipv4_route(self, index: str) -> None:
        self._write([{"path": "ip.route", "data": {"index": index, "no": True}}])

    def set_ipv4_route_enabled(self, index: str, enabled: bool) -> None:
        self._write(
            [
                {
                    "path": "ip.route",
                    "data": {"disable": {"index": index, "no": enabled}},
                }
            ]
        )

    def list_interfaces(self) -> list[Interface]:
        raw = self._get("show.interface")
        if isinstance(raw, dict) and isinstance(raw.get("interface"), (dict, list)):
            raw = raw["interface"]
        if isinstance(raw, list):
            items = [
                (str(item.get("id") or item.get("interface") or ""), item)
                for item in raw
                if isinstance(item, dict)
            ]
        elif isinstance(raw, dict):
            items = list(raw.items())
        else:
            items = []
        result: list[Interface] = []
        for ident, data in items:
            if not ident or not isinstance(data, dict):
                continue
            description = str(
                data.get("description") or data.get("global") or data.get("id") or ident
            )
            state = str(data.get("state", "")).lower()
            result.append(
                Interface(
                    ident=str(ident),
                    description=description,
                    connected=state in {"up", "connected"},
                )
            )
        return sorted(result, key=lambda item: item.ident.casefold())

    def _get(self, path: str) -> Any:
        url_path = "/".join(quote(part, safe="") for part in path.split("."))
        return self._request("GET", f"{self.base_url}/{url_path}", None)

    def _write(self, queries: list[dict[str, Any]]) -> Any:
        payload = [
            *queries,
            {"path": "system.configuration.save", "data": {}},
        ]
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request("POST", f"{self.base_url}/", body)

    def _request(self, method: str, url: str, body: bytes | None) -> Any:
        try:
            response = self.transport(
                method, url, body, dict(self.headers), self.timeout
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RciError(
                    "RCI вернул 401. Для KeeneticOS с обязательными токенами "
                    "укажите RCI_TOKEN."
                ) from exc
            raise RciError(f"RCI HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RciError(f"RCI недоступен: {exc}") from exc
        _raise_on_rci_error(response)
        return response


def _urllib_transport(
    method: str,
    url: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout: int,
) -> Any:
    request = urllib.request.Request(
        url=url,
        data=body,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RciError("RCI вернул некорректный JSON.") from exc


def _raise_on_rci_error(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        if value.get("status") == "error":
            message = str(value.get("message") or "неизвестная ошибка RCI")
            code = str(value.get("code") or "")
            location = f" ({path})" if path else ""
            suffix = f", код {code}" if code else ""
            raise RciError(f"{message}{suffix}{location}")
        for key, nested in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            _raise_on_rci_error(nested, child_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _raise_on_rci_error(nested, f"{path}[{index}]")


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []
