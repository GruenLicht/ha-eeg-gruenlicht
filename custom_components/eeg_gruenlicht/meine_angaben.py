"""Freigabe „Geräte in Meine Angaben" (ab 0.4.0).

Unabhängig vom Haushaltsprofil: Das Plugin meldet, welche Energiegeräte im
Haushalt stehen, als feste Felder an die GrünLicht-API. Die API trägt sie
über die Abrechnung in „Meine Angaben" ein (Herkunft „aus Home Assistant").
Was das Mitglied dort selbst eingetragen hat, bleibt; abweichende Werte
erscheinen als Vorschlag zum Übernehmen.

Gemeldet wird nur, was Home Assistant weiß oder im Dialog steht — nie ein
„nein". Fehlt ein Gerät später, meldet die API den Wert als zurückgezogen.
Schaltet das Mitglied die Freigabe ab, bleiben die Werte in „Meine Angaben"
stehen und werden nicht mehr geändert; offene Vorschläge verschwinden.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import GruenLichtClient, GruenLichtError
from .const import DOMAIN
from .profile import (
    BATTERY,
    EV,
    HEAT_PUMP,
    INVERTER,
    OPT_BATTERY_KWH,
    OPT_DEVICES,
    OPT_HAS_EV,
    OPT_HEAT_PUMP_KW,
    OPT_MEINE_ANGABEN,
    OPT_PV_KWP,
    OPT_WALLBOX_KW,
    WALLBOX,
    detect_devices,
)

_LOGGER = logging.getLogger(__name__)

# Wertebereiche von „Meine Angaben"; Werte außerhalb weist eeg-stack ab.
RANGES: dict[str, tuple[float, float]] = {
    "pv_kwp": (0.1, 100),
    "battery_kwh": (0.5, 200),
    "heat_pump_kw": (0.3, 20),
    "wallbox_kw": (1, 22),
}


def _number(key: str, value: Any) -> float | None:
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    low, high = RANGES[key]
    return number if low <= number <= high else None


def build_fields(hass: HomeAssistant, options: dict[str, Any]) -> dict[str, Any]:
    """Felder für „Meine Angaben" aus gewählten Geräten und Dialog-Angaben."""
    selected = set(options.get(OPT_DEVICES) or [])
    devices = [d for d in detect_devices(hass) if d.key in selected]
    types = {d.type for d in devices}

    pv_kwp = _number("pv_kwp", options.get(OPT_PV_KWP))
    battery_kwh = _number("battery_kwh", options.get(OPT_BATTERY_KWH))
    if battery_kwh is None:
        detected = [d.attributes.get("capacityKwh") for d in devices if d.type == BATTERY]
        total = sum(c for c in detected if isinstance(c, (int, float)))
        battery_kwh = _number("battery_kwh", total) if total else None
    heat_pump_kw = _number("heat_pump_kw", options.get(OPT_HEAT_PUMP_KW))
    wallbox_kw = _number("wallbox_kw", options.get(OPT_WALLBOX_KW))

    fields: dict[str, Any] = {}
    if INVERTER in types or pv_kwp:
        fields["pv_system"] = True
        if pv_kwp:
            fields["pv_kwp"] = pv_kwp
    if BATTERY in types or battery_kwh:
        fields["battery_storage"] = True
        if battery_kwh:
            fields["battery_kwh"] = battery_kwh
    if HEAT_PUMP in types or heat_pump_kw:
        fields["heat_pump"] = True
        if heat_pump_kw:
            fields["heat_pump_kw"] = heat_pump_kw
    if EV in types or WALLBOX in types or options.get(OPT_HAS_EV) or wallbox_kw:
        fields["electric_vehicle"] = True
        if wallbox_kw:
            fields["wallbox_kw"] = wallbox_kw
    return fields


class MeineAngabenReporter:
    """Meldet die Geräte bzw. beendet die Freigabe bei der EEG."""

    def __init__(self, hass: HomeAssistant, client: GruenLichtClient, entry: ConfigEntry) -> None:
        self.hass = hass
        self.client = client
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.meine_angaben")
        self.reported_at: str | None = None

    async def async_sync(self) -> None:
        data = await self._store.async_load() or {}
        self.reported_at = data.get("reported_at")
        if self.entry.options.get(OPT_MEINE_ANGABEN):
            fields = build_fields(self.hass, dict(self.entry.options))
            result = await self.client.put_meine_angaben(fields)
            self.reported_at = dt_util.utcnow().isoformat()
            await self._store.async_save({"reported_at": self.reported_at})
            _LOGGER.info(
                "Meine Angaben: %d Felder bekannt, %s neu gemeldet",
                len(fields), (result or {}).get("reported", "?"),
            )
            if (result or {}).get("blocked"):
                _LOGGER.warning("Meine Angaben: Für dieses Mitgliedskonto nimmt die EEG keine Meldungen an")
        elif data.get("reported_at"):
            await self.client.delete_meine_angaben()
            self.reported_at = None
            await self._store.async_save({})
            _LOGGER.info("Freigabe „Meine Angaben“ beendet")

    async def async_remove(self) -> None:
        """Beim Entfernen der Integration: Freigabe beenden (vor dem Abmelden)."""
        data = await self._store.async_load() or {}
        if data.get("reported_at"):
            try:
                await self.client.delete_meine_angaben()
            except GruenLichtError:
                _LOGGER.info("Freigabe „Meine Angaben“ konnte nicht beendet werden")
        await self._store.async_remove()
