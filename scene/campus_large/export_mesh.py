"""Export the static visuals of the campus SDF as one self-contained glTF 2.0 GLB."""
import argparse
from functools import lru_cache
import json
import math
from pathlib import Path
import struct
import subprocess
import tempfile
from xml.etree import ElementTree as ET

import numpy as np


def numbers(text):
    return np.array([float(n) for n in text.split()])


def pose(part):
    element = part.find("pose")
    if element is not None and element.get("relative_to"):
        raise ValueError(f"Unsupported reference frame on {part.get('name')}")
    x, y, z, roll, pitch, yaw = numbers(part.findtext("pose", "0 0 0 0 0 0"))
    cr, cp, cy = np.cos([roll, pitch, yaw])
    sr, sp, sy = np.sin([roll, pitch, yaw])
    return np.array([[cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr, x],
                     [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr, y],
                     [-sp, cp*sr, cp*cr, z], [0, 0, 0, 1]])


@lru_cache(maxsize=None)
def shape(kind):
    """Unit-size solid, as flat or smooth normals and indexed triangles."""
    vertices, normals, faces = [], [], []

    def face(points, directions=None):
        first = len(vertices)
        vertices.extend(points)
        if directions is None:
            normal = np.cross(np.subtract(points[1], points[0]), np.subtract(points[2], points[0]))
            directions = [normal / np.linalg.norm(normal)] * len(points)
        normals.extend(directions)
        for i in range(2, len(points)):
            faces.extend([first, first+i-1, first+i])

    corners = [(-.5,-.5), (.5,-.5), (.5,.5), (-.5,.5)]
    if kind == "plane":
        face([(x,y,0) for x,y in corners])
    elif kind == "box":
        lower, upper = [[(x,y,z) for x,y in corners] for z in (-.5,.5)]
        face(upper); face(lower[::-1])
        for i in range(4):
            j = (i+1)%4
            face([lower[i],lower[j],upper[j],upper[i]])
    elif kind == "cylinder":
        circle = [(math.cos(a),math.sin(a)) for a in np.arange(32)*math.tau/32]
        lower, upper = [[(x,y,z) for x,y in circle] for z in (-.5,.5)]
        face(upper); face(lower[::-1])
        for i in range(32):
            j = (i+1)%32
            face([lower[i],lower[j],upper[j],upper[i]], [(*circle[k],0) for k in (i,j,j,i)])
    elif kind in ("sphere", "ellipsoid"):
        def point(i, j):
            a, b = math.tau*i/16, math.pi*(j/8-.5)
            return (math.cos(a)*math.cos(b),math.sin(a)*math.cos(b),math.sin(b))
        for j in range(8):
            for i in range(16):
                pts = [point(i,j),point(i+1,j),point(i+1,j+1),point(i,j+1)]
                face(pts, pts)
    else:
        raise ValueError(f"Unsupported SDF primitive {kind}")
    return np.array(vertices), np.array(normals), np.array(faces, dtype="<u4")


def dimensions(geom):
    if geom.tag == "box":
        return numbers(geom.findtext("size"))
    if geom.tag == "plane":
        if not np.allclose(numbers(geom.findtext("normal","0 0 1")), (0,0,1)):
            raise ValueError("Expected horizontal campus plane")
        return [*numbers(geom.findtext("size")),1]
    if geom.tag == "ellipsoid":
        return numbers(geom.findtext("radii"))
    radius = float(geom.findtext("radius"))
    return [radius,radius,float(geom.findtext("length")) if geom.tag=="cylinder" else radius]


class Exporter:
    def __init__(self, stage, roots, world):
        self.stage, self.roots, self.world = stage, roots, world
        self.blob = bytearray()
        self.doc = {key:[] for key in ("nodes","meshes","materials","textures","images","accessors","bufferViews")}
        self.doc.update(asset={"version":"2.0","generator":"Edge Agent ROS · Gazebo static visuals"},
                        scenes=[{"nodes":[0]}], scene=0)
        # glTF viewers use +Y up. The renderer restores ROS +Z up on import.
        self.doc["nodes"].append({"name":"ROS world","rotation":[-math.sqrt(.5),0,0,math.sqrt(.5)],"children":[]})
        self.helper = stage/"assimp-reader"
        subprocess.run(["g++","-std=c++17","-O2",str(Path(__file__).with_suffix(".cpp")),
                        "-lassimp","-lpng","-o",str(self.helper)],check=True)

    def add(self, group, value):
        self.doc[group].append(value)
        return len(self.doc[group])-1

    def view(self, data):
        self.blob.extend(bytes(-len(self.blob)%4))
        index = self.add("bufferViews",dict(buffer=0,byteOffset=len(self.blob),byteLength=len(data)))
        self.blob.extend(data)
        return index

    def accessor(self, array, spec, bounds=False, normalized=False):
        component = {"f":5126,"u":{1:5121,2:5123,4:5125}.get(array.dtype.itemsize)}[array.dtype.kind]
        desc = dict(bufferView=self.view(array.tobytes()),componentType=component,count=len(array),type=spec)
        if bounds:
            desc.update(min=array.min(axis=0).tolist(),max=array.max(axis=0).tolist())
        if normalized:
            desc["normalized"] = True
        return self.add("accessors",desc)

    def primitive(self, positions, normals, indices, material, uv=None, colors=None):
        indices = indices.astype("<u2" if len(positions)<=65535 else "<u4")
        attrs = {"POSITION":self.accessor(positions.astype("<f4"),"VEC3",bounds=True),
                 "NORMAL":self.accessor(normals.astype("<f4"),"VEC3")}
        if uv is not None:
            attrs["TEXCOORD_0"] = self.accessor(uv.astype("<f4"),"VEC2")
        if colors is not None:
            attrs["COLOR_0"] = self.accessor(colors,"VEC4",normalized=True)
        return dict(attributes=attrs,indices=self.accessor(indices,"SCALAR"),material=material)

    def resolve(self, uri, base):
        if uri.startswith("model://"):
            name = uri.removeprefix("model://")
            candidates = [root/name for root in self.roots]
        else:
            raw = Path(uri.removeprefix("file://"))
            candidates = [base/raw]
            if raw.is_absolute() and raw.parts[1:2]==("campus",):
                candidates.insert(0,self.world.parent.joinpath(*raw.parts[2:]))
        for path in candidates:
            if path.exists():
                return path.resolve()
        raise FileNotFoundError(uri)

    @lru_cache(maxsize=None)
    def texture(self, path):
        if path.suffix.lower()==".png":
            output = self.stage/"thumbnail.png"
            # Preserve surveyed imagery resolution. Limit the many individual Fuel materials.
            limit = 1024 if any(path.is_relative_to(root) for root in self.roots) else 4096
            subprocess.run([self.helper,"image",path,output,str(limit)],check=True)
            data, mime = output.read_bytes(), "image/png"
        elif path.suffix.lower() in (".jpg",".jpeg"):
            data, mime = path.read_bytes(),"image/jpeg"
        else:
            raise ValueError(f"Unsupported diffuse texture {path}")
        image = self.add("images",dict(bufferView=self.view(data),mimeType=mime,name=path.name))
        return self.add("textures",dict(source=image))

    @lru_cache(maxsize=None)
    def material(self, name, rgb=(1.,1.,1.), path=None):
        pbr = dict(baseColorFactor=[*rgb,1],metallicFactor=0,roughnessFactor=1)
        mat = dict(name=name,pbrMetallicRoughness=pbr,doubleSided=True)
        if path:
            pbr["baseColorTexture"] = {"index":self.texture(path)}
            mat.update(alphaMode="MASK",alphaCutoff=.42)
        return self.add("materials",mat)

    @lru_cache(maxsize=None)
    def read_asset(self, path):
        output = self.stage/"import.bin"
        subprocess.run([self.helper,"geometry",path,output],check=True)
        data = memoryview(output.read_bytes())
        offset = 0
        def take(n):
            nonlocal offset
            result = data[offset:offset+n]; offset+=n
            if len(result)!=n:
                raise ValueError("Truncated Assimp stream")
            return result
        def string():
            return bytes(take(struct.unpack("<I",take(4))[0])).decode("utf-8")
        parts = []
        while offset<len(data):
            name, mat, tex = string(), string(), string()
            rgb = struct.unpack("<3f",take(12))
            vertices, indices = struct.unpack("<II",take(8))
            attributes = np.frombuffer(take(vertices*32),"<f4").reshape(-1,8)
            elements = np.frombuffer(take(indices*4),"<u4")
            parts.append((name,mat,tex,rgb,attributes,elements))
        return parts

    @lru_cache(maxsize=None)
    def imported_mesh(self, path, subset, override_texture, override_color):
        primitives = []
        for name, mat, tex, rgb, attrs, indices in self.read_asset(path):
            if subset and subset.casefold() not in (name+" "+mat).casefold():
                continue
            image = override_texture
            if tex and not image:
                image = self.resolve(tex.replace(chr(92),"/"),path.parent)
            material = self.material(mat,override_color or rgb,image)
            primitives.append(self.primitive(attrs[:,:3],attrs[:,3:6],indices,material,uv=attrs[:,6:8]))
        if not primitives:
            raise ValueError(f"Missing geometry {path} / {subset}")
        return self.add("meshes",dict(name=path.name+(" "+subset if subset else ""),primitives=primitives))

    def node(self, name, matrix, mesh):
        node = self.add("nodes",dict(name=name,mesh=mesh,matrix=matrix.T.reshape(-1).tolist()))
        self.doc["nodes"][0]["children"].append(node)

    def model(self, model, parent, base, name):
        transform = parent @ pose(model)
        groups = [[],[],[],[]]
        offset = 0
        for link in model.findall("link"):
            for vis in link.findall("visual"):
                geometry = vis.find("geometry")[0]
                matrix = transform @ pose(link) @ pose(vis)
                diffuse = vis.findtext("material/diffuse")
                rgb = tuple(numbers(diffuse)[:3]) if diffuse else None
                if geometry.tag=="mesh":
                    path = self.resolve(geometry.findtext("uri"),base)
                    albedo = vis.findtext(".//albedo_map")
                    tex = self.resolve(albedo,base) if albedo else None
                    mesh = self.imported_mesh(path, geometry.findtext("submesh/name"),tex,rgb)
                    scale = numbers(geometry.findtext("scale","1 1 1"))
                    self.node(name+"/"+vis.get("name"),matrix @ np.diag([*scale,1]),mesh)
                    continue
                vertices,normals,indices = shape(geometry.tag)
                matrix = matrix @ np.diag([*dimensions(geometry),1])
                transformed = normals @ np.linalg.inv(matrix[:3,:3])
                transformed /= np.linalg.norm(transformed,axis=1)[:,None]
                rgba = np.uint8(np.clip(np.array([*(rgb or (.8,.8,.8)),1])*255,0,255))
                groups[0].append(vertices @ matrix[:3,:3].T+matrix[:3,3])
                groups[1].append(transformed); groups[2].append(indices+offset)
                groups[3].append(np.tile(rgba,(len(vertices),1)))
                offset += len(vertices)
        if offset:
            p,n,i,c = (np.concatenate(parts) for parts in groups)
            primitive = self.primitive(p,n,i,self.material("SDF diffuse"),colors=c)
            self.node(name,np.eye(4),self.add("meshes",dict(name=name,primitives=[primitive])))

    def write(self, filename):
        world = ET.parse(self.world).find("world")
        for model in world.findall("model"):
            if model.findtext("static","false").lower() in ("1","true"):
                self.model(model,np.eye(4),self.world.parent,model.get("name"))
        for inc in world.findall("include"):
            model_dir = self.resolve(inc.findtext("uri"),self.world.parent)
            model = ET.parse(model_dir/"model.sdf").find("model")
            if model.findtext("static","false").lower() in ("1","true"):
                self.model(model,pose(inc),model_dir,inc.findtext("name",model.get("name")))
        self.blob.extend(bytes(-len(self.blob)%4))
        self.doc["buffers"] = [{"byteLength":len(self.blob)}]
        for key in ("textures","images"):
            if not self.doc[key]:
                self.doc.pop(key)
        desc = json.dumps(self.doc,separators=(",",":"),allow_nan=False).encode("utf-8")
        desc += b" "*(-len(desc)%4)
        with filename.open("wb") as f:
            f.write(struct.pack("<4sII",b"glTF",2,28+len(desc)+len(self.blob)))
            f.write(struct.pack("<I4s",len(desc),b"JSON")); f.write(desc)
            f.write(struct.pack("<I4s",len(self.blob),b"BIN\0")); f.write(self.blob)


def export(world, output, model_paths):
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        stage = Path(tmp)
        builder = Exporter(stage,[p.resolve() for p in model_paths],world.resolve())
        target = stage/"world.glb"
        builder.write(target)
        print(f"Campus GLB: {len(builder.doc['meshes'])} meshes, {len(builder.doc['nodes'])-1} nodes, "
              f"{target.stat().st_size:,} bytes",flush=True)
        target.replace(output)


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("world",type=Path); parser.add_argument("output",type=Path)
    parser.add_argument("--model-path",action="append",type=Path,default=[])
    args = parser.parse_args()
    export(args.world,args.output,args.model_path)
