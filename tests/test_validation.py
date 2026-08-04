from __future__ import annotations

import unittest

from keenetic_routes_bot.validation import (
    ValidationError,
    domain_covers,
    is_domain_entry,
    normalize_entry,
    normalize_group_name,
    parse_entries,
    parse_ipv4_routes,
    remove_entries,
)


class ValidationTests(unittest.TestCase):
    def test_normalizes_domains_wildcards_idn_and_networks(self) -> None:
        self.assertEqual(normalize_entry("*.Example.COM."), "example.com")
        self.assertEqual(normalize_entry("пример.рф"), "xn--e1afmkfd.xn--p1ai")
        self.assertEqual(normalize_entry("192.0.2.19/24"), "192.0.2.0/24")
        self.assertEqual(normalize_entry("2001:db8::1"), "2001:db8::1/128")

    def test_rejects_urls_and_paths(self) -> None:
        for value in ("https://example.com", "example.com/path", "localhost"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    normalize_entry(value)

    def test_parse_entries_deduplicates_and_ignores_comments(self) -> None:
        entries = parse_entries(
            """
            # comment
            example.com
            EXAMPLE.COM;
            192.0.2.0/24, api.example.com
            """
        )
        self.assertEqual(entries, ("example.com", "192.0.2.0/24", "api.example.com"))

    def test_group_name_limits(self) -> None:
        self.assertEqual(normalize_group_name("  My   routes  "), "My routes")
        with self.assertRaises(ValidationError):
            normalize_group_name("bad/name")

    def test_parse_ipv4_routes(self) -> None:
        routes = parse_ipv4_routes(
            "149.154.160.1/20 u1Host telegram\n"
            '31.13.64.0/18 u1Host "instagram facebook"'
        )
        self.assertEqual(routes[0].destination, "149.154.160.0/20")
        self.assertEqual(routes[0].interface, "u1Host")
        self.assertEqual(routes[1].comment, "instagram facebook")

    def test_parse_ipv4_routes_uses_default_interface(self) -> None:
        routes = parse_ipv4_routes("203.0.113.0/24", default_interface="Wireguard0")
        self.assertEqual(routes[0].interface, "Wireguard0")

    def test_remove_entries_reports_missing(self) -> None:
        remaining, missing = remove_entries(
            ("a.example", "b.example"), ("b.example", "c.example")
        )
        self.assertEqual(remaining, ("a.example",))
        self.assertEqual(missing, ("c.example",))

    def test_detects_domain_wildcard_coverage(self) -> None:
        self.assertTrue(domain_covers("yandex.ru", "search.yandex.ru"))
        self.assertTrue(domain_covers("YANDEX.RU", "yandex.ru"))
        self.assertFalse(domain_covers("notyandex.ru", "yandex.ru"))
        self.assertFalse(domain_covers("search.yandex.ru", "yandex.ru"))

    def test_distinguishes_domains_from_ip_entries(self) -> None:
        self.assertTrue(is_domain_entry("example.com"))
        self.assertFalse(is_domain_entry("192.0.2.1"))
        self.assertFalse(is_domain_entry("192.0.2.0/24"))
        self.assertFalse(is_domain_entry("2001:db8::/32"))


if __name__ == "__main__":
    unittest.main()
