"""Bus events for the problem sensors of a host, so automations need no entity list.

The agent's problem binary_sensors are MQTT entities: they belong to the MQTT
integration, not to this one. They are found in the entity registry through the
unique ids the agent publishes (``host_agent_<node>_<key>``), and every meaningful
change is re-fired as a ``host_agent_problem`` event.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_DEVICE_NAME, CONF_NODE_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

EVENT_PROBLEM = "host_agent_problem"
# Key of the host-level problem sensor: it aggregates the issues of every subject
HOST_PROBLEM_KEY = "problem"

# Transitions, named like the blueprint's alerts
PROBLEM = "problem"  # off -> on
NEW_ISSUE = "new_issue"  # on -> on with a different issue list
RESOLVED = "resolved"  # on -> off
OFFLINE = "offline"  # anything -> unavailable (agent LWT)
ONLINE = "online"  # unavailable -> known state


def unique_id_prefix(node_id: str) -> str:
    return f"{DOMAIN}_{node_id}_"


def owner_node(unique_id: str, node_ids: list[str]) -> str | None:
    """Node whose prefix matches, the longest one winning ("bkup" vs "bkup_2")."""
    matches = [node for node in node_ids if unique_id.startswith(unique_id_prefix(node))]
    return max(matches, key=len) if matches else None


def transition(old: State | None, new: State | None) -> str | None:
    """Kind of change, or None when it is not worth an event.

    A sensor appearing (no old state) or leaving ``unknown`` is not reported: after a
    restart of Home Assistant the retained states would otherwise alert again.
    """
    if old is None or new is None:
        return None
    before, after = old.state, new.state
    if after == STATE_UNAVAILABLE:
        return OFFLINE if before != STATE_UNAVAILABLE else None
    if before == STATE_UNAVAILABLE:
        return ONLINE if after != STATE_UNKNOWN else None
    if before == STATE_OFF and after == STATE_ON:
        return PROBLEM
    if before == STATE_ON and after == STATE_OFF:
        return RESOLVED
    if before == after == STATE_ON and old.attributes.get("issues") != new.attributes.get("issues"):
        return NEW_ISSUE
    return None


class ProblemEvents:
    """Fires ``host_agent_problem`` for the problem sensors of one host."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.node_id = str(entry.data[CONF_NODE_ID])
        self.entity_ids: set[str] = set()
        self.host_sensors: set[str] = set()
        self._unsub_states: CALLBACK_TYPE | None = None
        self._unsub_registry: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        self._unsub_registry = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_updated
        )
        self._refresh()
        return self.async_stop

    @callback
    def async_stop(self) -> None:
        for unsub in (self._unsub_states, self._unsub_registry):
            if unsub:
                unsub()
        self._unsub_states = self._unsub_registry = None

    def _problem_entities(self) -> set[str]:
        node_ids = [
            str(entry.data[CONF_NODE_ID])
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if CONF_NODE_ID in entry.data
        ]
        found = set()
        self.host_sensors = set()
        for reg in er.async_get(self.hass).entities.values():
            if (
                reg.platform == "mqtt"
                and reg.domain == "binary_sensor"
                and owner_node(reg.unique_id, node_ids) == self.node_id
                and (reg.device_class or reg.original_device_class) == "problem"
            ):
                found.add(reg.entity_id)
                if reg.unique_id == unique_id_prefix(self.node_id) + HOST_PROBLEM_KEY:
                    self.host_sensors.add(reg.entity_id)
        return found

    @callback
    def _refresh(self) -> None:
        entity_ids = self._problem_entities()
        if entity_ids == self.entity_ids and self._unsub_states:
            return
        if self._unsub_states:
            self._unsub_states()
            self._unsub_states = None
        self.entity_ids = entity_ids
        if entity_ids:
            self._unsub_states = async_track_state_change_event(
                self.hass, sorted(entity_ids), self._on_state_changed
            )
        _LOGGER.debug("%s: watching %d problem sensors", self.node_id, len(entity_ids))

    @callback
    def _on_registry_updated(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["entity_id"].startswith("binary_sensor."):
            self._refresh()

    @callback
    def _on_state_changed(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        kind = transition(old, new)
        if kind is None or old is None or new is None:
            return
        self.hass.bus.async_fire(EVENT_PROBLEM, self.event_data(kind, old, new))

    def event_data(self, kind: str, old: State, new: State) -> dict[str, Any]:
        return {
            "entity_id": new.entity_id,
            "transition": kind,
            "host": self.entry.data.get(CONF_DEVICE_NAME) or self.entry.title,
            "node_id": self.node_id,
            # True for the host's own problem sensor (all its issues), False for a
            # disk, filesystem, vault... sensor
            "host_sensor": new.entity_id in self.host_sensors,
            "kind": new.attributes.get("kind") or old.attributes.get("kind"),
            "name": new.attributes.get("friendly_name")
            or old.attributes.get("friendly_name")
            or new.entity_id,
            "state": new.state,
            "previous_state": old.state,
            "issues": list(new.attributes.get("issues") or []),
            "previous_issues": list(old.attributes.get("issues") or []),
            # How long the previous state lasted (problem duration, offline duration...);
            # for a new issue, how long the problem has been active
            "elapsed": int((new.last_updated - old.last_changed).total_seconds()),
            # Lets an automation check, after a delay, that nothing changed meanwhile
            "since": new.last_changed.isoformat(),
        }
