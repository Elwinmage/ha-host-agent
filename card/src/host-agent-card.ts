import { LitElement, css, html, nothing, type PropertyValues, type TemplateResult } from "lit";
import { property, state } from "lit/decorators.js";

import "./editor";
import { formatBytes, formatNumber, localize, relativeTime, type StringKey } from "./localize";
import { buildModel, firstDevice, numberState, type HostModel, type Subject } from "./model";
import type { HassEntity, HomeAssistant, HostAgentCardConfig, Section, Severity } from "./types";

const VERSION = "__VERSION__";
export const ALL_SECTIONS: Section[] = ["issues", "system", "disks", "filesystems", "storage", "gpus", "vaults", "jobs"];

const STATUS_ICON: Record<Severity, string> = {
  ok: "mdi:check-circle",
  warning: "mdi:alert",
  critical: "mdi:alert-octagon",
  unknown: "mdi:help-circle",
};

export class HostAgentCard extends LitElement {
  @property({ attribute: false }) public hass?: HomeAssistant;
  @state() private config?: HostAgentCardConfig;
  // Ephemeral: back to the configured default when the card is reloaded
  @state() private expanded = true;
  // Pending confirmation ("reboot", "<disk>:long") and open panels
  @state() private confirming?: string;
  @state() private diskMenu?: string;
  @state() private ruleEditor?: { category: string; threshold: string };
  @state() private showRules = false;
  private lastSeverity?: Severity;
  private confirmTimer?: number;

  public static getConfigElement(): HTMLElement {
    return document.createElement("host-agent-card-editor");
  }

  public static getStubConfig(hass: HomeAssistant): Partial<HostAgentCardConfig> {
    return { device_id: firstDevice(hass) };
  }

  public setConfig(config: HostAgentCardConfig): void {
    if (!config) throw new Error("Invalid configuration");
    this.config = { days: 14, ...config };
    this.expanded = (config.default_state ?? "expanded") === "expanded";
    this.lastSeverity = undefined;
  }

  public disconnectedCallback(): void {
    super.disconnectedCallback();
    window.clearTimeout(this.confirmTimer);
  }

  public getCardSize(): number {
    const model = this.model();
    if (!model) return 2;
    if (!this.expanded) return 1 + model.issues.length;
    return 2 + model.disks.length + model.filesystems.length + model.vaults.length + model.jobs.length;
  }

  public getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  protected shouldUpdate(changed: PropertyValues): boolean {
    if (changed.has("config")) return true;
    const old = changed.get("hass") as HomeAssistant | undefined;
    if (!old || !this.hass) return true;
    // Re-render only when one of this host's entities changed
    const nodeId = this.model()?.nodeId;
    if (!nodeId) return true;
    const prefix = `.${nodeId}_`;
    for (const id in this.hass.states) {
      if (id.includes(prefix) && old.states[id] !== this.hass.states[id]) return true;
    }
    return false;
  }

  protected willUpdate(changed: PropertyValues): void {
    if (!changed.has("hass") && !changed.has("config")) return;
    const severity = this.model()?.severity;
    // A problem that turns critical (or is critical at load) opens a collapsed card, once:
    // closing it again by hand is respected until the next critical event
    if (this.config?.auto_expand_critical && severity === "critical" && this.lastSeverity !== "critical") {
      this.expanded = true;
    }
    this.lastSeverity = severity;
  }

  private model(): HostModel | undefined {
    return this.hass && this.config ? buildModel(this.hass, this.config) : undefined;
  }

  private t(key: StringKey, vars?: Record<string, string | number>): string {
    return localize(this.hass, key, vars);
  }

  private shown(section: Section): boolean {
    return !this.config?.show?.length || this.config.show.includes(section);
  }

  private moreInfo(entityId?: string): void {
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  private async press(entity?: HassEntity, event?: Event): Promise<void> {
    event?.stopPropagation();
    if (entity && this.hass) await this.hass.callService("button", "press", { entity_id: entity.entity_id });
  }

  /** Ask for a second click on a risky action; the request lapses by itself. */
  private arm(key: string): void {
    this.confirming = key;
    window.clearTimeout(this.confirmTimer);
    this.confirmTimer = window.setTimeout(() => (this.confirming = undefined), 6000);
  }

  private disarm(): void {
    window.clearTimeout(this.confirmTimer);
    this.confirming = undefined;
  }

  private notify(message: string): void {
    this.dispatchEvent(new CustomEvent("hass-notification", { detail: { message }, bubbles: true, composed: true }));
  }

  private async setRule(
    model: HostModel,
    category: string,
    mode: "ignore" | "threshold" | "remove",
    threshold?: number,
  ): Promise<void> {
    if (!this.hass) return;
    try {
      await this.hass.callService("host_agent", "set_kernel_rule", {
        node_id: model.nodeId,
        category,
        mode,
        ...(threshold !== undefined ? { threshold } : {}),
      });
      this.ruleEditor = undefined;
    } catch (err) {
      this.notify(this.t("rule_error", { error: err instanceof Error ? err.message : String((err as any)?.message ?? err) }));
    }
  }

  // ------------------------------------------------------------------ //
  // Rendering
  // ------------------------------------------------------------------ //

  protected render(): TemplateResult {
    if (!this.hass || !this.config) return html``;
    if (!this.config.device_id && !this.config.node_id) {
      return html`<ha-card><div class="empty">${this.t("no_host")}</div></ha-card>`;
    }
    const model = this.model();
    if (!model) {
      return html`<ha-card><div class="empty">${this.t("not_found")}</div></ha-card>`;
    }
    const status = model.offline ? "offline" : model.severity;
    // Collapsed: the header (coloured by the worst problem) and the alarms, nothing else
    if (!this.expanded) {
      return html`
        <ha-card class="sev-${model.severity} collapsed">
          ${this.renderHeader(model, status)} ${this.renderIssues(model)}
        </ha-card>
      `;
    }
    return html`
      <ha-card class="sev-${model.severity}">
        ${this.renderHeader(model, status)}
        ${this.shown("issues") ? this.renderIssues(model) : nothing}
        ${this.shown("system") && model.system ? this.renderSystem(model.system, model) : nothing}
        ${this.shown("disks") && model.disks.length ? this.renderDisks(model.disks, model.offline) : nothing}
        ${this.shown("filesystems") && model.filesystems.length ? this.renderFilesystems(model.filesystems) : nothing}
        ${this.shown("storage") && model.pools.length ? this.renderPools(model.pools) : nothing}
        ${this.shown("gpus") && model.gpus.length ? this.renderGpus(model.gpus) : nothing}
        ${this.shown("vaults") && model.vaults.length ? this.renderVaults(model.vaults) : nothing}
        ${this.shown("jobs") && model.jobs.length ? this.renderJobs(model.jobs) : nothing}
      </ha-card>
    `;
  }

  private renderHeader(model: HostModel, status: string): TemplateResult {
    const update = model.update;
    const updateAvailable = update?.state === "on";
    const lastUpdate = model.lastUpdate?.state;
    return html`
      <div class="header">
        <ha-icon class="host-icon" icon="mdi:server"></ha-icon>
        <div class="title">
          <div class="name">${model.name}</div>
          <div class="meta">
            <button class="status ${status}" @click=${() => this.moreInfo(model.host?.entity_id)}>
              <ha-icon icon=${model.offline ? "mdi:lan-disconnect" : STATUS_ICON[model.severity]}></ha-icon>
              ${this.t(`status_${status}` as StringKey)}
            </button>
            ${updateAvailable
              ? html`<button class="chip update" @click=${() => this.moreInfo(update?.entity_id)}>
                  <ha-icon icon="mdi:package-up"></ha-icon>
                  ${this.t("update_available", { version: update?.attributes.latest_version })}
                </button>`
              : nothing}
            <span class="secondary">
              ${lastUpdate && lastUpdate !== "unknown" && lastUpdate !== "unavailable"
                ? this.t("updated", { time: relativeTime(this.hass, lastUpdate) })
                : nothing}
              ${model.version && model.version.state !== "unavailable"
                ? html`· ${this.t("agent", { version: model.version.state })}`
                : nothing}
            </span>
          </div>
        </div>
        <div class="actions">
          ${this.renderWake(model)} ${this.renderReboot(model)}
          ${model.refresh
            ? html`<button
                class="icon-button"
                title=${this.t("refresh")}
                ?disabled=${model.offline}
                @click=${(ev: Event) => this.press(model.refresh, ev)}
              >
                <ha-icon icon="mdi:refresh"></ha-icon>
              </button>`
            : nothing}
          <button
            class="icon-button"
            title=${this.t(this.expanded ? "collapse" : "expand")}
            aria-expanded=${this.expanded ? "true" : "false"}
            @click=${() => (this.expanded = !this.expanded)}
          >
            <ha-icon icon=${this.expanded ? "mdi:chevron-up" : "mdi:chevron-down"}></ha-icon>
          </button>
        </div>
      </div>
    `;
  }

  /** Wake-on-LAN: offered while the host is offline, when the integration created the button. */
  private renderWake(model: HostModel): TemplateResult | typeof nothing {
    if (!model.offline || !model.wake) return nothing;
    return html`<button
      class="icon-button wake"
      title=${this.t("wake")}
      ?disabled=${model.wake.state === "unavailable"}
      @click=${(ev: Event) => this.press(model.wake, ev)}
    >
      <ha-icon icon="mdi:power"></ha-icon>
    </button>`;
  }

  /** Reboot the host, after a confirmation; hidden by `show_restart: false`. */
  private renderReboot(model: HostModel): TemplateResult | typeof nothing {
    if (model.offline || !model.reboot || this.config?.show_restart === false) return nothing;
    if (this.confirming === "reboot") {
      return html`<span class="confirm">
        ${this.t("reboot_confirm")}
        <button
          class="btn danger"
          @click=${async (ev: Event) => {
            this.disarm();
            await this.press(model.reboot, ev);
          }}
        >
          ${this.t("confirm")}
        </button>
        <button class="btn" @click=${() => this.disarm()}>${this.t("cancel")}</button>
      </span>`;
    }
    return html`<button
      class="icon-button"
      title=${this.t("reboot")}
      ?disabled=${model.reboot.state === "unavailable"}
      @click=${(ev: Event) => {
        ev.stopPropagation();
        this.arm("reboot");
      }}
    >
      <ha-icon icon="mdi:restart"></ha-icon>
    </button>`;
  }

  private renderIssues(model: HostModel): TemplateResult | typeof nothing {
    if (!model.issues.length || model.offline) return nothing;
    // Kernel warnings can be hidden or given a threshold (admins); critical ones never
    const admin = this.hass?.user?.is_admin !== false;
    const kernelAttrs = model.kernel?.entity.attributes;
    const protectedCategories = (kernelAttrs?.protected as string[] | undefined) ?? ["filesystem", "io"];
    return html`
      <div class="issues">
        ${model.issues.map((issue) => {
          const category = issue.subject === "kernel" && issue.code.startsWith("kernel_") ? issue.code.slice(7) : undefined;
          const editable = admin && category !== undefined && !protectedCategories.includes(category);
          return html`<div class="issue-wrap">
            <div class="issue ${issue.severity}">
              <ha-icon icon=${STATUS_ICON[issue.severity]}></ha-icon>
              <span>${issue.subject_name ? html`<b>${issue.subject_name}</b> · ` : nothing}${issue.message}</span>
              ${editable
                ? html`<button
                    class="icon-button small"
                    title=${this.t("rule_edit")}
                    @click=${() => this.toggleRuleEditor(category!, kernelAttrs?.counts?.[category!])}
                  >
                    <ha-icon icon="mdi:eye-off-outline"></ha-icon>
                  </button>`
                : nothing}
            </div>
            ${editable && this.ruleEditor?.category === category ? this.renderRuleEditor(model, category!) : nothing}
          </div>`;
        })}
      </div>
    `;
  }

  private toggleRuleEditor(category: string, count?: number): void {
    this.ruleEditor = this.ruleEditor?.category === category ? undefined : { category, threshold: String(count ?? 1) };
  }

  private renderRuleEditor(model: HostModel, category: string): TemplateResult {
    const threshold = Number(this.ruleEditor?.threshold);
    const valid = Number.isInteger(threshold) && threshold >= 0;
    return html`<div class="rule-editor">
      <button class="btn" @click=${() => this.setRule(model, category, "ignore")}>
        <ha-icon icon="mdi:eye-off"></ha-icon>${this.t("rule_hide")}
      </button>
      <div class="threshold">
        <span>${this.t("rule_threshold")}</span>
        <input
          type="number"
          min="0"
          .value=${this.ruleEditor?.threshold ?? ""}
          @input=${(ev: Event) => {
            this.ruleEditor = { category, threshold: (ev.target as HTMLInputElement).value };
          }}
        />
        <span>${this.t("rule_occurrences")}</span>
        <button class="btn" ?disabled=${!valid} @click=${() => this.setRule(model, category, "threshold", threshold)}>
          ${this.t("rule_apply")}
        </button>
      </div>
    </div>`;
  }

  /** Chip of the rules in force, opening the list that lets an admin show a warning again. */
  private renderRules(model: HostModel): TemplateResult | typeof nothing {
    const attrs = model.kernel?.entity.attributes;
    const rules = (attrs?.rules ?? {}) as Record<string, { ignore?: boolean; threshold?: number | null }>;
    const masked = (attrs?.masked ?? {}) as Record<string, { count: number }>;
    const counts = (attrs?.counts ?? {}) as Record<string, number>;
    if (!Object.keys(rules).length) return nothing;
    const admin = this.hass?.user?.is_admin !== false;
    return html`
      <button class="chip plain" @click=${() => (this.showRules = !this.showRules)}>
        <ha-icon icon="mdi:eye-off-outline"></ha-icon>${this.t("masked_count", { count: Object.keys(masked).length })}
      </button>
      ${this.showRules
        ? html`<div class="rules">
            <div class="secondary">${this.t("kernel_rules")}</div>
            ${Object.entries(rules).map(
              ([category, rule]) => html`<div class="rule-row">
                <b>${category}</b>
                <span class="secondary">
                  ${rule.ignore
                    ? this.t("masked_ignore")
                    : this.t("masked_threshold", {
                        threshold: rule.threshold ?? 0,
                        count: masked[category]?.count ?? counts[category] ?? 0,
                      })}
                </span>
                ${admin
                  ? html`<button class="btn" @click=${() => this.setRule(model, category, "remove")}>
                      ${this.t("rule_remove")}
                    </button>`
                  : nothing}
              </div>`,
            )}
          </div>`
        : nothing}
    `;
  }

  private sectionTitle(key: StringKey, icon: string): TemplateResult {
    return html`<div class="section-title"><ha-icon icon=${icon}></ha-icon>${this.t(key)}</div>`;
  }

  private renderSystem(system: Subject, model: HostModel): TemplateResult {
    const temp = numberState(system.metric("cpu_temperature"));
    const load = numberState(system.metric("load"));
    const memory = numberState(system.metric("memory"));
    const boot = system.metric("boot")?.state;
    const cpus = system.entity.attributes.cpu_count as number | undefined;
    const chip = (icon: string, label: string, value: string, entity?: HassEntity, sev: Severity = "ok") =>
      html`<button class="chip sev-${sev}" @click=${() => this.moreInfo(entity?.entity_id)}>
        <ha-icon icon=${icon}></ha-icon><span class="label">${label}</span>${value}
      </button>`;
    const sevFor = (code: string): Severity =>
      (system.issues.find((issue) => issue.code === code)?.severity as Severity) ?? "ok";
    return html`
      <div class="section">
        ${this.sectionTitle("system", "mdi:chip")}
        <div class="chips">
          ${temp !== undefined
            ? chip("mdi:thermometer", this.t("cpu"), `${temp.toFixed(0)} °C`, system.metric("cpu_temperature"), sevFor("cpu_temperature_high"))
            : nothing}
          ${load !== undefined
            ? chip("mdi:gauge", this.t("load"), cpus ? `${load.toFixed(2)} / ${cpus}` : load.toFixed(2), system.metric("load"),
                cpus && load > cpus ? "warning" : "ok")
            : nothing}
          ${memory !== undefined
            ? chip("mdi:memory", this.t("memory"), `${memory.toFixed(0)} %`, system.metric("memory"), sevFor("memory_high"))
            : nothing}
          ${boot && boot !== "unknown" && boot !== "unavailable"
            ? html`<span class="chip plain"><ha-icon icon="mdi:power"></ha-icon>${this.t("booted", { time: relativeTime(this.hass, boot) })}</span>`
            : nothing}
          ${system.entity.attributes.reboot_required
            ? html`<span class="chip sev-warning"><ha-icon icon="mdi:restart-alert"></ha-icon>${this.t("reboot_required")}</span>`
            : nothing}
          ${this.renderPowerChip(system)}
          ${system.entity.attributes.ntp_synchronized === false
            ? chip("mdi:clock-alert", this.t("clock"), this.t("not_synced"), system.entity, "warning")
            : nothing}
          ${this.renderSubjectChip(model.updates, "mdi:package-up", (s) => {
            const pending = numberState(s.metric("pending")) ?? 0;
            const security = numberState(s.metric("security")) ?? 0;
            return pending
              ? security
                ? this.t("updates_security", { count: pending, security })
                : this.t("updates_count", { count: pending })
              : this.t("up_to_date");
          })}
          ${this.renderUpdateActions(model)}
          ${this.renderSubjectChip(model.services, "mdi:cogs", (s) => {
            const failed = numberState(s.metric("failed")) ?? 0;
            const down = s.issues.filter((issue) => issue.code.startsWith("service_down")).length;
            return failed || down ? this.t("services_failed", { count: failed + down }) : this.t("services_ok");
          })}
          ${this.renderSubjectChip(model.kernel, "mdi:console-line", (s) => {
            const errors = numberState(s.metric("errors")) ?? 0;
            return errors
              ? this.t("kernel_errors", { count: errors, hours: s.entity.attributes.window_hours ?? 24 })
              : this.t("kernel_ok");
          })}
          ${this.renderRules(model)}
        </div>
      </div>
    `;
  }

  /** Run a button of the integration; a refusal (backup running, helper outdated...) is shown as a notification. */
  private async runAction(entity: HassEntity | undefined, event?: Event): Promise<void> {
    try {
      await this.press(entity, event);
    } catch (err) {
      const error = err instanceof Error ? err.message : String((err as any)?.message ?? err);
      this.notify(this.t("upgrade_error", { error }));
    }
  }

  /** apt hosts: refresh the lists, upgrade (admins, after a confirmation) and follow the upgrade. */
  private renderUpdateActions(model: HostModel): TemplateResult | typeof nothing {
    if (model.offline || model.updates?.entity.attributes.manager !== "apt") return nothing;
    const attrs = model.aptUpgrade?.attributes ?? {};
    const state = attrs.upgrade_state as string | undefined;
    const admin = this.hass?.user?.is_admin !== false;
    if (state === "running") {
      return html`<span class="chip sev-warning"><ha-icon icon="mdi:progress-wrench"></ha-icon>${this.t("upgrade_running")}</span>`;
    }
    const pending = numberState(model.updates?.metric("pending")) ?? 0;
    const finished = Number(attrs.upgrade_finished) || 0;
    const recent = finished > 0 && Math.abs(Date.now() / 1000 - finished) < 3600;
    const log = Array.isArray(attrs.upgrade_log) ? (attrs.upgrade_log as string[]).join("\n") : "";
    return html`
      ${state === "failed"
        ? html`<span class="chip sev-critical" title=${log}>
            <ha-icon icon="mdi:package-variant-remove"></ha-icon>${this.t("upgrade_failed", { code: attrs.upgrade_exit ?? "?" })}
          </span>`
        : state === "ok" && recent
          ? html`<span class="chip sev-ok" title=${log}><ha-icon icon="mdi:package-variant-closed-check"></ha-icon>${this.t("upgrade_done")}</span>`
          : nothing}
      ${model.aptUpdate && admin
        ? html`<button
            class="icon-button small"
            title=${this.t("update_lists")}
            ?disabled=${model.aptUpdate.state === "unavailable"}
            @click=${(ev: Event) => this.runAction(model.aptUpdate, ev)}
          >
            <ha-icon icon="mdi:database-refresh"></ha-icon>
          </button>`
        : nothing}
      ${model.aptUpgrade && admin
        ? this.confirming === "upgrade"
          ? html`<span class="confirm">
              ${this.t("upgrade_confirm", { count: pending })}
              <button
                class="btn danger"
                @click=${async (ev: Event) => {
                  this.disarm();
                  await this.runAction(model.aptUpgrade, ev);
                }}
              >
                ${this.t("confirm")}
              </button>
              <button class="btn" @click=${() => this.disarm()}>${this.t("cancel")}</button>
            </span>`
          : html`<button
              class="icon-button small"
              title=${this.t("upgrade_packages")}
              ?disabled=${model.aptUpgrade.state === "unavailable" || pending === 0}
              @click=${(ev: Event) => {
                ev.stopPropagation();
                this.arm("upgrade");
              }}
            >
              <ha-icon icon="mdi:package-up"></ha-icon>
            </button>`
        : nothing}
    `;
  }

  /** Chip summarising a whole-host subject (updates, services, kernel log). */
  private renderSubjectChip(
    subject: Subject | undefined,
    icon: string,
    text: (subject: Subject) => string,
  ): TemplateResult | typeof nothing {
    if (!subject) return nothing;
    return html`<button class="chip sev-${subject.severity}" @click=${() => this.moreInfo(subject.entity.entity_id)}>
      <ha-icon icon=${icon}></ha-icon>${text(subject)}
    </button>`;
  }

  private renderPowerChip(system: Subject): TemplateResult | typeof nothing {
    const flags = (system.entity.attributes.pi_flags as string[] | undefined) ?? [];
    if (!flags.length) return nothing;
    const now = flags.includes("under_voltage");
    const label = now ? this.t("undervoltage_now") : flags.includes("under_voltage_occurred")
      ? this.t("undervoltage_past") : this.t("throttled");
    return html`<button class="chip sev-${now ? "critical" : "warning"}" @click=${() => this.moreInfo(system.entity.entity_id)}>
      <ha-icon icon="mdi:flash-alert"></ha-icon>${label}
    </button>`;
  }

  private bar(label: string, value: number | undefined, severity: Severity = "ok"): TemplateResult | typeof nothing {
    if (value === undefined) return nothing;
    return html`<div class="mini">
      <span class="mini-label">${label}</span>
      <div class="bar"><div class="fill sev-${severity}" style="width:${Math.min(value, 100)}%"></div></div>
      <span class="mini-value">${value.toFixed(0)} %</span>
    </div>`;
  }

  private renderGpus(gpus: Subject[]): TemplateResult {
    return html`
      <div class="section">
        ${this.sectionTitle("gpus", "mdi:expansion-card")}
        ${gpus.map((gpu) => {
          const attrs = gpu.entity.attributes;
          const temp = numberState(gpu.metric("temperature"));
          const power = numberState(gpu.metric("power"));
          const fan = attrs.fan as number | null | undefined;
          const details = [
            attrs.vendor === "nvidia" ? "NVIDIA" : attrs.vendor === "amd" ? "AMD" : attrs.vendor,
            power !== undefined
              ? attrs.power_limit_w ? `${power.toFixed(0)} / ${Number(attrs.power_limit_w).toFixed(0)} W` : `${power.toFixed(0)} W`
              : undefined,
            fan !== null && fan !== undefined ? this.t("fan", { value: attrs.fan_unit === "rpm" ? `${fan} rpm` : `${fan} %` }) : undefined,
          ].filter(Boolean);
          const memorySeverity: Severity = gpu.issues.some((issue) => issue.code === "gpu_memory_high") ? "warning" : "ok";
          return html`
            <div class="row" @click=${() => this.moreInfo(gpu.entity.entity_id)}>
              <ha-icon class="state sev-${gpu.severity}" icon="mdi:expansion-card"></ha-icon>
              <div class="info">
                <div class="primary">${attrs.name}</div>
                <div class="secondary">${details.join(" · ")}</div>
                ${this.bar(this.t("gpu_load"), numberState(gpu.metric("utilization")))}
                ${this.bar(this.t("gpu_vram"), numberState(gpu.metric("memory")), memorySeverity)}
              </div>
              ${temp !== undefined ? html`<div class="value">${temp.toFixed(0)} °C</div>` : nothing}
            </div>
            ${this.renderSubjectIssues(gpu)}
          `;
        })}
      </div>
    `;
  }

  private renderPools(pools: Subject[]): TemplateResult {
    return html`
      <div class="section">
        ${this.sectionTitle("storage", "mdi:database")}
        ${pools.map((pool) => {
          const attrs = pool.entity.attributes;
          const capacity = numberState(pool.metric("capacity"));
          const zfs = attrs.type === "zfs";
          const details = zfs
            ? [
                `ZFS · ${attrs.health}`,
                attrs.size_bytes ? formatBytes(this.hass, attrs.size_bytes) : undefined,
                attrs.scrub_running
                  ? this.t("scrub_running")
                  : attrs.last_scrub
                    ? this.t("scrub", { time: relativeTime(this.hass, attrs.last_scrub) })
                    : this.t("scrub_never"),
              ]
            : [
                `${attrs.level} · [${attrs.status ?? "?"}]`,
                attrs.action ? `${attrs.action} ${attrs.progress ?? ""} %` : undefined,
              ];
          return html`
            <div class="row" @click=${() => this.moreInfo(pool.entity.entity_id)}>
              <ha-icon class="state sev-${pool.severity}" icon=${zfs ? "mdi:database" : "mdi:harddisk-plus"}></ha-icon>
              <div class="info">
                <div class="primary">${attrs.name}</div>
                <div class="secondary">${details.filter(Boolean).join(" · ")}</div>
              </div>
              ${capacity !== undefined ? html`<div class="value">${capacity.toFixed(0)} %</div>` : nothing}
            </div>
            ${this.renderSubjectIssues(pool)}
          `;
        })}
      </div>
    `;
  }

  private renderSelfTest(disk: Subject, offline: boolean): TemplateResult | typeof nothing {
    // The agent publishes one button per kind of test and per disk; the card only links to them
    if ((!disk.metric("selftest_short", "button") && !disk.metric("selftest_long", "button")) || disk.missing) {
      return nothing;
    }
    return html`<button
      class="icon-button"
      title=${this.t("selftest_start")}
      ?disabled=${offline || Boolean(disk.entity.attributes.selftest_in_progress)}
      @click=${(ev: Event) => {
        ev.stopPropagation();
        this.disarm();
        this.diskMenu = this.diskMenu === disk.objectId ? undefined : disk.objectId;
      }}
    >
      <ha-icon icon="mdi:clipboard-pulse-outline"></ha-icon>
    </button>`;
  }

  private renderDiskMenu(disk: Subject): TemplateResult | typeof nothing {
    if (this.diskMenu !== disk.objectId) return nothing;
    const short = disk.metric("selftest_short", "button");
    const long = disk.metric("selftest_long", "button");
    const key = `${disk.objectId}:long`;
    const start = async (entity: HassEntity) => {
      await this.press(entity);
      this.diskMenu = undefined;
      this.disarm();
    };
    return html`<div class="disk-menu">
      ${short ? html`<button class="btn" @click=${() => start(short)}>${this.t("selftest_short")}</button>` : nothing}
      ${long
        ? this.confirming === key
          ? html`<button class="btn danger" @click=${() => start(long)}>${this.t("selftest_long_confirm")}</button>
              <button class="btn" @click=${() => this.disarm()}>${this.t("cancel")}</button>`
          : html`<button class="btn" @click=${() => this.arm(key)}>${this.t("selftest_long")}</button>`
        : nothing}
    </div>`;
  }

  private renderDisks(disks: Subject[], offline: boolean): TemplateResult {
    return html`
      <div class="section">
        ${this.sectionTitle("disks", "mdi:harddisk")}
        ${disks.map((disk) => {
          const attrs = disk.entity.attributes;
          const temp = numberState(disk.metric("temperature"));
          const ageHours = numberState(disk.metric("selftest_age"));
          const details = [
            attrs.selftest_in_progress ? this.t("selftest_running") : undefined,
            attrs.device,
            attrs.capacity_bytes ? formatBytes(this.hass, attrs.capacity_bytes) : undefined,
            attrs.power_on_hours
              ? this.t("power_on", { hours: formatNumber(this.hass, Number(attrs.power_on_hours)) })
              : undefined,
            attrs.selftest_supported
              ? ageHours !== undefined
                ? this.t("selftest", { time: relativeTime(this.hass, new Date(Date.now() - ageHours * 3600_000)) })
                : this.t("selftest_never")
              : undefined,
          ].filter(Boolean);
          return html`
            <div class="row" @click=${() => this.moreInfo(disk.entity.entity_id)}>
              <ha-icon class="state sev-${disk.severity}" icon=${disk.missing ? "mdi:harddisk-remove" : "mdi:harddisk"}></ha-icon>
              <div class="info">
                <div class="primary">${attrs.model || attrs.label || disk.objectId}</div>
                <div class="secondary">${disk.missing ? this.t("missing") : details.join(" · ")}</div>
              </div>
              ${temp !== undefined ? html`<div class="value">${temp} °C</div>` : nothing}
              ${attrs.selftest_in_progress ? html`<ha-icon class="spin" icon="mdi:progress-clock"></ha-icon>` : nothing}
              ${this.renderSelfTest(disk, offline)}
            </div>
            ${this.renderDiskMenu(disk)} ${this.renderSubjectIssues(disk)}
          `;
        })}
      </div>
    `;
  }

  private renderFilesystems(filesystems: Subject[]): TemplateResult {
    return html`
      <div class="section">
        ${this.sectionTitle("filesystems", "mdi:folder")}
        ${filesystems.map((fs) => {
          const attrs = fs.entity.attributes;
          const usedEntity = fs.metric("used_pct");
          const used = numberState(usedEntity);
          const mounted = attrs.mounted !== false && !fs.missing;
          return html`
            <div class="row fs" @click=${() => this.moreInfo((usedEntity ?? fs.entity).entity_id)}>
              <ha-icon class="state sev-${fs.severity}" icon=${mounted ? "mdi:folder" : "mdi:folder-alert"}></ha-icon>
              <div class="info">
                <div class="primary">
                  ${attrs.mountpoint}
                  <span class="secondary inline">
                    ${mounted
                      ? this.t("free", { value: formatBytes(this.hass, attrs.free_bytes) })
                      : this.t("not_mounted")}
                  </span>
                  ${attrs.read_only ? html`<span class="badge warn">${this.t("read_only")}</span>` : nothing}
                </div>
                ${mounted && used !== undefined
                  ? html`<div class="bar">
                      <div class="fill sev-${fs.severity}" style="width:${Math.min(used, 100)}%"></div>
                      ${attrs.warning_pct ? html`<div class="mark" style="left:${attrs.warning_pct}%"></div>` : nothing}
                      ${attrs.critical_pct ? html`<div class="mark crit" style="left:${attrs.critical_pct}%"></div>` : nothing}
                    </div>`
                  : nothing}
              </div>
              <div class="value">${used !== undefined ? `${used.toFixed(0)} %` : ""}</div>
            </div>
          `;
        })}
      </div>
    `;
  }

  private renderVaults(vaults: Subject[]): TemplateResult {
    const days = this.config?.days ?? 14;
    return html`
      <div class="section">
        ${this.sectionTitle("vaults", "mdi:backup-restore")}
        ${vaults.map((vault) => {
          const attrs = vault.entity.attributes;
          const history = ((attrs.history as { date: string; status: string | null }[]) || []).slice(-days);
          return html`
            <div class="row vault" @click=${() => this.moreInfo(vault.entity.entity_id)}>
              <ha-icon class="state sev-${vault.severity}" icon="mdi:backup-restore"></ha-icon>
              <div class="info">
                <div class="primary">
                  ${attrs.vault}
                  ${attrs.running ? html`<span class="badge">${this.t("running")}</span>` : nothing}
                </div>
                <div class="secondary">
                  ${this.t("last_success", { time: relativeTime(this.hass, attrs.last_success) })}
                  ${attrs.last_stats?.files
                    ? html` · ${this.t("files", { count: formatNumber(this.hass, attrs.last_stats.files) })}`
                    : nothing}
                  ${attrs.last_stats?.size_bytes ? html` · ${formatBytes(this.hass, attrs.last_stats.size_bytes)}` : nothing}
                </div>
              </div>
              <div class="timeline">
                ${history.map(
                  (day) => html`<span
                    class="day ${day.status ?? "none"}"
                    title="${day.date} · ${this.t(`day_${day.status ?? "none"}` as StringKey)}"
                  ></span>`,
                )}
              </div>
            </div>
            ${this.renderSubjectIssues(vault)}
          `;
        })}
      </div>
    `;
  }

  private renderJobs(jobs: Subject[]): TemplateResult {
    return html`
      <div class="section">
        ${this.sectionTitle("jobs", "mdi:calendar-sync")}
        ${jobs.map((job) => {
          const attrs = job.entity.attributes;
          return html`
            <div class="row" @click=${() => this.moreInfo(job.entity.entity_id)}>
              <ha-icon class="state sev-${job.severity}" icon="mdi:calendar-sync"></ha-icon>
              <div class="info">
                <div class="primary">${attrs.job}</div>
                <div class="secondary">${this.t("last_success", { time: relativeTime(this.hass, attrs.last_success) })}</div>
              </div>
            </div>
            ${this.renderSubjectIssues(job)}
          `;
        })}
      </div>
    `;
  }

  private renderSubjectIssues(subject: Subject): TemplateResult | typeof nothing {
    // Already listed at the top when the issues section is shown
    if (!subject.issues.length || this.shown("issues")) return nothing;
    return html`<div class="sub-issues">
      ${subject.issues.map((issue) => html`<div class="issue ${issue.severity}">${issue.message}</div>`)}
    </div>`;
  }

  static styles = css`
    :host {
      --ok: var(--success-color, #43a047);
      --warn: var(--warning-color, #ffa600);
      --crit: var(--error-color, #db4437);
      --unk: var(--disabled-color, #bdbdbd);
    }
    ha-card {
      overflow: hidden;
      border-top: 4px solid transparent;
    }
    ha-card.sev-ok { border-top-color: var(--ok); }
    ha-card.sev-warning { border-top-color: var(--warn); }
    ha-card.sev-critical { border-top-color: var(--crit); }
    ha-card.sev-unknown { border-top-color: var(--unk); }
    .empty { padding: 16px; color: var(--secondary-text-color); }
    button {
      font: inherit;
      color: inherit;
      background: none;
      border: none;
      cursor: pointer;
    }
    .header {
      display: flex;
      align-items: flex-start;
      gap: 12px;
      padding: 16px 16px 8px;
    }
    .host-icon { color: var(--state-icon-color, var(--primary-color)); --mdc-icon-size: 32px; }
    .title { flex: 1; min-width: 0; }
    .meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin-top: 4px; }
    .name { font-size: 1.2em; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .secondary { color: var(--secondary-text-color); font-size: 0.85em; }
    .secondary.inline { margin-left: 6px; }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      border-radius: 16px;
      font-size: 0.85em;
      font-weight: 500;
      color: #fff;
      --mdc-icon-size: 18px;
    }
    .status.ok { background: var(--ok); }
    .status.warning { background: var(--warn); }
    .status.critical { background: var(--crit); }
    .status.offline, .status.unknown { background: var(--unk); color: var(--primary-text-color); }
    .actions { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 2px; }
    /* Collapsed: the header takes the colour of the worst problem */
    ha-card.collapsed.sev-ok .header { background: color-mix(in srgb, var(--ok) 12%, transparent); }
    ha-card.collapsed.sev-warning .header { background: color-mix(in srgb, var(--warn) 16%, transparent); }
    ha-card.collapsed.sev-critical .header { background: color-mix(in srgb, var(--crit) 16%, transparent); }
    ha-card.collapsed.sev-unknown .header { background: color-mix(in srgb, var(--unk) 18%, transparent); }
    ha-card.collapsed .header { padding-bottom: 12px; }
    ha-card.collapsed .issues { padding-top: 8px; }
    .icon-button.small { padding: 2px; margin-left: auto; --mdc-icon-size: 18px; }
    .icon-button.wake { color: var(--ok); }
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      border-radius: 16px;
      font-size: 0.85em;
      border: 1px solid var(--divider-color);
      --mdc-icon-size: 16px;
    }
    .btn:hover { background: var(--secondary-background-color); }
    .btn[disabled] { opacity: 0.4; cursor: default; }
    .btn.danger { border-color: var(--crit); color: var(--crit); }
    .confirm { display: inline-flex; align-items: center; gap: 6px; font-size: 0.85em; }
    .issue span { flex: 1; }
    .rule-editor, .disk-menu, .rules {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      padding: 6px 8px;
      font-size: 0.85em;
    }
    .disk-menu { padding-left: 36px; }
    .rules { flex-direction: column; align-items: stretch; }
    .rule-row { display: flex; align-items: center; gap: 8px; }
    .rule-row .secondary { flex: 1; }
    .threshold { display: inline-flex; align-items: center; gap: 6px; }
    .threshold input {
      width: 4.5em;
      font: inherit;
      color: inherit;
      background: var(--card-background-color);
      border: 1px solid var(--divider-color);
      border-radius: 6px;
      padding: 2px 6px;
    }
    .icon-button { padding: 4px; border-radius: 50%; color: var(--secondary-text-color); }
    .icon-button:hover { background: var(--secondary-background-color); }
    .icon-button[disabled] { opacity: 0.4; cursor: default; }
    .issues, .sub-issues { padding: 0 16px 8px; display: grid; gap: 4px; }
    .sub-issues { padding-left: 56px; }
    .issue {
      display: flex;
      gap: 8px;
      align-items: flex-start;
      font-size: 0.9em;
      padding: 6px 8px;
      border-radius: 8px;
      border-left: 3px solid var(--warn);
      background: color-mix(in srgb, var(--warn) 10%, transparent);
      --mdc-icon-size: 18px;
    }
    .issue ha-icon { color: var(--warn); }
    .issue.critical { border-left-color: var(--crit); background: color-mix(in srgb, var(--crit) 10%, transparent); }
    .issue.critical ha-icon { color: var(--crit); }
    .section { padding: 4px 16px 8px; }
    .section-title {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.8em;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--secondary-text-color);
      padding: 8px 0 4px;
      border-top: 1px solid var(--divider-color);
      --mdc-icon-size: 16px;
    }
    .row {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 6px 0;
      cursor: pointer;
      border-radius: 8px;
    }
    .row:hover { background: color-mix(in srgb, var(--primary-text-color) 4%, transparent); }
    .info { flex: 1; min-width: 0; }
    .primary { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .value { font-variant-numeric: tabular-nums; color: var(--primary-text-color); white-space: nowrap; }
    .state { --mdc-icon-size: 24px; }
    .state.sev-ok { color: var(--ok); }
    .state.sev-warning { color: var(--warn); }
    .state.sev-critical { color: var(--crit); }
    .state.sev-unknown { color: var(--unk); }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 2px 0 4px; }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      border-radius: 16px;
      font-size: 0.85em;
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.1));
      --mdc-icon-size: 16px;
    }
    .chip .label { color: var(--secondary-text-color); }
    .chip.sev-warning { background: color-mix(in srgb, var(--warn) 18%, transparent); }
    .chip.sev-critical { background: color-mix(in srgb, var(--crit) 18%, transparent); }
    .chip.update { background: color-mix(in srgb, var(--primary-color) 15%, transparent); color: var(--primary-color); }
    .bar {
      position: relative;
      height: 8px;
      margin-top: 4px;
      border-radius: 4px;
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.15));
      overflow: hidden;
    }
    .fill { height: 100%; border-radius: 4px; background: var(--ok); transition: width 0.4s; }
    .fill.sev-warning { background: var(--warn); }
    .fill.sev-critical { background: var(--crit); }
    .mark { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--warn); opacity: 0.7; }
    .mark.crit { background: var(--crit); }
    .timeline { display: flex; gap: 2px; }
    .day { width: 8px; height: 18px; border-radius: 2px; background: var(--secondary-background-color, rgba(127, 127, 127, 0.15)); }
    .day.success { background: var(--ok); }
    .day.warning { background: var(--warn); }
    .day.failed { background: var(--crit); }
    .day.running { background: var(--primary-color); opacity: 0.6; }
    .badge.warn {
      background: color-mix(in srgb, var(--crit) 15%, transparent);
      color: var(--crit);
    }
    .mini { display: flex; align-items: center; gap: 6px; margin-top: 3px; font-size: 0.8em; }
    .mini-label { width: 40px; color: var(--secondary-text-color); }
    .mini .bar { flex: 1; margin-top: 0; height: 6px; }
    .mini-value { width: 38px; text-align: right; font-variant-numeric: tabular-nums; }
    .badge {
      font-size: 0.75em;
      padding: 1px 6px;
      margin-left: 6px;
      border-radius: 8px;
      background: color-mix(in srgb, var(--primary-color) 15%, transparent);
      color: var(--primary-color);
    }
    .spin { animation: spin 2s linear infinite; color: var(--primary-color); }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (max-width: 450px) {
      .timeline .day { width: 6px; }
    }
  `;
}

if (!customElements.get("host-agent-card")) {
  customElements.define("host-agent-card", HostAgentCard);
  (window as any).customCards = (window as any).customCards || [];
  (window as any).customCards.push({
    type: "host-agent-card",
    name: "Host Agent",
    description: "Health of a host monitored by ha-host-agent: disks, filesystems, backups, jobs.",
    preview: true,
    documentationURL: "https://github.com/Elwinmage/ha-host-agent",
  });
  console.info(`%c HOST-AGENT-CARD %c ${VERSION} `, "color:white;background:#3f51b5", "color:#3f51b5");
}
