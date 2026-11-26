"""Verify a deployed v1 HTTP tool against the production registry and the original receipt fixtures."""
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.kernel.catalog import registry
from edge_contracts.conformance import main


def registered(manifest):
    if not isinstance(manifest,dict) or manifest.get("effect") != "read" or not manifest.get("endpoint"):
        raise ValueError("a read-only HTTP tool manifest is required")
    # Isolate discovery from the host's other deployments and exercise the actual loader.
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder)/"provider.json").write_text(json.dumps(manifest,allow_nan=False),encoding="utf-8")
        with patch.dict(os.environ, {"EDGE_PLUGIN_DIR":folder, "EDGE_EXTRA_PLUGIN_DIR":""}), redirect_stdout(sys.stderr):
            cap = registry(legacy_only=True).get(manifest.get("id"))
    if cap is None or cap != manifest:
        raise ValueError("the production registry did not register this manifest in full")
    return cap


if __name__ == "__main__":
    sys.exit(main(registrar=registered))
