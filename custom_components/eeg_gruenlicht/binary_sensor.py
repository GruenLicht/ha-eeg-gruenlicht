"""Verbindungsstatus zur EEG."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import GruenLichtEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ConnectionSensor(entry.runtime_data)])


class ConnectionSensor(GruenLichtEntity, BinarySensorEntity):
    """An = letzte Abfrage bei der EEG erfolgreich."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        # Bleibt sichtbar und zeigt „getrennt", statt „nicht verfügbar".
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success
