"""Command line interface: `host-agent run|once|check|forget|cleanup-legacy|remove|version`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from host_agent import PROTOCOL_VERSION, __version__
from host_agent.collectors import build_collectors
from host_agent.collectors.base import run_command
from host_agent.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from host_agent.i18n import Translator
from host_agent.mqtt.discovery import Topics, detect_device_info, hostname
from host_agent.runner import Runner
from host_agent.state import StateStore

_LOGGER = logging.getLogger("host_agent")
CONNECT_TIMEOUT = 15


def _setup_logging(level: str) -> None:
    # systemd/journald adds timestamps itself
    fmt = (
        "%(levelname)s %(name)s: %(message)s"
        if os.environ.get("INVOCATION_ID")
        else "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=fmt)
    logging.getLogger("paho").setLevel(logging.WARNING)


def _topics(cfg: Config) -> Topics:
    return Topics(cfg.mqtt.discovery_prefix, cfg.mqtt.base_topic, cfg.node_id)


def _build_runner(cfg: Config, state_file: str | None) -> Runner:
    t = Translator(cfg.language)
    return Runner(
        collectors=build_collectors(cfg, t),
        topics=_topics(cfg),
        device=detect_device_info(cfg.node_id, cfg.name),
        translate=t,
        store=StateStore(state_file),
    )


def _client(cfg: Config, runner: Runner | None = None, with_will: bool = True) -> Any:
    from host_agent.mqtt.client import MqttClient  # paho only needed for MQTT commands

    return MqttClient(
        cfg.mqtt,
        _topics(cfg),
        on_connected=runner.request_republish if runner else None,
        on_ha_online=runner.request_republish if runner else None,
        on_command=runner.handle_command if runner else None,
        with_will=with_will,
    )


def _connect(client: Any, cfg: Config) -> bool:
    client.start()
    if not client.wait_connected(CONNECT_TIMEOUT):
        _LOGGER.error("Cannot connect to MQTT broker %s:%s", cfg.mqtt.host, cfg.mqtt.port)
        client.stop()
        return False
    return True


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_run(cfg: Config, _args: argparse.Namespace) -> int:
    runner = _build_runner(cfg, cfg.state_file)
    client = _client(cfg, runner)
    stop = threading.Event()

    def _stop(signum: int, _frame: Any) -> None:
        _LOGGER.info("Received signal %s, stopping", signum)
        stop.set()
        runner.wake()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _LOGGER.info("ha-host-agent %s starting (node %s)", __version__, cfg.node_id)
    client.start()
    try:
        runner.run(client, stop)
    finally:
        client.stop()
    return 0


def cmd_once(cfg: Config, args: argparse.Namespace) -> int:
    runner = _build_runner(cfg, None if args.dry_run else cfg.state_file)
    runner.collect_due(force=True)
    if args.dry_run:
        print(json.dumps(runner.debug_dump(), indent=2, ensure_ascii=False, default=str))
        return 0
    client = _client(cfg, with_will=False)
    if not _connect(client, cfg):
        return 1
    runner.publish(client, runner.build_snapshot(), force_discovery=True)
    client.flush()
    client.stop()
    return 0


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    ok = True

    def report(status: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok &= status
        print(f"[{'OK' if status else 'KO'}] {label}{(' - ' + detail) if detail else ''}")

    report(True, "Configuration", f"node_id={cfg.node_id}, language={cfg.language}")
    if cfg.smart.enabled:
        res = run_command([cfg.smart.smartctl, "--version"], 15)
        match = re.search(r"smartctl (\d+)\.(\d+)", res.stdout)
        if not match:
            report(False, "smartctl", res.stderr.strip() or "not found")
        else:
            version = (int(match.group(1)), int(match.group(2)))
            report(version >= (7, 0), "smartctl", f"{match.group(0)} (JSON output requires >= 7.0)")
        try:
            t = Translator(cfg.language)
            from host_agent.collectors.smart import SmartCollector

            devices = SmartCollector(cfg.smart, t).discover()
            report(
                bool(devices),
                "SMART devices",
                ", ".join(f"{d.name} [{d.type}]" for d in devices) or "none found (permissions? see README)",
            )
        except Exception as err:  # noqa: BLE001
            report(False, "SMART devices", str(err))
    if cfg.filesystem.enabled:
        for mp in cfg.filesystem.required:
            report(os.path.ismount(mp), f"Required mount {mp}")
    if not args.no_mqtt:
        client = _client(cfg, with_will=False)
        connected = _connect(client, cfg)
        report(connected, "MQTT", f"{cfg.mqtt.host}:{cfg.mqtt.port}")
        if connected:
            client.stop()
    return 0 if ok else 1


def cmd_forget(cfg: Config, args: argparse.Namespace) -> int:
    client = _client(cfg, with_will=False)
    if not _connect(client, cfg):
        return 1
    client.publish(_topics(cfg).command("forget"), args.subject, retain=False)
    client.flush()
    client.stop()
    print(f"Forget request sent for '{args.subject}' (handled by the running agent)")
    return 0


def legacy_topics(
    prefix: str, host: str, scopes: set[str], rsync_users: list[str], smartctl: str = "smartctl"
) -> list[str]:
    """Discovery topics created by the former bash scripts."""
    topics: list[str] = []
    if "smart" in scopes:
        res = run_command([smartctl, "--scan"], 15)
        for line in res.stdout.splitlines():
            if line.startswith("/dev/"):
                dev = line.split()[0].replace("/", "_")
                topics.append(f"{prefix}/binary_sensor/{host}_health_{dev}/config")
    if "space" in scopes:
        topics.append(f"{prefix}/sensor/{host}_used_space/config")
    if "update" in scopes:
        topics.append(f"{prefix}/sensor/{host}_update_datetime/config")
    if "vaults" in scopes:
        vaults = Path(f"/{host}/dirvish/vaults")
        if vaults.is_dir():
            topics += [f"{prefix}/binary_sensor/{host}_{v.name}/config" for v in sorted(vaults.iterdir())]
    if "rsync" in scopes:
        users = list(rsync_users)
        rsync_dir = Path(f"/{host}/dirvish/rsync")
        if not users and rsync_dir.is_dir():
            users = [d.name for d in sorted(rsync_dir.iterdir()) if d.is_dir()]
        topics += [f"{prefix}/sensor/rsync_{u}/config" for u in users]
    return topics


def cmd_cleanup_legacy(cfg: Config, args: argparse.Namespace) -> int:
    scopes = set(args.scope.split(","))
    host = args.legacy_host or hostname()
    topics = legacy_topics(cfg.mqtt.discovery_prefix, host, scopes, args.rsync_user or [], cfg.smart.smartctl)
    if not topics:
        print("No legacy topic found")
        return 0
    print("Legacy discovery topics:")
    for topic in topics:
        print(f"  {topic}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to delete the corresponding Home Assistant entities.")
        return 0
    client = _client(cfg, with_will=False)
    if not _connect(client, cfg):
        return 1
    for topic in topics:
        # An empty retained config deletes the entity in HA and clears the retained message
        client.publish(topic, b"", retain=True)
    client.flush()
    client.stop()
    print(f"{len(topics)} legacy entities removed")
    return 0


def cmd_remove(cfg: Config, args: argparse.Namespace) -> int:
    if not args.yes:
        print("This deletes the device and all its entities from Home Assistant. Use --yes to confirm.")
        return 1
    topics = _topics(cfg)
    state = StateStore(cfg.state_file).load()
    client = _client(cfg, with_will=False)
    if not _connect(client, cfg):
        return 1
    client.publish(topics.discovery, b"", retain=True)
    for key in state.known:
        client.publish(topics.state(key), b"", retain=True)
    client.publish(topics.availability, b"", retain=True)
    client.flush()
    client.stop()
    print("Device removed from Home Assistant. Stop the agent service to prevent re-creation.")
    return 0


def cmd_validate(cfg: Config, _args: argparse.Namespace) -> int:
    """Configuration already parsed by main(): only report success (used before deployment)."""
    print(f"Configuration OK (node_id={cfg.node_id})")
    return 0


def cmd_version(_cfg: Config | None, _args: argparse.Namespace) -> int:
    print(f"ha-host-agent {__version__} (protocol {PROTOCOL_VERSION})")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="host-agent", description="Home Assistant host monitoring agent")
    parser.add_argument("-c", "--config", default=os.environ.get("HOST_AGENT_CONFIG", DEFAULT_CONFIG_PATH))
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the agent (systemd service)")
    p = sub.add_parser("once", help="collect once and publish")
    p.add_argument("--dry-run", action="store_true", help="print discovery and states, no MQTT")
    p = sub.add_parser("check", help="check configuration, tools and broker")
    p.add_argument("--no-mqtt", action="store_true")
    p = sub.add_parser("forget", help="forget a removed disk or filesystem")
    p.add_argument("subject", help="subject key, e.g. disk_2420e8b02327")
    p = sub.add_parser("cleanup-legacy", help="remove entities created by the former bash scripts")
    p.add_argument(
        "--scope",
        default="smart,space,update",
        help="comma separated: smart,space,update,vaults,rsync (default: %(default)s)",
    )
    p.add_argument("--legacy-host", help="hostname used by the old scripts (default: this host)")
    p.add_argument("--rsync-user", action="append", help="rsync job name (repeatable)")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("remove", help="delete this device from Home Assistant")
    p.add_argument("--yes", action="store_true")
    sub.add_parser("validate", help="validate the configuration file and exit")
    sub.add_parser("version")
    return parser


COMMANDS = {
    "run": cmd_run,
    "once": cmd_once,
    "check": cmd_check,
    "forget": cmd_forget,
    "cleanup-legacy": cmd_cleanup_legacy,
    "remove": cmd_remove,
    "validate": cmd_validate,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        return cmd_version(None, args)
    try:
        cfg = load_config(args.config)
    except ConfigError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 2
    _setup_logging("DEBUG" if args.verbose else cfg.log_level)
    return COMMANDS[args.command](cfg, args)
