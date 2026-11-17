"""Named-place capability with a durable binding to the original waypoint plan."""
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import rclpy
from edge_interfaces.srv import GetMission, SubmitMission
from geometry_msgs.msg import Point
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import String
from layout import Layout, RouteError


class Pending(Exception):
    pass


class Scene(Node):
    def __init__(self, layout, directory):
        super().__init__("edge_semantic_scene")
        self.layout = layout
        self.position = None
        self.create_subscription(Odometry,"/edge/perception/pose",self.observe,qos_profile_sensor_data)
        self.publisher = self.create_publisher(String,"/edge/semantic_map",1)
        self.create_timer(0.8,self.publish)
        self.get_mission = self.create_client(GetMission,"/edge/get_mission")
        self.submit_mission = self.create_client(SubmitMission,"/edge/submit_mission")
        directory.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(directory/"routes.db",check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS routes
            (op TEXT PRIMARY KEY, place TEXT NOT NULL, plan TEXT NOT NULL)""")
        self.lock = threading.Lock()

    def observe(self,msg):
        if msg.header.frame_id == self.layout.data["frame"]:
            p=msg.pose.pose.position
            stamp=(msg.header.stamp.sec,msg.header.stamp.nanosec)
            if not self.position or stamp!=self.position[2]:
                self.position=((p.x,p.y),time.monotonic(),stamp)

    def sample(self):
        p = self.position
        return p[0] if p and time.monotonic()-p[1]<1.5 else None

    def describe(self):
        pos=self.sample()
        return {**self.layout.data,"digest":self.layout.digest,
                "current_zone":self.layout.zone(pos) if pos else None}

    def publish(self):
        self.publisher.publish(String(data=json.dumps(self.describe(),ensure_ascii=False)))

    def call(self,client,request):
        if not client.wait_for_service(timeout_sec=.4):
            raise Pending("ROS 任务内核暂不可用")
        future=client.call_async(request)
        done=threading.Event()
        future.add_done_callback(lambda _:done.set())
        if not done.wait(2):
            client.remove_pending_request(future)
            raise Pending("ROS 回执超时，将按原操作 ID 对账")
        return future.result()

    def navigate(self,op,place):
        with self.lock:
            row=self.db.execute("SELECT place,plan FROM routes WHERE op=?",(op,)).fetchone()
            if row:
                if row[0]!=place:
                    raise RouteError("scene.operation_conflict","操作 ID 已绑定其他目的地")
                plan=json.loads(row[1])
            else:
                pos=self.sample()
                if pos is None:
                    raise Pending("等待地图坐标系中的新鲜定位")
                path=self.layout.route(pos,place)
                plan={"scene":self.layout.data["id"],"revision":self.layout.data["revision"],
                      "digest":self.layout.digest,"path":path,"frame":self.layout.data["frame"],
                      "waypoints":[self.layout.nodes[k]["xy"] for k in path]}
                with self.db:
                    self.db.execute("INSERT INTO routes VALUES (?,?,?)",(op,place,json.dumps(plan)))
            status=self.call(self.get_mission,GetMission.Request(mission_id=op))
            if not status.found:
                req=SubmitMission.Request(mission_id=op,waypoints=[Point(x=float(x),y=float(y)) for x,y in plan["waypoints"]])
                result=self.call(self.submit_mission,req)
                if not result.accepted:
                    if "occupied" in result.detail:
                        raise Pending(result.detail)
                    raise RouteError("scene.dispatch_rejected",result.detail)
                return {"state":"RUNNING","route":plan}
            if status.state=="SUCCEEDED" and status.evidence:
                pt=status.evidence[-1]
                return {"state":"SUCCEEDED","evidence":{**plan,"place":place,"observed_xy":[pt.x,pt.y],
                        "verified_count":status.verified_count,"mission_id":op}}
            if status.state in ("RUNNING","CANCELING"):
                return {"state":"RUNNING","route":plan}
            return {"state":status.state,"detail":status.detail,
                    "error":{"code":"scene.motion_incomplete","message":status.detail,"details":{"route":plan}}}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        op=""
        status=200
        try:
            size=int(self.headers.get("Content-Length",0))
            if not 0<size<=16384:
                raise ValueError("invalid length")
            body=json.loads(self.rfile.read(size))
            op=body["operation_id"]
            if not isinstance(op,str) or not 0<len(op)<=256 or body.get("version")!="1":
                raise ValueError("invalid operation or version")
            if self.path=="/describe":
                result={"state":"SUCCEEDED","evidence":self.server.scene.describe()}
            elif self.path=="/navigate":
                place=body["arguments"]["place"]
                if not isinstance(place,str) or not 0<len(place)<=120:
                    raise ValueError("invalid place")
                result=self.server.scene.navigate(op,place)
            else:
                status,result=404,{"detail":"unknown capability"}
        except RouteError as exc:
            result={"state":"BLOCKED","detail":str(exc),"error":{"code":exc.code,"message":str(exc)}}
        except Pending as exc:
            status,result=503,{"detail":str(exc)}
        except (ValueError,TypeError,KeyError) as exc:
            status,result=400,{"detail":str(exc)}
        data=json.dumps({"operation_id":op,**result},ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__=="__main__":
    rclpy.init()
    scene=Scene(Layout(os.environ.get("EDGE_SCENE_FILE","/opt/edge/scene/campus.json")),Path("/var/lib/edge-scene"))
    threading.Thread(target=rclpy.spin,args=(scene,),daemon=True).start()
    server=ThreadingHTTPServer(("0.0.0.0",8092),Handler)
    server.scene=scene
    server.serve_forever()
