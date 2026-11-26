"""Play only the human board holder in the fixed Gazebo station during a real Agent task."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen


def operate(task_id, output, timeout, accepted_file=None, initial_delay=20, wait_for_retry=False):
    output.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    current = 0
    history = []
    previous = None
    first_request = None
    resumed = not wait_for_retry
    awaiting_adjustment_ack = False
    spare_poses = iter((9,10,11,12))
    accepted_at_placement = 0
    def position(index):
        subprocess.run(["docker","exec","edge-board-cal-cal-sim-1","/usr/local/bin/edge-entrypoint",
                        "python3","/fixture/board.py","present",str(index)],check=True)
        history.append(dict(kind="board_pose", pose=index, elapsed_s=round(time.monotonic()-start,2)))
    def save():
        tmp = output.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(output)
    try:
        while time.monotonic() - start < timeout:
            if accepted_file and not task_id:
                try:
                    task_id = json.loads(accepted_file.read_bytes())["id"]
                except (FileNotFoundError,json.JSONDecodeError):
                    time.sleep(.15)
                    continue
            try:
                with urlopen("http://127.0.0.1:8911/api/calibration-status",timeout=5) as response:
                    facts=json.load(response)
                with urlopen("http://127.0.0.1:8911/agent/tasks?id="+task_id,timeout=5) as response:
                    task=json.load(response)
            except (URLError,TimeoutError,ConnectionError):
                time.sleep(.25)
                continue
            if facts.get("task_id") != task_id:
                time.sleep(.2)
                continue
            phase, count = facts["phase"],facts["samples"]
            if phase!="ADJUST_BOARD":
                awaiting_adjustment_ack = False
            if not resumed:
                if task.get("state") in ("PLANNING","RUNNING","WAITING","QUEUED","RETRYING"):
                    resumed = True
                else:
                    time.sleep(.15)
                    continue
            if task.get("state") in ("FAILED","BLOCKED","CANCELED","RECOVERY_REQUIRED","REJECTED"):
                raise RuntimeError("agent ended without calibration: " + str(task.get("detail")))
            if previous != (phase,count):
                history.append(dict(kind="read_station",phase=phase,samples=count,
                                    elapsed_s=round(time.monotonic()-start,2)))
                save()
                previous = (phase,count)
            if facts["state"]=="VERIFIED" and phase=="VERIFIED":
                if count<8 or not any(h.get("phase")=="MORE_VIEWS" and h["samples"]==1 for h in history):
                    raise RuntimeError("expected verified distinct poses and duplicate-pose feedback")
                return
            if facts["state"]=="RECOVERY_REQUIRED" or phase=="FAILED":
                raise RuntimeError(facts.get("message"))
            # The first held board deliberately stays until the learner rejects it as a duplicate.
            if first_request is None and phase=="WAITING_FOR_BOARD":
                first_request = time.monotonic()
            next_pose = None
            if current==0 and first_request is not None and time.monotonic()-first_request>=initial_delay:
                next_pose = 1
            elif current==1 and count==1 and phase=="MORE_VIEWS":
                next_pose = 2
            elif current>1 and accepted_at_placement<count<8:
                next_pose = count+1
            elif current and phase=="ADJUST_BOARD" and not awaiting_adjustment_ack:
                next_pose = next(spare_poses)
                awaiting_adjustment_ack = True
                history.append(dict(kind="holder_adjustment",samples=count,from_pose=current,
                                    to_pose=next_pose,reason=facts["message"]))
            if next_pose is not None:
                current = next_pose
                accepted_at_placement = count
                position(current)
                save()
            time.sleep(.15)
        raise TimeoutError("timed out waiting for calibration feedback")
    finally:
        if current:
            position(0)
            save()


if __name__ == "__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    selection = cli.add_mutually_exclusive_group(required=True)
    selection.add_argument("--task-id")
    selection.add_argument("--accepted-file",type=Path,
                           help="Arm before submission and follow the browser's actual task ID")
    cli.add_argument("output",type=Path)
    cli.add_argument("--timeout",type=int,default=1100)
    cli.add_argument("--initial-delay",type=float,default=20,
                     help="Seconds for the simulated user to bring the board after the station asks")
    cli.add_argument("--wait-for-retry",action="store_true",
                     help="Wait for the existing task to resume before following its station guidance")
    args=cli.parse_args()
    operate(args.task_id,args.output,args.timeout,args.accepted_file,args.initial_delay,args.wait_for_retry)
