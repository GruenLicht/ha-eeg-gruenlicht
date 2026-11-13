"""Client für die GrünLicht-API mit DPoP-gebundenen Sitzungen (RFC 9449).

Das Plugin verhält sich gegenüber der API wie ein weiteres Handy:
eigener P-256-Geräteschlüssel, Pairing per Kurzcode mit Bestätigung im
Portal, danach ein opaker Sitzungs-Token, der nur zusammen mit einem
frischen DPoP-Nachweis dieses Schlüssels gilt.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Crockford-Base32 wie in der API (PairingCode.normalize).
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class GruenLichtError(Exception):
    """Basisfehler."""


class GruenLichtConnectionError(GruenLichtError):
    """API nicht erreichbar, Zeitüberschreitung oder 5xx."""


class GruenLichtAuthError(GruenLichtError):
    """Sitzung ungültig, widerrufen oder abgelaufen (401/403)."""


class GruenLichtPairingDenied(GruenLichtError):
    """Kurzcode unbekannt, abgelaufen oder schon verwendet."""


class GruenLichtRateLimited(GruenLichtError):
    """Zu viele Versuche (429)."""


def b64u(data: bytes) -> str:
    """base64url ohne Padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _compact_json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def normalize_code(raw: str) -> str | None:
    """Kurzcode so normalisieren, wie es die API tut, oder None."""
    code = re.sub(r"[^0-9A-Z]", "", (raw or "").upper())
    code = code.translate(str.maketrans("OIL", "011"))
    if len(code) != 8 or any(c not in _CODE_ALPHABET for c in code):
        return None
    return code


def htu(url: str) -> str:
    """Absolute URL ohne Query und Fragment, Standardport weggelassen."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port
    default = (parts.scheme == "https" and port in (None, 443)) or (
        parts.scheme == "http" and port in (None, 80)
    )
    netloc = host if default else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}"


class DeviceKey:
    """P-256-Geräteschlüssel des Plugins."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self._key = private_key
        numbers = private_key.public_key().public_numbers()
        self.jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": b64u(numbers.x.to_bytes(32, "big")),
            "y": b64u(numbers.y.to_bytes(32, "big")),
        }

    @classmethod
    def generate(cls) -> DeviceKey:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: str) -> DeviceKey:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("kein EC-Schlüssel")
        return cls(key)

    def to_pem(self) -> str:
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

    @property
    def thumbprint(self) -> str:
        """JWK-Thumbprint nach RFC 7638 (wie EcKey.thumbprint in der API)."""
        canonical = {k: self.jwk[k] for k in ("crv", "kty", "x", "y")}
        return b64u(hashlib.sha256(_compact_json(canonical)).digest())

    def proof(self, method: str, url: str, access_token: str | None = None) -> str:
        """Frischen DPoP-Nachweis für genau diesen Request bauen."""
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.jwk}
        payload: dict[str, Any] = {
            "htm": method.upper(),
            "htu": htu(url),
            "iat": int(time.time()),
            "jti": b64u(secrets.token_bytes(32)),
        }
        if access_token is not None:
            payload["ath"] = b64u(hashlib.sha256(access_token.encode("utf-8")).digest())

        signing_input = f"{b64u(_compact_json(header))}.{b64u(_compact_json(payload))}"
        der = self._key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{signing_input}.{b64u(signature)}"


class GruenLichtClient:
    """Dünner Client für die Endpunkte, die Stufe 1 braucht."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        key: DeviceKey,
        session_token: str | None = None,
    ) -> None:
        self._http = session
        self._base = base_url.rstrip("/")
        self._key = key
        self.session_token = session_token

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None, authed: bool = True
    ) -> tuple[int, Any]:
        url = f"{self._base}{path}"
        token = self.session_token if authed else None
        headers = {
            "Accept": "application/json",
            "DPoP": self._key.proof(method, url, token),
        }
        if token:
            headers["Authorization"] = f"DPoP {token}"
        try:
            async with self._http.request(
                method, url, json=body, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status >= 500:
                    raise GruenLichtConnectionError(f"HTTP {resp.status}")
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = None
                return resp.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise GruenLichtConnectionError(str(err)) from err

    async def start_pairing(self, code: str, device_label: str) -> dict[str, Any]:
        """POST /app/pair/start mit dem Kurzcode aus dem Portal."""
        status, data = await self._request(
            "POST",
            "/app/pair/start",
            {"code": code, "deviceKey": self._key.jwk, "deviceLabel": device_label},
            authed=False,
        )
        if status == 200 and isinstance(data, dict):
            return data
        if status == 429:
            raise GruenLichtRateLimited
        if status in (401, 403, 409):
            raise GruenLichtPairingDenied
        raise GruenLichtConnectionError(f"HTTP {status}")

    async def pairing_status(self, pairing_id: str) -> dict[str, Any]:
        """GET /app/pair/status/:id — awaiting | confirmed | rejected | expired."""
        status, data = await self._request(
            "GET", f"/app/pair/status/{pairing_id}", authed=False
        )
        if status == 200 and isinstance(data, dict):
            return data
        if status == 429:
            raise GruenLichtRateLimited
        if status in (401, 403):
            raise GruenLichtPairingDenied
        raise GruenLichtConnectionError(f"HTTP {status}")

    async def me(self) -> dict[str, Any]:
        """GET /me — Name, Mitgliedsnummer, EEG, Zählpunkte."""
        status, data = await self._request("GET", "/me")
        if status == 200 and isinstance(data, dict):
            return data
        if status in (401, 403):
            raise GruenLichtAuthError
        raise GruenLichtConnectionError(f"HTTP {status}")

    async def energy_day(self, day: str) -> dict[str, Any]:
        """GET /energy?granularity=day — Viertelstundenwerte eines Wiener Tages.

        Summiert über alle Zählpunkte des Mitglieds. Die Antwort enthält
        außerdem `coverage` (erster und letzter Tag mit Daten).
        """
        status, data = await self._request("GET", f"/energy?granularity=day&date={day}")
        if status == 200 and isinstance(data, dict):
            return data
        if status in (401, 403):
            raise GruenLichtAuthError
        raise GruenLichtConnectionError(f"HTTP {status}")

    async def put_home_profile(self, profile: dict[str, Any]) -> None:
        """PUT /home-profile — Haushaltsprofil (Stufe 3) ersetzen."""
        status, _ = await self._request("PUT", "/home-profile", profile)
        if status in (401, 403):
            raise GruenLichtAuthError
        if status not in (200, 201, 204):
            raise GruenLichtConnectionError(f"HTTP {status}")

    async def delete_home_profile(self) -> None:
        """DELETE /home-profile — Freigabe entzogen, Profil löschen."""
        status, _ = await self._request("DELETE", "/home-profile")
        if status in (401, 403):
            raise GruenLichtAuthError
        if status not in (200, 204, 404):
            raise GruenLichtConnectionError(f"HTTP {status}")

    async def contract_catalog(self) -> dict[str, Any] | None:
        """GET /contract-catalog — Lieferanten, Abnehmer und Produkte zur Auswahl.

        Optional: Solange die API keinen Katalog anbietet (404) oder nicht
        erreichbar ist, fragt der Dialog freie Texte ab.
        """
        try:
            status, data = await self._request("GET", "/contract-catalog")
        except GruenLichtConnectionError:
            return None
        return data if status == 200 and isinstance(data, dict) else None

    async def logout(self) -> None:
        """DELETE /session — Sitzung sofort widerrufen."""
        status, _ = await self._request("DELETE", "/session")
        if status in (401, 403):
            raise GruenLichtAuthError
        if status not in (200, 204):
            raise GruenLichtConnectionError(f"HTTP {status}")
