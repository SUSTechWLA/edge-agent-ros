"""Operator-only installation and local admission control for the task.record Service."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import uuid

from .cli import endpoint


def registry(root, url, action):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
    import plugin_manager

    name = "recorder.default"
    expected = {"schema_version": 1, "id": name, "version": "1", "endpoint": url,
                "provides": ["task.record"], "requires": [], "draining": action == "drain"}
    old, revision = plugin_manager.RuntimeRelease.installed(root / "runtime", name)
    if old and {**old, "draining": expected["draining"]} != expected:
        raise ValueError("existing recorder.default belongs to another configuration")
    unchanged = (old is None and action in ("remove", "drain")) or (old is not None and action == "ensure")
    receipt = {"action": "unchanged", "service": "task.record"}
    if unchanged or (action != "remove" and old == expected):
        return receipt
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / (name + ".json")
        path.write_text(json.dumps(expected), "utf8")
        op = "recorder-" + uuid.uuid4().hex
        request = argparse.Namespace(kind="runtime", root=root, id=name, definition=path,
                                     action="remove" if action=="remove" else "publish", expect=revision,
                                     cases=None, trace_id=op, parent_event_id=None, request_id=op)
        try:
            return plugin_manager.change(request)
        except plugin_manager.release.ReleaseRejected as error:
            # A concurrent first start can win the same managed descriptor publication.
            current, _ = plugin_manager.RuntimeRelease.installed(root / "runtime", name)
            if error.code == "version_conflict" and action == "ensure" and current == expected:
                return receipt
            raise


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    sub=cli.add_subparsers(dest="command",required=True)
    reg=sub.add_parser("registry")
    reg.add_argument("action",choices=("ensure","enable","drain","remove"))
    reg.add_argument("endpoint")
    reg.add_argument("--root",type=Path,default=Path("/plugins"))
    adm=sub.add_parser("admission")
    adm.add_argument("action",choices=("drain","enable","empty","state"))
    args=cli.parse_args()
    if args.command=="registry":
        result=registry(args.root,args.endpoint,args.action)
    else:
        url="http://127.0.0.1:8188"
        result=endpoint(url,"/health") if args.action in ("state","empty") else endpoint(url,"/admin/"+args.action,{})
        if args.action=="empty" and (not result["draining"] or result["active_count"]):
            cli.exit(2,"Recorder draining; finish the active recordings before disabling.\n")
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
