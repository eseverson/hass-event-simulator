"""Replay sandbox that runs in a child process.

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
