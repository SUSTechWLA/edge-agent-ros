"""Build the navigation fixtures inside the generated campus_large Gazebo world."""
from dataclasses import dataclass, replace
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class Actor:
    name: str
    x: float
    start_y: float
    end_y: float
    width: float
    depth: float
    speed: float
    delay: float

    @property
    def direction(self):
        return 1 if self.end_y > self.start_y else -1

    @property
    def truth(self):
        return f"/sdd058/fixture/dynamic/{self.name}/odom"

    @property
    def contacts(self):
        return f"/sdd058/fixture/dynamic/{self.name}/contacts"

    @property
    def command(self):
        return f"/model/{self.name}/joint/guide/cmd_vel"


DEPLOYMENT = {
    "crossing": ("188", "sdd058_campus_crossing"),
    "blocked": ("189", "sdd058_campus_blocked"),
    "doorway": ("190", "sdd058_campus_doorway"),
    "phase1": ("191", "sdd058_campus_phase1"),
}
PEDESTRIANS = (
    Actor("pedestrian_1", .78, -1.35, 1.97, .35, .38, .62, 0.0),
    Actor("pedestrian_2", 1.22, 1.62, -1.97, .34, .36, .58, .50),
)
AT_DOOR = tuple(replace(person, speed=0.) for person in PEDESTRIANS)
CASES = {"crossing": PEDESTRIANS, "doorway": AT_DOOR, "phase1": AT_DOOR,
         "blocked": (Actor("lane_barrier", 1.02, -1.48, .12, .32, 1.14, .55, 0.0),)}


def add(node, key, value=None, **attributes):
    element = ET.SubElement(node, key, attributes)
    if value is not None:
        element.text = " ".join(map(str, value)) if isinstance(value, tuple) else str(value)
    return element


def cuboid(link, name, size, color=None, pose=None, collision=True):
    for kind in ("collision", "visual") if collision else ("visual",):
        part = add(link, kind, name=name)
        if pose:
            add(part, "pose", pose)
        add(add(add(part, "geometry"), "box"), "size", size)
        if kind == "visual" and color:
            material = add(part, "material")
            add(material, "ambient", color)
            add(material, "diffuse", color)


def contact_sensor(link, collision_name, topic):
    sensor = add(link, "sensor", name="fixture_contacts", type="contact")
    add(sensor, "always_on", "true")
    add(sensor, "update_rate", 50)
    contact = add(sensor, "contact")
    add(contact, "collision", collision_name)
    add(contact, "topic", topic)


def crossing(world, *, name, start, size, direction, travel, truth_topic, contact_topic, color):
    model = add(world, "model", name=name, canonical_link="torso")
    add(model, "pose", (*start, 0, 0, 0))
    body = add(model, "link", name="torso")
    inertial = add(body, "inertial")
    add(inertial, "mass", 8)
    inertia = add(inertial, "inertia")
    for axis, v in (("ixx", .40), ("iyy", .39), ("izz", .25)):
        add(inertia, axis, v)
    cuboid(body, "surrogate", size, color)
    contact_sensor(body, "surrogate", contact_topic)
    joint = add(model, "joint", name="guide", type="prismatic")
    add(joint, "parent", "world")
    add(joint, "child", "torso")
    axis = add(joint, "axis")
    add(axis, "xyz", (0, direction, 0))
    limit = add(axis, "limit")
    add(limit, "lower", 0)
    add(limit, "upper", travel)
    add(limit, "effort", 1e6)
    controller = add(model, "plugin", filename="gz-sim-joint-controller-system",
                     name="gz::sim::systems::JointController")
    add(controller, "joint_name", "guide")
    truth = add(model, "plugin", filename="gz-sim-odometry-publisher-system",
                name="gz::sim::systems::OdometryPublisher")
    for key, value in (("odom_topic", truth_topic), ("odom_frame", "world"),
                       ("robot_base_frame", "torso"), ("dimensions", 2), ("odom_publish_frequency", 50)):
        add(truth, key, value)


def build(campus, output, case):
    tree = ET.parse(campus)
    world = tree.getroot().find("world")
    for name in ("teaching", "delft_surveyed_site", "edge_rover"):
        if world.find(f"model[@name='{name}']") is None:
            raise ValueError("Expected the generated campus_large world")
    add(world, "plugin", filename="gz-sim-contact-system", name="gz::sim::systems::Contact")
    for actor, color in zip(CASES[case], ((.85, .45, .08, 1), (.2, .7, .64, 1))):
        crossing(world, name=actor.name, start=(actor.x, actor.start_y, .34),
                 size=(actor.width, actor.depth, .65), direction=actor.direction,
                 travel=abs(actor.end_y - actor.start_y), truth_topic=actor.truth,
                 contact_topic=actor.contacts, color=color)
    for link in world.findall("model[@name='edge_rover']/link"):
        collision = link.find("collision")
        if collision is not None:
            contact_sensor(link, collision.get("name"), "/sdd058/fixture/contacts/" + link.get("name"))
    tree.write(output, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campus")
    parser.add_argument("output")
    parser.add_argument("--case", choices=tuple(CASES), required=True)
    args = parser.parse_args()
    build(args.campus, args.output, args.case)
