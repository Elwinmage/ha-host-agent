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
    expand: "Expand",
    collapse: "Collapse",
    wake: "Wake the host (Wake-on-LAN)",
    reboot: "Reboot the host",
    reboot_confirm: "Reboot?",
    update_lists: "Refresh the package lists (apt-get update)",
    upgrade_packages: "Upgrade the packages",
    upgrade_confirm: "Upgrade {count} packages? (configuration files are kept)",
    upgrade_running: "Upgrading the packages…",
    upgrade_failed: "Upgrade failed ({code})",
    upgrade_done: "Packages upgraded",
    upgrade_error: "Package update refused: {error}",
    confirm: "Confirm",
    cancel: "Cancel",
    selftest_start: "Start a self-test",
    selftest_short: "Short test",
    selftest_long: "Long test",
    selftest_long_confirm: "Confirm the long test (can take hours)",
    selftest_running: "self-test running",
    rule_edit: "Hide or limit this warning",
    rule_hide: "Stop showing this warning",
    rule_threshold: "Show it only above",
    rule_occurrences: "occurrences",
    rule_apply: "Apply",
    rule_remove: "Show again",
    rule_error: "Could not save the rule: {error}",
    masked_count: "{count} hidden",
    masked_ignore: "hidden",
    masked_threshold: "shown above {threshold} ({count} now)",
    kernel_rules: "Kernel warnings hidden",
    // editor
    default_state: "State when the card loads",
    expanded: "Expanded",
    collapsed: "Collapsed",
    auto_expand_critical: "Expand automatically on a critical problem",
    show_restart: "Show the reboot button",
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
    expand: "Déplier",
    collapse: "Replier",
    wake: "Réveiller l'hôte (Wake-on-LAN)",
    reboot: "Redémarrer l'hôte",
    reboot_confirm: "Redémarrer ?",
    update_lists: "Actualiser la liste des paquets (apt-get update)",
    upgrade_packages: "Mettre à jour les paquets",
    upgrade_confirm: "Mettre à jour {count} paquets ? (les fichiers de configuration sont conservés)",
    upgrade_running: "Mise à jour des paquets en cours…",
    upgrade_failed: "Échec de la mise à jour ({code})",
    upgrade_done: "Paquets mis à jour",
    upgrade_error: "Mise à jour des paquets refusée : {error}",
    confirm: "Confirmer",
    cancel: "Annuler",
    selftest_start: "Lancer un auto-test",
    selftest_short: "Test court",
    selftest_long: "Test long",
    selftest_long_confirm: "Confirmer le test long (peut durer des heures)",
    selftest_running: "auto-test en cours",
    rule_edit: "Masquer ou limiter cet avertissement",
    rule_hide: "Ne plus afficher cet avertissement",
    rule_threshold: "Ne l'afficher qu'au-delà de",
    rule_occurrences: "occurrences",
    rule_apply: "Appliquer",
    rule_remove: "Réafficher",
    rule_error: "Impossible d'enregistrer la règle : {error}",
    masked_count: "{count} masqué(s)",
    masked_ignore: "masqué",
    masked_threshold: "affiché au-delà de {threshold} ({count} actuellement)",
    kernel_rules: "Avertissements du noyau masqués",
    default_state: "État au chargement de la carte",
    expanded: "Dépliée",
    collapsed: "Repliée",
    auto_expand_critical: "Déplier automatiquement sur un problème critique",
    show_restart: "Afficher le bouton de redémarrage",
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
