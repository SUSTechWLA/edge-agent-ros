"""Expose the campus world's existing, immutable appearance pack to the task console."""
import hashlib
import json
from pathlib import Path


def load(root):
    if not root:
        return None
    from perception.map3d.texture import TexturePack
    root = Path(root)
    pack = TexturePack(root / "texture")
    if pack.info.get("status") != "ready":
        return pack
    try:
        layout = (root / "layout.json").read_bytes()
        scene = json.loads(layout)
        manifest = (root / "texture/manifest.json").read_bytes()
        with (root / "world.sdf").open("rb") as stream:
            world_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        if (hashlib.sha256(manifest).hexdigest() != pack.info["key"] or
                json.loads(manifest)["world_sha256"] != world_hash or
                (scene["id"], scene["revision"], scene["frame"]) !=
                (pack.info["scene_id"], pack.info["scene_revision"], pack.info["frame"])):
            raise ValueError("campus world registration mismatch")
        pack.info.update(world_sha256=world_hash, layout_sha256=hashlib.sha256(layout).hexdigest())
        pack.info.pop("route", None)  # The station's test circuit is unrelated to the selected task.
    except (OSError, ValueError, KeyError, TypeError):
        pack.info, pack.files = {"status": "error", "error": "campus_world_mismatch"}, {}
    return pack


def serve(handler, path):
    if path != "/api/campus-atlas" and not path.startswith(("/texture/", "/campus-viewer/")):
        return False
    pack = getattr(handler.server, "campus_atlas", None)
    if not pack or handler.observer.environment != "simulation":
        handler.respond(404, {"status": "absent"})
    elif path == "/api/campus-atlas":
        handler.respond(200, pack.info)
    elif path in pack.files:
        data = pack.files[path]
        handler.send_response(200)
        handler.send_header("Content-Type", "image/png" if path.endswith(".png") else "model/gltf-binary")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        handler.wfile.write(data)
    elif path in {"/campus-viewer/"+file for file in ("grid.js", "mesh.js", "spatial.js", "texture.js")}:
        data = (Path(__file__).resolve().parents[2]/"perception/map3d/web"/path.split("/")[-1]).read_bytes()
        handler.api_headers(200, "text/javascript; charset=utf-8", len(data))
        handler.wfile.write(data)
    else:
        handler.respond(404, {"status": "absent"})
    return True
