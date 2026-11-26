"""Serve one frozen-frame measurement service or one separately deployed site workflow."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
from types import MappingProxyType

from edge_contracts.spatial_facts import SpatialFactError, digest_ref, require
from perception.review055.adapters import check_args, success_or_blocked, verify_goal
from perception.review055.frames import frozen_frame, measure

def workflow_program(path, expected):
    require(not path.is_symlink(),"review.workflow_path")
    with path.open("rb") as src:
        raw=src.read(256*1024+1)
    actual="sha256:"+hashlib.sha256(raw).hexdigest()
    require(0<len(raw)<=256*1024 and (expected is None or actual==expected),"review.workflow_digest_mismatch")
    # Only a deployment-owned, image-pinned or explicitly digest-pinned file reaches this isolated process.
    ns={"__name__":"site_review_workflow","__file__":str(path)}
    exec(compile(raw,str(path),"exec"),ns)
    require(callable(ns.get("review")),"review.workflow_missing_entrypoint")
    return ns["review"],actual

class Worker:
    def __init__(self, role, directory, ref, path=None, expected=None):
        require(role in ("measure","workflow") and digest_ref(ref),"review.invalid_deployment")
        self.role,self.directory,self.ref=role,Path(directory),ref
        self.workflow=None
        self.health={"role":role,"frame_ref":ref,"ready":True}
        frozen_frame(self.directory,ref)
        if role=="workflow":
            require(path is None or digest_ref(expected),"review.external_workflow_needs_digest")
            src=Path(path) if path is not None else Path(__file__).with_name("site_workflow.py")
            self.workflow,self.health["workflow_digest"]=workflow_program(src,expected)

    def evaluate(self,path,request):
        if self.role=="measure":
            require(path=="/measure" and isinstance(request,dict) and set(request)=={"frame_ref","columns","statistic"}
                    and request["frame_ref"]==self.ref,"review.bad_query")
            info,depth=frozen_frame(self.directory,self.ref)
            return measure(info,depth,request["columns"],request["statistic"])
        require(path=="/invoke" and isinstance(request,dict) and isinstance(request.get("operation_id"),str)
                and 0<len(request["operation_id"])<=256,"review.invalid_operation")
        operation=request["operation_id"]
        try:
            args=request["arguments"]
            check_args(args)
            info,depth=frozen_frame(self.directory,self.ref)
            result=self.workflow(MappingProxyType(info),depth,args["columns"],args["distance_m"])
            require(isinstance(result,dict) and set(result)=={"analysis","review"},"review.invalid_workflow_output")
            verify_goal(result["analysis"],result["review"],args["columns"],args["distance_m"],self.ref)
            require(all(result["analysis"].get(k)==v for k,v in info.items()),"review.changed_frame_identity")
            return success_or_blocked(operation,evidence={**result,"frame_ref":self.ref,"implementation":"site_workflow",
                    "workflow_digest":self.health["workflow_digest"]})
        except (OSError,ValueError,KeyError,TypeError) as exc:
            code=getattr(exc,"code",None) or (str(exc) if str(exc).startswith("review.") else "review.workflow_failed")
            return success_or_blocked(operation,reason=code)

def handler(worker):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def reply(self,status,payload):
            raw=json.dumps(payload,separators=(",",":"),allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Length",str(len(raw)))
            self.send_header("Content-Type","application/json")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path=="/health":
                self.reply(200,worker.health)
            else:
                self.reply(404,{"error":{"code":"review.unknown_route"}})

        def do_POST(self):
            if self.path!=("/measure" if worker.role=="measure" else "/invoke"):
                return self.reply(404,{"error":{"code":"review.unknown_route"}})
            try:
                lengths=self.headers.get_all("Content-Length") or []
                require(len(lengths)==1 and re.fullmatch("[1-9][0-9]{0,4}",lengths[0]) and
                        int(lengths[0])<=32768 and "Transfer-Encoding" not in self.headers,"review.invalid_size")
                raw=self.rfile.read(int(lengths[0]))
                require(len(raw)==int(lengths[0]),"review.truncated_request")
                result=worker.evaluate(self.path,json.loads(raw))
            except (ValueError,TypeError,KeyError,RecursionError) as exc:
                return self.reply(422,{"error":{"code":getattr(exc,"code","review.invalid_request")}})
            except OSError:
                return self.reply(503,{"error":{"code":"review.frame_unavailable"}})
            self.reply(200,result)

        def log_message(self, *args):
            pass
    return Handler

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role",required=True,choices=("measure","workflow"))
    parser.add_argument("--inputs",default="/inputs",type=Path)
    parser.add_argument("--frame-ref",default=os.environ.get("EDGE_REVIEW_FRAME_REF"))
    parser.add_argument("--workflow",type=Path)
    parser.add_argument("--workflow-digest",default=os.environ.get("EDGE_REVIEW_WORKFLOW_DIGEST"))
    parser.add_argument("--port",type=int,default=8080)
    args=parser.parse_args()
    worker=Worker(args.role,args.inputs,args.frame_ref,args.workflow,args.workflow_digest)
    with HTTPServer(("0.0.0.0",args.port),handler(worker)) as server:
        server.serve_forever()

if __name__=="__main__":
    main()
