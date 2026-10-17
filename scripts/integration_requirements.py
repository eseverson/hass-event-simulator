"""Print the Python requirements an integration needs, one per line, for installing it next to the tests.

Home Assistant installs an integration's requirements when it loads it, but the sandbox runs with pip off and the tests run without Home Assistant's installer. This reads the installed core's manifests: the integration's own `requirements`, then those of every integration it lists under `dependencies` and `after_dependencies`, recursively.

    pip install $(python scripts/integration_requirements.py zha)
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import homeassistant.components


def requirements(domain: str) -> list[str]:
    """The requirements of `domain` and everything its manifest depends on, in first-seen order."""
    root = Path(homeassistant.components.__file__).parent
    seen: set[str] = set()
    found: dict[str, None] = {}
    pending = [domain]
    while pending:
        name = pending.pop(0)
        if name in seen:
            continue
        seen.add(name)
        manifest_path = root / name / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text())
        found.update(dict.fromkeys(manifest.get("requirements", [])))
        pending.extend(manifest.get("dependencies", []))
        pending.extend(manifest.get("after_dependencies", []))
    return list(found)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: integration_requirements.py <domain>")
    print("\n".join(requirements(sys.argv[1])))
