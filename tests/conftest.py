"""Test fixtures."""

from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """recorder is a manifest dependency, so every setup needs the mocked one.

    Autouse fixtures resolve before a test's own arguments, which is what
    recorder_mock needs: it has to be in place before the hass fixture starts.
    """
    yield
