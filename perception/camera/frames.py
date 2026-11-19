"""Bounded ROS camera preview and configuration contracts, independently testable."""
import hashlib
import json
import math
import re
import struct
import zlib

MAX_PIXELS=4096*2160
MAX_RAW=27*1024*1024
MAX_JPEG=2*1024*1024
TOPIC=re.compile(r"/(?:[A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z_][A-Za-z0-9_]*)*")
NAME=re.compile(r"[a-z][a-z0-9_-]{0,31}")


def unique(pairs):
    obj={}
    for key,value in pairs:
        if key in obj:
            raise ValueError("config_invalid")
        obj[key]=value
    return obj


def config(raw):
    if len(raw)>16384:
        raise ValueError("config_size")
    obj=json.loads(raw,object_pairs_hook=unique)
    if (not isinstance(obj,dict) or set(obj)!={"schema_version","sources"} or type(obj["schema_version"])!=int
            or obj["schema_version"]!=1 or not isinstance(obj["sources"],list) or len(obj["sources"])>8):
        raise ValueError("config_invalid")
    result={}
    for src in obj["sources"]:
        if (not isinstance(src,dict) or set(src)-{"id","topic","label","type","info_topic","ttl_s"}
                or not isinstance(src.get("id"),str) or not NAME.fullmatch(src["id"]) or src["id"] in result
                or src.get("type") not in ("Image","CompressedImage") or not isinstance(src.get("topic"),str)
                or len(src["topic"])>200 or not TOPIC.fullmatch(src["topic"])):
            raise ValueError("config_invalid")
        if "info_topic" in src and (not isinstance(src["info_topic"],str) or len(src["info_topic"])>200 or not TOPIC.fullmatch(src["info_topic"])):
            raise ValueError("config_invalid")
        ttl=src.get("ttl_s",3)
        label=src.get("label",src["id"])
        if (type(ttl) not in (float,int) or not .25<=ttl<=60 or not isinstance(label,str)
                or not 1<=len(label)<=80 or not label.isprintable()):
            raise ValueError("config_invalid")
        result[src["id"]]={**src,"label":label,"ttl_s":ttl}
    return result,hashlib.sha256(raw).hexdigest()


def dimensions(w,h):
    if not (1<=w<=8192 and 1<=h<=8192 and w*h<=MAX_PIXELS):
        raise ValueError("frame_dimensions")


def jpeg_frame(msg):
    data=msg.data
    if not 20<=len(data)<=MAX_JPEG:
        raise ValueError("jpeg_size")
    fmt=msg.format.lower()
    if not any(token in fmt for token in ("jpeg","jpg")):
        raise ValueError("jpeg_format")
    raw=bytes(data)
    if not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        raise ValueError("jpeg_marker")
    # Validate marker lengths and bounded dimensions before letting a browser decode entropy data.
    i=2
    wh=None
    while i<len(raw)-2:
        if raw[i]!=255:
            raise ValueError("jpeg_marker")
        while i<len(raw) and raw[i]==255:
            i+=1
        if i+3>len(raw):
            break
        marker=raw[i]
        length=int.from_bytes(raw[i+1:i+3],"big")
        if length<2 or i+1+length>len(raw)-2:
            raise ValueError("jpeg_marker")
        if marker in (0xc0,0xc1,0xc2):
            if length<11 or raw[i+3]!=8:
                raise ValueError("jpeg_format")
            h,w=struct.unpack_from(">HH",raw,i+4)
            dimensions(w,h)
            wh=(w,h)
        if marker==0xda:
            if wh and length>=6 and i+1+length<len(raw)-2:
                return raw,"image/jpeg",*wh,*wh,"jpeg"
            break
        i+=1+length
    raise ValueError("jpeg_marker")


def chunk(kind,buf):
    return struct.pack(">I",len(buf))+kind+buf+struct.pack(">I",zlib.crc32(kind+buf)&0xffffffff)


def raw_frame(msg):
    w,h,step=msg.width,msg.height,msg.step
    dimensions(w,h)
    enc=msg.encoding.lower()
    if enc not in ("rgb8","bgr8","mono8"):
        raise ValueError("raw_encoding")
    channels=1 if enc=="mono8" else 3
    if step<w*channels or step*h!=len(msg.data) or len(msg.data)>MAX_RAW:
        raise ValueError("raw_layout")
    scale=max(1,math.ceil(w/640),math.ceil(h/480))
    view=memoryview(msg.data)
    rows=[]
    pw,ph=math.ceil(w/scale),math.ceil(h/scale)
    for y in range(0,h,scale):
        row=view[y*step:y*step+w*channels]
        if channels==1:
            pixels=bytes(row[::scale])
        elif scale==1 and enc=="rgb8":
            pixels=bytes(row)
        else:
            pixels=bytearray(pw*3)
            for k in range(3):
                first=k if enc=="rgb8" else 2-k
                pixels[k::3]=bytes(row[first::3*scale])
        rows.append(b"\x00"+pixels)
    header=struct.pack(">IIBBBBB",pw,ph,8,0 if channels==1 else 2,0,0,0)
    png=b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",header)+chunk(b"IDAT",zlib.compress(b"".join(rows),2))+chunk(b"IEND",b"")
    return png,"image/png",w,h,pw,ph,enc


class Source:
    def __init__(self,spec):
        self.spec=spec
        self.state,self.reason="waiting","no_frame"
        self.received,self.rejected=0,0
        self.last_stamp=0
        self.arrival=None
        self.data=None
        self.meta={}
        self.info=None
        self.last_encode=-1.

    def clear(self,state,reason):
        self.state,self.reason,self.data=state,reason,None

    def observe(self,msg,now,ros_ns):
        stamp=msg.header.stamp.sec*10**9+msg.header.stamp.nanosec
        frame=msg.header.frame_id
        try:
            if not isinstance(frame,str) or not frame or len(frame)>200 or not frame.isprintable():
                raise ValueError("frame_id")
            if ros_ns<=0 or stamp<=0:
                raise ValueError("clock_unavailable")
            if stamp>ros_ns+500000000:
                raise ValueError("future_stamp")
            if ros_ns-stamp>self.spec["ttl_s"]*1e9:
                raise ValueError("old_stamp")
            if stamp<=self.last_stamp:
                raise ValueError("out_of_order")
            if now-self.last_encode<.2 and self.state=="online":
                return
            self.last_encode=now
            decode=raw_frame if self.spec["type"]=="Image" else jpeg_frame
            raw,mime,w,h,pw,ph,enc=decode(msg)
        except (ValueError,TypeError,OverflowError,struct.error) as ex:
            self.rejected+=1
            # A late duplicate cannot replace an already newer, fresh observation.
            if str(ex)!="out_of_order" or self.state!="online":
                self.clear("invalid",str(ex))
            return
        self.received+=1
        self.last_stamp,self.arrival,self.data,self.state,self.reason=stamp,now,raw,"online","frame_received"
        self.meta={"width":w,"height":h,"preview_width":pw,"preview_height":ph,"content_type":mime,
                   "encoding":enc,"stamp_ns":stamp,"stamp_ns_text":str(stamp),"frame_id":frame}

    def refresh(self,now,ros_ns):
        if self.state=="online" and (now-self.arrival>self.spec["ttl_s"] or ros_ns-self.last_stamp>self.spec["ttl_s"]*1e9):
            self.clear("stale","frame_timeout")

    def reset_clock(self):
        self.last_stamp,self.arrival,self.info,self.meta=0,None,None,{}
        self.clear("waiting","clock_reset")

    def calibration(self,msg,now):
        if len(msg.k)!=9 or not all(math.isfinite(v) for v in msg.k) or not msg.header.frame_id:
            self.info=None
            return
        self.info=(now,{"width":msg.width,"height":msg.height,"frame_id":msg.header.frame_id,
                        "stamp_ns":msg.header.stamp.sec*10**9+msg.header.stamp.nanosec,
                        "fx":float(msg.k[0]),"fy":float(msg.k[4]),"cx":float(msg.k[2]),"cy":float(msg.k[5]),
                        "calibrated":bool(msg.k[0]>0)})

    def status(self,now):
        cal=None
        if self.state=="online" and self.info and now-self.info[0]<=self.spec["ttl_s"]:
            c=self.info[1]
            if (all(c[k]==self.meta.get(k) for k in ("width","height","frame_id")) and c["stamp_ns"]>0
                    and abs(c["stamp_ns"]-self.last_stamp)<=self.spec["ttl_s"]*1e9):
                cal=c
        return {**self.spec,**self.meta,"state":self.state,"reason":self.reason,"received":self.received,
                "rejected":self.rejected,"camera_info":cal}
