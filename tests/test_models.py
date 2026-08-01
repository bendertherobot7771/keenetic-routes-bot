from __future__ import annotations

import unittest

from keenetic_routes_bot.models import DnsRoute, FqdnGroup, Ipv4Route


class ModelTests(unittest.TestCase):
    def test_fqdn_group_from_stock_rci_shape(self) -> None:
        group = FqdnGroup.from_rci(
            "openai",
            {
                "description": "openai",
                "include": [
                    {"address": "chatgpt.com"},
                    {"address": "openai.com"},
                ],
            },
        )
        self.assertEqual(group.name, "openai")
        self.assertEqual(group.entries, ("chatgpt.com", "openai.com"))

    def test_dns_route_round_trip(self) -> None:
        route = DnsRoute.from_rci(
            {
                "index": "4",
                "group": "openai",
                "interface": "u1Host",
                "auto": True,
                "reject": True,
            }
        )
        self.assertTrue(route.enabled)
        self.assertEqual(
            route.to_rci(),
            {
                "group": "openai",
                "gateway": "",
                "auto": True,
                "reject": True,
                "interface": "u1Host",
                "index": "4",
            },
        )

    def test_ipv4_route_converts_cidr_to_stock_fields(self) -> None:
        route = Ipv4Route(
            index="",
            destination="149.154.160.0/20",
            interface="u1Host",
            comment="telegram",
        )
        data = route.to_rci()
        self.assertEqual(data["network"], "149.154.160.0")
        self.assertEqual(data["mask"], "255.255.240.0")
        self.assertEqual(data["interface"], "u1Host")
        self.assertEqual(
            Ipv4Route.from_rci(
                {
                    "index": "7",
                    "network": data["network"],
                    "mask": data["mask"],
                    "interface": "u1Host",
                    "auto": True,
                }
            ).destination,
            "149.154.160.0/20",
        )


if __name__ == "__main__":
    unittest.main()
