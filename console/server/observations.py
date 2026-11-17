"""Local ROS observation manifests and bounded previews for installed message types."""
import json
import math
import re
import time
from dataclasses import dataclass
from numbers import Integral, Real
from pathlib import Path

from rclpy.qos import QoSProfile, ReliabilityPolicy
from rosidl_runtime_py.utilities import get_message


def preview(message):
    """Walk only the displayed prefix. Never turn a camera frame into a Python list."""
    budget = 192
    truncated = False

    def visit(value, depth):
        nonlocal budget, truncated
        budget -= 1
        if isinstance(value, (bool,str)):
            if isinstance(value,str) and len(value)>256:
                truncated = True
                return value[:256]
            return value
        if isinstance(value, Integral):
            return int(value)
        if isinstance(value, Real):
            return float(value) if math.isfinite(value) else str(float(value))
        if depth >= 8:
            truncated = True
            return None
        if hasattr(value,"get_fields_and_field_types"):
            result = {}
            for key in value.get_fields_and_field_types():
                if budget <= 0:
                    truncated = True
                    break
                result[key] = visit(getattr(value,key),depth+1)
            return result
        if hasattr(value,"__len__") and hasattr(value,"__getitem__"):
            length = len(value)
            result = []
            for index in range(min(length,32)):
                if budget <= 0:
                    break
                result.append(visit(value[index],depth+1))
            truncated |= len(result)<length
            return result
        if hasattr(value,"item"):  # numpy boolean scalar in a fixed ROS boolean array
            return visit(value.item(),depth+1)
        raise ValueError(f"unsupported ROS field of type {type(value).__name__}")

    data = visit(message,0)
    return data, truncated


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str = ""
    topic: str = ""
    type: str = ""
    ttl_s: float = 0
    error: str = ""

    @classmethod
    def load(cls, path):
        key = path.stem
        try:
            with path.open("rb") as file:
                raw = file.read(8193)
            if len(raw)>8192:
                raise ValueError("observation manifest exceeds 8 KiB")
            obj = json.loads(raw)
            if not isinstance(obj,dict):
                raise ValueError("expected a JSON object")
            key = obj["id"]
            if not isinstance(key,str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}",key):
                key = path.stem
                raise ValueError("invalid observation id")
            name,topic,typ,ttl = obj.get("name",key),obj["topic"],obj["type"],obj["ttl_s"]
            if not isinstance(name,str) or not 0<len(name)<=128:
                raise ValueError("name must contain 1 to 128 characters")
            if not isinstance(topic,str) or not topic.startswith("/") or len(topic)>255:
                raise ValueError("topic must be an absolute ROS topic name")
            if not isinstance(typ,str) or not re.fullmatch(r"[a-z][a-z0-9_]*/msg/[A-Z][A-Za-z0-9]*",typ):
                raise ValueError("type must be a package/msg/Message name")
            if isinstance(ttl,bool) or not isinstance(ttl,(int,float)) or not math.isfinite(ttl) or ttl<=0:
                raise ValueError("ttl_s must be a positive finite number")
            return cls(key,name,topic,typ,ttl)
        except (OSError,ValueError,KeyError,TypeError,RecursionError,OverflowError) as exc:
            return cls(key,str(key),error=f"{path.name}: {exc}")


class Observation:
    def __init__(self, config):
        self.config = config
        self.sub = None
        self.started = time.monotonic()
        self.received = self.sampled = self.data = self.stamp = self.frame = None
        self.truncated = False
        self.error = config.error
        self.last_status = "error" if self.error else "waiting"
        self.cause = None

    def update(self, message):
        data, truncated = preview(message)
        header = getattr(message,"header",None)
        stamp = (header.stamp.sec + header.stamp.nanosec*1e-9) if hasattr(header,"stamp") else None
        now = time.monotonic()
        if self.received is None or stamp is None or self.stamp!=stamp:
            self.sampled = now
        self.received, self.stamp = now, stamp
        self.frame = getattr(header,"frame_id",None)
        self.data, self.truncated = data,truncated
        self.error = ""

    def view(self, now, sim_clock):
        cfg = self.config
        age = now-self.sampled if self.sampled is not None else None
        if age is not None and self.stamp is not None and sim_clock is not None:
            age = max(age, sim_clock-self.stamp)
        fresh = not self.error and age is not None and age<=cfg.ttl_s
        status = "error" if self.error else "online" if fresh else "stale"
        if not self.error and age is None and now-self.started<=cfg.ttl_s:
            status = "waiting"
        return {"name":cfg.name,"topic":cfg.topic,"type":cfg.type,"ttl_s":cfg.ttl_s,"frame":self.frame,
                "stamp_s":self.stamp,"available":self.received is not None,"fresh":fresh,"status":status,
                "age_s":round(age,4) if age is not None else None, "error":self.error or None,
                "receive_age_s":round(now-self.received,4) if self.received is not None else None,
                "truncated":self.truncated,"data":self.data}


class Observations:
    def __init__(self, observer, directory):
        self.node = observer
        self.directory = Path(directory)
        self.items = {}
        self.next_reload = 0
        self.qos = QoSProfile(depth=1,reliability=ReliabilityPolicy.BEST_EFFORT)

    def snapshot(self, now):
        return {key:item.view(now,self.node.sim_clock) for key,item in self.items.items()}

    def record(self, item, kind, detail):
        cfg = item.config
        state = item.view(time.monotonic(),self.node.sim_clock)
        data = {key:state[key] for key in ("type","status","age_s","ttl_s","stamp_s","receive_age_s","error")}
        data.update(id=cfg.id,topic=cfg.topic)
        item.cause = self.node.event("observation."+kind,cfg.topic or "edge_console",detail,
                                     "warn" if kind in ("stale","error") else "info",data=data,cause=item.cause)

    def consume(self, item, msg):
        with self.node.lock:
            if self.items.get(item.config.id) is not item:
                return
            try:
                item.update(msg)
            except Exception as exc:  # isolate third-party generated message accessors
                item.error = f"{type(exc).__name__}: {exc}"[:512]

    def tick(self, now):
        if now>=self.next_reload:
            self.next_reload = now+1
            self.reload()
        for item in self.items.values():
            status = item.view(now,self.node.sim_clock)["status"]
            if item.last_status!=status:
                item.last_status = status
                text = {"online":"收到额外传感器采样","stale":"额外传感器超过采样时效","error":item.error}
                self.record(item,status,text.get(status,status))

    def reload(self):
        manifests = {}
        for path in sorted(self.directory.glob("*.json")):
            cfg = Manifest.load(path)
            if cfg.id in manifests:
                cfg = Manifest(cfg.id,cfg.id,error="duplicate observation id: "+cfg.id)
            manifests[cfg.id] = cfg
        for key,item in list(self.items.items()):
            if manifests.get(key) == item.config:
                continue
            if item.sub is not None:
                self.node.destroy_subscription(item.sub)
            del self.items[key]
            self.record(item,"removed","已注销额外传感器观测")
        for key,cfg in manifests.items():
            if key in self.items:
                continue
            item = self.items[key] = Observation(cfg)
            if not item.error:
                try:
                    cls = get_message(cfg.type)
                    item.sub = self.node.create_subscription(cls,cfg.topic,lambda msg,i=item:self.consume(i,msg),self.qos)
                except Exception as exc:  # errors from optional ROS packages or type support
                    item.error = f"{type(exc).__name__}: {exc}"[:512]
            if item.error:
                item.last_status = "error"
                self.record(item,"error",item.error)
            else:
                self.record(item,"added","已订阅额外传感器")
