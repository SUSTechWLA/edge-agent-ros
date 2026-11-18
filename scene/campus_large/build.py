"""Build a self-contained Harmonic world from pinned Fuel models and local training grounds."""
import argparse
import hashlib
import json
import math
import random
import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

if __package__:
    from .athletics import PITCH_LENGTH, playing_surface
else:
    from athletics import PITCH_LENGTH, playing_surface


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
    brick = (.63,.36,.27)
    frame = (.27,.33,.34)
    limestone = (.83,.79,.70)
    roof = (.35,.39,.40)
    def band(name, axis, fixed, mid, length, depth, base, height, color, solid=False):
        xy, dims = ((mid,fixed),(length,depth)) if axis == "x" else ((fixed,mid),(depth,length))
        shape(room,name,"box",(*xy,base+height/2),(*dims,height),color,solid)
        if solid and base < .5:
            features.append(dict(id=name,kind="wall",label="教学楼外墙",xy=list(xy),
                                 size=[*dims,height],base_z=base))

    def glazing(name, axis, fixed, outside, start, end, bays, base):
        # Deep masonry reveals surround a recessed ribbon window. Each floor has
        # its own wall and glazing; the volumes between opposing windows are hollow.
        width, mid = end-start, (end+start)/2
        bay = width/bays
        band(name+"_sill",axis,fixed,mid,width,.38,base,1.0,brick,True)
        band(name+"_header",axis,fixed,mid,width,.38,base+3.13,.55,limestone,True)
        band(name+"_sunshade",axis,fixed+outside*.21,mid,width,.59,base+3.11,.1,limestone)
        band(name+"_glass",axis,fixed-outside*.13,mid,width,.12,base+1.,2.13,GLASS,True)
        band(name+"_transom",axis,fixed,mid,width,.15,base+2.6,.065,frame)
        for i in range(bays+1):
            # End piers sit inside the wall span so the entrance stays 2.20 m wide.
            pos = min(end-.15,max(start+.15,start+i*bay))
            band(name+f"_pier_{i}",axis,fixed+outside*.08,pos,.30,.53,base+1.,2.13,CONCRETE,True)
        for i in range(bays):
            band(name+f"_mullion_{i}",axis,fixed,start+(i+.5)*bay,.073,.16,base+1.,2.13,frame)

    shape(room,"tile_floor","box",(-7.7,0,.005),(18.6,18,.01),(.74,.70,.58))
    # A four-storey classroom block and a shorter set-back studio wing. The
    # east portal remains at (1.6,0), level with the mission's odometry origin.
    for wing,x0,x1,y0,y1,floors in (("hall",-17,1.6,-9,9,4),("studio",-40,-17,-6,8,3)):
        for floor in range(floors):
            base = floor*3.68
            prefix = f"{wing}_{floor}"
            for y, sign in ((y0,-1),(y1,1)):
                glazing(prefix+f"_side_{sign}","x",y,sign,x0,x1,5,base)
            for x, sign in ((x0,-1),(x1,1)):
                if wing=="studio" and sign==1:
                    continue  # This interior face meets the classroom block.
                if wing=="hall" and sign==-1 and floor<3:
                    continue
                if wing=="hall" and sign==1 and floor==0:
                    glazing("portal_south","y",x,sign,y0,-1.1,2,base)
                    glazing("portal_north","y",x,sign,1.1,y1,2,base)
                else:
                    glazing(prefix+f"_end_{sign}","y",x,sign,y0,y1,4,base)
            top = base+3.68
            slab = "roof" if floor==floors-1 else "floor"
            shape(room,prefix+"_"+slab,"box",((x0+x1)/2,(y0+y1)/2,top),
                  (x1-x0+.48,y1-y0+.48,.20),roof if slab=="roof" else limestone,True)
        for y in (y0,y1):
            band(wing+"_parapet_"+str(y),"x",y,(x0+x1)/2,x1-x0+.36,.27,floors*3.68,.48,limestone,True)
        for x in (x0,x1):
            band(wing+"_end_parapet_"+str(x),"y",x,(y0+y1)/2,y1-y0,.27,floors*3.68,.48,limestone,True)
    # Close the two exposed strips where the recessed studio meets the larger hall.
    wall("junction_south",-17,-7.5,.35,3,11.04,shade=brick)
    wall("junction_north",-17,8.5,.35,1,11.04,shade=brick)
    wall("stair_enclosure",-30,4.7,5.2,4.5,2.4,11.14,limestone)
    shape(room,"stair_cap","box",(-30,4.7,13.62),(5.5,4.8,.2),roof,True)
    for i in range(5):
        shape(room,f"stair_louver_{i}","box",(-30,2.42,11.6+i*.27),(3.8,.12,.12),frame)
    for x in (-15., -11.7):
        shape(room,"roof_vent_"+str(x),"box",(x,5.,15.22),(2.5,1.6,.8),(.64,.67,.63),True)
        for i in range(5):
            shape(room,f"vent_grille_{x}_{i}","box",(x-.8+i*.40,4.17,15.26),(.19,.08,.49),frame)
    wall("lintel",1.6,0,.25,2.2,1.3,2.7,CONCRETE)
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
    shape(room,"porch","box",(3.0,0,3.16),(3.6,5.5,.22),limestone,True)
    shape(room,"porch_fascia","box",(4.77,0,3.24),(.14,5.55,.38),frame)
    for y in (-1.13,1.13):
        shape(room,"portal_jamb_"+str(y),"box",(1.79,y,1.36),(.15,.06,2.72),frame,True)
    return features


def stadium(world, output):
    field = static(world, "track")
    cx, cy = -103, 72
    # A single mesh draws continuous lane boundaries around both bends. The
    # continuous physics floor also supports the turf and running surface.
    path = playing_surface().write(output/"meshes")
    visual = ET.SubElement(field, "visual", name="running_track_and_pitch")
    tag(visual, "pose", values((cx,cy,0,0,0,0)))
    mesh = ET.SubElement(ET.SubElement(visual, "geometry"), "mesh")
    tag(mesh, "uri", path.relative_to(output).as_posix())
    shape(field,"pitch_foundation","box",(cx,cy,.006),(105,68,.012),(.24,.48,.26))
    net, alloy = (.74,.78,.72), (.42,.47,.47)
    # The opening is measured between the inside edges and below the crossbar.
    for side in (-1,1):
        gx, rear = cx + side*PITCH_LENGTH/2, cx + side*(PITCH_LENGTH/2+2)
        for sign in (-1,1):
            y = cy + sign*3.71
            shape(field,f"goal_post_{side}_{sign}","box",(gx,y,1.22),(.10,.10,2.44),WHITE,True)
            shape(field,f"goal_rear_{side}_{sign}","box",(rear,y,1.22),(.07,.07,2.44),alloy,True)
            for z in (.09,2.47):
                shape(field,f"goal_support_{side}_{sign}_{z}","box",((gx+rear)/2,y,z),(2,.07,.07),WHITE,True)
            for i in range(1,5):
                shape(field,f"goal_side_net_{side}_{sign}_{i}","box",((gx+rear)/2,y,i*.48),(2,.028,.028),net)
        shape(field,f"crossbar_{side}","box",(gx,cy,2.49),(.10,7.52,.10),WHITE,True)
        for i in range(17):
            shape(field,f"net_warp_{side}_{i}","box",(rear,cy-3.68+i*.46,1.22),(.028,.028,2.44),net)
        for i in range(6):
            shape(field,f"net_weft_{side}_{i}","box",(rear,cy,.08+i*.47),(.028,7.42,.028),net)
    # Split the grandstand at the entrance axis. The aisle connects the training
    # road to the south track edge without a fence or step in the middle.
    for side,rows in ((-1,4),(1,7)):
        for block in (-1,1):
            mid = cx + block*20.5
            for row in range(rows):
                height = .45 + row*.43
                y = cy + side*(48.3 + row*.85)
                shape(field,f"terrace_{side}_{block}_{row}","box",(mid,y,height/2),
                      (36,.86,height),CONCRETE,True)
                for bank in (-1,1):
                    shade = (.22,.40,.49) if (row//2)%2 == 0 else (.32,.52,.62)
                    shape(field,f"seat_{side}_{block}_{row}_{bank}","box",
                          (mid+bank*9.1,y,height+.10),(16.7,.48,.20),shade,True)
            if side == 1:
                shape(field,f"stand_roof_{block}","box",(mid,cy+51.2,6.3),(37.1,7.5,.28),(.61,.67,.68),True)
                for dx in (-16,0,16):
                    shape(field,f"stand_roof_post_{block}_{dx}","cylinder",
                          (mid+dx,cy+54,3.08),(.16,6.16),alloy,True)
                    shape(field,f"stand_spar_{block}_{dx}","box",
                          (mid+dx,cy+51.2,6.04),(.16,7.45,.25),alloy)
    for sx in (-1,1):
        for sy in (-1,1):
            x,y = cx+sx*45,cy+sy*47
            shape(field,f"floodlight_mast_{sx}_{sy}","cylinder",(x,y,8),(.20,16),alloy,True)
            shape(field,f"floodlight_rack_{sx}_{sy}","box",(x,y,15.8),(3.6,.44,1.04),(.27,.32,.33),True)
            for i in range(4):
                shape(field,f"floodlight_{sx}_{sy}_{i}","box",(x-1.35+i*.9,y-sy*.26,15.8),
                      (.65,.14,.70),(.91,.91,.81))


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
    line(road,"track_entrance",(-103,14),(-103,25.6),3.2,.02,CONCRETE)
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


def area(id_,label,bounds,origin,description,eye=None,target=None):
    x0,y0,x1,y1=bounds
    result = dict(id=id_,label=label,bounds=bounds,center=[(x0+x1)/2,(y0+y1)/2],origin=origin,description=description)
    if eye is not None:
        result["camera"] = {"eye":eye,"target":target}
    return result


def local_mesh(world_dir, uri):
    # file:///campus/ is the world volume in Compose, also supported by offline builds.
    path = (world_dir / uri.removeprefix("file:///campus/")).resolve()
    if not path.is_relative_to(world_dir.resolve()) or path.suffix.lower() != ".obj":
        raise ValueError(f"Expected local campus OBJ, got {uri}")
    return path


def generated_mesh_files(world, output):
    """Resolve generated OBJ → mtllib → diffuse map references within this world."""
    root = output.resolve()
    models = {local_mesh(root,node.text) for node in world.findall(".//geometry/mesh/uri")}
    def referenced(files, keyword):
        found = set()
        for path in files:
            with path.open(encoding="utf-8") as source:
                for row in source:
                    key, _, value = row.strip().partition(" ")
                    if key != keyword:
                        continue
                    asset = (path.parent/value.strip()).resolve()
                    if not asset.is_relative_to(root):
                        raise ValueError(f"Campus material outside world directory: {asset.name}")
                    found.add(asset)
        return found
    materials = referenced(models,"mtllib")
    images = referenced(materials,"map_Kd")
    return sorted(models | materials | images)


def scene_revision(world, output, layout):
    """Bind sensor/map alignment to the navigation graph, SDF and its local geometry."""
    digest = hashlib.sha256(json.dumps(layout,sort_keys=True,ensure_ascii=False).encode())
    digest.update((output/"world.sdf").read_bytes())
    digest.update(Path(__file__).with_name("asset-lock.json").read_bytes())
    for path in generated_mesh_files(world, output):
        digest.update(path.relative_to(output.resolve()).as_posix().encode())
        digest.update(path.read_bytes())
    return "2."+digest.hexdigest()[:16]


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
    for place in areas:
        if "camera" not in place:
            cx,cy=place["center"]
            x0,y0,x1,y1=place["bounds"]
            length=max(x1-x0,y1-y0)
            place["camera"]={"eye":[cx+length*.28,cy-length*.64,max(20,length*.34)],"target":[cx,cy,4]}
        view = place["camera"]
        camera(place["id"],view["eye"],view["target"])
    return {"center":center,"view_width":2*altitude*math.tan(1.18/2),"aspect":1000/650}


def build(assets, base, output):
    if __package__:
        from .real_site import append_site
    else:
        from real_site import append_site
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
    output.mkdir(parents=True,exist_ok=True)
    surveyed=append_site(world,output)
    dx0,dy0,dx1,dy1=surveyed["bounds"]
    total=[min(-198,dx0-4),min(-110,dy0-4),max(orig[2],dx1)+4,max(155,orig[3]+4,dy1+4)]
    size=max(abs(n) for n in total)*3+100
    for plane in world.findall("./model[@name='floor']//plane/size"):
        plane.text=values((size,size))
    for color in world.findall("./model[@name='floor']//material/diffuse"):
        color.text=".51 .55 .41 1"
    # Upstream sets its mesh to z=-0.25. The dominant Road faces in the OBJ are at z=0.
    # Lift the source so road paint and grass remain above the new continuous physics floor.
    include(world,"fuel_campus","model://Campus",(tx,ty,.265))
    features=teaching(world)
    stadium(world,output)
    road_lawn(world)
    trees=forest(world)
    areas=[surveyed,
           area("teaching","教学楼与主入口",[-41,-10,5,10],"本项目教学训练区","四层教学楼接三层侧翼。东侧雨棚下是可通行门廊，首层两间教室临走廊布置。",
                [31,-39,12],[-12,0,6]),
           area("track","田径场与足球场",[-194,17,-12,127],"本项目体育训练区","八道 400 米跑道包围 105 × 68 米足球场。可观察罚球区、角球弧、球门和分区看台。",
                [-73,-66,104],[-103,72,0]),
           area("fuel_campus","NCS 单层校园",orig,"Gazebo Fuel · NCS_robotmanager / Campus v2","保留原模型的道路、低墙、室内家具与绿化。源网格最高点为 3.49 米。"),
           area("teaching_interior","教学楼一层教室",[-17,-9,1.6,9],"本项目教学训练区","近距离观察桌椅、教室分隔与窗带；机器人出生点位于中间走廊。",
                [-15.8,-7.9,1.65],[-5.6,-4.1,1.25]),
           area("woods","混合树林与林道",[22,-97,112,-11],"本项目训练区 · OpenRobotics / Pine Tree v6",f"{trees} 棵疏密不同的松树与阔叶树，土路斜穿林地。",
                [22,-68,6.7],[78,-51,3]),
           area("road","校园车道",[9,-106,40,203],"本项目训练区","双向车道和人行横道，北段弯道接入 NCS 校园道路。",
                [9.8,-21,3.1],[14,25,1.5]),
           area("lawn","开阔草坪",[-133,-77,-25,-11],"本项目训练区","开阔草地、边缘长椅和一条通向建筑的步道。",
                [-134,-81,9],[-65,-44,0])]
    street=surveyed.pop("street_camera")
    x,y,_=street["eye"]
    areas.insert(1,area("campus_street","Mekelweg 街面 · EWI 塔楼",[x-20,y-20,x+20,y+20],
                        surveyed["origin"],"从 Mekelweg 的街面朝向 EWI 塔楼。相机高 3.2 米。"
                        "地面为配准航片，窗格为示意材质。",street["eye"],street["target"]))
    projection=viewpoints(world,total,areas)
    out_web=output/"overview"
    out_web.mkdir(parents=True,exist_ok=True)
    tree.write(output/"world.sdf",encoding="utf-8",xml_declaration=True)
    layout={"schema_version":1,"id":"campus_large","frame":"odom",
            "source":"代尔夫特实测建筑、Gazebo Fuel NCS Campus v2 与本项目训练区","alignment":"教学楼内机器人出生点为 odom 原点",
            "clearance_m":.32,"features":features,"zones":[
               {"id":"lobby","label":"教学楼一层","kind":"indoor","bounds":[-17,-9,1.59,9]},
               {"id":"door","label":"教学楼门廊","kind":"transition","bounds":[1.59,-1.1,2.5,1.1]},
               {"id":"entry","label":"教学楼门外","kind":"outdoor","bounds":[2.5,-4,4,4]}],
            "nodes":[{"id":"teaching_lobby","label":"教学楼门厅","zone":"lobby","xy":[0,0],"aliases":["教师楼门厅"]},
                     {"id":"teaching_door","label":"教学楼门口","zone":"door","xy":[1.7,0]},
                     {"id":"teaching_exit","label":"教学楼门外","zone":"entry","xy":[3.3,0],"aliases":["教学楼外","教师楼门外"]}],
            "edges":[["teaching_lobby","teaching_door"],["teaching_door","teaching_exit"]]}
    layout["revision"]=scene_revision(world,output,layout)
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
