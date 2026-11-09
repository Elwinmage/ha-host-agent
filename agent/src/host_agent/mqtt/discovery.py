"""Home Assistant MQTT device-based discovery payloads (HA >= 2025.10)."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from host_agent import PROJECT_URL, PROTOCOL_VERSION, __version__
from host_agent.model import EntitySpec

STATE_TEMPLATE = "{{ value_json.state }}"
ATTRS_TEMPLATE = "{{ value_json.attrs | tojson }}"
PAYLOAD_PRESS = "PRESS"


@dataclass
class Topics:
    discovery_prefix: str
    base_topic: str
    node_id: str

    @property
    def base(self) -> str:
        return f"{self.base_topic}/{self.node_id}"

    @property
    def discovery(self) -> str:
        return f"{self.discovery_prefix}/device/{self.node_id}/config"

    @property
    def availability(self) -> str:
        return f"{self.base}/availability"

    @property
    def ha_status(self) -> str:
        return f"{self.discovery_prefix}/status"

    def state(self, key: str) -> str:
        return f"{self.base}/state/{key}"

    def command(self, name: str) -> str:
        return f"{self.base}/cmd/{name}"


@dataclass
class DeviceInfo:
    node_id: str
    name: str
    model: str | None = None
    mac: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        dev: dict[str, Any] = {
            "ids": [f"host_agent_{self.node_id}"],
            "name": self.name,
            "mf": "ha-host-agent",
            "mdl": self.model or "Linux host",
            "sw": __version__,
        }
        if self.mac:
            # Shared with the HA integration so both config entries land on one device
            dev["cns"] = [["mac", self.mac]]
        return dev


def detect_device_info(node_id: str, name: str) -> DeviceInfo:
    model = None
    for path in ("/proc/device-tree/model", "/sys/class/dmi/id/product_name"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore").strip("\x00\n ")
        except OSError:
            continue
        if text:
            model = text
            break
    return DeviceInfo(node_id=node_id, name=name, model=model, mac=_primary_mac())


def _primary_mac() -> str | None:
    """MAC address of the interface carrying the default route."""
    try:
        with open("/proc/net/route", encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                parts = line.split()
                if len(parts) > 1 and parts[1] == "00000000":
                    mac = Path(f"/sys/class/net/{parts[0]}/address").read_text().strip()
                    if mac and mac != "00:00:00:00:00:00":
                        return mac.lower()
    except (OSError, StopIteration):
        pass
    return None


def component(spec: EntitySpec, topics: Topics) -> dict[str, Any]:
    node = topics.node_id
    comp: dict[str, Any] = {
        "p": spec.platform,
        "unique_id": f"host_agent_{node}_{spec.key}",
        "default_entity_id": f"{spec.platform}.{node}_{spec.key}",
        "name": spec.name,
    }
    if spec.platform == "button":
        comp["command_topic"] = topics.command(spec.key)
        comp["payload_press"] = PAYLOAD_PRESS
    else:
        state_topic = topics.state(spec.key)
        comp.update(
            {
                "state_topic": state_topic,
                "value_template": STATE_TEMPLATE,
                "json_attributes_topic": state_topic,
                "json_attributes_template": ATTRS_TEMPLATE,
            }
        )
        if spec.platform == "binary_sensor":
            comp["payload_on"] = "ON"
            comp["payload_off"] = "OFF"
    optional = {
        "device_class": spec.device_class,
        "unit_of_measurement": spec.unit,
        "state_class": spec.state_class,
        "entity_category": spec.entity_category,
        "icon": spec.icon,
        "suggested_display_precision": spec.precision,
    }
    comp.update({k: v for k, v in optional.items() if v is not None})
    return comp


def discovery_payload(
    device: DeviceInfo,
    specs: list[EntitySpec],
    topics: Topics,
    removed: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the single device discovery document.

    `removed` maps keys to platforms of components to delete: HA removes a
    component when its entry only contains the platform.
    """
    cmps = {spec.key: component(spec, topics) for spec in specs}
    for key, platform in (removed or {}).items():
        cmps.setdefault(key, {"p": platform})
    return {
        "dev": device.as_payload(),
        "o": {"name": "ha-host-agent", "sw": __version__, "url": PROJECT_URL},
        "avty_t": topics.availability,
        "qos": 1,
        "cmps": cmps,
    }


def state_payload(state: Any, attrs: dict[str, Any] | None = None) -> str:
    return json.dumps({"state": state, "attrs": attrs or {}}, ensure_ascii=False, default=str)


def hostname() -> str:
    return socket.gethostname().split(".")[0]


__all__ = [
    "PAYLOAD_PRESS",
    "PROTOCOL_VERSION",
    "DeviceInfo",
    "Topics",
    "component",
    "detect_device_info",
    "discovery_payload",
    "hostname",
    "state_payload",
]
