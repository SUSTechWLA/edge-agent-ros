"""Correlate an accepted alternate campus route with its original ROS scan/pose/dynamic recordings."""
import argparse
from bisect import bisect_right
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message
from exploration.corridor_review import sample_review
from exploration.survey_ros import Rover, ns


def audit(directory, task):
    mission=json.loads((directory/f"{task}.terminal.json").read_bytes())["mission"]
    bag,=list((directory/"recovery-rosbag").glob("*.db3"))
    db=sqlite3.connect(bag.resolve().as_uri()+"?immutable=1",uri=True)
    topics={i:(name,get_message(kind)) for i,name,kind in db.execute("select id,name,type from topics")}
    capture=SimpleNamespace(view=None,raw_scans=[],pose_samples=[],dynamic_samples=[])
    clocks, events = [], []
    for topic,stamp,data in db.execute("select topic_id,timestamp,data from messages order by timestamp,id"):
        name,kind=topics[topic]
        msg=deserialize_message(data,kind)
        if name=="/scan":
            Rover.on_scan(capture,msg)
        elif name=="/edge/perception/pose":
            Rover.on_pose(capture,msg)
        elif name=="/edge/perception/dynamic_obstacles":
            Rover.on_dynamic(capture,msg)
        elif name=="/clock":
            clocks.append((stamp,ns(msg.clock)))
        elif name=="/edge/events":
            e=json.loads(msg.data)
            if e.get("module")=="edge.gateway" and str(e.get("operation_id","")).startswith(task+"/"):
                events.append((stamp,e))
    db.close()
    scans={r["stamp_ns"]:r for r in capture.raw_scans}
    poses={r["stamp_ns"]:r for r in capture.pose_samples}
    release=[]
    ticks=[w for w,t in clocks]
    for e in mission["events"]:
        proof=e["data"]
        if e["kind"]!="dynamic_resumed" or not proof.get("original_tracks_clear"):
            continue
        paired=sorted((d for d in capture.dynamic_samples if
                       d.get("scan_ref","").startswith(proof["gateway_boot_id"]+"-") and
                       d["stamp_ns"] in scans and d["odom_stamp_ns"] in poses and
                       d["stamp_ns"]<=proof["last_stamp_ns"]),key=lambda d:d["scan_seq"])
        tail=paired[-proof["clear_samples"]:]
        assert len(tail)>=5 and tail[-1]["stamp_ns"]==proof["last_stamp_ns"]
        first,last=tail[0]["stamp_ns"],tail[-1]["stamp_ns"]
        assert last-first>=650_000_000 and (last-first)//1_000_000==proof["clear_span_ms"]
        assert all(b["scan_seq"]==a["scan_seq"]+1 and b["odom_stamp_ns"]>a["odom_stamp_ns"] and
                   0<b["stamp_ns"]-a["stamp_ns"]<=200_000_000 for a,b in zip(tail,tail[1:]))
        rows=[]
        for d in tail:
            sample={"scan":scans[d["stamp_ns"]],"pose":poses[d["odom_stamp_ns"]],"dynamic":d}
            gap,reason=sample_review(sample,proof["goal"],proof["previous_goal"])
            assert reason=="clear_observation" and gap>=.55,(reason,gap)
            rows.append(dict(scan_ref=d["scan_ref"],scan_seq=d["scan_seq"],scan_stamp_ns=d["stamp_ns"],
                             odom_stamp_ns=d["odom_stamp_ns"],clearance_m=gap))
        start,= [(w,n) for w,n in events if n["kind"]=="action.started" and
                 n["operation_id"].startswith(proof["next_operation_id"]+"/")]
        tick=bisect_right(ticks,start[0])-1
        assert tick>=0
        age=clocks[tick][1]-last
        assert 0<=age<=300_000_000,(age,start)
        release.append(dict(previous_goal=proof["previous_goal"],goal=proof["goal"],bound_map=proof["map_ref"],
                            parent_operation=proof["task_operation_id"],next_operation=proof["next_operation_id"],
                            gateway_action_event=start[1]["event_id"],samples=rows,span_ms=(last-first)/1e6,
                            min_clearance_m=min(x["clearance_m"] for x in rows),latest_scan_age_ms_at_gateway=age/1e6))
    return dict(task=task,recorded_samples={"scans":len(scans),"poses":len(poses),"dynamic":len(capture.dynamic_samples)},
                alternate_releases=release)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--id",required=True)
    args=parser.parse_args()
    result=audit(args.out,args.id)
    (args.out/"recovery-packets.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"task":args.id,"alternate_releases":len(result["alternate_releases"])}))
