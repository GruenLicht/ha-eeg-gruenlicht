"""Ende-zu-Ende: Einrichtung, Sensoren, Trennen im Portal, Entfernen."""

import base64
import json

from homeassistant import config_entries
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.eeg_gruenlicht.const import DOMAIN

BASE = "https://api.eeg-gruenlicht.at/api"
MEMBER = {
    "id": "m-123",
    "name": "Christoph Test",
    "membershipNo": "0815",
    "community": "Grünes Licht Bad Vöslau",
    "communityId": "437a846f",
    "role": "Prosumer",
    "meterPoints": [
        {"number": "AT0030000000000000000000000000001", "direction": "consumption"},
        {"number": "AT0030000000000000000000000000002", "direction": "feedin"},
    ],
}


def _claims(proof: str) -> tuple[dict, dict]:
    def dec(part):
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

    h, p, _ = proof.split(".")
    return dec(h), dec(p)


def _mock_pairing(aioclient_mock, status="confirmed"):
    aioclient_mock.post(
        f"{BASE}/app/pair/start",
        json={"pairingId": "p1", "pairingCode": "482913", "expiresIn": 120},
    )
    body = {"status": status}
    if status == "confirmed":
        body.update(sessionToken="tok-abc", member=MEMBER)
    aioclient_mock.get(f"{BASE}/app/pair/status/p1", json=body)
    # Stufe 2: noch keine Viertelstundenwerte vorhanden.
    aioclient_mock.get(f"{BASE}/energy", json={"buckets": [], "coverage": {}})


async def _run_flow(hass: HomeAssistant, code="k7f2-9qrm"):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": code})
    if result["type"] is not FlowResultType.SHOW_PROGRESS:
        return result
    assert result["description_placeholders"]["pairing_code"] == "482913"
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_pairing_creates_entry_and_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    _mock_pairing(aioclient_mock)
    aioclient_mock.get(f"{BASE}/me", json=MEMBER)

    result = await _run_flow(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "EEG GrünLicht – Christoph Test"
    assert result["data"]["session_token"] == "tok-abc"
    assert "BEGIN PRIVATE KEY" in result["data"]["private_key"]
    await hass.async_block_till_done()

    # Pairing-Start: Code normalisiert, Gerätename gesetzt, DPoP ohne ath.
    start = next(c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/app/pair/start"))
    body = start[2]
    assert body["code"] == "K7F29QRM"
    assert body["deviceLabel"].startswith("Home Assistant")
    header, claims = _claims(start[3]["DPoP"])
    assert header["jwk"] == body["deviceKey"]
    assert claims["htm"] == "POST" and claims["htu"] == f"{BASE}/app/pair/start"
    assert "ath" not in claims

    # /me: Session-Token im Authorization-Header, DPoP mit ath.
    me = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/me")][-1]
    assert me[3]["Authorization"] == "DPoP tok-abc"
    _, me_claims = _claims(me[3]["DPoP"])
    assert me_claims["htm"] == "GET" and "ath" in me_claims

    assert hass.states.get("sensor.eeg_grunlicht_energy_community").state == "Grünes Licht Bad Vöslau"
    assert hass.states.get("sensor.eeg_grunlicht_membership_number").state == "0815"
    points = hass.states.get("sensor.eeg_grunlicht_meter_points")
    assert points.state == "2"
    assert points.attributes["meter_points"][1]["direction"] == "feedin"
    assert hass.states.get("binary_sensor.eeg_grunlicht_connection").state == STATE_ON


async def test_invalid_format_stays_on_form(hass: HomeAssistant, aioclient_mock) -> None:
    result = await _run_flow(hass, code="abc")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"code": "invalid_format"}
    assert aioclient_mock.call_count == 0


async def test_unknown_code(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(f"{BASE}/app/pair/start", status=401, json={"error": "unauthorized"})
    result = await _run_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_code"}


async def test_rate_limited(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(f"{BASE}/app/pair/start", status=429, json={})
    result = await _run_flow(hass)
    assert result["errors"] == {"base": "too_many_attempts"}


async def test_rejected_in_portal(hass: HomeAssistant, aioclient_mock) -> None:
    _mock_pairing(aioclient_mock, status="rejected")
    result = await _run_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_rejected"


async def test_session_revoked_starts_reauth_and_remove_logs_out(
    hass: HomeAssistant, aioclient_mock
) -> None:
    _mock_pairing(aioclient_mock)
    aioclient_mock.get(f"{BASE}/me", json=MEMBER)
    result = await _run_flow(hass)
    entry = result["result"]
    await hass.async_block_till_done()

    # Im Portal getrennt: /me antwortet 401 -> Verbindung aus, Reauth-Dialog.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/me", status=401, json={})
    aioclient_mock.delete(f"{BASE}/session", status=204)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.eeg_grunlicht_connection").state == STATE_OFF
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in flows)

    # Entfernen meldet die Sitzung ab.
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert any(c[0] == "DELETE" for c in aioclient_mock.mock_calls)
