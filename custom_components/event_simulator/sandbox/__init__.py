"""Replay sandbox that runs in a child process.

Nothing here runs in the live Home Assistant. Everything below this package runs only in a separate child process (`runner.py` starts it) that boots its own throwaway `HomeAssistant` in a temporary config dir, with a stripped environment and a scratch working directory. That is why the engine may patch Home Assistant's internals (the service registry, trace store, restore-state dump, time functions; each marked at its use site): it only ever patches its own instance, so a core refactor can at worst make a replay fail with an error, never touch live state. The time patching follows Home Assistant's own test harness (core's `tests/patch_time.py` and `tests/common.py`, shipped as `pytest_homeassistant_custom_component.patch_time` / `.common`); see `vclock.py`.

Modules here must stay importable on their own: no imports of the parent integration package and no relative imports reaching above this package.

The live side starts a replay with `child_command()`: it runs this directory as a script (`python <sandbox dir>`), so the child never needs `custom_components` to be importable. `__main__.py` then loads this directory as a private package and hands stdin/stdout to `engine.run_scenario`.
"""

from __future__ import annotations

import os
import sys

SANDBOX_DIR = os.path.dirname(os.path.abspath(__file__))


def child_command(python: str | None = None) -> list[str]:
    """The argv that starts one replay child: scenario JSON on its stdin, result JSON on its stdout.

    `python` defaults to `sys.executable`, i.e. the interpreter running Home Assistant, so the child imports the same `homeassistant` package as the live instance.
    """
    return [python or sys.executable, SANDBOX_DIR]
