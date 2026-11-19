"""ROS image layout checks and portable PNG encoders for public preview data."""
import hashlib
import math
import re
import struct
import zlib

MAX_PIXELS=4096*2160
MAX_RAW=27*1024*1024
TOPIC=re.compile(r"/(?:[A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z_][A-Za-z0-9_]*)*")
NAME=re.compile(r"[a-z][a-z0-9_-]{0,31}")


def unique(pairs):
    obj={}
    for key,value in pairs:
        if key in obj:
            raise ValueError("config_invalid")
        obj[key]=value
    return obj


def dimensions(w,h):
    if not (1<=w<=8192 and 1<=h<=8192 and w*h<=MAX_PIXELS):
        raise ValueError("frame_dimensions")


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


def encode_gazebo(frame):
    w, h, pitch = frame.width, frame.height, frame.step
    pixels = frame.data
    # gz.msgs.PixelFormatType.RGB_INT8 is the wire enum value 3.
    if (frame.pixel_format_type != 3 or not 1 <= w <= 4096 or not 1 <= h <= 2160 or
            pitch < w * 3 or len(pixels) != pitch * h or len(pixels) > 32 * 1024 * 1024):
        raise ValueError("unsupported Gazebo RGB8 layout")
    stamp = frame.header.stamp.sec * 1_000_000_000 + frame.header.stamp.nsec
    if stamp <= 0:
        raise ValueError("waiting for a stamped frame")
    rows = [b"\x00" + pixels[y * pitch:y * pitch + w * 3] for y in range(h)]
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows), 3)) + chunk(b"IEND", b"")
    return png, {"width": w, "height": h, "stamp_ns": stamp, "sha256": hashlib.sha256(png).hexdigest()}
