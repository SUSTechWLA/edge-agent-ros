"""Generate the local, runtime-only deploy/robot ref from an immutable Git source tree."""
import argparse
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

REF = "refs/heads/deploy/robot"
ROOT = Path(__file__).resolve().parents[1]
DIRS = ("src/edge_core/", "src/edge_interfaces/", "agent/", "client/", "perception/", "console/",
        "sessions_plugin/", "evidence/", "model_provider/", "calibration/", "exploration/",
        "contracts/", "policies/")
FILES = {"compose.hardware.yaml", "compose.hardware-observe.yaml", "compose.sensor-health.yaml",
         "compose.camera.yaml", "compose.camera-hardware.yaml", "compose.rgbd.yaml",
         "compose.rgbd-hardware.yaml", "compose.model-provider.yaml", "compose.model-provider-hardware.yaml",
         "compose.model-provider-cloud.yaml", "docker/entrypoint.sh", "scripts/deploy.sh", "scripts/robot.sh",
         ".dockerignore", "LICENSE"}
RUNTIME_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".cpp", ".hpp", ".h", ".c", ".action",
                    ".srv", ".msg", ".xml", ".js", ".css", ".html", ".svg", ".png", ".wasm"}


def git(*args, data=None, env=None):
    return subprocess.check_output(["git", *args], cwd=ROOT, input=data, env=env)


def entries(rev):
    result = {}
    for line in git("ls-tree", "-rz", rev).split(b"\0"):
        if line:
            item, name = line.split(b"\t", 1)
            mode, kind, oid = item.decode().split()
            if kind == "blob":
                result[name.decode()] = (mode, oid)
    return result


def projection(source):
    selected = {}
    for name, entry in entries(source).items():
        path = PurePosixPath(name)
        if name.startswith("deploy/robot/"):
            name = name.removeprefix("deploy/robot/")
        elif name not in FILES:
            if not name.startswith(DIRS) or path.suffix in {".md", ".pyc"} or path.name.startswith("test_"):
                continue
            if "tests" in path.parts or name == "exploration/ros_sim.py":
                continue
            if any(p.startswith(".env") for p in path.parts):
                raise ValueError("unexpected runtime entry: " + name)
            if path.suffix not in RUNTIME_SUFFIXES and path.name != "CMakeLists.txt":
                continue
        if (entry[0] not in {"100644","100755"} or name in selected or
                any(p.startswith(".env") for p in path.parts)):
            raise ValueError("unexpected runtime entry: " + name)
        selected[name] = entry
    required = {"README.md","scripts/robot.sh","compose.hardware.yaml","calibration/factory.py","exploration/frontier.py"}
    if required - selected.keys():
        raise ValueError("incomplete source: " + str(sorted(required - selected.keys())))
    return selected


def publish(revision):
    source = git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
    selected = projection(source)
    lookup = subprocess.run(["git","rev-parse","--verify","--quiet",REF],cwd=ROOT,capture_output=True)
    if lookup.returncode not in (0,1):
        raise ValueError("cannot inspect deployment ref")
    old = lookup.stdout.decode().strip()
    if old:
        previous = entries(old)
        pointer = previous.pop("ROBOT_SOURCE", None)
        if not pointer:
            raise ValueError("existing deployment ref has no recorded source")
        prior = git("cat-file","blob",pointer[1]).decode().strip()
        prior_tree = git("rev-parse", old+"^{tree}").decode().strip()
        prior_message = git("show","-s","--format=%B",old).decode().strip()
        if prior_message != f"build(robot): deploy runtime from {prior}\n\nRobot-Tree: {prior_tree}":
            raise ValueError("deployment branch contains independent edits; reconcile in the source branch")
        if selected == previous:
            return old
        if subprocess.run(["git","merge-base","--is-ancestor",prior,source],cwd=ROOT).returncode:
            raise ValueError("release source diverged from the previous release")
    if ("branch " + REF).encode() in git("worktree","list","--porcelain").splitlines():
        raise ValueError("deployment ref is checked out; first detach that worktree at its current commit")
    pointer = git("hash-object","-w","--stdin",data=(source+"\n").encode()).decode().strip()
    selected["ROBOT_SOURCE"] = ("100644",pointer)
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp)/"index"))
        lines = [f"{mode} {oid}\t{name}\0".encode() for name,(mode,oid) in sorted(selected.items())]
        git("update-index","-z","--index-info",data=b"".join(lines),env=env)
        tree = git("write-tree",env=env).decode().strip()
    args = ["-p",old] if old else []
    commit = git("commit-tree",tree,*args,"-m","build(robot): deploy runtime from "+source,
                 "-m","Robot-Tree: "+tree).decode().strip()
    git("update-ref",REF,commit,old or "0"*len(source))
    return commit


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("revision", nargs="?", default="HEAD")
    try:
        print(publish(cli.parse_args().revision))
    except (ValueError, subprocess.CalledProcessError) as exc:
        cli.exit(1, str(exc)+"\n")
