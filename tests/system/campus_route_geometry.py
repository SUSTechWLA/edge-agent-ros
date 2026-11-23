"""Independent geometry audit of the published campus route using Gazebo SDF and OBJ files."""
from collections import defaultdict
from functools import lru_cache
import math
import json
from pathlib import Path
from xml.etree import ElementTree as ET


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def pose(element):
    x,y,z,r,p,a=map(float,element.findtext("pose","0 0 0 0 0 0").split())
    check(abs(r)+abs(p)<1e-5,f"Tilted static site geometry {element.get('name')}")
    return x,y,z,a


def transform(t,p):
    x,y,z,a=t
    return (x+math.cos(a)*p[0]-math.sin(a)*p[1],y+math.sin(a)*p[0]+math.cos(a)*p[1],z+p[2])


def place(*elements):
    t=(0,0,0,0)
    for element in elements:
        p=pose(element)
        t=(*transform(t,p),t[3]+p[3])
    return t


def dist_point(p,a,b):
    dx,dy=b[0]-a[0],b[1]-a[1]
    t=max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/(dx*dx+dy*dy))) if dx or dy else 0
    return math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy)


def orient(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def poly_distance(a,b,points):
    edges=list(zip(points,points[1:]+points[:1]))
    inside=[orient(c,d,a) for c,d in edges]
    if len(points)>2 and min(inside)<-1e-7 and max(inside)<1e-7:
        return 0.
    if len(points)>2 and max(inside)>1e-7 and min(inside)>-1e-7:
        return 0.
    result=math.inf
    for c,d in edges:
        if orient(a,b,c)*orient(a,b,d)<0 and orient(c,d,a)*orient(c,d,b)<0:
            return 0.
        result=min(result,dist_point(a,c,d),dist_point(b,c,d),dist_point(c,a,b),dist_point(d,a,b))
    return result


def clip_z(polygon,height,above):
    result=[]
    if not polygon:
        return result
    prev=polygon[-1]
    for curr in polygon:
        a,b=(prev[2]>=height)==above,(curr[2]>=height)==above
        if a!=b:
            t=(height-prev[2])/(curr[2]-prev[2])
            result.append(tuple(prev[i]+t*(curr[i]-prev[i]) for i in range(3)))
        if b:
            result.append(curr)
        prev=curr
    return result


def resolve(uri,world_dir):
    if uri.startswith("model://"):
        return Path("/assets/models")/uri[8:]
    p=Path(uri.removeprefix("file://"))
    if p.is_absolute() and p.parts[1:2]==("campus",):
        return world_dir.joinpath(*p.parts[2:])
    return world_dir/p


@lru_cache(maxsize=8)
def obj(path):
    positions=[]; uv=[]; faces=[]; group=path.stem
    for line in path.read_text().splitlines():
        fields=line.split()
        if not fields:
            continue
        if fields[0]=="v":
            positions.append(tuple(map(float,fields[1:4])))
        elif fields[0]=="vt":
            uv.append(tuple(map(float,fields[1:3])))
        elif fields[0] in ("g","o"):
            group=fields[1]
        elif fields[0]=="f":
            refs=[s.split("/") for s in fields[1:]]
            for a,b in zip(refs[1:],refs[2:]):
                faces.append((group,[refs[0],a,b]))
    return positions,uv,faces


@lru_cache(maxsize=8)
def pine_envelope(path,z,slab=(.065,.60)):
    """Bound the actual DAE triangles intersecting the rover's body-height slab."""
    root=ET.parse(path)
    check(root.findtext(".//{*}up_axis")=="Z_UP","Unexpected pine up axis")
    geometries={g.get("id"):g.find("{*}mesh") for g in root.findall(".//{*}library_geometries/{*}geometry")}
    radius=0.
    for node in root.findall(".//{*}visual_scene/{*}node"):
        inst=node.find("{*}instance_geometry")
        if inst is None:continue
        mesh=geometries[inst.get("url")[1:]]
        matrix=list(map(float,node.findtext("{*}matrix").split()))
        for tri in mesh.findall("{*}triangles"):
            inputs=tri.findall("{*}input")
            v=next(i for i in inputs if i.get("semantic")=="VERTEX")
            vertex=mesh.find("{*}vertices[@id='"+v.get("source")[1:]+"']/{*}input[@semantic='POSITION']")
            coords=mesh.find("{*}source[@id='"+vertex.get("source")[1:]+"']/{*}float_array")
            raw=list(map(float,coords.text.split()))
            positions=[tuple(sum(matrix[4*j+k]*raw[i+k] for k in range(3))+matrix[4*j+3]+(z if j==2 else 0)
                             for j in range(3)) for i in range(0,len(raw),3)]
            stride=max(int(i.get("offset")) for i in inputs)+1
            idx=list(map(int,tri.findtext("{*}p").split()))[int(v.get("offset"))::stride]
            for i in range(0,len(idx),3):
                clipped=clip_z(clip_z([positions[k] for k in idx[i:i+3]],slab[0],True),slab[1],False)
                for p in clipped:radius=max(radius,math.hypot(*p[:2]))
    check(radius>0,"No pine geometry at robot height")
    return radius


def obstacles(models,world_dir,slab=(.065,.60)):
    result=[]
    def add(name,polygon,radius=0.):
        xmin=min(v[0] for v in polygon)-radius; xmax=max(v[0] for v in polygon)+radius
        ymin=min(v[1] for v in polygon)-radius; ymax=max(v[1] for v in polygon)+radius
        result.append((name,polygon,radius,(xmin,ymin,xmax,ymax)))
    for name,model in models.items():
        if model.tag=="include":
            if model.findtext("uri")=="model://Pine Tree":
                p=pose(model)
                add(name,[p[:2]],pine_envelope(Path("/assets/models/Pine Tree/meshes/pine_tree.dae"),p[2],slab))
            continue
        if model.findtext("static","false") not in ("1","true"):
            continue
        for link in model.findall("link"):
            for collision in link.findall("collision"):
                t=place(model,link,collision); g=collision.find("geometry")[0]
                label=name+"/"+collision.get("name")
                if g.tag=="plane":
                    continue
                if g.tag=="mesh":
                    path=resolve(g.findtext("uri"),world_dir)
                    if path.name=="pine_tree.dae":
                        add(label,[t[:2]],pine_envelope(path,t[2],slab));continue
                    check(path.suffix.lower()==".obj",f"Unsupported static collision mesh {path}")
                    verts,_,faces=obj(path)
                    scale=tuple(map(float,g.findtext("scale","1 1 1").split()))
                    verts=[transform(t,[v[i]*scale[i] for i in range(3)]) for v in verts]
                    for group,refs in faces:
                        pts=[verts[int(r[0])-1] for r in refs]
                        pts=clip_z(clip_z(pts,slab[0],True),slab[1],False)
                        if pts:
                            add(label+"/"+group,[p[:2] for p in pts])
                    continue
                if g.tag=="box":
                    a,b,h=[v/2 for v in map(float,g.findtext("size").split())]
                elif g.tag=="cylinder":
                    a=b=float(g.findtext("radius")); h=float(g.findtext("length"))/2
                elif g.tag=="ellipsoid":
                    a,b,h=map(float,g.findtext("radii").split())
                elif g.tag=="sphere":
                    a=b=h=float(g.findtext("radius"))
                else:
                    raise ValueError(f"Unhandled physical obstacle {g.tag}")
                if t[2]+h<slab[0] or t[2]-h>slab[1]:continue
                if g.tag=="box":
                    add(label,[transform(t,[a*i,b*j,0])[:2] for i,j in ((-1,-1),(1,-1),(1,1),(-1,1))])
                else:
                    add(label,[t[:2]],max(a,b))
    return result


def terrain_hit(model,world_dir,p):
    """Interpolate the world position and original image UV of the actual terrain triangle."""
    for link in model.findall("link"):
        for vis in link.findall("visual"):
            mesh=vis.find("geometry/mesh")
            if mesh is None or not mesh.findtext("uri","").endswith("/terrain.obj"):continue
            vertices,uv,faces=obj(resolve(mesh.findtext("uri"),world_dir))
            t=place(model,link,vis)
            for _,refs in faces:
                tri=[transform(t,vertices[int(r[0])-1]) for r in refs]
                a,b,c=tri; det=orient(a,b,c)
                if abs(det)<1e-9:continue
                weights=[orient(b,c,p)/det,orient(c,a,p)/det,orient(a,b,p)/det]
                if min(weights)<-1e-7:continue
                coords=[uv[int(r[1])-1] for r in refs]
                return {"uv":[sum(w*v[j] for w,v in zip(weights,coords)) for j in (0,1)],
                        "surface_z_m":sum(w*v[2] for w,v in zip(weights,tri)),
                        "vertices":[int(r[0]) for r in refs],"barycentric":weights}
    raise AssertionError(f"Published street anchor {p} misses the surveyed ground mesh")


def bgt_footpath(world_dir,anchor,hit):
    # Survey control independent of the scene generator. Inspect the original
    # Kadaster polygon including holes, and separately check the imagery UV.
    rd=[85610+anchor[0],445601+anchor[1]]
    collection=read_collection(world_dir/"real_site/mekelweg-footpath.json")
    check(collection["crs"]=="EPSG:28992","Unexpected BGT coordinate reference")
    def inside(ring):
        x,y=rd;odd=False
        for a,b in zip(ring,ring[1:]+ring[:1]):
            if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
                odd=not odd
        return odd
    matches=[]
    for feature in collection["features"]:
        poly=feature["geometry"]
        if feature["properties"]["function"]=="voetpad" and poly["type"]=="Polygon":
            rings=poly["coordinates"]
            if inside(rings[0]) and not any(inside(r) for r in rings[1:]):matches.append(feature["id"])
    check(matches,"Street tie-in misses all original BGT pedestrian polygons")
    uv=[(rd[0]-85200)/545,(rd[1]-445440)/1170]
    check(math.dist(hit["uv"],uv)<.0001,"Street surface UV does not register to the surveyed road")
    return {"crs":collection["crs"],"rd_m":rd,"footway_ids":matches,"reference_uv":uv,"source":collection["source"]}


@lru_cache(maxsize=4)
def read_collection(path):
    return json.loads(path.read_bytes())


def route_check(world,manifest,world_dir):
    models={m.get("name"):m for m in world.findall("model")}
    models.update({m.findtext("name"):m for m in world.findall("include")})
    check("fuel_campus" not in models,"The isolated old NCS display still occupies campus space")
    route=next(r for r in manifest["routes"] if r["id"]=="campus_walk")
    nodes={n["id"]:n["xy"] for n in route["nodes"]}
    check(len(nodes)==len(route["nodes"]) and len(nodes)>=5,"Route has duplicate or missing landmarks")
    check({"teaching_exit","plaza","track_gate","woodland","street_join"}<=nodes.keys(),"Missing physical test sites")
    check(math.dist(nodes["teaching_exit"],[3.3,0])<.05,"Published route fails to join the verified door crossing")
    graph=defaultdict(set); results=[]; shapes=obstacles(models,world_dir); street_checks=[]
    check(shapes,"Missing static obstacles")
    for edge in route["edges"]:
        start,end=edge["from"],edge["to"]; a,b=nodes[start],nodes[end]
        graph[start].add(end);graph[end].add(start)
        model=models[edge["model"]]
        matches=[(l,v) for l in model.findall("link") for v in l.findall("visual") if v.get("name")==edge["visual"]]
        check(len(matches)==1,f"Missing or duplicate route surface {edge}")
        link,vis=matches[0]; t=place(model,link,vis)
        length,width,height=map(float,vis.findtext("geometry/box/size").split())
        ends=[transform(t,[x,0,0])[:2] for x in (-length/2,length/2)]
        gap=min(max(math.dist(a,x),math.dist(b,y)) for x,y in (ends,ends[::-1]))
        check(gap<.035 and abs(width-edge["width_m"])<.02 and width>=1.1,
              f"{start}→{end}: SDF surface misses declared line/width by {gap:.3f} m")
        z=t[2]+height/2
        check(-.01<=z<=.16,f"Road above/below grade at {start}")
        if edge.get("surface")=="survey_footpath":
            count=max(1,math.ceil(math.dist(a,b)))
            ids=set()
            for i in range(count+1):
                p=[x+(y-x)*i/count for x,y in zip(a,b)]
                hit=terrain_hit(models["delft_surveyed_site"],world_dir,p)
                ids.update(bgt_footpath(world_dir,p,hit)["footway_ids"])
                check(0<hit["surface_z_m"]-z<.04,"SDF witness obscures the surveyed street")
            street_checks.append({"from":start,"to":end,"samples":count+1,"footway_ids":sorted(ids)})
        xmin,xmax=sorted([a[0],b[0]]);ymin,ymax=sorted([a[1],b[1]])
        closest=10.; blocker=None
        for name,poly,r,bb in shapes:
            if bb[2]<xmin-closest or bb[0]>xmax+closest or bb[3]<ymin-closest or bb[1]>ymax+closest:continue
            d=dist_point(poly[0],a,b)-r if len(poly)==1 else poly_distance(a,b,poly)
            if d<closest:closest=d;blocker=name
        check(closest>=.32,f"{start}→{end} physical corridor blocked by {blocker} at {closest:.3f} m")
        results.append({"from":start,"to":end,"surface":model.get("name")+"/"+vis.get("name"),
                        "width_m":width,"length_m":round(math.dist(a,b),3),"max_endpoint_gap_m":round(gap,4),
                        "top_z_m":round(z,4),"nearest_obstacle":blocker,"centerline_clearance_m_capped_at_10":round(closest,3)})
    reached=set(); queue=["teaching_exit"]
    while queue:
        node=queue.pop()
        if node not in reached:
            reached.add(node);queue.extend(graph[node]-reached)
    check(reached==set(nodes),"Disconnected route graph")
    anchor=nodes["street_join"]
    hit=terrain_hit(models["delft_surveyed_site"],world_dir,anchor)
    survey=bgt_footpath(world_dir,anchor,hit)
    arrival=[row for row in results if "street_join" in (row["from"],row["to"])]
    check(arrival and all(abs(row["top_z_m"]-hit["surface_z_m"])<.08 for row in arrival),"Join has a vertical step")
    check(street_checks,"No segment follows the native surveyed street")
    return {"nodes":sorted(nodes),"segments":results,"surveyed_segments":street_checks,"obstacle_projections":len(shapes),"horizontal_body_radius_m":.32,
            "obstacle_z_interval_m":[.065,.60],"street_anchor_xy_m":anchor,"terrain_triangle":hit,
            "independent_footway":survey,
            "total_network_m":round(sum(row["length_m"] for row in results),2)}
