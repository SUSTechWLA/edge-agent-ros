"""DSH providers for measured exploration, semantics, routes, and map publication."""

import argparse
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import logging
from pathlib import Path
import re
import sqlite3

from edge_contracts.occupancy import Grid, _json, finite, save_map
from edge_contracts.preview_http import PreviewHTTP, json_reply
from edge_contracts.semantic_scene import compact_scene
from exploration.corridor_review import review_corridor
from exploration.survey_route import route
from exploration.survey_semantic import assess_semantics, identity, is_evidenced_marker, resolve_place
from exploration.survey_strategy import frontier, validate_avoid_regions, xy


MAP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
REVISION = re.compile(r"[0-9a-f]{64}")
REQUEST_LIMIT = 2_000_000
ROLES = {"frontier": ("/frontier",), "semantic": ("/semantic","/resolve"),
         "maps": ("/publish", "/load", "/scene"), "route": ("/route",), "corridor": ("/corridor-review",)}
ROLES["readonly"] = tuple(path for role in ("frontier", "semantic", "route", "corridor") for path in ROLES[role])


def match(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def indexed_signature(value):
    return hashlib.sha256(_json(value)).hexdigest()


def candidate_valid(item):
    if (not isinstance(item, dict) or not identity(item.get("entity_id"))
            or not identity(item.get("name"), 64) or not xy(item.get("anchor"))
            or item.get("state") not in ("proposed", "confirmed")):
        return False
    pose = item.get("entrance_pose")
    return ((pose is None and item["state"] == "proposed")
            or (isinstance(pose, list) and len(pose) == 3 and all(finite(v) for v in pose)))


class MapStore:
    """Index only durable artifacts. An operation ID cannot change an admitted payload."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "map-catalog.sqlite"
        with self.open() as db:
            db.execute("CREATE TABLE IF NOT EXISTS maps (operation TEXT PRIMARY KEY, "
                       "map_id TEXT NOT NULL, revision TEXT NOT NULL, frame TEXT NOT NULL, "
                       "UNIQUE (map_id, revision))")

    @contextmanager
    def open(self):
        db = sqlite3.connect(self.database, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def publish(self, body):
        map_id, operation = body["map_id"], body["operation_id"]
        raw_grid, semantic = body["grid"], body["semantic"]
        if (not match(MAP_ID, map_id) or not identity(operation, 240) or not isinstance(semantic, list)
                or len(semantic) > 128 or not all(candidate_valid(row) for row in semantic)
                or len({row["entity_id"] for row in semantic}) != len(semantic)):
            raise ValueError("invalid_map_publication")
        grid = Grid(raw_grid)
        if any(item["state"] == "confirmed" and not is_evidenced_marker(item, grid) for item in semantic):
            raise ValueError("confirmed_marker_evidence_invalid")
        data = dict(frame=grid.frame, origin=[grid.ox, grid.oy, grid.yaw], width=grid.width,
                    height=grid.height, resolution=grid.resolution, cells=list(grid.cells))
        metadata = {key: body[key] for key in
                    ("operation_id", "robot_id", "calibration_id", "stamp_ns", "state", "reason")}
        if any(not identity(metadata[key], 240) for key in ("robot_id", "calibration_id", "state", "reason")):
            raise ValueError("invalid_map_provenance")
        metadata.update(map_id=map_id, semantic=semantic)
        if "avoid" in body:
            metadata["avoid"] = validate_avoid_regions(body["avoid"])
        revision = indexed_signature([data, metadata])
        ref = {"id": map_id, "revision": revision, "frame": grid.frame, "digest": revision}
        with self.open() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT map_id, revision FROM maps WHERE operation=?", (operation,)).fetchone()
            if existing is not None and existing != (map_id, revision):
                raise ValueError("map_operation_conflict")
            manifest = save_map(self.directory / map_id / revision, data, metadata=metadata)
            if existing is None:
                db.execute("INSERT INTO maps VALUES (?, ?, ?, ?)", (operation, map_id, revision, grid.frame))
        return {"status": "ok", "map_ref": ref, "manifest": manifest}

    def scene(self, body):
        """Read a bounded set of semantic entities from one verified, immutable map."""
        if set(body) - {"map_id", "revision", "query", "limit"} or not match(REVISION, body.get("revision")):
            raise ValueError("scene_revision_required")
        limit, query = body.get("limit", 4), body.get("query", "")
        if type(limit) != int or not 1 <= limit <= 6 or not isinstance(query, str) or len(query) > 96:
            raise ValueError("invalid_scene_query")
        loaded = self.load(body)
        ref = loaded["map_ref"]
        query = query.strip().casefold()
        rows = []
        for row in loaded["semantic"]:
            names = [row["entity_id"], row["name"], *row.get("aliases", [])]
            if not query or any(query == str(name).casefold() for name in names):
                rows.append(row)
        nodes, edges = [], []
        for row in sorted(rows, key=lambda row: row["entity_id"])[:limit]:
            node = dict(id=row["entity_id"], label=row["name"], kind="place",
                        state=row["state"], position_m=row["anchor"])
            nodes.append(node)
            if row.get("entrance_pose") is not None:
                x, y, yaw = row["entrance_pose"]
                approach = "approach." + indexed_signature(row["entity_id"])[:20]
                nodes.append(dict(id=approach, label=row["name"], kind="viewpoint",
                                  state=row["state"], position_m=[x, y], yaw_rad=yaw))
                edges.append([approach, "approach_to", node["id"]])
        return compact_scene(dict(version=1, scope="map_revision", source_ref=ref["revision"],
                                  frame=ref["frame"], map_ref=ref, nodes=nodes, edges=edges,
                                  stamp_ns=str(loaded["manifest"]["metadata"]["stamp_ns"]),
                                  omitted=max(0, len(rows)-limit)))

    def load(self, body):
        map_id, revision = body["map_id"], body.get("revision")
        if not match(MAP_ID, map_id) or revision is not None and not match(REVISION, revision):
            raise ValueError("invalid_map_reference")
        with self.open() as db:
            if revision is None:
                rows = db.execute("SELECT revision, frame, operation FROM maps WHERE map_id=? LIMIT 2",
                                  (map_id,)).fetchall()
            else:
                rows = db.execute("SELECT revision, frame, operation FROM maps WHERE map_id=? AND revision=?",
                                  (map_id, revision)).fetchall()
        if not rows:
            raise LookupError("map_not_found")
        if len(rows) != 1:
            raise ValueError("map_revision_required")
        revision, frame, operation = rows[0]
        directory = self.directory / map_id / revision
        try:
            manifest = json.loads((directory / "manifest.json").read_bytes())
            data = (directory / "occupancy.json").read_bytes()
            metadata = manifest["metadata"]
            grid = json.loads(data)
            actual = {key: hashlib.sha256((directory / key).read_bytes()).hexdigest()
                      for key in ("occupancy.json", "map.yaml", "map.pgm")}
            valid = (actual == manifest["sha256"] and metadata["map_id"] == map_id
                     and metadata["operation_id"] == operation and grid["frame"] == frame
                     and indexed_signature([grid, metadata]) == revision)
            if not valid:
                raise ValueError("map_artifact_invalid")
        except (ValueError, TypeError, OSError, KeyError) as error:
            raise ValueError("map_artifact_invalid") from error
        return {"status": "ok", "map_ref": {"id": map_id, "revision": revision, "frame": frame, "digest": revision},
                "manifest": manifest, "grid": grid, "semantic": metadata["semantic"], "avoid": metadata.get("avoid", [])}


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, value):
        _, media, raw, _ = json_reply(status, value)
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self.respond(200, {"status": "ok", "paths": sorted(self.server.node)})
        else:
            self.respond(404, {"error": "provider_endpoint_unavailable"})

    def do_POST(self):
        handler = self.server.node.get(self.path)
        if handler is None:
            self.respond(404, {"error": "provider_endpoint_unavailable"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= REQUEST_LIMIT:
                self.respond(413 if length > REQUEST_LIMIT else 400, {"error": "invalid_request_size"})
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("expected_object")
            result = handler(body)
        except KeyError:
            self.respond(400, {"error": "required_field_missing"})
        except LookupError as error:
            self.respond(404, {"error": str(error)})
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            self.respond(409 if str(error) == "map_operation_conflict" else 422, {"error": str(error)})
        except (OSError, sqlite3.Error):
            logging.exception("survey provider unavailable")
            self.respond(503, {"error": "provider_unavailable"})
        else:
            self.respond(200, result)

    def log_message(self, format, *args):
        if args and len(args) > 1 and str(args[1]) != "200":
            super().log_message(format, *args)


def serve(bind, port, state, role):
    endpoints = {"/frontier": frontier, "/semantic": assess_semantics, "/resolve": resolve_place, "/route": route,
                 "/corridor-review": review_corridor}
    if role in ("all", "maps"):
        maps = MapStore(state)
        endpoints.update({"/publish": maps.publish, "/load": maps.load, "/scene": maps.scene})
    if role != "all":
        endpoints = {path: endpoints[path] for path in ROLES[role]}
    with PreviewHTTP((bind, port), endpoints, Handler) as server:
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8138)
    parser.add_argument("--state", default="/var/lib/edge-survey-maps")
    parser.add_argument("--role", choices=("all", *ROLES), default="all")
    args = parser.parse_args()
    serve(args.bind, args.port, args.state, args.role)


if __name__ == "__main__":
    main()
