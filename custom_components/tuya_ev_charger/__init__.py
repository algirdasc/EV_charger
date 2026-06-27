from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError

from .const import (
    CHARGER_PROFILE_DEPOW_V2,
    CHARGER_PROFILES,
    CONF_CHARGER_PROFILE,
    CONF_CHARGER_PROFILE_JSON,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    CONF_SCAN_INTERVAL,
    DEFAULT_CHARGER_PROFILE,
    DEFAULT_CHARGER_PROFILE_JSON,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    DP_CHARGER_INFO,
    DP_CURRENT_TARGET,
    DP_DO_CHARGE,
    DP_METRICS,
    DP_WORK_STATE_DEBUG,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    PLATFORMS,
    SERVICE_PROFILE_ASSISTANT,
)
from .coordinator import TuyaEVChargerDataUpdateCoordinator
from .tuya_ev_charger import TuyaEVChargerClient

LOGGER = logging.getLogger(__name__)

SERVICE_DATA_ENTRY_ID = "entry_id"
SERVICE_DATA_APPLY = "apply"

SERVICE_PROFILE_ASSISTANT_SCHEMA = vol.Schema(
    {
        vol.Optional(SERVICE_DATA_ENTRY_ID): str,
        vol.Optional(SERVICE_DATA_APPLY, default=False): bool,
    }
)


@dataclass(slots=True)
class TuyaEVChargerRuntimeData:
    client: TuyaEVChargerClient
    coordinator: TuyaEVChargerDataUpdateCoordinator


def _scan_interval_seconds(entry: ConfigEntry) -> int:
    try:
        configured_value = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        configured_value = DEFAULT_SCAN_INTERVAL_SECONDS
    return max(MIN_SCAN_INTERVAL_SECONDS, min(MAX_SCAN_INTERVAL_SECONDS, configured_value))


def _charger_profile(entry: ConfigEntry) -> str:
    configured_value = entry.options.get(
        CONF_CHARGER_PROFILE,
        entry.data.get(CONF_CHARGER_PROFILE, DEFAULT_CHARGER_PROFILE),
    )
    normalized = str(configured_value).strip().lower()
    if normalized in CHARGER_PROFILES:
        return normalized
    return DEFAULT_CHARGER_PROFILE


def _charger_profile_json(entry: ConfigEntry) -> str:
    configured_value = entry.options.get(
        CONF_CHARGER_PROFILE_JSON,
        entry.data.get(CONF_CHARGER_PROFILE_JSON, DEFAULT_CHARGER_PROFILE_JSON),
    )
    return str(configured_value or "").strip()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = TuyaEVChargerClient(
        device_id=entry.data[CONF_DEVICE_ID],
        host=entry.data[CONF_HOST],
        local_key=entry.data[CONF_LOCAL_KEY],
        protocol_version=entry.data[CONF_PROTOCOL_VERSION],
        charger_profile=_charger_profile(entry),
        charger_profile_json=_charger_profile_json(entry),
    )

    try:
        await client.async_connect()
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Unable to initialize charger client for {entry.title}: {err}"
        ) from err

    coordinator = TuyaEVChargerDataUpdateCoordinator(
        hass=hass,
        client=client,
        update_interval=timedelta(seconds=_scan_interval_seconds(entry)),
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Unable to fetch initial charger state for {entry.title}: {err}"
        ) from err

    runtime_data = TuyaEVChargerRuntimeData(client=client, coordinator=coordinator)
    entry.runtime_data = runtime_data
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    LOGGER.debug("Tuya EV charger integration initialized: %s", entry.title)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_register_services(hass: HomeAssistant) -> None:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("services_registered"):
        return

    async def _handle_profile_assistant(call: ServiceCall) -> None:
        entry = _resolve_entry_from_call(hass, call)
        runtime_data = _resolve_runtime_data(entry)
        apply_suggestion = bool(call.data.get(SERVICE_DATA_APPLY, False))

        report = await async_profile_assistant_report(runtime_data.client)
        suggested = str(report.get("suggested_profile", "")).lower()
        applied_profile: str | None = None
        if apply_suggestion and suggested in CHARGER_PROFILES:
            new_options = dict(entry.options)
            new_options[CONF_CHARGER_PROFILE] = suggested
            hass.config_entries.async_update_entry(entry, options=new_options)
            applied_profile = suggested

        payload = {
            "entry_id": entry.entry_id,
            "entry_title": entry.title,
            "suggested_profile": suggested or None,
            "applied_profile": applied_profile,
            "report": report,
        }
        hass.bus.async_fire(f"{DOMAIN}_profile_assistant", payload)
        persistent_notification.async_create(
            hass=hass,
            title=f"Tuya EV Charger Profile Assistant ({entry.title})",
            message=(
                "Profile assistant report:\n\n```json\n"
                f"{json.dumps(payload, indent=2, ensure_ascii=True)}\n```"
            ),
            notification_id=f"{DOMAIN}_{entry.entry_id}_profile_assistant",
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_PROFILE_ASSISTANT,
        _handle_profile_assistant,
        schema=SERVICE_PROFILE_ASSISTANT_SCHEMA,
    )
    domain_data["services_registered"] = True


def _resolve_entry_from_call(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry:
    entry_id = str(call.data.get(SERVICE_DATA_ENTRY_ID, "")).strip()
    entries = hass.config_entries.async_entries(DOMAIN)
    loaded_entries = [
        entry for entry in entries if getattr(entry, "runtime_data", None) is not None
    ]
    if entry_id:
        for entry in loaded_entries:
            if entry.entry_id == entry_id:
                return entry
        raise ServiceValidationError(
            f"Entry '{entry_id}' is not loaded for domain '{DOMAIN}'."
        )
    if len(loaded_entries) == 1:
        return loaded_entries[0]
    if not loaded_entries:
        raise ServiceValidationError(f"No loaded '{DOMAIN}' entries found.")
    raise ServiceValidationError(
        f"Multiple '{DOMAIN}' entries loaded, provide '{SERVICE_DATA_ENTRY_ID}'."
    )


def _resolve_runtime_data(entry: ConfigEntry) -> TuyaEVChargerRuntimeData:
    runtime_data: TuyaEVChargerRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        raise ServiceValidationError(
            f"Charger runtime data is unavailable for entry '{entry.entry_id}'."
        )
    return runtime_data


async def async_profile_assistant_report(client: TuyaEVChargerClient) -> dict[str, Any]:
    """Inspect the raw DPS payload and suggest the closest charger profile."""
    dps = await client.async_get_raw_dps()
    if dps is None:
        return {"error": "Unable to read DPS payload from charger."}

    candidates: dict[str, list[str]] = {
        "metrics": [],
        "charger_info": [],
        "do_charge": [],
        "current_target": [],
        "work_state_debug": [],
    }
    for dp_id, value in dps.items():
        if _looks_like_metrics(value):
            candidates["metrics"].append(dp_id)
        if _looks_like_charger_info(value):
            candidates["charger_info"].append(dp_id)
        if _coerce_optional_bool(value) is not None:
            candidates["do_charge"].append(dp_id)
        if _looks_like_current_target(value):
            candidates["current_target"].append(dp_id)
        if _looks_like_state_debug(value):
            candidates["work_state_debug"].append(dp_id)

    known_depows = {
        DP_METRICS,
        DP_CHARGER_INFO,
        DP_DO_CHARGE,
        DP_CURRENT_TARGET,
        DP_WORK_STATE_DEBUG,
    }
    suggestion = (
        CHARGER_PROFILE_DEPOW_V2 if known_depows.issubset(set(dps.keys())) else "generic_v1"
    )

    return {
        "suggested_profile": suggestion,
        "detected_dp_ids": sorted(dps.keys()),
        "candidates": candidates,
        "sample_values": {key: dps[key] for key in sorted(dps.keys())[:15]},
    }


def _coerce_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "on"}:
            return True
        if lowered in {"false", "0", "off"}:
            return False
    return None


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_like_metrics(value: Any) -> bool:
    if isinstance(value, dict):
        if "L1" in value:
            return True
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return False
        if isinstance(payload, dict) and "L1" in payload:
            return True
    return False


def _looks_like_charger_info(value: Any) -> bool:
    if isinstance(value, dict):
        keys = {str(key).lower() for key in value.keys()}
        if {"model", "manufacturer"}.intersection(keys):
            return True
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return False
        if isinstance(payload, dict):
            keys = {str(key).lower() for key in payload.keys()}
            return bool({"model", "manufacturer"}.intersection(keys))
    return False


def _looks_like_current_target(value: Any) -> bool:
    parsed = _coerce_optional_int(value)
    if parsed is None:
        return False
    return 6 <= parsed <= 32


def _looks_like_state_debug(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().upper()
    return normalized in {"STANDBY", "WORKING", "DONE", "FAULT"}
