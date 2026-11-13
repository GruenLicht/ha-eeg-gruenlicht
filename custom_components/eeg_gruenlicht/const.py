"""Konstanten der EEG-GrünLicht-Integration."""

from datetime import timedelta

DOMAIN = "eeg_gruenlicht"
VERSION = "0.3.0"

DEFAULT_BASE_URL = "https://api.eeg-gruenlicht.at/api"
PORTAL_URL = "https://abrechnung.eeg-gruenlicht.at"

# So erscheint das Plugin in der Geräteliste des Mitgliederportals.
DEVICE_LABEL = f"Home Assistant (EEG GrünLicht {VERSION})"

CONF_BASE_URL = "base_url"
CONF_CODE = "code"
CONF_SESSION_TOKEN = "session_token"
CONF_PRIVATE_KEY = "private_key"
CONF_MEMBER_ID = "member_id"

# Option: Viertelstundenwerte als Langzeitstatistik importieren (Stufe 2).
CONF_IMPORT_ENERGY = "import_energy"
IMPORT_INTERVAL = timedelta(hours=12)

# /api/me wird alle 30 Minuten abgefragt. Das hält die Sitzung aktiv
# (Leerlauf-Ablauf serverseitig 30 Tage) und zeigt der EEG „zuletzt aktiv".
UPDATE_INTERVAL = timedelta(minutes=30)

# Pairing-Status: die API drosselt auf 120 Abfragen je 5 Minuten und IP,
# alle 3 Sekunden bleibt deutlich darunter.
PAIR_POLL_SECONDS = 3
