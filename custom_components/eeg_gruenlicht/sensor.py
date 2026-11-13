"""Sensoren der Stufe 1: EEG, Mitgliedsnummer, Rolle, Zählpunkte, letzte Synchronisierung."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import GruenLichtEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            MemberFieldSensor(coordinator, "community", "community"),
            MemberFieldSensor(coordinator, "membership_no", "membershipNo"),
            MemberFieldSensor(coordinator, "role", "role"),
            MeterPointsSensor(coordinator),
            LastSyncSensor(coordinator),
            ImportedUntilSensor(coordinator),
            ProfileReportedSensor(coordinator),
        ]
    )


class MemberFieldSensor(GruenLichtEntity, SensorEntity):
    """Ein Textfeld aus /api/me."""

    def __init__(self, coordinator, key: str, field: str) -> None:
        super().__init__(coordinator, key)
        self._field = field

    @property
    def native_value(self) -> str | None:
        value = self.member.get(self._field)
        return None if value in (None, "") else str(value)


class MeterPointsSensor(GruenLichtEntity, SensorEntity):
    """Anzahl der Zählpunkte, die Liste steht in den Attributen."""

    _attr_icon = "mdi:meter-electric-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "meter_points")

    def _points(self) -> list[dict[str, Any]]:
        points = self.member.get("meterPoints") or []
        # Ältere API-Antworten liefern nur Nummern statt Objekten.
        return [p if isinstance(p, dict) else {"number": str(p)} for p in points]

    @property
    def native_value(self) -> int:
        return len(self._points())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"meter_points": self._points()}


class LastSyncSensor(GruenLichtEntity, SensorEntity):
    """Zeitpunkt der letzten erfolgreichen Abfrage bei der EEG."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "last_sync")

    @property
    def native_value(self) -> datetime | None:
        return (self.coordinator.data or {}).get("synced_at")


class ImportedUntilSensor(GruenLichtEntity, SensorEntity):
    """Letzter Tag mit echten Viertelstundenwerten in der Statistik (Stufe 2)."""

    _attr_device_class = SensorDeviceClass.DATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "imported_until")

    @property
    def native_value(self) -> date | None:
        importer = self.coordinator.importer
        return importer.last_data_day if importer else None

    @property
    def extra_state_attributes(self) -> dict:
        importer = self.coordinator.importer
        if not importer:
            return {}
        return {
            "statistic_ids": list(importer.ids.values()),
            "last_loaded_day": importer.last_day.isoformat() if importer.last_day else None,
            "import_running": importer.running,
        }


class ProfileReportedSensor(GruenLichtEntity, SensorEntity):
    """Wann das Haushaltsprofil zuletzt an die EEG gemeldet wurde (Stufe 3)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "profile_reported")

    @property
    def native_value(self) -> datetime | None:
        reporter = self.coordinator.reporter
        if not reporter or not reporter.reported_at:
            return None
        return datetime.fromisoformat(reporter.reported_at)
