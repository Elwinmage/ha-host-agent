// Minimal subset of the Home Assistant frontend types used by the card.

export interface HassEntity {
  entity_id: string;
  state: string;
  attributes: Record<string, any>;
  last_changed: string;
  last_updated: string;
}

export interface HassEntityRegistryDisplayEntry {
  entity_id: string;
  device_id?: string;
  platform?: string;
  entity_category?: string;
  hidden?: boolean;
}

export interface HassDevice {
  id: string;
  name: string | null;
  name_by_user: string | null;
}

export interface HomeAssistant {
  user?: { is_admin?: boolean };
  states: Record<string, HassEntity>;
  entities: Record<string, HassEntityRegistryDisplayEntry>;
  devices: Record<string, HassDevice>;
  language: string;
  locale?: { language: string };
  callService(domain: string, service: string, data?: Record<string, unknown>): Promise<unknown>;
}

export type Section = "issues" | "system" | "disks" | "filesystems" | "storage" | "gpus" | "vaults" | "jobs";

export interface HostAgentCardConfig {
  type: string;
  device_id?: string;
  node_id?: string;
  title?: string;
  days?: number;
  show?: Section[];
  // State of the card when it loads: expanded (everything) or collapsed (header and alarms only)
  default_state?: "expanded" | "collapsed";
  // Expand a collapsed card when a critical problem appears
  auto_expand_critical?: boolean;
  // Reboot button (when the integration created it for this host); default true
  show_restart?: boolean;
}

export type Severity = "ok" | "warning" | "critical" | "unknown";

export interface Issue {
  id: string;
  subject: string;
  code: string;
  severity: "warning" | "critical";
  message: string;
  subject_name?: string;
}
