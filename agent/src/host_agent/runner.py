"""Scheduling, aggregation and publication loop."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from host_agent import PROTOCOL_VERSION, __version__
from host_agent.collectors.base import Collector
from host_agent.i18n import Translator
from host_agent.model import (
    CRITICAL,
    WARNING,
    CollectorResult,
    EntitySpec,
    Issue,
    Reading,
    max_severity,
    problem_reading,
)
from host_agent.mqtt.discovery import PAYLOAD_PRESS, DeviceInfo, Topics, discovery_payload, state_payload
from host_agent.state import AgentState, StateStore

_LOGGER = logging.getLogger(__name__)

HOST_PROBLEM = "problem"
REFRESH_DEBOUNCE = 10.0


class Transport(Protocol):
    @property
    def connected(self) -> bool: ...

    def publish(self, topic: str, payload: str | bytes, retain: bool = True, qos: int = 1) -> None: ...


@dataclass
class Snapshot:
    entities: list[EntitySpec] = field(default_factory=list)
    readings: list[Reading] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    removed: dict[str, str] = field(default_factory=dict)


def _local(iso: str) -> str:
    """Human readable local time for messages (attributes keep ISO 8601)."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class Runner:
    def __init__(
        self,
        collectors: list[Collector],
        topics: Topics,
        device: DeviceInfo,
        translate: Translator,
        store: StateStore,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.collectors = collectors
        self.topics = topics
        self.device = device
        self.t = translate
        self.store = store
        self.clock = clock
        self.now = now
        self.state: AgentState = store.load()
        self.transport: Transport | None = None

        self._results: dict[str, CollectorResult] = {}
        self._next_run: dict[str, float] = {}
        self._failures: dict[str, Issue] = {}
        self._removed: dict[str, str] = {}
        self._snapshot: Snapshot | None = None
        self._discovery_hash: str | None = None

        self._wake = threading.Event()
        self._commands: queue.Queue[tuple[str, str]] = queue.Queue()
        self._force_collect = True
        self._force_publish = True
        self._last_refresh = -REFRESH_DEBOUNCE

    # ------------------------------------------------------------------ #
    # Callbacks from the transport thread: only set flags and wake the loop
    # ------------------------------------------------------------------ #

    def request_republish(self) -> None:
        self._force_publish = True
        self._discovery_hash = None
        self._wake.set()

    def handle_command(self, name: str, payload: str) -> None:
        self._commands.put((name, payload))
        self._wake.set()

    # ------------------------------------------------------------------ #
    # Collection
    # ------------------------------------------------------------------ #

    def _process_commands(self) -> None:
        while True:
            try:
                name, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            if name == "refresh" and payload in (PAYLOAD_PRESS, ""):
                if self.clock() - self._last_refresh >= REFRESH_DEBOUNCE:
                    _LOGGER.info("Refresh requested")
                    self._last_refresh = self.clock()
                    self._force_collect = True
            elif name == "forget":
                self.forget(payload.strip())
            elif self._dispatch_button(name):
                self._force_collect = True
            else:
                _LOGGER.debug("Ignoring unknown command %s", name)

    def _dispatch_button(self, key: str) -> bool:
        spec = self.state.known.get(key)
        if spec is None or spec.platform != "button" or spec.source is None:
            return False
        owner = next((c for c in self.collectors if c.name == spec.source), None)
        return bool(owner and owner.on_command(key))

    def forget(self, subject: str) -> list[str]:
        """Drop a persistent subject (e.g. a disk that was removed on purpose)."""
        keys = [k for k, s in self.state.known.items() if s.subject == subject]
        for key in keys:
            self._removed[key] = self.state.known.pop(key).platform
        self.state.last_seen.pop(subject, None)
        if keys:
            _LOGGER.info("Forgot subject %s (%d entities)", subject, len(keys))
            self._force_publish = True
            self._snapshot = self.build_snapshot()
        return keys

    def collect_due(self, force: bool = False) -> bool:
        now = self.clock()
        ran = False
        for collector in self.collectors:
            if not force and now < self._next_run.get(collector.name, 0):
                continue
            ran = True
            self._next_run[collector.name] = now + collector.interval
            try:
                result = collector.collect()
                for spec in result.entities:
                    spec.source = collector.name
                self._results[collector.name] = result
                self._failures.pop(collector.name, None)
            except Exception as err:
                _LOGGER.exception("Collector %s failed", collector.name)
                self._failures[collector.name] = Issue(
                    HOST_PROBLEM,
                    f"collector_failed_{collector.name}",
                    WARNING,
                    self.t("collector_failed", name=collector.name, error=str(err)[:200]),
                )
                previous = self._results.get(collector.name)
                if previous:  # keep entities, blank the values
                    self._results[collector.name] = CollectorResult(
                        entities=previous.entities,
                        readings=[Reading(r.key, None) for r in previous.readings],
                    )
        if ran:
            self._snapshot = self.build_snapshot()
            self.store.save(self.state)
        return ran

    # ------------------------------------------------------------------ #
    # Aggregation
    # ------------------------------------------------------------------ #

    def build_snapshot(self) -> Snapshot:
        merged = CollectorResult()
        for result in self._results.values():
            merged.extend(result)
        merged.issues.extend(self._failures.values())

        now_iso = self.now().isoformat()
        current = {s.key for s in merged.entities}
        for spec in merged.entities:
            if spec.subject and spec.role == "problem":
                self.state.last_seen[spec.subject] = now_iso

        # Persistent subjects that vanished are reported as missing
        removed = dict(self._removed)
        for key, spec in self.state.known.items():
            if key in current or key.startswith("_"):
                continue
            if spec.persistent and spec.subject and self._still_wanted(key, spec):
                merged.entities.append(spec)
                if spec.role == "problem":
                    since = self.state.last_seen.get(spec.subject, "?")
                    issue = Issue(key, "missing", CRITICAL, self.t("missing", since=_local(since)))
                    merged.issues.append(issue)
                    merged.readings.append(
                        problem_reading(
                            key, [issue], {"missing": True, "last_seen": since, "label": spec.subject_label}
                        )
                    )
                else:
                    merged.readings.append(Reading(key, None))
            elif not self._is_host_key(key):
                removed[key] = spec.platform

        host_entities, host_readings = self._host_part(merged)
        merged.entities.extend(host_entities)
        merged.readings.extend(host_readings)

        self.state.known = {s.key: s for s in merged.entities}
        return Snapshot(merged.entities, merged.readings, merged.issues, removed)

    def _still_wanted(self, key: str, spec: EntitySpec) -> bool:
        # State files written by 0.1.0 have no source: infer it from the key
        source = spec.source or (
            "smart" if key.startswith("disk_") else "filesystem" if key.startswith("fs_") else None
        )
        owner = next((c for c in self.collectors if c.name == source), None)
        if owner is None:
            return source is None  # collector disabled: release its subjects
        return owner.keeps(spec)

    @staticmethod
    def _is_host_key(key: str) -> bool:
        return key in {HOST_PROBLEM, "problems", "last_update", "agent_version", "refresh"}

    def _host_part(self, merged: CollectorResult) -> tuple[list[EntitySpec], list[Reading]]:
        names = {s.key: s.name for s in merged.entities if s.role == "problem"}
        issues = sorted(merged.issues, key=lambda i: (-1 if i.severity == CRITICAL else 0, i.id))
        detailed = [{**i.as_dict(), "subject_name": names.get(i.subject)} for i in issues]
        severity = max_severity([i.severity for i in issues])
        entities = [
            EntitySpec(
                HOST_PROBLEM,
                "binary_sensor",
                self.t("host_problem"),
                device_class="problem",
                icon="mdi:server",
                role="problem",
            ),
            EntitySpec(
                "problems",
                "sensor",
                self.t("host_problems"),
                state_class="measurement",
                icon="mdi:alert-circle-outline",
            ),
            EntitySpec(
                "last_update",
                "sensor",
                self.t("host_last_update"),
                device_class="timestamp",
                entity_category="diagnostic",
            ),
            EntitySpec(
                "agent_version",
                "sensor",
                self.t("host_agent_version"),
                entity_category="diagnostic",
                icon="mdi:tag-outline",
            ),
            EntitySpec("refresh", "button", self.t("host_refresh"), icon="mdi:refresh"),
        ]
        readings = [
            Reading(
                HOST_PROBLEM,
                "ON" if issues else "OFF",
                {
                    "severity": severity,
                    "critical_count": sum(1 for i in issues if i.severity == CRITICAL),
                    "warning_count": sum(1 for i in issues if i.severity == WARNING),
                    "issues": detailed,
                },
            ),
            Reading("problems", len(issues)),
            Reading("last_update", self.now().isoformat()),
            Reading("agent_version", __version__, {"protocol_version": PROTOCOL_VERSION}),
        ]
        return entities, readings

    # ------------------------------------------------------------------ #
    # Publication
    # ------------------------------------------------------------------ #

    def _publish_discovery(self, transport: Transport, snapshot: Snapshot, force: bool) -> None:
        payload = discovery_payload(self.device, snapshot.entities, self.topics, snapshot.removed)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if force or digest != self._discovery_hash:
            transport.publish(self.topics.discovery, encoded, retain=True)
            self._discovery_hash = digest
            _LOGGER.info(
                "Published discovery (%d entities, %d removed)", len(snapshot.entities), len(snapshot.removed)
            )

    def publish(self, transport: Transport, snapshot: Snapshot, force_discovery: bool = False) -> None:
        self._publish_discovery(transport, snapshot, force_discovery)

        for reading in snapshot.readings:
            transport.publish(self.topics.state(reading.key), state_payload(reading.state, reading.attrs))

        if snapshot.removed:
            for key in snapshot.removed:
                transport.publish(self.topics.state(key), b"", retain=True)  # clear retained state
            # HA deletes platform-only components; then retain a clean document
            self._removed.clear()
            snapshot.removed = {}
            self._publish_discovery(transport, snapshot, force=True)

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def step(self) -> None:
        self._process_commands()
        force = self._force_collect
        self._force_collect = False
        ran = self.collect_due(force=force)
        transport = self.transport
        if transport is None or not transport.connected or self._snapshot is None:
            return
        if ran or self._force_publish:
            force_discovery = self._force_publish and self._discovery_hash is None
            self._force_publish = False
            self.publish(transport, self._snapshot, force_discovery=force_discovery)

    def run(self, transport: Transport, stop: threading.Event) -> None:
        self.transport = transport
        while not stop.is_set():
            self.step()
            next_due = min(self._next_run.values(), default=self.clock() + 60)
            timeout = min(max(next_due - self.clock(), 1.0), 60.0)
            self._wake.wait(timeout)
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    def debug_dump(self) -> dict[str, Any]:
        snap = self._snapshot or self.build_snapshot()
        return {
            "discovery": discovery_payload(self.device, snap.entities, self.topics, snap.removed),
            "states": {r.key: {"state": r.state, "attrs": r.attrs} for r in snap.readings},
        }
