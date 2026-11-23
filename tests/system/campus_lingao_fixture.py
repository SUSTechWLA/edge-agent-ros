"""Dimension-matched four-wheel physics fixture for local, static-obstacle tests."""
import math
from xml.etree import ElementTree as ET


def field(node, tag, value):
    ET.SubElement(node, tag).text = str(value)


def pose(node, x, y, z, roll=0.):
    field(node, "pose", f"{x:.9g} {y:.9g} {z:.9g} {roll:.12g} 0 0")


def inertia(link, mass, diagonal, offset=0.):
    inertial = ET.SubElement(link, "inertial")
    pose(inertial, 0., 0., offset)
    field(inertial, "mass", mass)
    tensor = ET.SubElement(inertial, "inertia")
    for name, value in zip(("ixx", "iyy", "izz"), diagonal):
        field(tensor, name, value)


def box_shapes(link, name, size, offset=0.):
    for kind in ("collision", "visual"):
        node = ET.SubElement(link, kind, name=name)
        pose(node, 0., 0., offset)
        field(ET.SubElement(ET.SubElement(node, "geometry"), "box"), "size", " ".join(map(str, size)))


def box_inertia(link, mass, size, offset=0.):
    x,y,z = size
    inertia(link, mass, [mass*(y*y+z*z)/12, mass*(x*x+z*z)/12, mass*(x*x+y*y)/12], offset)


def fixed_box(rover, name, size, center, mass):
    link = ET.SubElement(rover, "link", name=name)
    pose(link, *center)
    box_shapes(link, name, size)
    box_inertia(link, mass, size)
    joint = ET.SubElement(rover, "joint", name=name+"_mount", type="fixed")
    field(joint, "parent", "base_link")
    field(joint, "child", name)


def install(rover):
    """Archive Xacro uses base_foot at ground, this SDF uses the wheel-centre plane."""
    chassis = (.467, .329, .190)
    radius, width, wheelbase, track = .075, .050, .240, .281
    base = rover.find("link[@name='base_link']")
    drive = rover.find("plugin[@filename='gz-sim-diff-drive-system']")
    for link in rover.findall("link"):
        if link is not base:
            rover.remove(link)
    for joint in rover.findall("joint"):
        rover.remove(joint)
    for child in list(base):
        if child.tag != "sensor" or child.get("name") not in ("imu", "horizontal_lidar"):
            base.remove(child)
    # Virtual 2D scanner above the payload. Its calibration is independent of
    # either real MID-360 and the physical RGB-D camera.
    base.find("sensor[@name='horizontal_lidar']/pose").text = "0.08 0 0.40 0 0 0"
    rover.find("pose").text = f"0 0 {radius+.001} 0 0 0"
    body_offset = .255 - chassis[2]/2 - radius
    box_shapes(base, "body", chassis, body_offset)
    box_inertia(base, 25., chassis, body_offset)
    fixed_box(rover, "payload", (.30, .25, .157), (0, 0, .255+.157/2-radius), 5.)
    # Axis-aligned envelopes of archived MID-360 meshes transformed by the
    # archived mount poses. Heights supplied here are relative to ground.
    for name, low, high in (
            ("front_mid360", (.171148,-.025917,.156), (.258852,.034180,.244)),
            ("rear_mid360", (-.202481,.075820,.218), (-.129692,.135917,.282))):
        center = [(a+b)/2 for a,b in zip(low,high)]
        center[2] -= radius
        fixed_box(rover, name, tuple(b-a for a,b in zip(low,high)), center, .3)
    for tag in ("left_joint", "right_joint"):
        for el in drive.findall(tag):
            drive.remove(el)
    drive.find("wheel_separation").text = str(track)
    drive.find("wheel_radius").text = str(radius)
    # Four powered tyres. A skid-steer wheel rolls longitudinally while its
    # contact patch can slip laterally during a turn. Friction coefficients
    # are simulation assumptions, not measured coefficients for this rover.
    for end, x in (("front", wheelbase/2), ("rear", -wheelbase/2)):
        for side, y in (("left", track/2), ("right", -track/2)):
            name = f"{end}_{side}_wheel"
            link = ET.SubElement(rover, "link", name=name)
            pose(link, x, y, 0., -math.pi/2)
            mass = 1.5
            transverse = mass*(3*radius*radius+width*width)/12
            inertia(link, mass, (transverse, transverse, mass*radius*radius/2))
            for kind in ("collision", "visual"):
                shell = ET.SubElement(link, kind, name="tyre")
                cylinder = ET.SubElement(ET.SubElement(shell, "geometry"), "cylinder")
                field(cylinder, "radius", radius)
                field(cylinder, "length", width)
                if kind == "collision":
                    friction = ET.SubElement(ET.SubElement(ET.SubElement(shell,"surface"),"friction"),"ode")
                    field(friction, "fdir1", "0 0 1")   # axle in tyre coordinates
                    field(friction, "mu", .035)
                    field(friction, "mu2", 1.0)
            joint = ET.SubElement(rover, "joint", name=name+"_axle", type="revolute")
            field(joint, "parent", "base_link")
            field(joint, "child", name)
            ET.SubElement(ET.SubElement(joint, "axis"), "xyz", expressed_in="__model__").text = "0 1 0"
            field(drive, side+"_joint", joint.get("name"))
