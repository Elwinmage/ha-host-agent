"""Config flow: add a host from SSH credentials, bootstrap it and install the agent."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

try:
    # Home Assistant 2026.10+ validates flows with probatio (voluptuous-compatible)
    import probatio as vol
except ImportError:  # older Home Assistant
    import voluptuous as vol  # type: ignore[no-redef,unused-ignore]
from homeassistant.components import mqtt
from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from . import remote
from .agent_config import (
    build_config,
    import_collectors,
    import_language,
)
from .bundle import BundleError, load_bundle, load_remote_files
from .const import (
    ADMIN_USER,
    CONF_AUTO_UPDATE,
    CONF_COLLECTORS,
    CONF_DEPLOYED_VERSION,
    CONF_DEVICE_NAME,
    CONF_HOST_KEY,
    CONF_LANGUAGE,
    CONF_MAC,
    CONF_MACHINE_ID,
    CONF_MQTT_HOST,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    CONF_NODE_ID,
    CONF_PRIVATE_KEY,
    CONF_SUDO_PASSWORD,
    DEFAULT_BASE_TOPIC,
    DEFAULT_MQTT_PORT,
    DEFAULT_SSH_PORT,
    DOMAIN,
    HEALTH_TIMEOUT,
    LANGUAGES,
)
from .options_flow import HostAgentOptionsFlow
from .ssh import (
    CannotConnect,
    HostKeyMismatch,
    InvalidAuth,
    InvalidKey,
    SshClient,
    SshError,
    generate_client_key,
)

_LOGGER = logging.getLogger(__name__)

LOCAL_BROKERS = {"localhost", "127.0.0.1", "::1", "core-mosquitto", "homeassistant", "mosquitto"}

PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
KEY_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


def _credentials_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
            vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
            vol.Optional(CONF_PRIVATE_KEY): KEY_SELECTOR,
            vol.Optional(CONF_SUDO_PASSWORD): PASSWORD_SELECTOR,
        }
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "host"


class InstallError(Exception):
    """Installation failed; `key` is a translation key, `detail` a message."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


async def wait_for_agent(hass: HomeAssistant, node_id: str, version: str, timeout: float) -> bool:
    """Wait until the agent reports `version` and is online over MQTT."""
    base = f"{DEFAULT_BASE_TOPIC}/{node_id}"
    state: dict[str, Any] = {"online": False, "version": None}
    done = asyncio.Event()

    @callback
    def _check() -> None:
        if state["online"] and state["version"] == version:
            done.set()

    @callback
    def _availability(msg: mqtt.ReceiveMessage) -> None:
        state["online"] = msg.payload == "online"
        _check()

    @callback
    def _version(msg: mqtt.ReceiveMessage) -> None:
        try:
            state["version"] = json.loads(msg.payload).get("state")
        except (TypeError, ValueError, AttributeError):
            return
        _check()

    unsubs = [
        await mqtt.async_subscribe(hass, f"{base}/availability", _availability, qos=1),
        await mqtt.async_subscribe(hass, f"{base}/state/agent_version", _version, qos=1),
    ]
    try:
        async with asyncio.timeout(timeout):
            await done.wait()
        return True
    except TimeoutError:
        return False
    finally:
        for unsub in unsubs:
            unsub()


class HostAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._host = ""
        self._port = DEFAULT_SSH_PORT
        self._credentials: dict[str, Any] = {}
        self._info: remote.SystemInfo | None = None
        self._host_key: str | None = None
        self._fingerprint = ""
        self._mqtt: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None
        self._error: InstallError | None = None
        self._result_data: dict[str, Any] = {}
        self._result_options: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HostAgentOptionsFlow:
        return HostAgentOptionsFlow()

    # ------------------------------------------------------------------ #
    # Step 1: connection
    # ------------------------------------------------------------------ #

    async def _probe(self, user_input: dict[str, Any], pinned: str | None = None) -> dict[str, str]:
        """Connect with the admin credentials, run the preflight and check sudo."""
        if not user_input.get(CONF_PASSWORD) and not user_input.get(CONF_PRIVATE_KEY):
            return {"base": "auth_required"}
        sudo_password = user_input.get(CONF_SUDO_PASSWORD) or user_input.get(CONF_PASSWORD)
        try:
            async with self._client(user_input) as ssh:
                self._info = await remote.preflight(ssh)
                assert ssh.server_host_key is not None
                self._host_key = ssh.server_host_key.public_key
                self._fingerprint = ssh.server_host_key.fingerprint
                if self._info.uid != 0 and not await remote.check_sudo(ssh, self._info, sudo_password):
                    return {"base": "sudo_failed"}
        except InvalidKey:
            return {"base": "invalid_key"}
        except InvalidAuth:
            return {"base": "invalid_auth"}
        except (CannotConnect, HostKeyMismatch):
            return {"base": "cannot_connect"}
        except SshError:
            _LOGGER.exception("Unexpected SSH error")
            return {"base": "unknown"}
        self._credentials = dict(user_input)
        return {}

    def _client(self, credentials: Mapping[str, Any], host_key: str | None = None) -> SshClient:
        return SshClient(
            self._host,
            self._port,
            credentials[CONF_USERNAME],
            password=credentials.get(CONF_PASSWORD) or None,
            private_key=credentials.get(CONF_PRIVATE_KEY) or None,
            host_key=host_key,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._port = int(user_input[CONF_PORT])
            errors = await self._probe(user_input)
            if not errors:
                assert self._info is not None
                if not self._info.systemd:
                    return self.async_abort(reason="no_systemd")
                if not self._info.python_ok:
                    return self.async_abort(
                        reason="python_too_old", description_placeholders={"python": self._info.python or "?"}
                    )
                await self.async_set_unique_id(self._info.machine_id or self._info.mac or self._host)
                self._abort_if_unique_id_configured()
                return await self.async_step_host_key()

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_SSH_PORT)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
            }
        ).extend(_credentials_schema(user_input or {}).schema)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # ------------------------------------------------------------------ #
    # Step 2: host key confirmation (trust on first use)
    # ------------------------------------------------------------------ #

    def _host_placeholders(self) -> dict[str, str]:
        info = self._info
        assert info is not None
        return {
            "host": self._host,
            "fingerprint": self._fingerprint,
            "hostname": info.hostname,
            "os": info.os or "?",
            "arch": info.arch,
            "python": info.python,
            "smartctl": info.smartctl or "-",
            "installed": info.installed or "-",
        }

    async def async_step_host_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_agent()
        return self.async_show_form(
            step_id="host_key", data_schema=vol.Schema({}), description_placeholders=self._host_placeholders()
        )

    # ------------------------------------------------------------------ #
    # Step 3: MQTT settings used by the agent
    # ------------------------------------------------------------------ #

    async def _suggested_broker(self) -> str:
        entries = self.hass.config_entries.async_entries(mqtt.DOMAIN)
        broker = str(entries[0].data.get("broker", "")) if entries else ""
        if broker and broker not in LOCAL_BROKERS:
            return broker
        try:
            return await async_get_source_ip(self.hass, target_ip=self._host)
        except (HomeAssistantError, OSError):
            return broker

    async def async_step_agent(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._info is not None
        if user_input is not None:
            self._mqtt = user_input
            return await self.async_step_install()

        language = self.hass.config.language[:2] if self.hass.config.language else "en"
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_NAME, default=self._info.hostname): str,
                vol.Required(CONF_MQTT_HOST, default=await self._suggested_broker()): str,
                vol.Required(CONF_MQTT_PORT, default=DEFAULT_MQTT_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Optional(CONF_MQTT_USERNAME): str,
                vol.Optional(CONF_MQTT_PASSWORD): PASSWORD_SELECTOR,
                vol.Required(CONF_LANGUAGE, default=language if language in LANGUAGES else "en"): vol.In(
                    LANGUAGES
                ),
                vol.Required(CONF_AUTO_UPDATE, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="agent", data_schema=schema, description_placeholders={"hostname": self._info.hostname}
        )

    # ------------------------------------------------------------------ #
    # Step 4: bootstrap + installation (progress)
    # ------------------------------------------------------------------ #

    async def _bootstrap(self, ssh_key_comment: str) -> tuple[str, str | None]:
        """Run the bootstrap with the admin credentials; returns (private key, existing config)."""
        assert self._info is not None and self._host_key is not None
        files = await self.hass.async_add_executor_job(load_remote_files)
        bundle = await self.hass.async_add_executor_job(load_bundle)
        key = await self.hass.async_add_executor_job(generate_client_key, ssh_key_comment)
        sudo_password = self._credentials.get(CONF_SUDO_PASSWORD) or self._credentials.get(CONF_PASSWORD)
        existing = None
        try:
            async with self._client(self._credentials, self._host_key) as ssh:
                if self._info.installed:
                    existing = await remote.read_existing_config(ssh, self._info, sudo_password)
                await remote.bootstrap(ssh, self._info, sudo_password, key.public_key, files, bundle.unit)
        except HostKeyMismatch as err:
            raise InstallError("host_key_changed") from err
        except SshError as err:
            raise InstallError("ssh_failed", str(err)) from err
        except remote.RemoteError as err:
            raise InstallError("bootstrap_failed", str(err)) from err

        # The new restricted account must work before anything is stored
        try:
            async with SshClient(
                self._host, self._port, ADMIN_USER, private_key=key.private_key, host_key=self._host_key
            ) as admin:
                await remote.status(admin)
        except (SshError, remote.RemoteError) as err:
            raise InstallError("admin_login_failed", str(err)) from err
        return key.private_key, existing

    async def _install(self) -> None:
        assert self._info is not None and self._host_key is not None
        try:
            bundle = await self.hass.async_add_executor_job(load_bundle)
        except BundleError as err:
            raise InstallError("bundle_missing", str(err)) from err

        private_key, existing = await self._bootstrap(f"home-assistant@{self._info.hostname}")
        node_id = _slug(self._info.hostname)
        data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_PRIVATE_KEY: private_key,
            CONF_HOST_KEY: self._host_key,
            CONF_NODE_ID: node_id,
            CONF_MACHINE_ID: self._info.machine_id,
            CONF_MAC: self._info.mac,
            CONF_DEVICE_NAME: self._mqtt[CONF_DEVICE_NAME],
            CONF_MQTT_HOST: self._mqtt[CONF_MQTT_HOST],
            CONF_MQTT_PORT: self._mqtt[CONF_MQTT_PORT],
            CONF_MQTT_USERNAME: self._mqtt.get(CONF_MQTT_USERNAME) or None,
            CONF_MQTT_PASSWORD: self._mqtt.get(CONF_MQTT_PASSWORD) or None,
        }
        options = {
            CONF_AUTO_UPDATE: self._mqtt[CONF_AUTO_UPDATE],
            # Keep the settings of a manual installation
            CONF_LANGUAGE: import_language(existing, self._mqtt[CONF_LANGUAGE])
            if existing
            else self._mqtt[CONF_LANGUAGE],
            CONF_COLLECTORS: import_collectors(existing),
        }
        try:
            async with SshClient(
                self._host, self._port, ADMIN_USER, private_key=private_key, host_key=self._host_key
            ) as admin:
                await remote.install(
                    admin, bundle, build_config(data, options), data[CONF_MQTT_PASSWORD] or ""
                )
        except (SshError, remote.RemoteError) as err:
            raise InstallError("install_failed", str(err)) from err

        if not await wait_for_agent(self.hass, node_id, bundle.version, HEALTH_TIMEOUT):
            raise InstallError("agent_not_reporting", f"{data[CONF_MQTT_HOST]}:{data[CONF_MQTT_PORT]}")
        data[CONF_DEPLOYED_VERSION] = bundle.version
        self._result_data = data
        self._result_options = options

    async def _run_install(self) -> None:
        try:
            await self._install()
        except InstallError as err:
            self._error = err
        except Exception as err:
            _LOGGER.exception("Unexpected installation error")
            self._error = InstallError("unknown", str(err))

    async def async_step_install(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._task is None:
            self._task = self.hass.async_create_task(self._run_install())
        if not self._task.done():
            return self.async_show_progress(
                step_id="install",
                progress_action="install",
                progress_task=self._task,
                description_placeholders={"hostname": self._info.hostname if self._info else self._host},
            )
        self._task = None
        if self._error:
            return self.async_show_progress_done(next_step_id="install_failed")
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_install_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._error is not None
        return self.async_abort(
            reason=self._error.key, description_placeholders={"detail": self._error.detail}
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._result_data[CONF_DEVICE_NAME], data=self._result_data, options=self._result_options
        )

    # ------------------------------------------------------------------ #
    # Re-authentication: new admin credentials, new host key, new HA key
    # ------------------------------------------------------------------ #

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            errors = await self._probe(user_input)
            if not errors:
                if self._host_key != entry.data[CONF_HOST_KEY]:
                    return await self.async_step_reauth_host_key()
                return await self._async_finish_reauth()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema(user_input or {}),
            errors=errors,
            description_placeholders={"host": self._host},
        )

    async def async_step_reauth_host_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_finish_reauth()
        return self.async_show_form(
            step_id="reauth_host_key",
            data_schema=vol.Schema({}),
            description_placeholders=self._host_placeholders(),
        )

    async def _async_finish_reauth(self) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        assert self._info is not None
        try:
            private_key, _ = await self._bootstrap(f"home-assistant@{self._info.hostname}")
        except InstallError as err:
            return self.async_abort(reason=err.key, description_placeholders={"detail": err.detail})
        return self.async_update_reload_and_abort(
            entry,
            data_updates={CONF_PRIVATE_KEY: private_key, CONF_HOST_KEY: self._host_key},
        )
