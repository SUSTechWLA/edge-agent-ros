"""Offline whole-map playback. Geometry and revision boundaries belong to global_map."""
import argparse
import base64
import hashlib
import io
import json
import math
import re
from bisect import bisect_right
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
import tempfile

from . import global_map, showcase


@contextmanager
def map_output(destination):
    """Publish one complete map replay to a new or empty directory on the same filesystem."""
    destination=Path(destination).absolute()
    def check():
        if destination.is_symlink() or destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise FileExistsError("map replay requires a new or empty output directory: "+str(destination))
    check()
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".map-render-",dir=destination.parent) as temp:
        staged=Path(temp)/"output"
        staged.mkdir()
        yield staged
        check()
        if destination.exists():
            destination.rmdir()  # succeeds only while still empty
        staged.rename(destination)


def display_image_role(row):
    role=row.get("image_role")
    source,topic=row.get("source"),row.get("topic")
    sourced=isinstance(source,str) and 0<len(source)<=200 and source.isprintable()
    named=isinstance(topic,str) and re.fullmatch(r"/(?:[A-Za-z_0-9]+/)*[A-Za-z_0-9]+",topic)
    simulated=row.get("clock_domain")=="gazebo_sim_ns" or sourced and source.startswith(("gz.","gazebo"))
    if sourced and named and role in ("onboard","overview"):
        return "overview_sim" if role=="overview" and simulated else role
    if row["kind"]=="rgb" and role is None and sourced and named and simulated and topic=="/commission/overview":
        return "overview_sim"
    return "unknown"


def preview_image(path):
    from PIL import Image,ImageOps
    with Image.open(path) as src:
        im=ImageOps.contain(src.convert("RGB"),(360,240))
        out=io.BytesIO()
        im.save(out,format="JPEG",quality=72)
        return "data:image/jpeg;base64,"+base64.b64encode(out.getvalue()).decode("ascii")


def saved_revision(info):
    grid=info["grid"]
    meta=info.get("metadata",{})
    bounds=meta.get("map_bounds",[0,0,grid["width"],grid["height"]])
    xmin,ymin,_,_=bounds
    known=[[xmin+i%grid["width"],ymin+i//grid["width"],v] for i,v in enumerate(grid["cells"]) if v>=0]
    return dict(segment=1,t=0,revision=meta.get("map_revision",0),frame=grid["frame"],bounds=bounds,
                origin=meta.get("map_anchor",grid["origin"]),resolution=grid["resolution"],changes=known,known_cells=len(known),
                added=0,updated=0,base=True,reason="已保存地图",stamp_ns=str(meta.get("stamp_ns","")),sha256=info["manifest_sha256"])


def write_html(destination, model):
    data=json.dumps(model,ensure_ascii=False,separators=(",",":"),allow_nan=False).replace("<","\\u003c")
    html=Path(__file__).with_name("map-view.html").read_text("utf8")
    destination.write_text(html.replace("__MAP_PAYLOAD__",data),encoding="utf8")


def saved_view(directory, digest, destination):
    info=global_map.read_map(directory,manifest_sha256=digest)
    model=dict(title="已保存的整图",description="已核对清单与地图内容，可缩放、平移查看。",task_id=digest,
               duration=0,saved=True,events=[],revisions=[saved_revision(info)],poses=[],signals=[],cameras={})
    write_html(destination,model)


class MapPlayback:
    def __init__(self,record,guide,axis,view,*,captioner,base=None,base_publisher_id=None,base_info=None):
        self.record=record
        self.view=view
        ms=lambda r: (r["_at"]-record.first)//1_000_000
        self.history=global_map.MapHistory(record.header["recording_id"],base=base,base_publisher_id=base_publisher_id)
        revisions=[]
        self.model=dict(task_id=record.header["task_id"],title=guide.get("title") or record.header["task_id"],
                        description="按观测时间展开累计地图。拖动平移，滚轮缩放。",duration=math.ceil((record.end-record.first)/1e6),
                        saved=False,revisions=revisions,poses=[],events=[],diagnostics=[],signals=[],cameras={})
        if base_info: revisions.append(saved_revision(base_info))
        for row in record.all:
            if row["kind"]!="map": continue
            blob=row["_path"].read_bytes() if row.get("_path") else None
            try:
                grid=global_map.decode_observation(row,blob)
                d=self.history.advance(row,grid)
            except ValueError as exc:
                cause=str(exc)[:250]
                if (cause=="map.attachment_digest" and blob is not None and hashlib.sha256(blob).hexdigest()!=row.get("sha256")
                        or cause=="map.base_scope_mismatch" and row.get("valid") is not False):
                    raise
                detail=row.get("detail") if row.get("valid") is False else None
                fault=dict(t=ms(row),seq=row["seq"],event_id=row["event_id"],stamp_ns=str(row["stamp_ns"]),
                           code=cause.split(":",1)[0],title=f'地图观测 #{row["seq"]} 未采用',
                           detail=cause+(" · "+detail[:200] if isinstance(detail,str) and detail else ""))
                if row.get("sha256"): fault["sha256"]=row["sha256"]
                self.model["diagnostics"].append(fault)
                continue
            if not d["changes"] and revisions and d["segment"]==revisions[-1]["segment"] and d["revision"]==revisions[-1]["revision"]:
                continue
            revisions.append(dict(d,t=ms(row),stamp_ns=str(d["stamp_ns"]),added=d["new_cells"],
                                  updated=d["changed_cells"],sha256=row["sha256"],seq=row["seq"],event_id=row["event_id"]))
        if any(b["t"]<a["t"] for a,b in zip(revisions,revisions[1:])):
            raise ValueError("map revisions are out of playback order; choose --axis received_ns")
        self.revision_ms=[r["t"] for r in revisions]
        ext=None
        prev=None
        for rev in revisions:
            if rev["segment"]!=prev: ext=None
            for x,y,_ in rev["changes"]:
                ext=[x,y,x+1,y+1] if ext is None else [min(ext[0],x),min(ext[1],y),max(ext[2],x+1),max(ext[3],y+1)]
            rev["fit"]=ext.copy() if ext else rev["bounds"]
            prev=rev["segment"]
        for row in view.events:
            if row["kind"] in ("rgb","overview","map","fused_pose","world_pose","laser_scan","imu"): continue
            name,detail=captioner(row,guide)
            self.model["events"].append(dict(t=ms(row),title=name,detail=detail,seq=row["seq"],event_id=row["event_id"]))
        self.model["diagnostics"].sort(key=lambda e:(e["t"],e["seq"]))
        self.model["events"].extend(self.model["diagnostics"])
        self.model["events"].sort(key=lambda e:(e["t"],e["seq"]))
        for signal in view.signals:
            samples=[]
            for row in record.by.get(signal["source"],[]):
                reading,_=view.samples(row["_at"],signal)
                value=reading["value"]
                samples.append(dict(t=ms(row),value=value,text=showcase.formatted(value) if value is not None else "—",status=showcase.STATUS[reading["status"]],
                                    seq=row["seq"]))
            self.model["signals"].append(dict(signal,samples=samples))
        for row in record.by.get("fused_pose",[]):
            t=ms(row)
            i=bisect_right(self.revision_ms,t)-1
            if i<0: continue
            r=revisions[i]
            if row.get("frame")!=r["frame"] or not isinstance(row.get("xy"),list) or len(row["xy"])!=2: continue
            # Historical data lacks cross-device alignment evidence. In a segmented recording
            # require explicit matching identity, even when both frames happen to be called odom.
            identity=row.get("map_identity")
            if identity is not None and identity!=r.get("identity"): continue
            if r["segment"]>1 and identity is None: continue
            if not all(showcase.finite(v) for v in (*row["xy"],row.get("yaw"))): continue
            self.model["poses"].append(dict(t=t,xy=row["xy"],yaw=row["yaw"],frame=row["frame"],segment=r["segment"]))
        self.cameras=defaultdict(list)
        for row in sorted(record.by.get("rgb",[])+record.by.get("overview",[]),key=lambda r:(r["_at"],r["seq"])):
            if not row.get("_path"): continue
            role=display_image_role(row)
            t=ms(row)
            shots=self.cameras[role]
            if shots and t-shots[-1]["t"]<500: continue
            shots.append(dict(t=t,hash=row["sha256"],src=preview_image(row["_path"]),seq=row["seq"]))
        self.model["cameras"]=dict(self.cameras)
        self._cursor=-1
        self._segment=None
        self._cells={}
        self._seed=set()
        self._hot={}
        self._raster=None
        self._raster_at=None

    def export(self,out):
        infos=self.history.segments()
        refs=[]
        for info in infos:
            segment=info["segment"]
            folder=out/"maps"/f"segment-{segment}"
            folder.parent.mkdir(exist_ok=True)
            self.history.save_segment(segment,folder,operation_id=f'{self.record.header["recording_id"]}/map-export/{segment}')
            digest=hashlib.sha256((folder/"manifest.json").read_bytes()).hexdigest()
            saved_view(folder,digest,folder/"index.html")
            refs.append(dict(segment=segment,folder=folder.relative_to(out).as_posix(),manifest_sha256=digest))
        self.model["saved_maps"]=refs
        write_html(out/"map.html",self.model)
        return refs

    def draw(self,t,options,guide):
        from PIL import Image,ImageDraw
        from .render import BG,PANEL,WHITE,GRAY,GREEN,AMBER,caption,font,paragraph
        img=Image.new("RGB",(1440,810),BG)
        d=ImageDraw.Draw(img)
        def txt(x,y,text,size=20,color=WHITE,width=550,lines=1):
            paragraph(d,(x,y),str(text),size,width,options.font,color,lines)
        txt(34,24,guide.get("title") or self.record.header["task_id"],34,WHITE,1120)
        txt(1190,28,f"+{t/1000:.1f} s",27,AMBER,220)
        d.rounded_rectangle((28,90,961,732),14,fill=PANEL)
        d.rounded_rectangle((975,90,1410,732),14,fill=PANEL)
        txt(53,108,"累计占据地图",22)
        n=bisect_right(self.revision_ms,t)-1
        if n<self._cursor:
            self._cursor=-1;self._segment=None;self._cells.clear();self._seed.clear()
        for idx in range(self._cursor+1,n+1):
            rev=self.model["revisions"][idx]
            if rev["segment"]!=self._segment:
                self._cells.clear();self._seed.clear();self._segment=rev["segment"]
            self._hot={}
            for x,y,value in rev["changes"]:
                self._hot[x,y]="changed" if (x,y) in self._cells else "new"
                self._cells[x,y]=value
                if rev.get("base"): self._seed.add((x,y))
                else: self._seed.discard((x,y))
        self._cursor=n
        if n<0:
            txt(265,378,"等待第一幅可用地图观测",29,GRAY)
        else:
            r=self.model["revisions"][n]
            b=r["fit"];sc=min(830/max(8,b[2]-b[0]),505/max(8,b[3]-b[1]))
            cx=(b[2]+b[0])/2;cy=(b[3]+b[1])/2
            area=self._raster if n==self._raster_at else Image.new("RGB",(886,534),"#263945")
            X=lambda x:443+(x-cx)*sc
            Y=lambda y:267-(y-cy)*sc
            if self._raster_at!=n:
                layer=ImageDraw.Draw(area)
                for (x,y),v in self._cells.items():
                    col="#bccfc3" if v<50 else "#dd9260"
                    if (x,y) in self._seed: col="#988fb9"
                    elif (x,y) in self._hot:
                        col=("#74dcba" if v<50 else "#c58d6a") if self._hot[x,y]=="new" else ("#ffcf84" if v<50 else "#f49a54")
                    layer.rectangle((math.floor(X(x)),math.floor(Y(y+1)),math.ceil(X(x+1))-1,math.ceil(Y(y))-1),fill=col)
                self._raster,self._raster_at=area,n
            area=area.copy();layer=ImageDraw.Draw(area)
            # Scale belongs to the current observed coordinate extent.
            bar=100*r["resolution"]/sc;pw=10**math.floor(math.log10(bar));metres=(5 if bar/pw>=5 else 2 if bar/pw>=2 else 1)*pw
            length=metres*sc/r["resolution"];layer.rectangle((14,486,38+length,523),fill=BG)
            layer.text((22,488),f"{metres:g} m",font=font(options.font,15),fill=WHITE)
            layer.line((22,515,22+length,515),fill=WHITE,width=2)
            poses=self.model["poses"]
            pi=bisect_right(poses,t,key=lambda p:p["t"])-1
            pose=poses[pi] if pi>=0 else None
            ox,oy,a=r["origin"]
            def position(p):
                vx,vy=p["xy"][0]-ox,p["xy"][1]-oy
                return X((vx*math.cos(a)+vy*math.sin(a))/r["resolution"]),Y((-vx*math.sin(a)+vy*math.cos(a))/r["resolution"])
            for p in poses[:pi+1]:
                if p["segment"]==r["segment"]:
                    x,y=position(p)
                    layer.ellipse((x-1.2,y-1.2,x+1.2,y+1.2),fill="#467d8a")
            if pose and pose["segment"]==r["segment"] and t-pose["t"]<1200:
                px,py=position(pose)
                rot=a-pose["yaw"]
                points=[(px+u*math.cos(rot)-v*math.sin(rot),py+u*math.sin(rot)+v*math.cos(rot)) for u,v in [(10,0),(-8,-6),(-5,0),(-8,6)]]
                layer.polygon(points,fill=BG,outline=WHITE,width=2)
            img.paste(area,(52,154))
            txt(54,699,f'{r["frame"]}  ·  {r["resolution"]:.3g} m/格  ·  分段 {r["segment"]}  ·  修订 {r["revision"]}',16,GRAY,880)
            txt(1000,114,"已观测区域",20,GRAY)
            txt(1000,151,f'{r["known_cells"]:,} 格',38,GREEN,382)
            txt(1000,218,f'本次新增 {r["added"]}  ·  更新 {r["updated"]}',20,AMBER,380)
            txt(1000,254,f'距上次地图观测 {(t-r["t"])/1000:.1f} s',17,GRAY,375)
        faults=self.model["diagnostics"]
        fi=bisect_right(faults,t,key=lambda e:e["t"])-1
        if fi>=0 and (n<0 or faults[fi]["seq"]>self.model["revisions"][n].get("seq",0)):
            fault=faults[fi]
            txt(275,115,"观测 #"+str(fault["seq"])+" 未采用 · "+fault["code"],16,AMBER,655)
        at=self.record.first+int(t*1e6)
        events=self.model["events"]
        active=bisect_right(events,t,key=lambda e:e["t"])-1
        name,details=(events[active]["title"],events[active]["detail"]) if active>=0 else caption(None,guide)
        txt(1000,312,"任务事件",18,GRAY)
        txt(1000,349,name,24,WHITE,370,2)
        txt(1000,428,details,17,GRAY,365,3)
        for i,s in enumerate(self.view.signals):
            reading,_=self.view.samples(at,s)
            value=reading["value"];v=showcase.STATUS[reading["status"]] if value is None else showcase.formatted(value)+" "+s["unit"]
            if reading.get("stale"): v+=" · 超时"
            txt(1000,536+i*47,s["label"],17,GRAY,180)
            txt(1195,535+i*47,v,18,GREEN,184)
        txt(34,756,"累计已知区　浅色 自由　橙色 占据　深色 未知　高亮 最近变化",17,GRAY,940)
        txt(1000,756,self.record.header["task_id"],16,GRAY,397)
        return img


if __name__=="__main__":
    cli=argparse.ArgumentParser(description="Verify and view a previously saved whole occupancy map offline")
    cli.add_argument("--saved-map",type=Path,required=True)
    cli.add_argument("--manifest-sha256",required=True)
    cli.add_argument("--out",type=Path,required=True)
    args=cli.parse_args()
    saved_view(args.saved_map,args.manifest_sha256,args.out)
