"""Freigabe „Geräte in Meine Angaben" und Statusmeldung (ab 0.4.0)."""

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eeg_gruenlicht.api import DeviceKey
from custom_components.eeg_gruenlicht.const import DOMAIN, VERSION
from custom_components.eeg_gruenlicht.meine_angaben import build_fields
from custom_components.eeg_gruenlicht.profile import detect_devices

from .test_profile import CATALOG, MEMBER, _add_home

BASE = "https://api.eeg-gruenlicht.at/api"
RESULT = {"reported": 6, "rejected": [], "blocked": False, "unreachable": False}


def _all_devices(hass: HomeAssistant) -> list[str]:
    return [d.key for d in detect_devices(hass)]


async def test_fields_from_detected_devices_and_dialog(hass: HomeAssistant) -> None:
    _add_home(hass)
    fields = build_fields(
        hass,
        {"devices": _all_devices(hass), "pv_kwp": 9.8, "heat_pump_kw": 3.456, "wallbox_kw": 11},
    )
    assert fields == {
        "pv_system": True,
        "pv_kwp": 9.8,
        "battery_storage": True,
        "battery_kwh": 12.8,  # aus dem erkannten BYD-Speicher
        "heat_pump": True,
        "heat_pump_kw": 3.46,
        "electric_vehicle": True,  # Ladeleistung heißt E-Auto
        "wallbox_kw": 11.0,
    }


async def test_fields_never_report_no_and_drop_out_of_range(hass: HomeAssistant) -> None:
    _add_home(hass)
    # Nichts ausgewählt, nichts eingetragen: nichts melden (auch kein „nein").
    assert build_fields(hass, {"devices": []}) == {}
    # Eingetragene Kapazität schlägt die erkannte; Werte außerhalb des Bereichs fallen weg.
    fields = build_fields(
        hass, {"devices": _all_devices(hass), "battery_kwh": 15, "pv_kwp": 250, "heat_pump_kw": 0}
    )
    assert fields["battery_kwh"] == 15.0
    assert fields["pv_system"] is True and "pv_kwp" not in fields
    assert "heat_pump_kw" not in fields and fields["heat_pump"] is True
    assert "electric_vehicle" not in fields
    assert build_fields(hass, {"has_ev": True}) == {"electric_vehicle": True}


async def _setup(hass: HomeAssistant, aioclient_mock) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/me", json=MEMBER)
    aioclient_mock.get(f"{BASE}/energy", json={"buckets": [], "coverage": {}})
    aioclient_mock.get(f"{BASE}/contract-catalog", json=CATALOG)
    aioclient_mock.put(f"{BASE}/home-profile", status=204)
    aioclient_mock.put(f"{BASE}/home-assistant/status", status=204)
    aioclient_mock.put(f"{BASE}/home-assistant/meine-angaben", json=RESULT)
    aioclient_mock.delete(f"{BASE}/home-assistant/meine-angaben", status=204)
    aioclient_mock.delete(f"{BASE}/session", status=204)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="M-1",
        data={
            "base_url": BASE,
            "session_token": "tok",
            "private_key": DeviceKey.generate().to_pem(),
            "member_id": "M-1",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def _calls(aioclient_mock, method: str, suffix: str) -> list:
    return [c for c in aioclient_mock.mock_calls if c[0] == method and str(c[1]).endswith(suffix)]


async def test_status_reported_on_setup(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    status = _calls(aioclient_mock, "PUT", "/home-assistant/status")
    assert status[-1][2] == {
        "pluginVersion": VERSION,
        "importEnergy": True,
        "reportProfile": False,
        "meineAngaben": False,
        "controllable": False,
    }
    assert _calls(aioclient_mock, "PUT", "/meine-angaben") == []
    assert _calls(aioclient_mock, "DELETE", "/meine-angaben") == []


async def test_meine_angaben_without_household_profile(hass: HomeAssistant, aioclient_mock) -> None:
    _add_home(hass)
    entry = await _setup(hass, aioclient_mock)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"import_energy": True, "report_profile": False, "meine_angaben": True}
    )
    assert result["step_id"] == "devices"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"pv_kwp": 9.8, "heat_pump_kw": 3.5}
    )
    # Ohne Haushaltsprofil keine Fragen zu Lieferverträgen.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)

    puts = _calls(aioclient_mock, "PUT", "/home-assistant/meine-angaben")
    assert len(puts) == 1
    assert puts[0][2] == {
        "fields": {
            "pv_system": True,
            "pv_kwp": 9.8,
            "battery_storage": True,
            "battery_kwh": 12.8,
            "heat_pump": True,
            "heat_pump_kw": 3.5,
        }
    }
    assert _calls(aioclient_mock, "PUT", "/home-profile") == []
    assert _calls(aioclient_mock, "PUT", "/home-assistant/status")[-1][2]["meineAngaben"] is True
    state = hass.states.get("sensor.eeg_grunlicht_meine_angaben_reported")
    assert state.state not in ("unknown", "unavailable")

    # Freigabe abschalten -> Widerruf bei der EEG (Werte bleiben dort stehen).
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"import_energy": True, "report_profile": False, "meine_angaben": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(_calls(aioclient_mock, "DELETE", "/home-assistant/meine-angaben")) == 1
    assert len(_calls(aioclient_mock, "PUT", "/home-assistant/meine-angaben")) == 1
    assert _calls(aioclient_mock, "PUT", "/home-assistant/status")[-1][2]["meineAngaben"] is False


async def test_remove_ends_meine_angaben_before_logout(hass: HomeAssistant, aioclient_mock) -> None:
    _add_home(hass)
    entry = await _setup(hass, aioclient_mock)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"import_energy": True, "report_profile": False, "meine_angaben": True}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    order = [str(c[1]).rsplit("/api", 1)[1] for c in aioclient_mock.mock_calls if c[0] == "DELETE"]
    assert order == ["/home-assistant/meine-angaben", "/session"]
