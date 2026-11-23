"""Time a resident read-only provider and caller on loopback using offline replay data."""
import argparse
from collections import Counter
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from perception.rover_preview.provider import Handler, Provider, VERSION


class QuietHandler(Handler):
    def log_message(self,*args):
        pass


def measure(item, repeats):
    measurements=[]
    statuses=Counter()
    with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1",0),QuietHandler) as server:
        server.provider=Provider(Path(tmp),item["profile"],"http://127.0.0.1:1")
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            snap=dict(item["snapshot"])
            objective={"kind":"static_local_path","frame_id":snap["base_frame"]}
            if "goal" in item:
                objective["goal_body_m"]=item["goal"]
            request={"schema_version":1,"provider_version":VERSION,"clock_id":"unix","objective":objective,
                     "budget_ms":50,"snapshot":snap}
            for i in range(repeats+2):
                start=time.perf_counter_ns()
                # Only the admission clock is regenerated. The source geometry remains the archived input.
                snap["scan_stamp_ns"]=str(time.time_ns())
                payload=json.dumps(request,separators=(",",":"),allow_nan=False).encode()
                conn=HTTPConnection("127.0.0.1",server.server_port,timeout=3)
                try:
                    conn.request("POST","/evaluate",payload,{"Content-Type":"application/json"})
                    reply=conn.getresponse()
                    result=json.loads(reply.read())
                finally:
                    conn.close()
                elapsed=(time.perf_counter_ns()-start)/1e6
                if result.get("motion_authorized") is not False:
                    raise AssertionError("expected read-only response")
                error=result.get("error",{}).get("code")
                if reply.status!=200 and (error!="planning.deadline" or result.get("path_body_m")!=[]):
                    raise AssertionError((reply.status,result.get("error")))
                if i>=2:
                    measurements.append(elapsed)
                    statuses[error or result["status"]]+=1
        finally:
            server.shutdown()
            thread.join()
            server.provider.pool.shutdown(wait=True)
    return {"p50_ms":statistics.median(measurements),"p95_ms":sorted(measurements)[math.ceil(.95*repeats)-1],
            "max_ms":max(measurements),"responses":dict(statuses),
            "round_trips_over_50ms":sum(m>50 for m in measurements)}


if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs",type=Path,required=True)
    ap.add_argument("--iterations",type=int,default=60)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    if args.iterations<3:
        ap.error("at least three timed samples required")
    raw=args.inputs.read_bytes()
    inputs=json.loads(raw)
    source_dir=ROOT/"perception"/"rover_preview"
    result={"method":"Two warmups, repeated caller JSON encode, loopback connection, provider work, reply decode.",
            "clock":"Source geometry replayed with fresh admission stamps; no acquisition, ROS, LLM or actuation.",
            "platform":platform.platform(),"machine":platform.machine(),"python":platform.python_version(),
            "iterations_per_case":args.iterations,"inputs_file_sha256":hashlib.sha256(raw).hexdigest(),
            "provider_version":VERSION,"service_budget_ms":50,
            "source_sha256":{name:hashlib.sha256((source_dir/name).read_bytes()).hexdigest()
                             for name in ("planner.py","trajectory.py","evaluator.py","provider.py")},
            "cases":{key:measure(inputs[key],args.iterations) for key in ("clear","centered","field_hold","dense")}}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    for name,row in result["cases"].items():
        print(name,round(row["p95_ms"],2),"ms p95",row["responses"])
