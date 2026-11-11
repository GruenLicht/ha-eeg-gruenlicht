"""Regelmäßige Abfrage von /api/me."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import GruenLichtAuthError, GruenLichtClient, GruenLichtConnectionError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class GruenLichtCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Hält die Mitgliedsdaten aktuell und merkt sich die letzte erfolgreiche Abfrage."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: GruenLichtClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.importer = None  # StatisticsImporter, gesetzt in async_setup_entry

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            member = await self.client.me()
        except GruenLichtAuthError as err:
            # Sitzung im Portal getrennt oder abgelaufen: HA fragt nach einem neuen Code.
            raise ConfigEntryAuthFailed("Verbindung zur EEG wurde getrennt") from err
        except GruenLichtConnectionError as err:
            raise UpdateFailed(f"EEG-API nicht erreichbar: {err}") from err
        return {"member": member, "synced_at": dt_util.utcnow()}
