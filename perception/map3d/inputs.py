"""Discover optional, bounded PointCloud2 subscriptions from operator-owned JSON files."""
from collections import Counter
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat

from rclpy.validate_full_topic_name import validate_full_topic_name

MAX_SOURCES = 4
EXTRA_VOXELS = 2048


@dataclass(frozen=True)
class Input:
    id: str
    name: str = ""
    topic: str = ""
    expected_frame: str = ""
    ttl_s: float = 3
    error: str = ""


def read_one(path):
    key = path.stem
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError()
            raw = stream.read(4097)
        if len(raw)>4096:
            raise ValueError()
        obj=json.loads(raw)
        if (not isinstance(obj,dict) or obj.get("id")!=key or obj.get("type")!="sensor_msgs/msg/PointCloud2"
                or not isinstance(obj.get("topic"),str) or len(obj["topic"])>255):
            raise ValueError()
        validate_full_topic_name(obj["topic"])
        title = obj.get("name", key)
        frame = obj.get("expected_frame", "")
        ttl = obj.get("ttl_s",3)
        if (not isinstance(title,str) or not 1<=len(title)<=96 or not title.isprintable()
                or not isinstance(frame,str) or (frame and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_/.-]{0,127}",frame))
                or type(ttl) not in (float,int) or not math.isfinite(ttl) or not .1<=ttl<=120):
            raise ValueError()
        return Input(key,title,obj["topic"],frame,float(ttl))
    except (ValueError,TypeError,OSError,RuntimeError,RecursionError,OverflowError):
        return Input(key,error="manifest_invalid")


def discover(directory, primary):
    result={}
    try:
        with os.scandir(directory) as entries:
            for index,entry in enumerate(entries):
                if index>=64:
                    raise ValueError("input_catalog_limit")
                path=Path(entry.path)
                if path.suffix != ".json":
                    continue
                if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,62}",path.stem) or len(result)>=MAX_SOURCES:
                    raise ValueError("input_catalog_limit")
                result[path.stem]=read_one(path)
    except FileNotFoundError:
        return result
    topics=Counter(cfg.topic for cfg in result.values() if not cfg.error)
    for key,cfg in result.items():
        if not cfg.error and (cfg.topic==primary or topics[cfg.topic]>1):
            result[key]=Input(key,cfg.name,cfg.topic,cfg.expected_frame,cfg.ttl_s,"topic_conflict")
    return result
