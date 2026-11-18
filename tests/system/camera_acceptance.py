"""Exercise independent Gazebo RGB, a second ROS JPEG source, map-free preview, and hot configuration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError,URLError
from urllib.request import urlopen
import uuid
import zlib

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"perception/camera"))
from test_frames import JPEG


def wait(read,deadline=50):
    end=time.monotonic()+deadline
    while time.monotonic()<end:
        try:
            result=read()
            if result:
                return result
        except (OSError,URLError,ValueError,StopIteration):
            pass
        time.sleep(.2)
    raise AssertionError("camera state timeout")


def require(condition,reason):
    if not condition:
        raise AssertionError(reason)


def publish():
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CompressedImage
    rclpy.init()
    node=Node("camera_fixture",parameter_overrides=[Parameter("use_sim_time",value=True)])
    pub=node.create_publisher(CompressedImage,"/fixture/camera/jpeg",qos_profile_sensor_data)
    end=time.monotonic()+10
    while time.monotonic()<end:
        rclpy.spin_once(node,timeout_sec=.05)
        ns=node.get_clock().now()
        if ns.nanoseconds and pub.get_subscription_count():
            msg=CompressedImage()
            msg.header.stamp=ns.to_msg()
            msg.header.frame_id="fixture_optical"
            msg.format="jpeg"
            msg.data=JPEG
            pub.publish(msg)
        time.sleep(.06)
    node.destroy_node()
    rclpy.shutdown()


def run(image,out):
    project="edge-camera-"+uuid.uuid4().hex[:12]
    report={"passed":False,"project":project,"image":image,"scope":"Gazebo RGB and CameraInfo, synthetic ROS JPEG; desktop Linux containers"}
    with tempfile.TemporaryDirectory() as tmp:
        folder=Path(tmp)
        src=folder/"sources.json"
        base=json.loads((ROOT/"perception/camera/config/sources.json").read_bytes())
        def install(value):
            staging=src.with_suffix(".next")
            staging.write_bytes(json.dumps(value).encode())
            staging.replace(src)
        install(base)
        env={**os.environ,"EDGE_IMAGE":image,"EDGE_CAMERA_PORT":"0","EDGE_CAMERA_CONFIG_DIR":str(folder),
             "EDGE_ROS_DOMAIN_ID":"155","COMPOSE_DISABLE_ENV_FILE":"1"}
        dc=["docker","compose","-p",project,"-f",str(ROOT/"compose.yaml"),"-f",str(ROOT/"compose.camera.yaml"),
            "--profile","camera-sensor"]
        def stack(*args):
            return subprocess.check_output([*dc,*args],cwd=ROOT,env=env,text=True,encoding="utf-8",errors="replace").strip()
        child=None
        try:
            stack("up","-d","--no-build","sim","bridge","camera","camera-sensor")
            stack("exec","-T","camera","python3","-B","/opt/edge/perception/camera/test_http_clock.py","-v")
            report["camera_read_clock_regression"]="passed"
            baseurl="http://"+stack("port","camera","8771")
            def read(path):
                with urlopen(baseurl+path,timeout=2) as response:
                    return response.read(),dict(response.headers)
            def records():
                return json.loads(read("/api/cameras")[0])
            def state(id_,state_):
                return next((s for s in records()["sources"] if s["id"]==id_ and s["state"]==state_),None)
            front=wait(lambda:state("front","online"))
            front=wait(lambda:state("front","online") if (state("front","online") or {}).get("camera_info") else None)
            require((front["width"],front["height"],front["frame_id"])==(320,240,"edge_camera_front"),"Gazebo source")
            png,head=read("/api/cameras/front/image")
            p=png.index(b"IDAT")
            raw=zlib.decompress(png[p+4:p+4+struct.unpack(">I",png[p-4:p])[0]])
            bins=len(set(raw))
            require(len(raw)==240*(320*3+1) and bins>8 and head["X-Frame-Id"]=="edge_camera_front","actual RGB pixels")
            out.parent.mkdir(parents=True,exist_ok=True)
            out.with_suffix(".png").write_bytes(png)
            report["gazebo"]={**{k:front[k] for k in ("width","height","frame_id","encoding","camera_info")},
                               "png_sha256":hashlib.sha256(png).hexdigest(),"unique_sample_bytes":bins,"preview_stamp_ns":head["X-Frame-Stamp-Ns"]}
            extra={"id":"jpeg-fixture","type":"CompressedImage","topic":"/fixture/camera/jpeg","ttl_s":2}
            install({"schema_version":1,"sources":[*base["sources"],extra]})
            wait(lambda:state("jpeg-fixture","waiting"))
            # The container uses its own ROS environment and the identical portable fixture.
            container=stack("ps","-q","camera")
            remote=Path(__file__).read_text(encoding="utf-8").replace('ROOT=Path(__file__).resolve().parents[2]','ROOT=Path("/opt/edge")')
            child=subprocess.Popen(["docker","exec","-i",container,"/usr/local/bin/edge-entrypoint","python3","-B","-","--publish"],
                                   stdin=subprocess.PIPE,stdout=subprocess.DEVNULL)
            child.stdin.write(remote.encode())
            child.stdin.close()
            jpeg=wait(lambda:state("jpeg-fixture","online"),20)
            wire,head=read("/api/cameras/jpeg-fixture/image")
            require(wire==JPEG and head["Content-Type"]=="image/jpeg" and jpeg["width"]==2,"independent JPEG source")
            require(state("front","online"),"Gazebo source lost during hot add")
            install({"schema_version":1,"sources":[extra,extra]})
            wait(lambda:records()["config_status"]=="invalid")
            require(state("front","online"),"bad config removed existing stream")
            install(base)
            wait(lambda:records()["config_status"]=="ok" and len(records()["sources"])==1)
            try:
                read("/api/cameras/jpeg-fixture/image")
                raise AssertionError("removed source served")
            except HTTPError as error:
                require(error.code==404,"removed source status")
            report["hot_config"]={"added_jpeg_frame":jpeg["frame_id"],"jpeg_sha256":hashlib.sha256(wire).hexdigest(),
                                  "invalid_kept_existing":True,"removed_http_status":404}
            stack("stop","camera-sensor")
            stale=wait(lambda:state("front","stale"))
            try:
                read("/api/cameras/front/image")
                raise AssertionError("stale bytes served")
            except HTTPError as error:
                require(error.code==503,"stale status")
            stack("start","camera-sensor")
            renewed=wait(lambda:state("front","online"))
            require(renewed["stamp_ns"]>stale["stamp_ns"],"old frame on recovery")
            report["outage"]={"reason":stale["reason"],"stale_http_status":503,"old_stamp":stale["stamp_ns"],"new_stamp":renewed["stamp_ns"]}
            report["passed"]=True
        finally:
            if child is not None:
                child.wait(timeout=16)
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
            stack("down","--volumes","--remove-orphans","--timeout","5")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish",action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--out",type=Path)
    args=parser.parse_args()
    if args.publish:
        publish()
    elif args.image and args.out:
        run(args.image,args.out)
    else:
        parser.error("use --image and --out")
