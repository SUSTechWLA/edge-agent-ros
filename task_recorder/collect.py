"""Run the Agent watcher and optional ROS source for one already-admitted recording."""
import argparse
import signal
import subprocess
import sys
import time
from urllib.parse import quote

from .cli import endpoint, finite_positive


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("task_id")
    cli.add_argument("recording_id")
    cli.add_argument("--url",required=True)
    cli.add_argument("--agent",required=True)
    cli.add_argument("--software",action="store_true")
    cli.add_argument("--sim",action="store_true")
    cli.add_argument("--camera")
    cli.add_argument("--timeout",type=finite_positive,default=86400)
    a=cli.parse_args()
    path="/recordings/"+quote(a.recording_id,safe="")
    pfx=[sys.executable,"-u","-B","-m"]
    workers={}
    failure_reason="collector terminated before a terminal receipt"
    def interrupted(*_): raise InterruptedError("collector shutdown")
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGINT,interrupted)
    try:
        if not a.software:
            args=pfx+["task_recorder.ros_source","--url",a.url,"--task-id",a.task_id,"--recording-id",a.recording_id]
            if a.sim: args.append("--sim")
            if a.camera: args.extend(["--camera",a.camera])
            workers[subprocess.Popen(args)]="ROS observer"
        # The archive's declared sources select live state, historical events and optional text.
        observer=subprocess.Popen(pfx+["task_recorder.cli","--recorder",a.url,"watch",a.recording_id,
            "--agent",a.agent,"--wait-for-task","--finish","--timeout",str(a.timeout)])
        workers[observer]="Agent observer"
        while all(p.poll() is None for p in workers):
            time.sleep(.2)
        if endpoint(a.url,path)["status"]=="ACTIVE":
            proc=next(p for p in workers if p.returncode is not None)
            failure_reason=f"{workers[proc]} exited with code {proc.returncode} before a terminal receipt"
            raise RuntimeError(failure_reason)
    except BaseException:
        if endpoint(a.url,path)["status"]=="ACTIVE":
            endpoint(a.url,path+"/finish",{"task_id":a.task_id,"status":"INTERRUPTED","reason":failure_reason})
        raise
    finally:
        for proc in workers:
            if proc.poll() is None: proc.terminate()
        for proc in workers:
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__=="__main__":
    main()
