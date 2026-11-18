"""Rebuild the distributable Delft meshes from pinned raw CityJSON pages.

Developer-only dependencies and the source-acquisition command are in
real_site_data/README.md. Scene startup only needs Python's standard library.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen
from zipfile import ZipFile

import mapbox_earcut
import numpy as np
from PIL import Image, ImageDraw


DATA = Path(__file__).with_name("real_site_data")
BBOX = (85200, 445440, 85745, 446610)
WIDTH, HEIGHT = BBOX[2] - BBOX[0], BBOX[3] - BBOX[1]
IMAGE_SIZE = (WIDTH * 2, HEIGHT * 2)
API = "https://api.3dbag.nl/collections/pand/items?bbox={}&limit=100"
WMS = ("https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0?SERVICE=WMS&REQUEST=GetMap"
       "&VERSION=1.1.1&LAYERS=2025_ortho25&STYLES=&SRS=EPSG:28992"
       f"&BBOX={','.join(map(str,BBOX))}&WIDTH={IMAGE_SIZE[0]}&HEIGHT={IMAGE_SIZE[1]}&FORMAT=image/png")


def divide(box):
    w, s, e, n = box
    x, y = (w+e)//2, (s+n)//2
    return [(w,s,x,y), (x,s,e,y), (w,y,x,n), (x,y,e,n)]


def fetch(url):
    with urlopen(Request(url, headers={"User-Agent": "edge-agent-ros map-import/1.0"}), timeout=90) as response:
        return response.read()


def acquire(cache):
    """Split dense queries until every spatial page fits; no offset gaps."""
    cache.mkdir(parents=True, exist_ok=True)
    queue = divide(BBOX)
    while queue:
        box = queue.pop(0)
        path = cache / ("_".join(map(str, box)) + ".json")
        if not path.exists():
            path.write_bytes(fetch(API.format(",".join(map(str, box)))))
        page = json.loads(path.read_bytes())
        if any(link["rel"] == "next" for link in page["links"]):
            queue.extend(divide(box))
    photo = cache / "ortho.source"
    if not photo.exists():
        photo.write_bytes(fetch(WMS))


def load_sources(cache):
    """Keep only complete spatial pages; shared boundary features have a stable BAG ID."""
    sources, buildings = {}, {}
    for path in sorted(cache.glob("*.json")):
        page = json.loads(path.read_bytes())
        if any(link["rel"] == "next" for link in page["links"]):
            continue
        sources[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if not page["features"]:
            continue
        transform = page["metadata"]["transform"]
        for feature in page["features"]:
            buildings.setdefault(feature["id"], (feature, transform))
    if not buildings:
        raise ValueError("No CityJSON features in the pinned spatial pages")
    return sources, buildings


def triangulate(rings, vertices):
    """Project onto the most stable plane, preserve holes and original face winding."""
    outer = vertices[rings[0]]
    n = np.cross(outer - outer[0], np.roll(outer, -1, axis=0) - outer[0]).sum(axis=0)
    magnitude = np.linalg.norm(n)
    if magnitude < 1e-10:
        return
    normal = n / magnitude
    coords = vertices[np.concatenate(rings)]
    plane = np.delete(coords, np.argmax(abs(normal)), axis=1)
    ends = np.cumsum([len(r) for r in rings], dtype=np.uint32)
    indices = mapbox_earcut.triangulate_float64(plane, ends)
    for indices3 in indices.reshape(-1, 3):
        a, b, c = coords[indices3]
        product = np.dot(np.cross(b-a, c-a), normal)
        if abs(product) > 1e-10:
            yield ([a,b,c] if product > 0 else [a,c,b]), normal


class Obj:
    def __init__(self):
        self.positions, self.texcoords, self.normals = {}, {}, {}
        self.faces = []
        self.material = None

    def add(self, poly, normal, material):
        if len(poly) < 3:
            return 0
        tangent = np.array([-normal[1], normal[0]])
        if np.linalg.norm(tangent) > 0:
            tangent /= np.linalg.norm(tangent)
        # Gazebo's ODE trimesh path requires indexed normals on every submesh.
        ni = self.normals.setdefault(tuple(np.round(normal, 8)), len(self.normals)+1)
        indices = []
        for xyz in poly:
            if material in ("brick", "glass"):
                uv = np.dot(xyz[:2], tangent)/7.2, xyz[2]/3.6
            else:
                uv = xyz[0]/WIDTH, xyz[1]/HEIGHT
            key = tuple(np.round(xyz, 3))
            p = self.positions.setdefault(key, len(self.positions)+1)
            t = self.texcoords.setdefault(tuple(np.round(uv, 7)), len(self.texcoords)+1)
            indices.append((p,t))
        count = 0
        for i in range(1, len(poly)-1):
            a,b,c = indices[0], indices[i], indices[i+1]
            if len({a[0],b[0],c[0]}) < 3:
                continue
            if self.material != material:
                self.faces.append("usemtl " + material)
                self.material = material
            self.faces.append("f " + " ".join(f"{p}/{t}/{ni}" for p,t in (a,b,c)))
            count += 1
        return count

    def encode(self):
        lines = ["mtllib buildings.mtl"]
        lines.extend("v %.3f %.3f %.3f" % v for v in self.positions)
        lines.extend("vt %.7f %.7f" % t for t in self.texcoords)
        lines.extend("vn %.8f %.8f %.8f" % n for n in self.normals)
        return ("\n".join(lines+self.faces)+"\n").encode()


def building_mesh(buildings):
    mesh = Obj()
    counts = Counter()
    included, perimeter = [], []
    for building_id, (feature, transform) in sorted(buildings.items()):
        vertices = np.array(feature["vertices"])*transform["scale"] + transform["translate"]
        vertices[:,:2] -= BBOX[:2]
        parts = [g for obj in feature["CityObjects"].values() for g in obj.get("geometry", [])
                 if g["lod"] == "2.2" and g["type"] == "Solid"]
        if not parts:
            continue
        shell_indices = [i for part in parts for shell in part["boundaries"]
                         for surface in shell for ring in surface for i in ring]
        xy = vertices[shell_indices,:2]
        # Keep a complete surveyed shell or leave the entire building out.
        if (xy.min(axis=0) < 3).any() or (xy.max(axis=0) > (WIDTH-3,HEIGHT-3)).any():
            perimeter.append(building_id)
            continue
        # Keep each building's measured vertical differences. Align its ground
        # to the flat Gazebo support plane; local NAP elevation is not terrain.
        ground = [i for part in parts for shell, types in zip(part["boundaries"], part["semantics"]["values"])
                  for rings, kind in zip(shell, types)
                  if part["semantics"]["surfaces"][kind]["type"] == "GroundSurface"
                  for ring in rings for i in ring]
        vertices[:,2] -= min(vertices[ground,2]) if ground else min(vertices[:,2])
        total = 0
        mesh.faces.append("g " + building_id.replace(".", "_"))
        for part in parts:
            semantics = part["semantics"]
            for shell, types in zip(part["boundaries"], semantics["values"]):
                for rings, idx in zip(shell, types):
                    kind = semantics["surfaces"][idx]["type"]
                    if kind == "WallSurface":
                        material = "glass" if vertices[:,2].max() > 55 else "brick"
                    else:
                        material = "ortho" if kind == "RoofSurface" else "base"
                    for triangle, normal in triangulate(rings, vertices):
                        count = mesh.add(triangle, normal, material)
                        counts[kind] += count
                        total += count
        if total:
            included.append(building_id)
    return mesh, included, perimeter, counts


def materials():
    parts = []
    for name, image in (("ortho","ortho"), ("brick","brick"), ("glass","glass"), ("base",None)):
        parts.extend((f"newmtl {name}", "Ka 0.30 0.30 0.30", "Kd 1.0 1.0 1.0" if image else "Kd 0.38 0.37 0.35", "Ks 0 0 0"))
        if image:
            parts.append(f"map_Kd {image}.png")
    return ("\n".join(parts)+"\n").encode()


def facade(path, glass):
    im = Image.new("RGB", (192,96), (163,170,169) if glass else (158,118,94))
    draw = ImageDraw.Draw(im)
    for y in range(0,96,6):
        if not glass:
            draw.line((0,y,191,y), fill=(140,110,90))
            for x in range(0 if y%12==0 else -12,192,24):
                draw.line((x,y,x,y+5), fill=(134,103,85))
    for x in range(0,192,48):
        margin, top, bottom = (2,8,74) if glass else (5,16,66)
        draw.rectangle((x+margin,top,x+47-margin,bottom), fill=(49,65,70))
        for row in range(top+3,bottom-2):
            blue = int(110+24*(bottom-row)/(bottom-top))
            draw.line((x+margin+3,row,x+44-margin,row), fill=(blue-48,blue-18,blue))
        draw.line((x+24,top,x+24,bottom), fill=(59,72,75),width=2)
        draw.line((x+margin,top+16,x+47-margin,top+16), fill=(65,78,80),width=2)
    im.save(path)


def rebuild(cache, output):
    output.mkdir(parents=True, exist_ok=True)
    sources, buildings = load_sources(cache)
    mesh, included, perimeter, counts = building_mesh(buildings)
    faces = mesh.encode()
    (output/"buildings.obj.gz").write_bytes(gzip.compress(faces, compresslevel=9, mtime=0))
    (output/"buildings.mtl").write_bytes(materials())
    terrain = Obj()
    terrain.add([np.array([x,y,.025]) for x,y in ((0,0),(WIDTH,0),(WIDTH,HEIGHT),(0,HEIGHT))],
                np.array([0.,0.,1.]), "ortho")
    (output/"terrain.obj").write_bytes(terrain.encode())
    for name in ("brick", "glass"):
        facade(output/f"{name}.png", name=="glass")
    with Image.open(cache/"ortho.source") as im:
        if im.size != IMAGE_SIZE:
            raise ValueError(f"Unexpected aerial raster dimensions {im.size}")
        im.convert("RGB").save(output/"ortho.png", compress_level=9)
    runtime_files = {f: hashlib.sha256((output/f).read_bytes()).hexdigest() for f in
                     ("terrain.obj","buildings.mtl","ortho.png","brick.png","glass.png")}
    runtime_files["buildings.obj.gz"] = hashlib.sha256(faces).hexdigest()
    manifest = dict(bbox_rd_m=BBOX, crs="EPSG:7415", buildings=len(included),
                    triangles_by_surface=counts, top_m=max(v[2] for v in mesh.positions),
                    runtime_files=runtime_files, cityjson_query=API, cityjson_pages=sources,
                    wms_query=WMS, wms_sha256=hashlib.sha256((cache/"ortho.source").read_bytes()).hexdigest(),
                    source_building_ids=included, omitted_perimeter_ids=perimeter,
                    geometry_boundary="whole LoD2.2 shells only, 3 m inset")
    (output/"manifest.json").write_bytes((json.dumps(manifest,indent=2,sort_keys=True)+"\n").encode())
    print(f"{len(included)} buildings, {sum(counts.values())} triangles, height {manifest['top_m']:.2f} m")


def verify_inputs(cache, lock):
    actual, _ = load_sources(cache)
    if (actual != lock["cityjson_pages"] or
            hashlib.sha256((cache/"ortho.source").read_bytes()).hexdigest() != lock["wms_sha256"]):
        raise ValueError("Input differs from the manifest. Inspect the upstream revision and map bounds before updating.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, help="Use a directory of downloaded pages in place of the pinned archive")
    parser.add_argument("--output", type=Path, default=DATA)
    parser.add_argument("--fetch", action="store_true", help="Acquire upstream pages and aerial imagery into cache")
    args = parser.parse_args()
    lock = json.loads((DATA/"manifest.json").read_bytes())
    with TemporaryDirectory() as tmp:
        cache = args.cache or Path(tmp)
        if args.fetch:
            acquire(cache)
        elif not args.cache:
            with ZipFile(DATA/"sources.zip") as archive:
                for name in [*lock["cityjson_pages"], "ortho.source"]:
                    (cache/name).write_bytes(archive.read(name))
        verify_inputs(cache, lock)
        rebuild(cache, args.output)
