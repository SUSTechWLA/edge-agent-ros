"""Check a Lingao footprint against the archived site's Xacro and instanced lidar mesh."""
import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = ROOT / "src/edge_gateway/config/footprints/lingao-archive-v1.json"
IDENTITY = tuple(tuple(int(i == j) for j in range(4)) for i in range(4))


def xml(source):
    root = ET.fromstring(source)
    for element in root.iter():
        element.tag = element.tag.split("}")[-1]
    return root


def multiply(a, b):
    return tuple(tuple(sum(x*y for x, y in zip(row, col)) for col in zip(*b)) for row in a)


def point(matrix, xyz):
    return tuple(sum(a*b for a, b in zip(row, (*xyz, 1))) for row in matrix[:3])


def scalar(token, variables):
    """The archived Xacro uses literals, signed property names, and division by 2."""
    expr = token.removeprefix("${").removesuffix("}").strip()
    name, _, divisor = expr.partition("/")
    raw = name.removeprefix("-")
    value = variables[raw] if raw in variables else float(raw)
    return (-value if name.startswith("-") else value) / (float(divisor) if divisor else 1)


def pose(origin, variables):
    x, y, z = (scalar(t, variables) for t in origin.get("xyz").split())
    r, p, a = (scalar(t, variables) for t in origin.get("rpy").split())
    cr, cp, ca = map(math.cos, (r, p, a))
    sr, sp, sa = map(math.sin, (r, p, a))
    return ((ca*cp, ca*sp*sr-sa*cr, ca*sp*cr+sa*sr, x),
            (sa*cp, sa*sp*sr+ca*cr, sa*sp*cr-ca*sr, y),
            (-sp, cp*sr, cp*cr, z), (0, 0, 0, 1))


def mesh_vertices(mesh):
    """Read geometry instances, not raw geometry arrays in unplaced part frames."""
    if mesh.findtext("asset/up_axis") != "Y_UP":
        raise ValueError("The archive check expects a Y-up COLLADA asset")
    unit = float(mesh.find("asset/unit").get("meter"))
    geometries = {}
    for geom in mesh.findall("library_geometries/geometry"):
        source = geom.find("mesh/vertices/input[@semantic='POSITION']").get("source")[1:]
        vertex_data = geom.find(f"mesh/source[@id='{source}']")
        accessor = vertex_data.find("technique_common/accessor")
        if accessor.get("stride") != "3" or accessor.get("offset", "0") != "0":
            raise ValueError("Unsupported COLLADA position layout")
        values = list(map(float, vertex_data.findtext("float_array").split()))
        if len(values) != 3*int(accessor.get("count")):
            raise ValueError("Invalid COLLADA POSITION count")
        geometries[geom.get("id")] = list(zip(values[::3], values[1::3], values[2::3]))

    def visit(node, parent):
        values = list(map(float, node.findtext("matrix").split()))
        world = multiply(parent, [values[i:i+4] for i in range(0, 16, 4)])
        for inst in node.findall("instance_geometry"):
            for v in geometries[inst.get("url")[1:]]:
                x, y, z = point(world, v)
                yield (unit*x, -unit*z, unit*y)
        for child in node.findall("node"):
            yield from visit(child, world)

    selected = mesh.find("scene/instance_visual_scene").get("url")[1:]
    for node in mesh.findall(f"library_visual_scenes/visual_scene[@id='{selected}']/node"):
        yield from visit(node, IDENTITY)


def bounds(points):
    axes = zip(*points)
    return {axis: [min(v), max(v)] for axis, v in zip(("x", "y", "z"), axes)}


def measure(profile, model, mesh):
    variables = {e.get("name"): float(e.get("value")) for e in model.findall("property")}
    variables["pi"] = math.pi
    joints = {j.find("child").get("link"): j for j in model.findall("joint")}
    base = profile["source"]["archive_origin"]

    def in_base(link):
        if link == base:
            return IDENTITY
        joint = joints[link]
        return multiply(in_base(joint.find("parent").get("link")), pose(joint.find("origin"), variables))

    parts = {}
    for name in ("base", "payload"):
        collision = model.find(f"link[@name='{name}']/collision")
        size = tuple(map(float, collision.find("geometry/box").get("size").split()))
        half = [(v/-2, v/2) for v in size]
        placed = multiply(in_base(name), pose(collision.find("origin"), variables))
        parts[name] = bounds(point(placed, v) for v in itertools.product(*half))

    # Exact projection of a capped cylinder onto each world axis, including its width.
    macro = model.find("macro[@name='wheel']")
    wheel = macro.find("link/collision")
    cyl = wheel.find("geometry/cylinder")
    radius = scalar(cyl.get("radius"), variables)
    width = scalar(cyl.get("length"), variables)
    for inst in model.findall("wheel"):
        params = dict(variables, x=scalar(inst.get("x"), variables), y=scalar(inst.get("y"), variables))
        joint = macro.find("joint")
        placed = multiply(in_base(joint.find("parent").get("link")), pose(joint.find("origin"), params))
        placed = multiply(placed, pose(wheel.find("origin"), params))
        component = {}
        for axis, (ux, uy, uz, center) in zip(("x", "y", "z"), placed[:3]):
            extent = radius*math.hypot(ux, uy) + width*abs(uz)/2
            component[axis] = [center-extent, center+extent]
        parts[inst.get("name")] = component

    vertices = list(mesh_vertices(mesh))
    for name in ("front_mid360", "rear_mid360"):
        visual = model.find(f"link[@name='{name}']/visual")
        geometry = visual.find("geometry/mesh")
        if Path(geometry.get("filename")).name != profile["source"]["lidar_mesh"]:
            raise ValueError(f"Unexpected mesh for {name}")
        scale = tuple(map(float, geometry.get("scale").split()))
        placed = multiply(in_base(name), pose(visual.find("origin"), variables))
        parts[name] = bounds(point(placed, tuple(v*s for v, s in zip(vertex, scale))) for vertex in vertices)

    modeled_links = {link.get("name") for link in model.findall("link")
                     if link.find("visual/geometry") is not None or link.find("collision/geometry") is not None}
    wheel_names = {wheel.get("name") for wheel in model.findall("wheel")}
    if len(wheel_names) != 4 or parts.keys() != modeled_links | wheel_names or not vertices:
        raise ValueError("Archived component set incomplete")
    envelope = {a: [min(b[a][0] for b in parts.values()), max(b[a][1] for b in parts.values())]
                for a in ("x", "y", "z")}
    extents = dict(front_m=envelope["x"][1], rear_m=-envelope["x"][0],
                   left_m=envelope["y"][1], right_m=-envelope["y"][0])
    spare = {name: profile[name]-size for name, size in extents.items()}
    kinematics = dict(wheelbase_m=variables["wheel_base"], track_m=variables["wheel_track"], wheel_radius_m=radius)
    passed = (min(spare.values()) >= 0 and profile["reference_point"] == "drive_center"
              and profile["kinematics"] == "differential_drive"
              and all(math.isclose(profile[k], v, abs_tol=1e-9) for k, v in kinematics.items()))
    return {"profile_contains_archive_model": passed, "frame": base, "bounds_units": "m",
            "lidar_instanced_position_count": len(vertices), "part_aabb_m": parts, "envelope_aabb_m": envelope,
            "profile_minus_model_m": spare, "kinematics": kinematics,
            "model_tire_width_m": width, "mesh_axis_conversion": "Y_UP to Z_UP: (x,-z,y)",
            "source_basis": "Site Xacro describes measured chassis dimensions and wheel spacing. "
                            "Lidar positions and visual mesh transforms are nominal.",
            "limitations": ["Xacro estimates tire width; the payload box is illustrative.",
                            "No confirmed installed D435 or RTK exterior, brackets, or cable envelope.",
                            "No loaded braking, slip, latency or current mount-position measurement.",
                            "XY/yaw of simulation base_link are aligned with the archive base_foot by profile."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    raw_profile = args.profile.read_bytes()
    profile = json.loads(raw_profile)
    source = profile["source"]
    sources = {}
    roots = []
    for name, digest in ((source["model"], source["sha256"]), (source["lidar_mesh"], source["mesh_sha256"])):
        raw = (args.model_dir/name).read_bytes()
        sources[name] = hashlib.sha256(raw).hexdigest()
        if sources[name] != digest:
            raise ValueError(f"Archive digest mismatch for {name}")
        roots.append(xml(raw))
    report = {"schema_version": 1, "profile_id": profile["profile_id"],
              "profile_sha256": hashlib.sha256(raw_profile).hexdigest(),
              "source_sha256": sources, **measure(profile, *roots)}
    output = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(output.encode())
    else:
        print(output, end="")
    return 0 if report["profile_contains_archive_model"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
