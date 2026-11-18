"""SDD-022 fixture tests for bounded image processing and source timing."""
from base64 import b64decode
import json
import struct
from types import SimpleNamespace as Obj
import unittest
import zlib

from frames import config,raw_frame,jpeg_frame,Source

JPEG=b64decode("/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD8rZ55LmaSaaRpZZGLvI7EszE5JJPUmiiis6fwL0OvF/7xU/xP8z//2Q==")
SPEC={"id":"front","topic":"/cam","type":"Image","ttl_s":1.}


def image(encoding="rgb8",data=b"\xff\x00\x00\x00\xff\x00xx",**fields):
    frame=Obj(width=2,height=1,encoding=encoding,step=len(data),data=data,
              header=Obj(frame_id="optical",stamp=Obj(sec=1,nanosec=0)))
    vars(frame).update(fields)
    return frame


def pixels(png):
    start=png.index(b"IDAT")
    return zlib.decompress(png[start+4:start+4+struct.unpack(">I",png[start-4:start])[0]])


class Frames(unittest.TestCase):
    def test_raw_rgb_bgr_mono_padding_and_scaling(self):
        self.assertEqual(pixels(raw_frame(image())[0]),b"\x00\xff\x00\x00\x00\xff\x00")
        self.assertEqual(pixels(raw_frame(image("bgr8"))[0]),b"\x00\x00\x00\xff\x00\xff\x00")
        self.assertEqual(pixels(raw_frame(image("mono8",b"\x0a\x30xx"))[0]),b"\x00\x0a\x30")
        large=image("rgb8",b"\x00\x11\x22"*641,width=641)
        png,_,_,_,pw,ph,_=raw_frame(large)
        self.assertEqual((pw,ph,len(pixels(png))),(321,1,964))
        self.assertEqual(pixels(png)[1:],b"\x00\x11\x22"*321)

    def test_raw_dimensions_encoding_and_truncation(self):
        for frame in (image(width=0),image(height=8192,width=8192),image(encoding="16UC1"),
                      image(step=1),image(data=b"a"),image(step=10)):
            with self.assertRaises(ValueError):
                raw_frame(frame)

    def test_jpeg_standard_dimensions_and_damage(self):
        self.assertEqual(jpeg_frame(Obj(format="rgb8; jpeg compressed bgr8",data=JPEG)),
                         (JPEG,"image/jpeg",2,2,2,2,"jpeg"))
        for fmt,data in (("png",JPEG),("jpeg",JPEG[:-12]),("jpeg",b"a"*100),("jpeg",JPEG.replace(b"\xff\xc0",b"\xff\xf7"))):
            with self.assertRaises(ValueError):
                jpeg_frame(Obj(format=fmt,data=data))

    def test_clock_and_stale_are_separate(self):
        s=Source(SPEC)
        start=image()
        s.observe(start,0,10**9)
        self.assertEqual(s.state,"online")
        s.observe(start,.3,10**9)
        self.assertEqual(s.rejected,1)
        s.refresh(1.1,10**9)
        self.assertEqual((s.state,s.data),("stale",None))
        s.reset_clock()
        s.observe(start,2,1)
        self.assertEqual(s.reason,"future_stamp")
        s.observe(start,3,3*10**9)
        self.assertEqual(s.reason,"old_stamp")
        s.observe(start,4,10**9)
        self.assertEqual(s.state,"online")

    def test_camera_info_requires_matched_frame_geometry_and_stamp(self):
        s=Source(SPEC)
        m=image()
        s.observe(m,0,10**9)
        s.calibration(Obj(width=2,height=1,header=m.header,k=[1,0,1,0,2,1,0,0,1]),.1)
        self.assertTrue(s.status(.2)["camera_info"]["calibrated"])
        self.assertIsNone(s.status(2)["camera_info"])
        m.header.frame_id="different"
        s.calibration(Obj(width=2,height=1,header=m.header,k=[1]*9),.3)
        self.assertIsNone(s.status(.4)["camera_info"])

    def test_routes_enforce_unique_identity_types_and_count(self):
        def wire(rows):
            return json.dumps({"schema_version":1,"sources":rows}).encode()
        self.assertEqual(config(wire([SPEC]))[0]["front"]["topic"],"/cam")
        for rows in ([SPEC,SPEC],[dict(SPEC,topic="relative")],[dict(SPEC,type="bad")],
                     [dict(SPEC,ttl_s=float("nan"))],[dict(SPEC,id=str(i)) for i in range(9)]):
            with self.assertRaises(ValueError):
                config(wire(rows))
        with self.assertRaises(ValueError):
            config(b'{"schema_version":1,"sources":[],"sources":[]}')


if __name__=="__main__":
    unittest.main()
