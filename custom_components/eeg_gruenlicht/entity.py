"""Gemeinsame Basis der Entitäten."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MEMBER_ID, DOMAIN, PORTAL_URL, VERSION
from .coordinator import GruenLichtCoordinator


class GruenLichtEntity(CoordinatorEntity[GruenLichtCoordinator]):
    """Alle Entitäten hängen an einem Dienst-Gerät „EEG GrünLicht"."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GruenLichtCoordinator, key: str) -> None:
        super().__init__(coordinator)
        member_id = coordinator.config_entry.data[CONF_MEMBER_ID]
        self._attr_translation_key = key
        self._attr_unique_id = f"{member_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, member_id)},
            name="EEG GrünLicht",
            manufacturer="EEG Grünes Licht",
            model="Mitgliedskonto",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=PORTAL_URL,
        )

    @property
    def member(self) -> dict:
        return (self.coordinator.data or {}).get("member") or {}
