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
  states: Record<string, HassEntity>;
  entities: Record<string, HassEntityRegistryDisplayEntry>;
  devices: Record<string, HassDevice>;
  language: string;
  locale?: { language: string };
  callService(domain: string, service: string, data?: Record<string, unknown>): Promise<unknown>;
}

export type Section = "issues" | "system" | "disks" | "filesystems" | "vaults" | "jobs";

export interface HostAgentCardConfig {
  type: string;
  device_id?: string;
  node_id?: string;
  title?: string;
  days?: number;
  show?: Section[];
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
