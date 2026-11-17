"""Install or remove the evidence provider's three HTTP capability manifests."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

TOOLS = ("read_session", "evidence_schema", "query_evidence")


def register(directory, endpoint, remove=False):
    base = urlsplit(endpoint)
    if base.scheme not in ("http", "https") or not base.hostname or base.username or base.password or base.query or base.fragment:
        raise ValueError("provider URL must be http(s), without credentials, query or fragment")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    templates = Path(__file__).resolve().parents[1] / "evidence" / "tools"
    prepared = []
    for tool in TOOLS:
        target = directory / ("evidence." + tool + ".json")
        if target.exists() and json.loads(target.read_text(encoding="utf-8"))["id"] != tool:
            raise ValueError("target contains a different capability: " + target.name)
        manifest = json.loads((templates / (tool + ".json")).read_text(encoding="utf-8"))
        manifest["endpoint"] = endpoint.rstrip("/") + "/invoke?tool=" + tool
        prepared.append((target, manifest))
    for target, manifest in prepared:
        if remove:
            target.unlink(missing_ok=True)
            continue
        descriptor, name = tempfile.mkstemp(prefix=".evidence-", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)
    return {"action": "removed" if remove else "installed", "tools": list(TOOLS)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="/plugins")
    parser.add_argument("--endpoint", default="http://evidence:8125")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    print(json.dumps(register(args.directory, args.endpoint, args.remove)))
