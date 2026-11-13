"""Stufe 3: Haushaltsprofil an die EEG melden (nur nach Freigabe).

Das Plugin erkennt die Energiegeräte aus der Geräteliste von Home Assistant,
ergänzt sie um die Angaben aus dem Freigabe-Dialog (Leistungen, Lieferverträge)
und schickt das Ergebnis an die GrünLicht-API. Laufende Messwerte und
Seriennummern werden nie übertragen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import GruenLichtClient, GruenLichtError
from .const import DOMAIN, VERSION

_LOGGER = logging.getLogger(__name__)

# Zweck und Empfänger — stehen auch im Freigabe-Dialog und gehen mit der Meldung mit.
PURPOSE = "individuellere Angebote der EEG"
RECIPIENTS = "Vorstand der EEG; Entwickler nur, soweit sie ein konkretes Angebot umsetzen"

# Gerätetypen
INVERTER = "inverter"
BATTERY = "battery"
HEAT_PUMP = "heat_pump"
WALLBOX = "wallbox"
EV = "electric_vehicle"
METER = "meter"
DEVICE_TYPES = (INVERTER, BATTERY, HEAT_PUMP, WALLBOX, EV, METER)

# Integrationen, deren Geräte eindeutig einem Typ zugehören.
DOMAIN_TYPES: dict[str, str] = {
    # Wärmepumpen
    "daikin_onecta": HEAT_PUMP, "luxtronik": HEAT_PUMP, "luxtronik2": HEAT_PUMP,
    "nibe_heatpump": HEAT_PUMP, "myuplink": HEAT_PUMP, "vicare": HEAT_PUMP,
    "mypyllant": HEAT_PUMP, "stiebel_eltron": HEAT_PUMP, "idm_heatpump": HEAT_PUMP,
    "ochsner": HEAT_PUMP, "lambda_heat_pumps": HEAT_PUMP,
    # Wallboxen
    "evcc": WALLBOX, "go_e": WALLBOX, "goecharger": WALLBOX, "go_echarger": WALLBOX,
    "easee": WALLBOX, "keba": WALLBOX, "wallbox": WALLBOX, "zaptec": WALLBOX,
    "ocpp": WALLBOX, "nrgkick": WALLBOX, "openwb": WALLBOX, "webasto_unite": WALLBOX,
    # E-Autos
    "tesla_fleet": EV, "teslemetry": EV, "tessie": EV, "bmw_connected_drive": EV,
    "volvo": EV, "renault": EV, "kia_uvo": EV, "hyundai_kia_connect": EV,
    "volkswagen_we_connect_id": EV, "audiconnect": EV, "mercedes_me": EV, "skoda_connect": EV,
}

# Integrationen von Wechselrichtern; ihre Geräte werden nach Modell weiter unterschieden.
INVERTER_DOMAINS = {
    "fronius", "sma", "solaredge", "huawei_solar", "enphase_envoy", "kostal_plenticore",
    "growatt_server", "solax", "goodwe", "sungrow", "solarman", "victron", "sonnen",
    "senec", "e3dc", "fronius_modbus", "solarlog", "apsystems", "hoymiles",
}

_RE_METER = re.compile(r"smart ?meter|zähler|meter ts|energy ?meter", re.I)
_RE_BATTERY = re.compile(r"batter|speicher|storage|powerwall|byd|battery-box", re.I)
_RE_GATEWAY = re.compile(r"gateway|datalogger|solarnet|brp\d|bridge|hub", re.I)
_RE_HEAT_PUMP = re.compile(r"wärmepumpe|heat ?pump|altherma|vitocal|arotherm|luxtronik", re.I)
_RE_WALLBOX = re.compile(r"wallbox|charger|ladestation|evse", re.I)
_RE_CAPACITY = re.compile(r"capacit|kapazit", re.I)


@dataclass
class DetectedDevice:
    """Ein erkanntes Energiegerät."""

    key: str                      # stabile ID innerhalb von Home Assistant (device_id)
    type: str
    manufacturer: str | None
    model: str | None
    name: str | None
    integration: str
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        parts = [p for p in (self.manufacturer, self.model or self.name) if p]
        return " ".join(parts) or self.integration

    def as_payload(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "integration": self.integration,
            "attributes": self.attributes,
        }


def _classify(domain: str, device: dr.DeviceEntry) -> str | None:
    text = " ".join(filter(None, [device.manufacturer, device.model, device.name]))
    if domain in DOMAIN_TYPES:
        return DOMAIN_TYPES[domain]
    if domain in INVERTER_DOMAINS:
        if _RE_METER.search(text):
            return METER
        if _RE_BATTERY.search(text):
            return BATTERY
        if _RE_GATEWAY.search(text) or not device.model:
            return None
        return INVERTER
    if _RE_HEAT_PUMP.search(text):
        return HEAT_PUMP
    if _RE_WALLBOX.search(text):
        return WALLBOX
    return None


def _score(device: dr.DeviceEntry) -> int:
    """Welches Gerät einer Integration ist das „Hauptgerät"? (Daikin legt 7 an.)"""
    score = 0
    if device.model:
        score += 2
        if not _RE_GATEWAY.search(device.model):
            score += 2
    if device.name and re.search(r"indoor|hydro|inneneinheit", device.name, re.I):
        score += 1
    if device.name and _RE_GATEWAY.search(device.name):
        score -= 3
    return score


def _battery_capacity_kwh(hass: HomeAssistant, entities: list[er.RegistryEntry]) -> float | None:
    for ent in entities:
        if ent.domain != "sensor":
            continue
        ident = f"{ent.entity_id} {ent.original_name or ''} {ent.unique_id}"
        if not _RE_CAPACITY.search(ident):
            continue
        state = hass.states.get(ent.entity_id)
        if state is None:
            continue
        try:
            value = float(state.state)
        except ValueError:
            continue
        unit = state.attributes.get("unit_of_measurement")
        if unit == "Wh":
            return round(value / 1000, 2)
        if unit == "kWh":
            return round(value, 2)
    return None


def detect_devices(hass: HomeAssistant) -> list[DetectedDevice]:
    """Energiegeräte aus der Geräteliste erkennen, je Integration und Typ eines."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    best: dict[tuple[str, str], tuple[int, dr.DeviceEntry, str]] = {}

    for device in dev_reg.devices.values():
        if device.disabled_by:
            continue
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain == DOMAIN:
                continue
            dtype = _classify(entry.domain, device)
            if dtype is None:
                continue
            # Batterie und Zähler können je Integration mehrfach vorkommen,
            # Wechselrichter-Geräte ebenso (mehrere WR) — Schlüssel ist dann das Gerät.
            group = device.id if dtype in (INVERTER, BATTERY, METER) else entry_id
            key = (dtype, group)
            score = _score(device)
            if key not in best or score > best[key][0]:
                best[key] = (score, device, entry.domain)

    result: list[DetectedDevice] = []
    for (dtype, _), (_, device, domain) in sorted(best.items(), key=lambda i: (DEVICE_TYPES.index(i[0][0]), i[1][1].name or "")):
        attributes: dict[str, Any] = {}
        if dtype == BATTERY:
            capacity = _battery_capacity_kwh(hass, er.async_entries_for_device(ent_reg, device.id))
            if capacity:
                attributes["capacityKwh"] = capacity
        result.append(
            DetectedDevice(
                key=device.id,
                type=dtype,
                manufacturer=device.manufacturer,
                model=device.model,
                name=device.name_by_user or device.name,
                integration=domain,
                attributes=attributes,
            )
        )
    return result


# --- Angaben aus dem Freigabe-Dialog ----------------------------------------

OPT_REPORT = "report_profile"
OPT_DEVICES = "devices"
OPT_PV_KWP = "pv_kwp"
OPT_HEAT_PUMP_KW = "heat_pump_kw"
OPT_WALLBOX_KW = "wallbox_kw"
OPT_HAS_EV = "has_ev"
OPT_SUPPLIER = "supplier"
OPT_SUPPLY_PRODUCT = "supply_product"
OPT_SUPPLY_TARIFF = "supply_tariff_type"
OPT_BUYER = "feed_in_buyer"
OPT_FEED_IN_PRODUCT = "feed_in_product"
OPT_FEED_IN_TARIFF = "feed_in_tariff_type"
OPT_NOTES = "notes"
OPT_CONSENT_AT = "profile_consent_at"

TARIFF_TYPES = ["fixed", "indexed", "dynamic", "other", "unknown"]


def build_payload(hass: HomeAssistant, options: dict[str, Any]) -> dict[str, Any]:
    """Die Meldung an die EEG aus erkannten Geräten und Dialog-Angaben bauen."""
    selected = set(options.get(OPT_DEVICES) or [])
    devices = [d for d in detect_devices(hass) if d.key in selected]

    def add_attr(dtype: str, key: str, value: Any) -> None:
        if value in (None, "", 0):
            return
        target = next((d for d in devices if d.type == dtype), None)
        if target is not None:
            target.attributes[key] = value
        else:
            devices.append(
                DetectedDevice(key=f"manual-{dtype}", type=dtype, manufacturer=None,
                               model=None, name=None, integration="manual",
                               attributes={key: value})
            )

    add_attr(INVERTER, "pvPowerKwp", options.get(OPT_PV_KWP))
    add_attr(HEAT_PUMP, "powerKw", options.get(OPT_HEAT_PUMP_KW))
    add_attr(WALLBOX, "chargingPowerKw", options.get(OPT_WALLBOX_KW))
    if options.get(OPT_HAS_EV) and not any(d.type == EV for d in devices):
        devices.append(DetectedDevice(key="manual-ev", type=EV, manufacturer=None, model=None,
                                      name=None, integration="manual"))

    def contract(party_key: str, product_key: str, tariff_key: str) -> dict[str, Any] | None:
        party = (options.get(party_key) or "").strip()
        product = (options.get(product_key) or "").strip()
        tariff = options.get(tariff_key) or "unknown"
        if not party and not product:
            return None
        return {"company": party or None, "product": product or None, "tariffType": tariff}

    return {
        "pluginVersion": VERSION,
        "consentAt": options.get(OPT_CONSENT_AT),
        "purpose": PURPOSE,
        "devices": [d.as_payload() for d in devices],
        "supply": contract(OPT_SUPPLIER, OPT_SUPPLY_PRODUCT, OPT_SUPPLY_TARIFF),
        "feedIn": contract(OPT_BUYER, OPT_FEED_IN_PRODUCT, OPT_FEED_IN_TARIFF),
        "notes": (options.get(OPT_NOTES) or "").strip() or None,
    }


class ProfileReporter:
    """Meldet das Profil bzw. löscht es wieder, wenn die Freigabe entzogen wird."""

    def __init__(self, hass: HomeAssistant, client: GruenLichtClient, entry: ConfigEntry) -> None:
        self.hass = hass
        self.client = client
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.profile")
        self.reported_at: str | None = None

    async def async_sync(self) -> None:
        data = await self._store.async_load() or {}
        self.reported_at = data.get("reported_at")
        if self.entry.options.get(OPT_REPORT):
            payload = build_payload(self.hass, dict(self.entry.options))
            await self.client.put_home_profile(payload)
            self.reported_at = dt_util.utcnow().isoformat()
            await self._store.async_save({"reported_at": self.reported_at})
            _LOGGER.info("Haushaltsprofil an die EEG gemeldet (%d Geräte)", len(payload["devices"]))
        elif data.get("reported_at"):
            await self.client.delete_home_profile()
            self.reported_at = None
            await self._store.async_save({})
            _LOGGER.info("Freigabe entzogen: Haushaltsprofil bei der EEG gelöscht")

    async def async_remove(self) -> None:
        """Beim Entfernen der Integration: Profil löschen (vor dem Abmelden)."""
        data = await self._store.async_load() or {}
        if data.get("reported_at"):
            try:
                await self.client.delete_home_profile()
            except GruenLichtError:
                _LOGGER.info("Haushaltsprofil konnte nicht gelöscht werden")
        await self._store.async_remove()
