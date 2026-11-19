"""Manage an Edge tool or service descriptor without executing a provider or contacting ROS."""
import argparse
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys

import author_tool as release
from module_journal import ModuleJournal, canonical
import runtime_plugins as runtime


def descriptor(path):
    try:
        item = runtime._descriptor(path)
    except RecursionError as exc:
        raise ValueError("runtime manifest is too deeply nested") from exc
    item.setdefault("draining", False)
    return item


def revision(item):
    return hashlib.sha256(canonical(item).encode()).hexdigest()


class RuntimeRelease:
    module = "plugin.manager"
    id_field = "plugin_id"

    @staticmethod
    def check_id(name):
        if not isinstance(name,str) or not runtime.NAME.fullmatch(name):
            raise ValueError("invalid runtime plugin id")
        return name

    @staticmethod
    def candidate(args):
        if getattr(args, "cases", None) is not None:
            raise ValueError("runtime service checks use descriptor and dependency validation")
        item = descriptor(args.definition)
        return item, {"version":revision(item), "declared_version":item["version"]}

    @staticmethod
    def installed(directory, name):
        try:
            item = descriptor(directory / (name + ".json"))
        except FileNotFoundError:
            return None, "absent"
        return item, revision(item)

    @staticmethod
    def check_graph(directory, name, item):
        before = runtime.ServiceSnapshot(directory, observing=True)
        after = before.with_change(name, item)
        regressions = before.bindings.keys() & after.problems.keys()
        failed = name if name in after.problems else next(iter(sorted(regressions)), None)
        if failed:
            code, reason = after.problems[failed]
            rejection = "service_conflict" if code == "plugin.service_conflict" else "dependency_unavailable"
            raise release.ReleaseRejected(rejection, f"{failed}: {code}: {reason}")


# This registry is owned by the management process. A manifest cannot install a handler.
TYPES = {"derived":release.DerivedRelease, "runtime":RuntimeRelease}


def change(args):
    if args.kind == "v2":
        from v2_registry import command
        return command(args)
    policy = TYPES[args.kind]
    directory = Path(args.root) / args.kind
    request = argparse.Namespace(**{**vars(args), "registry":directory})
    if args.action == "check":
        item, evidence = policy.candidate(request)
        key = item["id"]
        if getattr(args, "expect", None) is not None:
            _previous, version = policy.installed(directory, key)
            if args.expect != version:
                raise release.ReleaseRejected("version_conflict", f"registry changed; current version is {version}")
        policy.check_graph(directory, key, item)
        return {"id":key, **evidence}
    if args.kind == "derived":
        return release.change(request)
    with release.writer_lock(directory), closing(ModuleJournal(directory/".history"/"module-events.sqlite")) as journal:
        return release.publish_managed(request, journal, policy)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("validate","check","publish","drain","remove"):
        cmd = actions.add_parser(action)
        new_only = action in ("validate","drain")
        read_only = action in ("validate","check")
        cmd.add_argument("--kind", choices=["v2"] if new_only else sorted([*TYPES,"v2"]), required=True)
        cmd.add_argument("--root", type=Path, required=action!="validate", help="parent of the managed registries")
        cmd.add_argument("--trust-root", type=Path, help="site-owned v2 catalog for this local admin process")
        cmd.add_argument("--expect", required=not read_only, help="installed CAS revision or absent")
        if action in ("drain","remove"):
            cmd.add_argument("id")
        else:
            cmd.add_argument("definition", type=Path)
            cmd.add_argument("--cases", type=Path, help="independent samples for derived tools")
        if not read_only:
            cmd.add_argument("--trace-id")
            cmd.add_argument("--parent-event-id")
            cmd.add_argument("--request-id", help="replay the receipt of the same logical change")
    args = parser.parse_args()
    try:
        sys.stdout.buffer.write(release.json_bytes(change(args)))
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError,OverflowError) as exc:
        message = str(exc)
        if args.kind == "v2" and hasattr(exc,"code") and not message.startswith(exc.code):
            message = f"{exc.code}: {message}"
        parser.exit(1, f"{message}\n")


if __name__ == "__main__":
    main()
