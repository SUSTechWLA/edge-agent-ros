"""Bake georeferenced, north-up color and surface-height images for the pinned campus."""
import argparse
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from xml.etree import ElementTree as ET
import zlib

import numpy as np

if __package__:
    from .build import generated_mesh_files, local_mesh
else:
    from build import generated_mesh_files, local_mesh


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def png(path, pixels):
    """Write packed RGB or grayscale rows. No image codec or network install needed."""
    h, w = pixels.shape[:2]
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = bytearray()
    encoder = zlib.compressobj(6)
    for row in pixels:
        raw.extend(encoder.compress(b"\x00" + row.tobytes()))
    raw.extend(encoder.flush())
    header = struct.pack(">IIBBBBB", w, h, 8, 2 if pixels.ndim == 3 else 0, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def numbers(text):
    return tuple(float(v) for v in text.split())


def pose(element):
    x, y, z, roll, pitch, yaw = numbers(element.findtext("pose", "0 0 0 0 0 0"))
    if abs(roll) + abs(pitch) > 1e-7:
        raise ValueError("Expected a Z-up static campus model")
    return x, y, z, yaw


def compose(parent, child):
    x,y,z,yaw = parent
    dx,dy,dz,turn = child
    co,si = math.cos(yaw),math.sin(yaw)
    return x+co*dx-si*dy, y+si*dx+co*dy, z+dz, yaw+turn


class Atlas:
    def __init__(self, bounds, width, ground):
        self.bounds = np.array(bounds, dtype=np.float64)
        self.width = width
        self.height = round(width * (bounds[3]-bounds[1]) / (bounds[2]-bounds[0]))
        self.dx = (bounds[2]-bounds[0])/width
        self.dy = (bounds[3]-bounds[1])/self.height
        self.rgb = np.empty((self.height, width, 3), dtype=np.uint8)
        self.rgb[:] = np.uint8(np.array(ground[:3])*255)
        self.z = np.zeros((self.height, width), dtype=np.float32)

    def primitive(self, v, parent=(0.,0.,0.,0.)):
        x, y, z, yaw = compose(parent, pose(v))
        geom = v.find("geometry")[0]
        if geom.tag == "box":
            a, b, c = [n/2 for n in numbers(geom.findtext("size"))]
        elif geom.tag == "cylinder":
            a = b = float(geom.findtext("radius"))
            c = float(geom.findtext("length"))/2
        elif geom.tag == "ellipsoid":
            a, b, c = numbers(geom.findtext("radii"))
        else:
            raise ValueError(f"Unknown static training-area geometry {geom.tag}")
        co, si = math.cos(yaw), math.sin(yaw)
        rx, ry = abs(co)*a+abs(si)*b, abs(si)*a+abs(co)*b
        x0 = max(0, math.floor((x-rx-self.bounds[0])/self.dx))
        x1 = min(self.width, math.ceil((x+rx-self.bounds[0])/self.dx))
        y0 = max(0, math.floor((self.bounds[3]-y-ry)/self.dy))
        y1 = min(self.height, math.ceil((self.bounds[3]-y+ry)/self.dy))
        if x0>=x1 or y0>=y1:
            return
        wx = self.bounds[0] + (np.arange(x0,x1)+.5)*self.dx-x
        wy = self.bounds[3] - (np.arange(y0,y1)[:,None]+.5)*self.dy-y
        u, t = (co*wx+si*wy)/a, (-si*wx+co*wy)/b
        rad = u*u+t*t
        inside = (np.abs(u)<=1)&(np.abs(t)<=1) if geom.tag == "box" else rad<=1
        albedo = np.array(numbers(v.findtext("material/diffuse"))[:3])*255
        if geom.tag=="ellipsoid":
            up = np.sqrt(np.maximum(0.,1-rad))
            top = z + c*up
            shade = .72+.30*np.maximum(0.,(.42*(co*u-si*t)-.32*(si*u+co*t)+up)*.9)
            paint = np.uint8(np.clip(shade[:,:,None]*albedo,0,255))
        else:
            top = z+c
            paint = np.uint8(albedo)
        heights, colors = self.z[y0:y1,x0:x1], self.rgb[y0:y1,x0:x1]
        visible = inside & (top>=heights)
        np.copyto(heights, top, where=visible, casting="unsafe")
        np.copyto(colors, paint, where=visible[:,:,None])

    def shade(self):
        """Directional shadows from the rendered surface, in metres; heights remain unchanged."""
        shadow = np.ones_like(self.z)
        height = float(self.z.max())
        for step in np.geomspace(max(self.dx,self.dy)*1.5, max(4., height)*.72, num=15):
            col = max(1,round(step*.78/self.dx))
            row = max(1,round(step*.62/self.dy))
            if col >= self.width or row >= self.height:
                continue
            a, b = self.z[:-row,:-col], self.z[row:,col:]
            occluded = (b-a) > math.hypot(col*self.dx,row*self.dy)*1.35
            view = shadow[:-row,:-col]
            np.minimum(view, np.where(occluded, .71, 1.), out=view)
        self.rgb[:] = self.rgb * shadow[:,:,None]


def compile_mesh(tmp):
    library = tmp / "texture_mesh.so"
    subprocess.run(["g++","-O2","-std=c++17","-shared","-fPIC",str(Path(__file__).with_name("texture_mesh.cpp")),
                    "-lassimp","-lpng","-o",str(library)],check=True)
    mesh = ctypes.CDLL(str(library)).mesh
    ptr = np.ctypeslib.ndpointer
    mesh.argtypes = [ctypes.c_char_p,ctypes.c_char_p,ctypes.c_int,ptr(dtype=np.float64,ndim=1),
                     ctypes.c_int,ctypes.c_int,ptr(dtype=np.float64,ndim=1),
                     ptr(dtype=np.uint8,ndim=3,flags="C_CONTIGUOUS"),ptr(dtype=np.float32,ndim=2,flags="C_CONTIGUOUS")]
    mesh.restype = ctypes.c_int
    return mesh


def bake(assets, world_dir, output, width=2048):
    world_path = world_dir / "world.sdf"
    world = ET.parse(world_path).getroot().find("world")
    overview = json.loads((world_dir / "overview/manifest.json").read_text())
    layout = json.loads((world_dir / "layout.json").read_text())
    bounds = overview["bounds"]
    if layout["id"] != "campus_large" or overview["frame"] != layout["frame"]:
        raise ValueError("Expected registered campus world and layout")
    if not 64<=width<=4096 or not 1<=round(width*(bounds[3]-bounds[1])/(bounds[2]-bounds[0]))<=4096:
        raise ValueError("Invalid texture size")
    if width * round(width*(bounds[3]-bounds[1])/(bounds[2]-bounds[0])) > 6_000_000:
        raise ValueError("Texture exceeds pixel budget")
    output.mkdir(parents=True,exist_ok=True)
    with (output/".bake.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        source_hash = sha(world_path)
        parts = [source_hash,sha(world_dir/"layout.json"),sha(world_dir/"overview/manifest.json"),
                 sha(Path(__file__)),sha(Path(__file__).with_name("texture_mesh.cpp")),
                 sha(Path(__file__).with_name("asset-lock.json")),str(width)]
        # OBJ, MTL and image bytes can change without the visual's URI changing.
        for item in generated_mesh_files(world, world_dir):
            parts += [item.relative_to(world_dir.resolve()).as_posix(), sha(item)]
        revision = hashlib.sha256("/".join(parts).encode()).hexdigest()[:20]
        path = output/"manifest.json"
        if path.is_file():
            try:
                previous = json.loads(path.read_text())
            except (OSError, ValueError):
                previous = {}
            if previous.get("revision")==revision and all((output/f).is_file() and sha(output/f)==previous.get(k)
                    for f,k in (("texture.png","sha256"),("height.png","height_sha256"))):
                return previous
        floor = world.find("./model[@name='floor']/link/visual/material/diffuse")
        atlas = Atlas(bounds,width,numbers(floor.text))
        # Compose mounts /tmp noexec. Build the native rasterizer in a private
        # staging directory on the local campus volume and remove it after use.
        with tempfile.TemporaryDirectory(dir=output) as stage:
            stage = Path(stage)
            mesh = compile_mesh(stage)
            for inc in world.findall("include"):
                name = inc.findtext("uri").removeprefix("model://")
                model = assets/"models"/name
                x,y,z,yaw = pose(inc)
                if name == "Campus":
                    visual = ET.parse(model/"model.sdf").find(".//visual")
                    vx,vy,vz,va = pose(visual)
                    if vx or vy or va: raise ValueError("Unsupported Fuel visual transform")
                    meshfile = model/visual.findtext("geometry/mesh/uri").split("Campus/")[-1]
                    texdir = meshfile.parent
                    z += vz
                elif name == "Pine Tree":
                    meshfile = model/"meshes/pine_tree.dae"
                    texdir = model/"materials/textures"
                else:
                    raise ValueError(f"Unexpected campus include {name}")
                ret = mesh(os.fsencode(meshfile),os.fsencode(texdir),int(name=="Pine Tree"),
                           np.array([x,y,z,yaw]),width,atlas.height,atlas.bounds,atlas.rgb,atlas.z)
                if ret: raise RuntimeError(f"Failed to render {name}")
            for model in world.findall("model"):
                if model.get("name") in ("floor","edge_rover","campus_viewpoints"):
                    continue
                for link in model.findall("link"):
                    parent = compose(pose(model),pose(link))
                    for visual in link.findall("visual"):
                        obj = visual.find("geometry/mesh")
                        if obj is None:
                            atlas.primitive(visual,parent)
                            continue
                        if numbers(obj.findtext("scale","1 1 1")) != (1.,1.,1.):
                            raise ValueError("Expected metric campus OBJ")
                        path = local_mesh(world_dir,obj.findtext("uri"))
                        placement = np.array(compose(parent,pose(visual)))
                        if mesh(os.fsencode(path),os.fsencode(path.parent),0,placement,width,atlas.height,
                                atlas.bounds,atlas.rgb,atlas.z):
                            raise RuntimeError(f"Failed to render {path.name}")
            atlas.shade()
            highest = float(atlas.z.max())
            scale = max(1,math.ceil(highest/255*100))/100
            png(stage/"texture.png",atlas.rgb)
            png(stage/"height.png",np.uint8(np.clip(np.rint(atlas.z/scale),0,255)))
            result = dict(name="校园",scene_id=layout["id"],scene_revision=layout["revision"],frame=layout["frame"],
                          bounds=bounds,width=width,height=atlas.height,revision=revision,source="Gazebo 校园世界 · 米制场景几何与材质",
                          world_sha256=source_hash,areas=overview["areas"],height_scale_m=scale,height_offset_m=0.,
                          height_max_m=highest,sha256=sha(stage/"texture.png"),height_sha256=sha(stage/"height.png"))
            if world.find("./model[@name='delft_surveyed_site']") is not None:
                result["attribution"] = [
                    {"label":"© 3DBAG by tudelft3d and 3DGI","url":"https://docs.3dbag.nl/en/copyright/"},
                    {"label":"Luchtfoto © Beeldmateriaal / PDOK","url":"https://www.pdok.nl/ogc-webservices/-/article/pdok-luchtfoto-rgb-open-"}]
            (stage/"manifest.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
            for name in ("texture.png","height.png","manifest.json"):
                os.replace(stage/name,output/name)
            return result


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("assets","world","output"):
        p.add_argument("--"+name,required=True,type=Path)
    p.add_argument("--width",type=int,default=2048)
    args=p.parse_args()
    result=bake(args.assets,args.world,args.output,args.width)
    print(f"Campus texture {result['revision']} {result['width']} x {result['height']} px; max Z {result['height_max_m']:.2f} m")
