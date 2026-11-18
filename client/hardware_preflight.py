"""Read-only commissioning check against the running hardware console, using the console's own configuration."""
import argparse
import json
import math
import os
import platform
import re
import sys
import time
from urllib.request import ProxyHandler, build_opener

DEFAULTS = {"odom": ("/odom", "odom"), "imu": ("/imu", "imu_link"), "scan": ("/scan", "laser_link")}


def configuration(env):
    sources = {}
    for key, (default_topic, default_frame) in DEFAULTS.items():
        topic = env.get("EDGE_" + key.upper() + "_TOPIC", default_topic)
        frame = env.get("EDGE_" + key.upper() + "_FRAME", default_frame)
        if not re.fullmatch(r"(/[a-zA-Z_][a-zA-Z0-9_]*)+", topic):
            raise ValueError("invalid ROS topic " + key + ": " + topic)
        sources[key] = {"topic": topic, "frame": frame}
    domain = int(env.get("ROS_DOMAIN_ID", "72"))
    port = int(env.get("EDGE_HTTP_PORT", "8765"))
    robot = env.get("EDGE_ROBOT_ID", "hardware-edge")
    base = env.get("EDGE_BASE_FRAME", "base_link")
    if not (0 <= domain <= 232 and 1 <= port <= 65535 and robot.strip()):
        raise ValueError("check ROS_DOMAIN_ID, EDGE_CONSOLE_PORT and EDGE_ROBOT_ID")
    if not all(f and not f.startswith("/") and not any(c.isspace() for c in f)
               for f in [base, *(s["frame"] for s in sources.values())]):
        raise ValueError("expected nonempty TF frames without a leading slash or spaces")
    if len({s["topic"] for s in sources.values()}) != len(sources):
        raise ValueError("odometry, IMU and scan have different ROS types and need different topics")
    sources["odom"]["child_frame"] = base
    return {"robot_id":robot, "domain":domain, "port":port, "sources":sources}


def numeric(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def inspect(state, cfg, selected):
    issues, readings = [], {}
    if (state.get("environment") != "hardware" or state.get("robot_id") != cfg["robot_id"]
            or state.get("mode") != "live" or not state.get("session_id")):
        issues.append("console.identity_mismatch")
    for key in selected:
        expected = cfg["sources"][key]
        sample = state.get("sources", {}).get(key) or {}
        data = sample.get("data") or {}
        stamp, now = sample.get("stamp_s"), state.get("now")
        delta = round(now-stamp,5) if numeric(stamp) and numeric(now) else None
        readings[key] = {"topic":sample.get("topic"),"frame":data.get("frame"),"stamp_s":stamp,
                         "age_s":sample.get("age_s"),"wall_minus_stamp_s":delta}
        if key == "odom":
            readings[key]["child_frame"] = data.get("child_frame")
        if sample.get("topic") != expected["topic"]:
            issues.append(key + ".topic_mismatch")
        if not sample.get("available"):
            issues.append(key + ".missing")
            continue
        if not sample.get("fresh"):
            issues.append(key + ".stale")
        if data.get("frame") != expected["frame"] or (key == "odom" and data.get("child_frame") != expected["child_frame"]):
            issues.append(key + ".frame_mismatch")
        # Physical drivers must provide ROS system-time samples. The public
        # source's freshness TTL also defines how much acquisition lag is usable.
        ttl = sample.get("ttl_s")
        if delta is None or stamp <= 0 or not numeric(ttl) or not -.25 <= delta <= ttl:
            issues.append(key + ".clock_mismatch")
    return issues, readings


def check(cfg, selected, timeout):
    http = build_opener(ProxyHandler({}))
    url = f"http://127.0.0.1:{cfg['port']}/api/state"
    previous, session = {}, None
    report = {"passed":False, "architecture":platform.machine(), "revision":os.getenv("EDGE_REVISION","unknown"),
              "robot_id":cfg["robot_id"],"ros_domain_id":cfg["domain"],"expected":{k:cfg["sources"][k] for k in selected}}
    deadline = time.monotonic() + timeout
    while True:
        try:
            with http.open(url, timeout=min(2.,timeout)) as response:
                state=json.load(response)
            issues, readings = inspect(state,cfg,selected)
            current_session=state.get("session_id")
            report.update(session_id=current_session,task_proxy_mode=state.get("task_proxy_mode"),
                          samples=readings,checked_at=state.get("now"))
            if session and session != current_session:
                report["issues"]=["console.session_changed"]
                return report
            session = current_session
            if issues:
                previous = {}
            elif previous:
                issues=[key+".stamp_not_advancing" for key in selected
                        if readings[key]["stamp_s"] <= previous[key]]
                if not issues:
                    report.update(passed=True,issues=[],previous_stamps=previous)
                    return report
            else:
                previous = {k:r["stamp_s"] for k,r in readings.items()}
                issues=["samples.waiting_for_second_stamp"]
            report["issues"] = issues
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            report.update(issues=["console.unreadable"],detail=str(exc)[:180])
            previous = {}
        remaining = deadline-time.monotonic()
        if remaining<=0:
            return report
        time.sleep(min(.25,remaining))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources",nargs="*",help="required ROS inputs: odom imu scan (default all)")
    parser.add_argument("--timeout",type=float,default=6)
    parser.add_argument("--check-config",action="store_true")
    args=parser.parse_args()
    selected=args.sources or list(DEFAULTS)
    if set(selected)-set(DEFAULTS) or len(selected)!=len(set(selected)) or not .25<=args.timeout<=120:
        parser.error("supply unique odom/imu/scan inputs and --timeout in [0.25,120] seconds")
    try:
        cfg=configuration(os.environ)
    except ValueError as exc:
        print(json.dumps({"passed":False,"issues":["config.invalid"],"detail":str(exc)}))
        return 2
    report={"passed":True,"checked":"configuration"} if args.check_config else check(cfg,selected,args.timeout)
    print(json.dumps(report,allow_nan=False,ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__=="__main__":
    sys.exit(main())
