"""Options flow: guided forms for each collector, pushed to the agent on save."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from . import remote
from .agent_config import CollectorsError, validate_collectors
from .const import CONF_AUTO_UPDATE, CONF_COLLECTORS, CONF_LANGUAGE, LANGUAGES

LIST = selector.TextSelector(selector.TextSelectorConfig(multiple=True))
TEXT = selector.TextSelector()
JOB_SOURCES = ["path", "report"]


def _number(
    minimum: float, maximum: float, step: float = 1, unit: str | None = None
) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _int(value: Any) -> int:
    return int(float(value))


def _get(data: dict[str, Any], path: str, default: Any) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _clean_list(values: Any) -> list[str]:
    return [str(v).strip() for v in values or [] if str(v).strip()]


# Field: (form key, path in the collector section, default, selector, converter)
Field = tuple[str, str, Any, Any, Callable[[Any], Any]]

SECTIONS: dict[str, list[Field]] = {
    "smart": [
        ("enabled", "enabled", True, selector.BooleanSelector(), bool),
        ("interval_minutes", "interval", 3600, _number(1, 1440, unit="min"), lambda v: _int(v) * 60),
        ("temperature_warning", "temperature.warning", 55, _number(30, 90, unit="°C"), _int),
        ("temperature_critical", "temperature.critical", 65, _number(30, 100, unit="°C"), _int),
        ("selftest_enabled", "selftest.enabled", True, selector.BooleanSelector(), bool),
        ("selftest_short_hours", "selftest.short_interval_hours", 24, _number(0, 720, unit="h"), _int),
        ("selftest_long_days", "selftest.long_interval_days", 7, _number(0, 90, unit="d"), _int),
        ("selftest_window", "selftest.window", "02:00-06:00", TEXT, str),
        ("selftest_max_age_hours", "selftest_max_age_hours", 168, _number(0, 8760, unit="h"), _int),
        ("exclude", "exclude", [], LIST, _clean_list),
    ],
    "filesystem": [
        ("enabled", "enabled", True, selector.BooleanSelector(), bool),
        ("required", "required", [], LIST, _clean_list),
        ("include_mounts", "include_mounts", [], LIST, _clean_list),
        ("warning", "thresholds.warning", 85, _number(1, 100, unit="%"), _int),
        ("critical", "thresholds.critical", 95, _number(1, 100, unit="%"), _int),
    ],
    "dirvish": [
        ("enabled", "enabled", True, selector.BooleanSelector(), bool),
        ("banks", "banks", [], LIST, _clean_list),
        ("exclude", "exclude", [], LIST, _clean_list),
        ("max_age_hours", "max_age_hours", 26, _number(1, 2160, unit="h"), _int),
        ("history_days", "history_days", 14, _number(1, 60, unit="d"), _int),
        ("vanished_is_ok", "vanished_is_ok", True, selector.BooleanSelector(), bool),
    ],
    "system": [
        ("enabled", "enabled", True, selector.BooleanSelector(), bool),
        ("cpu_warning", "cpu_temperature.warning", 75, _number(30, 110, unit="°C"), _int),
        ("cpu_critical", "cpu_temperature.critical", 85, _number(30, 120, unit="°C"), _int),
        ("memory_warning", "memory.warning", 90, _number(1, 100, unit="%"), _int),
        ("memory_critical", "memory.critical", 97, _number(1, 100, unit="%"), _int),
    ],
}

# Fields checked to be warning <= critical (form keys)
ORDERED_PAIRS = [
    ("temperature_warning", "temperature_critical"),
    ("warning", "critical"),
    ("cpu_warning", "cpu_critical"),
    ("memory_warning", "memory_critical"),
]


def section_schema(name: str, section: dict[str, Any]) -> vol.Schema:
    schema: dict[Any, Any] = {}
    for key, path, default, sel, _conv in SECTIONS[name]:
        value = _get(section, path, default)
        if path == "interval" and name == "smart":
            value = int(value) // 60
        schema[vol.Optional(key, default=value)] = sel
    return vol.Schema(schema)


def apply_section(name: str, section: dict[str, Any], user_input: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(section)
    for key, path, default, _sel, conv in SECTIONS[name]:
        _set(result, path, conv(user_input.get(key, default)))
    return result


class HostAgentOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._job_index: int | None = None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @property
    def _collectors(self) -> dict[str, Any]:
        return copy.deepcopy(self.config_entry.options.get(CONF_COLLECTORS) or {})

    @property
    def _jobs(self) -> list[dict[str, Any]]:
        return list(_get(self._collectors, "jobs.items", []))

    async def _save(self, options: dict[str, Any]) -> tuple[ConfigFlowResult | None, dict[str, str], str]:
        """Validate on the host, then store. Returns (result, errors, detail)."""
        current = dict(self.config_entry.options)
        agent_changed = options.get(CONF_LANGUAGE) != current.get(CONF_LANGUAGE) or (
            options.get(CONF_COLLECTORS) or {}
        ) != (current.get(CONF_COLLECTORS) or {})
        if agent_changed:
            runtime = getattr(self.config_entry, "runtime_data", None)
            if runtime is None:
                return None, {"base": "not_loaded"}, ""
            try:
                await runtime.async_configure(options)
            except (HomeAssistantError, remote.RemoteError) as err:
                return None, {"base": "configure_failed"}, str(err)
        return self.async_create_entry(data=options), {}, ""

    async def _save_collectors(
        self, collectors: dict[str, Any]
    ) -> tuple[ConfigFlowResult | None, dict[str, str], str]:
        return await self._save({**self.config_entry.options, CONF_COLLECTORS: collectors})

    # ------------------------------------------------------------------ #
    # Menu
    # ------------------------------------------------------------------ #

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "smart", "filesystem", "dirvish", "jobs", "system", "advanced"],
        )

    async def async_step_general(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        options = dict(self.config_entry.options)
        if user_input is not None:
            result, errors, detail = await self._save({**options, **user_input})
            if result:
                return result
            options.update(user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_AUTO_UPDATE, default=options.get(CONF_AUTO_UPDATE, False)): bool,
                vol.Required(CONF_LANGUAGE, default=options.get(CONF_LANGUAGE, "en")): vol.In(LANGUAGES),
            }
        )
        return self.async_show_form(
            step_id="general", data_schema=schema, errors=errors, description_placeholders={"detail": detail}
        )

    async def _section_step(self, name: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        collectors = self._collectors
        section = collectors.get(name) or {}
        if user_input is not None:
            for low, high in ORDERED_PAIRS:
                if (
                    low in user_input
                    and high in user_input
                    and float(user_input[low]) > float(user_input[high])
                ):
                    errors[low] = "warning_above_critical"
            window = user_input.get("selftest_window")
            if window and not re.fullmatch(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", str(window).strip()):
                errors["selftest_window"] = "invalid_window"
            if not errors:
                collectors[name] = apply_section(name, section, user_input)
                result, errors, detail = await self._save_collectors(collectors)
                if result:
                    return result
            section = apply_section(name, section, user_input)
        return self.async_show_form(
            step_id=name,
            data_schema=section_schema(name, section),
            errors=errors,
            description_placeholders={"detail": detail},
        )

    async def async_step_smart(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._section_step("smart", user_input)

    async def async_step_filesystem(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._section_step("filesystem", user_input)

    async def async_step_dirvish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._section_step("dirvish", user_input)

    async def async_step_system(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._section_step("system", user_input)

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #

    async def async_step_jobs(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["job_add"]
        if self._jobs:
            options += ["job_select", "job_delete"]
        return self.async_show_menu(step_id="jobs", menu_options=options)

    def _job_names(self) -> list[selector.SelectOptionDict]:
        return [
            selector.SelectOptionDict(value=str(i), label=job["name"]) for i, job in enumerate(self._jobs)
        ]

    async def async_step_job_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._job_index = int(user_input["job"])
            return await self.async_step_job_edit()
        return self.async_show_form(
            step_id="job_select",
            data_schema=vol.Schema(
                {
                    vol.Required("job"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._job_names())
                    )
                }
            ),
        )

    async def async_step_job_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._job_index = None
        return await self.async_step_job_edit(user_input)

    async def async_step_job_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        jobs = self._jobs
        current = jobs[self._job_index] if self._job_index is not None else {}
        values = {
            "name": current.get("name", ""),
            "source": "report" if current.get("report") else "path",
            "path": current.get("path", ""),
            "success_pattern": current.get("success_pattern", ""),
            "max_age_hours": current.get("max_age_hours", 26),
        }
        if user_input is not None:
            values.update(user_input)
            name = str(values["name"]).strip()
            others = [j["name"] for i, j in enumerate(jobs) if i != self._job_index]
            if not name:
                errors["name"] = "required"
            elif _slug(name) in {_slug(o) for o in others}:
                errors["name"] = "duplicate_job"
            if values["source"] == "path" and not str(values.get("path") or "").strip():
                errors["path"] = "required"
            pattern = str(values.get("success_pattern") or "").strip()
            if pattern:
                try:
                    re.compile(pattern)
                except re.error:
                    errors["success_pattern"] = "invalid_regex"
            if not errors:
                job: dict[str, Any] = {"name": name, "max_age_hours": _int(values["max_age_hours"])}
                if values["source"] == "report":
                    job["report"] = True
                else:
                    job["path"] = str(values["path"]).strip()
                    if pattern:
                        job["success_pattern"] = pattern
                if self._job_index is None:
                    jobs.append(job)
                else:
                    jobs[self._job_index] = job
                collectors = self._collectors
                _set(collectors, "jobs.items", jobs)
                result, errors, detail = await self._save_collectors(collectors)
                if result:
                    return result

        schema = vol.Schema(
            {
                vol.Required("name", default=values["name"]): str,
                vol.Required("source", default=values["source"]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=JOB_SOURCES, translation_key="job_source")
                ),
                vol.Optional("path", default=values["path"] or ""): str,
                vol.Optional("success_pattern", default=values["success_pattern"] or ""): str,
                vol.Required("max_age_hours", default=values["max_age_hours"]): _number(1, 2160, unit="h"),
            }
        )
        return self.async_show_form(
            step_id="job_edit", data_schema=schema, errors=errors, description_placeholders={"detail": detail}
        )

    async def async_step_job_delete(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            remove = {int(i) for i in user_input.get("jobs", [])}
            collectors = self._collectors
            _set(collectors, "jobs.items", [j for i, j in enumerate(self._jobs) if i not in remove])
            result, errors, detail = await self._save_collectors(collectors)
            if result:
                return result
        return self.async_show_form(
            step_id="job_delete",
            data_schema=vol.Schema(
                {
                    vol.Required("jobs"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._job_names(), multiple=True)
                    )
                }
            ),
            errors=errors,
            description_placeholders={"detail": detail},
        )

    # ------------------------------------------------------------------ #
    # Advanced: raw collectors section
    # ------------------------------------------------------------------ #

    async def async_step_advanced(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        collectors: Any = self._collectors
        if user_input is not None:
            collectors = user_input.get(CONF_COLLECTORS)
            try:
                validated = validate_collectors(collectors)
            except CollectorsError as err:
                errors[CONF_COLLECTORS] = "invalid_collectors"
                detail = str(err)
            else:
                result, errors, detail = await self._save_collectors(validated)
                if result:
                    return result
        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {vol.Optional(CONF_COLLECTORS, default=collectors or {}): selector.ObjectSelector()}
            ),
            errors=errors,
            description_placeholders={"detail": detail},
        )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
