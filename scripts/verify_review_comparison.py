"""Independently replay sealed RGB-D pixels and audit four native task receipts. Standard library only."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import struct
import tempfile
import zlib


SHA = re.compile(r"sha256:[a-f0-9]{64}")
EXPECTED = ("review_workflow", "review_composed", "review_builtin", "review_workflow")
KINDS = {"review_builtin":"builtin", "review_workflow":"site_workflow", "review_composed":"agent_graph"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest(obj):
    return sha(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode())


def load(path):
    return json.loads(path.read_bytes())


def instant(value):
    result = datetime.fromisoformat(value.replace("Z","+00:00"))
    require(result.tzinfo is not None, "missing clock offset")
    return result


def close(actual, expected):
    return (actual is expected if expected is None else type(actual) in (int,float)
            and math.isclose(actual,expected,abs_tol=1e-12,rel_tol=0))


def raster(directory, frame, kind):
    ref = frame[kind+"_ref"]
    require(isinstance(ref,str) and SHA.fullmatch(ref), "invalid PNG reference")
    path = directory / (ref[7:]+".png")
    require(not path.is_symlink() and path.stat().st_size <= 4*1024*1024, "unexpected PNG path or size")
    png = path.read_bytes()
    require(sha(png)==ref and png.startswith(b"\x89PNG\r\n\x1a\n"), "PNG hash or signature mismatch")
    chunks = []
    offset = 8
    while offset < len(png):
        size,tag = struct.unpack_from(">I4s",png,offset)
        end = offset+8+size
        require(end+4<=len(png) and zlib.crc32(png[offset+4:end])==int.from_bytes(png[end:end+4],"big"),
                "invalid PNG chunk")
        chunks.append((tag,png[offset+8:end]))
        offset=end+4
    require(len(chunks)>=3 and chunks[0][0]==b"IHDR" and chunks[-1]==(b"IEND",b"")
            and all(k==b"IDAT" for k,_ in chunks[1:-1]),"unsupported PNG layout")
    w,h,bits,color,compression,filters,interlace = struct.unpack(">IIBBBBB",chunks[0][1])
    layout=(8,2) if kind=="rgb" else (16,0)
    require((w,h)==(frame["width"],frame["height"]) and 8<=w<=640 and 8<=h<=480
            and (bits,color)==layout and (compression,filters,interlace)==(0,0,0),"incorrect image geometry")
    stride=1+w*(3 if kind=="rgb" else 2)
    inflater=zlib.decompressobj()
    data=inflater.decompress(b"".join(buf for _,buf in chunks[1:-1]),stride*h+1)
    require(len(data)==stride*h and inflater.eof and not inflater.unused_data and not inflater.unconsumed_tail
            and not any(data[::stride]),"unsupported scanline filter or corrupt image")
    return data, stride


def measure(directory, frame, columns, limit):
    raster(directory,frame,"rgb")
    raw,stride=raster(directory,frame,"depth")
    w,h=frame["width"],frame["height"]
    scale=frame["depth_scale_m"]
    require(type(scale) in (float,int) and 0<scale<=.1 and columns<=w,"invalid depth scale or column count")
    require(0<=int(frame["depth_stamp_ns"]) and 0<=int(frame["rgb_stamp_ns"])
            and abs(int(frame["depth_stamp_ns"])-int(frame["rgb_stamp_ns"]))<=50_000_000,"unpaired frame timestamps")
    columns_data=[[] for _ in range(columns)]
    bounds=[w*i//columns for i in range(columns+1)]
    for y in range(h):
        row=struct.unpack_from(">"+str(w)+"H",raw,y*stride+1)
        for i,values in enumerate(columns_data):
            values.extend(x for x in row[bounds[i]:bounds[i+1]] if 0<x<65535)
    regions=[]
    for i,values in enumerate(columns_data):
        total=(bounds[i+1]-bounds[i])*h
        count=len(values)
        native=(statistics.quantiles(values,n=10,method="inclusive")[0] if count>=2
                else values[0] if count else None)
        depth=native*scale if native is not None else None
        state=("unknown" if count*5<total*3 or depth is None else
               "near_surface" if depth-limit<=1e-12 else "above_threshold")
        regions.append({"column":i+1,"bounds_xyxy":[bounds[i],0,bounds[i+1],h],"pixels":total,
                        "valid_pixels":count,"valid_fraction":count/total,"depth_m":depth,"state":state})
    return regions


def assert_pixels(facts, frame, ref, regions, limit):
    a,r=facts["analysis"],facts["review"]
    require(facts["frame_ref"]==a["frame_ref"]==ref and a.get("basis")=="sealed_rgbd_replay", "wrong frame binding")
    for key in ("rgb_ref","depth_ref","calibration_ref","rgb_source_id","depth_source_id","actor_ref","clock_id",
                "frame_id","rgb_stamp_ns","depth_stamp_ns","width","height","depth_scale_m"):
        require(str(a[key])==str(frame[key]),"source metadata mismatch: "+key)
    require(a["statistic"]==r["statistic"]=="p10" and a["unit"]=="m" and a["channel"]=="depth"
            and a["grid"]==[1,len(regions)] and r["column_count"]==len(regions) and r["distance_m"]==limit
            and r["scope"]=="camera_optical_depth_only" and r["order"]=="nearest_first","incorrect measurement semantics")
    require(len(a["regions"])==len(r["regions"])==len(regions),"wrong region count")
    for observed,reported,expected in zip(a["regions"],r["regions"],regions):
        require(all(observed[k]==expected[k] for k in ("pixels","valid_pixels","bounds_xyxy"))
                and close(observed["value"],expected["depth_m"]),"measurement differs from pixels")
        require(all(reported[k]==expected[k] for k in ("pixels","valid_pixels","column","state"))
                and all(close(reported[k],expected[k]) for k in ("depth_m","valid_fraction")),"incorrect classification")
    near=sorted((x["depth_m"],x["column"]) for x in regions if x["state"]=="near_surface")
    require(r["unknown_columns"]==[x["column"] for x in regions if x["state"]=="unknown"]
            and r["review_order"]==[col for _,col in near],"incorrect human review priority")


def event_log(task):
    require(not task.get("events_has_more") and task["events"],"export complete event pages")
    result={}
    previous=None
    seen_ids=set()
    for row in sorted(task["events"],key=lambda item:item["seq"]):
        payload=row["payload"]
        tr=(json.loads(payload) if isinstance(payload,str) else payload)["trace"]
        require(row["task_id"]==task["id"] and tr["event_id"]==row["event_id"] and tr["trace_id"]==task["trace_id"]
                and row["event_id"] not in seen_ids and tr["parent_event_id"]==previous
                and tr["trigger"]==task["trigger"],"broken native event chain")
        seen_ids.add(row["event_id"])
        previous=row["event_id"]
        result.setdefault(row["kind"],[]).append((row,tr))
    return result


def one(events, kind):
    choices=events.get(kind,[])
    require(len(choices)==1,"expected one "+kind)
    return choices[0]


def check_graph(events, args, evidence, text):
    row,tr=one(events,"planner.graph_composed")
    graph=tr["graph"]
    require(graph==args["graph"]==evidence["graph"] and digest(graph)==evidence["graph_sha256"],"graph pin mismatch")
    requests={v["call_id"]:(e,v) for e,v in events["model.request"]
              if v.get("prompt",{}).get("id")=="planner.compose_read_graph"}
    reply_id=None
    for e,res in events["model.response"]:
        if res.get("call_id") not in requests or e["seq"]>=row["seq"]:
            continue
        content=res["artifact"].get("content")
        if isinstance(content,dict) and isinstance(content.get("message",{}).get("content"),str):
            if json.loads(content["message"]["content"])==graph:
                request,meta=requests[res["call_id"]]
                body=meta["artifact"]["content"]
                user=[m["content"] for m in body["messages"] if m["role"]=="user"]
                require(len(user)==1,"missing model input")
                context=json.loads(user[0])
                selected_ops={n["op"] for n in graph["nodes"]}
                require(context["request"]==text and context["arguments"]=={k:v for k,v in args.items() if k!="graph"}
                        and context["root"]=="frame" and context["required_output_type"]=="review"
                        and selected_ops<set(context["operations"]) and "graph" not in context,
                        "model input differs from the task or offered no operation choice")
                require(request["seq"]<e["seq"] and res["provider"]==meta["provider"]==tr["provider"]=="ollama/"+body["model"]
                        and res["protocol"]==meta["protocol"]=="ollama.chat","unexpected graph author")
                reply_id=e["event_id"]
    require(reply_id,"graph did not match a persisted Ollama response")
    analysis,review=evidence["analysis"],evidence["review"]
    classified={k:v for k,v in review.items() if k not in ("order","review_order")}
    expected=[("measure_p10","review.measure","frame","regions",{"frame_ref":evidence["frame_ref"],
               "columns":args["columns"],"statistic":"p10"},analysis),
              ("classify_near","review.classify","regions","classified",{"analysis":analysis,"distance_m":args["distance_m"]},classified),
              ("rank_nearest","review.rank","classified","review",{"classified":classified,"order":"nearest_first"},review)]
    receipts=evidence["graph_receipts"]
    require(len(receipts)==len(graph["nodes"])==len(expected),"wrong graph length")
    source="frame"
    for node, receipt, (op,service,src_type,out_type,payload,output) in zip(graph["nodes"],receipts,expected):
        require(node["input"]==source and node["op"]==op and all(receipt[k]==v for k,v in node.items())
                and (receipt["service"],receipt["input_type"],receipt["output_type"])==(service,src_type,out_type)
                and (receipt["input_sha256"],receipt["output_sha256"])==(digest(payload),digest(output)),
                "graph edge or service receipt mismatch")
        source=node["id"]
    require(source==graph["output"],"wrong graph output")
    return {"model":tr["provider"],"model_reply_event":reply_id,"composition_event":row["event_id"],
            "graph_digest":digest(graph),"services":[x["service"] for x in receipts]}


def check_run(task, tool, frame, ref, regions, limit):
    tid,trigger=task["id"],task["trigger"]
    require(trigger.get("kind")=="user" and trigger.get("source")=="user" and trigger.get("id")==tid
            and task["state"]=="SUCCEEDED" and len(task["steps"])==1,"invalid user task")
    events=event_log(task)
    step=task["steps"][0]
    pin,op=step["soft_pin"],step["operation_id"]
    require("physical_pin" not in step and step["soft_cap"]["effect"]=="read" and step["status"]=="SUCCEEDED"
            and step["tool"]==pin["name"]==tool and pin["reservation_id"]==op==tid+"/step/1","wrong task/tool/operation")
    deps=pin["dependencies"]
    pid=pin["plugin_id"]
    closure={"review_workflow":{pid}, "review_builtin":{pid,"review055.measure"},
             "review_composed":{pid,"review055.measure","review055.classify","review055.rank"}}
    require(set(deps)==closure[tool] and all(len(identity)==3 and all(SHA.fullmatch(v) for v in identity[1:])
                                            for identity in deps.values()),"incorrect frozen service closure")
    require(pid=="review055."+tool.removeprefix("review_") and pin["version"]==step["version"]==deps[pid][0]
            and deps[pid][1]==pin["content_digest"] and SHA.fullmatch(pin["content_digest"])
            and digest([pid,tool,deps])==pin["binding"]==step["binding"],"invalid frozen dependency closure")
    args,facts=step["args"],step["evidence"]
    require(args["columns"]==len(regions) and args["distance_m"]==limit and facts["implementation"]==KINDS[tool],
            "arguments or implementation diverged")
    assert_pixels(facts,frame,ref,regions,limit)
    family_row,family=one(events,"planner.family_selected")
    select_row,selection=one(events,"planner.implementation_selected")
    candidates=selection["candidates"]
    chosen=next((c for c in candidates if c["eligible"]),{})
    require(family["selected"]==selection["contract"]=="obstacle_review" and selection["tool"]==chosen.get("tool")==tool
            and chosen["binding"]==pin["binding"] and chosen["provider"]==pid and family_row["seq"]<select_row["seq"],
            "selection diverged from the admitted binding")
    frozen_row,frozen=one(events,"planner.frozen")
    require(frozen["steps"]==[{"tool":tool,"binding":pin["binding"],"operation_id":op}],"freeze event mismatch")
    request,call=one(events,"tool.request")
    response,returned=one(events,"tool.response")
    verified,_=one(events,"verified")
    require(request["operation_id"]==response["operation_id"]==verified["operation_id"]==op
            and call["binding"]==returned["binding"]==step["binding"] and call["call_id"]==returned["call_id"]
            and call["provider"]==returned["provider"]=="soft-runtime" and call["module"]==tool
            and call["artifact"].get("content")=={"operation_id":op,"arguments":args,"version":step["version"]}
            and returned["artifact"].get("content")==facts
            and select_row["seq"]<frozen_row["seq"]<request["seq"]<response["seq"]<verified["seq"],
            "dispatch, response or verified receipt mismatch")
    require(not any(k.startswith("ros.") for k in events),"unexpected ROS call in task events")
    result={"task_id":tid, "operation_id":op, "provider":pid, "version":pin["version"], "binding":step["binding"],
            "content_digest":pin["content_digest"], "dependencies":deps, "selected_at":select_row["utc"],
            "verified_at":verified["utc"], "verified_event":verified["event_id"], "selection_event":select_row["event_id"],
            "index_digest":selection["index_digest"], "priority":candidates}
    if tool=="review_composed":
        result["graph"]=check_graph(events,args,facts,task["text"])
    if tool=="review_workflow":
        require(SHA.fullmatch(facts["workflow_digest"]),"invalid workflow identity")
        result["workflow_digest"]=facts["workflow_digest"]
    return result


def site_evidence(directory, runs, frame_ref, drain):
    """Optionally tie the frozen admission to the deployment and host snapshots."""
    snapshot = {}
    bindings = {r["priority"][0]["tool"]:r["binding"] for r in runs[:3]}
    approved = directory / "approved.json"
    if approved.exists():
        require(load(approved)["bindings"] == bindings, "site allowlist does not match frozen bindings")
        snapshot["approved_bindings"] = len(bindings)
    pack_path = directory / "pack.json"
    if pack_path.exists():
        pack = load(pack_path)
        require(pack["tools"] == bindings and pack["frame_ref"] == frame_ref
                and pack["workflow_digest"] == runs[0]["workflow_digest"], "site pack differs from task bindings")
        snapshot.update(pack_lock=pack["lock"], image_digest=pack["soft_platform_digest"])

    before_path = directory / "plugins-before.json"
    after_path = directory / "plugins-after.json"
    if before_path.exists() or after_path.exists():
        before = {p["id"]:p for p in load(before_path)["plugins"]}
        after = {p["id"]:p for p in load(after_path)["plugins"]}
        closure = {pid:identity for run in runs for pid,identity in run["dependencies"].items()}
        require(before.keys() == after.keys() == closure.keys(), "site snapshot has missing or extra plugins")
        for pid, (version, content, _) in closure.items():
            previous, current = before[pid], after[pid]
            require(previous["version"] == current["version"] == version
                    and previous["content_digest"] == current["content_digest"] == content
                    and previous["class"] == current["class"] == "soft"
                    and previous["availability"] == "available", "deployment differs from frozen plugin closure")
            if pid == drain["id"]:
                require(current["draining"] is True and current["availability"] == "unavailable"
                        and (previous["revision"],current["revision"]) == (drain["previous"],drain["revision"]),
                        "host snapshot does not match drain")
            else:
                require(current["availability"] == "available" and previous["revision"] == current["revision"],
                        "an additional plugin became unavailable or changed revision")
        snapshot["admitted_releases"] = len(closure)

    isolation = directory / "isolation-before.json"
    if isolation.exists():
        roles = load(isolation)
        required = {"agent","soft-host","review-worker","review-workflow"}
        require(required <= roles.keys(), "missing runtime isolation evidence")
        for item in roles.values():
            require(item["privileged"] is False and item["read_only_rootfs"] is True and "ALL" in item["cap_drop"]
                    and item["devices"] == [] and item["device_requests"] == [] and item["network_mode"] != "host",
                    "unexpected privileges or devices in deployment snapshot")
        for role in ("review-worker", "review-workflow"):
            require({"target":"/inputs","rw":False} in roles[role]["mounts"]
                    and all(not mount["rw"] for mount in roles[role]["mounts"]), "writable perception input")
        snapshot["container_isolation_checked_at_start"] = sorted(roles)
    return snapshot


def verify(directory, columns, limit):
    require(1<=columns<=8 and .1<=limit<=8,"invalid expected parameters")
    frame=load(directory/"frame.json")
    ref=sha((directory/"frame.json").read_bytes())
    regions=measure(directory,frame,columns,limit)
    runs=[]
    texts=[]
    for i,tool in enumerate(EXPECTED,1):
        task=load(directory/f"{i:02}.json")
        runs.append(check_run(task,tool,frame,ref,regions,limit))
        texts.append(task["text"])
    require(all(s==texts[0] and s.strip() for s in texts) and len({r["task_id"] for r in runs})==4,
            "require four independent submissions of identical natural language")
    require(all(run["priority"][0]["eligible"] is True and run["priority"][0]["tool"]==tool
                for run,tool in zip(runs[:3],EXPECTED)), "initial task did not exercise its preferred implementation")
    require(runs[0]["binding"]==runs[3]["binding"] and runs[0]["workflow_digest"]==runs[3]["workflow_digest"]
            and len({r["content_digest"] for r in runs})==3,"distinct implementations or backup identity not proven")
    config=(directory/"index-before.json").read_bytes()
    require(config==(directory/"index-after.json").read_bytes()
            and sha(config)==runs[2]["index_digest"]==runs[3]["index_digest"],"policy changed during failover")
    candidates=json.loads(config)["contracts"]["obstacle_review"]["implementations"]
    for run in runs[2:]:
        expected=[{k:c[k] for k in ("provider","tool","binding","origin")} for c in candidates]
        actual=[{k:c[k] for k in expected[0]} for c in run["priority"]]
        require(expected==actual,"candidate priority or binding changed")
    require(candidates[0]["tool"]=="review_builtin" and runs[2]["priority"][0]["eligible"] is True
            and runs[3]["priority"][0]["eligible"] is False, "preferred plugin was not withdrawn")
    drain=load(directory/"drain.json")
    require(drain["action"]=="drain" and drain["draining"] is True and drain["id"]==runs[2]["provider"]
            and drain["content_digest"]==runs[2]["content_digest"] and drain["previous"]!=drain["revision"]
            and drain["revision"]==digest([drain["id"],drain["generation"],"drain",drain["content_digest"]])[7:],
            "invalid native drain receipt")
    ops=[json.loads(line) for line in (directory/"ops.jsonl").read_bytes().splitlines() if line.strip()]
    drains=[e for e in ops if e.get("kind")=="site.drain" and e.get("provider")==drain["id"]
            and e.get("source_ref")==drain["event_id"]]
    require(len(drains)==1 and instant(runs[2]["verified_at"])<=instant(drains[0]["utc"])<=instant(runs[3]["selected_at"]),
            "drain event is missing or out of order")
    baseline=instant(runs[2]["selected_at"])
    fallback=instant(runs[3]["selected_at"])
    require(all(e.get("policy_sha256")==sha(config) for e in ops if e.get("kind")=="site.preference"
                and baseline<=instant(e["utc"])<=fallback),"operator changed preference during fallback")
    workflow=directory/"workflow.py"
    if workflow.exists():
        require(sha(workflow.read_bytes())==runs[0]["workflow_digest"],"workflow source mismatch")
    return {"schema_version":1,"passed":True,"task_text":texts[0],"columns":columns,"distance_m":limit,
            "frame_ref":ref,"rgb_ref":frame["rgb_ref"],"depth_ref":frame["depth_ref"],"depth_stamp_ns":frame["depth_stamp_ns"],
            "pixel_measurements":regions,"review_order":[col for _,col in sorted((r["depth_m"],r["column"])
                for r in regions if r["state"]=="near_surface")],
            "unknown_columns":[r["column"] for r in regions if r["state"]=="unknown"],
            "runs":runs,"drain_event_id":drain["event_id"],"unchanged_index":sha(config),
            "site_evidence":site_evidence(directory,runs,ref,drain),
            "workflow_source_verified":workflow.exists(),"dispatch_scope":{"user_tasks":len(runs),"read_only_steps":4,
            "physical_steps_in_these_tasks":0,"ros_request_events_in_these_tasks":0,"independent_hardware_monitor":False}}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",required=True,type=Path)
    parser.add_argument("--columns",required=True,type=int)
    parser.add_argument("--distance-m",required=True,type=float)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    dest=args.output or args.input/"verification.json"
    try:
        report=verify(args.input,args.columns,args.distance_m)
    except (OSError,ValueError,TypeError,KeyError,IndexError,struct.error,zlib.error) as error:
        report={"schema_version":1,"passed":False,"error":str(error)}
    with tempfile.NamedTemporaryFile(mode="w",dir=dest.parent,delete=False,encoding="utf8") as handle:
        json.dump(report,handle,indent=2,ensure_ascii=False,allow_nan=False)
        handle.write("\n")
    Path(handle.name).replace(dest)
    if not report["passed"]:
        parser.exit(1,"Verification failed: "+report["error"]+"\n")
    print(f"Verified {len(report['runs'])} user tasks and {len(report['pixel_measurements'])} RGB-D columns.")
