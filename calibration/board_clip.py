"""Stationary board capture using source timestamps. Original observations stay lossless."""
import base64
from io import BytesIO
import math
import struct

import numpy as np


def stationary(first, current, limits):
    n = np.asarray(first["normal"])
    angle = np.dot(n,current["normal"])
    if angle < math.cos(math.radians(limits["max_rotation_deg"])):
        return False
    if np.linalg.norm(np.subtract(first["center_m"],current["center_m"])) > limits["max_drift_m"]:
        return False
    if "holes_m" in first:
        # The cloud's four holes have no intrinsic ordering.
        a,b = np.asarray(first["holes_m"]),np.asarray(current["holes_m"])
        if not np.isfinite(a).all() or not np.isfinite(b).all():
            return False
        distances = np.linalg.norm(a[:,None,:] - b[None,:,:],axis=2)
        if max(distances.min(axis=1).max(), distances.min(axis=0).max()) > limits["max_drift_m"]:
            return False
    return True


class Window:
    def __init__(self, limits):
        self.limits = limits
        self.frames = []

    def clear(self):
        self.frames.clear()

    def add(self, archive, detection, jpeg, size, camera, stamps):
        stamp = stamps["camera"]
        if self.frames:
            first, last = self.frames[0], self.frames[-1]
            continuous = (set(stamps) == set(last["stamps"]) and
                          all(0 < value-last["stamps"][key] <= self.limits["max_gap_s"]*1e9
                              for key,value in stamps.items()))
            if not continuous or not stationary(first["camera"],camera,self.limits) or last["size"] != size:
                self.clear()
            elif stamp-last["stamps"]["camera"] < 160_000_000:
                return
        self.frames.append(dict(archive=archive, detection=detection, jpeg=jpeg, size=size,
                                camera=camera,stamps=stamps))

    def progress(self):
        elapsed = (self.frames[-1]["stamps"]["camera"] - self.frames[0]["stamps"]["camera"])/1e9 if self.frames else 0
        return dict(active=bool(self.frames), frames=len(self.frames),duration_s=round(elapsed,3),
                    required_frames=self.limits["frames"],required_duration_s=self.limits["duration_s"])

    def ready(self):
        return (len(self.frames) >= self.limits["frames"] and
                self.frames[-1]["stamps"]["camera"] - self.frames[0]["stamps"]["camera"] >=
                self.limits["duration_s"] * 1e9)


def cloud_context(frames, max_points=100_000):
    """Send only prior XYZ windows to compute, keeping RGB and full raw scans in the archive."""
    clouds = {}
    for row in frames:
        with np.load(BytesIO(base64.b64decode(row["archive"], validate=True)), allow_pickle=False) as bundle:
            for key in bundle.files:
                if key.startswith("cloud_"):
                    clouds.setdefault(key,[]).append(bundle[key])
    pooled = {}
    for key,arrays in clouds.items():
        points = np.concatenate(arrays)
        if len(points) > max_points:
            points = points[np.linspace(0,len(points)-1,max_points,dtype=int)]
        pooled[key] = points.astype(np.float32)
    out = BytesIO()
    np.savez_compressed(out,**pooled)
    return base64.b64encode(out.getvalue()).decode("ascii")


def avi(frames):
    """Encode a standard Motion JPEG AVI with source-derived playback rate; NPZ retains full timing."""
    w,h = frames[0]["size"]
    images = [row["jpeg"] for row in frames]
    delta_ns = frames[-1]["stamps"]["camera"] - frames[0]["stamps"]["camera"]
    us = max(1, round(delta_ns/(len(images)-1)/1000))
    largest = max(map(len,images))

    def chunk(tag, data):
        return tag + struct.pack("<I",len(data)) + data + (b"\0" if len(data)%2 else b"")

    def group(tag, data):
        return chunk(b"LIST",tag + data)

    avih = struct.pack("<14I",us,round(largest*1e6/us),0,0x10,len(images),0,1,largest,w,h,0,0,0,0)
    strh = struct.pack("<4s4sIHH8I4h",b"vids",b"MJPG",0,0,0,0,us,1000000,0,len(images),largest,
                       0xffffffff,0,0,0,w,h)
    strf = struct.pack("<IiiHH4sIiiII",40,w,h,1,24,b"MJPG",w*h*3,0,0,0,0)
    hdrl = group(b"hdrl",chunk(b"avih",avih)+group(b"strl",chunk(b"strh",strh)+chunk(b"strf",strf)))
    movi, index = bytearray(), bytearray()
    for jpg in images:
        index.extend(struct.pack("<4sIII",b"00dc",0x10,4+len(movi),len(jpg)))
        movi.extend(chunk(b"00dc",jpg))
    return chunk(b"RIFF",b"AVI "+hdrl+group(b"movi",movi)+chunk(b"idx1",index))


def last_jpeg(movie):
    """Recover the exact final recorded preview from a station-authored Motion JPEG clip."""
    def chunks(start,end):
        while start + 8 <= end:
            size=struct.unpack_from("<I",movie,start+4)[0]
            content=start+8
            if content+size>end:
                raise ValueError("invalid_capture_video_chunk")
            yield movie[start:start+4],content,content+size
            start=content+size+(size%2)
    if movie[:4]!=b"RIFF" or movie[8:12]!=b"AVI ":
        raise ValueError("invalid_capture_video")
    for tag,start,end in chunks(12,len(movie)):
        if tag==b"LIST" and movie[start:start+4]==b"movi":
            images=[(a,b) for kind,a,b in chunks(start+4,end) if kind==b"00dc"]
            if images:
                a,b=images[-1]
                return movie[a:b]
    raise ValueError("no_recorded_preview")
