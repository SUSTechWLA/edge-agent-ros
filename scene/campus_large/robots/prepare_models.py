"""Build compact Gazebo link meshes from a pinned MuJoCo Menagerie checkout.

This maintainer-only converter requires numpy, scipy, trimesh and fast-simplification.
Simulation and browser export use the resulting offline models.zip.
"""
import argparse
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

import fast_simplification
import numpy as np
from scipy.spatial.transform import Rotation
import trimesh


HERE = Path(__file__).parent
COMMIT = "370c43965323c0944b433b202aac5058b5f907a9"
MODELS = {"crazyflie":("bitcraze_crazyflie_2","cf2"),
          "go1":("unitree_go1","go1"), "g1":("unitree_g1","g1")}


def numbers(text):
    return np.fromstring(text,sep=" ")


def vector(values):
    return " ".join(f"{x:.9g}" for x in values)


def element(parent,name,text):
    ET.SubElement(parent,name).text = str(text)


def transform(node):
    result = np.eye(4)
    result[:3,3] = numbers(node.get("pos","0 0 0"))
    quat = numbers(node.get("quat","1 0 0 0"))
    result[:3,:3] = Rotation.from_quat(quat[[1,2,3,0]]).as_matrix()
    return result


def pose(matrix):
    return vector([*matrix[:3,3],*Rotation.from_matrix(matrix[:3,:3]).as_euler("xyz")])


@lru_cache
def geometry(path):
    mesh = trimesh.load_mesh(path,process=True)
    if isinstance(mesh,trimesh.Scene):
        mesh = mesh.to_geometry()
    if len(mesh.faces)>2200:
        points,faces = fast_simplification.simplify(np.asarray(mesh.vertices).copy(),
                            np.asarray(mesh.faces,dtype=np.int32).copy(),target_count=2200)
        mesh=trimesh.Trimesh(points,faces,process=False)
    return mesh


def obj(mesh):
    lines = ["# Decimated articulated robot link. Units: metres. Z up."]
    lines.extend("v %.7f %.7f %.7f" % tuple(p) for p in mesh.vertices)
    lines.extend("vn %.7f %.7f %.7f" % tuple(n) for n in mesh.vertex_normals)
    lines.extend("f "+" ".join(f"{i+1}//{i+1}" for i in face) for face in mesh.faces)
    return ("\n".join(lines)+"\n").encode()


def convert(name, folder, mjcf, blobs):
    tree = ET.parse(folder/(mjcf+".xml"))
    root = tree.find("./worldbody/body")
    materials = {a.get("name"):a.get("rgba") for a in tree.findall("./asset/material")}
    assets = {a.get("name") or Path(a.get("file")).stem:a.get("file") for a in tree.findall("./asset/mesh")}
    joint_names = [j.get("name") for j in root.iter("joint")]
    key = tree.find("./keyframe/key")
    neutral = numbers(key.get("qpos")) if key is not None else np.zeros(7)
    angles = dict(zip(joint_names,neutral[7:]))
    active = ({j for j in joint_names if j.endswith("_thigh_joint")} if name=="go1"
              else {"left_shoulder_pitch_joint"} if name=="g1" else set())
    links = {"base_link": np.eye(4)}
    groups = defaultdict(list)
    joints = {}
    used = set()

    def walk(body,parent_matrix,link):
        matrix = parent_matrix@transform(body)
        if body is root:
            # Model origin is at its feet for the two standing platforms and at
            # the vehicle centre for the quadrotor. Pose feedback shares it.
            matrix[:3,3] = neutral[:3] if name!="crazyflie" else 0
        j = body.find("joint")
        if j is not None:
            jid = j.get("name")
            axis = numbers(j.get("axis","1 0 0" if j.get("class")=="abduction" else "0 1 0"))
            neutral_rot = np.eye(4)
            neutral_rot[:3,:3] = Rotation.from_rotvec(axis*angles.get(jid,0)).as_matrix()
            matrix = matrix@neutral_rot
            if jid in active:
                link = body.get("name")
                links[link] = matrix.copy()
                joints[jid] = (link,axis)
        for g in body.findall("geom"):
            if g.get("class") != "visual" or not g.get("mesh"):
                continue
            filename = assets[g.get("mesh")]
            used.add(filename)
            mesh = geometry(folder/"assets"/filename).copy()
            mesh.apply_transform(np.linalg.inv(links[link])@matrix@transform(g))
            rgba = materials.get(g.get("material"),"0.25 0.25 0.25 1" if name=="go1" else "0.70 0.70 0.70 1")
            groups[(link,rgba)].append(mesh)
        for child in body.findall("body"):
            walk(child,matrix,link)

    walk(root,np.eye(4),"base_link")
    model=ET.Element("model",name=name)
    bounds = {}
    for link_name,matrix in links.items():
        link=ET.SubElement(model,"link",name=link_name)
        element(link,"pose",pose(matrix))
        element(link,"gravity","false")
        inertial=ET.SubElement(link,"inertial")
        relevant = [m for (ln,_),parts in groups.items() if ln==link_name for m in parts]
        all_mesh = trimesh.util.concatenate(relevant)
        bounds[link_name]=all_mesh.bounds
        # The servo benches combine several rigid links. Derive consistent box
        # inertias for their collision proxies; no free-walking balance claim.
        size = np.maximum(all_mesh.extents,.002)
        mass = .027 if name=="crazyflie" else (7.0 if name=="go1" else 28.) if link_name=="base_link" else .9
        if name!="crazyflie":
            element(inertial,"pose",vector([*all_mesh.bounds.mean(axis=0),0,0,0]))
        element(inertial,"mass",mass)
        inertia=ET.SubElement(inertial,"inertia")
        for key_,i,j in (("ixx",1,2),("iyy",0,2),("izz",0,1)):
            element(inertia,key_,mass*(size[i]**2+size[j]**2)/12)
        for key_ in ("ixy","ixz","iyz"):
            element(inertia,key_,0)
        for idx,((ln,rgba),parts) in enumerate(groups.items()):
            if ln!=link_name:
                continue
            visual=ET.SubElement(link,"visual",name=f"part_{idx}")
            shape=ET.SubElement(ET.SubElement(visual,"geometry"),"mesh")
            filename=f"{name}_{link_name}_{idx}.obj"
            element(shape,"uri","file:///campus/robots/"+filename)
            mat=ET.SubElement(visual,"material")
            element(mat,"diffuse",rgba)
            element(mat,"ambient",rgba)
            blobs[filename]=obj(trimesh.util.concatenate(parts))
        # Base-only conservative contact. Articulated verification observes the
        # joint encoder; motor benches don't model detailed self-contact.
        if link_name=="base_link":
            col=ET.SubElement(link,"collision",name="body")
            dimensions={"crazyflie":(.065,.065,.017),"go1":(.45,.20,.13),"g1":(.25,.31,.24)}[name]
            height={"crazyflie":0.,"go1":.27,"g1":.83}[name]
            element(col,"pose",vector([0,0,height,0,0,0]))
            element(ET.SubElement(ET.SubElement(col,"geometry"),"box"),"size",vector(dimensions))
    if joints:
        mount=ET.SubElement(model,"joint",name="bench_mount",type="fixed")
        element(mount,"parent","world")
        element(mount,"child","base_link")
    for jid,(child,axis) in joints.items():
        joint=ET.SubElement(model,"joint",name=jid,type="revolute")
        element(joint,"parent","base_link")
        element(joint,"child",child)
        joint_axis=ET.SubElement(joint,"axis")
        element(joint_axis,"xyz",vector(axis))
        limit=ET.SubElement(joint_axis,"limit")
        for tag,value in {"lower":-1.4 if name=="g1" else -.5,"upper":.5 if name=="g1" else .65,
                          "velocity":2,"effort":60}.items():
            element(limit,tag,value)
        element(ET.SubElement(joint_axis,"dynamics"),"damping",.15)
    ET.indent(model)
    blobs[name+".sdf"]=ET.tostring(model,encoding="utf-8",xml_declaration=True)+b"\n"
    files = [folder/(mjcf+".xml"),folder/"LICENSE",*(folder/"assets"/f for f in sorted(used))]
    return {str(f.relative_to(folder)).replace("\\","/"):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def rebuild(source):
    blobs,sources={},{}
    for name,(directory,mjcf) in MODELS.items():
        folder=source/directory
        sources[directory]=convert(name,folder,mjcf,blobs)
        target = HERE/"licenses"
        target.mkdir(exist_ok=True)
        shutil.copyfile(folder/"LICENSE",target/(name+".txt"))
        blobs[name+".LICENSE.txt"]=(folder/"LICENSE").read_bytes()
    with ZipFile(HERE/"models.zip","w") as z:
        for name,data in sorted(blobs.items()):
            z.writestr(ZipInfo(name),data,compress_type=ZIP_DEFLATED,compresslevel=9)
    lock={"upstream":"https://github.com/google-deepmind/mujoco_menagerie","commit":COMMIT,
          "original_sha256":sources,"files":{n:hashlib.sha256(data).hexdigest() for n,data in blobs.items()}}
    (HERE/"assets.json").write_bytes((json.dumps(lock,indent=2,sort_keys=True)+"\n").encode())


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path,help="Pinned MuJoCo Menagerie checkout or matching model directories")
    rebuild(parser.parse_args().source)
