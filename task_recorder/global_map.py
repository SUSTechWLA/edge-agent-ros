"""Accumulate recorded ROS occupancy windows on one explicitly identified planar lattice."""
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
from copy import deepcopy

from exploration.frontier import Grid
from exploration.map_artifact import save_map


MAX_CELLS = 262144
MAX_SIDE = 4096
CELL_EPSILON = 0.0001
SCOPE_FIELDS = {"environment", "robot_id", "map_id", "epoch", "clock_domain", "calibration_id"}
FILE_LIMITS = {"manifest.json":65536, "map.pgm":MAX_CELLS+256,
               "map.yaml":4096, "occupancy.json":4*1024*1024}


def require(predicate, code):
    if not predicate:
        raise ValueError(code)


def scope(value):
    require(isinstance(value, dict) and set(value) == SCOPE_FIELDS, "map.identity_invalid")
    for field in SCOPE_FIELDS:
        item = value[field]
        require(isinstance(item, str) and 1 <= len(item) <= 200 and item.isprintable()
                and item.strip() == item, "map.identity_invalid")
    return dict(value)


def read_bounded(directory, name):
    path = directory / name
    limit = FILE_LIMITS[name]
    require(not path.is_symlink() and path.is_file() and path.resolve() == path, "map.artifact_path")
    with path.open("rb") as stream:
        data = stream.read(limit+1)
    require(len(data) <= limit, "map.artifact_size")
    return data


def fingerprint(grid):
    header = [grid.frame, grid.width, grid.height, grid.resolution, grid.ox, grid.oy, grid.yaw]
    h = hashlib.sha256(json.dumps(header, separators=(",", ":"), allow_nan=False).encode())
    h.update(bytes(v+1 for v in grid.cells))
    return h.hexdigest()


def coordinates(x, y):
    require(math.isfinite(x) and math.isfinite(y), "map.origin_off_lattice")
    i, j = round(x), round(y)
    require(abs(x-i) <= CELL_EPSILON and abs(y-j) <= CELL_EPSILON, "map.origin_off_lattice")
    return i, j


def read_map(directory, *, manifest_sha256):
    """Open a pinned map for static display, independently of any live robot."""
    root = Path(directory).resolve(strict=True)
    require(isinstance(manifest_sha256,str), "map.manifest_digest")
    expected = manifest_sha256.removeprefix("sha256:")
    require(re.fullmatch("[0-9a-f]{64}",expected), "map.manifest_digest")
    raw = read_bounded(root,"manifest.json")
    require(hmac.compare_digest(hashlib.sha256(raw).hexdigest(),expected), "map.manifest_digest")
    manifest = json.loads(raw)
    require(isinstance(manifest,dict) and type(manifest.get("schema_version")) is int
            and manifest["schema_version"] == 1, "map.manifest_invalid")
    hashes = manifest.get("sha256")
    require(isinstance(hashes,dict) and set(hashes) == set(FILE_LIMITS)-{"manifest.json"}, "map.artifact_set")
    data = None
    for name in sorted(hashes):
        raw = read_bounded(root,name)
        require(hashes[name] == hashlib.sha256(raw).hexdigest(),"map.artifact_digest")
        if name == "occupancy.json":
            data = json.loads(raw)
    try:
        g = Grid(data)
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        raise ValueError("map.invalid_grid") from exc
    known = sum(v >= 0 for v in g.cells)
    require(all(manifest.get(key)==data[key] for key in ("frame","origin","resolution","width","height"))
            and manifest.get("known_cells")==known and manifest.get("total_cells")==len(g.cells),"map.manifest_geometry")
    meta = manifest.get("metadata")
    require(isinstance(meta,dict) and all(isinstance(meta.get(k),str) and meta[k].strip()
            for k in ("robot_id","calibration_id","state","reason","operation_id")) and type(meta.get("stamp_ns")) is int
            and 0<meta["stamp_ns"]<2**63, "map.metadata")
    return {"grid":data,"metadata":meta,"manifest_sha256":expected,"known_cells":known}


class GlobalMap:
    """Single ordered stream. A caller owning multiple streams must keep separate instances."""

    def __init__(self, identity):
        self._identity = scope(identity)
        self._anchor = None
        self._bounds = None
        self._frame = None
        self._resolution = None
        self._seen = {}
        self._fingerprint = None
        self.base_manifest_sha256 = None
        self._parent_manifest = None
        self._loaded_revision = None
        self._map_source = None
        self.last_stamp_ns = None
        self.revision = 0

    @property
    def identity(self):
        return dict(self._identity)

    def update(self, grid, *, stamp_ns, identity):
        require(scope(identity) == self._identity, "map.identity_mismatch")
        require(type(stamp_ns) is int and 0 < stamp_ns < 2**63, "map.stamp_invalid")
        if self.last_stamp_ns is not None:
            require(stamp_ns >= self.last_stamp_ns, "map.clock_rewind")
        require(isinstance(grid, dict), "map.invalid_grid")
        try:
            g = Grid(grid)
        except (KeyError, TypeError, OverflowError, ValueError) as exc:
            raise ValueError("map.invalid_grid: "+str(exc)) from exc
        digest = fingerprint(g)
        if stamp_ns == self.last_stamp_ns:
            require(digest == self._fingerprint, "map.stamp_conflict")
            return self._delta([],0)
        anchor = self._anchor
        ox, oy = 0, 0
        if anchor is None:
            anchor = (g.ox, g.oy, g.yaw)
        else:
            require(g.frame == self._frame, "map.frame_changed")
            require(math.isclose(g.resolution,self._resolution,abs_tol=0,rel_tol=1e-7), "map.resolution_changed")
            require(abs(math.remainder(g.yaw-anchor[2],math.tau)) <= 1e-7, "map.orientation_changed")
            dx, dy = g.ox-anchor[0], g.oy-anchor[1]
            c, s = math.cos(anchor[2]), math.sin(anchor[2])
            ox, oy = coordinates((dx*c+dy*s)/self._resolution,(-dx*s+dy*c)/self._resolution)
        require(max(math.ulp(g.ox),math.ulp(g.oy)) <= g.resolution*CELL_EPSILON, "map.origin_precision")
        x0, y0, x1, y1 = ox, oy, ox+g.width, oy+g.height
        if self._bounds is not None:
            ax, ay, bx, by = self._bounds
            x0, y0, x1, y1 = min(ax,x0), min(ay,y0), max(bx,x1), max(by,y1)
        w, h = x1-x0, y1-y0
        require(w<=MAX_SIDE and h<=MAX_SIDE and w*h<=MAX_CELLS, "map.capacity")
        # All validation is complete. Only cells observed in this window are updated.
        changed = []
        new_cells = 0
        for index, value in enumerate(g.cells):
            if value < 0:
                continue
            y, x = divmod(index,g.width)
            key = ox+x, oy+y
            if value != self._seen.get(key):
                new_cells += key not in self._seen
                self._seen[key] = value
                changed.append([key[0],key[1],value])
        if self._anchor is None:
            self._anchor, self._frame, self._resolution = anchor, g.frame, g.resolution
        self._bounds = x0,y0,x1,y1
        if self._loaded_revision == self.revision:
            self._parent_manifest = self.base_manifest_sha256
            self._loaded_revision = None
        self.last_stamp_ns = stamp_ns
        self.revision += 1
        self._fingerprint = digest
        return self._delta(changed,new_cells)

    def _delta(self, changes, new_cells):
        return {"stamp_ns":self.last_stamp_ns, "revision":self.revision, "frame":self._frame,
                "origin":list(self._anchor), "resolution":self._resolution, "bounds":list(self._bounds),
                "changes":changes, "known_cells":len(self._seen), "identity":self.identity,
                "new_cells":new_cells,"changed_cells":len(changes)-new_cells}

    def snapshot(self):
        require(self.revision > 0, "map.empty")
        xmin, ymin, xmax, ymax = self._bounds
        w, h = xmax-xmin, ymax-ymin
        cells = [-1]*(w*h)
        for (x,y), value in self._seen.items():
            cells[(y-ymin)*w+x-xmin] = value
        x, y, yaw = self._anchor
        c, s = math.cos(yaw), math.sin(yaw)
        res = self._resolution
        return {"frame":self._frame,"width":w,"height":h,"resolution":res,
                "origin":[x+res*(xmin*c-ymin*s),y+res*(xmin*s+ymin*c),yaw],"cells":cells}

    def _checkpoint_metadata(self):
        metadata = {"robot_id":self._identity["robot_id"], "calibration_id":self._identity["calibration_id"],
                    "state":"RECORDED", "reason":"observed_occupancy",
                    "stamp_ns":self.last_stamp_ns, "map_identity":self.identity,
                    "map_anchor":list(self._anchor), "map_bounds":list(self._bounds),
                    "map_revision":self.revision, "last_input_sha256":self._fingerprint}
        if self._parent_manifest is not None:
            metadata["base_manifest_sha256"] = self._parent_manifest
        if self._map_source is not None:
            metadata["map_source"] = dict(self._map_source)
        return metadata

    def save(self, destination, *, operation_id):
        grid = self.snapshot()
        return save_checkpoint(destination,grid,self._checkpoint_metadata(),operation_id)

    @classmethod
    def load_base(cls, directory, *, identity, manifest_sha256, allow_legacy=False):
        """Verify an explicitly pinned artifact, then restore its lattice and original clock fence."""
        obj = cls(identity)
        artifact = read_map(directory,manifest_sha256=manifest_sha256)
        meta,data,expected = artifact["metadata"],artifact["grid"],artifact["manifest_sha256"]
        require(meta["state"] in ("RECORDED","MAPPED"), "map.metadata")
        require(all(meta.get(k)==identity[k] for k in ("robot_id","calibration_id")), "map.identity_mismatch")
        native = "map_identity" in meta
        if native:
            require(scope(meta["map_identity"]) == obj._identity, "map.identity_mismatch")
            anchor = meta.get("map_anchor")
            require(isinstance(anchor,list) and len(anchor)==3 and all(type(v) in (int,float) and math.isfinite(v) for v in anchor),
                    "map.anchor_invalid")
            obj._anchor,obj._frame,obj._resolution = tuple(anchor),data["frame"],data["resolution"]
        else:
            require(allow_legacy is True and meta["state"]=="MAPPED","map.legacy_identity_approval")
        result = obj.update(data,stamp_ns=meta.get("stamp_ns"),identity=identity)
        if native:
            require(meta.get("map_bounds")==result["bounds"] and type(meta.get("map_revision")) is int
                    and meta["map_revision"]>0 and isinstance(meta.get("last_input_sha256"),str)
                    and re.fullmatch("[0-9a-f]{64}",meta["last_input_sha256"]), "map.history_invalid")
            obj.revision = meta["map_revision"]
            obj._fingerprint = meta["last_input_sha256"]
            obj._loaded_revision = obj.revision
            if "base_manifest_sha256" in meta:
                parent = meta["base_manifest_sha256"]
                require(isinstance(parent,str) and re.fullmatch("[0-9a-f]{64}",parent),"map.history_invalid")
                obj._parent_manifest = parent
        else:
            obj._parent_manifest = expected
        obj.base_manifest_sha256 = expected
        if "map_source" in meta:
            obj._map_source = valid_source(meta["map_source"])
        return obj


def save_checkpoint(destination,grid,meta,op):
    require(isinstance(op,str) and 1<=len(op)<=200 and op.isprintable() and op.strip()==op,"map.operation_invalid")
    return save_map(destination,grid,metadata={**meta,"operation_id":op})


def valid_source(source):
    require(isinstance(source,dict) and {"publisher_id","topic","fixed_frame"}<=set(source)
            <= {"publisher_id","topic","fixed_frame","map_load_time_ns"}, "map.source_invalid")
    for key in ("publisher_id","topic"):
        v = source[key]
        require(v is None or isinstance(v,str) and 0<len(v)<=256 and v.strip()==v and v.isprintable(),"map.source_invalid")
    require(type(source["fixed_frame"]) is bool, "map.source_invalid")
    load_time=source.get("map_load_time_ns")
    require(load_time is None or type(load_time) is int and 0<=load_time<2**63,"map.source_invalid")
    return {**source,"map_load_time_ns":load_time or None}


class MapHistory:
    """Per-recording segment history shared by live views and ordered archive playback."""

    MAX_SEGMENTS = 32
    MAX_CLOSED_CELLS = 3*MAX_CELLS

    def __init__(self, recording_id, *, base=None, base_publisher_id=None):
        require(isinstance(recording_id,str) and 0<len(recording_id)<=256 and recording_id.strip()==recording_id
                and recording_id.isprintable(),"map.recording_id")
        self.recording_id = recording_id
        self._local = "recording:"+hashlib.sha256(recording_id.encode()).hexdigest()
        require((base is None and base_publisher_id is None) or
                (isinstance(base,GlobalMap) and base.base_manifest_sha256 and isinstance(base_publisher_id,str)
                 and 0<len(base_publisher_id)<=256), "map.base_scope_mismatch")
        self.current = None
        self._base = deepcopy(base) if base is not None else None
        self._base_publisher = base_publisher_id
        self._closed = []
        self._closed_cells = 0
        self._seq = None
        self._event_id = None
        self._reset = False
        self._source = None
        self._scope = None
        self._start = None
        self._reason = None

    def _metadata(self,event):
        require(isinstance(event,dict) and event.get("kind")=="map","map.event_kind")
        if "recording_id" in event:
            require(event["recording_id"]==self.recording_id,"map.recording_mismatch")
        gid = event.get("publisher_gid")
        require(gid is None or event.get("publisher_id",gid)==gid,"map.publisher_mismatch")
        source = valid_source({k:event.get(k,d) for k,d in (("publisher_id",gid),("topic",None),
                                                             ("fixed_frame",False),("map_load_time_ns",None))})
        if "map_identity" in event:
            identity = scope(event["map_identity"])
            require(event.get("clock_domain",identity["clock_domain"])==identity["clock_domain"],"map.clock_domain_mismatch")
            return identity,source,"declared"
        identity = scope({"environment":"recording","robot_id":self._local,"map_id":self._local,"epoch":self._local,
                          "calibration_id":"unqualified","clock_domain":event.get("clock_domain",self._local)})
        return identity,source,"recording_local"

    def _info(self):
        area=self.current
        return {"segment":len(self._closed)+1,"reason":self._reason,"scope":self._scope,"identity":area.identity,
                **self._source,"first_stamp_ns":self._start,"last_stamp_ns":area.last_stamp_ns,
                "revision":area.revision,"frame":area._frame,"origin":list(area._anchor),"resolution":area._resolution,
                "bounds":list(area._bounds),"known_cells":len(area._seen)}

    def segments(self):
        prior = [deepcopy(info) for info,_,_,_ in self._closed]
        return prior + ([self._info()] if self.current is not None else [])

    def snapshot(self,segment=None):
        require(self.current is not None,"map.empty")
        if segment is None:
            return self.current.snapshot()
        require(type(segment) is int and 1<=segment<=len(self._closed)+1,"map.segment_unknown")
        if segment==len(self._closed)+1:
            return self.current.snapshot()
        _,geo,compact,_ = self._closed[segment-1]
        return {**deepcopy(geo),"cells":[v-1 for v in compact]}

    def save_segment(self, segment, destination, *, operation_id):
        grid=self.snapshot(segment)
        if segment is None or segment==len(self._closed)+1:
            meta=self.current._checkpoint_metadata()
        else:
            meta=self._closed[segment-1][3]
        return save_checkpoint(destination,grid,meta,operation_id)

    def _start_segment(self,area,reason,stamp,source,classification):
        if self.current is not None:
            bounds = self.current._bounds
            cells = (bounds[2]-bounds[0])*(bounds[3]-bounds[1])
            require(len(self._closed)+2<=self.MAX_SEGMENTS and self._closed_cells+cells<=self.MAX_CLOSED_CELLS,
                    "map.history_capacity")
            grid = self.current.snapshot()
            compact = bytes(v+1 for v in grid.pop("cells"))
            self._closed.append((self._info(),grid,compact,self.current._checkpoint_metadata()))
            self._closed_cells += cells
        self.current = area
        self._start,self._reason,self._scope,self._source = stamp,reason,classification,source

    def advance(self,event,grid):
        identity,source,classification = self._metadata(event)
        seq = event.get("seq")
        require(seq is None or type(seq) is int and seq>0,"map.sequence_invalid")
        require(seq is None or self._seq is None or seq>=self._seq,"map.sequence_rewind")
        stamp = event.get("stamp_ns")
        require(type(stamp) is int and 0<stamp<2**63,"map.stamp_invalid")
        require(isinstance(grid,dict) and isinstance(grid.get("cells"),(list,tuple)) and len(grid["cells"])<=MAX_CELLS,
                "map.invalid_grid")
        require(event.get("frame",grid.get("frame"))==grid.get("frame"),"map.event_frame_mismatch")
        known = sum(type(value) is int and value>=0 for value in grid["cells"])
        require("known" not in event or type(event["known"]) is int and event["known"]==known,"map.event_count_mismatch")
        require(type(event.get("map_reset",False)) is bool,"map.source_invalid")
        fixed = classification=="declared" and source["publisher_id"] is not None and source["fixed_frame"]
        area = self.current
        event_id=event.get("event_id")
        require(event_id is None or isinstance(event_id,str) and 0<len(event_id)<=256,"map.event_id")
        reason = None
        if area is None:
            reason = "first_observation"
        elif seq is not None and seq==self._seq or event_id is not None and event_id==self._event_id:
            require(identity==area.identity and source==self._source and stamp==area.last_stamp_ns and
                    seq==self._seq and event_id==self._event_id and event.get("map_reset",False)==self._reset,"map.sequence_conflict")
            # A matching sequence number is a retry, never a new reset.
        elif identity!=area.identity:
            reason = "identity_changed"
        elif source!=self._source:
            reason = "source_changed"
        elif stamp<area.last_stamp_ns:
            reason = "clock_rewind"
        elif event.get("map_reset"):
            reason = "explicit_reset"
        elif stamp>area.last_stamp_ns and not known and area._seen and not fixed:
            reason = "all_unknown_reset"

        if reason is None:
            try:
                delta = area.update(grid,stamp_ns=stamp,identity=identity)
            except ValueError as error:
                if str(error) not in ("map.frame_changed","map.resolution_changed","map.orientation_changed","map.origin_off_lattice"):
                    raise
                reason = str(error).split(".",1)[1]
        if reason is not None:
            if self._base is not None:
                area = self._base
                require(fixed and source["publisher_id"]==self._base_publisher and identity==area.identity
                        and (area._map_source is None or area._map_source==source)
                        and not event.get("map_reset"),"map.base_scope_mismatch")
                reason = "approved_base"
            else:
                area = GlobalMap(identity)
            delta = area.update(grid,stamp_ns=stamp,identity=identity)
            self._start_segment(area,reason,stamp,source,classification)
            self._base = None
        area._map_source = source
        if seq is not None:
            self._seq = seq
        self._event_id,self._reset = event_id,event.get("map_reset",False)
        return {**delta,"segment":len(self._closed)+1,"segment_started":reason is not None,"reason":reason,
                "scope":classification,"publisher_id":source["publisher_id"],"observed_known":known}
