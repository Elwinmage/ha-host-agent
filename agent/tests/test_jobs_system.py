from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from host_agent.collectors.jobs import JobsCollector, write_report
from host_agent.collectors.system import SystemCollector, cpu_temperature, memory_used_pct
from host_agent.config import ConfigError, JobsConfig, JobSpec, SystemConfig, Thresholds, parse_config

NOW = datetime(2026, 11, 11, 12, 0).astimezone()


def touch(path, when: datetime, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def readings(result):
    return {r.key: r for r in result.readings}


def test_file_job_freshness(tmp_path, t_en):
    touch(tmp_path / "eric/eric_1.log", NOW - timedelta(hours=60), "sent 1 bytes\ntotal size is 42\n")
    touch(tmp_path / "eric/eric_2.log", NOW - timedelta(hours=5), "rsync error: timeout\n")
    jobs = JobsConfig(
        items=[
            JobSpec(
                "rsync eric",
                path=str(tmp_path / "eric/eric_*.log"),
                success_pattern="total size is",
                max_age_hours=48,
            ),
            JobSpec("rsync macherie", path=str(tmp_path / "macherie/*.log")),
        ]
    )
    result = JobsCollector(jobs, tmp_path / "reports", t_en, now=lambda: NOW).collect()
    eric = readings(result)["job_rsync_eric"]
    assert eric.attrs["kind"] == "job"
    assert eric.attrs["last_status"] == "failed"
    assert eric.attrs["last_file"].endswith("eric_2.log")
    assert {i["code"] for i in eric.attrs["issues"]} == {"job_outdated", "job_failed"}
    assert readings(result)["job_rsync_eric_last_success"].state == (NOW - timedelta(hours=60)).isoformat()
    assert readings(result)["job_rsync_macherie"].attrs["issues"][0]["code"] == "job_never"


def test_file_job_without_pattern(tmp_path, t_en):
    touch(tmp_path / "eric/eric_2.log", NOW - timedelta(hours=5))
    jobs = JobsConfig(items=[JobSpec("eric", path=str(tmp_path / "eric/*.log"))])
    eric = readings(JobsCollector(jobs, tmp_path, t_en, now=lambda: NOW).collect())["job_eric"]
    assert eric.state == "OFF"


def test_report_job(tmp_path, t_fr):
    reports = tmp_path / "reports"
    jobs = JobsConfig(items=[JobSpec("offsite", report=True, max_age_hours=26)])
    collector = JobsCollector(jobs, reports, t_fr, now=lambda: NOW)
    assert readings(collector.collect())["job_offsite"].attrs["issues"][0]["code"] == "job_never"

    write_report(reports, "offsite", True, None, NOW - timedelta(hours=3))
    write_report(reports, "offsite", False, "disque plein", NOW - timedelta(hours=1))
    job = readings(collector.collect())["job_offsite"]
    assert job.attrs["last_status"] == "failed"
    assert job.attrs["last_success"] == (NOW - timedelta(hours=3)).isoformat()
    assert [i["message"] for i in job.attrs["issues"]] == ["Dernière exécution en échec (disque plein)"]
    assert oct((reports / "offsite.json").stat().st_mode)[-3:] == "644"


def test_jobs_config_validation():
    cfg = parse_config(
        {
            "collectors": {
                "jobs": {
                    "items": [
                        {
                            "name": "rsync eric",
                            "path": "/bkup/dirvish/rsync/eric/eric_*.log",
                            "max_age_hours": 48,
                        },
                        {"name": "offsite", "report": True},
                    ]
                }
            }
        }
    )
    assert [j.key for j in cfg.jobs.items] == ["job_rsync_eric", "job_offsite"]
    for bad in (
        {"name": "x"},
        {"name": "x", "path": "/a", "report": True},
        {"name": "x", "path": "/a", "success_pattern": "("},
    ):
        with pytest.raises(ConfigError):
            parse_config({"collectors": {"jobs": {"items": [bad]}}})
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config(
            {
                "collectors": {
                    "jobs": {"items": [{"name": "a b", "path": "/x"}, {"name": "a-b", "path": "/y"}]}
                }
            }
        )


@pytest.fixture
def fake_root(tmp_path):
    hw = tmp_path / "sys/class/hwmon/hwmon0"
    hw.mkdir(parents=True)
    (hw / "name").write_text("cpu_thermal\n")
    (hw / "temp1_input").write_text("78312\n")
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/meminfo").write_text(
        "MemTotal: 1000000 kB\nMemFree: 100000 kB\nMemAvailable: 400000 kB\n"
    )
    (tmp_path / "proc/uptime").write_text("3600.5 7000.0\n")
    (tmp_path / "proc/loadavg").write_text("0.42 0.30 0.20 1/100 1234\n")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    (tmp_path / "run").mkdir()
    (tmp_path / "run/reboot-required").write_text("")
    return tmp_path


def test_system(fake_root, t_en):
    assert cpu_temperature(str(fake_root)) == 78.3
    assert memory_used_pct(str(fake_root)) == 60.0
    cfg = SystemConfig(memory=Thresholds(50, 90))
    result = SystemCollector(cfg, t_en, root=str(fake_root), now=lambda: NOW).collect()
    r = readings(result)
    assert r["system_cpu_temperature"].state == 78.3
    assert r["system_load"].state == 0.42
    assert r["system_boot"].state == (NOW - timedelta(seconds=3600.5)).replace(microsecond=0).isoformat()
    assert r["system"].attrs["reboot_required"] is True
    assert r["system"].attrs["os"] == "Debian GNU/Linux 13 (trixie)"
    assert {(i["code"], i["severity"]) for i in r["system"].attrs["issues"]} == {
        ("cpu_temperature_high", "warning"),
        ("memory_high", "warning"),
    }


def test_system_thermal_zone_fallback(tmp_path):
    zone = tmp_path / "sys/class/thermal/thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "type").write_text("x86_pkg_temp\n")
    (zone / "temp").write_text("45000\n")
    assert cpu_temperature(str(tmp_path)) == 45.0
