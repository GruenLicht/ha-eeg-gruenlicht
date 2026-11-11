"""Stufe 2: Viertelstundenwerte der EEG als Langzeitstatistik in Home Assistant.

Die EEG liefert je Wiener Tag 92/96/100 Viertelstunden (Zeitumstellung). Home
Assistant speichert Langzeitstatistik stündlich, deshalb werden je vier
Viertelstunden zu einer Stunde zusammengefasst und als „external statistics"
mit fortlaufender Summe geschrieben. Im Energie-Dashboard und in der
Statistik-Karte stehen sie dann wie jeder andere Zähler zur Verfügung.

Die Messwerte kommen vom Netzbetreiber mit 10 bis 15 Tagen Verzögerung, bis
dahin liefert die API für diese Tage Nullen. Darum werden bei jedem Lauf die
letzten 30 Tage erneut geladen und die Summe ab dem Stand davor neu aufgebaut.
Welcher Tag zuletzt echte Werte hatte, merkt sich der Importer gesondert.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import GruenLichtClient, GruenLichtError
from .const import DOMAIN

try:  # ab 2025.4
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN: dict[str, Any] = {"mean_type": StatisticMeanType.NONE, "unit_class": "energy"}
except ImportError:  # ältere Versionen
    _MEAN = {"has_mean": False}

_LOGGER = logging.getLogger(__name__)

# (Schlüssel in der Statistik-ID, Feld in /api/energy, Anzeigename)
METRICS: list[tuple[str, str, str]] = [
    ("consumption", "consumptionKwh", "Verbrauch"),
    ("from_eeg", "fromEegKwh", "Bezug aus der EEG"),
    ("from_grid", "fromGridKwh", "Bezug aus dem Netz"),
    ("production", "productionKwh", "Erzeugung"),
    ("to_eeg", "toEegKwh", "Einspeisung in die EEG"),
    ("to_grid", "toGridKwh", "Einspeisung ins Netz"),
]

BACKFILL_DAYS = 365      # so weit holt der erste Lauf höchstens zurück
REFETCH_DAYS = 30        # so viele Tage werden bei jedem Lauf neu geladen
REQUEST_PAUSE = 0.5      # Sekunden zwischen zwei Tagesabfragen (schont die API)

STORE_VERSION = 1


def statistic_id(member_id: str, metric: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", member_id.lower()).strip("_") or "mitglied"
    return f"{DOMAIN}:{slug}_{metric}"


def hourly_values(buckets: list[dict[str, Any]]) -> dict[datetime, dict[str, float]]:
    """Viertelstunden zu UTC-Stunden zusammenfassen."""
    hours: dict[datetime, dict[str, float]] = {}
    for bucket in buckets:
        start = datetime.fromisoformat(bucket["start"])
        hour = dt_util.as_utc(start).replace(minute=0, second=0, microsecond=0)
        acc = hours.setdefault(hour, {m: 0.0 for m, _, _ in METRICS})
        for metric, field, _ in METRICS:
            acc[metric] += float(bucket.get(field) or 0.0)
    return hours


def _has_values(buckets: list[dict[str, Any]]) -> bool:
    return any(float(b.get(field) or 0.0) for b in buckets for _, field, _ in METRICS)


def _parse_day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class StatisticsImporter:
    """Lädt Tageswerte und schreibt sie als stündliche Statistik."""

    def __init__(
        self, hass: HomeAssistant, client: GruenLichtClient, member_id: str, entry_id: str
    ) -> None:
        self.hass = hass
        self.client = client
        self.member_id = member_id
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry_id}")
        self._lock = asyncio.Lock()
        self.last_day: date | None = None       # letzter geladener Tag
        self.last_data_day: date | None = None  # letzter Tag mit echten Werten
        self.running = False

    @property
    def ids(self) -> dict[str, str]:
        return {m: statistic_id(self.member_id, m) for m, _, _ in METRICS}

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.last_day = _parse_day(data.get("last_day"))
        self.last_data_day = _parse_day(data.get("last_data_day"))

    async def async_remove_store(self) -> None:
        await self._store.async_remove()

    async def async_run(self) -> int:
        """Einen Importlauf ausführen. Liefert die Zahl der geladenen Tage."""
        if self._lock.locked():
            return 0
        async with self._lock:
            self.running = True
            try:
                return await self._async_import()
            finally:
                self.running = False

    async def _async_import(self) -> int:
        today = dt_util.now(dt_util.get_time_zone("Europe/Vienna")).date()
        probe = await self.client.energy_day(today.isoformat())
        coverage = probe.get("coverage") or {}
        first = _parse_day(coverage.get("firstDataAt"))
        last = _parse_day(coverage.get("lastDataAt"))
        if first is None or last is None:
            _LOGGER.debug("Noch keine Viertelstundenwerte bei der EEG")
            return 0

        end = min(last, today)
        if self.last_day is not None:
            start = max(first, self.last_day - timedelta(days=REFETCH_DAYS))
        else:
            start = max(first, end - timedelta(days=BACKFILL_DAYS - 1))
        if start > end:
            return 0

        sums = await self._async_sums_before(start)
        days = 0
        day = start
        while day <= end:
            try:
                data = await self.client.energy_day(day.isoformat())
            except GruenLichtError as err:
                _LOGGER.warning("Viertelstundenwerte für %s nicht geladen: %s", day, err)
                break
            buckets = data.get("buckets") or []
            self._write_day(buckets, sums)
            self.last_day = day
            if _has_values(buckets) and (self.last_data_day is None or day > self.last_data_day):
                self.last_data_day = day
            days += 1
            await self._store.async_save(
                {
                    "last_day": day.isoformat(),
                    "last_data_day": self.last_data_day.isoformat() if self.last_data_day else None,
                }
            )
            day += timedelta(days=1)
            if day <= end:
                await asyncio.sleep(REQUEST_PAUSE)

        _LOGGER.info("EEG-Viertelstundenwerte importiert: %d Tage bis %s", days, self.last_day)
        return days

    def _write_day(self, buckets: list[dict[str, Any]], sums: dict[str, float]) -> None:
        hours = hourly_values(buckets)
        for metric, _, name in METRICS:
            rows = []
            for hour in sorted(hours):
                value = hours[hour][metric]
                sums[metric] += value
                rows.append({"start": hour, "state": round(value, 3), "sum": round(sums[metric], 3)})
            if not rows:
                continue
            metadata = {
                **_MEAN,
                "has_sum": True,
                "name": f"EEG GrünLicht {name}",
                "source": DOMAIN,
                "statistic_id": self.ids[metric],
                "unit_of_measurement": "kWh",
            }
            async_add_external_statistics(self.hass, metadata, rows)

    async def _async_sums_before(self, start_day: date) -> dict[str, float]:
        """Fortlaufende Summe je Kennzahl unmittelbar vor dem Importfenster."""
        vienna = dt_util.get_time_zone("Europe/Vienna")
        start = dt_util.as_utc(datetime.combine(start_day, datetime.min.time(), vienna))
        ids = set(self.ids.values())
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(days=2),
            start,
            ids,
            "hour",
            None,
            {"sum"},
        )
        sums: dict[str, float] = {}
        for metric, sid in self.ids.items():
            rows = stats.get(sid) or []
            sums[metric] = float(rows[-1].get("sum") or 0.0) if rows else 0.0
        return sums
