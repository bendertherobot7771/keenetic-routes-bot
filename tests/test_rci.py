from __future__ import annotations

import json
import unittest
from typing import Any

from keenetic_routes_bot.models import DnsRoute, FqdnGroup, Ipv4Route
from keenetic_routes_bot.rci import KeeneticRciClient, RciError


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, dict[str, str], int]] = []
        self.get_responses: dict[str, Any] = {}
        self.post_response: Any = [{"status": [{"status": "message"}]}]

    def __call__(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: int,
    ) -> Any:
        decoded = json.loads(body.decode("utf-8")) if body else None
        self.calls.append((method, url, decoded, headers, timeout))
        if method == "GET":
            return self.get_responses[url]
        return self.post_response


class RciClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = FakeTransport()
        self.client = KeeneticRciClient(
            "http://127.0.0.1:79/rci",
            transport=self.transport,
        )

    def test_reads_stock_fqdn_groups_and_routes(self) -> None:
        self.transport.get_responses[
            "http://127.0.0.1:79/rci/show/sc/object-group/fqdn"
        ] = {
            "openai": {
                "description": "OpenAI",
                "include": [{"address": "openai.com"}],
            }
        }
        self.transport.get_responses[
            "http://127.0.0.1:79/rci/show/sc/dns-proxy/route"
        ] = [
            {
                "index": "1",
                "group": "openai",
                "interface": "u1Host",
                "auto": True,
            }
        ]
        groups = self.client.list_groups()
        routes = self.client.list_dns_routes()
        self.assertEqual(groups[0].description, "OpenAI")
        self.assertEqual(routes[0].interface, "u1Host")

    def test_replace_group_uses_same_payload_as_stock_ui_and_saves(self) -> None:
        groups_url = "http://127.0.0.1:79/rci/show/sc/object-group/fqdn"
        self.transport.get_responses[groups_url] = {
            "openai": {
                "description": "openai",
                "include": [{"address": "old.example"}],
            }
        }
        self.client.save_group(
            FqdnGroup(
                name="openai",
                description="openai",
                entries=("openai.com", "chatgpt.com"),
            )
        )
        method, url, payload, _, _ = self.transport.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://127.0.0.1:79/rci/")
        self.assertEqual(
            payload,
            [
                {
                    "object-group": {"fqdn": {"openai": {"include": {"no": True}}}}
                },
                {
                    "object-group": {
                        "fqdn": {
                            "openai": {
                                "description": "openai",
                                "include": [
                                    {"address": "openai.com"},
                                    {"address": "chatgpt.com"},
                                ],
                            }
                        }
                    }
                },
                {"system": {"configuration": {"save": {}}}},
            ],
        )

    def test_creates_dns_route_and_saves(self) -> None:
        self.client.save_dns_route(
            DnsRoute(
                index="",
                group="openai",
                interface="u1Host",
                auto=True,
                reject=True,
            )
        )
        payload = self.transport.calls[-1][2]
        self.assertEqual(
            payload[0],
            {
                "dns-proxy": {"route": {
                    "group": "openai",
                    "gateway": "",
                    "auto": True,
                    "reject": True,
                    "interface": "u1Host",
                    "disable": False,
                }},
            },
        )
        self.assertEqual(
            payload[-1],
            {"system": {"configuration": {"save": {}}}},
        )

    def test_adds_ipv4_route_in_native_shape(self) -> None:
        self.client.add_ipv4_routes(
            [
                Ipv4Route(
                    index="",
                    destination="31.13.64.0/18",
                    interface="u1Host",
                    auto=True,
                    comment="instagram",
                )
            ]
        )
        data = self.transport.calls[-1][2][0]["ip"]["route"]
        self.assertEqual(data["network"], "31.13.64.0")
        self.assertEqual(data["mask"], "255.255.192.0")
        self.assertEqual(data["comment"], "instagram")
        self.assertTrue(
            all("path" not in item for item in self.transport.calls[-1][2])
        )

    def test_raises_for_nested_rci_error(self) -> None:
        self.transport.get_responses["http://127.0.0.1:79/rci/show/version"] = {
            "status": "error",
            "code": "123",
            "message": "failed",
        }
        with self.assertRaisesRegex(RciError, "failed"):
            self.client.version()

    def test_token_header_is_configurable(self) -> None:
        transport = FakeTransport()
        transport.get_responses["http://127.0.0.1:79/rci/show/version"] = {
            "release": "5.2"
        }
        client = KeeneticRciClient(
            token="secret",
            token_header="X-Keenetic-Token",
            token_prefix="",
            transport=transport,
        )
        client.version()
        self.assertEqual(transport.calls[-1][3]["X-Keenetic-Token"], "secret")


if __name__ == "__main__":
    unittest.main()
