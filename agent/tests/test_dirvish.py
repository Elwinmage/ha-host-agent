from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from host_agent.collectors.dirvish import (
    DirvishCollector,
    classify_status,
    parse_banks,
    parse_summary,
)
from host_agent.config import DirvishConfig

NOW = datetime(2026, 11, 11, 12, 0).astimezone()

SUMMARY = """client: bkup
tree: /home
Server: bkup
Bank: {bank}
vault: {vault}
Image: {image}
Reference: 20261109
Image-now: {begin}
Expire: +30 days == 2026-12-10 03:00:01
exclude:
        lost+found/
SET permissions devices init numeric-ids stats xdev

ACTION: rsync -vrltH --delete --stats
Backup-begin: {begin}
Backup-complete: {complete}
Status: {status}
"""


def make_image(bank, vault, day: datetime, status="success", summary=True, complete_after=12):
    image = bank / vault / day.strftime("%Y%m%d")
    (image / "tree").mkdir(parents=True)
    if summary:
        begin = day.strftime("%Y-%m-%d %H:%M:%S")
        complete = (day + timedelta(minutes=complete_after)).strftime("%Y-%m-%d %H:%M:%S")
        (image / "summary").write_text(
            SUMMARY.format(
                bank=bank, vault=vault, image=image.name, begin=begin, complete=complete, status=status
            )
        )
    return image


@pytest.fixture
def bank(tmp_path):
    bank = tmp_path / "vaults"
    for vault in ("home", "mails", "mythtv"):
        (bank / vault / "dirvish").mkdir(parents=True)
    (bank / "not_a_vault").mkdir()
    return bank


def readings(result):
    return {r.key: r for r in result.readings}


def test_parse_summary_and_status():
    data = parse_summary(
        SUMMARY.format(
            bank="/b",
            vault="v",
            image="i",
            begin="2026-11-11 03:00:01",
            complete="2026-11-11 03:12:44",
            status="success",
        )
    )
    assert data["backup-begin"] == "2026-11-11 03:00:01"
    assert data["status"] == "success"
    assert classify_status("success", True) == "success"
    assert classify_status("warning (24) -- file vanished on sender", True) == "success"
    assert classify_status("warning (24) -- file vanished on sender", False) == "warning"
    assert classify_status("warning (23) -- partial transfer", True) == "warning"
    assert classify_status("error (255) -- unexplained error", True) == "failed"
    assert classify_status(None, True) == "failed"


def test_parse_banks(tmp_path):
    conf = tmp_path / "master.conf"
    conf.write_text("bank:\n\t/bkup/dirvish/vaults\n  /srv/other # comment\nexclude:\n\tlost+found/\n")
    assert parse_banks(str(conf)) == ["/bkup/dirvish/vaults", "/srv/other"]
    assert parse_banks(str(tmp_path / "missing")) == []


def test_healthy_vault_with_history(bank, t_fr):
    for days in range(5):
        make_image(bank, "home", NOW.replace(hour=3) - timedelta(days=days))
    make_image(bank, "home", NOW.replace(hour=3) - timedelta(days=5), status="error (255) -- boom")
    collector = DirvishCollector(
        DirvishConfig(banks=[str(bank)], exclude=["mythtv"], history_days=7), t_fr, now=lambda: NOW
    )
    result = collector.collect()
    keys = {e.key for e in result.entities}
    assert keys == {"vault_home", "vault_home_last_success", "vault_mails", "vault_mails_last_success"}
    home = readings(result)["vault_home"]
    assert home.state == "OFF"
    assert home.attrs["kind"] == "vault"
    assert home.attrs["last_status"] == "success"
    assert home.attrs["last_duration_s"] == 12 * 60
    assert [d["status"] for d in home.attrs["history"]] == [
        None,
        "failed",
        "success",
        "success",
        "success",
        "success",
        "success",
    ]
    assert readings(result)["vault_home_last_success"].state.startswith("2026-11-11T03:12")
    # empty vault: never backed up
    mails = readings(result)["vault_mails"]
    assert mails.attrs["issues"][0]["code"] == "vault_never"
    assert mails.attrs["issues"][0]["message"] == "Aucune sauvegarde réussie trouvée"


def test_outdated_and_failed(bank, t_en):
    make_image(bank, "home", NOW - timedelta(days=3))
    make_image(bank, "home", NOW - timedelta(hours=10), status="error (12) -- protocol error")
    collector = DirvishCollector(
        DirvishConfig(banks=[str(bank)], exclude=["mails", "mythtv"]), t_en, now=lambda: NOW
    )
    home = readings(collector.collect())["vault_home"]
    codes = {(i["code"], i["severity"]) for i in home.attrs["issues"]}
    assert codes == {("vault_outdated", "critical"), ("vault_failed", "critical")}
    assert home.attrs["severity"] == "critical"


def test_recent_failure_after_success_is_critical_but_not_outdated(bank, t_en):
    make_image(bank, "home", NOW - timedelta(days=1))
    make_image(bank, "home", NOW - timedelta(hours=2), status="warning (23) -- partial transfer")
    collector = DirvishCollector(
        DirvishConfig(banks=[str(bank)], exclude=["mails", "mythtv"]), t_en, now=lambda: NOW
    )
    home = readings(collector.collect())["vault_home"]
    assert [(i["code"], i["severity"]) for i in home.attrs["issues"]] == [("vault_failed", "warning")]


def test_running_image_is_not_a_failure(bank, t_en):
    make_image(bank, "home", NOW - timedelta(days=1))
    make_image(bank, "home", NOW - timedelta(minutes=30), summary=False)
    collector = DirvishCollector(
        DirvishConfig(banks=[str(bank)], exclude=["mails", "mythtv"]), t_en, now=lambda: NOW
    )
    home = readings(collector.collect())["vault_home"]
    assert home.state == "OFF"
    assert home.attrs["running"] is True
    assert home.attrs["history"][-1]["status"] == "running"


def test_keeps_excluded_vaults_released(bank, t_en):
    from host_agent.model import EntitySpec

    collector = DirvishCollector(DirvishConfig(banks=[str(bank)], exclude=["mythtv"]), t_en)
    assert collector.keeps(EntitySpec("vault_home", "binary_sensor", "x", subject_label="home"))
    assert not collector.keeps(EntitySpec("vault_mythtv", "binary_sensor", "x", subject_label="mythtv"))
