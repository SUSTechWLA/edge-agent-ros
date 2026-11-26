"""SDD-049 AC-5/6. Run in an isolated Linux ROS domain with the Agent port free."""
import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import runpy
import socket
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/plugin-conformance"
PYTHON = [sys.executable, "-B"]
AGENT = "http://127.0.0.1:8090"
AGENT_COMMAND = [*PYTHON, str(ROOT / "agent/service.py")]
TASK = "wp5-status"
RID = "status-observation"
OPENER = build_opener(ProxyHandler({}))


def request(url, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, body, {"Content-Type": "application/json"})
    with OPENER.open(req, timeout=3) as reply:
        return json.load(reply)


def check(ok, message):
    if not ok:
        raise AssertionError(message)


def wait(inspect, description, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = inspect()
            if value:
                return value
        except (URLError, TimeoutError):
            pass
        time.sleep(.08)
    raise TimeoutError(description)


def vacant(port=0):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        return listener.getsockname()[1]


@contextmanager
def running(command, env, ready):
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            def started():
                if proc.poll() is not None:
                    raise RuntimeError(f"fixture process exited with status {proc.returncode}: {command}")
                return request(ready)
            wait(started, ready)
            yield
        except Exception:
            log.seek(max(0, log.seek(0, os.SEEK_END) - 4000))
            print(log.read().decode(errors="replace"), file=sys.stderr)
            raise
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def agent_environment(directory):
    plugins = directory / "plugins"
    plugins.mkdir(parents=True)
    return dict(
        os.environ, EDGE_AGENT_BIND="127.0.0.1", EDGE_TASK_DIR=str(directory / "state"),
        EDGE_PLUGIN_DIR=str(plugins), EDGE_EXTRA_PLUGIN_DIR="", EDGE_CONTRACT_DIR=str(directory / "contracts"),
        EDGE_SOFT_SOCKET="", EDGE_PHYSICAL_SOCKET="", EDGE_RETRY_LIMIT="0", EDGE_PLANNER_URL="", EDGE_OLLAMA_URL="",
    )


def submit(key, text, expected):
    result = request(AGENT+"/agent/tasks", {"id":key,"text":text})
    check(result["id"] == key, "task acceptance identity")
    def terminal():
        row = request(AGENT+"/agent/tasks?id="+key)
        return row if row["state"] not in ("PLANNING","WAITING","RUNNING") else None
    row = wait(terminal,key)
    check(row["state"] == expected, (key, row["state"], row["detail"]))
    dispatch = [e["operation_id"] for e in row["events"] if e["kind"]=="dispatch"]
    check(dispatch == [key+"/step/1"], (key, dispatch))
    step, = row["steps"]
    check(step["operation_id"] == dispatch[0] and step["tool"] == "status", "wrong step binding")
    if expected == "SUCCEEDED":
        evidence = step["evidence"]
        check(all(evidence[k]==v for k,v in {"source":"/odom","frame":"odom","x":1.25,"y":-2.5,"speed":.125}.items()),
              "native status evidence differs from published ROS sample")
    else:
        errors = [json.loads(e["payload"])["diagnostic"]["code"] for e in row["events"] if e["kind"]=="retry_exhausted"]
        check(errors == ["tool.odom_stale"], errors)
    return {"state": row["state"], "operation_id": step["operation_id"], "dispatch_count": len(dispatch)}


def observe_agent(marker):
    """Run the actual Agent main. Observe its existing ROS executor without replacing callbacks."""
    import rclpy
    sys.path.insert(0, str(ROOT / "agent"))
    from planning import Unavailable

    def observe():
        # The parent signals after receiving a real HTTP receipt from this Agent.
        while not marker.with_suffix(".requested").exists():
            time.sleep(.05)
        executor = rclpy.get_global_executor()
        while rclpy.ok():
            for node in executor.get_nodes():
                if node.get_name() != "edge_language_agent":
                    continue
                try:
                    sample = node.odometry()
                except Unavailable:
                    continue
                pending = marker.with_suffix(".pending")
                write_json(pending, sample)
                pending.replace(marker)
                return
            time.sleep(.04)

    Thread(target=observe, daemon=True).start()
    sys.argv[:] = [AGENT_COMMAND[-1]]
    runpy.run_path(AGENT_COMMAND[-1], run_name="__main__")


@contextmanager
def streaming(sensor, stamp):
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data

    publisher = sensor.create_publisher(Odometry, "/odom", qos_profile_sensor_data)
    msg = Odometry()
    msg.header.stamp.sec, msg.header.frame_id = stamp, "odom"
    msg.pose.pose.position.x, msg.pose.pose.position.y = 1.25, -2.5
    msg.pose.pose.orientation.w, msg.twist.twist.linear.x = 1., .125
    done = Event()

    def broadcast():
        while not done.is_set():
            msg.header.stamp.nanosec += 1
            publisher.publish(msg)
            done.wait(.04)

    thread = Thread(target=broadcast)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()
        sensor.destroy_publisher(publisher)


def run_tasks(env, recorder, sensor, stamp):
    marker = Path(env["EDGE_TASK_DIR"]).parent / "observed-odometry.json"
    cmd = [*PYTHON, str(Path(__file__).resolve()), "--observe-odometry", str(marker)]
    with running(cmd, env, AGENT+"/agent/capabilities"):
        catalog = request(AGENT+"/agent/capabilities")["records"]
        check(any(r["id"]=="task.record" and r["source"]=="v1_service" for r in catalog) == bool(recorder),
              "recorder registration disagrees with test deployment")
        rows = {"wp5-missing":submit("wp5-missing","status","RECOVERY_REQUIRED")}
        # A DDS graph match can outlive the previous Agent. Require a receipt from the new Robot.
        with streaming(sensor, stamp):
            marker.with_suffix(".requested").touch()
            wait(marker.is_file, "new Agent receiving this publisher's ROS odometry")
            sample = json.loads(marker.read_bytes())
            check(sample["source"] == "/odom" and sample["stamp"]["sec"] == stamp, sample)
            rows[TASK] = submit(TASK, "报告状态", "SUCCEEDED")
            rows["wp5-pose"] = submit("wp5-pose", "请查询一下机器人当前位置", "SUCCEEDED")
        if recorder:
            subprocess.run([*PYTHON,"-m","task_recorder.cli","--recorder",recorder,"watch",RID,
                            "--agent",AGENT,"--finish","--timeout","12"], check=True, env=env, cwd=ROOT,
                           timeout=18, stdout=subprocess.DEVNULL)
    return rows, sample


def independent_counts(directory):
    with closing(sqlite3.connect((directory/"state/tasks.db").as_uri()+"?mode=ro", uri=True)) as db:
        return dict(db.execute("SELECT operation_id,count(*) FROM events WHERE kind='dispatch' GROUP BY operation_id"))


def cli(root, label, manifest, url, command, env):
    casefile = FIXTURES / (label + ".cases.json")
    out = root / (label + "-report.json")
    argv = [*PYTHON, "-m", "edge_contracts.conformance", "--manifest", str(manifest), "--url", url,
            "--cases", str(casefile), "--out", str(out), "--process", *command]
    run = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=100)
    report = json.loads(out.read_bytes()) if out.exists() else {}
    failures = [c.get("failure") for c in report.get("cases", []) if not c["passed"]]
    for name in ("health", "version", "invalid_arguments", "timeout", "lifecycle"):
        outcome = report.get("checks", {}).get(name, {})
        if outcome.get("status") != "passed":
            failures.append((name, outcome.get("failure", "not run")))
    check(run.returncode == 0 and report.get("all_checks_passed") and not failures,
          (label, report.get("failure"), failures, run.stderr[-1400:]))
    return report


def run(root, result):
    import rclpy

    vacant(8090)
    online, offline = root/"with-recorder", root/"without-recorder"
    online_env, offline_env = agent_environment(online), agent_environment(offline)
    recorder_port = vacant()
    rec = f"http://127.0.0.1:{recorder_port}"
    state = root/"recordings"
    rec_command = [*PYTHON,"-m","task_recorder.service","--port",str(recorder_port),"--state",str(state)]
    samples = result["native_odometry_readiness"] = {}
    rclpy.init()
    sensor = rclpy.create_node("wp5_synthetic_odometry")
    try:
        with running(rec_command,online_env,rec+"/health"):
            subprocess.run([*PYTHON,"-m","task_recorder.manage","registry","ensure",rec,
                            "--root",online_env["EDGE_PLUGIN_DIR"]],check=True,cwd=ROOT,env=online_env,
                           stdout=subprocess.DEVNULL,timeout=10)
            # Only this lifecycle fixture writes a recording. The generic probe uses GET.
            request(rec+"/recordings/start",{"recording_id":RID,"task_id":TASK,"sources":["agent_state","agent_trace"]})
            result["recorder_running"], samples["recorder_running"] = run_tasks(online_env, rec, sensor, stamp=17)
            summary = request(rec+"/recordings/"+RID)
            check(summary["status"]=="FINISHED" and summary["task_status"]=="SUCCEEDED" and
                  all(summary["counts"].get(name,0)>0 for name in ("agent_trace","agent_state")),
                  "the production recorder did not archive task evidence")
            result["recording"] = {k:summary[k] for k in ("recording_id","task_id","status","task_status","counts")}
        vacant(recorder_port)
        result["recorder_stopped"], samples["recorder_stopped"] = run_tasks(offline_env, None, sensor, stamp=18)
    finally:
        sensor.destroy_node()
        rclpy.shutdown()
    expected = {key+"/step/1":1 for key in ("wp5-missing",TASK,"wp5-pose")}
    check(result["recorder_running"] == result["recorder_stopped"], "task terminal states or operations changed")
    check(independent_counts(online)==independent_counts(offline)==expected,"SQLite legacy dispatch counts differ")
    result["sqlite_dispatch_counts"] = expected

    result["recorder_probe"] = cli(root, "recorder", FIXTURES / "recorder.manifest.json", rec, rec_command, online_env)
    result["status_probe"] = cli(root, "status", FIXTURES / "status.manifest.json", AGENT, AGENT_COMMAND, online_env)
    check(independent_counts(online)==expected,"status reads or process restart redispatched an old operation")
    ep = vacant()
    env = dict(online_env,EDGE_EVIDENCE_BIND="127.0.0.1",EDGE_EVIDENCE_PORT=str(ep),
               EDGE_EVIDENCE_JOURNAL=str(root/"evidence.sqlite"))
    result["evidence_probe"] = cli(
        root, "evidence", ROOT / "evidence/tools/evidence_schema.json",
        f"http://127.0.0.1:{ep}/invoke?tool=evidence_schema", [*PYTHON, "-m", "evidence.provider"], env,
    )
    with closing(sqlite3.connect((root/"evidence.sqlite").as_uri()+"?mode=ro", uri=True)) as db:
        op = result["evidence_probe"]["cases"][0]["operation_id"]
        count, = db.execute("SELECT count(*) FROM evidence_requests WHERE operation_id=? AND receipt IS NOT NULL",(op,)).fetchone()
        check(count==1,"evidence replay did not retain one persisted terminal receipt")
        result["evidence_persisted_receipts_for_replayed_operation"] = count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",required=True,type=Path)
    args = parser.parse_args()
    result = {"passed":False,"environment":"Linux isolated ROS domain; synthetic odometry; production HTTP and SQLite"}
    try:
        with tempfile.TemporaryDirectory(prefix="edge-wp5-") as folder:
            run(Path(folder),result)
        result["passed"] = True
    except Exception as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        args.out.parent.mkdir(parents=True,exist_ok=True)
        write_json(args.out, result)
    print(args.out)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--observe-odometry":
        observe_agent(Path(sys.argv[2]))
    else:
        main()
