# ha-host-agent

Monitoring of Linux hosts (backup servers first) in Home Assistant:

- **agent** (`agent/`): Python agent on each host, publishing SMART health,
  filesystem usage and (soon) backup status over MQTT;
- **blueprint** (`blueprints/`): alert automation for any problem reported by the agents;
- **integration** (`custom_components/host_agent/`, upcoming): adds a host from its IP
  and SSH credentials, installs, configures and updates the agent, serves the card;
- **card** (`card/`, upcoming): dedicated Lovelace card, served by the integration.

## Status

| Step | Content | State |
|---|---|---|
| 1 | Agent: SMART, filesystems, device discovery, LWT, refresh, legacy cleanup; alert blueprint | ✅ 0.1.1 |
| 2 | Agent: dirvish vaults, rsync jobs, push hooks | ⏳ |
| 3 | Integration: config flow, SSH bootstrap, deployment, update entity, repairs | ⏳ |
| 4 | Integration: options flow (jobs, thresholds) | ⏳ |
| 5 | Card | ⏳ |

## Alert blueprint

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FElwinmage%2Fha-host-agent%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fhost_agent%2Fhost_agent_alert.yaml)

`blueprints/automation/host_agent/host_agent_alert.yaml`

- alert after a confirmation delay when a problem sensor turns on, filtered by severity;
- new alert when another issue appears while a problem is active;
- "back to normal" and "agent offline / back online" alerts (LWT based);
- optional persistent notification, kept up to date and dismissed when resolved;
- your own actions (mobile app, Telegram, e-mail…) with `title`, `message`,
  `severity`, `event`, `host`, `issues` variables;
- no alert storm when Home Assistant restarts.

Example action:

```yaml
- action: notify.mobile_app_pixel
  data:
    title: "{{ title }}"
    message: "{{ message }}"
    data:
      priority: "{{ 'high' if severity == 'critical' else 'normal' }}"
```

See [agent/README.md](agent/README.md) for installation, entities and the MQTT contract.
