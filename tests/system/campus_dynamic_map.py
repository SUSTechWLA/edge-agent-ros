"""Import campus geometry and registered indoor and outdoor stop positions."""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from edge_contracts.occupancy import Grid, clearance_mask
from exploration.survey_providers import Handler, MapStore, PreviewHTTP, indexed_signature
from exploration.survey_semantic import identity
from exploration.site_config import site_config

MAP_ID = "campus-teaching-survey"
CONFIG = Path("/teaching-navigation.json")
LAYOUT = Path("/campus/layout.json")


def site_markers(config, layout, grid):
    zones = {z["id"]: z for z in layout["zones"]}
    nodes = {n["id"]: n for n in layout["nodes"]}
    safe, _ = clearance_mask(grid, config["radius"])
    markers = []
    for item in config["annotations"]:
        if "layout_node" in item:
            node = nodes[item["layout_node"]]
            zone = zones[node["zone"]]
            pose = [*node["xy"], 0.]
            aliases = [*item["aliases"], node["label"], *node.get("aliases", [])]
            source = "simulation_layout_node"
        else:
            zone = zones[item["zone"]]
            pose = item["entrance_pose"]
            aliases = item["aliases"]
            source = "simulation_site_annotation"
        if not all(identity(a, 120) for a in [item["name"], item["entity_id"], *aliases]):
            raise ValueError("invalid_campus_place_label")
        x, y, _ = pose
        index = grid.index([x,y])
        for bounds in (zone["bounds"], config["area"]["bounds"]):
            x0, y0, x1, y1 = bounds
            if not x0 < x < x1 or not y0 < y < y1:
                raise ValueError("stop position outside registered area")
        if index < 0 or not safe[index]:
            raise ValueError("imported stop position inside excluded footprint")
        markers.append(dict(**{k:item[k] for k in ("entity_id","name")},
                            aliases=sorted(set(aliases)-{item["name"]}), entrance_pose=pose, anchor=[x,y],
                            frame=grid.frame, environment=zone["kind"], zone=zone["id"], zone_label=zone["label"],
                            state="confirmed", source_kind=source))
    if not markers or len({m["entity_id"] for m in markers}) != len(markers):
        raise ValueError("campus requires unique semantic entity IDs")
    return markers


def publication(config=None):
    config = config or json.loads(CONFIG.read_bytes())
    layout = json.loads(LAYOUT.read_bytes())
    if layout["id"] != "campus_large" or layout["frame"] != "odom":
        raise ValueError("campus layout identity mismatch")
    floor_objects = [f for f in layout["features"] if f["base_z"] < .6]
    bounds = config["area"]["bounds"]
    site = config.get("site")
    size, resolution, origin = 120, .1, [-6., -6.]
    if site:
        from campus_walk_grid import on_walk, occupied, paved_edges, static_obstacles
        base_bounds = json.loads(CONFIG.read_bytes())["area"]["bounds"]
        nodes, corridors, world = paved_edges(LAYOUT.parent,site,layout["revision"])
        obstacles = static_obstacles(world,bounds)
        size, resolution, origin = site["grid"]["cells"], site["grid"]["resolution_m"], site["grid"]["origin"]
        layout["zones"].append(site["zone"])
        config = dict(config, annotations=[*config["annotations"], *[
            dict(marker, entrance_pose=[*nodes[marker["walk_node"]],0.], zone=site["zone"]["id"])
            for marker in site["annotations"]]])
    cells = []
    for y in range(size):
        for x in range(size):
            px, py = (x+.5)*resolution+origin[0], (y+.5)*resolution+origin[1]
            known = bounds[0] <= px <= bounds[2] and bounds[1] <= py <= bounds[3]
            if site:
                known = known and (base_bounds[0]<=px<=base_bounds[2] and base_bounds[1]<=py<=base_bounds[3]
                                   or on_walk(px,py,corridors))
                blocked = occupied(px,py,obstacles,resolution)
            else:
                blocked = any(abs(px-f["xy"][0]) <= (f["size"][0]+.1)/2 and
                              abs(py-f["xy"][1]) <= (f["size"][1]+.1)/2 for f in floor_objects)
            cells.append(100 if blocked else 0 if known else -1)
    grid = dict(width=size, height=size, resolution=struct.unpack("f", struct.pack("f", resolution))[0],
                origin=[*origin, 0.], frame=layout["frame"], cells=cells)
    markers = site_markers(config, layout, Grid(grid))
    map_id = config["navigation_map_id"]
    meta = dict(map_id=map_id, scene_revision=layout["revision"],
                layout_sha256=hashlib.sha256(LAYOUT.read_bytes()).hexdigest(),
                world_sha256=hashlib.sha256(Path("/campus/world.sdf").read_bytes()).hexdigest(),
                source="campus_large.layout", annotation_source=CONFIG.name)
    if site:
        meta.update(walk_nodes=site["walk"]["nodes"], route_source="campus_large.overview.manifest",
                    site_sha256=indexed_signature(site))
    revision = indexed_signature([grid, markers, meta])
    map_ref = dict(id=map_id, frame="odom", digest=revision, revision=revision)
    scene = dict(map_ref=map_ref, frame=layout["frame"], source=meta["source"], features=layout["features"],
                 zones=layout["zones"])
    return dict(status="ok", map_ref=map_ref, scene_geometry=scene, grid=grid, semantic=markers, manifest=meta, avoid=[])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "seed"))
    args = parser.parse_args()
    config = site_config(CONFIG)
    data = publication(config)
    map_id = data["map_ref"]["id"]
    if args.command == "serve":
        surveys = MapStore("/var/lib/edge-survey-maps")
        imports = {map_id:data}
        if map_id != MAP_ID:
            imports[MAP_ID] = publication()
        def publish(body):
            if body.get("map_id") in imports:
                raise ValueError("imported_map_id_reserved")
            return surveys.publish(body)

        def load(body):
            if body.get("map_id") not in imports:
                return surveys.load(body)
            data = imports[body["map_id"]]
            if body.get("revision") not in (None, data["map_ref"]["revision"]):
                raise LookupError("map_not_found")
            return data
        with PreviewHTTP(("0.0.0.0", 8138), {"/load": load, "/publish": publish}, Handler) as server:
            server.serve_forever()
    else:
        from exploration.ros_sim import checkpoint
        path = Path("/session/sessions.json")
        rows = json.loads(path.read_bytes()) if path.is_file() else {}
        if map_id in rows and rows[map_id].get("map_ref") != data["map_ref"]:
            raise ValueError("existing site session belongs to a different map")
        if map_id not in rows:
            grid = data["grid"]
            known = sum(c >= 0 for c in grid["cells"])
            rows[map_id] = dict(id=map_id, kind="map_import", text=config["area"]["aliases"][0]+"底图与地点标注",
                status="SUCCEEDED", phase="MAP_PUBLISHED", events=[], map_ref=data["map_ref"],
                area=config["area"]["bounds"], grid=grid,
                coverage=dict(known=known, total=len(grid["cells"]), unknown=len(grid["cells"])-known),
                markers=[dict(x=m["entrance_pose"][0], y=m["entrance_pose"][1], label=m["name"],
                              status=m["state"], entity_id=m["entity_id"]) for m in data["semantic"]],
                evidence=data["manifest"])
        rows[map_id]["scene_geometry"] = data["scene_geometry"]
        checkpoint(path, rows)
        print(json.dumps(data["map_ref"]))


if __name__ == "__main__":
    main()
