"""Public synthetic survey and four independent start poses for the opt-in navigation bench."""
from navigation.planner import map_reference


def catalog():
    ground = {"site_id": "bench", "map_id": "ground", "revision": "1", "frame_id": "map", "kind": "occupancy",
              "geometry": {"frame": "map", "origin": [0, 0, 0], "resolution": 1, "width": 9, "height": 9,
                           "cells": [100 if x == 4 and y <= 4 else 0 for y in range(9) for x in range(9)]}}
    def graph(name, nodes, corridors):
        return {"site_id": "bench", "map_id": name, "revision": "1", "frame_id": "map", "kind": "graph",
                "geometry": {"nodes": nodes, "edges": [{"from": a, "to": b, "tags": [tag]}
                             for a, b, tag in corridors]}}
    walking = graph("walkways", {"entry": [1.5, 1.5], "ramp": [4.5, 5.5], "office": [7.5, 1.5]},
                    [("entry", "office", "stairs"), ("entry", "ramp", "level"), ("ramp", "office", "level")])
    air = graph("airspace", {"entry": [1.5, 1.5, 1], "overhead": [4.5, 1.5, 3], "office": [7.5, 1.5, 3]},
                [("entry", "overhead", "air"), ("overhead", "office", "air")])
    return {"maps": {"ground": ground, "walkways": walking, "airspace": air},
            "profiles": {
                "wheeled": {"route_type": "ground", "dimensions": 2, "algorithm_ref": "astar", "clearance_m": 0.25},
                "quadruped": {"route_type": "terrain", "dimensions": 2, "algorithm_ref": "dijkstra", "allowed_tags": ["level"]},
                "pedestrian": {"route_type": "guidance", "dimensions": 2, "algorithm_ref": "astar", "allowed_tags": ["stairs", "level"]},
                "multicopter": {"route_type": "flight", "dimensions": 3, "algorithm_ref": "astar", "allowed_tags": ["air"]}},
            "actors": {"rover": {"map_key": "ground", "profile_ref": "wheeled"},
                       "dog": {"map_key": "walkways", "profile_ref": "quadruped"},
                       "person": {"map_key": "walkways", "profile_ref": "pedestrian"},
                       "drone": {"map_key": "airspace", "profile_ref": "multicopter"}},
            "destinations": {"office": [
                {"map_key": "ground", "profiles": ["wheeled"], "position_m": [7.5, 1.5]},
                {"map_key": "walkways", "profiles": ["quadruped", "pedestrian"], "position_m": [7.5, 1.5]},
                {"map_key": "airspace", "profiles": ["multicopter"], "position_m": [7.5, 1.5, 3]}]}}


def start_pose(actor_ref):
    site = catalog()
    record = site["maps"][site["actors"][actor_ref]["map_key"]]
    return {"actor_ref": actor_ref, "map_ref": map_reference(record), "frame_id": "map",
            "localization_epoch": "bench-1", "clock_id": "replay", "stamp_ns": 1000000000,
            "position_m": [1.5, 1.5, 1.0] if actor_ref == "drone" else [1.5, 1.5]}
