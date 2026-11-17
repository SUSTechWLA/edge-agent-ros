"""Build the Gazebo geometry from the same surveyed layout the plugin publishes."""
import os
from pathlib import Path
from xml.etree import ElementTree as ET

from layout import Layout


def generate(base, layout, output):
    tree = ET.parse(base)
    world = tree.getroot().find("world")
    def box(name,xy,size,z,color,solid):
        model=ET.SubElement(world,"model",name=name)
        ET.SubElement(model,"static").text="true"
        ET.SubElement(model,"pose").text=" ".join(map(str,[*xy,z,0,0,0]))
        link=ET.SubElement(model,"link",name="body")
        for tag in (("visual","collision") if solid else ("visual",)):
            part=ET.SubElement(link,tag,name="shape")
            ET.SubElement(ET.SubElement(ET.SubElement(part,"geometry"),"box"),"size").text=" ".join(map(str,size))
            if tag=="visual":
                ET.SubElement(ET.SubElement(part,"material"),"diffuse").text=color
    for z in layout.data["zones"]:
        x0,y0,x1,y1=z["bounds"]
        color={"indoor":"0.67 0.7 0.69 1","outdoor":"0.38 0.54 0.26 1","transition":"0.82 0.70 0.46 1"}
        box(z["id"]+"_surface",[(x0+x1)/2,(y0+y1)/2],[x1-x0,y1-y0,.002],.001,color[z["kind"]],False)
    for f in layout.data["features"]:
        box(f["id"],f["xy"],f["size"],f.get("base_z",0)+f["size"][2]/2,
            "0.24 0.45 0.25 1" if f["kind"]=="landmark" else "0.64 0.61 0.55 1",True)
    tree.write(output,encoding="utf-8",xml_declaration=True)


if __name__=="__main__":
    base="/opt/edge/sim/world.sdf"
    path=os.getenv("EDGE_SCENE_FILE","/opt/edge/scene/campus.json")
    output=base
    if path:
        output="/tmp/edge-world.sdf"
        generate(base,Layout(path),output)
    os.execlp("gz","gz","sim","-s","-r","--headless-rendering",output)
