"""Isolated experiment's Agent Tool. Persist its claim before starting a single worker."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

ROOT=Path("/work")
LOCK=threading.Lock()
ACTIVE={}


def poll(body):
    op=body["operation_id"]
    task,sep,step=op.partition("/step/")
    if not re.fullmatch(r"[a-z0-9-]{1,72}",task) or sep!="/step/" or step!="1" or body.get("arguments")!={} or body.get("version")!="1":
        raise ValueError("invalid operation or arguments")
    claim=ROOT/("claim-"+task+".json")
    report=ROOT/task/"report.json"
    with LOCK:
        if not claim.exists():
            claim.write_text(json.dumps({"operation_id":op,"task_id":task}),encoding="utf8")
            with (ROOT/("worker-"+task+".log")).open("xb") as log:
                ACTIVE[task]=subprocess.Popen([sys.executable,"-B","/experiment/campus_survey_worker.py",
                    "--output",str(ROOT),"--operation",task,"--trace-id",task,"--calibration","/factory"],
                    stdout=log,stderr=subprocess.STDOUT)
        proc=ACTIVE.get(task)
        if report.exists():
            row=json.loads(report.read_bytes())
            if row["state"]=="SUCCEEDED":
                return dict(operation_id=op,state="SUCCEEDED",evidence={k:row[k] for k in
                    ("map_file","saved_known_gain","survey_arrivals","inspection_file","inspection_xy","home_error_m","map_sha256")})
            if row["state"] in ("BLOCKED","FAILED","RECOVERY_REQUIRED"):
                return dict(operation_id=op,state="BLOCKED",detail=row.get("reason",row["state"]))
        if proc is None or proc.poll() is not None:
            return dict(operation_id=op,state="BLOCKED",detail="worker stopped; reconcile original operation")
        return dict(operation_id=op,state="RUNNING")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            if self.path!="/survey": raise ValueError("unknown tool")
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=32768: raise ValueError("invalid request size")
            result=poll(json.loads(self.rfile.read(size)))
            code=200
        except (ValueError,KeyError,TypeError) as exc:
            result,code={"detail":str(exc)},400
        payload=json.dumps(result).encode()
        self.send_response(code)
        self.send_header("Content-Length",str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__=="__main__":
    if os.environ.get("EDGE_COMMISSION_MODE")!="isolated_gazebo": sys.exit("requires private simulation")
    ThreadingHTTPServer(("0.0.0.0",8193),Handler).serve_forever()
