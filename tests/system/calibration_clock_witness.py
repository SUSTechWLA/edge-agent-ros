"""Audit a browser-submitted Gazebo clock-calibration trial independently of the solver."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

from calibration_clock_fixture import INJECTION_NS


def docker(service, *args, body=None):
    return subprocess.check_output(["docker","exec","-i","edge-board-cal-"+service+"-1",*args],
                                   input=body)


def request(path, body=None):
    script = ("import sys,urllib.request as u; data=sys.stdin.buffer.read(); "
              "print(u.urlopen(u.Request('http://127.0.0.1:8141'+sys.argv[1],"
              "data=data or None, headers={'Content-Type':'application/json'}),timeout=10).read().decode())")
    return json.loads(docker("calibration-session","python3","-c",script,path,
                             body=json.dumps(body).encode() if body is not None else None))


def run(folder, proof, mode):
    task = json.loads((folder/"task.json").read_bytes())
    spatial = json.loads((folder/"spatial.json").read_bytes())
    status = request("/clock-status")
    assert status["environment"] == "simulation" and spatial["before"] == spatial["after"]
    assert status["task_id"] == task["id"] and task["text"] == "请为当前两台 MID360 与 D435 自主标定软件时间偏移。"
    capture = next(s["evidence"] for s in task["steps"]
                   if s["tool"]=="record_clock_motion" and s["status"]=="SUCCEEDED")
    digest = capture["archive_sha256"]
    archive = docker("calibration-session","cat",
                     f"/var/lib/edge-calibration/clock-captures/{task['id']}/{digest}.json")
    assert hashlib.sha256(archive).hexdigest() == digest
    traces = json.loads(archive)["traces"]
    assert all(len(rows)>=24 for rows in traces.values()) and len(traces)==3
    relay = json.loads(docker("cal-clock-relay","cat","/tmp/clock-relay-audit.json"))
    assert all(row["injected_ns"] - row["original_ns"] == INJECTION_NS[k] for k,row in relay.items())
    motion = docker("cal-sim","/usr/local/bin/edge-entrypoint","ros2","topic","info","/cmd_vel").decode()
    assert "Publisher count: 0" in motion
    steps = [{"tool":s["tool"],"operation_id":s["operation_id"],"status":s["status"]} for s in task["steps"]]
    result = dict(task_id=task["id"],text=task["text"],state=task["state"],model=task["provider"],
                  steps=steps,task_retries=task["retries"],capture_sha256=digest,spatial=spatial,
                  clock_state=status["state"],motion_publishers=0,sample_counts=capture["sample_counts"],
                  injected_stamp_shifts_ns=INJECTION_NS, live_relay_witness=relay)
    if mode == "moving":
        assert task["state"]=="SUCCEEDED" and status["state"]=="VERIFIED"
        assert status["spatial_revision"]==spatial["after"] and status["capture_sha256"]==digest
        result.update(clock_revision=status["revision"], epoch=status["clock_epoch"], sources={})
        ids=("robot_id","environment","profile_sha256","spatial_revision","clock_epoch","revision")
        cam_stamps={r["stamp_ns"] for r in traces["camera"]}
        for k,s in status["sources"].items():
            expected = INJECTION_NS["camera"]-INJECTION_NS[k]
            err = abs(s["offset_ns"]-expected)
            assert err <= min(20_000_000,s["uncertainty_ns"])
            middle = sum(s["valid_raw_ns"])//2
            lidar_stamp=next(t for t in sorted((r["stamp_ns"] for r in traces[k]),key=lambda t:abs(t-middle))
                             if t+expected in cam_stamps)
            pair = {**{name:status[name] for name in ids},"source":k,"lidar_stamp_ns":lidar_stamp,
                    "camera_stamp_ns":lidar_stamp+expected,"budget_ns":25_000_000,
                    "lidar_radius_ns":0,"camera_radius_ns":0}
            good = request("/assess-clock-pair",pair)
            out = request("/assess-clock-pair",{**pair,"lidar_stamp_ns":s["valid_raw_ns"][1]+100_000_000})
            far = request("/assess-clock-pair",{**pair,"camera_stamp_ns":pair["camera_stamp_ns"]+50_000_000})
            assert good["accepted"] and not out["accepted"] and not far["accepted"]
            result["sources"][k]=dict(expected_offset_ns=expected, absolute_error_ns=err, **s,
                                      scheduled_pair=pair, observed_pair=good,
                                      outside_window=out, excessive_skew=far)
    else:
        assert task["state"] in ("FAILED","BLOCKED") and status["state"]!="VERIFIED"
        assert "excitation" in status["message"],status
        result["reason"]=status["message"]
    proof.mkdir(parents=True,exist_ok=True)
    (proof/(mode+"-planes.json.gz")).write_bytes(gzip.compress(archive,mtime=0))
    (proof/(mode+".json")).write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({"task_id":task["id"],"case":mode,"state":task["state"],"passed":True}))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("browser_output",type=Path)
    parser.add_argument("proof",type=Path)
    parser.add_argument("mode",choices=("moving","static"))
    args=parser.parse_args()
    run(args.browser_output,args.proof,args.mode)
