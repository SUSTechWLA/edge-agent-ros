"""Drive back through the portal, then leave again using only the ordinary Agent API."""
import math


def verify(api, observed, await_fact, terminal, original, report):
    session = api("/api/state")["session_id"]
    report.update(session=session, first_outbound_task=original["task_id"], legs=[])
    points = ((3.3,0.),(0.,0.),(3.3,0.))
    requests = (("回到教学楼门厅","teaching_lobby"),("前往教学楼门外","teaching_exit"))
    for leg,((text,destination),start,goal) in enumerate(zip(requests,points,points[1:]),1):
        before = observed()
        for source in ("truth","odom"):
            assert math.dist(before[source][-1][1:3],start)<.20, f"{source} was not at the previous destination"
        key = original["task_id"]+"-continuation-"+str(leg)
        row = {"task_id":key,"text":text,"destination_id":destination}
        report["legs"].append(row)
        api("/agent/tasks",{"id":key,"text":text,"trigger":{"kind":"user","source":"campus-audit","id":key}})
        task = await_fact(lambda:terminal(key),f"continuous campus leg {leg} completion",120)
        row.update(state=task["state"],trace_id=task.get("trace_id"),detail=task.get("detail"))
        if task["state"]!="SUCCEEDED":
            row["steps"]=task.get("steps",[])
            row["events"]=api("/api/events?task_id="+key+"&limit=150")["events"]
        assert task["state"]=="SUCCEEDED",f"{text}: {task['state']} {task.get('detail')}"
        step = task["steps"][0]
        evidence = step["evidence"]
        assert step["tool"]=="navigate_place" and evidence["destination_id"]==destination
        assert evidence["verified_count"]==len(evidence["waypoints"])>0
        assert evidence["digest"]==original["evidence"]["digest"],"scene changed inside the continuous mission sequence"
        def parked():
            current=observed()
            for source in ("truth","odom"):
                final=current[source][-3:]
                if len(final)!=3 or any(r[0]<=before[source][-1][0] or math.dist(r[1:3],goal)>=.20 or abs(r[3])>=.08 for r in final):
                    return None
            return current
        end=await_fact(parked,f"continuous leg {leg} ground truth and wheel odometry at rest",20)
        paths={}
        for source in ("truth","odom"):
            trail=[r for r in end[source] if r[0]>=before[source][-1][0]]
            crossings=[r for r in trail if abs(r[1]-1.6)<.16 and abs(r[2])<.8]
            assert crossings and math.dist(start,trail[0][1:3])<.20,f"{source} missed the physical passage in leg {leg}"
            travelled=sum(math.dist(a[1:3],b[1:3]) for a,b in zip(trail,trail[1:]))
            assert travelled>2.8, f"{source} missed most of the motion"
            paths[source]={"samples":len(trail),"start":trail[0],"gate":crossings[len(crossings)//2],
                           "stop":trail[-1],"distance_m":round(travelled,3),"goal_error_m":round(math.dist(trail[-1][1:3],goal),4)}
        row.update(tool=step["tool"],scene_digest=evidence["digest"],operation=evidence["mission_id"],
                   physical=paths,waypoints=evidence["waypoints"],place=evidence["place"])
    assert api("/api/state")["session_id"]==session,"the simulation or Agent restarted during the round trip"
    trace_ids=[original["trace_id"],*(row["trace_id"] for row in report["legs"])]
    assert all(trace_ids) and len(set(trace_ids))==3,"three auditable requests are required"
    report["continuation_ground_truth_m"]=round(sum(row["physical"]["truth"]["distance_m"] for row in report["legs"]),3)
    report["restored_destination"]="teaching_exit"
