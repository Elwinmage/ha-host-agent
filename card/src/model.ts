// Resolve the entities of one host from the Home Assistant state.
//
// Problem binary sensors carry a `kind` attribute (host, disk, filesystem, vault, job,
// system); their metrics share the same object id plus a suffix, as created by the agent
// discovery (`<platform>.<node>_<key>`).

import type { HassEntity, HomeAssistant, HostAgentCardConfig, Issue, Severity } from "./types";

export interface Subject {
  entity: HassEntity;
  objectId: string;
  severity: Severity;
  issues: Issue[];
  missing: boolean;
  metric(suffix: string, domain?: string): HassEntity | undefined;
}

export interface HostModel {
  nodeId: string;
  name: string;
  host?: HassEntity;
  offline: boolean;
  severity: Severity;
  issues: Issue[];
  lastUpdate?: HassEntity;
  version?: HassEntity;
  update?: HassEntity;
  refresh?: HassEntity;
  system?: Subject;
  disks: Subject[];
  gpus: Subject[];
  pools: Subject[];
  services?: Subject;
  updates?: Subject;
  kernel?: Subject;
  filesystems: Subject[];
  vaults: Subject[];
  jobs: Subject[];
}

const UNAVAILABLE = new Set(["unavailable", "unknown"]);

function objectId(entityId: string): string {
  return entityId.split(".", 2)[1];
}

/** node_id of the configured host: explicit, else from the integration's update entity. */
export function resolveNodeId(hass: HomeAssistant, config: HostAgentCardConfig): string | undefined {
  if (config.node_id) return config.node_id;
  if (!config.device_id) return undefined;
  for (const entry of Object.values(hass.entities)) {
    if (entry.device_id !== config.device_id) continue;
    const nodeId = hass.states[entry.entity_id]?.attributes?.node_id;
    if (typeof nodeId === "string" && nodeId) return nodeId;
  }
  return undefined;
}

function severityOf(entity: HassEntity): Severity {
  if (UNAVAILABLE.has(entity.state)) return "unknown";
  if (entity.state !== "on") return "ok";
  return entity.attributes.severity === "critical" ? "critical" : "warning";
}

export function buildModel(hass: HomeAssistant, config: HostAgentCardConfig): HostModel | undefined {
  const nodeId = resolveNodeId(hass, config);
  if (!nodeId) return undefined;
  const prefix = `${nodeId}_`;
  const entities = Object.values(hass.states).filter((entity) => objectId(entity.entity_id).startsWith(prefix));
  if (!entities.length) return undefined;

  const byId = new Map(entities.map((entity) => [entity.entity_id, entity]));
  const find = (domain: string, oid: string) => byId.get(`${domain}.${oid}`);

  const subject = (entity: HassEntity): Subject => {
    const oid = objectId(entity.entity_id);
    return {
      entity,
      objectId: oid,
      severity: severityOf(entity),
      issues: (entity.attributes.issues as Issue[]) || [],
      missing: Boolean(entity.attributes.missing),
      metric: (suffix: string, domain = "sensor") => find(domain, `${oid}_${suffix}`),
    };
  };

  const problems = entities.filter(
    (entity) => entity.entity_id.startsWith("binary_sensor.") && typeof entity.attributes.kind === "string",
  );
  const ofKind = (kind: string) =>
    problems
      .filter((entity) => entity.attributes.kind === kind)
      .map(subject)
      .sort((a, b) => String(a.entity.attributes.friendly_name).localeCompare(String(b.entity.attributes.friendly_name)));

  const host = problems.find((entity) => entity.attributes.kind === "host") ?? find("binary_sensor", `${nodeId}_problem`);
  const deviceUpdate = config.device_id
    ? Object.values(hass.entities).find(
        (entry) => entry.device_id === config.device_id && entry.entity_id.startsWith("update."),
      )
    : undefined;
  const device = config.device_id ? hass.devices?.[config.device_id] : undefined;
  const offline = !host || UNAVAILABLE.has(host.state);
  const system = ofKind("system")[0];

  return {
    nodeId,
    name: config.title || device?.name_by_user || device?.name || nodeId,
    host,
    offline,
    severity: offline ? "unknown" : host ? severityOf(host) : "unknown",
    issues: (host?.attributes.issues as Issue[]) || [],
    lastUpdate: find("sensor", `${nodeId}_last_update`),
    version: find("sensor", `${nodeId}_agent_version`),
    update: deviceUpdate ? hass.states[deviceUpdate.entity_id] : undefined,
    refresh: find("button", `${nodeId}_refresh`),
    system,
    disks: ofKind("disk"),
    filesystems: ofKind("filesystem").sort((a, b) =>
      String(a.entity.attributes.mountpoint).localeCompare(String(b.entity.attributes.mountpoint)),
    ),
    vaults: ofKind("vault"),
    jobs: ofKind("job"),
    gpus: ofKind("gpu"),
    pools: ofKind("pool"),
    services: ofKind("services")[0],
    updates: ofKind("updates")[0],
    kernel: ofKind("kernel")[0],
  };
}

/** First host_agent device, for the card picker preview. */
export function firstDevice(hass: HomeAssistant): string | undefined {
  return Object.values(hass.entities).find((entry) => entry.platform === "host_agent" && entry.device_id)?.device_id;
}

export function numberState(entity?: HassEntity): number | undefined {
  if (!entity || UNAVAILABLE.has(entity.state)) return undefined;
  const value = Number(entity.state);
  return Number.isFinite(value) ? value : undefined;
}
