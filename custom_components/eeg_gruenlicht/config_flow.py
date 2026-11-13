"""Einrichtung: Kurzcode eingeben, im Portal bestätigen, fertig."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .api import (
    DeviceKey,
    GruenLichtClient,
    GruenLichtConnectionError,
    GruenLichtError,
    GruenLichtPairingDenied,
    GruenLichtRateLimited,
    normalize_code,
)
from .profile import (
    OPT_BUYER,
    OPT_CONSENT_AT,
    OPT_DEVICES,
    OPT_FEED_IN_PRODUCT,
    OPT_FEED_IN_TARIFF,
    OPT_HAS_EV,
    OPT_HEAT_PUMP_KW,
    OPT_NOTES,
    OPT_PV_KWP,
    OPT_REPORT,
    OPT_SUPPLIER,
    OPT_SUPPLY_PRODUCT,
    OPT_SUPPLY_TARIFF,
    OPT_WALLBOX_KW,
    PURPOSE,
    RECIPIENTS,
    TARIFF_TYPES,
    detect_devices,
)
from .const import (
    CONF_BASE_URL,
    CONF_CODE,
    CONF_IMPORT_ENERGY,
    CONF_MEMBER_ID,
    CONF_PRIVATE_KEY,
    CONF_SESSION_TOKEN,
    DEFAULT_BASE_URL,
    DEVICE_LABEL,
    DOMAIN,
    PAIR_POLL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): str})


class GruenLichtConfigFlow(ConfigFlow, domain=DOMAIN):
    """Kopplung mit der EEG über Kurzcode und Portal-Bestätigung."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GruenLichtOptionsFlow()

    def __init__(self) -> None:
        self._base_url = DEFAULT_BASE_URL
        self._key: DeviceKey | None = None
        self._client: GruenLichtClient | None = None
        self._pairing: dict[str, Any] | None = None
        self._wait_task: asyncio.Task | None = None
        self._result: dict[str, Any] | None = None
        self._failure: str | None = None

    # --- Schritt 1: Kurzcode -------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_code_step("user", user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._base_url = entry_data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_code_step("reauth_confirm", user_input)

    async def _async_code_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = normalize_code(user_input[CONF_CODE])
            if code is None:
                errors[CONF_CODE] = "invalid_format"
            else:
                self._key = await self.hass.async_add_executor_job(DeviceKey.generate)
                self._client = GruenLichtClient(
                    async_get_clientsession(self.hass), self._base_url, self._key
                )
                try:
                    self._pairing = await self._client.start_pairing(code, DEVICE_LABEL)
                except GruenLichtPairingDenied:
                    errors["base"] = "invalid_code"
                except GruenLichtRateLimited:
                    errors["base"] = "too_many_attempts"
                except GruenLichtConnectionError:
                    errors["base"] = "cannot_connect"
                except GruenLichtError:
                    _LOGGER.exception("Unerwarteter Fehler beim Pairing")
                    errors["base"] = "unknown"
                else:
                    return await self.async_step_confirm()

        return self.async_show_form(step_id=step_id, data_schema=CODE_SCHEMA, errors=errors)

    # --- Schritt 2: auf die Bestätigung im Portal warten ---------------------

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._pairing is not None
        if self._wait_task is None:
            self._wait_task = self.hass.async_create_task(self._async_wait_for_portal())

        if not self._wait_task.done():
            return self.async_show_progress(
                step_id="confirm",
                progress_action="wait_confirm",
                description_placeholders={"pairing_code": str(self._pairing["pairingCode"])},
                progress_task=self._wait_task,
            )

        outcome, payload = self._wait_task.result()
        self._wait_task = None
        if outcome == "confirmed":
            self._result = payload
            return self.async_show_progress_done(next_step_id="finish")
        self._failure = outcome
        return self.async_show_progress_done(next_step_id="failed")

    async def _async_wait_for_portal(self) -> tuple[str, dict[str, Any] | None]:
        """Status abfragen, bis das Mitglied bestätigt, ablehnt oder es abläuft."""
        assert self._client is not None and self._pairing is not None
        pairing_id = str(self._pairing["pairingId"])
        deadline = asyncio.get_running_loop().time() + int(self._pairing.get("expiresIn", 120)) + 10
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(PAIR_POLL_SECONDS)
            try:
                data = await self._client.pairing_status(pairing_id)
            except GruenLichtConnectionError:
                continue  # kurzer Aussetzer, weiter warten
            except GruenLichtError:
                return "denied", None
            state = data.get("status")
            if state == "confirmed" and data.get("sessionToken"):
                return "confirmed", data
            if state == "rejected":
                return "rejected", None
            if state == "expired":
                return "expired", None
        return "expired", None

    async def async_step_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason=f"pairing_{self._failure or 'denied'}")

    # --- Schritt 3: Eintrag anlegen ------------------------------------------

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._result is not None and self._key is not None and self._client is not None
        token = self._result["sessionToken"]
        member = self._result.get("member") or {}
        member_id = str(member.get("id", ""))
        data = {
            CONF_BASE_URL: self._base_url,
            CONF_SESSION_TOKEN: token,
            CONF_PRIVATE_KEY: self._key.to_pem(),
            CONF_MEMBER_ID: member_id,
        }

        await self.async_set_unique_id(member_id)
        if self.source == SOURCE_REAUTH:
            entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
            if entry is None or entry.unique_id != member_id:
                await self._async_discard_session(token)
                return self.async_abort(reason="wrong_member")
            return self.async_update_reload_and_abort(entry, data=data)

        if self._async_current_entries() and any(
            e.unique_id == member_id for e in self._async_current_entries()
        ):
            await self._async_discard_session(token)
            return self.async_abort(reason="already_configured")

        name = member.get("name") or "Mitglied"
        return self.async_create_entry(title=f"EEG GrünLicht – {name}", data=data)

    async def _async_discard_session(self, token: str) -> None:
        """Eine gerade ausgestellte, nicht gebrauchte Sitzung wieder abmelden."""
        assert self._client is not None
        self._client.session_token = token
        try:
            await self._client.logout()
        except GruenLichtError:
            _LOGGER.debug("Überzählige Sitzung konnte nicht abgemeldet werden")


TYPE_LABELS = {
    "de": {"inverter": "Wechselrichter", "battery": "Batteriespeicher", "heat_pump": "Wärmepumpe",
           "wallbox": "Wallbox", "electric_vehicle": "E-Auto", "meter": "Zähler"},
    "en": {"inverter": "Inverter", "battery": "Battery", "heat_pump": "Heat pump",
           "wallbox": "Wallbox", "electric_vehicle": "Electric vehicle", "meter": "Meter"},
}

DEVICE_FIELDS = (OPT_DEVICES, OPT_PV_KWP, OPT_HEAT_PUMP_KW, OPT_WALLBOX_KW, OPT_HAS_EV, OPT_NOTES)
SUPPLY_FIELDS = (OPT_SUPPLIER, OPT_SUPPLY_PRODUCT, OPT_SUPPLY_TARIFF)
FEED_IN_FIELDS = (OPT_BUYER, OPT_FEED_IN_PRODUCT, OPT_FEED_IN_TARIFF)


def _number(unit: str, maximum: float) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=0, max=maximum, step=0.1, unit_of_measurement=unit,
                             mode=NumberSelectorMode.BOX)
    )


def _suggested(value: Any) -> dict[str, Any]:
    return {"suggested_value": value} if value not in (None, "") else {}


def _names(items: list[dict[str, Any]] | None) -> list[str]:
    return sorted({str(i.get("name")) for i in items or [] if i.get("name")})


def _company(items: list[dict[str, Any]] | None, company: str | None) -> dict[str, Any] | None:
    for item in items or []:
        if company and str(item.get("name", "")).casefold() == company.casefold():
            return item
    return None


def _products(items: list[dict[str, Any]] | None, company: str | None) -> list[str]:
    found = _company(items, company)
    return _names(found.get("products")) if found else []


def _catalog_tariff(items: list[dict[str, Any]] | None, company: str | None, product: str | None) -> str | None:
    """Tarifart aus dem Katalog, wenn das Produkt dort eingeordnet ist."""
    found = _company(items, company)
    for prod in (found or {}).get("products") or []:
        if product and str(prod.get("name", "")).casefold() == product.casefold():
            value = prod.get("tariffType")
            return value if value in TARIFF_TYPES else None
    return None


class GruenLichtOptionsFlow(OptionsFlow):
    """Freigaben je Stufe; Stufe 3 mit Geräteauswahl und Lieferverträgen."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._catalog: dict[str, Any] | None = None

    @property
    def _opts(self) -> dict[str, Any]:
        return {**self.config_entry.options, **self._data}

    def _take(self, fields: tuple[str, ...], user_input: dict[str, Any]) -> None:
        self._data.update({k: None for k in fields})
        self._data.update(user_input)

    async def _async_catalog(self) -> dict[str, Any]:
        if self._catalog is None:
            coordinator = getattr(self.config_entry, "runtime_data", None)
            client = getattr(coordinator, "client", None)
            self._catalog = (await client.contract_catalog() if client else None) or {}
        return self._catalog

    def _company_selector(self, names: list[str]) -> Any:
        if not names:
            return TextSelector()
        return SelectSelector(
            SelectSelectorConfig(options=names, custom_value=True, mode=SelectSelectorMode.DROPDOWN, sort=True)
        )

    def _tariff_selector(self) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(options=TARIFF_TYPES, translation_key="tariff_type",
                                 mode=SelectSelectorMode.DROPDOWN)
        )

    # --- Schritt 1: Freigaben -------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            if user_input.get(OPT_REPORT):
                return await self.async_step_devices()
            return self._finish()
        opts = self._opts
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_IMPORT_ENERGY, default=opts.get(CONF_IMPORT_ENERGY, True)): bool,
                    vol.Required(OPT_REPORT, default=bool(opts.get(OPT_REPORT, False))): bool,
                }
            ),
            description_placeholders={"purpose": PURPOSE, "recipients": RECIPIENTS},
        )

    # --- Schritt 2: Geräte ------------------------------------------------------

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._take(DEVICE_FIELDS, user_input)
            return await self.async_step_supply()

        opts = self._opts
        lang = "de" if self.hass.config.language.startswith("de") else "en"
        detected = detect_devices(self.hass)
        keys = [d.key for d in detected]
        chosen = [k for k in (opts.get(OPT_DEVICES) or keys) if k in keys]
        schema: dict[Any, Any] = {}
        if detected:
            schema[vol.Optional(OPT_DEVICES, default=chosen)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=d.key, label=f"{TYPE_LABELS[lang][d.type]}: {d.label}")
                        for d in detected
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        schema[vol.Optional(OPT_PV_KWP, description=_suggested(opts.get(OPT_PV_KWP)))] = _number("kWp", 1000)
        schema[vol.Optional(OPT_HEAT_PUMP_KW, description=_suggested(opts.get(OPT_HEAT_PUMP_KW)))] = _number("kW", 100)
        schema[vol.Optional(OPT_WALLBOX_KW, description=_suggested(opts.get(OPT_WALLBOX_KW)))] = _number("kW", 50)
        schema[vol.Optional(OPT_HAS_EV, default=bool(opts.get(OPT_HAS_EV, False)))] = bool
        schema[vol.Optional(OPT_NOTES, description=_suggested(opts.get(OPT_NOTES)))] = TextSelector(
            TextSelectorConfig(multiline=True)
        )
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(schema),
            description_placeholders={"count": str(len(detected))},
        )

    # --- Schritt 3: Strombezug --------------------------------------------------

    async def async_step_supply(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        catalog = await self._async_catalog()
        suppliers = catalog.get("suppliers")
        if user_input is not None:
            self._take(SUPPLY_FIELDS, user_input)
            # Mit Katalog fragt der Folgeschritt das Produkt ab: als Auswahl, wenn
            # der Lieferant dort Produkte hat, sonst als Freitext.
            if suppliers and user_input.get(OPT_SUPPLIER):
                return await self.async_step_supply_product()
            return await self.async_step_feed_in()

        opts = self._opts
        schema: dict[Any, Any] = {
            vol.Optional(OPT_SUPPLIER, description=_suggested(opts.get(OPT_SUPPLIER))): self._company_selector(_names(suppliers)),
        }
        if not suppliers:
            schema[vol.Optional(OPT_SUPPLY_PRODUCT, description=_suggested(opts.get(OPT_SUPPLY_PRODUCT)))] = TextSelector()
        schema[vol.Optional(OPT_SUPPLY_TARIFF, default=opts.get(OPT_SUPPLY_TARIFF) or "unknown")] = self._tariff_selector()
        return self.async_show_form(step_id="supply", data_schema=vol.Schema(schema))

    async def async_step_supply_product(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[OPT_SUPPLY_PRODUCT] = user_input.get(OPT_SUPPLY_PRODUCT)
            self._fill_tariff(OPT_SUPPLY_TARIFF, "suppliers", OPT_SUPPLIER, OPT_SUPPLY_PRODUCT)
            return await self.async_step_feed_in()
        catalog = await self._async_catalog()
        products = _products(catalog.get("suppliers"), self._opts.get(OPT_SUPPLIER))
        return self.async_show_form(
            step_id="supply_product",
            data_schema=vol.Schema(
                {
                    vol.Optional(OPT_SUPPLY_PRODUCT, description=_suggested(self.config_entry.options.get(OPT_SUPPLY_PRODUCT))): self._company_selector(products),
                }
            ),
            description_placeholders={"company": str(self._opts.get(OPT_SUPPLIER))},
        )

    # --- Schritt 4: Einspeisung ---------------------------------------------------

    async def async_step_feed_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        catalog = await self._async_catalog()
        buyers = catalog.get("feedInBuyers")
        if user_input is not None:
            self._take(FEED_IN_FIELDS, user_input)
            if buyers and user_input.get(OPT_BUYER):
                return await self.async_step_feed_in_product()
            return self._finish()

        opts = self._opts
        schema: dict[Any, Any] = {
            vol.Optional(OPT_BUYER, description=_suggested(opts.get(OPT_BUYER))): self._company_selector(_names(buyers)),
        }
        if not buyers:
            schema[vol.Optional(OPT_FEED_IN_PRODUCT, description=_suggested(opts.get(OPT_FEED_IN_PRODUCT)))] = TextSelector()
        schema[vol.Optional(OPT_FEED_IN_TARIFF, default=opts.get(OPT_FEED_IN_TARIFF) or "unknown")] = self._tariff_selector()
        return self.async_show_form(step_id="feed_in", data_schema=vol.Schema(schema))

    async def async_step_feed_in_product(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[OPT_FEED_IN_PRODUCT] = user_input.get(OPT_FEED_IN_PRODUCT)
            self._fill_tariff(OPT_FEED_IN_TARIFF, "feedInBuyers", OPT_BUYER, OPT_FEED_IN_PRODUCT)
            return self._finish()
        catalog = await self._async_catalog()
        products = _products(catalog.get("feedInBuyers"), self._opts.get(OPT_BUYER))
        return self.async_show_form(
            step_id="feed_in_product",
            data_schema=vol.Schema(
                {
                    vol.Optional(OPT_FEED_IN_PRODUCT, description=_suggested(self.config_entry.options.get(OPT_FEED_IN_PRODUCT))): self._company_selector(products),
                }
            ),
            description_placeholders={"company": str(self._opts.get(OPT_BUYER))},
        )

    def _fill_tariff(self, tariff_key: str, section: str, company_key: str, product_key: str) -> None:
        """Weiß das Mitglied die Tarifart nicht, sie aber der Katalog, wird sie übernommen."""
        if self._data.get(tariff_key) not in (None, "unknown"):
            return
        known = _catalog_tariff(
            (self._catalog or {}).get(section), self._data.get(company_key), self._data.get(product_key)
        )
        if known:
            self._data[tariff_key] = known

    # --- Abschluss ----------------------------------------------------------------

    def _finish(self) -> ConfigFlowResult:
        data = self._opts
        if data.get(OPT_REPORT):
            # Zeitpunkt der Einwilligung: beim ersten Einschalten, danach unverändert.
            if not self.config_entry.options.get(OPT_REPORT) or not data.get(OPT_CONSENT_AT):
                data[OPT_CONSENT_AT] = dt_util.utcnow().isoformat()
        else:
            data[OPT_CONSENT_AT] = None
        return self.async_create_entry(data=data)
