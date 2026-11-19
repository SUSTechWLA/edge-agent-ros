"""Serve one immutable, local, georeferenced appearance pack per viewer instance."""
import hashlib
import json
import math
from pathlib import Path
import re
import struct
from urllib.parse import urlsplit


def bounds(value):
    if not isinstance(value, list) or len(value) != 4 or not all(
        type(v) in (int, float) and math.isfinite(v) and abs(v) <= 1e6 for v in value
    ) or value[2] <= value[0] or value[3] <= value[1]:
        raise ValueError("texture_bounds_invalid")
    return value


def registered_route(route, info):
    if (not isinstance(route,dict) or not isinstance(route.get("id"),str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}",route["id"])
            or not info["scene_revision"] or route.get("scene_revision")!=info["scene_revision"]
            or route.get("frame")!=info["frame"]):
        raise ValueError("texture_route_registration_invalid")
    points,length = route.get("points"),route.get("length_m")
    xmin,ymin,xmax,ymax = info["bounds"]
    if (not isinstance(points,list) or not 2<=len(points)<=256
            or any(not isinstance(p,list) or len(p)!=2
                   or not all(type(v) in (int,float) for v in p)
                   or not (xmin<=p[0]<=xmax and ymin<=p[1]<=ymax) for p in points)
            or type(length) not in (int,float) or not 0<length<=10000):
        raise ValueError("texture_route_geometry_invalid")
    legs=[math.dist(a,b) for a,b in zip(points,points[1:])]
    if min(legs)<=0 or abs(sum(legs)-length)>.002:
        raise ValueError("texture_route_length_invalid")
    return {k:route[k] for k in ("id","frame","scene_revision","points","length_m")}


def bounded_read(path, budget):
    with path.open("rb") as file:
        size = file.seek(0, 2)
        if not 0 < size <= budget:
            raise ValueError("texture_size_invalid")
        file.seek(0)
        raw = file.read(size)
    if len(raw) != size:
        raise ValueError("texture_size_invalid")
    return raw


def image(directory, filename, digest, width, height):
    raw = bounded_read(directory / filename, 20 * 1024**2)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("texture_checksum_mismatch")
    if (raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR" or len(raw) < 45
            or struct.unpack(">II", raw[16:24]) != (width, height)):
        raise ValueError("texture_image_invalid")
    return raw


class TexturePack:
    def __init__(self, directory):
        self.files = {}
        self.info = None
        if directory:
            try:
                self.load(Path(directory))
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                code = str(exc)
                self.info = {"status":"error","error":code if code.startswith("texture_") else "texture_pack_invalid"}

    def load(self, root):
        raw = bounded_read(root / "manifest.json", 64 * 1024)
        meta = json.loads(raw)
        width, height = meta["width"], meta["height"]
        if not all(type(v) is int and 0 < v <= 4096 for v in (width,height)) or width*height > 6_000_000:
            raise ValueError("texture_dimensions_invalid")
        info = {k:meta[k] for k in ("name","scene_id","revision","frame","source")}
        if not all(isinstance(x,str) and 0 < len(x) <= 512 for x in info.values()):
            raise ValueError("texture_metadata_invalid")
        info.update(bounds=bounds(meta["bounds"]),width=width,height=height)
        info["scene_revision"] = str(meta.get("scene_revision",""))[:64]
        if "route" in meta:
            try:
                info["route"] = registered_route(meta["route"], info)
            except ValueError as exc:
                info["route_error"] = str(exc)
        info["areas"] = []
        attribution = meta.get("attribution",[])
        if (not isinstance(attribution,list) or len(attribution)>10 or
                not all(isinstance(item,dict) and isinstance(item.get("label"),str)
                        and 0<len(item["label"])<=150 and isinstance(item.get("url"),str)
                        and len(item["url"])<=512 and urlsplit(item["url"]).scheme=="https"
                        and urlsplit(item["url"]).hostname for item in attribution)):
            raise ValueError("texture_attribution_invalid")
        info["attribution"] = [{"label":item["label"],"url":item["url"]} for item in attribution]
        areas = meta.get("areas",[])
        if not isinstance(areas,list) or len(areas) > 64:
            raise ValueError("texture_areas_invalid")
        for item in areas:
            area = {"id":str(item["id"])[:64], "label":str(item["label"])[:96],
                    "bounds":bounds(item["bounds"])}
            if "camera" in item:
                camera = {k:item["camera"][k] for k in ("eye","target")}
                if not all(isinstance(v,list) and len(v)==3 and all(type(c) in (int,float)
                           and math.isfinite(c) and abs(c)<=1e6 for c in v) for v in camera.values()) or camera["eye"]==camera["target"]:
                    raise ValueError("texture_camera_invalid")
                area["camera"] = camera
            info["areas"].append(area)
        # The URL identifies the registration as well as both payload hashes.
        key = hashlib.sha256(raw).hexdigest()
        base = "/texture/" + key + "/"
        files = {base+"texture.png":image(root,"texture.png",meta["sha256"],width,height)}
        info.update(status="ready",key=key,url=base+"texture.png",sha256=meta["sha256"])
        if "height_sha256" in meta:
            scale, offset = meta["height_scale_m"], meta.get("height_offset_m",0)
            if not (math.isfinite(scale) and 0 < scale <= 100 and math.isfinite(offset) and abs(offset) <= 1e6):
                raise ValueError("texture_height_invalid")
            files[base+"height.png"] = image(root,"height.png",meta["height_sha256"],width,height)
            info.update(height_url=base+"height.png",height_scale_m=scale,height_offset_m=offset,
                        height_sha256=meta["height_sha256"])
        if "mesh_sha256" in meta:
            # Pin the validated edition in memory, just like the PNGs. An independent
            # scene rebuild may atomically replace scene.glb while requests are in flight.
            try:
                glb = bounded_read(root / "scene.glb", 128 * 1024**2)
                if len(glb)<20 or struct.unpack_from("<4sII",glb)!=(b"glTF",2,len(glb)):
                    raise ValueError("texture_mesh_format_invalid")
                if hashlib.sha256(glb).hexdigest()!=meta["mesh_sha256"]:
                    raise ValueError("texture_mesh_checksum_mismatch")
                files[base+"scene.glb"] = glb
                info.update(mesh_url=base+"scene.glb", mesh_sha256=meta["mesh_sha256"], mesh_bytes=len(glb))
            except (OSError, ValueError) as exc:
                info["mesh_error"] = str(exc) if isinstance(exc,ValueError) else "texture_mesh_missing"
        self.info, self.files = info, files

    def for_map(self, snapshot):
        info = self.info
        if not info or info["status"] != "ready":
            return info
        if snapshot["environment"] != "simulation" or snapshot["frame"] != info["frame"]:
            return {"status":"error","error":"texture_frame_mismatch"}
        scene = snapshot["scene"]
        if not scene.get("id"):
            return {"status":"waiting","error":"texture_waiting_for_scene"}
        if (scene.get("status") == "error" or scene["id"] != info["scene_id"] or
                (info["scene_revision"] and scene.get("revision") != info["scene_revision"])):
            return {"status":"error","error":"texture_scene_mismatch"}
        return info
