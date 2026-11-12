import type { HomeAssistant } from "./types";

const STRINGS = {
  en: {
    status_ok: "OK",
    status_warning: "Warning",
    status_critical: "Critical",
    status_offline: "Offline",
    status_unknown: "Unknown",
    no_host: "Select a Host Agent device in the card configuration.",
    not_found: "No agent data found for this device.",
    updated: "Updated {time}",
    agent: "agent {version}",
    update_available: "Update {version}",
    refresh: "Refresh",
    issues: "Issues",
    system: "System",
    disks: "Disks",
    filesystems: "Filesystems",
    vaults: "Backups",
    jobs: "Jobs",
    cpu: "CPU",
    load: "Load",
    memory: "Memory",
    booted: "Up since {time}",
    reboot_required: "Reboot required",
    selftest: "self-test {time}",
    selftest_never: "no self-test",
    power_on: "{hours} h",
    free: "{value} free",
    not_mounted: "not mounted",
    last_success: "last success {time}",
    never: "never",
    running: "running",
    missing: "missing",
    day_success: "success",
    day_warning: "warning",
    day_failed: "failed",
    day_running: "running",
    day_none: "no backup",
    storage: "RAID / ZFS",
    gpus: "Graphics cards",
    clock: "Clock",
    not_synced: "not synchronised",
    updates_count: "{count} updates",
    updates_security: "{count} updates · {security} security",
    up_to_date: "Up to date",
    services_failed: "{count} failed services",
    services_ok: "Services OK",
    kernel_errors: "{count} kernel errors ({hours} h)",
    kernel_ok: "Kernel log OK",
    undervoltage_now: "Under-voltage",
    undervoltage_past: "Under-voltage since boot",
    throttled: "Throttled",
    fan: "fan {value}",
    gpu_load: "GPU",
    gpu_vram: "VRAM",
    scrub: "scrub {time}",
    scrub_running: "scrub running",
    scrub_never: "never scrubbed",
    files: "{count} files",
    read_only: "read-only",
    // editor
    device_id: "Host",
    node_id: "Node id (agent installed without the integration)",
    title: "Title",
    days: "Days in the backup timeline",
    show: "Sections",
  },
  fr: {
    status_ok: "OK",
    status_warning: "Avertissement",
    status_critical: "Critique",
    status_offline: "Hors ligne",
    status_unknown: "Inconnu",
    no_host: "Choisissez un appareil Host Agent dans la configuration de la carte.",
    not_found: "Aucune donnée d'agent trouvée pour cet appareil.",
    updated: "Mis à jour {time}",
    agent: "agent {version}",
    update_available: "Mise à jour {version}",
    refresh: "Actualiser",
    issues: "Anomalies",
    system: "Système",
    disks: "Disques",
    filesystems: "Systèmes de fichiers",
    vaults: "Sauvegardes",
    jobs: "Tâches",
    cpu: "CPU",
    load: "Charge",
    memory: "Mémoire",
    booted: "Démarré {time}",
    reboot_required: "Redémarrage requis",
    selftest: "auto-test {time}",
    selftest_never: "aucun auto-test",
    power_on: "{hours} h",
    free: "{value} libres",
    not_mounted: "non monté",
    last_success: "dernière réussite {time}",
    never: "jamais",
    running: "en cours",
    missing: "introuvable",
    day_success: "réussie",
    day_warning: "avertissement",
    day_failed: "échec",
    day_running: "en cours",
    day_none: "aucune sauvegarde",
    storage: "RAID / ZFS",
    gpus: "Cartes graphiques",
    clock: "Horloge",
    not_synced: "non synchronisée",
    updates_count: "{count} mises à jour",
    updates_security: "{count} mises à jour · {security} sécurité",
    up_to_date: "À jour",
    services_failed: "{count} services en échec",
    services_ok: "Services OK",
    kernel_errors: "{count} erreurs noyau ({hours} h)",
    kernel_ok: "Noyau OK",
    undervoltage_now: "Sous-tension",
    undervoltage_past: "Sous-tension depuis le démarrage",
    throttled: "Bridé",
    fan: "ventilateur {value}",
    gpu_load: "GPU",
    gpu_vram: "VRAM",
    scrub: "scrub {time}",
    scrub_running: "scrub en cours",
    scrub_never: "jamais scrubé",
    files: "{count} fichiers",
    read_only: "lecture seule",
    device_id: "Hôte",
    node_id: "Identifiant de nœud (agent installé sans l'intégration)",
    title: "Titre",
    days: "Jours dans la frise des sauvegardes",
    show: "Sections",
  },
} as const;

export type StringKey = keyof (typeof STRINGS)["en"];

export function language(hass?: HomeAssistant): "en" | "fr" {
  const lang = (hass?.locale?.language || hass?.language || "en").slice(0, 2);
  return lang === "fr" ? "fr" : "en";
}

export function localize(hass: HomeAssistant | undefined, key: StringKey, vars: Record<string, string | number> = {}): string {
  let text: string = STRINGS[language(hass)][key] ?? STRINGS.en[key] ?? key;
  for (const [name, value] of Object.entries(vars)) {
    text = text.replace(`{${name}}`, String(value));
  }
  return text;
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 86400],
  ["month", 30 * 86400],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
  ["second", 1],
];

export function relativeTime(hass: HomeAssistant | undefined, value: string | Date | null | undefined): string {
  if (!value) return localize(hass, "never");
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return localize(hass, "never");
  const seconds = (date.getTime() - Date.now()) / 1000;
  const format = new Intl.RelativeTimeFormat(language(hass), { numeric: "auto" });
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size || unit === "second") {
      return format.format(Math.round(seconds / size), unit);
    }
  }
  return "";
}

export function formatBytes(hass: HomeAssistant | undefined, bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "-";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const number = new Intl.NumberFormat(language(hass), { maximumFractionDigits: value < 10 ? 1 : 0 }).format(value);
  return `${number} ${units[index]}`;
}

export function formatNumber(hass: HomeAssistant | undefined, value: number): string {
  return new Intl.NumberFormat(language(hass)).format(value);
}
