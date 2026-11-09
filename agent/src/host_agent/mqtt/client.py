"""Thin wrapper around paho-mqtt handling availability, birth and commands."""

from __future__ import annotations

import json
import logging
import secrets
import ssl
import threading
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from host_agent.config import MqttConfig
from host_agent.mqtt.discovery import Topics

_LOGGER = logging.getLogger(__name__)

ONLINE = "online"
OFFLINE = "offline"


class MqttClient:
    def __init__(
        self,
        cfg: MqttConfig,
        topics: Topics,
        on_connected: Callable[[], None] | None = None,
        on_ha_online: Callable[[], None] | None = None,
        on_command: Callable[[str, str], None] | None = None,
        with_will: bool = True,
    ) -> None:
        self.cfg = cfg
        self.topics = topics
        self._on_connected = on_connected
        self._on_ha_online = on_ha_online
        self._on_command = on_command
        self._with_will = with_will
        self._connected = threading.Event()
        client_id = cfg.client_id or f"host-agent-{topics.node_id}"
        if not with_will:
            # One-shot CLI commands must not kick the running daemon off the broker
            client_id = f"{client_id}-cli-{secrets.token_hex(3)}"
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )
        if cfg.username:
            self.client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            self.client.tls_set(ca_certs=cfg.ca_certs, cert_reqs=ssl.CERT_REQUIRED)
            if cfg.tls_insecure:
                self.client.tls_insecure_set(True)
        if with_will:
            self.client.will_set(topics.availability, OFFLINE, qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=2, max_delay=120)
        self.client.on_connect = self._handle_connect
        self.client.on_disconnect = self._handle_disconnect
        self.client.on_message = self._handle_message

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        self.client.connect_async(self.cfg.host, self.cfg.port, keepalive=self.cfg.keepalive)
        self.client.loop_start()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def stop(self) -> None:
        if self._connected.is_set() and self._with_will:
            info = self.client.publish(self.topics.availability, OFFLINE, qos=1, retain=True)
            info.wait_for_publish(timeout=5)
        self.client.disconnect()
        self.client.loop_stop()

    # -- publishing -------------------------------------------------------- #

    def publish(self, topic: str, payload: str | bytes, retain: bool = True, qos: int = 1) -> None:
        self.client.publish(topic, payload, qos=qos, retain=retain)

    def publish_json(self, topic: str, data: dict[str, Any], retain: bool = True) -> None:
        self.publish(topic, json.dumps(data, ensure_ascii=False, default=str), retain=retain)

    def flush(self, timeout: float = 10) -> None:
        """Best effort wait until queued messages are sent (used by one-shot commands)."""
        info = self.client.publish(f"{self.topics.base}/flush", b"", qos=1, retain=False)
        info.wait_for_publish(timeout=timeout)

    # -- callbacks --------------------------------------------------------- #

    def _handle_connect(
        self, client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None
    ) -> None:
        if reason_code.is_failure:
            _LOGGER.error("MQTT connection refused: %s", reason_code)
            return
        _LOGGER.info("Connected to MQTT broker %s:%s", self.cfg.host, self.cfg.port)
        if self._with_will:
            client.publish(self.topics.availability, ONLINE, qos=1, retain=True)
        client.subscribe([(self.topics.ha_status, 1), (self.topics.command("+"), 1)])
        self._connected.set()
        if self._on_connected:
            self._on_connected()

    def _handle_disconnect(
        self, _client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None
    ) -> None:
        self._connected.clear()
        if reason_code != 0:
            _LOGGER.warning("Disconnected from MQTT broker (%s), reconnecting", reason_code)

    def _handle_message(self, _client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode("utf-8", errors="replace")
        if msg.topic == self.topics.ha_status:
            if payload == ONLINE and self._on_ha_online:
                _LOGGER.info("Home Assistant is online, republishing discovery")
                self._on_ha_online()
            return
        prefix = self.topics.command("")
        if msg.topic.startswith(prefix) and self._on_command:
            self._on_command(msg.topic[len(prefix) :], payload)
