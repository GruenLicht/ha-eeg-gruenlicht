"""Gemeinsame Test-Fixtures."""

from unittest.mock import patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Custom Integrations laden; der Recorder muss vor hass stehen (Stufe 2)."""
    yield


@pytest.fixture(autouse=True)
def fast_poll():
    """Pairing-Status ohne Wartezeit abfragen."""
    with (
        patch("custom_components.eeg_gruenlicht.config_flow.PAIR_POLL_SECONDS", 0),
        patch("custom_components.eeg_gruenlicht.statistics.REQUEST_PAUSE", 0),
    ):
        yield
