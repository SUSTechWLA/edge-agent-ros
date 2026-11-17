"""Measure a real local model choosing an evidence query after reading a short session."""
import argparse
import base64
import json
import math
from pathlib import Path
import subprocess
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
import uuid

ROOT=Path(__file__).resolve().parents[2]


def experiment(url, endpoint, model_project, report, save):
    http=build_opener(ProxyHandler({}))
    def api(base,path,body=None):
        req=Request(base.rstrip("/")+path,data=json.dumps(body).encode() if body is not None else None,
                    headers={"Content-Type":"application/json"})
        with http.open(req,timeout=20) as reply:
            return json.load(reply)
    tasks=api(url,"/api/review/tasks?state=issues&limit=100")["tasks"]
    task=next(t for t in tasks if "navigation.path_blocked" in t["detail"])
    report.update(task_id=task["id"],fixture=task)
    session=api(url,"/api/session?"+urlencode({"task_id":task["id"]}))
    schema=api(endpoint,"/schema")
    report.update(session_markdown=session["markdown"],tool_schema=schema)
    save()
    def model(prompt, response_schema):
        body={"model":"qwen2.5:1.5b","stream":False,"format":response_schema,
              "options":{"temperature":0,"num_ctx":4096},
              "messages":[{"role":"system","content":"你是证据诊断助手，严格依据已提供事实和字段。输出符合格式的 JSON。"},
                          {"role":"user","content":prompt}]}
        encoded=base64.b64encode(json.dumps(body,ensure_ascii=False).encode()).decode()
        script=("import base64,json,urllib.request\n"
                "body=base64.b64decode('"+encoded+"')\n"
                "req=urllib.request.Request('http://model:11434/api/chat',data=body,headers={'Content-Type':'application/json'})\n"
                "with urllib.request.urlopen(req,timeout=100) as r: print(r.read().decode())\n")
        raw=subprocess.check_output(["docker","compose","-p",model_project,"-f",str(ROOT/"compose.yaml"),
                                     "exec","-T","console","python3","-"],input=script.encode(),cwd=ROOT)
        reply=json.loads(raw)
        call={"prompt":prompt,"response_schema":response_schema,"raw_answer":reply["message"]["content"],
              "usage":{"prompt_tokens":reply.get("prompt_eval_count"),"output_tokens":reply.get("eval_count")}}
        report["model_calls"].append(call)
        save()
        call["parsed"]=json.loads(call["raw_answer"])
        save()
        return call["parsed"],call["usage"]
    discovery_prompt=("请编写只读SQL，查询本次events表中实际有哪些kind及各类数量，以便下一步定位制动。"
                      "输入已由task限定范围，底层记录task_id列可能空。输出sql。\n"+
                      json.dumps(schema,ensure_ascii=False))
    inventory, inventory_usage=model(discovery_prompt,{"type":"object","properties":{"sql":{"type":"string"}},"required":["sql"]})
    inventory_op="inventory-"+uuid.uuid4().hex[:10]
    inventory_reply=api(endpoint,"/invoke?tool=query_evidence",{
        "operation_id":inventory_op,"version":"1","arguments":{"task_id":task["id"],"sql":inventory["sql"],
             "after_seq":0,"limit":50,"archive_id":""}})
    report["event_discovery"]={"sql":inventory["sql"],"usage":inventory_usage,"receipt":inventory_reply}
    save()
    assert inventory_reply["state"]=="SUCCEEDED", inventory_reply
    vocabulary={key:inventory_reply["evidence"][key] for key in ("columns","rows")}
    prompt=("请依据这份任务会话和已查到的实际事件类型，设计一条查询以查明制动时记录的精确前向距离。"
            "返回名为 seq 和 distance_m 的两列，距离字段为 data_json 的 front_range_m；选择对应制动事件。"
            "输入events已由task限定范围，底层记录task_id列可能空，无需再次筛选task_id。"
            "使用SQLite json_extract函数从JSON中读数。只输出 sql。\n\n"+session["markdown"]+
            "\n\n"+json.dumps({k:schema[k] for k in ("table","columns","allowed_functions","scope") if k in schema},
                                 ensure_ascii=False)+"\n实际事件类型\n"+json.dumps(vocabulary,ensure_ascii=False))
    attempts=[]
    report["attempts"]=attempts
    for _ in range(3):
        query, usage=model(prompt,{"type":"object","properties":{"sql":{"type":"string"}},"required":["sql"]})
        op="model-evidence-"+uuid.uuid4().hex[:10]
        receipt=api(endpoint,"/invoke?tool=query_evidence",{
            "operation_id":op,"version":"1","arguments":{"task_id":task["id"],"sql":query["sql"],
                "after_seq":0,"limit":5,"archive_id":""},"trace":{"trace_id":op}})
        feedback={"rows_returned":len(receipt.get("evidence",{}).get("rows",[])),
                  "columns":receipt.get("evidence",{}).get("columns",[]),
                  "rows":receipt.get("evidence",{}).get("rows",[])}
        attempts.append({"sql":query["sql"],"state":receipt["state"],"usage":usage,"error":receipt.get("error"),
                         "feedback":feedback,"receipt":receipt})
        save()
        if (receipt["state"]=="SUCCEEDED" and receipt["evidence"]["rows"]
                and {"seq","distance_m"}<=set(feedback["columns"])):
            break
        prompt+="\n上次查询及工具反馈，请根据schema纠正后再写SQL。\n"+json.dumps(attempts[-1],ensure_ascii=False)
        if feedback["rows_returned"]==0:
            prompt+=("\n查询返回0行。请从实际事件类型清单中选择制动的kind，code与kind是两个字段。"
                     "查询events已完成任务范围关联，去掉task_id过滤以保留ROS动作。必须返回seq和测量值。")
        elif not {"seq","distance_m"}<=set(feedback["columns"]):
            prompt+="\n请把原始事件seq保留，并把距离表达式命名 AS distance_m。"
    if (receipt["state"]!="SUCCEEDED" or not receipt["evidence"].get("rows")
            or not {"seq","distance_m"}<=set(feedback["columns"])):
        raise AssertionError("query did not return source seq and distance_m after three attempts")
    evidence=receipt["evidence"]
    report["selected_evidence"]=evidence
    answer_prompt=("从工具 rows 选择一行，按 columns 的列顺序原样复制：seq 作为 event_seq，distance_m 作为 distance_m。"
                   "不要编造事件编号，不要把行号或数量当seq，不要四舍五入。\n"+
                   json.dumps({key:evidence[key] for key in ("columns","rows")},ensure_ascii=False))
    report["answer_attempts"]=[]
    verified=False
    # A failed SQL round consumes one correction round. A successful SQL result
    # is reused when only the answer is wrong. Total diagnostic rounds <= 3.
    for number in range(4-len(attempts)):
        answer,second_usage=model(answer_prompt,
            {"type":"object","properties":{"event_seq":{"type":"integer"},"distance_m":{"type":"number"}},
             "required":["event_seq","distance_m"],"additionalProperties":False})
        answer_attempt={"round":len(attempts)+number,"answer":answer,"usage":second_usage}
        report["answer_attempts"].append(answer_attempt)
        report["answer"]=answer
        save()
        try:
            assert isinstance(answer,dict) and type(answer.get("event_seq")) is int,"event_seq must be an integer"
            assert type(answer.get("distance_m")) in (int,float) and math.isfinite(answer["distance_m"]),"distance must be finite"
            seq_idx=evidence["columns"].index("seq")
            distance_idx=evidence["columns"].index("distance_m")
            assert any(type(row[seq_idx]) is int and row[seq_idx]==answer["event_seq"]
                       and type(row[distance_idx]) in (int,float)
                       and abs(row[distance_idx]-answer["distance_m"])<1e-9 for row in evidence["rows"]),\
                "回答中的事件号和距离并非工具 rows 中同一行的原值，请按列位置原样复制。"
            assert answer["event_seq"] in {ref["seq"] for ref in evidence.get("refs",[])},"event has no source reference"
            archive=api(url,"/api/history")
            assert archive["archive_id"]==evidence["coverage"]["archive_id"],"archive changed during validation"
            original=api(url,"/api/replay?"+urlencode({"at":answer["event_seq"]}))["event"]
            answer_attempt["source_event"]=original
            assert original["kind"]=="action.braking","selected source is not the disclosed braking fixture"
            assert abs(original["data"]["front_range_m"]-answer["distance_m"])<0.000001,"distance differs from original event"
            answer_attempt["matches_source"]=True
            verified=True
            save()
            break
        except Exception as error:
            answer_attempt["validation_failure"]={"type":type(error).__name__,"message":str(error)}
            if hasattr(error,"read"):
                answer_attempt["validation_failure"]["http_body"]=error.read().decode("utf-8","replace")[:4000]
                error.close()
            save()
            answer_prompt=("上次回答校验失败，请依据同一工具结果纠正，输出 seq 与 distance_m 的原值。\n"+
                json.dumps({key:evidence[key] for key in ("columns","rows")},ensure_ascii=False)+
                "\n上次回答和真实校验反馈：\n"+json.dumps(answer_attempt,ensure_ascii=False))
    assert verified,"real model answer did not match source within three diagnostic rounds"
    report.update({"passed":True,"model":"qwen2.5:1.5b","model_call_count":len(report["model_calls"]),"task_id":task["id"],
            "generated_sql":query["sql"],"event_seq":answer["event_seq"],"distance_m":answer["distance_m"],
            "matches_source":True,"attempts":attempts,"answer_usage":second_usage,
            "summary_bytes":len(session["markdown"].encode()),"query_result_bytes":len(json.dumps(evidence).encode())})


def run(url,endpoint,model_project,out):
    report={"passed":False,"model":"qwen2.5:1.5b","model_calls":[],"attempts":[],
            "limitations":"One fixed disclosed navigation fixture and a known front_range_m field path. "
                "At most three diagnostic correction rounds after one event-kind discovery call. "
                "This validates real model query execution and source-checked extraction, not general diagnosis or autonomous repair. "
                "No tasks or robot controls are submitted."}
    out.parent.mkdir(parents=True,exist_ok=True)
    def save():
        temporary=out.with_suffix(out.suffix+".tmp")
        temporary.write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+"\n",encoding="utf-8")
        temporary.replace(out)
    save()
    try:
        experiment(url,endpoint,model_project,report,save)
    except Exception as error:
        report["failure"]={"type":type(error).__name__,"message":str(error)}
        if hasattr(error,"read"):
            report["failure"]["http_body"]=error.read().decode("utf-8","replace")[:4000]
            error.close()
    finally:
        report["model_call_count"]=len(report["model_calls"])
        save()
        print(json.dumps(report,ensure_ascii=False))
    assert report["passed"],f"Real-model fixture failed; complete attempts and answers: {out}"


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",required=True)
    parser.add_argument("--evidence-url",required=True)
    parser.add_argument("--model-project",required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    run(args.url,args.evidence_url,args.model_project,args.out)
