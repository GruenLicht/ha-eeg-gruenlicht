"""EEG GrünLicht — Home-Assistant-Integration.

Stufe 1: Kopplung mit dem Mitgliedskonto, Mitgliedsdaten als Sensoren.
Stufe 2: Viertelstundenwerte der EEG als Langzeitstatistik (abschaltbar).
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import DeviceKey, GruenLichtAuthError, GruenLichtClient, GruenLichtError
from .const import (
    CONF_BASE_URL,
    CONF_IMPORT_ENERGY,
    CONF_MEMBER_ID,
    CONF_PRIVATE_KEY,
    CONF_SESSION_TOKEN,
    IMPORT_INTERVAL,
)
from .coordinator import GruenLichtCoordinator
from .statistics import StatisticsImporter

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

type GruenLichtConfigEntry = ConfigEntry[GruenLichtCoordinator]


def _client(hass: HomeAssistant, entry: ConfigEntry, key: DeviceKey) -> GruenLichtClient:
    return GruenLichtClient(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        key,
        entry.data[CONF_SESSION_TOKEN],
    )


def import_enabled(entry: ConfigEntry) -> bool:
    return bool(entry.options.get(CONF_IMPORT_ENERGY, True))


async def async_setup_entry(hass: HomeAssistant, entry: GruenLichtConfigEntry) -> bool:
    key = await hass.async_add_executor_job(DeviceKey.from_pem, entry.data[CONF_PRIVATE_KEY])
    client = _client(hass, entry, key)
    coordinator = GruenLichtCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    importer = StatisticsImporter(hass, client, entry.data[CONF_MEMBER_ID], entry.entry_id)
    await importer.async_load()
    coordinator.importer = importer

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if import_enabled(entry):

        async def _async_import(_now: datetime | None = None) -> None:
            try:
                await importer.async_run()
            except GruenLichtAuthError:
                entry.async_start_reauth(hass)
            except GruenLichtError as err:
                _LOGGER.warning("Import der Viertelstundenwerte fehlgeschlagen: %s", err)
            finally:
                coordinator.async_update_listeners()

        entry.async_on_unload(async_track_time_interval(hass, _async_import, IMPORT_INTERVAL))

        @callback
        def _start(_event=None) -> None:
            entry.async_create_background_task(hass, _async_import(), "eeg_gruenlicht_import")

        _start()

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: GruenLichtConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Beim Entfernen der Integration die Sitzung bei der EEG abmelden."""
    try:
        key = await hass.async_add_executor_job(DeviceKey.from_pem, entry.data[CONF_PRIVATE_KEY])
        await _client(hass, entry, key).logout()
    except (GruenLichtError, KeyError, ValueError):
        _LOGGER.info("Sitzung bei der EEG konnte nicht abgemeldet werden; im Portal trennen")
    importer = StatisticsImporter(hass, None, entry.data.get(CONF_MEMBER_ID, ""), entry.entry_id)  # type: ignore[arg-type]
    await importer.async_remove_store()
