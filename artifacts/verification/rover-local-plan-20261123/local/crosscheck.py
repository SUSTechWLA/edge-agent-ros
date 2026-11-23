"""Independent cross-check of retained Gazebo-SDF, ROS receipts and per-link physics contacts."""
import argparse,bisect,collections,hashlib,json,math
from pathlib import Path
from xml.etree import ElementTree as ET

def number_vec(node,name,default):
 return tuple(map(float,node.findtext(name,default).split()))

def pose(n):return number_vec(n,'pose','0 0 0 0 0 0')
def at(x,y,dx,dy,a):return (x+dx*math.cos(a)-dy*math.sin(a),y+dx*math.sin(a)+dy*math.cos(a))
def nearest_box(x,y,xlo,ylo,xhi,yhi):return math.hypot(max(xlo-x,0,x-xhi),max(ylo-y,0,y-yhi))
def segment_distance(p,a,b):
 u,v=b[0]-a[0],b[1]-a[1]
 l=u*u+v*v
 f=max(0,min(1,((p[0]-a[0])*u+(p[1]-a[1])*v)/l)) if l else 0
 return math.dist(p,(a[0]+f*u,a[1]+f*v))

def independent(root):
 obs=list(map(json.loads,(root/'witness/observations.jsonl').open()))
 world_bytes=(root/'witness/world.sdf').read_bytes()
 world=ET.fromstring(world_bytes).find('world')
 geo=json.loads((root/'witness/geometry.json').read_bytes())
 s=json.loads((root/'witness/score.json').read_bytes())
 m=json.loads((root/'mission.json').read_bytes())
 truth=[r for r in obs if r['kind']=='truth']
 scan={r['stamp_ns']:r for r in obs if r['kind']=='scan'}
 rep={r['stamp_ns']:r for r in obs if r['kind']=='report'}
 all_ev=[r['event'] for r in obs if r['kind']=='event' and r['event']['operation_id']==s['operation']]
 first,last=[e['data']['scan_stamp_ns'] for e in all_ev if e['kind'] in ('action.started','action.result')]
 scan_active=set(t for t in scan if first<=t<=last)
 report_active=set(t for t in rep if first<=t<=last)
 measured=json.loads((root/'witness/measurements.json').read_bytes())
 assert all(x['stamp_ns'] in scan_active for x in measured)
 r=world.find("model[@name='edge_rover']")
 sensors={l.get('name'):(l.find("sensor[@type='contact']/contact/topic").text,l.find("sensor[@type='contact']/contact/collision").text) for l in r.findall('link') if l.find("sensor[@type='contact']") is not None}
 assert set(sensors)==set(['base_link','left_wheel','right_wheel','rear_caster'])
 for name,(topic,colname) in sensors.items():
  assert topic=='/sdd056/contacts/'+name and r.find(f"link[@name='{name}']/collision[@name='{colname}']") is not None
 contacts=[x for x in obs if x['kind']=='contacts']
 pairs=collections.Counter((a,b) for x in contacts for a,b,_,_ in x['pairs'])
 assert all('floor::floor_link::floor' in p for p in pairs)
 assert {p[0] for p in pairs}=={'edge_rover::left_wheel::tyre','edge_rover::right_wheel::tyre','edge_rover::rear_caster::ball'}
 # Independently compare the box with the actual moving tyre spheres, caster and chassis polygon.
 bbox=world.find("model[@name='sdd056_center_box']")
 box_footprint_min=None
 if bbox is not None:
  bx,by,*_=pose(bbox)
  sx,sy,sz=number_vec(bbox,'link/collision/geometry/box/size','')
  assert sx==sy==.5 and sz==1.
  box=[(bx+i*sx/2,by+j*sy/2) for i,j in ((1,1),(-1,1),(-1,-1),(1,-1))]
  def per_pose(v):
   x,y=v['xy'];yaw=v['yaw'];mins=[]
   for link in r.findall('link'):
    for c in link.findall('collision'):
     lx,ly,*_=pose(link);cx,cy,*_=pose(c)
     px,py=at(x,y,lx+cx,ly+cy,yaw)
     if c.find('geometry/sphere') is not None:
      mins.append(nearest_box(px,py,bx-.25,by-.25,bx+.25,by+.25)-float(c.findtext('geometry/sphere/radius')))
     else:
      w,h,_=number_vec(c,'geometry/box/size','')
      face=[at(px,py,i*w/2,j*h/2,yaw) for i,j in ((1,1),(-1,1),(-1,-1),(1,-1))]
      mins += [segment_distance(p,a,b) for shape,other in ((face,box),(box,face)) for p in shape for a,b in zip(other,other[1:]+other[:1])]
   return min(mins)
  box_footprint_min=min(per_pose(v) for v in truth)
  if s['passed']:assert box_footprint_min>=.45 and box_footprint_min>=s['min_swept_clearance_m']-1e-8
 independent_box=[]
 if bbox is not None:
  truth_ns=[t["stamp_ns"] for t in truth]
  lx,ly,*_=pose(r.find("./link/sensor[@name='horizontal_lidar']"))
  def hit_axis(a,p,lo,hi):
   if abs(a)<1e-10:return (-math.inf,math.inf) if lo<=p<=hi else (math.inf,-math.inf)
   e,f=(lo-p)/a,(hi-p)/a
   return min(e,f),max(e,f)
  for stamp in sorted(scan_active):
   q=bisect.bisect_left(truth_ns,stamp)
   if q==0 or q==len(truth):raise ValueError("scan outside independent pose coverage")
   ta,tb=truth[q-1],truth[q];u=(stamp-ta["stamp_ns"])/(tb["stamp_ns"]-ta["stamp_ns"])
   x,y=[a+(b-a)*u for a,b in zip(ta["xy"],tb["xy"])]; dyaw=tb["yaw"]-ta["yaw"]; yaw=ta["yaw"]+u*math.atan2(math.sin(dyaw),math.cos(dyaw))
   ox,oy=at(x,y,lx,ly,yaw); sc=scan[stamp];faces={}
   for i in range(len(sc["ranges"])):
    a=yaw+sc["start"]+i*sc["step"];ux,uy=math.cos(a),math.sin(a)
    tx,tx2=hit_axis(ux,ox,bx-.25,bx+.25);ty,ty2=hit_axis(uy,oy,by-.25,by+.25);t=max(tx,ty)
    if t>min(tx2,ty2) or not sc["range_min"]<=t<=sc["range_max"]:continue
    px,py=ox+t*ux,oy+t*uy
    bearing=math.remainder(math.atan2(py-y,px-x)-yaw,2*math.pi)
    if abs(bearing)<=math.pi/4:
     side=((0,ux>0) if tx>=ty else (1,uy>0));faces.setdefault(side,[]).append((px,py))
   if not faces:continue
   candidates=[]
   for (axis,sgn),points in faces.items():
    if axis==0:
     vals=[b for _,b in points];p=points[0][0],max(min(vals),min(y,max(vals)))
    else:
     vals=[a for a,_ in points];p=max(min(vals),min(x,max(vals))),points[0][1]
    candidates.append((math.dist((x,y),p),math.remainder(math.atan2(p[1]-y,p[0]-x)-yaw,2*math.pi)))
   dist,ang=min(candidates); rr=rep[stamp]["report"]
   independent_box.append(dict(stamp_ns=stamp,range_error_m=abs(dist-rr["nearest_range_m"]),
         bearing_error_deg=abs(math.degrees(math.remainder(ang-rr["nearest_bearing_rad"],2*math.pi)))))
  # The isolated plaza contains no intervening collision in any of these rays.
  # Verify the first oracle's world-occluded candidate timestamps are exactly reproduced.
  assert {q["stamp_ns"] for q in independent_box}=={q["stamp_ns"] for q in measured}
 observed_arrival=math.dist(truth[-1]['xy'],s['goal'])
 assert observed_arrival==s['arrival_error_m'] and hashlib.sha256(world_bytes).hexdigest()==s['source_sdf_sha256']
 assert scan_active==report_active and all(rep[t]['report']['stamp_ns']==t for t in scan_active)
 assert len(measured)==len({x['stamp_ns'] for x in measured})==s['visible_box_samples']
 out=dict(scenario=s['scenario'],passed=s['passed'],world_sha256=s['source_sdf_sha256'],truth_samples=len(truth),
   all_samples_including_setup={k:sum(r['kind']==k for r in obs) for k in ('scan','report','contacts')},
   full_operation_scan_count=len(scan_active),all_scans_have_report=scan_active==report_active,
   checked_all_box_measurements=len(measured),bearing_failures=sum((x.get('bearing_error_deg') or 0)>2 for x in measured),
   box_surface_to_footprint_min_sample_m=box_footprint_min,arrival_error_independent_m=observed_arrival,
   clearance_from_whole_scene_m=s['min_swept_clearance_m'],contact_set=dict((' // '.join(k),v) for k,v in pairs.items()),
   contact_sensor_targets=sensors,model_geometry_file='witness/world.sdf',core_state=m['state'],
   crosscheck_max_range_error_m=max((q["range_error_m"] for q in independent_box),default=None),
   crosscheck_max_bearing_error_deg=max((q["bearing_error_deg"] for q in independent_box),default=None),
   unique_event_ids=len(set(e['event_id'] for e in all_ev))==len(all_ev))
 return out

if __name__=='__main__':
 cli=argparse.ArgumentParser();cli.add_argument('base',type=Path);cli.add_argument('scenarios',nargs='*',default=('clear','centered'));args=cli.parse_args()
 for sc in args.scenarios:print(json.dumps(independent(args.base/sc),ensure_ascii=False))
