"""Build a self-contained Harmonic world from pinned Fuel models and local training grounds."""
import argparse
import json
import math
import random
import shutil
from pathlib import Path
from xml.etree import ElementTree as ET


def tag(parent, name, value):
    child = ET.SubElement(parent, name)
    child.text = value
    return child


def values(numbers):
    return " ".join(f"{n:.6g}" for n in numbers)


def static(world, name):
    model = ET.SubElement(world, "model", name=name)
    tag(model, "static", "true")
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


BRICK=(.65,.41,.28)
CONCRETE=(.74,.71,.65)
GLASS=(.21,.45,.53)
WHITE=(.91,.91,.83)
GREEN=(.18,.42,.20)
ASPHALT=(.19,.21,.22)


def teaching(world):
    room=static(world,"teaching")
    features=[]
    def wall(name, x, y, length, width, height=4.0, base=0, shade=BRICK):
        shape(room,name,"box",(x,y,base+height/2),(length,width,height),shade,True)
        features.append(dict(id=name,kind="wall",label="教学楼结构",xy=[x,y],size=[length,width,height],base_z=base))
    shape(room,"tile_floor","box",(-7.7,0,.005),(18.6,18,.01),(.74,.70,.58))
    wall("back",-17,0,.25,18)
    wall("north",-7.7,9,18.8,.25)
    wall("south",-7.7,-9,18.8,.25)
    for side in (-1,1):
        wall("door_side_"+str(side),1.6,side*5.05,.25,7.9)
    wall("lintel",1.6,0,.25,2.2,1.3,2.7,CONCRETE)
    wall("ceiling",-7.7,0,19.1,18.6,.16,4,CONCRETE)
    # Upstairs is a facade. The ground floor has two classrooms with genuine doorways.
    wall("upper_facade",-7.7,0,18.6,18,5.5,4.16)
    for col in range(7):
        x=-15+col*2.45
        for y in (-9.14,9.14):
            for z in (2.1,6.5,8.4):
                shape(room,f"window_{col}_{y}_{z}","box",(x,y,z),(1.65,.14,1.02),GLASS)
    for y in (-3.7,3.7):
        wall(f"partition_back_{y}",-12,y,9.8,.14,3.8,shade=CONCRETE)
        wall(f"partition_front_{y}",-1.7,y,6.4,.14,3.8,shade=CONCRETE)
    for row in range(3):
        for col in range(4):
            x=-14+col*3
            for side in (-1,1):
                y=side*(5.1+row*1.25)
                seat=f"{row}_{col}_{side}"
                shape(room,"desktop_"+seat,"box",(x,y,.75),(1.25,.60,.09),(.64,.45,.25),True)
                for leg in (-1,1):
                    shape(room,f"desk_support_{seat}_{leg}","box",(x+leg*.48,y,.36),(.08,.46,.72),CONCRETE,True)
                shape(room,"chair_"+seat,"box",(x,y+.5,.24),(.46,.41,.48),(.22,.33,.30),True)
    for side in (-1,1):
        shape(room,f"whiteboard_{side}","box",(1.40,side*6.4,1.55),(.09,3.6,1.1),(.87,.90,.81),True)
    lamp=ET.SubElement(world,"light",name="teaching_lights",type="point")
    tag(lamp,"pose","-7 -3 3.6 0 0 0")
    tag(lamp,"diffuse","1 .95 .86 1")
    atten=ET.SubElement(lamp,"attenuation")
    for key,value in (("range","24"),("constant","0.65"),("linear","0.015"),("quadratic","0.002")):
        tag(atten,key,value)
    shape(room,"noticeboard","box",(.9,2.9,1.45),(.10,2.45,1.12),(.13,.36,.27),True)
    for y in (-2.1,2.1):
        shape(room,"porch_post"+str(y),"cylinder",(3.9,y,1.55),(.15,3.1),CONCRETE,True)
    shape(room,"porch","box",(2.7,0,3.16),(3.0,5.1,.22),CONCRETE,True)
    return features


def stadium(world):
    field=static(world,"track")
    cx,cy=-103,72
    red=(.62,.245,.124)
    pale=(.95,.86,.68)
    for idx,(radius,color,z) in enumerate(((40,red,.014),(33,GREEN,.028))):
        shape(field,"center"+str(idx),"box",(cx,cy,z),(84.4,2*radius,.012),color)
        for side in (-1,1):
            shape(field,f"bend_{idx}_{side}","cylinder",(cx+side*42.2,cy,z),(radius,.012),color)
    for lane in range(7):
        radius=33+lane*1.10
        for side in (-1,1):
            line(field,f"straight_{lane}_{side}",(cx-42.2,cy+side*radius),(cx+42.2,cy+side*radius),.14,.045,pale)
            for j in range(24):
                a=math.pi*(-.5+j/24) + (0 if side==1 else math.pi)
                b=a+math.pi/24
                ox=cx+side*42.2
                line(field,f"curve_{lane}_{side}_{j}",(ox+radius*math.cos(a),cy+radius*math.sin(a)),
                     (ox+radius*math.cos(b),cy+radius*math.sin(b)),.18,.045,pale)
    for y in (-26,26):
        line(field,"touchline"+str(y),(cx-50,cy+y),(cx+50,cy+y),.18,.048,WHITE)
    for x in (-50,0,50):
        line(field,"endline"+str(x),(cx+x,cy-26),(cx+x,cy+26),.18,.048,WHITE)
    for i in range(32):
        a=i*math.tau/32
        b=(i+1)*math.tau/32
        line(field,"center_circle"+str(i),(cx+8*math.cos(a),cy+8*math.sin(a)),(cx+8*math.cos(b),cy+8*math.sin(b)),.16,.049,WHITE)
    for side in (-1,1):
        gx=cx+side*50
        for y in (-3.66,3.66):
            shape(field,f"goal_{side}_{y}","cylinder",(gx,cy+y,1.22),(.07,2.44),WHITE,True)
        shape(field,"crossbar"+str(side),"box",(gx,cy,2.44),(.13,7.45,.13),WHITE,True)
        for row in range(4):
            height=(row+1)*.42
            shape(field,f"stands_{side}_{row}","box",(cx,cy+side*(43+row*.82),height/2),(43,.82,height),CONCRETE,True)


def road_lawn(world):
    road=static(world,"road")
    line(road,"campus_drive",(14,-106),(14,154),8,.012,ASPHALT)
    line(road,"athletics_branch",(-103,14),(45,14),6,.012,ASPHALT)
    for y in range(-102,154,9):
        line(road,"divider"+str(y),(14,y),(14,y+4),.15,.027,(.89,.72,.28))
    for side in (-1,1):
        line(road,"walk"+str(side),(14+side*5.1,-106),(14+side*5.1,154),1.9,.017,CONCRETE)
    # These coordinates join the imported Road face near (-157.24, 7.75) in the locked OBJ.
    connector=[(14,151),(15,174),(22,190),(36.55,202.63)]
    for i,(a,b) in enumerate(zip(connector,connector[1:])):
        line(road,"north_link_"+str(i),a,b,8,.016,ASPHALT)
        shape(road,"north_joint_"+str(i),"cylinder",(*a,.016),(4,.014),ASPHALT)
        for part in range(3):
            t1=part/3
            t2=t1+.15
            dash=[tuple(a[j]+(b[j]-a[j])*t for j in (0,1)) for t in (t1,t2)]
            line(road,f"north_divider_{i}_{part}",*dash,.15,.035,(.89,.72,.28))
    line(road,"track_entrance",(-104,14),(-104,34),3.2,.02,CONCRETE)
    line(road,"trail_entrance",(18.5,-66),(24,-66),2.8,.025,(.58,.48,.31))
    line(road,"lawn_entrance",(4,-10),(9.3,-8),2.6,.03,(.71,.61,.44))
    for i in range(10):
        x=10.4+i*.8
        line(road,"crossing"+str(i),(x,-1.6),(x,1.6),.34,.038,WHITE)
    lawn=static(world,"lawn")
    shape(lawn,"grass","box",(-79,-44,.009),(107,66,.016),(.32,.55,.255))
    for i in range(4):
        y=-67+i*14
        shape(lawn,"bench"+str(i),"box",(-23.8,y,.29),(1.05,2.3,.58),(.48,.32,.19),True)
    line(lawn,"walkway",(-136,-84),(5,-10),2.6,.03,(.71,.61,.44))


def forest(world):
    woods=static(world,"woods")
    shape(woods,"forest_floor","box",(67,-54,.012),(89,85,.016),(.27,.355,.175))
    rng=random.Random(247)
    total=0
    for j in range(9):
        for i in range(10):
            x=29+i*8.4+rng.uniform(-2.6,2.6)
            y=-92+j*9.6+rng.uniform(-2.5,2.5)
            if abs(y+55-.27*(x-67))<3.3:
                continue
            total+=1
            if total%5==0:
                include(world,"pine_"+str(total),"model://Pine Tree",(x,y,0),rng.uniform(0,math.tau))
                continue
            h=rng.uniform(3.5,6.8)
            rad=rng.uniform(1.6,3.3)
            shape(woods,"trunk"+str(total),"cylinder",(x,y,h*.46),(.15+h*.024,h*.92),(.36,.25,.16),True)
            shade=(rng.uniform(.12,.26),rng.uniform(.32,.50),rng.uniform(.10,.22))
            for k in range(3):
                a=rng.uniform(0,math.tau)
                shape(woods,f"crown_{total}_{k}","ellipsoid",(x+math.cos(a)*.8,y+math.sin(a)*.8,h+k*.53),
                      (rad,rad*.89,rad*1.12),shade)
    # Low returns in the understorey exercise sensors at wheel height; keep the diagonal trail open.
    for n in range(26):
        x,y=rng.uniform(26,108),rng.uniform(-92,-16)
        if abs(y+55-.27*(x-67))<4:
            continue
        radius=rng.uniform(.55,1.25)
        shade=(.35,.36,.31) if n%3==0 else (.25,.38,.17)
        shape(woods,"undergrowth_"+str(n),"ellipsoid",(x,y,.27),(radius,.8*radius,.48),shade)
        shape(woods,"undergrowth_base_"+str(n),"cylinder",(x,y,.21),(.55*radius,.42),shade,True)
    line(woods,"trail",(24,-66),(110,-43),2.8,.025,(.58,.48,.31))
    return total


def mesh_extent(model_dir):
    sdf=ET.parse(model_dir/"model.sdf")
    mesh=sdf.find(".//visual/geometry/mesh")
    uri=mesh.findtext("uri")
    path=model_dir/uri.split("Campus/")[-1]
    scale=[float(x) for x in (mesh.findtext("scale") or "1 1 1").split()]
    lo=[math.inf]*3
    hi=[-math.inf]*3
    vertices=0
    with path.open(encoding="utf-8",errors="replace") as obj:
        for line_text in obj:
            if line_text.startswith("v "):
                xyz=tuple(float(v)*s for v,s in zip(line_text.split()[1:4],scale))
                lo=[min(a,b) for a,b in zip(lo,xyz)]
                hi=[max(a,b) for a,b in zip(hi,xyz)]
                vertices+=1
    if vertices<3 or min(hi[0]-lo[0],hi[1]-lo[1])<1:
        raise ValueError("Campus mesh does not contain the expected metric geometry")
    return lo,hi,vertices


def area(id_,label,bounds,origin,description):
    x0,y0,x1,y1=bounds
    return dict(id=id_,label=label,bounds=bounds,center=[(x0+x1)/2,(y0+y1)/2],origin=origin,description=description)


def viewpoints(world, total, areas):
    model=ET.SubElement(world,"model",name="campus_viewpoints")
    tag(model,"static","true")
    def camera(name,xyz,look,width=1000,height=650):
        link=ET.SubElement(model,"link",name=name+"_station")
        dx,dy,dz=(v-w for v,w in zip(look,xyz))
        pitch=-math.atan2(dz,math.hypot(dx,dy))
        tag(link,"pose",values((*xyz,0,pitch,math.atan2(dy,dx))))
        sensor=ET.SubElement(link,"sensor",type="camera",name=name+"_camera")
        tag(sensor,"topic",f"/campus/view/{name}/image")
        tag(sensor,"update_rate","1")
        cam=ET.SubElement(sensor,"camera")
        tag(cam,"horizontal_fov","1.18")
        img=ET.SubElement(cam,"image")
        tag(img,"width",str(width));tag(img,"height",str(height));tag(img,"format","R8G8B8")
        distance=math.sqrt(dx*dx+dy*dy+dz*dz)
        near=max(.10,xyz[2]*.3)
        clip=ET.SubElement(cam,"clip")
        tag(clip,"near",str(near));tag(clip,"far",str(max(600,distance*2.5)))
    x0,y0,x1,y1=total
    center=[(x0+x1)/2,(y0+y1)/2]
    altitude=max(x1-x0,(y1-y0)*1000/650)*.85
    # Facing north with optical down. The north-up overview is also the coordinate reference for the map.
    camera("overview",(*center,altitude),(center[0],center[1]+.001,0))
    orig=areas[0]
    cx,cy=orig["center"]
    length=max(orig["bounds"][2]-orig["bounds"][0],orig["bounds"][3]-orig["bounds"][1])
    camera("campus",(cx+length*.28,cy-length*.64,max(20,length*.34)),(cx,cy,4))
    camera("teaching",(-15.8,-7.9,1.65),(-5.6,-4.1,1.25))
    camera("track",(-33,24,35),(-112,80,0))
    camera("woods",(22,-68,6.7),(78,-51,3))
    camera("road",(9.8,-21,3.1),(14,25,1.5))
    camera("lawn",(-134,-81,9),(-65,-44,0))
    return {"center":center,"view_width":2*altitude*math.tan(1.18/2),"aspect":1000/650}


def build(assets, base, output):
    if not all((assets/"models"/n/"model.sdf").is_file() for n in ("Campus","Pine Tree")):
        raise SystemExit("Missing pinned campus assets. Run python scene/campus_large/fetch_assets.py from the repository root.")
    tree=ET.parse(base)
    world=tree.getroot().find("world")
    for m in list(world.findall("model")):
        if m.get("name") not in ("edge_rover","floor"):
            world.remove(m)
    lo,hi,vertices=mesh_extent(assets/"models"/"Campus")
    tx,ty=22-lo[0],23-lo[1]
    orig=[22,23,hi[0]+tx,hi[1]+ty]
    total=[-198,-110,max(118,orig[2]+4),max(155,orig[3]+4)]
    size=max(abs(n) for n in total)*3+100
    for plane in world.findall("./model[@name='floor']//plane/size"):
        plane.text=values((size,size))
    for color in world.findall("./model[@name='floor']//material/diffuse"):
        color.text=".51 .55 .41 1"
    # Upstream sets its mesh to z=-0.25. The dominant Road faces in the OBJ are at z=0.
    # Lift the source so road paint and grass remain above the new continuous physics floor.
    include(world,"fuel_campus","model://Campus",(tx,ty,.265))
    features=teaching(world)
    stadium(world)
    road_lawn(world)
    trees=forest(world)
    areas=[area("campus","原始校园建筑群",orig,"Gazebo Fuel · NCS_robotmanager / Campus v2","上游发布的带材质校园网格，视觉与碰撞共用其原始几何。"),
           area("teaching","教师楼与教学楼门厅",[-17.2,-9.2,4.2,9.2],"本项目训练区","一楼有教室、桌椅、走廊和可通过的门廊。机器人从楼内出发。"),
           area("track","田径场与足球场",[-186,25,-20,119],"本项目训练区","弯道、六条跑道、足球场线、球门和看台。"),
           area("woods","混合树林与林道",[22,-97,112,-11],"本项目训练区 · OpenRobotics / Pine Tree v6",f"{trees} 棵疏密不同的松树与阔叶树，土路斜穿林地。"),
           area("road","校园车道",[9,-106,40,203],"本项目训练区","双向车道和人行横道，北段弯道接入原校园道路。"),
           area("lawn","开阔草坪",[-133,-77,-25,-11],"本项目训练区","开阔草地、边缘长椅和一条通向建筑的步道。")]
    projection=viewpoints(world,total,areas)
    out_web=output/"overview"
    out_web.mkdir(parents=True,exist_ok=True)
    tree.write(output/"world.sdf",encoding="utf-8",xml_declaration=True)
    layout={"schema_version":1,"id":"campus_large","revision":"1.0","frame":"odom",
            "source":"Gazebo Fuel NCS Campus v2 与具名训练区","alignment":"教学楼内机器人出生点为 odom 原点",
            "clearance_m":.32,"features":features,"zones":[
               {"id":"lobby","label":"教学楼一层","kind":"indoor","bounds":[-17,-9,1.59,9]},
               {"id":"door","label":"教学楼门廊","kind":"transition","bounds":[1.59,-1.1,2.5,1.1]},
               {"id":"entry","label":"教学楼门外","kind":"outdoor","bounds":[2.5,-4,4,4]}],
            "nodes":[{"id":"teaching_lobby","label":"教学楼门厅","zone":"lobby","xy":[0,0],"aliases":["教师楼门厅"]},
                     {"id":"teaching_door","label":"教学楼门口","zone":"door","xy":[1.7,0]},
                     {"id":"teaching_exit","label":"教学楼门外","zone":"entry","xy":[3.3,0],"aliases":["教学楼外","教师楼门外"]}],
            "edges":[["teaching_lobby","teaching_door"],["teaching_door","teaching_exit"]]}
    manifest={"bounds":total,"unit":"m","frame":"odom","areas":areas,"source_vertices":vertices,"trees":trees,
              "projection":projection,"spawn":[0,0],"source_file":"models/Campus/model.sdf"}
    for path,obj in ((output/"layout.json",layout),(out_web/"manifest.json",manifest)):
        path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("overview.html"),out_web/"index.html")
    print(f"Campus ready: {vertices} source vertices; {trees} trees; extent {total}",flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ("assets","base","output"):
        parser.add_argument("--"+arg,type=Path,required=True)
    args=parser.parse_args()
    build(args.assets,args.base,args.output)
