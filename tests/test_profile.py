"""Stufe 3: Geräte erkennen, Freigabe-Dialog, Meldung und Löschen."""

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eeg_gruenlicht.api import DeviceKey
from custom_components.eeg_gruenlicht.const import DOMAIN
from custom_components.eeg_gruenlicht.profile import detect_devices

BASE = "https://api.eeg-gruenlicht.at/api"
MEMBER = {"id": "M-1", "name": "Test", "membershipNo": "1", "meterPoints": []}
CATALOG = {
    "source": "test",
    "suppliers": [
        {"id": "s1", "name": "Wien Energie", "products": [{"id": "p1", "name": "Optima Garant", "tariffType": "fixed"}]},
        {"id": "s2", "name": "EVN", "products": []},
    ],
    "feedInBuyers": [],
}


def _add_home(hass: HomeAssistant) -> None:
    """Geräte wie bei Christoph: Fronius GEN24 + Smart Meter + BYD, Daikin Altherma."""
    fronius = MockConfigEntry(domain="fronius", title="SolarNet")
    daikin = MockConfigEntry(domain="daikin_onecta", title="Daikin")
    fronius.add_to_hass(hass)
    daikin.add_to_hass(hass)
    reg = dr.async_get(hass)

    def dev(entry, ident, manufacturer, model, name):
        return reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(entry.domain, ident)},
            manufacturer=manufacturer, model=model, name=name,
        )

    dev(fronius, "logger", "Fronius", None, "SolarNet")
    dev(fronius, "inv", "Fronius", "GEN24/Tauro/Verto", "PV-Fabianek")
    dev(fronius, "meter", "Fronius", "Smart Meter TS 65A-3", "Smart Meter TS 65A-3")
    battery = dev(fronius, "bat", "BYD", "BYD Battery-Box Premium HV", "BYD Battery-Box Premium HV")
    dev(daikin, "gw", "Daikin", "BRP069A78", "Altherma Gateway")
    dev(daikin, "main", "Daikin", "BRP069A78", "Altherma")
    dev(daikin, "hydro", "Daikin", "ELVH12S23EJ9W", "Altherma IndoorUnitHydro")
    dev(daikin, "out", "Daikin", None, "Altherma OutdoorUnit")

    ent = er.async_get(hass).async_get_or_create(
        "sensor", "fronius", "bat_cap", config_entry=fronius, device_id=battery.id,
        original_name="Maximale Kapazität",
    )
    hass.states.async_set(ent.entity_id, "12800.0", {"unit_of_measurement": "Wh"})


async def test_detect_devices(hass: HomeAssistant) -> None:
    _add_home(hass)
    found = {(d.type, d.model): d for d in detect_devices(hass)}
    assert set(found) == {
        ("inverter", "GEN24/Tauro/Verto"),
        ("battery", "BYD Battery-Box Premium HV"),
        ("meter", "Smart Meter TS 65A-3"),
        ("heat_pump", "ELVH12S23EJ9W"),
    }
    assert found[("battery", "BYD Battery-Box Premium HV")].attributes == {"capacityKwh": 12.8}


async def _setup(hass: HomeAssistant, aioclient_mock) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/me", json=MEMBER)
    aioclient_mock.get(f"{BASE}/energy", json={"buckets": [], "coverage": {}})
    aioclient_mock.get(f"{BASE}/contract-catalog", json=CATALOG)
    aioclient_mock.put(f"{BASE}/home-profile", status=204)
    aioclient_mock.delete(f"{BASE}/home-profile", status=204)
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


def _calls(aioclient_mock, method: str) -> list:
    return [c for c in aioclient_mock.mock_calls if c[0] == method and str(c[1]).endswith("/home-profile")]


async def test_nothing_reported_without_permission(hass: HomeAssistant, aioclient_mock) -> None:
    _add_home(hass)
    await _setup(hass, aioclient_mock)
    assert _calls(aioclient_mock, "PUT") == []
    assert _calls(aioclient_mock, "DELETE") == []


async def test_options_flow_reports_and_withdraws(hass: HomeAssistant, aioclient_mock) -> None:
    _add_home(hass)
    entry = await _setup(hass, aioclient_mock)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"import_energy": True, "report_profile": True}
    )
    assert result["step_id"] == "devices"
    assert result["description_placeholders"]["count"] == "4"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"pv_kwp": 9.8, "heat_pump_kw": 3.5, "has_ev": True, "notes": "Pool-Pumpe"}
    )
    assert result["step_id"] == "supply"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "Wien Energie", "supply_tariff_type": "unknown"}
    )
    assert result["step_id"] == "supply_product"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supply_product": "Optima Garant"}
    )
    assert result["step_id"] == "feed_in"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"feed_in_buyer": "OeMAG", "feed_in_product": "Marktpreis", "feed_in_tariff_type": "indexed"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)

    puts = _calls(aioclient_mock, "PUT")
    assert len(puts) == 1
    body = puts[0][2]
    assert body["purpose"] == "individuellere Angebote der EEG"
    assert body["consentAt"]
    types = {d["type"]: d for d in body["devices"]}
    assert set(types) == {"inverter", "battery", "meter", "heat_pump", "electric_vehicle"}
    assert types["inverter"]["attributes"] == {"pvPowerKwp": 9.8}
    assert types["battery"]["attributes"] == {"capacityKwh": 12.8}
    assert types["heat_pump"]["model"] == "ELVH12S23EJ9W"
    assert types["heat_pump"]["attributes"] == {"powerKw": 3.5}
    assert body["supply"] == {"company": "Wien Energie", "product": "Optima Garant", "tariffType": "fixed"}
    assert body["feedIn"] == {"company": "OeMAG", "product": "Marktpreis", "tariffType": "indexed"}
    assert body["notes"] == "Pool-Pumpe"
    assert "serial" not in str(body).lower()

    state = hass.states.get("sensor.eeg_grunlicht_household_profile_reported")
    assert state.state not in ("unknown", "unavailable")

    # Freigabe entziehen -> Profil wird gelöscht.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"import_energy": True, "report_profile": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(_calls(aioclient_mock, "DELETE")) == 1
    assert entry.options["profile_consent_at"] is None


async def test_product_asked_as_free_text_for_supplier_without_catalog_products(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Lieferant ohne Produkte im Katalog oder gar nicht im Katalog: Produkt als Freitext."""
    _add_home(hass)
    entry = await _setup(hass, aioclient_mock)

    for supplier, product in (("EVN", "Optima Aktiv Strom"), ("Pullstrom", "Ora")):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"import_energy": True, "report_profile": True}
        )
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["step_id"] == "supply"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": supplier, "supply_tariff_type": "indexed"}
        )
        assert result["step_id"] == "supply_product", supplier
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supply_product": product}
        )
        assert result["step_id"] == "feed_in"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done(wait_background_tasks=True)

        body = _calls(aioclient_mock, "PUT")[-1][2]
        assert body["supply"] == {"company": supplier, "product": product, "tariffType": "indexed"}
        assert body["feedIn"] is None
