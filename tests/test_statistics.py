"""Stufe 2: Viertelstundenwerte landen als stündliche Statistik mit fortlaufender Summe."""

from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.eeg_gruenlicht.api import DeviceKey
from custom_components.eeg_gruenlicht.const import DOMAIN
from custom_components.eeg_gruenlicht.statistics import hourly_values, statistic_id

BASE = "https://api.eeg-gruenlicht.at/api"
VIENNA = dt_util.get_time_zone("Europe/Vienna")
MEMBER = {"id": "M-123", "name": "Test", "membershipNo": "1", "meterPoints": []}


def _day_buckets(day, consumption=0.1, from_eeg=0.04):
    start = datetime.combine(day, datetime.min.time(), VIENNA)
    out, cur = [], start
    while cur < start + timedelta(days=1):
        out.append(
            {
                "start": cur.isoformat(),
                "consumptionKwh": consumption,
                "fromEegKwh": from_eeg,
                "fromGridKwh": round(consumption - from_eeg, 3),
                "productionKwh": 0.0,
                "toEegKwh": 0.0,
                "toGridKwh": 0.0,
            }
        )
        cur = dt_util.as_utc(cur) + timedelta(minutes=15)
        cur = cur.astimezone(VIENNA)
    return out


def test_dst_day_has_25_hours() -> None:
    hours = hourly_values(_day_buckets(datetime(2026, 10, 25).date()))
    assert len(hours) == 25
    assert all(abs(v["consumption"] - 0.4) < 1e-9 for v in hours.values())


async def test_import_backfills_and_is_idempotent(hass: HomeAssistant, aioclient_mock) -> None:
    today = dt_util.now(VIENNA).date()
    d1, d2, d3 = (today - timedelta(days=n) for n in (3, 2, 1))
    coverage = {"firstDataAt": d1.isoformat(), "lastDataAt": d3.isoformat()}
    for day in (d1, d2):
        aioclient_mock.get(
            f"{BASE}/energy?granularity=day&date={day.isoformat()}",
            json={"buckets": _day_buckets(day), "coverage": coverage},
        )
    # Tag ohne Messwerte vom Netzbetreiber: die API liefert Nullen.
    aioclient_mock.get(
        f"{BASE}/energy?granularity=day&date={d3.isoformat()}",
        json={"buckets": _day_buckets(d3, consumption=0.0, from_eeg=0.0), "coverage": coverage},
    )
    aioclient_mock.get(f"{BASE}/energy", json={"buckets": [], "coverage": coverage})
    aioclient_mock.get(f"{BASE}/me", json=MEMBER)

    key = DeviceKey.generate()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="M-123",
        data={
            "base_url": BASE,
            "session_token": "tok",
            "private_key": key.to_pem(),
            "member_id": "M-123",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    sid = statistic_id("M-123", "consumption")
    assert sid == "eeg_gruenlicht:m_123_consumption"

    async def _last_sum():
        start = dt_util.as_utc(datetime.combine(d1, datetime.min.time(), VIENNA))
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, start, None, {sid}, "hour", None, {"sum", "state"}
        )
        return stats[sid]

    rows = await _last_sum()
    assert len(rows) == 72
    assert abs(rows[0]["state"] - 0.4) < 1e-6
    assert abs(rows[-1]["sum"] - 9.6 * 2) < 1e-6

    state = hass.states.get("sensor.eeg_grunlicht_quarter_hour_values_until")
    assert state.state == d2.isoformat()  # letzter Tag mit echten Werten
    assert state.attributes["last_loaded_day"] == d3.isoformat()

    # Zweiter Lauf lädt das Überlappungsfenster neu, zählt aber nichts doppelt.
    await entry.runtime_data.importer.async_run()
    await async_wait_recording_done(hass)
    rows = await _last_sum()
    assert len(rows) == 72
    assert abs(rows[-1]["sum"] - 19.2) < 1e-6
