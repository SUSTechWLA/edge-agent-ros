"""Bake the running campus and publish its map entry in this project's two workbenches."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from capture import RUNTIME, WORLD, atomic, file_digest, identity, read_record, record
import hashlib
import tempfile
import xml.etree.ElementTree as ET


def install_route(world, directory):
    """Publish the surveyed walk from the same building edition and SDF road segments."""
    manifest = directory/"manifest.json"
    meta = json.loads(manifest.read_bytes())
    overview = json.loads((world/"overview"/"manifest.json").read_bytes())
    layout = json.loads((world/"layout.json").read_bytes())
    graphs = [r for r in overview.get("routes",[]) if r.get("id")=="campus_walk"]
    if not graphs:
        if meta.pop("route",None) is not None:
            record(manifest,meta)
        return
    if (len(graphs)!=1 or overview["frame"]!=layout["frame"] or layout["frame"]!=meta["frame"]
            or overview.get("unit")!="m" or overview.get("scene_revision",layout["revision"])!=layout["revision"]
            or layout["revision"]!=meta["scene_revision"] or meta["world_sha256"]!=file_digest(world/"world.sdf")):
        raise ValueError("campus walk and atlas registration differ")
    route = graphs[0]
    nodes, edges = route["nodes"],route["edges"]
    if not 2<=len(nodes)<=256 or len(edges)!=len(nodes)-1 or len({n["id"] for n in nodes})!=len(nodes):
        raise ValueError("campus walk is disconnected or exceeds its point budget")
    road = ET.parse(world/"world.sdf").find("./world/model[@name='road']")
    visuals = {v.get("name"):v for v in road.findall("link/visual")} if road is not None else {}
    points = [n["xy"] for n in nodes]
    xmin,ymin,xmax,ymax = meta["bounds"]
    if any(len(p)!=2 or not all(type(v) in (int,float) and math.isfinite(v) for v in p)
           or not (xmin<=p[0]<=xmax and ymin<=p[1]<=ymax) for p in points):
        raise ValueError("campus walk coordinate outside the atlas")
    length = 0.
    for i,edge in enumerate(edges):
        if (edge["from"]!=nodes[i]["id"] or edge["to"]!=nodes[i+1]["id"]
                or edge["model"]!="road" or edge["visual"] not in visuals):
            raise ValueError("campus walk edge not present in the registered road model")
        visual = visuals[edge["visual"]]
        a,b = points[i:i+2]
        x,y,_,_,_,yaw = map(float,visual.findtext("pose").split())
        span = float(visual.findtext("geometry/box/size").split()[0])
        dx,dy = span*math.cos(yaw)/2,span*math.sin(yaw)/2
        if math.dist((x-dx,y-dy),a)>.012 or math.dist((x+dx,y+dy),b)>.012 or span<=0:
            raise ValueError("campus walk endpoints do not match Gazebo geometry")
        length += math.dist(a,b)
    if length>10000:
        raise ValueError("campus walk exceeds the length budget")
    overlay = dict(id=route["id"],frame=meta["frame"],scene_revision=layout["revision"],
                   points=points,length_m=round(length,3))
    if meta.get("route")!=overlay:
        meta["route"]=overlay
        record(manifest,meta)


def install_mesh(world, directory, models):
    manifest = directory / "manifest.json"
    meta = json.loads(manifest.read_bytes())
    if meta["world_sha256"] != file_digest(world):
        raise RuntimeError("the map atlas belongs to another world")
    exporter = Path(__file__).with_name("export_mesh.py")
    # The atlas revision covers the local SDF, meshes, materials, locked Fuel inputs and layout.
    fingerprint = hashlib.sha256(" ".join((meta["revision"],file_digest(exporter),
                                           file_digest(exporter.with_suffix(".cpp")))).encode()).hexdigest()
    destination = directory / "scene.glb"
    if (meta.get("mesh_revision")==fingerprint and destination.is_file()
            and file_digest(destination)==meta.get("mesh_sha256")):
        return
    with tempfile.TemporaryDirectory(dir=directory) as stage:
        output = Path(stage) / "scene.glb"
        subprocess.run([sys.executable,"-B",str(exporter),str(world),str(output),
                        "--model-path",str(models)],check=True)
        if not 20 <= output.stat().st_size <= 128 * 1024**2:
            raise ValueError("campus GLB outside map service size budget")
        meta.update(mesh_sha256=file_digest(output),mesh_revision=fingerprint)
        output.replace(destination)
        record(manifest,meta)


def register(directory, views_file, port):
    if not 1 <= port <= 65535:
        raise ValueError("invalid campus map port")
    entries = json.loads(views_file.read_bytes()) if views_file.exists() else []
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("invalid campus view registry")
    registered = [entry for entry in entries if entry.get("id") != "map3d"]
    if len(registered) >= 16:
        raise ValueError("the campus workbench has reached its 16-view limit")
    registered.append({"id":"map3d", "label":"校园纹理地图", "slot":"map", "port":port})
    atomic(views_file, (json.dumps(registered, ensure_ascii=False, indent=2)+"\n").encode())
    record(directory/"map-link.json", {"port":port})
    atomic(directory/"index.html", Path(__file__).with_name("overview.html").read_bytes())


def main():
    import fcntl
    with (WORLD/".map-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = identity(WORLD)
        if read_record(RUNTIME) != active:
            raise RuntimeError("campus assets and the running world differ; use the guarded campus reload")
        subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("texture.py")),
                        "--assets", "/assets", "--world", str(WORLD), "--output", str(WORLD/"texture")], check=True)
        install_mesh(WORLD/"world.sdf", WORLD/"texture", Path("/assets/models"))
        install_route(WORLD, WORLD/"texture")
        if identity(WORLD) != active or read_record(RUNTIME) != active:
            raise RuntimeError("the campus world changed during texture generation; repeat campus map installation")
        register(WORLD/"overview", Path("/views/views.json"), int(os.environ["EDGE_CAMPUS_MAP_PORT"]))


if __name__ == "__main__":
    main()
