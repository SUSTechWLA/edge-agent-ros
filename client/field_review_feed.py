"""Project a recording selection, native task ledger and plugin-author receipts onto the read-only Console."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from urllib.parse import urlencode

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from edge_contracts.trace_content import sanitized

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
HASH = re.compile(r"sha256:([a-f0-9]{64})")


def read(path):
    return json.loads(path.read_bytes())


def atomic(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def get(url, path, **query):
    endpoint = url.rstrip("/") + "/agent/" + path + "?" + urlencode(query)
    try:
        with urlopen(endpoint, timeout=4) as res:
            raw=res.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:
                raise ValueError("oversize Agent response")
            return json.loads(raw)
    except HTTPError as error:
        if error.code == 404:
            return {}
        raise


def bound_image(directory, output, ref):
    match = HASH.fullmatch(ref) if isinstance(ref,str) else None
    if not match:
        return None
    filename = match[1] + ".png"
    target = output / filename
    source=target if target.is_file() else directory/filename
    try:
        with source.open("rb") as stream:
            content=stream.read(8*1024*1024+1)
    except FileNotFoundError:
        return None
    if (len(content) > 8*1024*1024 or not content.startswith(b"\x89PNG\r\n\x1a\n")
            or hashlib.sha256(content).hexdigest() != match[1]):
        raise ValueError("frame digest/length mismatch")
    if source != target:
        atomic(target, content)
    return filename


PHASES = {
    "gap_observed":"检测到缺少的感知能力", "author_requested":"车端模型开始编写插件",
    "author_replied":"模型返回候选程序", "candidate_rejected":"候选未通过站点验收",
    "candidate_verified":"独立用例验收通过", "plugin_loaded":"新插件已进入在线工具目录",
    "continuation_submitted":"已提交原任务的只读续办", "continuation_terminal":"续办结束",
    "retrying":"重新核验发布状态", "failed":"插件发布未完成",
}

def select(row, keys):
    return {k:row[k] for k in keys.split() if k in row}


def valid_sha(value):
    return isinstance(value,str) and HASH.fullmatch(value) is not None


def stamp(value):
    return isinstance(value,str) and re.fullmatch("[1-9][0-9]{0,18}",value) is not None and int(value)<=2**63-1


def public_measurement(analysis, review):
    a=select(analysis,"width height frame_id clock_id actor_ref unit grid program statistic channel depth_scale_m "
             "rgb_ref rgb_stamp_ns depth_ref depth_stamp_ns calibration_ref calibration_stamp_ns "
             "rgb_source_id depth_source_id calibration_source_id")
    a["regions"]=[select(r,"bounds_xyxy pixels valid_pixels value") for r in analysis.get("regions",[]) if isinstance(r,dict)]
    v=select(review,"reason column_count distance_m review_order unknown_columns program_sha256 scope statistic")
    v["regions"]=[select(r,"column state depth_m valid_fraction valid_pixels pixels") for r in review.get("regions",[])
                  if isinstance(r,dict)]
    # Keep missing data visible, including a failed provider reply without a calibrated source frame.
    valid_columns=set()
    for i,region in enumerate(a["regions"],1):
        if (type(region.get("valid_pixels")) is int and region["valid_pixels"]>0
                and type(region.get("value")) in (int,float) and region["value"]>0):
            valid_columns.add(i)
        else:
            region["value"]=None
    for region in v["regions"]:
        if region.get("column") not in valid_columns:
            region.update(state="unknown",depth_m=None)
    v["review_order"]=[col for col in v.get("review_order",[]) if col in valid_columns]
    if not valid_columns:
        n=v.get("column_count")
        if type(n) is not int and a.get("grid") and len(a["grid"])==2:
            n=a["grid"][1]
        if type(n) is int and 1<=n<=8:
            v.update(unknown_columns=list(range(1,n+1)))
        v["review_order"]=[]
        for region in v["regions"]:
            region.update(state="unknown",depth_m=None)
        return a,v,"unknown"
    return a,v,"measured"


def reuse_evidence(runs, add):
    previous={}
    for run in sorted(runs,key=lambda row:row.get("receipt_utc","")):
        a=run.get("analysis",{})
        if (run.get("measurement_status")!="measured" or run.get("state")!="SUCCEEDED" or not run.get("receipt_event_id")
                or not run.get("operation_id") or not run.get("receipt_utc") or not run.get("version")
                or not all(valid_sha(run.get(k)) for k in ("binding","content_digest","program_digest"))
                or not run.get("pin_verified") or not run.get("provider") or not run.get("tool")
                or not a.get("depth_source_id") or not a.get("clock_id")
                or not valid_sha(a.get("depth_ref")) or not stamp(a.get("depth_stamp_ns"))):
            continue
        signature=(run["provider"],run["tool"],run["version"],run["binding"],run["content_digest"],run["program_digest"])
        original=previous.setdefault(signature,run)
        if (original["task_id"]==run["task_id"] or original["operation_id"]==run["operation_id"]
                or a["depth_source_id"]!=original["analysis"]["depth_source_id"]
                or a.get("clock_id")!=original["analysis"].get("clock_id")
                or int(a["depth_stamp_ns"])<=int(original["analysis"]["depth_stamp_ns"])):
            continue
        proof={"from_task_id":original["task_id"],"to_task_id":run["task_id"],"same_binding":True,
               "plugin_id":run["provider"],"tool_id":run["tool"],"version":run["version"],"binding":run["binding"],
               "content_digest":run["content_digest"],"program_digest":run["program_digest"],
               "author_calls":run["author_calls"]}
        for prefix,owner in (("from",original),("to",run)):
            for field in ("operation_id","receipt_utc","receipt_event_id","ledger_task_id"):
                proof[prefix+"_"+field]=owner[field]
            for field in ("depth_ref","depth_stamp_ns","rgb_ref","rgb_stamp_ns"):
                proof[prefix+"_"+field]=owner["analysis"].get(field)
        run["reuse"]=proof
        add("plugin.reused","两次独立请求用同一版本和绑定，读取了不同采样时刻的深度帧。",
            run["task_id"],run["receipt_utc"],source=proof)


def instant(value):
    if not isinstance(value,str):
        return None
    try:
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def native_events(task):
    for row in task.get("events",[]):
        payload=row.get("payload",{})
        if isinstance(payload,str):
            payload=json.loads(payload)
        trace=payload.get("trace",{})
        if (row.get("task_id")==task["id"] and row.get("event_id") and trace.get("event_id")==row["event_id"]
                and trace.get("trace_id")==task["trace_id"] and trace.get("trigger")==task["trigger"]
                and instant(row.get("utc"))):
            yield row,trace


def add_maintenance(path, runs, add):
    if not path.is_file():
        return
    with path.open("rb") as stream:
        raw=stream.read(16385)
    if len(raw)>16384:
        raise ValueError("oversize maintenance records")
    rows=json.loads(raw)
    if isinstance(rows,dict):
        rows=rows.get("events")
    if not isinstance(rows,list) or len(rows)>16:
        raise ValueError("invalid maintenance records")
    selected={run["task_id"] for run in runs}
    for row in rows:
        if not isinstance(row,dict) or row.get("task_id") not in selected or not instant(row.get("utc")):
            continue
        detail=row.get("detail")
        source=row.get("source")
        if not isinstance(source,dict) or not isinstance(detail,str) or not detail.strip() or len(detail)>320:
            continue
        source={k:v for k,v in select(source,"event_id container_id image_digest started_at reference").items()
                if isinstance(v,str) and 0<len(v)<=200}
        if source:
            add("operator.maintenance",detail,row["task_id"],row["utc"],source=source)


def snapshot(options):
    selection = read(options.selection)
    tasks = selection.get("task_ids")
    if tasks is None:
        rows = get(options.agent, "task-index", limit=16).get("tasks", [])
        tasks = [r["id"] for r in reversed(rows) if r["created"] >= selection["since"]]
    if not isinstance(tasks, list) or len(tasks)>16 or any(not isinstance(k,str) or not ID.fullmatch(k) for k in tasks):
        raise ValueError("invalid field recording selection")
    runs, events = [], []
    ready_file=options.author_state/"ready.json"
    ready=instant(read(ready_file).get("utc")) if ready_file.is_file() else None
    def add(phase, detail, key, utc, **data):
        events.append(dict(phase=phase, detail=detail, task_id=key, utc=utc, **data))
    for key in dict.fromkeys(tasks):
        task = get(options.agent, "tasks", id=key, limit=160)
        trigger=task.get("trigger") or {}
        if (task.get("id")!=key or trigger.get("id")!=key or trigger.get("kind")!="user"
                or trigger.get("source")!="user" or not task.get("trace_id")):
            continue
        created=instant(task["created"])
        run = dict(task_id=key,text=task["text"],state=task["state"],created=task["created"],
                   trace_id=task["trace_id"],author_calls=0 if ready and created and ready<=created else None)
        runs.append(run)
        add("submitted",task["text"],key,task["created"])
        job = options.author_state / "jobs" / key
        history = job / "events.jsonl"
        attempts=set()
        if history.is_file():
            for line in history.read_bytes().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:  # A writer may still be appending the tail.
                    continue
                phase = row.get("phase")
                if (phase in PHASES and row.get("task_id")==key and row.get("trace_id")==task["trace_id"]
                        and instant(row.get("utc"))):
                    source=select(row,"attempt model request_sha256 response_sha256 program_sha256 case_count "
                                  "binding content_digest plugin_id event_id family gap_event_id blocked_event_id "
                                  "followup_task_id state code")
                    add(phase,PHASES[phase],key,row["utc"],source=source)
                    if phase=="author_requested" and type(row.get("attempt")) is int:
                        attempts.add(row["attempt"])
                        run["author_calls"]=len(attempts)
                    if phase in ("author_replied","candidate_verified","plugin_loaded"):
                        field={"author_replied":"authored_utc","candidate_verified":"qualified_utc",
                               "plugin_loaded":"loaded_utc"}[phase]
                        run[field]=row["utc"]
        for name in ("candidate-1.json","candidate-2.json","published-plugin.json"):
            file = job / name
            if file.is_file():
                doc=read(file)
                if name.startswith("candidate-") and "program" in doc:
                    run["candidate"] = {**select(doc,"name description"),
                                        "program":select(doc["program"],"statistic unknown_if near_if review_order")}
                if name == "published-plugin.json":
                    release=select(doc,"plugin_id tool_id version content_digest program_digest code_digest binding event_id")
                    proof=doc.get("validation",{})
                    release["validation"]={**select(proof,"program_sha256 case_count"),
                        "cases":[select(c,"case states review_order passed") for c in proof.get("cases",[]) if isinstance(c,dict)]}
                    run["release"]=release
        child_post = job / "followup-post.json"
        owner = task
        if child_post.is_file():
            post=read(child_post)
            cid = post.get("id")
            if isinstance(cid,str) and ID.fullmatch(cid):
                child = get(options.agent,"tasks",id=cid,limit=160)
                cause=child.get("trigger") or {}
                release=run.get("release") or {}
                if (child.get("id")==cid and cause.get("kind")=="agent" and cause.get("source")=="plugin.supervisor"
                        and cause.get("id")==cid and cause.get("parent_task_id")==key
                        and cause.get("trace_id")==child.get("trace_id")==task["trace_id"] and post.get("trigger")==cause
                        and release.get("event_id") and cause.get("parent_event_id")==release["event_id"]
                        and valid_sha(release.get("binding")) and
                        cause.get("read_only_tool")=={"id":release["tool_id"],"binding":release["binding"]}):
                    owner=child
                    run.update(continuation=cid,original_state=task["state"],state=child["state"])
        verified={}
        no_depth={}
        for instance in [task] + ([owner] if owner is not task else []):
            for row,trace in native_events(instance):
                kind = row["kind"]
                source={"event_id":row["event_id"],"ledger_task_id":instance["id"],"trace_id":instance["trace_id"]}
                if kind=="planner.family_selected" and isinstance(trace.get("selected"),str) and trace.get("provider"):
                    source.update(select(trace,"selected provider reason"))
                    sha=(trace.get("artifact") or {}).get("sha256","")
                    if isinstance(sha,str) and re.fullmatch("[a-f0-9]{64}",sha):
                        source["artifact_sha256"]=sha
                    run.setdefault("family_selections",[]).append(dict(source,utc=row["utc"]))
                    add(kind,source.get("reason") or source["selected"],key,row["utc"],source=source)
                if kind == "planner.plugin_gap":
                    add(kind,trace.get("reason") or "现有工具不能完整交付本次观测",key,row["utc"],
                        source={**source,**select(trace,"family provider")})
                if kind in ("planner.context","tool.request","tool.error"):
                    add(kind,{"planner.context":"模型读取已授权的只读工具目录","tool.request":"内核派发已绑定的只读步骤",
                              "tool.error":"工具报告执行异常"}[kind],key,row["utc"],source=source)
                op=row.get("operation_id")
                if instance is owner and op:
                    if kind=="verified":
                        verified[op]=row
                    if trace.get("code")=="query.no_valid_pixels" or (trace.get("diagnosis") or {}).get("code")=="query.no_valid_pixels":
                        no_depth[op]=row
        for step in owner.get("steps",[]):
            pin = step.get("soft_pin") or {}
            evidence = step.get("evidence") or {}
            op=step.get("operation_id")
            args=select(step.get("args") or {},"columns distance_m")
            if op in no_depth and isinstance(evidence,dict) and not evidence.get("review"):
                evidence={"review":{"reason":"query.no_valid_pixels","column_count":args.get("columns")}}
            if not isinstance(evidence,dict) or not isinstance(evidence.get("review"),dict):
                continue
            analysis,review,status=public_measurement(evidence.get("analysis") or {},evidence["review"])
            run.update(operation_id=op,binding=step.get("binding"),tool=step.get("tool"),args=args,
                       provider=pin.get("plugin_id"),analysis=analysis,review=review,measurement_status=status,
                       ledger_task_id=owner["id"],version=pin.get("version"),content_digest=pin.get("content_digest"),
                       program_digest=review.get("program_sha256"),pin_verified=bool(pin and
                       pin.get("name")==step.get("tool") and pin.get("binding")==step.get("binding")))
            for name, directory, field in (("image",options.rgb,"rgb_ref"),("depth_image",options.depth,"depth_ref")):
                file = bound_image(directory, options.out, analysis.get(field))
                if file:
                    run[name]=file
            receipt=verified.get(op)
            if step.get("status")=="SUCCEEDED" and owner["state"]=="SUCCEEDED" and receipt:
                run.update(receipt_utc=receipt["utc"],receipt_event_id=receipt["event_id"])
                add("measurement","本次深度帧的分区测量与复核顺序已返回" if status=="measured" else "有效深度不足，分区状态未知",
                    key,receipt["utc"],binding=run["binding"],operation_id=op,
                    source={"event_id":receipt["event_id"],"ledger_task_id":owner["id"]})
        if owner["state"] in ("BLOCKED","RECOVERY_REQUIRED","FAILED") and not history.exists():
            add("blocked",owner["state"],key,owner["updated"])
    reuse_evidence(runs,add)
    add_maintenance(options.selection.with_name("maintenance.json"),runs,add)
    events.sort(key=lambda r:instant(r["utc"]))
    return sanitized({"schema_version":1,"mode":"live","source":"Jetson · Agent 任务账本 / 站点发布回执 / RealSense D435",
            "first_text":runs[0]["text"] if runs else "", "second_text":runs[1]["text"] if len(runs)>1 else "",
            "runs":runs,"events":events})


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent",required=True)
    for name in ("selection","author-state","out","rgb","depth"):
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--once",action="store_true")
    o=p.parse_args()
    o.out.mkdir(exist_ok=True,parents=True)
    while True:
        try:
            projection=snapshot(o)
            atomic(o.out/"manifest.json",json.dumps(projection,ensure_ascii=False,allow_nan=False).encode("utf8"))
        except (OSError,ValueError,URLError) as exc:
            if o.once:
                raise
            print(datetime.now(timezone.utc).isoformat(),type(exc).__name__,str(exc)[:200],flush=True)
        if o.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
