"""SDD-048. Re-submit only a sealed recording's measured OccupancyGrid attachments via HTTP and compare live maps to the rendered segments."""
import argparse
import base64
from contextlib import contextmanager
import hashlib
from http.server import ThreadingHTTPServer
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener


ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from task_recorder.global_map import read_map
from task_recorder.service import Archive, Handler


def check(ok,detail):
    if not ok:
        raise AssertionError(detail)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def member(root,name):
    root=root.resolve(strict=True)
    entry=Path(name)
    check(not entry.is_absolute() and not entry.drive and ".." not in entry.parts,"unexpected evidence path")
    path=root/entry
    check(path.resolve(strict=True)==path and path.is_relative_to(root),"unexpected evidence path")
    return path


@contextmanager
def recorder():
    """A real HTTP recorder with its own archive, empty database and loopback port."""
    with tempfile.TemporaryDirectory(prefix="edge-map-replay-") as root:
        archive=Archive(root)
        try:
            with ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
                server.archive=archive
                thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
                thread.start()
                try:
                    yield f"http://127.0.0.1:{server.server_port}/recordings"
                finally:
                    server.shutdown()
                    thread.join()
        finally:
            archive.close()


class SegmentOracle:
    """Independent lattice counter. Consumes measured cells and examines every output cell."""
    def __init__(self,grid):
        self.anchor=grid["origin"]
        self.res=grid["resolution"]
        self.frame=grid["frame"]
        self.seen={}
        self.bounds=None
        self.added=self.updated=self.frames=0

    def observe(self,grid):
        x,y,angle=self.anchor
        c,s=math.cos(angle),math.sin(angle)
        dx,dy=grid["origin"][0]-x,grid["origin"][1]-y
        u,v=(c*dx+s*dy)/self.res,(-s*dx+c*dy)/self.res
        check(abs(u-round(u))<1e-4 and abs(v-round(v))<1e-4,"incompatible lattice within one segment")
        check(grid["frame"]==self.frame and math.isclose(grid["resolution"],self.res,rel_tol=1e-7)
              and abs(math.remainder(grid["origin"][2]-angle,math.tau))<=1e-7,"geometry changed without segmentation")
        ox,oy=round(u),round(v)
        xmin,ymin,xmax,ymax=ox,oy,ox+grid["width"],oy+grid["height"]
        if self.bounds:
            a,b,p,q=self.bounds
            xmin,ymin,xmax,ymax=min(a,xmin),min(b,ymin),max(p,xmax),max(q,ymax)
        self.bounds=xmin,ymin,xmax,ymax
        added,updated=0,0
        for i,value in enumerate(grid["cells"]):
            if value==-1:
                continue
            coord=(ox+i%grid["width"],oy+i//grid["width"])
            prior=self.seen.get(coord)
            added+=prior is None
            updated+=prior is not None and prior!=value
            self.seen[coord]=value
        self.added+=added
        self.updated+=updated
        self.frames+=1
        return added,updated

    def compare(self,grid):
        x0,y0,x1,y1=self.bounds
        w,h=x1-x0,y1-y0
        x,y,angle=self.anchor
        c,s=math.cos(angle),math.sin(angle)
        expected=[x+self.res*(x0*c-y0*s),y+self.res*(x0*s+y0*c),angle]
        check(grid["width"]==w and grid["height"]==h and grid["frame"]==self.frame and
              math.isclose(grid["resolution"],self.res,rel_tol=1e-7) and
              all(math.isclose(a,b,abs_tol=1e-8,rel_tol=0) for a,b in zip(grid["origin"],expected)),"snapshot geometry mismatch")
        cells=grid["cells"]
        check(len(cells)==w*h and all(v==self.seen.get((x0+i%w,y0+i//w),-1) for i,v in enumerate(cells)),
              "snapshot differs from measured cells")


def verify(recording,layout,report):
    header=json.loads(member(recording,"recording.json").read_bytes())
    movie=json.loads(member(layout,"video.json").read_bytes())
    rid,task=header["recording_id"],header["task_id"]
    check(header["status"]!="ACTIVE" and rid==movie["recording_id"] and movie["task_id"]==task,"different or unsealed recording")
    timeline=member(recording,"media/mission/timeline.jsonl")
    timeline_hash=digest(timeline)
    check(movie["timeline_sha256"]==timeline_hash and digest(member(layout,"task.mp4"))==movie["video_sha256"],"timeline or video SHA mismatch")
    refs=movie["map"]["segments"]
    check([r["segment"] for r in refs]==list(range(1,len(refs)+1)) and refs,"missing or unordered map segments")
    report.update(recording_id=rid, task_id=task, timeline_sha256=timeline_hash, video_sha256=movie["video_sha256"],
                  input_counts=header["counts"], submitted_kinds=["map"], map_frames=0, anchor_shifts=0,
                  origin_shift_samples=[], segment_transitions=[], verified_segments=[])
    opener=build_opener(ProxyHandler({}))
    def request(url,body=None):
        raw=None if body is None else json.dumps(body,allow_nan=False).encode()
        with opener.open(Request(url,data=raw,headers={"Content-Type":"application/json"}),timeout=12) as response:
            return json.load(response)
    with recorder() as endpoint:
        request(endpoint+"/start",{"recording_id":rid,"task_id":task,"sources":["map"]})
        api=endpoint+"/"+quote(rid,safe="")
        check(request(api+"/global-map")["grid"] is None,"nonempty initial live map")
        oracles=[]
        previous=None
        seq=0
        with timeline.open(encoding="utf8") as stream:
            for line in stream:
                row=json.loads(line)
                check(type(row.get("seq")) is int and row["seq"]>seq,"unordered archive sequence")
                seq=row["seq"]
                if row["kind"]!="map":
                    continue
                path=member(timeline.parent,row["file"])
                check(0<path.stat().st_size<=8*1024**2 and digest(path)==row["sha256"],"map attachment digest mismatch")
                raw=path.read_bytes()
                grid=json.loads(raw)
                payload={k:v for k,v in row.items() if k not in ("received_ns","seq","file","sha256")}
                payload["media"]={"name":row["file"],"base64":base64.b64encode(raw).decode("ascii")}
                written=request(api+"/events",payload)
                live=request(api+"/global-map")
                check(not written["duplicate"] and live["accepted_seq"]==written["seq"] and not live["catching_up"] and
                      live["faults"]==0 and live["status"]=="ACTIVE","new map not visible during capture")
                latest=live["latest"]
                segment=latest["segment"]
                if previous and previous["grid"]["origin"]!=grid["origin"]:
                    report["anchor_shifts"]+=1
                    if len(report["origin_shift_samples"])<8:
                        report["origin_shift_samples"].append({"archive_seq":seq,"previous":previous["grid"]["origin"],"current":grid["origin"]})
                if previous:
                    prev=previous["row"]
                    fixed=bool(row.get("map_identity") and row.get("publisher_id") and row.get("fixed_frame") is True)
                    source_changed=any(prev.get(k)!=row.get(k) for k in ("publisher_id","map_identity","topic","clock_domain"))
                    source_changed|=(prev.get("map_load_time_ns") or 0)!=(row.get("map_load_time_ns") or 0)
                    cleared=not fixed and not any(v>=0 for v in grid["cells"]) and bool(oracles[-1].seen)
                    boundary=source_changed or row["stamp_ns"]<prev["stamp_ns"] or row.get("map_reset") or cleared
                    if boundary:
                        check(segment==previous["segment"]+1,"independent reset/source evidence was merged")
                if segment>len(oracles):
                    check(segment==len(oracles)+1,"non-monotonic segment number")
                    oracles.append(SegmentOracle(grid))
                    report["segment_transitions"].append({"segment":segment,"source_seq":seq,"stamp_ns":row["stamp_ns"],
                                                         "reason":latest["reason"]})
                else:
                    check(segment==len(oracles),"unexpected rollback to a historical segment")
                oracle=oracles[-1]
                added,updated=oracle.observe(grid)
                check((latest["new_cells"],latest["changed_cells"],latest["known_cells"])==(added,updated,len(oracle.seen)),
                      f"wrong per-frame counts at archive sequence {seq}")
                oracle.compare(live["grid"])
                report["map_frames"]+=1
                previous={"grid":grid,"row":row,"segment":segment}
        check(report["map_frames"]==header["counts"].get("map") and len(oracles)==len(refs),"missing source frames or segments")
        request(api+"/finish",{"task_id":task,"status":header.get("task_status") or "COMPLETE"})
        for ref,oracle in zip(refs,oracles):
            sid=ref["segment"]
            end=request(api+"/global-map?segment="+str(sid))
            check(end["status"]=="FINISHED" and end["selected_segment"]==sid and not end["faults"],"sealed retrieval failed")
            folder=member(layout,ref["folder"])
            artifact=read_map(folder,manifest_sha256=ref["manifest_sha256"])
            oracle.compare(artifact["grid"])
            check(artifact["grid"]==end["grid"],"live and saved grids differ")
            check(oracle.added==len(oracle.seen)==artifact["known_cells"],"cumulative known count mismatch")
            segment=end["segments"][sid-1]
            check(artifact["metadata"]["map_identity"]==segment["identity"] and
                  artifact["metadata"]["stamp_ns"]==segment["last_stamp_ns"],"saved map provenance mismatch")
            report["verified_segments"].append({"segment":sid,"frames":oracle.frames,"new_cells":oracle.added,
                "changed_cells":oracle.updated,"final_known":len(oracle.seen),"total_cells":len(end["grid"]["cells"]),
                "manifest_sha256":ref["manifest_sha256"],"native_cells_equal":True})
    report.update(origin_shift_coverage="observed" if report["anchor_shifts"] else "absent_in_this_recording",
                  segment_boundary_coverage="observed" if len(refs)>1 else "absent_in_this_recording",
                  excluded_from_projection=["camera frames","robot paths","Gazebo world pose","scene model"],passed=True)


def main():
    cmd=argparse.ArgumentParser(description=__doc__)
    cmd.add_argument("--recording",type=Path,required=True,help="sealed directory containing recording.json")
    cmd.add_argument("--layout",type=Path,required=True,help="finished --layout map output directory containing video.json")
    cmd.add_argument("--out",type=Path,required=True)
    args=cmd.parse_args()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    report={"spec":"SDD-048","passed":False}
    with args.out.open("x",encoding="utf8") as output:
        try:
            verify(args.recording,args.layout,report)
        except Exception as error:
            report["error"]=str(error)
            raise
        finally:
            json.dump(report,output,ensure_ascii=False,indent=2)
            output.write("\n")
    print(f"{report['map_frames']} observed grids; {len(report['verified_segments'])} live/saved segment(s) matched")


if __name__=="__main__":
    main()
