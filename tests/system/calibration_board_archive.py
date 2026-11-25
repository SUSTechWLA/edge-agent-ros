"""Independently inspect the accepted recording set. No solver or planner imports."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib
from zipfile import ZipFile

import numpy as np


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def png(rgb):
    h, w, channels = rgb.shape
    if channels != 3 or rgb.dtype != np.uint8:
        raise ValueError("recording is not an RGB8 image")
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag+data))
    rows=b"".join(b"\x00"+np.ascontiguousarray(row).tobytes() for row in rgb)
    return (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",w,h,8,2,0,0,0))+
            chunk(b"IDAT",zlib.compress(rows))+chunk(b"IEND",b""))


def inspect(state, profile, task_id, out, bundle=False):
    ledger = json.loads((state/"sessions.json").read_bytes())
    publication = ledger["published"]
    revision = publication["revision"]
    content = {k:v for k,v in publication.items() if k!="revision"}
    encoded=json.dumps(content, sort_keys=True, separators=(",",":"), ensure_ascii=False,allow_nan=False).encode()
    if digest(encoded)!=revision or publication["task_id"]!=task_id or publication["environment"]!="simulation":
        raise ValueError("publication is not the selected simulation task")
    if not re.fullmatch(r"[\w.-]+",task_id) or task_id in {"..","."}:
        raise ValueError("unsafe task id")
    session = ledger["tasks"][task_id]
    if session["phase"]!="VERIFIED" or session["revision"]!=revision:
        raise ValueError("unverified calibration")
    training, holdout=map(set,(publication["training_ids"],publication["validation_ids"]))
    if training & holdout or len(training)<6 or len(holdout)<2:
        raise ValueError("independent holdout is missing")
    rows=[]
    frames={"camera":profile["camera"]["frame"], **profile["solver"]["lidar_frames"]}
    seen=set()
    out.mkdir(parents=True,exist_ok=True)
    for pose in session["poses"]:
        ref=pose["sha256"]
        if not re.fullmatch("[0-9a-f]{64}",ref):
            raise ValueError("invalid archive reference")
        path=state/"captures"/task_id/(ref+".npz")
        raw=path.read_bytes()
        if digest(raw)!=ref:
            raise ValueError("recording digest differs from receipt")
        with np.load(path,allow_pickle=False) as record:
            meta=json.loads(str(record["metadata"]))
            stamp=str(meta["stamp_ns"]); stamps=meta["stamps_ns"]
            span=max(stamps.values())-min(stamps.values())
            if (stamp in seen or stamp!=pose["obs"]["id"] or meta["frames"]!=frames or
                set(stamps)!=set(frames)|{"info"} or span>profile["max_skew_ns"]):
                raise ValueError("sensor identity or synchronization mismatch")
            clouds = {}
            for role in profile["solver"]["lidar_frames"]:
                cloud=record["cloud_"+role]
                if cloud.ndim!=2 or cloud.shape[1]!=3 or not np.all(np.isfinite(cloud)) or not len(cloud):
                    raise ValueError("empty or invalid recorded LiDAR geometry")
                clouds[role]=len(cloud)
            if not rows:
                (out/"first-recorded-frame.png").write_bytes(png(record["rgb"]))
            seen.add(stamp)
            rows.append(dict(sha256=ref,bytes=len(raw),observation_id=stamp,stream_stamps_ns=stamps,
                             skew_ns=span,frame_ids=meta["frames"],rgb_shape=list(record["rgb"].shape),
                             point_counts=clouds,set="fit" if stamp in training else "holdout"))
    if seen!=training|holdout or set(r["sha256"] for r in rows)!=set(publication["samples"]):
        raise ValueError("published training/holdout do not match the recording set")
    (out/"recordings-audit.json").write_text(json.dumps(dict(task_id=task_id,revision=revision,
        records=rows,fit_count=len(training),holdout_count=len(holdout),passed=True),
        indent=2,ensure_ascii=False),encoding="utf-8")
    if bundle:
        tmp=out/"recordings.partial"
        with ZipFile(tmp,"w") as archive:
            for row in rows:
                name=row["sha256"]+".npz"
                archive.write(state/"captures"/task_id/name,"frames/"+name)
            archive.write(out/"recordings-audit.json","recordings-audit.json")
            archive.writestr("sensor-profile.json",json.dumps(profile,ensure_ascii=False,indent=2))
            archive.writestr("verified-extrinsics.json",json.dumps(publication,ensure_ascii=False,indent=2))
        tmp.replace(out/"recordings.zip")
    print(json.dumps(publication["report"]["transforms"],allow_nan=False))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state",type=Path)
    parser.add_argument("profile",type=Path)
    parser.add_argument("task_id")
    parser.add_argument("out",type=Path)
    parser.add_argument("--bundle",action="store_true")
    args=parser.parse_args()
    inspect(args.state,json.loads(args.profile.read_bytes()),args.task_id,args.out,args.bundle)
