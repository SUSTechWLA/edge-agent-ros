"""Exercise the exact commissioned straight-line code with private DDS topics and synthetic MCU data."""
import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry


def run_fixture(vendor,case,direction):
    op=str(uuid.uuid4())
    base="/edge/commissioning/shadow_"+op.replace("-","")
    mux=rclpy.create_node("shadow_mux");mcu=rclpy.create_node("shadow_mcu")
    pub=mcu.create_publisher(Odometry,base+"/lingao/odom",10)
    requested=[0.]
    observed=[]
    def request(msg):
        requested[0]=msg.linear.x
        observed.append((time.monotonic(),msg.linear.x))
    mux.create_subscription(Twist,base+"/cmd_vel",request,10)
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        args=[sys.executable,"-m","calibration.lingao_trial_ros","--operation-id",op,"--robot-id","shadow-rig",
              "--direction",direction,"--vendor-file",str(vendor),"--shadow-root",folder]
        child=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        start=prior=time.monotonic();grant=dx=0.
        first_nonzero=None
        stalled_at=None
        resumed=False
        x_at_watchdog_stop=None
        try:
            while child.poll() is None:
                rclpy.spin_once(mux,timeout_sec=.005)
                now=time.monotonic()
                if now-start>14:raise TimeoutError(case)
                if now-start>3.4 and not grant:grant=now+.499
                if first_nonzero is None and requested[0]:first_nonzero=now
                if case=="controller_stall" and first_nonzero and not stalled_at:
                    child.send_signal(signal.SIGSTOP)
                    stalled_at=now
                permitted=bool(grant) and now<grant and case!="no_guard"
                takeover=case=="takeover" and first_nonzero and now-first_nonzero>.12
                estop=case=="estop" and first_nonzero and now-first_nonzero>.12
                velocity=requested[0] if permitted and not takeover and not estop else 0.
                if case=="no_motion":velocity=0.
                if case=="wrong_way":velocity=-velocity
                if case=="no_stop" and first_nonzero and not permitted:velocity=.008
                dx+=(now-prior)*velocity
                if stalled_at and now>=grant:
                    if x_at_watchdog_stop is None: x_at_watchdog_stop=dx
                    if not resumed and now-grant>.2:
                        assert dx==x_at_watchdog_stop and abs(dx)<=.0115
                        child.send_signal(signal.SIGCONT)
                        resumed=True
                prior=now
                if not (case=="stale" and first_nonzero and now-first_nonzero>.12):
                    msg=Odometry()
                    msg.header.stamp=mcu.get_clock().now().to_msg()
                    msg.header.frame_id="odom";msg.child_frame_id="wrong_frame" if case=="wrong_frame" else "base_foot"
                    msg.pose.pose.orientation.w=1.;msg.pose.pose.position.x=dx;msg.twist.twist.linear.x=velocity
                    pub.publish(msg)
                if permitted:
                    data=dict(schema="edge.lingao.directional-lease.v1",robot_id="shadow-rig",operation_id=op,boot_id=boot_id,
                              direction="forward" if case=="wrong_guard" else direction,
                              rear_or_front_stop_verified=True,clear=True,
                              takeover_active=bool(takeover),estop_active=bool(estop),watchdog_armed=True,
                              observed_monotonic_ns=time.monotonic_ns(),hard_stop_monotonic_ns=int(grant*1e9))
                    tmp=root/"next"
                    tmp.write_text(json.dumps(data))
                    os.replace(tmp,root/(op+".json"))
                time.sleep(.008)
            output=child.communicate()[0]
            last=output.strip().splitlines()[-1]
            if not last.startswith("{"):raise ValueError(output)
            result=json.loads(last)
            assert (child.returncode==0)==(result["state"]=="VERIFIED")
            assert (result["state"]=="VERIFIED")== (case=="ok"),(case,result)
            # Replaying the identical immutable operation must fail before a publisher is created.
            again=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=5)
            assert again.returncode!=0 and "UNIQUE constraint" in again.stdout
            with sqlite3.connect(root/"operations.sqlite3") as db:
                assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0]==1
            assert all(abs(v)<=.02 for _,v in observed)
            assert all((v<=0 if direction=="reverse" else v>=0) for _,v in observed)
            if case in ("no_guard","wrong_guard","wrong_frame"):assert not observed
            if case in ("takeover","estop"):assert not any(v for t,v in observed if t>first_nonzero+.20)
            assert result["schema"]=="edge.lingao.relative-trial.shadow.v1" and result["environment"]=="shadow"
            if case=="controller_stall":assert resumed
            result.update(fixture_case=case,replay_denied=True,observed_commands=len(observed),
                          fixture_x_at_independent_cutoff_m=x_at_watchdog_stop)
            return result
        finally:
            if child.poll() is None:child.kill();child.wait()
            mcu.destroy_node();mux.destroy_node()


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("vendor",type=Path)
    p.add_argument("--out",type=Path,required=True)
    args=p.parse_args()
    rclpy.init(args=[])
    try:
        rows=[]
        for direction,case in [("reverse","ok"),("forward","ok"),("reverse","no_guard"),("reverse","no_motion"),
                               ("reverse","wrong_way"),("reverse","stale"),("reverse","takeover"),("reverse","no_stop"),
                               ("reverse","estop"),("reverse","wrong_guard"),("reverse","wrong_frame"),("reverse","controller_stall")]:
            row=run_fixture(args.vendor,case,direction)
            rows.append(row)
            print(case,direction,row["state"],row["reason"],row["signed_progress_m"],flush=True)
            args.out.write_text(json.dumps({"environment":"isolated_dds_synthetic_mcu","trials":rows},indent=2)+"\n")
    finally:rclpy.shutdown()
