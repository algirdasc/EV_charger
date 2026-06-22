from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TuyaEVChargerRuntimeData
from .const import (
    CARD_ROLE_CHARGE_SESSION,
    CARD_ROLE_INDEX,
    CARD_ROLE_SCHEDULE_ENABLED,
)
from .entity import TuyaEVChargerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime_data: TuyaEVChargerRuntimeData = entry.runtime_data
    async_add_entities(
        [
            TuyaEVChargerChargeSessionSwitch(entry, runtime_data),
            TuyaEVChargerNfcSwitch(entry, runtime_data),
            TuyaEVChargerScheduleSwitch(entry, runtime_data),
        ]
    )


class TuyaEVChargerChargeSessionSwitch(TuyaEVChargerEntity, SwitchEntity):
    _attr_translation_key = "charge_session"
    _attr_icon = "mdi:ev-station"

    def __init__(self, entry: ConfigEntry, runtime_data: TuyaEVChargerRuntimeData) -> None:
        super().__init__(
            entry=entry,
            runtime_data=runtime_data,
            card_role=CARD_ROLE_CHARGE_SESSION,
            card_index=CARD_ROLE_INDEX[CARD_ROLE_CHARGE_SESSION],
        )
        self._attr_unique_id = f"{runtime_data.client.device_id}_charge_session"
        # Last enable/disable command we successfully sent. The charger's
        # do_charge DP reverts to False whenever no current is flowing (no car
        # drawing, PAUSE, or WORKING after a completed charge), so reporting it
        # directly makes evcc see "charger out of sync: expected enabled, got
        # disabled". evcc expects Enabled() to mirror its last Enable() command,
        # so we hold the commanded value until the next command. Seeded from the
        # device (None) before any command and after a restart.
        self._commanded_on: bool | None = None

    @property
    def is_on(self) -> bool:
        if self._commanded_on is not None:
            return self._commanded_on
        data = self.coordinator.data
        if data is None:
            return False
        if data.do_charge is not None:
            return data.do_charge
        return data.work_state_debug == "WORKING"

    async def async_turn_on(self, **kwargs: object) -> None:
        if not await self._runtime_data.client.async_set_charge_enabled(True):
            raise HomeAssistantError("Unable to start charging session.")
        self._commanded_on = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: object) -> None:
        if not await self._runtime_data.client.async_set_charge_enabled(False):
            raise HomeAssistantError("Unable to stop charging session.")
        self._commanded_on = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class TuyaEVChargerNfcSwitch(TuyaEVChargerEntity, SwitchEntity):
    _attr_translation_key = "nfc_enabled"
    _attr_icon = "mdi:nfc"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, runtime_data: TuyaEVChargerRuntimeData) -> None:
        super().__init__(entry=entry, runtime_data=runtime_data)
        self._attr_unique_id = f"{runtime_data.client.device_id}_nfc_enabled"

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        if data is None or data.nfc_enabled is None:
            return False
        return data.nfc_enabled

    async def async_turn_on(self, **kwargs: object) -> None:
        if not await self._runtime_data.client.async_set_nfc_enabled(True):
            raise HomeAssistantError("Unable to enable NFC.")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: object) -> None:
        if not await self._runtime_data.client.async_set_nfc_enabled(False):
            raise HomeAssistantError("Unable to disable NFC.")
        await self.coordinator.async_request_refresh()


class TuyaEVChargerScheduleSwitch(TuyaEVChargerEntity, SwitchEntity):
    _attr_translation_key = "schedule_enabled"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, entry: ConfigEntry, runtime_data: TuyaEVChargerRuntimeData) -> None:
        super().__init__(
            entry=entry,
            runtime_data=runtime_data,
            card_role=CARD_ROLE_SCHEDULE_ENABLED,
            card_index=CARD_ROLE_INDEX[CARD_ROLE_SCHEDULE_ENABLED],
        )
        self._attr_unique_id = f"{runtime_data.client.device_id}_schedule_enabled"

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return bool(data and data.schedule_enabled)

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        data = self.coordinator.data
        start = (data.schedule_start if data else None) or "00:00"
        end = (data.schedule_end if data else None) or "00:00"
        if not await self._runtime_data.client.async_set_schedule(enabled, start, end):
            raise HomeAssistantError("Unable to update charging schedule.")
        await self.coordinator.async_request_refresh()
