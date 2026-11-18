"""Manage only the campus Compose project, independently of the workstation's active robot."""
import argparse
from contextlib import closing
import http.client
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
PROJECT = "edge-agent-campus"
SERVICES = ("sim", "bridge", "witness", "gateway", "core", "perception", "scene", "agent", "console",
            "campus-capture", "campus-overview","campus-robot-bridge","campus-fleet",
            "crazyflie-agent","go1-agent","g1-agent","session")
CHILDREN = ("crazyflie", "go1", "g1")


class ReloadBlocked(RuntimeError):
    pass


def read_local(port, path):
    # Resolve the port from this Compose project's running console. Ignore proxy settings.
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        client.request("GET", path)
        response = client.getresponse()
        if response.status != 200:
            raise ReloadBlocked(f"{path.partition('?')[0]} returned HTTP {response.status}")
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ReloadBlocked("campus status exceeds the read budget")
        return json.loads(body)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise ReloadBlocked("cannot verify campus task and motion status through the console") from exc
    finally:
        client.close()


def idle_stamp(get):
    tasks = get("/api/review/tasks?state=active&limit=1")
    try:
        count = tasks["counts"]["active"]
        rows = tasks["tasks"]
        if type(count) is not int or count < 0 or not isinstance(rows, list) or bool(count) != bool(rows):
            raise ValueError()
        if count:
            raise ReloadBlocked(f"{count} active campus task(s), including {rows[0]['id']!r} ({rows[0]['state']})")
        state = get("/api/state")
        if state["mode"] != "live" or state["environment"] != "simulation" or not state["ledger_id"]:
            raise ReloadBlocked("campus does not have a live simulation and core ledger")
        if state["sources"]["semantics"]["data"]["id"] != "campus_large":
            raise ReloadBlocked("the console is not observing the large campus")
        if state["active_operation"]:
            raise ReloadBlocked(f"campus action is still active: {state['active_operation']!r}")
        for mission in state["missions"]:
            if mission["state"] not in ("SUCCEEDED","FAILED","CANCELED"):
                raise ReloadBlocked(f"campus core mission {mission['mission_id']!r} requires attention ({mission['state']})")
        odom = state["sources"]["odom"]
        if odom["fresh"] is not True or not math.isfinite(odom["stamp_s"]) or odom["stamp_s"] <= 0:
            raise ReloadBlocked("waiting for fresh campus odometry")
        vx, vy, _ = odom["data"]["linear_velocity"]
        wz = odom["data"]["w"]
        if not all(math.isfinite(v) for v in (vx,vy,wz)):
            raise ValueError()
        if math.hypot(vx,vy) >= .025 or abs(wz) >= .05:
            raise ReloadBlocked("campus rover is still moving; wait for confirmed standstill")
        return odom["stamp_s"]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ReloadBlocked("incomplete campus task, ledger or velocity evidence") from exc


def child_work(root):
    """Inspect durable state even after agent ingress has closed. Never create databases."""
    pending = []
    for robot in CHILDREN:
        path = root/robot/"tasks.db"
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=2)) as db:
            row = db.execute("""SELECT id,state FROM tasks WHERE state IS NULL OR
                state NOT IN ('SUCCEEDED','FAILED','CANCELED','BLOCKED') ORDER BY rowid LIMIT 1""").fetchone()
        if row:
            pending.append(f"{robot} task {row[0]!r} ({row[1]})")
    with closing(sqlite3.connect((root/"fleet"/"operations.sqlite").as_uri()+"?mode=ro",uri=True,timeout=2)) as db:
        row = db.execute("SELECT robot,op FROM receipts WHERE result IS NULL ORDER BY rowid LIMIT 1").fetchone()
    if row:
        pending.append(f"{row[0]} actuator operation {row[1]!r}")
    return pending


def guard_children(output):
    # The isolated stack may have a generated Compose project name. Resolve the
    # actual volume names rather than accidentally inspecting the user's live fleet.
    try:
        volumes = json.loads(output("config","--format","json"))["volumes"]
        mounts = []
        for source, target in [*((r+"-tasks",r) for r in CHILDREN),("campus-fleet-state","fleet")]:
            name = volumes[source]["name"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*",name):
                raise ValueError("invalid Compose volume name")
            mounts.extend(["--volume",f"{name}:/verify/{target}:ro"])
        data = output("run","--rm","--no-deps","-T","--entrypoint","python3",*mounts,"campus-build",
                      "-B","/opt/edge/scene/campus_large/run.py","--guard-child-ledgers")
        pending = json.loads(data)
        if not isinstance(pending,list) or not all(isinstance(item,str) for item in pending):
            raise ValueError("invalid child ledger response")
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
        raise ReloadBlocked("cannot verify the campus child task journals and outstanding actuator operations") from exc
    if pending:
        raise ReloadBlocked("unfinished campus work: "+", ".join(pending))


def reload_campus(run, output, containers, fetch=read_local, check_children=guard_children):
    # Compose includes project orphans here, such as optional ROS sensors from an extra overlay.
    members = {}
    for line in output("ps","--status","running","--format","{{.Service}} {{.ID}}").splitlines():
        service, container_id = line.split()
        if not re.fullmatch(r"[0-9a-f]{12,64}",container_id):
            raise ReloadBlocked("cannot verify a running campus container identity")
        members.setdefault(service, []).append(container_id)
    if "campus-build" in members:
        raise ReloadBlocked("a campus world build is already running")
    extras = [container_id for service, ids in members.items() if service not in SERVICES for container_id in ids]
    if members:
        if not {"console","agent","sim","core","gateway"}.issubset(members):
            raise ReloadBlocked("campus control stack is incomplete; use start to restore it before a guarded reload")
        address = output("port","console","8080").strip()
        binding = re.fullmatch(r"127\.0\.0\.1:(\d+)",address)
        if not binding:
            raise ReloadBlocked("cannot identify the running campus console's loopback port")
        port = int(binding[1])
        body_agents = tuple(r+"-agent" for r in CHILDREN)
        fleet = {"campus-fleet",*body_agents}
        # A fully legacy campus has no child journals. A partially stopped fleet
        # can still have pending work; stop early instead of treating it as legacy.
        installed = set(output("ps","--all","--format","{{.Service}}").split())
        has_children = bool(fleet & (installed | members.keys()))
        if not has_children:
            try:
                declared = output("exec","-T","sim","python3","-c",
                    "import json; print(json.dumps(bool(json.load(open('/campus/layout.json')).get('robots'))))")
                expected_fleet = json.loads(declared)
                if type(expected_fleet) is not bool:
                    raise ValueError()
                has_children = expected_fleet
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
                raise ReloadBlocked("cannot establish whether the running campus requires child robot agents") from exc
        if has_children and not fleet.issubset(members):
            raise ReloadBlocked("campus robot control stack is incomplete; restore it before a guarded reload")
        admission = ("agent",*body_agents) if has_children else ("agent",)
        def get(path):
            return fetch(port,path)
        previous = idle_stamp(get)
        if has_children:
            check_children(output)
        try:
            run("stop",*admission)
            if extras:
                containers("stop",*extras)
            deadline = time.monotonic() + 6
            while idle_stamp(get) <= previous:
                if time.monotonic() >= deadline:
                    raise ReloadBlocked("the campus clock did not advance to a second verified standstill sample")
                time.sleep(.15)
            if has_children:
                check_children(output)
        except BaseException:
            # Restore task admission on uncertainty; Gazebo and motion owners are still running.
            try:
                if extras:
                    containers("start",*extras)
            finally:
                run("start",*admission)
            raise
        run("stop",*(service for service in SERVICES if service not in admission and service in members))
    run("up","-d","--no-build",*SERVICES)
    if extras:
        containers("start",*extras)
    run("exec","-T","core","/usr/local/bin/edge-entrypoint","python3","/opt/edge/client/ready.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "map", "review", "status", "logs", "stop", "down", "reload"])
    parser.add_argument("--with-lidar",action="store_true",help="also start the campus fixed ROS point-cloud station with map")
    parser.add_argument("--image", default=os.getenv("EDGE_CAMPUS_IMAGE") or os.getenv("EDGE_IMAGE") or "edge-agent-ros:v0.1")
    parser.add_argument("--assets-dir", type=Path, default=os.getenv("EDGE_CAMPUS_ASSETS_DIR") or ROOT / "artifacts/sim-assets")
    parser.add_argument("--console-port", type=int, default=os.getenv("EDGE_CAMPUS_CONSOLE_PORT") or 8766)
    parser.add_argument("--overview-port", type=int, default=os.getenv("EDGE_CAMPUS_OVERVIEW_PORT") or 8780)
    parser.add_argument("--map-port", type=int, default=os.getenv("EDGE_CAMPUS_MAP_PORT") or 8773)
    args = parser.parse_args()
    if args.with_lidar and args.action != "map":
        parser.error("--with-lidar is used with the map action")
    ports = [args.console_port, args.overview_port, args.map_port]
    if len(set(ports)) != len(ports) or not all(1 <= port <= 65535 for port in ports):
        parser.error("select distinct TCP ports in the range 1 through 65535")
    assets = args.assets_dir.expanduser().resolve()
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("COMPOSE_", "EDGE_", "DEEPSEEK_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_IMAGE=args.image, EDGE_ROS_DOMAIN_ID="73",
               EDGE_CAMPUS_ASSETS_DIR=str(assets), EDGE_CAMPUS_CONSOLE_PORT=str(args.console_port),
               EDGE_CAMPUS_OVERVIEW_PORT=str(args.overview_port), EDGE_CAMPUS_MAP_PORT=str(args.map_port))
    compose = ["docker", "compose", "-p", PROJECT, "-f", "compose.yaml", "-f", "compose.campus.yaml"]
    def run(*params):
        subprocess.run([*compose, *params], cwd=ROOT, env=env, check=True)
    def output(*params):
        return subprocess.check_output([*compose,*params],cwd=ROOT,env=env,text=True,encoding="utf-8")
    def containers(action,*ids):
        subprocess.run(["docker",action,*ids],cwd=ROOT,env=env,check=True)

    if args.action in ("start", "reload"):
        if not (assets / "models" / "Pine Tree" / "model.sdf").is_file():
            parser.error(f"campus assets missing in {assets}. Run python scene/campus_large/fetch_assets.py and use its cache path.")
        if args.action == "reload":
            try:
                reload_campus(run,output,containers)
            except ReloadBlocked as exc:
                parser.exit(2, f"Campus reload blocked: {exc}\n")
        else:
            run("up", "-d", "--no-build", *SERVICES)
        print(f"Campus overview: http://127.0.0.1:{args.overview_port}/")
        print(f"Robot console: http://127.0.0.1:{args.console_port}/")
    elif args.action == "review":
        if not output("ps","--status","running","-q","console").strip():
            parser.error("campus console is offline; run campus start first")
        run("up","-d","--no-build","session")
        print("Campus task sessions: http://"+output("port","console","8080").strip()+"/#sessions")
    elif args.action == "map":
        if not output("ps", "--status", "running", "-q", "sim").strip():
            parser.error("campus simulation is offline; run campus start before installing its map")
        # The viewer pins its immutable texture pack at process start.
        run("up", "-d", "--no-build", "--force-recreate", "map3d")
        if args.with_lidar:
            run("up","-d","--no-deps","--no-build","map3d-sensor")
        print(f"Campus map: http://127.0.0.1:{args.map_port}/?view=2d")
    elif args.action == "status":
        run("ps", "-a")
    elif args.action == "logs":
        run("logs", "--tail", "60", "campus-build", "sim", "campus-capture", "campus-overview")
    elif args.action == "stop":
        run("stop", *SERVICES, "map3d", "map3d-sensor")
    else:
        run("down")


if __name__ == "__main__":
    if sys.argv[1:]==["--guard-child-ledgers"]:
        print(json.dumps(child_work(Path("/verify"))))
    else:
        main()
