"""Test fixtures."""

from __future__ import annotations

import os

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """recorder is a manifest dependency, so every setup needs the mocked one.

    Autouse fixtures resolve before a test's own arguments, which is what
    recorder_mock needs: it has to be in place before the hass fixture starts.
    """
    yield


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "requires_zha: needs ZHA's Python packages (`scripts/integration_requirements.py zha`)")


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip a `requires_zha` test when ZHA's packages are missing; CI sets EVENT_SIMULATOR_REQUIRE_ZHA=1 so a missing install fails instead of passing quietly."""
    if item.get_closest_marker("requires_zha") is None:
        return
    try:
        import homeassistant.components.zha.device_trigger  # noqa: F401, PLC0415
    except ImportError as err:
        if os.environ.get("EVENT_SIMULATOR_REQUIRE_ZHA") == "1":
            pytest.fail(f"ZHA's packages are required here: {err}")
        pytest.skip(f"ZHA's packages are not installed: {err}")
