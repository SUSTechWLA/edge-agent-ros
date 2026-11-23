"""The three read-only implementations share source bytes and a goal, and admit different recipes."""
import asyncio
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib
from collections import Counter

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from edge_contracts.spatial_facts import SpatialFactError
from perception.review055 import adapters
from perception.review055.server import Worker
from perception.review055.site_workflow import lower_decile

def sha(data):
    return "sha256:"+hashlib.sha256(data).hexdigest()

def png(w,h,bits,color,rows):
    def chunk(tag,data):
        return struct.pack(">I",len(data))+tag+data+struct.pack(">I",zlib.crc32(tag+data))
    return (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",w,h,bits,color,0,0,0))
            +chunk(b"IDAT",zlib.compress(b"".join(b"\0"+row for row in rows)))+chunk(b"IEND",b""))

def graph(order="rank_nearest",stat="measure_p10"):
    return {"nodes":[{"id":"sample","op":stat,"input":"frame"},
                     {"id":"select","op":"classify_near","input":"sample"},
                     {"id":"sort","op":order,"input":"select"}],"output":"sort"}

class Context:
    def __init__(self,profile,ref,services):
        self.profile,self.ref,self.services=profile,ref,services
        self.tools=self
        self.exports={}
    def config(self):
        return {"profile":self.profile,"settings":{"frame_ref":self.ref}}
    def provide(self,name,call):
        self.services[name]=call
    def register(self,name,call):
        self.exports[name]=call
    def require(self,name):
        return self.services[name]

class Review(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir=Path(self.tmp.name)
        # Columns: four 1000mm values every row, exactly 60% at 300mm, only 30% valid at 200mm.
        w,h=12,10
        rgb=png(w,h,8,2,[bytes(w*3)]*h)
        rows=[struct.pack(">12H",*[1000]*4,*([300]*4 if y<6 else [0]*4),
                          *([200]*4 if y<3 else [65535]*4)) for y in range(h)]
        depth=png(w,h,16,0,rows)
        self.meta={"schema_version":1,"width":w,"height":h,"actor_ref":"rover","clock_id":"unix",
                   "frame_id":"optical","rgb_source_id":"rgb","depth_source_id":"z16","rgb_ref":sha(rgb),
                   "depth_ref":sha(depth),"calibration_ref":sha(b"known calib"),"rgb_stamp_ns":"100000000",
                   "depth_stamp_ns":"100000000","depth_scale_m":.001}
        for data in (rgb,depth):
            (self.dir/(sha(data)[7:]+".png")).write_bytes(data)
        self.ref=self.save_manifest()
        self.sensor=Worker("measure",self.dir,self.ref)
        self.user=Worker("workflow",self.dir,self.ref)
        self.calls=[]
        async def measure(payload):
            self.calls.append(payload)
            return [200,self.sensor.evaluate("/measure",payload)]
        self.svc={"review.measure":measure}
        for name in ("review.classify","review.rank"):
            adapters.apply(Context({"service":name},self.ref,self.svc))
        self.export={}
        for name in ("review_composed","review_builtin"):
            ctx=Context({"legacy":{"id":name}},self.ref,self.svc)
            adapters.apply(ctx)
            self.export[name]=ctx.exports[name]

    def save_manifest(self):
        raw=json.dumps(self.meta).encode()
        (self.dir/"frame.json").write_bytes(raw)
        return sha(raw)

    def run_tool(self,name,**extra):
        wire={"operation_id":"op/"+name,"arguments":{"columns":3,"distance_m":1.,**extra}}
        return self.user.evaluate("/invoke",wire) if name=="user" else asyncio.run(self.export[name](wire))

    def test_three_implementations_inclusive_and_partial_coverage(self):
        results=[self.run_tool("review_builtin"),self.run_tool("user"),self.run_tool("review_composed",graph=graph())]
        for run in results:
            self.assertEqual(run["state"],"SUCCEEDED",run)
            evidence=run["evidence"]
            self.assertEqual(evidence["frame_ref"],self.ref)
            self.assertEqual(evidence["analysis"]["rgb_ref"],self.meta["rgb_ref"])
            review=evidence["review"]
            self.assertEqual(review["review_order"],[2,1])
            self.assertEqual(review["unknown_columns"],[3])
            self.assertEqual([r["depth_m"] for r in review["regions"]],[1.,.3,.2])
        self.assertEqual(results[0]["evidence"]["review"],results[1]["evidence"]["review"])
        self.assertEqual(results[0]["evidence"]["review"],results[2]["evidence"]["review"])
        self.assertEqual(len(self.calls),2)  # workflow ran its own histogram over the same depth PNG
        self.assertEqual([r["service"] for r in results[2]["evidence"]["graph_receipts"]],
                         ["review.measure","review.classify","review.rank"])
        self.assertEqual(results[1]["evidence"]["workflow_digest"],self.user.health["workflow_digest"])

    def test_invalid_graph_is_rejected_before_consuming_frames(self):
        for mutate in (lambda g:g["nodes"][0].update(input="later"),
                       lambda g:g["nodes"][1].update(input="frame"),
                       lambda g:g["nodes"].append({"id":"extra","op":"measure_median","input":"frame"}),
                       lambda g:g["nodes"][1].update(op="publish_velocity"),
                       lambda g:g["nodes"][0].update(id="frame")):
            g=graph()
            mutate(g)
            self.assertEqual(self.run_tool("review_composed",graph=g)["state"],"BLOCKED")
        self.assertEqual(self.calls,[])

    def test_complete_but_wrong_goal_is_blocked(self):
        for recipe in (graph(order="rank_image"),graph(stat="measure_median")):
            self.assertEqual(self.run_tool("review_composed",graph=recipe)["state"],"BLOCKED")
        self.user.workflow=lambda *args:{"analysis":{},"review":{}}
        self.assertEqual(self.run_tool("user")["state"],"BLOCKED")

    def test_tampered_pixels_manifest_or_clock_never_yield_review(self):
        self.meta["depth_stamp_ns"]="900000000"
        new=self.save_manifest()
        with self.assertRaises(SpatialFactError):
            Worker("workflow",self.dir,new)
        self.assertEqual(self.run_tool("user")["state"],"BLOCKED")
        self.meta["depth_stamp_ns"]="100000000"
        self.assertEqual(self.save_manifest(),self.ref)
        (self.dir/(self.meta["rgb_ref"][7:]+".png")).write_bytes(b"substitute frame")
        self.assertEqual(self.run_tool("user")["state"],"BLOCKED")
        with self.assertRaises(SpatialFactError):
            self.sensor.evaluate("/measure",{"frame_ref":self.ref,"columns":3,"statistic":"p10"})

    def test_external_workflow_must_match_deployed_digest(self):
        bad=self.dir/"workflow.py"
        bad.write_text('raise RuntimeError("must not execute")')
        for expected in (None,sha(b"wrong")):
            with self.assertRaises(SpatialFactError):
                Worker("workflow",self.dir,self.ref,bad,expected)

    def test_site_histogram_handles_interpolation_and_single_valid_pixel(self):
        self.assertEqual(lower_decile(Counter({1200:1,300:1}),2),390)
        self.assertEqual(lower_decile(Counter({400:1}),1),400)
        self.assertIsNone(lower_decile(Counter(),0))

    def test_all_missing_preserves_region_coverage_and_unknown(self):
        payload=png(12,10,16,0,[b"\0"*24]*10)
        self.meta["depth_ref"]=sha(payload)
        (self.dir/(sha(payload)[7:]+".png")).write_bytes(payload)
        self.ref=self.save_manifest()
        self.user=Worker("workflow",self.dir,self.ref)
        response=self.run_tool("user")
        self.assertEqual(response["state"],"SUCCEEDED")
        review=response["evidence"]["review"]
        self.assertEqual(review["review_order"],[])
        self.assertEqual(review["unknown_columns"],[1,2,3])
        self.assertTrue(all(r["depth_m"] is None for r in review["regions"]))
