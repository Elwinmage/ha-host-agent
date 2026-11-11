import { LitElement, html, type TemplateResult } from "lit";
import { property, state } from "lit/decorators.js";

import { localize, type StringKey } from "./localize";
import type { HomeAssistant, HostAgentCardConfig, Section } from "./types";

const SECTIONS: Section[] = ["issues", "system", "disks", "filesystems", "vaults", "jobs"];

export class HostAgentCardEditor extends LitElement {
  @property({ attribute: false }) public hass?: HomeAssistant;
  @state() private config?: HostAgentCardConfig;

  public setConfig(config: HostAgentCardConfig): void {
    this.config = config;
  }

  private schema() {
    const options = SECTIONS.map((section) => ({ value: section, label: localize(this.hass, section as StringKey) }));
    return [
      { name: "device_id", selector: { device: { filter: [{ integration: "host_agent" }] } } },
      { name: "title", selector: { text: {} } },
      { name: "days", selector: { number: { min: 3, max: 60, mode: "box" } } },
      { name: "show", selector: { select: { multiple: true, mode: "list", options } } },
      { name: "node_id", selector: { text: {} } },
    ];
  }

  private computeLabel = (field: { name: string }): string => localize(this.hass, field.name as StringKey);

  private changed(event: CustomEvent): void {
    const config = { ...event.detail.value } as HostAgentCardConfig;
    for (const key of Object.keys(config) as (keyof HostAgentCardConfig)[]) {
      const value = config[key];
      if (value === "" || value === undefined || (Array.isArray(value) && !value.length)) delete config[key];
    }
    this.config = config;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }

  protected render(): TemplateResult {
    if (!this.hass || !this.config) return html``;
    return html`<ha-form
      .hass=${this.hass}
      .data=${this.config}
      .schema=${this.schema()}
      .computeLabel=${this.computeLabel}
      @value-changed=${this.changed}
    ></ha-form>`;
  }
}

if (!customElements.get("host-agent-card-editor")) {
  customElements.define("host-agent-card-editor", HostAgentCardEditor);
}
