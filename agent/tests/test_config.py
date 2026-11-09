from __future__ import annotations

from pathlib import Path

import pytest

from host_agent.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).parents[1] / "packaging" / "config.example.yaml"


def test_example_config_is_valid(tmp_path, monkeypatch):
    secret = tmp_path / "mqtt.secret"
    secret.write_text("s3cret\n")
    text = EXAMPLE.read_text().replace("/etc/host-agent/mqtt.secret", str(secret))
    path = tmp_path / "config.yaml"
    path.write_text(text)
    monkeypatch.delenv("HOST_AGENT_MQTT_PASSWORD", raising=False)

    cfg = load_config(path)
    assert cfg.node_id == "bkup"
    assert cfg.language == "fr"
    assert cfg.mqtt.password == "s3cret"
    assert cfg.smart.ata_attribute_thresholds == {5: 0, 187: 0, 197: 0, 198: 0}
    assert cfg.filesystem.required == []
    assert cfg.filesystem.thresholds_for("/").critical == 95


def test_env_password_wins(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_MQTT_PASSWORD", "from-env")
    cfg = parse_config({"mqtt": {"host": "h", "password": "inline"}})
    assert cfg.mqtt.password == "from-env"


def test_defaults():
    cfg = parse_config({"node_id": "My Host!"})
    assert cfg.node_id == "my_host"
    assert cfg.smart.interval == 3600
    assert cfg.smart.devices == []


@pytest.mark.parametrize(
    "data, message",
    [
        ({"mqtt": {"hots": "x"}}, "Unknown option"),
        ({"language": "de"}, "language"),
        ({"collectors": {"smart": {"interval": 10}}}, "at least 60"),
        ({"collectors": {"smart": {"temperature": {"warning": 70, "critical": 60}}}}, "lower"),
        ({"mqtt": {"port": "1883"}}, "invalid type"),
        ({"collectors": {"smart": {"devices": [42]}}}, "devices"),
        ({"collectors": {"smart": {"selftest": {"window": "25:00-03:00"}}}}, "window"),
        ({"collectors": {"smart": {"selftest": {"window": "3h-5h"}}}}, "window"),
    ],
)
def test_invalid(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data)
