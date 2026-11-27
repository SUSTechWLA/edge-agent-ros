"""Build from a clean deploy/robot clone and export the tested Linux ARM64 field kit."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(args, cwd=ROOT, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, stderr=subprocess.STDOUT)


def field_guide(tree, source):
    archived = tree / "README.md"
    if archived.is_file():
        return archived.read_bytes()
    return run(["git", "cat-file", "blob", source + ":deploy/robot/README.md"], cwd=ROOT)


def recording_smoke(tree, image, arch, env):
    # Exercise the same read-only recorder bind mount as the deployed Compose
    # profile. Loopback isolates the fixture from any live robot's ROS domain.
    with tempfile.TemporaryDirectory() as folder:
        override=Path(folder)/"isolated.json"
        override.write_text(json.dumps({"services":{"recorder-hardware-source":{"network_mode":"none"}}}),"utf8")
        container="edge-camera-smoke-"+uuid.uuid4().hex[:10]
        compose=["docker","compose","--project-name",container,
                 "-f","compose.hardware.yaml","-f","compose/task-recorder.yaml","-f",str(override)]
        spec=ROOT/"tests/system/recording_board_smoke.py"
        invocation=compose+["run","--rm","--name",container,"--no-deps","-T","-v",f"{spec}:/opt/edge/recording_board_smoke.py:ro",
                            "recorder-hardware-source","python3","-B","/opt/edge/recording_board_smoke.py"]
        fixture_env={**env,"EDGE_RECORDER_IMAGE":image}
        try:
            result=subprocess.run(invocation,cwd=tree,env=fixture_env,capture_output=True,text=True,timeout=100)
        finally:
            subprocess.run(["docker","rm","-f",container],capture_output=True)
        if result.returncode:
            raise RuntimeError("recording profile failed on "+arch+": "+(result.stderr+result.stdout)[-2500:])
        report=json.loads(result.stdout.splitlines()[-1])
        if not report["passed"] or report["machine"]!={"arm64":"aarch64","amd64":"x86_64"}[arch]:
            raise ValueError("invalid recording smoke identity for "+arch)
        return {**report,"image":image,"plugin_source":"deploy/robot task_recorder read-only Compose mount"}


def package(ref, output):
    deploy = run(["git","rev-parse","--verify",ref+"^{commit}"]).decode().strip()
    source = run(["git","show",deploy+":ROBOT_SOURCE"]).decode().strip()
    short = source[:12]
    output.mkdir(parents=True,exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".robot-kit-", dir=output))
    target = output/("rover-"+short)
    if target.exists():
        stage.rmdir()
        raise ValueError("a kit already exists at " + str(target))
    # Keep partial logs under artifacts when a build fails; publish only after both smokes.
    with tempfile.TemporaryDirectory(prefix="edge-robot-clone-") as tmp:
        tree = Path(tmp)/"robot"
        bundle = stage/"edge-agent-ros-robot.bundle"
        run(["git","bundle","create",str(bundle),"refs/heads/deploy/robot"])
        run(["git","clone","-q","--branch","deploy/robot","--single-branch",str(bundle),str(tree)])
        if run(["git","rev-parse","HEAD"],tree).decode().strip() != deploy:
            raise ValueError("bundle points at a different deployment; rerun on the current deployment")
        run(["git","bundle","verify",str(bundle)],tree)
        def build(arch):
            tag = "edge-agent-ros/hardware:robot-"+short+("-amd64" if arch=="amd64" else "")
            env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_","COMPOSE_"))}
            env.update(EDGE_REVISION=source, EDGE_HARDWARE_IMAGE=tag, DOCKER_DEFAULT_PLATFORM="linux/"+arch,
                       COMPOSE_DISABLE_ENV_FILE="1", EDGE_ROS_BASE=os.getenv("ROBOT_BASE_"+arch.upper(),
                       ("arm64v8/" if arch=="arm64" else "")+"ros:jazzy-ros-base"))
            with (stage/("build-"+arch+".log")).open("wb") as log:
                subprocess.run(["docker","compose","-p","edge-release-"+arch,"-f","compose.hardware.yaml","build","core"],
                               cwd=tree,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
            metadata=json.loads(run(["docker","image","inspect",tag]))[0]
            if metadata["Architecture"]!=arch or metadata["Config"]["Labels"]["org.opencontainers.image.revision"]!=source:
                raise ValueError("image identity mismatch for "+tag)
            run(["docker","run","--rm","--network","none","--platform","linux/"+arch,tag,"python3","-B","-c",
                 "from calibration import ros_capture, targets, factory; from exploration import frontier, map_artifact"])
            smoke=stage/("smoke-"+arch+".json")
            run([sys.executable,str(ROOT/"tests/system/board_smoke.py"),"--image",tag,
                 "--config-root",str(tree),"--out",str(smoke)])
            report=json.loads(smoke.read_bytes())
            if not report["passed"] or report["revision"]!=source:
                raise ValueError("board smoke did not verify "+tag)
            camera=recording_smoke(tree,tag,arch,env)
            (stage/("recording-"+arch+".json")).write_text(json.dumps(camera,indent=2)+"\n","utf8")
            return tag
        with ThreadPoolExecutor(max_workers=2) as pool:
            _, arm=pool.map(build,("amd64","arm64"))
        image = stage/"hardware-linux-arm64.tar.gz"
        with subprocess.Popen(["docker","image","save",arm],stdout=subprocess.PIPE) as proc:
            with gzip.open(image,"wb",compresslevel=1) as out:
                shutil.copyfileobj(proc.stdout,out,1024*1024)
            if proc.wait():
                raise RuntimeError("docker image save failed")
        run(["docker","load","-i",str(image)])
        (stage/"START-HERE.md").write_bytes(field_guide(tree, source))
        (stage/"release.json").write_text(json.dumps(dict(source=source,deployment=deploy,image=arm,
               bundle_complete=True,platform="linux/arm64",hardware_test="emulation on build host"),indent=2)+"\n","utf8")
        hashes=[]
        for path in sorted(stage.iterdir()):
            with path.open("rb") as stream:
                hashes.append(hashlib.file_digest(stream,"sha256").hexdigest()+"  "+path.name)
        (stage/"SHA256SUMS").write_text("\n".join(hashes)+"\n",encoding="ascii",newline="\n")
    stage.rename(target)
    return target


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"artifacts/deployment")
    args=parser.parse_args()
    try:
        print(package("refs/heads/deploy/robot",args.output.resolve()))
    except subprocess.CalledProcessError as error:
        sys.exit((error.output or b"Build failed; see the build logs in the new .robot-kit- directory.").decode(errors="replace")[-3000:])
