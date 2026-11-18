"""The TU Delft X sports grounds and a continuous, synthetic pedestrian circuit in odom."""
import math
import random
from xml.etree import ElementTree as ET

if __package__:
    from .athletics import PlayingSurface
    from .sdf import include, line, shape, static, tag
else:
    from athletics import PlayingSurface
    from sdf import include, line, shape, static, tag


TRACK_CENTER = (-234,61)
TRACK_YAW = math.radians(20)
CAMPUS_EYE = [-145,-295,145]
CAMPUS_TARGET = [-128,55,6]


def field_xy(x,y):
    co,si = math.cos(TRACK_YAW), math.sin(TRACK_YAW)
    return (round(TRACK_CENTER[0]+co*x-si*y,3), round(TRACK_CENTER[1]+si*x+co*y,3))


# The primary walk turns south from the teaching portal. Its return through the
# woodland runs parallel to the surveyed motorway, on the campus side of the belt.
# The last three samples are from the Dutch BGT Mekelweg west-side footpath.
WALK = [
    ("teaching_exit",(3.3,0)), ("plaza",(11.5,0)), ("terrace",(12,-13)),
    ("faculty_walk",(-15,-16)), ("court_walk",(-54,-15)), ("sports_walk",(-96,-5)),
    ("sports_green",(-141,5)), ("gallery_walk",(-179,7)),
    ("south_concourse",field_xy(0,-57)), ("track_gate",field_xy(0,-46.6)),
    ("west_concourse",field_xy(-1.5,-55.7)), ("meadow_entry",(-238,-17)),
    ("lawn",(-243,-44)), ("copse_entry",(-245,-73)), ("grove_west",(-245,-110)),
    ("woodland",(-151,-80)), ("grove_east",(-88,-59)), ("forest_exit",(-82,-39)),
    ("garden_walk",(-58,-32)), ("east_green",(-12,-22)), ("east_plaza",(18,-14)),
    ("campus_gate",(41,0)), ("gate_apron",(55.5,8)), ("street_join",(65.5,14)),
    ("mekelweg_bend",(58.8,29)), ("mekelweg",(51,47)),
]
SENSOR_STATIONS = [
    {"id":"pitch_centre","xy":list(TRACK_CENTER),"yaw":TRACK_YAW,"environment":"open"},
    {"id":"woodland_trail","xy":[-151,-80],"yaw":.32,"environment":"cluttered"},
    {"id":"teaching_forecourt","xy":[7.8,.7],"yaw":math.pi,"environment":"street"},
]
WALK_COLOR = (.67,.61,.53)
GRAVEL = (.62,.53,.39)
METAL = (.20,.26,.25)
WOOD = (.61,.43,.27)
FOLIAGE = [(.18,.34,.18),(.20,.38,.20),(.24,.42,.22),(.29,.46,.22)]


def point_distance(point, a, b):
    dx,dy=b[0]-a[0],b[1]-a[1]
    t=max(0,min(1,((point[0]-a[0])*dx+(point[1]-a[1])*dy)/(dx*dx+dy*dy)))
    return math.hypot(point[0]-a[0]-t*dx,point[1]-a[1]-t*dy)


def protected(point, margin):
    return any(point_distance(point,a,b)<margin for (_,a),(_,b) in zip(WALK,WALK[1:]))


def bench(link, name, x, y, yaw):
    # Three fine timber seat boards on a low frame, with legs separately sensed.
    c,s=math.cos(yaw),math.sin(yaw)
    for i in range(3):
        offset=(i-1)*.17
        shape(link,f"{name}_seat_{i}","box",(x-s*offset,y+c*offset,.53),(2.1,.145,.10),WOOD,True,yaw)
    for i in (-1,1):
        shape(link,f"{name}_foot_{i}","box",(x+c*i*.76,y+s*i*.76,.24),(.13,.46,.48),METAL,True,yaw)


def lamp(link,name,x,y,yaw):
    shape(link,name+"_base","cylinder",(x,y,.19),(.19,.38),METAL,True)
    shape(link,name+"_column","cylinder",(x,y,2.5),(.075,4.9),METAL,True)
    dx,dy=math.cos(yaw),math.sin(yaw)
    shape(link,name+"_arm","box",(x+.38*dx,y+.38*dy,4.95),(.95,.13,.13),METAL,yaw=yaw)
    shape(link,name+"_lamp","box",(x+.8*dx,y+.8*dy,4.88),(.46,.31,.13),(.82,.81,.70),yaw=yaw)


def link_mesh(link,name,path,output):
    part=ET.SubElement(link,"visual",name=name)
    mesh=ET.SubElement(ET.SubElement(part,"geometry"),"mesh")
    tag(mesh,"uri",path.relative_to(output).as_posix())


def pedestrian_grounds(world, output):
    road=static(world,"road")
    lawn=static(world,"lawn")
    plaza=PlayingSurface()
    palette={"ledge":(.60,.60,.54),"paver":(.63,.54,.45),"drain":(.37,.40,.37),
             "stone":(.76,.71,.62),"gravel":GRAVEL,"border":(.67,.65,.56)}
    for i in range(5):
        palette["brick"+str(i)]=(.66+i*.019,.53+i*.019,.42+i*.02)
    # The old clay courts become an entrance forecourt. A narrow planted margin
    # bounds the paving, and the forecourt extends under the building's open porch.
    court=[(1.62,-8.9),(5.8,-13.5),(18.4,-13.5),(23,-8.9),(23,7.4),
           (18.8,11.2),(5.8,11.2),(1.62,7.6)]
    plaza.polygon("faculty_court","paver",court,.074)
    rng=random.Random(629)
    # Basket-weave pairs are geometry so close range and aerial LOD share colours.
    for row in range(45):
        y=-10.2+row*.44
        for col in range(38):
            x=4.65+col*.44
            for half in (0,1):
                dx,dy=(.22,.44) if (row+col)%2==0 else (.44,.22)
                x0,y0=x+(.22*half if dx<dy else 0),y+(.22*half if dy<dx else 0)
                plaza.polygon("laid_brick","brick"+str(rng.randrange(5)),
                              [(x0+.008,y0+.008),(x0+dx-.008,y0+.008),
                               (x0+dx-.008,y0+dy-.008),(x0+.008,y0+dy-.008)],.080)
    for i,(a,b) in enumerate(zip(court,court[1:]+court[:1])):
        line(road,"court_edging_"+str(i),a,b,.17,.084,(.77,.72,.62))
    for x in (6.8,11.3,15.8,20.3):
        line(road,"paving_band_"+str(x),(x,-10.2),(x,9.4),.13,.086,(.79,.74,.65))
    route={"id":"campus_walk","nodes":[{"id":id_,"xy":list(xy)} for id_,xy in WALK],"edges":[]}
    for i,((first,a),(last,b)) in enumerate(zip(WALK,WALK[1:])):
        trail=12<=i<=17
        width = 2.8 if trail else 3.5 if i>=18 else 4.1
        name="walk_"+first+"_to_"+last
        # On the final mapped footpath, retain the georeferenced aerial surface.
        # The thin SDF witness sits below its 25 mm surface along these two edges.
        existing=i>=23
        shade=GRAVEL if trail else WALK_COLOR
        z=.016 if existing else .062
        line(road,name,a,b,width,z,shade,.010)
        shape(road,"join_"+last,"cylinder",(*b,z),(width/2,.01),shade)
        route["edges"].append({"from":first,"to":last,"model":"road","visual":name,"width_m":width,
                               "surface":"survey_footpath" if existing else "gravel" if trail else "paved"})
        if existing:
            continue
        dx,dy=b[0]-a[0],b[1]-a[1]
        length=math.hypot(dx,dy)
        nx,ny=-dy/length,dx/length
        if not trail:
            for sign in (-1,1):
                offset=sign*(width/2-.07)
                line(road,f"walk_edge_{i}_{sign}",(a[0]+nx*offset,a[1]+ny*offset),
                     (b[0]+nx*offset,b[1]+ny*offset),.12,.073,(.77,.73,.66))
            for n in range(1,math.ceil(length/4.8)):
                t=n*4.8/length
                x,y=a[0]+dx*t,a[1]+dy*t
                line(road,f"joint_{i}_{n}",(x-nx*(width/2-.16),y-ny*(width/2-.16)),
                     (x+nx*(width/2-.16),y+ny*(width/2-.16)),.045,.071,(.54,.52,.46))
        if 3<=i<=7 and length>20:
            # Lamp + avenue tree pairs define the arrival sequence from the street.
            x,y=(a[0]+b[0])/2+nx*3.1,(a[1]+b[1])/2+ny*3.1
            lamp(road,"avenue_light_"+str(i),x,y,math.atan2(-ny,-nx))
    for i,(x,y,angle) in enumerate(((5.8,8.3,0),(20.2,-6.5,math.pi/2),
                                    (-126,-.8,.25),(-244,-37,1.28),(-234,-45,1.2))):
        bench(lawn,"seat_"+str(i),x,y,angle)
    # Resting terrace touches the meadow; the open turf itself comes from the aerial.
    shape(lawn,"meadow_seating","cylinder",(-238,-41,.053),(5.4,.018),(.67,.61,.50))
    link_mesh(road,"forecourt_paving",plaza.write(output/"meshes","forecourt",palette),output)
    route["survey_road"]={"name":"Mekelweg west footpath","join_node":"street_join","xy":[65.5,14],
                          "source":"PDOK BGT footpath + 2025 georeferenced orthophoto","frame":"odom"}
    return route


def trees(world):
    woods=static(world,"woods")
    rng=random.Random(2907)
    count=0
    placed=[]
    def tree(x,y,park=False):
        nonlocal count
        if protected((x,y),2.65) or any(math.hypot(x-a,y-b)<3.6 for a,b in placed):
            return
        placed.append((x,y))
        count+=1
        if not park and count%3==0:
            include(world,"pine_"+str(count),"model://Pine Tree",(x,y,.025),rng.uniform(0,math.tau))
            return
        height=rng.uniform(5.4,8.3) if park else rng.uniform(4.8,8.7)
        radius=height*rng.uniform(.28,.37)
        shape(woods,f"trunk_{count}","cylinder",(x,y,height*.34),(.15+height*.022,height*.68),(.32,.245,.17),True)
        tone=rng.randrange(len(FOLIAGE))
        for k in range(5):
            angle=k*math.tau/5+rng.uniform(-.2,.2)
            extent=radius*rng.uniform(.72,.93)
            shade=FOLIAGE[(tone+(k==3))%len(FOLIAGE)]
            shape(woods,f"crown_{count}_{k}","ellipsoid",
                  (x+radius*.47*math.cos(angle),y+radius*.47*math.sin(angle),height*(.77+.05*(k%2))),
                  (extent,extent*rng.uniform(.80,.99),radius*rng.uniform(.85,1.20)),shade)
    # Actual canopy belt on the aerial. Stagger stems around a winding clear trail.
    for i in range(25):
        t=i/24
        for j in range(5):
            dx,dy=rng.uniform(-2,2),rng.uniform(-2,2)
            tree(-253+178*t+1.9*j+dx,-100+55*t-6.0*j+dy)
    for x,y in ((-152.0,-76.6),(-149.5,-83.9)):
        tree(x,y)
    for start,end in ((3,8),(18,21)):
        for n in range(start,end):
            a,b=WALK[n][1],WALK[n+1][1]
            dx,dy=b[0]-a[0],b[1]-a[1]
            length=math.hypot(dx,dy)
            for i in range(round(length/13)):
                t=(i+.5)/max(1,round(length/13))
                x,y=a[0]+t*dx,a[1]+t*dy
                tree(x-dy/length*4.1,y+dx/length*4.1,True)
    for x,y in ((23,9.7),(24,-5),(4.2,-11.9),(-40,-9.8),(-235,-35),(-252,-33),(-233,-48)):
        tree(x,y,True)
    return count
