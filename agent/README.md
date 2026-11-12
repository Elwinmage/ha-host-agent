# ha-host-agent — agent

Python agent running on each monitored host. It reads SMART data and
filesystem usage, evaluates them against thresholds and publishes the result to
Home Assistant through MQTT (device-based discovery, retained states, LWT).

The agent works on its own; the `host_agent` Home Assistant integration (same
repository, upcoming) will install, configure and update it over SSH.

## Requirements

- Python ≥ 3.11 (Debian Bookworm, Trixie, Ubuntu 24.04+)
- `smartmontools` ≥ 7.0 (`smartctl --json`)
- systemd
- Home Assistant ≥ 2025.10 with the MQTT integration

No compiled dependency: `host-agent.pyz` is the same file on armv7, arm64 and x86_64.

## Installation

```bash
# build (or download the release asset)
agent/packaging/build_pyz.sh            # -> agent/dist/host-agent.pyz

# on the host
sudo apt install smartmontools
sudo agent/packaging/install.sh agent/dist/host-agent.pyz
sudoedit /etc/host-agent/config.yaml    # mqtt section
echo 'mqtt-password' | sudo tee /etc/host-agent/mqtt.secret >/dev/null
sudo host-agent check
sudo systemctl restart host-agent && journalctl -u host-agent -f
```

Layout:

| Path | Content |
|---|---|
| `/opt/host-agent/versions/<x.y.z>/host-agent.pyz` | installed versions |
| `/opt/host-agent/current` | symlink to the active version (rollback = move the link) |
| `/usr/local/bin/host-agent` | CLI wrapper (`python3 /opt/host-agent/current/host-agent.pyz --config /etc/host-agent/config.yaml`) |
| `/etc/host-agent/config.yaml`, `mqtt.secret` | configuration, `root:host-agent 0640` |
| `/var/lib/host-agent/state.json` | known disks/filesystems (missing detection) |

### Permissions

The service runs as the unprivileged `host-agent` user. `smartctl` gets only the
capabilities it needs through systemd (`CAP_SYS_RAWIO` for ATA/SAT pass-through,
`CAP_SYS_ADMIN` for NVMe, `CAP_DAC_READ_SEARCH` to open the devices), with
`ProtectSystem=strict` and `NoNewPrivileges`. No sudo, no root process.

If a device still cannot be read (`check` lists no disk), set `sudo: true` in the
`smart` section and allow `host-agent` to run `/usr/sbin/smartctl` without password.

## Configuration

See [`packaging/config.example.yaml`](packaging/config.example.yaml); every option is
documented there. Unknown options are rejected at start-up.

## Commands

```
host-agent [-c CONFIG] run                 # daemon (systemd)
host-agent once [--dry-run]                # collect once; --dry-run prints discovery + states
host-agent check [--no-mqtt]               # config, smartctl, devices, required mounts, broker
host-agent forget <subject>                # forget a disk/filesystem removed on purpose
host-agent notify <collector>              # collect now (hooks): dirvish, jobs, smart...
host-agent report <job> [--failed] [--message M]   # result of a job declared with report: true
host-agent cleanup-legacy [--scope ...] [--apply]   # delete entities of the former bash scripts
host-agent remove --yes                    # delete the device from Home Assistant
host-agent version
```

## Home Assistant entities

One device per host (`host_agent_<node_id>`). Entity ids are stable:
`<platform>.<node_id>_<key>`.

| Entity | Key | Notes |
|---|---|---|
| Overall status (binary_sensor, problem) | `problem` | ON when any issue exists; attributes `severity`, `issues`, `critical_count`, `warning_count` |
| Issues (sensor) | `problems` | number of issues |
| Last update (sensor, timestamp) | `last_update` | diagnostic |
| Agent version (sensor) | `agent_version` | attribute `protocol_version` |
| Refresh (button) | `refresh` | forces a collection |
| Disk (binary_sensor, problem) | `disk_<serial>` | attributes: model, serial, device, power-on hours, self-test info, watched attributes, `severity`, `issues` |
| Disk temperature | `disk_<serial>_temperature` | unknown when the USB bridge does not report it |
| Last successful self-test | `disk_<serial>_selftest_age` | power-on hours since; only when the drive supports self-tests |
| Start short / long self-test (buttons) | `disk_<serial>_selftest_short`, `_long` | config category |
| Filesystem (binary_sensor, problem) | `fs_<mount>` | attributes: mountpoint, source, fstype, sizes, thresholds |
| Used space | `fs_<mount>_used_pct` | % (same formula as `df`) |
| Free space | `fs_<mount>_free` | GiB (MiB below 10 GiB) |
| Backup (binary_sensor, problem) | `vault_<vault>` | dirvish vault; attributes: last image, status, duration, `last_success`, `running`, `history` (one entry per day) |
| Last successful backup | `vault_<vault>_last_success` | timestamp |
| Job (binary_sensor, problem) | `job_<name>` | attributes: last run, status, message, file |
| Last successful run | `job_<name>_last_success` | timestamp |
| System (binary_sensor, problem) | `system` | attributes: reboot required, OS, kernel, model |
| CPU temperature, load, memory used, last boot | `system_cpu_temperature`, `system_load`, `system_memory`, `system_boot` | |

| GPU (binary_sensor, problem) | `gpu_<pci bus>` | NVIDIA / AMD; attributes: name, vendor, power limit, fan, throttle reasons |
| GPU temperature, load, memory, power | `gpu_<bus>_temperature`, `_utilization`, `_memory`, `_power` | |
| RAID / ZFS (binary_sensor, problem) | `raid_<md>`, `zpool_<pool>` | state, members, last scrub... |
| ZFS pool capacity | `zpool_<pool>_capacity` | % |
| Services (binary_sensor, problem) | `services` | failed units, watched services |
| Failed units | `services_failed` | |
| Updates (binary_sensor, problem) | `updates` | package lists in attributes |
| Pending / security updates | `updates_pending`, `updates_security` | |
| Kernel log (binary_sensor, problem) | `kernel` | counts per category, last messages |
| Kernel storage errors | `kernel_errors` | over `window_hours` |
| Inodes used | `fs_<mount>_inodes` | % (diagnostic) |

Every problem entity carries a `kind` attribute (`host`, `disk`, `filesystem`, `vault`,
`job`, `system`, `gpu`, `pool`, `services`, `updates`, `kernel`), used by the card.

### Other checks

| Check | Source | Issue |
|---|---|---|
| GPU temperature, video memory, slowdown | `nvidia-smi` / amdgpu sysfs | warning / critical; thermal slowdown or power brake = warning |
| Raspberry Pi power | firmware `get_throttled` (sysfs or `vcgencmd`) | under-voltage now = critical, since boot = warning, throttling = warning |
| Clock | `timedatectl` | not synchronised = warning |
| Read-only filesystem | `/proc/mounts` | critical unless listed in `readonly_ok` |
| Inodes | `statvfs` | thresholds |
| Kernel log | `journalctl -k` over `window_hours` | filesystem / disk I/O errors = critical; NVMe, SATA link, USB storage resets (only mass-storage devices, outside the 2 min after a resume or boot), under-voltage = warning |
| systemd | `systemctl` | failed unit = warning, watched service not active = critical |
| RAID | `/proc/mdstat` | degraded / inactive = critical, rebuild = warning |
| ZFS | `zpool list`, `zpool status` | not ONLINE or data errors = critical, scrub errors / scrub too old / capacity = warning |
| Updates | `apt-get -s dist-upgrade`, `dnf check-update` | security updates pending = warning |
| Backup consistency | `rsync --stats` at the end of each dirvish image log | size or file count drop, burst of changed files vs the median of the previous images = warning |

The agent account is added to the `video`, `render`, `systemd-journal` and `adm` groups (when
they exist) at each installation, for read access to the GPUs, the Raspberry Pi firmware and
the kernel log.

Each issue is `{id, subject, code, severity, message}`; `severity` is `warning` or
`critical`. Disks are keyed by serial number, so `sda` becoming `sdb` changes nothing.
A known disk, or a `required` mountpoint, that disappears is reported as a
**critical** issue until it comes back or is forgotten (`host-agent forget disk_xxx`).

### What is checked

- **ATA / SAT**: overall health, attributes failing now or in the past, raw counters
  (reallocated, pending, uncorrectable — configurable), SMART error log, self-test
  log (failure of the latest meaningful test, age of the last successful one;
  aborted/interrupted tests are ignored but counted), temperature.
- **NVMe**: critical warning bits, media errors, available spare vs threshold, wear
  (`percentage_used`), temperature vs the limits reported by the drive, self-test log
  when supported.
- **SCSI**: overall health, grown defects.
- smartctl exit status bits are decoded (e.g. bit 2 "some commands failed", typical
  of a USB bridge queried without `-d sat`).
- While a self-test runs, the drive is not polled (some USB bridges abort it).

### Self-tests

The agent schedules SMART self-tests itself (`smart.selftest`): a long test every
7 days and a short one every 24 h by default, only inside a local time window
(02:00–06:00). Due dates come from the drive's own self-test log, so nothing is lost
on restart and tests run by other tools are taken into account. After starting a
test, the agent does not query the drive until the expected duration has elapsed.
Each disk also exposes **Start short/long self-test** buttons.

Disable any `-s` schedule in `/etc/smartd.conf` (or smartd itself) to avoid
two schedulers fighting over the drive.

## MQTT contract (protocol 1)

| Topic | Retained | Payload |
|---|---|---|
| `homeassistant/device/<node>/config` | yes | device discovery document |
| `host_agent/<node>/availability` | yes | `online` / `offline` (LWT) |
| `host_agent/<node>/state/<key>` | yes | `{"state": ..., "attrs": {...}}` |
| `host_agent/<node>/cmd/refresh` | no | `PRESS` |
| `host_agent/<node>/cmd/forget` | no | subject key |

Discovery is republished when Home Assistant announces itself on
`homeassistant/status`.

### Backups and jobs

**dirvish**: the vaults of the banks listed in `/etc/dirvish/master.conf` (or
`collectors.dirvish.banks`) are found automatically. For each vault the agent reads the
`summary` of its images: last status (`success`, `warning (24)` counted as success by
default, other warnings, errors), age of the last successful image (critical above
`max_age_hours`, 26 h by default), image still running, per-day history. Unlike the former
script, a backup that has not run *yet today* is not an error.

For an immediate update after each run, add to `master.conf`:

```
post-server: /usr/local/bin/host-agent notify dirvish
```

**Jobs**: anything else that must run regularly.

```yaml
collectors:
  jobs:
    items:
      - name: rsync eric                     # newest file = last run
        path: /bkup/dirvish/rsync/eric/eric_*.log
        success_pattern: "total size is"     # optional: must appear at the end of the file
        max_age_hours: 48
      - name: offsite copy                   # pushed by the job itself
        report: true
```

A `report` job calls, when it ends:

```bash
host-agent report "offsite copy"                          # success
host-agent report "offsite copy" --failed --message "disk full"
```

The result is kept in `/var/lib/host-agent/reports/` (so a broker outage only delays it)
and the agent publishes it immediately.

## Migration from the bash scripts

1. Install and start the agent (step above), check the new device in HA.
2. Stop the old daemon and cron jobs:
   `systemctl disable --now home-assistant.service` and remove the
   `check-hd-mqtt.sh` / `mqtt_update.sh` cron entries.
   Since 0.3.0 `dirvish-chk-mqtt.sh`, `dirvish-chk.sh` (e-mail) and `rsync-chk-mqtt.sh`
   are replaced too (dirvish collector, `rsync` jobs, alert blueprint).
3. Delete the old entities (dry run first):
   ```bash
   host-agent cleanup-legacy --scope smart,space,update,vaults,rsync
   host-agent cleanup-legacy --scope smart,space,update,vaults,rsync --apply
   ```
4. Update the dashboard (`binary_sensor.bkup_health_dev_sda` →
   `binary_sensor.bkup_disk_<serial>`; **the logic is inverted**: ON now means
   *problem*), and replace the e-mail alerts with the blueprint.

## Development

```bash
cd agent
pip install -e '.[dev]'
pytest && ruff check src tests && mypy src
```

Test fixtures in `tests/fixtures` are real `smartctl --json` outputs (serial numbers
anonymised). New hardware: add `smartctl --json -a -d <type> /dev/xxx` output there.
