"""Replay a historical rejected proposal on an isolated local DSH site, then verify real repair and reuse."""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import struct
import sys
import time
from urllib.parse import urlencode
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from client import field_plugin_watch as watcher
from perception.spatial_facts.stationary_fusion import depth_raster


FIRST = "请筛查前方近距障碍候选。把当前画面分为三列，按有效深度的第10百分位识别1.2米以内的近距表面。每列有效像素不足60%记为未知，按从近到远给出优先人工复核的列号。"
NEXT = "将这幅画面平分为两列筛查近距障碍候选。取有效深度第10百分位，阈值0.85米，覆盖率低于60%记为未知，按近到远给出优先复核的列号。"


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def read(path):
    return json.loads(path.read_bytes())


def seed(folder, model):
    """Only the previous (normalized) proposal is reconstructed from cd2f5e0a. Never seed the repair."""
    program = {"near_if":{"left":"depth_m", "right":"distance_m", "op":"lt"},
               "review_order":"nearest_first", "statistic":"median",
               "unknown_if":{"left":"valid_fraction", "right":0.6, "op":"le"}}
    proposal = {"name":"review_depth_column", "description":"复核画面中的近距候选", "program":program}
    folder.mkdir(parents=True, exist_ok=False)
    watcher.save(folder/"author-response-1.json", {"model":model, "done":True, "done_reason":"stop",
                 "message":{"role":"assistant", "content":json.dumps(proposal, ensure_ascii=False)},
                 "replay_source":"normalized proposal from cd2f5e0a; description supplied by test"}, immutable=True)


def verify_pixels(step, png, columns, threshold):
    facts = step["evidence"]
    sample, review = facts["analysis"], facts["review"]
    check(sample["depth_ref"]==watcher.deploy.sha(png), "receipt points to different depth bytes")
    raw,width,height,stride = depth_raster(png)
    ranking, unknown = [], []
    check(len(review["regions"])==columns and review["column_count"]==columns and
          review["statistic"]=="p10" and review["distance_m"]==threshold, "review contract changed")
    for col, reported in enumerate(review["regions"]):
        left,right = width*col//columns, width*(col+1)//columns
        values = []
        for row in range(height):
            start = row*stride+1
            values.extend(n for n, in struct.iter_unpack(">H",raw[start+2*left:start+2*right]) if n)
        values.sort()
        count, total = len(values), (right-left)*height
        z = None
        if values:
            whole,rem = divmod(count-1,10)
            mm = values[whole] if rem==0 else (values[whole]*(10-rem)+values[whole+1]*rem)/10
            z = mm*sample["depth_scale_m"]
        state = "above_threshold"
        if count*10 < total*6 or z is None:
            state = "unknown"
            unknown.append(col+1)
        elif z <= threshold+1e-12:
            state = "near_surface"
            ranking.append((z,col+1))
        check(reported["column"]==col+1 and reported["pixels"]==total and reported["valid_pixels"]==count
              and reported["state"]==state and (reported["depth_m"]==z or (
                  z is not None and reported["depth_m"] is not None and math.isclose(reported["depth_m"],z,abs_tol=1e-12))),
              "depth/coverage/state differs from archived pixels")
    check([col for _,col in sorted(ranking)]==review["review_order"] and unknown==review["unknown_columns"],
          "review order/unknown list differs from pixels")
    return {"depth_ref":sample["depth_ref"], "rgb_ref":sample["rgb_ref"], "depth_stamp_ns":sample["depth_stamp_ns"],
            "review_order":review["review_order"], "unknown_columns":unknown, "regions":review["regions"]}


def run(a):
    a.state.mkdir(parents=True, exist_ok=False)
    base = a.agent_url.rstrip("/")+"/agent"
    api = lambda endpoint, body=None: watcher.decode(watcher.fetch(base+"/"+endpoint, watcher.wire(body) if body is not None else None))
    def task(tid):
        return api("tasks?"+urlencode({"id":tid,"limit":500}))
    def wait_task(tid):
        end=time.monotonic()+a.timeout
        while time.monotonic()<end:
            result=task(tid)
            if result["state"] in watcher.TERMINAL:
                return result
            time.sleep(.5)
        raise RuntimeError("Agent timeout: "+tid)
    def submit(text):
        tid="p0-"+uuid.uuid4().hex
        ack=api("tasks",{"id":tid,"text":text})
        check(ack["id"]==tid, "unexpected acknowledgement")
        return tid
    tags=watcher.decode(watcher.fetch(a.ollama_url.rstrip("/")+"/api/tags"))["models"]
    identity=next(m for m in tags if m["name"]==a.model)
    png=a.depth_png.read_bytes()
    with closing(sqlite3.connect(a.state/"watch.sqlite")) as db:
        w=watcher.Watch(a,db)
        w.scan()
        check(not any(r.get("provider","").startswith("field.") for r in api("capabilities")["records"]),
              "use an isolated clean profile; found existing field plugins")
        orig=wait_task(submit(FIRST))
        watcher.save(a.state/"origin.json", orig)
        check(orig["state"]=="BLOCKED" and not orig["steps"] and
              any(e["kind"]=="planner.plugin_gap" for e in orig["events"]), "no native authoring gap")
        job=a.state/"jobs"/orig["id"]
        seed(job,a.model)
        until=time.monotonic()+a.timeout
        while time.monotonic()<until:
            w.tick()
            found=db.execute("SELECT state FROM jobs WHERE task=?",(orig["id"],)).fetchone()
            if found and found[0]!="pending":
                check(found[0]=="SUCCEEDED","author/continuation ended in "+str(found[0]))
                break
            time.sleep(.6)
        else:
            raise RuntimeError("watcher timed out")
        events=[json.loads(line) for line in (job/"events.jsonl").read_text(encoding="utf8").splitlines()]
        check([r["attempt"] for r in events if r["phase"]=="candidate_rejected"]==[1] and
              [r["attempt"] for r in events if r["phase"]=="candidate_verified"]==[2],"expected reject then repair")
        previous=json.loads(read(job/"author-request-2.json")["messages"][1]["content"])["previous_attempt"]
        check({x["field"] for x in previous["issues"]}=={"statistic","unknown_if","near_if"},
              "missing actionable boundary/statistic feedback")
        model_reply=read(job/"author-response-2.json")
        check(model_reply.get("eval_count",0)>0 and model_reply.get("total_duration",0)>0 and model_reply["model"]==a.model,
              "repair missing real model completion counters")
        pub=read(job/"published-plugin.json")
        follow=read(job/"followup.json")
        reuse=wait_task(submit(NEXT))
        watcher.save(a.state/"reuse.json",reuse)
        w.tick()
        check(db.execute("SELECT COUNT(*) FROM published").fetchone()[0]==1 and
              db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==1,"unexpected extra author job")
        check(follow["trace_id"]==orig["trace_id"] and follow["trigger"]["parent_task_id"]==orig["id"] and
              follow["trigger"]["parent_event_id"]==pub["event_id"], "continuation origin mismatch")
        results=[]
        for t,cols,dist in ((follow,3,1.2),(reuse,2,.85)):
            check(t["state"]=="SUCCEEDED" and len(t["steps"])==1, "read task did not succeed")
            step=t["steps"][0]
            check(step["status"]=="SUCCEEDED" and step["binding"]==pub["binding"] and step["tool"]==pub["tool_id"]
                  and step["args"]=={"columns":cols,"distance_m":dist}
                  and step["evidence"]["review"]["program_sha256"]==pub["program_digest"],"incorrect pinned step")
            check(sum(e["kind"]=="tool.request" for e in t["events"])==1,"unexpected dispatch count")
            results.append({"task":t["id"],"operation_id":step["operation_id"],"state":t["state"],"args":step["args"],
                            **verify_pixels(step,png,cols,dist)})
        check(results[0]["operation_id"]!=results[1]["operation_id"],"reuse must get an independent operation")
        report={"schema_version":1,"scenario":"local DSH / sealed RGB-D replay","passed":True,
                "model":identity["name"],"model_digest":identity["digest"],"seed":"cd2f5e0a normalized first proposal",
                "first_attempt":"replayed; model transport objects reconstructed from historical RPN, description supplied",
                "first_rejection":next(e["local_reason"] for e in events if e["phase"]=="candidate_rejected"),
                "repair":{"attempt":2,"source":"local Ollama /api/chat","issues":previous["issues"],
                          "generated_tokens":model_reply["eval_count"],"duration_ns":model_reply["total_duration"]},
                "job_count":1,"original":{"id":orig["id"],"trace_id":orig["trace_id"],"state":orig["state"]},
                "publication":{k:pub[k] for k in ("plugin_id","version","binding","program_digest","content_digest",
                                                     "plugin_event_id","event_id","active_lock")},
                "qualification_cases":pub["validation"]["case_count"],"runs":results}
        watcher.save(a.state/"verification.json",report)
        print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ("site","registry","policy","state","depth-png"):
        parser.add_argument("--"+key,type=Path,required=True)
    for key in ("agent-url","ollama-url","model"):
        parser.add_argument("--"+key,required=True)
    parser.add_argument("--family-profile",type=Path,required=True)
    parser.add_argument("--model-timeout",type=int,default=360)
    parser.add_argument("--poll-seconds",type=float,default=1)
    parser.add_argument("--timeout",type=int,default=420)
    run(parser.parse_args())
