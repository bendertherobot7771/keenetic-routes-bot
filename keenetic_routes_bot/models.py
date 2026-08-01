from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FqdnGroup:
    name: str
    description: str = ""
    entries: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_rci(cls, name: str, data: Any) -> "FqdnGroup":
        raw = data if isinstance(data, dict) else {}
        includes = raw.get("include", [])
        if isinstance(includes, dict):
            includes = [includes]
        entries = tuple(
            str(item["address"])
            for item in includes
            if isinstance(item, dict) and item.get("address")
        )
        return cls(
            name=str(name),
            description=str(raw.get("description") or name),
            entries=entries,
        )


@dataclass(frozen=True)
class DnsRoute:
    index: str
    group: str
    interface: str = ""
    gateway: str = ""
    auto: bool = False
    reject: bool = False
    enabled: bool = True

    @classmethod
    def from_rci(cls, data: Any) -> "DnsRoute":
        raw = data if isinstance(data, dict) else {}
        return cls(
            index=str(raw.get("index", "")),
            group=str(raw.get("group", "")),
            interface=str(raw.get("interface", "")),
            gateway=str(raw.get("gateway", "")),
            auto=bool(raw.get("auto", False)),
            reject=bool(raw.get("reject", False)),
            enabled=not bool(raw.get("disable", False)),
        )

    def to_rci(self, *, include_index: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "group": self.group,
            "gateway": self.gateway,
            "auto": self.auto,
            "reject": self.reject,
        }
        if self.interface:
            data["interface"] = self.interface
        if include_index and self.index:
            data["index"] = self.index
        else:
            data["disable"] = not self.enabled
        return data


@dataclass(frozen=True)
class Ipv4Route:
    index: str
    destination: str
    gateway: str = ""
    interface: str = ""
    auto: bool = True
    reject: bool = False
    enabled: bool = True
    comment: str = ""

    @classmethod
    def from_rci(cls, data: Any) -> "Ipv4Route":
        import ipaddress

        raw = data if isinstance(data, dict) else {}
        if raw.get("default"):
            destination = "0.0.0.0/0"
        elif raw.get("host"):
            destination = f"{raw['host']}/32"
        elif raw.get("network"):
            network = str(raw["network"])
            mask = str(raw.get("mask", "255.255.255.255"))
            destination = str(ipaddress.ip_network(f"{network}/{mask}", strict=False))
        else:
            destination = ""
        return cls(
            index=str(raw.get("index", "")),
            destination=destination,
            gateway=str(raw.get("gateway", "")),
            interface=str(raw.get("interface", "")),
            auto=bool(raw.get("auto", False)),
            reject=bool(raw.get("reject", False)),
            enabled=not bool(raw.get("disable", False)),
            comment=str(raw.get("comment", "")),
        )

    def to_rci(self, *, include_index: bool = False) -> dict[str, Any]:
        import ipaddress

        network = ipaddress.ip_network(self.destination, strict=False)
        data: dict[str, Any] = {
            "gateway": self.gateway,
            "auto": self.auto,
            "reject": self.reject,
            "comment": self.comment,
            "disable": not self.enabled,
        }
        if self.interface:
            data["interface"] = self.interface
        if include_index and self.index:
            data["index"] = self.index
        if network.prefixlen == 0:
            data["default"] = True
        elif network.prefixlen == 32:
            data["host"] = str(network.network_address)
        else:
            data["network"] = str(network.network_address)
            data["mask"] = str(network.netmask)
        return data


@dataclass(frozen=True)
class Interface:
    ident: str
    description: str
    connected: bool = False
