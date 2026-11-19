"""Install the default Session service through the existing audited plugin manager."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import plugin_manager
from runtime_plugins import ServiceSnapshot


def register(directory, endpoint="http://session:8126/session", remove=False, if_missing=False):
    root = Path(directory)
    name = "session.default"
    if if_missing:
        owners = ServiceSnapshot(root / "runtime", observing=True).owners.get("session.read", [])
        if owners:
            return {"action":"unchanged","service":"session.read","owners":sorted(owners)}
    installed, revision = plugin_manager.RuntimeRelease.installed(root / "runtime", name)
    if remove and installed is None:
        return {"action":"unchanged", "plugin_id":name, "state":"absent"}
    descriptor = {"schema_version":1, "id":name, "version":"1", "endpoint":endpoint,
                  "provides":["session.read"], "requires":[], "draining":False}
    if not remove and installed == descriptor:
        return {"action":"unchanged", "plugin_id":name, "state":"installed"}
    request = "session-install-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="edge-session-") as folder:
        definition = Path(folder) / (name + ".json")
        definition.write_text(json.dumps(descriptor), encoding="utf-8")
        return plugin_manager.change(argparse.Namespace(
            action="remove" if remove else "publish",kind="runtime",root=root,
            id=name,definition=definition,cases=None,expect=revision,
            trace_id=request,parent_event_id=None,request_id=request))


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory",default="/plugins")
    parser.add_argument("--endpoint",default="http://session:8126/session")
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument("--remove",action="store_true")
    mode.add_argument("--if-missing",action="store_true",help="Preserve an installed session.read binding, including draining providers")
    args=parser.parse_args()
    print(json.dumps(register(args.directory,args.endpoint,args.remove,args.if_missing)))
