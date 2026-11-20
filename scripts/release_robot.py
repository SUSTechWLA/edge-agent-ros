"""Generate the local, runtime-only deploy/robot ref from an immutable Git source tree."""
import argparse
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

REF = "refs/heads/deploy/robot"
ROOT = Path(__file__).resolve().parents[1]
# agent/ includes the kernel and its independent reader. Providers also need
# edge_contracts/ on the board. analysis/ runs on exported copies at the workstation.
DIRS = ("src/edge_core/", "src/edge_interfaces/", "agent/", "edge_contracts/", "client/", "perception/", "console/",
        "sessions_plugin/", "evidence/", "model_provider/", "calibration/", "exploration/",
        "contracts/", "policies/", "task_recorder/", "navigation/")
FILES = {"compose.hardware.yaml", "compose.hardware-observe.yaml", "compose.sensor-health.yaml",
         "compose.camera.yaml", "compose.camera-hardware.yaml", "compose.rgbd.yaml",
         "compose.rgbd-hardware.yaml", "compose.model-provider.yaml", "compose.model-provider-hardware.yaml",
         "compose.model-provider-cloud.yaml", "docker/entrypoint.sh", "scripts/deploy.sh", "scripts/robot.sh",
         "compose.task-recorder.yaml", "scripts/record-task.sh", "scripts/record-task.ps1", ".dockerignore", "LICENSE"}
FILES.update({"compose.soft-runtime.yaml","compose.soft-runtime-hardware.yaml","compose.physical-camera.yaml",
              "scripts/prepare_soft_pilots.py","scripts/prepare_physical_camera.py","scripts/robot-plugins.sh","scripts/check_board_plugins.py",
              "capabilities/soft-pilots/pack.json","capabilities/camera/pack.json",
              "profiles/bench.json","profiles/bench-camera.json","profiles/observe.json"})
FILES.update({"scripts/prepare_navigation.py", "scripts/navigation_odometry_bench.py", "profiles/navigation-bench.json", "profiles/navigation-live.json",
              "capabilities/navigation-common/pack.json", "capabilities/spatial-replay/pack.json",
              "capabilities/spatial-published/pack.json", "compose.navigation-readonly.yaml",
              "compose.navigation-camera-info.yaml", "compose.navigation-camera-image.yaml",
             "compose.navigation-camera-depth.yaml", "compose.navigation-livox.yaml", "compose.stationary-perception.yaml",
             "capabilities/perception-static/pack.json", "capabilities/perception-markup/pack.json",
             "examples/stationary-perception/jetson-rover.json", "scripts/prepare_perception_site.py",
             "scripts/evolve_read_projection.sh", "scripts/evolve_read_projection.py",
             "scripts/verify_perception_candidate.py", "scripts/run_object_model_isolated.sh",
             "compose.object-perception.yaml", "examples/object-perception/jetson-rover.json",
             "capabilities/perception-objects/pack.json", "compose.frame-query.yaml", "capabilities/perception-queries/pack.json"})
RUNTIME_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".cpp", ".hpp", ".h", ".c", ".action",
                    ".srv", ".msg", ".xml", ".js", ".css", ".html", ".svg", ".png", ".wasm"}
BOARD_SCAFFOLD = {".gitattributes", ".gitignore", "map-inputs/.gitkeep", "observations/.gitkeep",
                  "plugins/derived/.gitkeep"}
FORMAT_TRAILER = "Robot-Format: runtime-only-v2"


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


def projection(source, *, legacy=False):
    selected = {}
    for name, entry in entries(source).items():
        path = PurePosixPath(name)
        if name.startswith("deploy/robot/"):
            name = name.removeprefix("deploy/robot/")
            if any(p.startswith(".env") for p in path.parts):
                raise ValueError("unexpected runtime entry: " + name)
            if not legacy and name not in BOARD_SCAFFOLD:
                continue
        elif name not in FILES:
            if not name.startswith(DIRS) or path.suffix in {".md", ".pyc"} or path.name.startswith("test_"):
                continue
            if "tests" in path.parts or name == "exploration/ros_sim.py":
                continue
            if not legacy and (any(p.lower() in {"docs", "doc", "manuals"} for p in path.parts)
                               or path.stem.lower() == "readme"):
                continue
            if any(p.startswith(".env") for p in path.parts):
                raise ValueError("unexpected runtime entry: " + name)
            if path.suffix not in RUNTIME_SUFFIXES and path.name != "CMakeLists.txt":
                continue
        if (entry[0] not in {"100644","100755"} or name in selected or
                any(p.startswith(".env") for p in path.parts)):
            raise ValueError("unexpected runtime entry: " + name)
        selected[name] = entry
    required = {"scripts/robot.sh","compose.hardware.yaml","calibration/factory.py","exploration/frontier.py"}
    if legacy:
        required.add("README.md")
    if required - selected.keys():
        raise ValueError("incomplete source: " + str(sorted(required - selected.keys())))
    return selected


def legacy_format(deployment, source):
    tree = git("rev-parse", deployment+"^{tree}").decode().strip()
    message = git("show", "-s", "--format=%B", deployment).decode().strip()
    original = f"build(robot): deploy runtime from {source}\n\nRobot-Tree: {tree}"
    if message == original:
        return True
    if message == original + "\n" + FORMAT_TRAILER:
        return False
    raise ValueError("deployment branch contains independent edits; reconcile in the source branch")


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
        legacy = legacy_format(old, prior)
        if not legacy and source == prior and selected == previous:
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
                 "-m","Robot-Tree: "+tree+"\n"+FORMAT_TRAILER).decode().strip()
    git("update-ref",REF,commit,old or "0"*len(source))
    return commit


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("revision", nargs="?", default="HEAD")
    try:
        print(publish(cli.parse_args().revision))
    except (ValueError, subprocess.CalledProcessError) as exc:
        cli.exit(1, str(exc)+"\n")
