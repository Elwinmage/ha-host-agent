"""Per host runtime: agent state from MQTT and lifecycle operations over SSH."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from . import remote
from .agent_config import build_config
from .bundle import AgentBundle
from .const import (
    ADMIN_USER,
    AUTO_UPDATE_DELAY,
    AUTO_UPDATE_RETRY,
    CONF_AUTO_UPDATE,
    CONF_DEPLOYED_VERSION,
    CONF_HOST_KEY,
    CONF_MQTT_PASSWORD,
    CONF_NODE_ID,
    CONF_PRIVATE_KEY,
    DEFAULT_BASE_TOPIC,
    DOMAIN,
    HEALTH_TIMEOUT,
    ISSUE_HOST_KEY,
    ISSUE_PROTOCOL,
    PROTOCOL_VERSION,
)
from .ssh import HostKeyMismatch, InvalidAuth, SshClient, SshError

_LOGGER = logging.getLogger(__name__)


def is_newer(candidate: str, installed: str | None) -> bool:
    if not installed:
        return True
    try:
        return AwesomeVersion(candidate) > AwesomeVersion(installed)
    except AwesomeVersionException:
        return candidate != installed


@dataclass
class AgentState:
    online: bool | None = None
    version: str | None = None
    protocol: int | None = None
    listeners: list[Callable[[], None]] = field(default_factory=list)


class HostAgentRuntime:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, bundle: AgentBundle) -> None:
        self.hass = hass
        self.entry = entry
        self.bundle = bundle
        self.agent = AgentState()
        self.busy = False
        self.progress: int | None = None
        self._lock = asyncio.Lock()
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._version_waiters: list[tuple[str, asyncio.Future[None]]] = []
        self._last_auto_attempt: float | None = None
        self._auto_timer: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def node_id(self) -> str:
        return str(self.entry.data[CONF_NODE_ID])

    @property
    def base(self) -> str:
        return f"{DEFAULT_BASE_TOPIC}/{self.node_id}"

    @property
    def installed_version(self) -> str | None:
        return self.agent.version or self.entry.data.get(CONF_DEPLOYED_VERSION)

    @property
    def update_available(self) -> bool:
        return is_newer(self.bundle.version, self.installed_version)

    def ssh(self) -> SshClient:
        return SshClient(
            self.entry.data[CONF_HOST],
            self.entry.data[CONF_PORT],
            ADMIN_USER,
            private_key=self.entry.data[CONF_PRIVATE_KEY],
            host_key=self.entry.data[CONF_HOST_KEY],
        )

    # ------------------------------------------------------------------ #
    # MQTT tracking
    # ------------------------------------------------------------------ #

    async def async_start(self) -> None:
        self._unsubscribe.append(
            await mqtt.async_subscribe(self.hass, f"{self.base}/availability", self._on_availability, qos=1)
        )
        self._unsubscribe.append(
            await mqtt.async_subscribe(self.hass, f"{self.base}/state/agent_version", self._on_version, qos=1)
        )
        self._schedule_auto_update(AUTO_UPDATE_DELAY)

    async def async_stop(self) -> None:
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()
        if self._auto_timer:
            self._auto_timer()
            self._auto_timer = None

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.agent.listeners.append(listener)
        return lambda: self.agent.listeners.remove(listener)

    @callback
    def _notify(self) -> None:
        for listener in list(self.agent.listeners):
            listener()

    @callback
    def _on_availability(self, msg: mqtt.ReceiveMessage) -> None:
        self.agent.online = msg.payload == "online"
        self._notify()
        self._resolve_waiters()

    @callback
    def _on_version(self, msg: mqtt.ReceiveMessage) -> None:
        try:
            data = json.loads(msg.payload)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.agent.version = data.get("state")
        protocol = (data.get("attrs") or {}).get("protocol_version")
        self.agent.protocol = protocol if isinstance(protocol, int) else None
        self._update_protocol_issue()
        self._notify()
        self._resolve_waiters()

    @callback
    def _resolve_waiters(self) -> None:
        for version, future in list(self._version_waiters):
            if self.agent.online and self.agent.version == version and not future.done():
                future.set_result(None)

    async def _wait_for_version(self, version: str, timeout: float) -> bool:
        future: asyncio.Future[None] = self.hass.loop.create_future()
        entry = (version, future)
        self._version_waiters.append(entry)
        self._resolve_waiters()
        try:
            await asyncio.wait_for(future, timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self._version_waiters.remove(entry)

    @callback
    def _update_protocol_issue(self) -> None:
        issue_id = f"{ISSUE_PROTOCOL}_{self.entry.entry_id}"
        if self.agent.protocol is not None and self.agent.protocol != PROTOCOL_VERSION:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_PROTOCOL,
                translation_placeholders={
                    "host": self.entry.title,
                    "agent": str(self.agent.protocol),
                    "integration": str(PROTOCOL_VERSION),
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # ------------------------------------------------------------------ #
    # Operations
    # ------------------------------------------------------------------ #

    def _handle_ssh_error(self, err: SshError) -> HomeAssistantError:
        if isinstance(err, HostKeyMismatch):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_HOST_KEY}_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_HOST_KEY,
                translation_placeholders={"host": self.entry.title},
            )
            self.entry.async_start_reauth(self.hass)
            return HomeAssistantError(f"{self.entry.title}: SSH host key changed, operation refused")
        if isinstance(err, InvalidAuth):
            self.entry.async_start_reauth(self.hass)
        return HomeAssistantError(f"{self.entry.title}: {err}")

    def _set_progress(self, busy: bool, progress: int | None = None) -> None:
        self.busy = busy
        self.progress = progress
        self._notify()

    async def async_install(self) -> None:
        """Deploy the bundled agent, check it reports back over MQTT, roll back otherwise."""
        if self._lock.locked():
            raise HomeAssistantError(f"{self.entry.title}: an operation is already running")
        async with self._lock:
            target = self.bundle.version
            config = build_config(dict(self.entry.data), dict(self.entry.options))
            self._set_progress(True, 10)
            try:
                async with self.ssh() as ssh:
                    self._set_progress(True, 30)
                    await remote.install(
                        ssh, self.bundle, config, self.entry.data.get(CONF_MQTT_PASSWORD) or ""
                    )
                    self._set_progress(True, 70)
                    if not await self._wait_for_version(target, HEALTH_TIMEOUT):
                        _LOGGER.warning(
                            "%s: agent %s did not report over MQTT, rolling back", self.entry.title, target
                        )
                        await remote.rollback(ssh)
                        raise HomeAssistantError(
                            f"{self.entry.title}: agent {target} did not come online over MQTT; "
                            "previous version restored"
                        )
            except SshError as err:
                raise self._handle_ssh_error(err) from err
            except remote.RemoteError as err:
                raise HomeAssistantError(f"{self.entry.title}: {err}") from err
            finally:
                self._set_progress(False)
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_DEPLOYED_VERSION: target}
            )
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_HOST_KEY}_{self.entry.entry_id}")
            _LOGGER.info("%s: agent %s installed", self.entry.title, target)

    async def async_configure(self, options: dict[str, Any]) -> None:
        """Push a new configuration (used by the options flow before saving)."""
        async with self._lock:
            config = build_config(dict(self.entry.data), options)
            try:
                async with self.ssh() as ssh:
                    await remote.configure(ssh, config)
            except SshError as err:
                raise self._handle_ssh_error(err) from err

    async def async_restart(self) -> None:
        async with self._lock:
            try:
                async with self.ssh() as ssh:
                    await remote.restart(ssh)
            except SshError as err:
                raise self._handle_ssh_error(err) from err
            except remote.RemoteError as err:
                raise HomeAssistantError(f"{self.entry.title}: {err}") from err

    async def async_uninstall(self, purge: bool = True) -> None:
        async with self.ssh() as ssh:
            await remote.uninstall(ssh, purge)

    # ------------------------------------------------------------------ #
    # Automatic update
    # ------------------------------------------------------------------ #

    @callback
    def _schedule_auto_update(self, delay: float) -> None:
        if self._auto_timer:
            self._auto_timer()
        self._auto_timer = async_call_later(self.hass, delay, self._auto_update)

    async def _auto_update(self, _now: Any) -> None:
        self._auto_timer = None
        if not self.entry.options.get(CONF_AUTO_UPDATE) or not self.update_available:
            return
        recently_tried = (
            self._last_auto_attempt is not None
            and time.monotonic() - self._last_auto_attempt < AUTO_UPDATE_RETRY
        )
        if not self.agent.online or recently_tried:
            self._schedule_auto_update(3600)
            return
        self._last_auto_attempt = time.monotonic()
        _LOGGER.info("%s: automatic update to %s", self.entry.title, self.bundle.version)
        try:
            await self.async_install()
        except HomeAssistantError as err:
            _LOGGER.error("%s: automatic update failed: %s", self.entry.title, err)
