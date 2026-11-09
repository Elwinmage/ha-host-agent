"""Minimal translation table for entity names and issue messages."""

from __future__ import annotations

from typing import Any

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        # Host entities
        "host_problem": "Overall status",
        "host_problems": "Issues",
        "host_last_update": "Last update",
        "host_agent_version": "Agent version",
        "host_refresh": "Refresh",
        # Disk entities
        "disk_health": "Disk {label}",
        "disk_temperature": "Temperature {label}",
        "disk_selftest_age": "Last successful self-test {label}",
        # Filesystem entities
        "fs_problem": "Filesystem {mount}",
        "fs_used_pct": "Used space {mount}",
        "fs_free": "Free space {mount}",
        # Issues
        "smart_failed": "SMART overall health check FAILED",
        "attr_failing": "Attribute {name} is at or below the vendor threshold",
        "attr_failed_past": "Attribute {name} went below the vendor threshold in the past",
        "attr_nonzero": "{name} = {value} (max {max})",
        "selftest_failed": "Last self-test failed ({type}: {status})",
        "selftest_outdated": "No successful self-test for {hours} h ({aborted} aborted since)",
        "selftest_never": "No successful self-test recorded",
        "temperature_high": "Temperature {value} °C (limit {limit} °C)",
        "nvme_critical_warning": "NVMe critical warning: {flags}",
        "nvme_media_errors": "{value} media error(s)",
        "nvme_spare_low": "Available spare {value} % (threshold {limit} %)",
        "nvme_wear": "Wear level {value} % (limit {limit} %)",
        "scsi_grown_defects": "{value} grown defect(s)",
        "error_log": "{count} error(s) in the SMART error log",
        "smart_command_failed": "Some SMART commands failed (USB bridge? try device type 'sat')",
        "smart_unavailable": "SMART data unavailable: {detail}",
        "smart_exit_flag": "smartctl reports: {detail}",
        "missing": "Not found since {since}",
        "fs_usage": "Used space {value} % (limit {limit} %)",
        "fs_not_mounted": "Not mounted",
        "collector_failed": "Collector {name} failed: {error}",
    },
    "fr": {
        "host_problem": "État général",
        "host_problems": "Anomalies",
        "host_last_update": "Dernière mise à jour",
        "host_agent_version": "Version de l'agent",
        "host_refresh": "Actualiser",
        "disk_health": "Disque {label}",
        "disk_temperature": "Température {label}",
        "disk_selftest_age": "Dernier auto-test réussi {label}",
        "fs_problem": "Système de fichiers {mount}",
        "fs_used_pct": "Espace utilisé {mount}",
        "fs_free": "Espace libre {mount}",
        "smart_failed": "Auto-diagnostic SMART en ÉCHEC",
        "attr_failing": "Attribut {name} sous le seuil constructeur",
        "attr_failed_past": "Attribut {name} passé sous le seuil constructeur par le passé",
        "attr_nonzero": "{name} = {value} (max {max})",
        "selftest_failed": "Dernier auto-test en échec ({type} : {status})",
        "selftest_outdated": "Aucun auto-test réussi depuis {hours} h ({aborted} interrompu(s) depuis)",
        "selftest_never": "Aucun auto-test réussi enregistré",
        "temperature_high": "Température {value} °C (seuil {limit} °C)",
        "nvme_critical_warning": "Alerte critique NVMe : {flags}",
        "nvme_media_errors": "{value} erreur(s) média",
        "nvme_spare_low": "Réserve disponible {value} % (seuil {limit} %)",
        "nvme_wear": "Usure {value} % (seuil {limit} %)",
        "scsi_grown_defects": "{value} défaut(s) acquis",
        "error_log": "{count} erreur(s) dans le journal SMART",
        "smart_command_failed": "Certaines commandes SMART ont échoué (pont USB ? forcer le type 'sat')",
        "smart_unavailable": "Données SMART indisponibles : {detail}",
        "smart_exit_flag": "smartctl signale : {detail}",
        "missing": "Introuvable depuis {since}",
        "fs_usage": "Espace utilisé {value} % (seuil {limit} %)",
        "fs_not_mounted": "Non monté",
        "collector_failed": "Collecteur {name} en échec : {error}",
    },
}

SUPPORTED_LANGUAGES = tuple(TEXTS)


class Translator:
    def __init__(self, language: str = "en") -> None:
        self.language = language if language in TEXTS else "en"

    def __call__(self, key: str, **kwargs: Any) -> str:
        template = TEXTS[self.language].get(key) or TEXTS["en"].get(key) or key
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
