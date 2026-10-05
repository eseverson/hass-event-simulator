"""Child-process entry point: one scenario JSON on stdin, one result JSON on stdout, logs on stderr.

Launched as `python <sandbox dir>` (see `child_command()`); `python -m custom_components.event_simulator.sandbox` works too when that package is importable.

Exit codes:
- 0: a result was written. It may carry a structured `error` (invalid scenario or automation); that is an answer, not a crash.
- 2: stdin was not a JSON object; an `invalid_scenario` error result is still written.
- 1: the engine crashed; an `internal_error` result is written when stdout is still usable, and the traceback goes to stderr.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import sys
import traceback
from typing import Any

# Package name the sandbox directory is loaded under when run as a script; private so it cannot clash with an installed top-level `sandbox`.
_PRIVATE_PACKAGE = "_event_simulator_sandbox"


def _load_package() -> str:
    """Import this directory as a package so the engine's relative imports work; return its name."""
    if __package__:
        return __package__
    here = os.path.dirname(os.path.abspath(__file__))
    # `python <dir>` puts the directory itself first on sys.path, which would let its modules shadow top-level names.
    if sys.path and os.path.abspath(sys.path[0] or os.curdir) == here:
        del sys.path[0]
    if _PRIVATE_PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PRIVATE_PACKAGE, os.path.join(here, "__init__.py"), submodule_search_locations=[here]
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PRIVATE_PACKAGE] = module
        spec.loader.exec_module(module)
    return _PRIVATE_PACKAGE


def _write(result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("EVENT_SIMULATOR_LOG_LEVEL", "WARNING").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    engine = importlib.import_module(f"{_load_package()}.engine")
    try:
        scenario = json.loads(sys.stdin.buffer.read())
    except (ValueError, UnicodeDecodeError) as err:
        _write(engine.error_result("invalid_scenario", f"stdin is not JSON: {err}"))
        return 2
    if not isinstance(scenario, dict):
        _write(engine.error_result("invalid_scenario", "The scenario must be a JSON object"))
        return 2
    try:
        result = engine.run_scenario(scenario)
    except Exception as err:  # noqa: BLE001 - the process boundary turns any crash into a result
        traceback.print_exc()
        _write(engine.error_result("internal_error", f"{type(err).__name__}: {err}"))
        return 1
    _write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
