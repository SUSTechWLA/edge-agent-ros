"""World-coordinate static shapes shared by the campus facilities."""
import math
from xml.etree import ElementTree as ET


def tag(parent, name, value):
    child = ET.SubElement(parent, name)
    child.text = value
    return child


def values(numbers):
    return " ".join(f"{n:.6g}" for n in numbers)


def static(world, name, position=None, yaw=0):
    model = ET.SubElement(world, "model", name=name)
    tag(model, "static", "true")
    if position is not None:
        tag(model, "pose", values((*position,0,0,yaw)))
    return ET.SubElement(model, "link", name="site")


def shape(link, name, kind, position, dimensions, color, solid=False, yaw=0):
    for role in (("visual", "collision") if solid else ("visual",)):
        part = ET.SubElement(link, role, name=name)
        tag(part, "pose", values((*position, 0, 0, yaw)))
        geom = ET.SubElement(ET.SubElement(part, "geometry"), kind)
        if kind == "box":
            tag(geom, "size", values(dimensions))
        elif kind == "cylinder":
            tag(geom, "radius", str(dimensions[0]))
            tag(geom, "length", str(dimensions[1]))
        elif kind == "ellipsoid":
            tag(geom, "radii", values(dimensions))
        if role == "visual":
            mat = ET.SubElement(part, "material")
            tag(mat, "ambient", values((*color, 1)))
            tag(mat, "diffuse", values((*color, 1)))


def line(link, name, a, b, width, z, color, height=.014):
    dx, dy = b[0]-a[0], b[1]-a[1]
    shape(link,name,"box",((a[0]+b[0])/2,(a[1]+b[1])/2,z),(math.hypot(dx,dy),width,height),
          color,yaw=math.atan2(dy,dx))


def include(world, name, uri, position, yaw=0):
    element = ET.SubElement(world,"include")
    tag(element,"uri",uri)
    tag(element,"name",name)
    tag(element,"pose",values((*position,0,0,yaw)))
    tag(element,"static","true")
