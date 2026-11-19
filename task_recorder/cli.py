"""Control recordings from a Docker host with standard Python and no ROS installation."""
import argparse
import json
import math
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

from . import agent_trace

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "BLOCKED", "RECOVERY_REQUIRED"}
TASK_STATES = TERMINAL | {"PLANNING", "PLANNED", "WAITING", "RUNNING", "PENDING", "QUEUED"}
TRANSIENT_HTTP = {429, 502, 503, 504}


class EndpointError(RuntimeError):
    def __init__(self, status, reason):
        self.status=status
        super().__init__(reason)


def endpoint(url, path, data=None):
    parsed=urlsplit(url)
    if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("use an HTTP(S) service URL without inline credentials, query or fragment")
    request=Request(url.rstrip("/")+path,data=None if data is None else json.dumps(data).encode(),
                    headers={"Content-Type":"application/json"} if data is not None else {})
    try:
        with urlopen(request,timeout=5) as stream:
            body=stream.read(2*1024**2+1)
    except HTTPError as exc:
        # Agent error details can contain user-provided inputs; do not echo response bodies.
        exc.close()
        raise EndpointError(exc.code,"service returned HTTP "+str(exc.code)) from None
    except (TimeoutError, OSError, URLError) as exc:
        raise EndpointError(0,"service unavailable or timed out") from exc
    if len(body)>2*1024**2:
        raise ValueError("service response exceeds 2 MiB")
    return json.loads(body)


def finite_positive(value):
    n=float(value)
    if not math.isfinite(n) or n<=0:
        raise argparse.ArgumentTypeError("expected a finite positive duration")
    return n


def finish_recording(recorder, path, task_id, status, reason):
    try:
        return endpoint(recorder,path+"/finish",{"task_id":task_id,"status":status,"reason":reason})
    except EndpointError as exc:
        if exc.status!=409:
            raise
        current=endpoint(recorder,path)
        if current["task_id"]!=task_id or current["status"]=="ACTIVE":
            raise
        return current


def append_observation(recorder, path, event, deadline, interval):
    """Preserve the same receipt identity after an ambiguous HTTP write."""
    while time.monotonic() < deadline:
        try:
            endpoint(recorder,path+"/events",event)
            return None
        except EndpointError as exc:
            if exc.status==409:
                current=endpoint(recorder,path)
                if current["status"]!="ACTIVE":
                    return current
            if exc.status and exc.status not in TRANSIENT_HTTP:
                raise
            time.sleep(min(interval,.5))
    raise TimeoutError("recording append timed out")


def watch(recorder, rid, agent, *, wait_for_task, finish, interval, timeout):
    path="/recordings/"+quote(rid,safe="")
    session=endpoint(recorder,path)
    if session["status"]!="ACTIVE":
        return session
    if "agent_state" not in session["sources"]:
        raise ValueError("watch requires agent_state among the declared sources")
    task_id=session["task_id"]
    deadline=time.monotonic()+timeout
    seen=False
    previous=None
    watch_id=uuid.uuid4().hex
    serial=0
    after=0
    trace_enabled="agent_trace" in session["sources"]
    text_pending="agent_request" in session["sources"]
    while time.monotonic()<deadline:
        current=endpoint(recorder,path)
        if current["status"]!="ACTIVE":
            return current
        events=[]
        more=False
        try:
            query={"id":task_id,"limit":1}
            if trace_enabled:
                query.update(after=after,limit=agent_trace.PAGE_SIZE)
            task=endpoint(agent,"/agent/tasks?"+urlencode(query))
            if task.get("id")!=task_id:
                raise ValueError("agent returned a different task identity")
            state=task["state"]
            if state not in TASK_STATES:
                raise ValueError("unrecognized Agent task state")
            seen=True
            # Closed schema: no arbitrary text, tool arguments, event details,
            # provider credentials, or tool evidence are copied to the archive.
            steps=task.get("steps",[])
            cursor=task.get("cursor",0)
            if not isinstance(steps,list) or type(cursor) is not int or not 0<=cursor<=len(steps):
                raise ValueError("invalid task progress")
            summary={"phase":"task_state","state":state,"step_count":len(steps),"cursor":cursor}
            if text_pending:
                request=agent_trace.request_text(task)
                if request:
                    events.append(request)
            if trace_enabled:
                trace_rows,more=agent_trace.page(task,after)
                events.extend(trace_rows)
        except EndpointError as exc:
            if exc.status==404 and (wait_for_task or seen):
                summary={"phase":"task_temporarily_missing" if seen else "waiting_for_submission"}
            elif exc.status==0 or exc.status in TRANSIENT_HTTP:
                summary={"phase":"agent_unavailable"}
            else:
                raise
        for event in events:
            closed=append_observation(recorder,path,event,deadline,interval)
            if closed:
                return closed
            if event["kind"]=="agent_request":
                text_pending=False
            else:
                after=event["source_seq"]
        if summary!=previous:
            serial+=1
            event={"kind":"agent_state","task_id":task_id,"event_id":f"watch-{watch_id}-{serial}",
                   "stamp_ns":time.time_ns(),"clock_domain":"observer_unix_ns",**summary}
            closed=append_observation(recorder,path,event,deadline,interval)
            if closed:
                return closed
            previous=summary
            print(json.dumps({"recording_id":rid,**summary},ensure_ascii=False),flush=True)
        if more:
            continue
        if summary.get("state") in TERMINAL:
            if finish:
                return finish_recording(recorder,path,task_id,summary["state"],"Agent terminal state observed")
            return endpoint(recorder,path)
        time.sleep(min(interval,max(0,deadline-time.monotonic())))
    raise TimeoutError("watch timed out; the recording is still owned by the recorder")


def main(argv=None):
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--recorder",default=os.environ.get("EDGE_RECORDER_URL","http://127.0.0.1:8188"))
    commands=cli.add_subparsers(dest="command",required=True)
    for name in ("start","status","stop","export","watch"):
        command=commands.add_parser(name)
        command.add_argument("recording_id")
        if name=="start":
            command.add_argument("--task-id",required=True)
            command.add_argument("--sources",nargs="+",default=["agent_state"])
        if name=="watch":
            command.add_argument("--agent",required=True,help="Agent base URL or a Console proxy for /agent/tasks")
            command.add_argument("--wait-for-task",action="store_true",help="allow 404 before submission")
            command.add_argument("--finish",action="store_true",help="close the recording on the observed terminal state")
            command.add_argument("--interval",type=finite_positive,default=.5)
            command.add_argument("--timeout",type=finite_positive,default=300)
    args=cli.parse_args(argv)
    path="/recordings/"+quote(args.recording_id,safe="")
    try:
        if args.command=="start":
            result=endpoint(args.recorder,"/recordings/start",{"recording_id":args.recording_id,
                            "task_id":args.task_id,"sources":args.sources})
        elif args.command=="status":
            result=endpoint(args.recorder,path)
        elif args.command=="export":
            result=endpoint(args.recorder,path+"/export",{})
        elif args.command=="stop":
            summary=endpoint(args.recorder,path)
            if summary["status"]=="ACTIVE":
                result=finish_recording(args.recorder,path,summary["task_id"],"STOPPED","operator ended the recording")
            else:
                result=endpoint(args.recorder,path+"/export",{})
        else:
            result=watch(args.recorder,args.recording_id,args.agent,wait_for_task=args.wait_for_task,
                         finish=args.finish,interval=args.interval,timeout=args.timeout)
        print(json.dumps(result,ensure_ascii=False))
    except (ValueError, KeyError, EndpointError, TimeoutError) as exc:
        cli.exit(2,str(exc)+"\n")


if __name__=="__main__":
    main()
