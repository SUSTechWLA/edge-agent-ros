"""Loopback-only DSH archive workbench. All observations and planner clocks come from recordings."""
import argparse
from contextlib import ExitStack
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from edge_contracts.service_directory import PluginUnavailable
from .core import SceneError
from .replay_planning import ReplayPlanning, ROOT
from .runtime import ReplaySite, SceneRuntimeError
from .textured_glb import encode_textured_glb


ARCHIVE_ID = "jetson-20261123"
DEFAULT_ARCHIVE = ROOT / "artifacts/verification/jetson-field-scene-20261123/sensor-evidence"
WEB = ROOT / "console/web"
FUSION_RENDERER = {"/fusion-grid.js": "grid.js", "/fusion-spatial.js": "spatial.js"}
ASSETS = {"fusion-map.html": "text/html; charset=utf-8", "fusion-map.css": "text/css; charset=utf-8",
          "fusion-map.js": "text/javascript; charset=utf-8",
          "scene-layers.html": "text/html; charset=utf-8", "scene-layers.css": "text/css; charset=utf-8",
          "scene-layers.js": "text/javascript; charset=utf-8", "plan-scene.js": "text/javascript; charset=utf-8"}


class Workbench:
    def __init__(self, site, planner):
        self.site = site
        self.planner = planner
        self.archive_id = ARCHIVE_ID if site.archive is not None else site.fusion_manifest_ref

    def state(self):
        catalog = self.site.catalog()
        ready = any(row.get("kind") == "texture" and row.get("status") == "ready" for row in catalog["plugins"])
        label = "Jetson · 双 MID-360 / RealSense D435" if self.site.archive is not None else "跨传感器标定回放"
        return {"archive": {"id": self.archive_id, "label": label},
                "texture_enabled": ready, "plugins": catalog["plugins"], "cases": sorted(self.planner.fixtures) if self.planner else [],
                "mode": "archive_replay", "motion_authorized": False}

    def layer(self, kind, scene_ref=None, layer_ref=None):
        if scene_ref is None:
            frozen = self.site.freeze([kind])
            if frozen.get("status") != "ready":
                return 422, frozen
            scene_ref = frozen["scene_ref"]
            layer_ref = frozen["scene"]["layers"][0]["layer_ref"]
        pinned = self.site.read(kind, layer_ref, scene_ref=scene_ref)
        return 200, {"descriptor": pinned["layer"], "layer_ref": layer_ref,
                     "scene_ref": scene_ref, "scene": pinned["data"]}

    def post(self, path, body):
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        if path in ("/api/layers", "/api/compose"):
            if body.get("archive") != self.archive_id:
                raise ValueError("archive is unavailable")
        if path == "/api/layers" and set(body) in ({"archive", "layer"}, {"archive", "layer", "scene_ref", "layer_ref"}):
            if not isinstance(body["layer"], str) or ("scene_ref" in body and not all(
                    isinstance(body[k], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", body[k]) for k in ("scene_ref", "layer_ref"))):
                raise ValueError("invalid layer or frozen reference")
            return self.layer(body["layer"], body.get("scene_ref"), body.get("layer_ref"))
        if path == "/api/compose" and set(body) in ({"archive", "layers"}, {"archive", "layers", "limits"}):
            layers = body["layers"]
            limits = body.get("limits", {})
            if (not isinstance(layers, list) or not 1 <= len(layers) <= 12
                    or any(not isinstance(k, str) for k in layers) or len(layers) != len(set(layers))):
                raise ValueError("expected distinct map layer names")
            if not isinstance(limits, dict) or set(limits) - {"roi_m", "frame_id", "max_error_m", "max_resolution_m", "max_skew_ms"}:
                raise ValueError("unknown scene query constraint")
            result = self.site.freeze(layers, **limits)
            return (200 if result.get("status") == "ready" else 422), result
        if path == "/api/texture" and set(body) == {"enabled"} and type(body["enabled"]) is bool:
            change = self.site.enable("texture") if body["enabled"] else self.site.disable("texture")
            return 200, {**self.state(), "change": change}
        if path == "/api/plan" and set(body) == {"case"} and isinstance(body["case"], str):
            if not self.planner:
                raise SceneRuntimeError("scene.provider_not_configured")
            return 200, self.planner.run(body["case"])
        raise ValueError("unknown archive operation or fields")

    def textured_map(self, scene_ref, layer_ref):
        result = self.site.read("texture", layer_ref, scene_ref=scene_ref)
        layer = result["layer"]
        png = self.site.texture_png(layer_ref, expected=layer["provider"]["binding"])
        return encode_textured_glb(scene_ref, layer, result["data"], png)


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, value, mime=None, filename=None):
        payload = value if mime else json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def route(self):
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed or self.headers.get("Origin") not in (None, "http://" + host):
            return self.respond(403, {"code": "workbench.origin"})
        path = urlsplit(self.path).path
        work = self.server.workbench
        try:
            if self.command == "GET":
                if path in FUSION_RENDERER:
                    asset = ROOT / "perception/map3d/web" / FUSION_RENDERER[path]
                    return self.respond(200, asset.read_bytes(), "text/javascript; charset=utf-8")
                name = path[1:]
                if path == "/":
                    name = "fusion-map.html" if work.site.archive is None else "scene-layers.html"
                elif path == "/fusion":
                    name = "fusion-map.html"
                if name in ASSETS:
                    return self.respond(200, (WEB / name).read_bytes(), ASSETS[name])
                if path == "/api/state":
                    return self.respond(200, work.state())
                if path == "/api/fusion/map":
                    status, result = work.layer("fusion")
                    return self.respond(status, result)
                cloud = re.fullmatch(r"/api/fusion/([0-9a-f]{64})/([0-9a-f]{64})\.ply", path)
                if cloud:
                    from .lidar_rgb import encode_ply
                    result = work.site.read("fusion", "sha256:" + cloud[2], scene_ref="sha256:" + cloud[1])
                    return self.respond(200, encode_ply(result["data"]), "application/octet-stream",
                                        f"lidar-rgb-{cloud[2][:12]}.ply")
                scene = re.fullmatch(r"/api/scenes/([0-9a-f]{64})", path)
                if scene:
                    return self.respond(200, work.site.snapshot("sha256:" + scene[1]))
                model = re.fullmatch(r"/api/maps/([0-9a-f]{64})/([0-9a-f]{64})\.glb", path)
                if model:
                    data = work.textured_map("sha256:" + model[1], "sha256:" + model[2])
                    return self.respond(200, data, "model/gltf-binary", f"rgbd-surface-{model[2][:12]}.glb")
                match = re.fullmatch(r"/camera/([0-9a-f]{64})\.png", path)
                if match:
                    descriptor = work.site.describe("texture")
                    png = work.site.texture_png(descriptor["layer_ref"])
                    if sha256(png).hexdigest() == match[1]:
                        return self.respond(200, png, "image/png")
                return self.respond(404, {"code": "workbench.missing"})
            if (len(self.headers.get_all("Content-Length", [])) != 1 or self.headers.get("Transfer-Encoding")
                    or self.headers.get_content_type() != "application/json"):
                return self.respond(400, {"code": "workbench.invalid_request"})
            length = int(self.headers["Content-Length"])
            if not 0 < length <= 8192:
                return self.respond(413, {"code": "workbench.request_size"})
            self.connection.settimeout(3)
            status, result = work.post(path, json.loads(self.rfile.read(length)))
            self.respond(status, result)
        except (PluginUnavailable, SceneRuntimeError, SceneError) as error:
            self.respond(409, {"status": "refused", "code": error.code, "message": str(error), "scene_ref": None})
        except (ValueError, KeyError, TypeError, TimeoutError) as error:
            self.respond(400, {"status": "refused", "code": "workbench.invalid_request", "message": str(error)})
        except OSError as error:
            self.respond(503, {"status": "unavailable", "code": "workbench.upstream", "message": str(error)})

    do_GET = route
    do_POST = route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--archive", type=Path)
    source.add_argument("--fusion-directory", type=Path, help="paired LiDAR/RGB-D capture or registered survey replay directory")
    parser.add_argument("--fusion-manifest-ref", help="required sha256 digest of the selected capture.json or map.json")
    parser.add_argument("--state-parent", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--grid-directory", type=Path, help="optional independent archived Nav2 map")
    parser.add_argument("--grid-manifest-ref", help="required sha256 digest of the optional Nav2 archive manifest")
    parser.add_argument("--port", type=int, default=8757)
    options = parser.parse_args()
    if bool(options.fusion_directory) != bool(options.fusion_manifest_ref):
        parser.error("--fusion-directory and --fusion-manifest-ref must be supplied together")
    archive = None if options.fusion_directory else (options.archive or DEFAULT_ARCHIVE).resolve()
    if bool(options.grid_directory) != bool(options.grid_manifest_ref):
        parser.error("--grid-directory and --grid-manifest-ref must be supplied together")
    options.state_parent.mkdir(exist_ok=True, parents=True)
    with ExitStack() as resources:
        server = resources.enter_context(ThreadingHTTPServer(("127.0.0.1", options.port), Handler))
        state = Path(tempfile.mkdtemp(prefix="scene057-", dir=options.state_parent))
        site = resources.enter_context(ReplaySite(archive, state,
            grid_directory=options.grid_directory, grid_manifest_ref=options.grid_manifest_ref,
            fusion_directory=options.fusion_directory, fusion_manifest_ref=options.fusion_manifest_ref))
        server.workbench = Workbench(site, ReplayPlanning(resources, archive, state) if archive else None)
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/", "state_directory": str(state)}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
