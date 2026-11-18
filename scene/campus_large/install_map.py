"""Bake the running campus and publish its map entry in this project's two workbenches."""
import json
import os
from pathlib import Path
import subprocess
import sys

from capture import RUNTIME, WORLD, atomic, file_digest, identity, read_record, record
import hashlib
import tempfile


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
        if identity(WORLD) != active or read_record(RUNTIME) != active:
            raise RuntimeError("the campus world changed during texture generation; repeat campus map installation")
        register(WORLD/"overview", Path("/views/views.json"), int(os.environ["EDGE_CAMPUS_MAP_PORT"]))


if __name__ == "__main__":
    main()
