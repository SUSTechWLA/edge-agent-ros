"""Serve one immutable, local, georeferenced appearance pack per viewer instance."""
import hashlib
import json
import math
from pathlib import Path
import struct


def bounds(value):
    if not isinstance(value, list) or len(value) != 4 or not all(
        type(v) in (int, float) and math.isfinite(v) and abs(v) <= 1e6 for v in value
    ) or value[2] <= value[0] or value[3] <= value[1]:
        raise ValueError("texture_bounds_invalid")
    return value


def bounded_read(path, budget):
    with path.open("rb") as file:
        raw = file.read(budget + 1)
    if not 0 < len(raw) <= budget:
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
        info["areas"] = []
        areas = meta.get("areas",[])
        if not isinstance(areas,list) or len(areas) > 64:
            raise ValueError("texture_areas_invalid")
        for item in areas:
            info["areas"].append({"id":str(item["id"])[:64], "label":str(item["label"])[:96],
                                  "bounds":bounds(item["bounds"])})
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
