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

from .api import (
    DeviceKey,
    GruenLichtClient,
    GruenLichtConnectionError,
    GruenLichtError,
    GruenLichtPairingDenied,
    GruenLichtRateLimited,
    normalize_code,
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


class GruenLichtOptionsFlow(OptionsFlow):
    """Freigaben je Stufe ein- und ausschalten."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_IMPORT_ENERGY, True)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_IMPORT_ENERGY, default=current): bool}),
        )
