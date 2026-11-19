"""Read-only interpretation of recorded signals and event identities for showcase playback."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
import math
import re


DEFAULTS = (
    ("laser_scan", "metrics.nearest_m", "激光最近回波", "m", .65),
    ("imu", "metrics.angular_z_rad_s", "IMU 实测角速度", "rad/s", .5),
    ("twist", "linear", "下发线速度", "m/s", .65),
    ("fused_pose", "xy.0", "融合位置 X", "m", .7),
    ("map", "known", "地图已知格", "格", 1.5),
)
KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
STATUS = {"unselected":"未选录","waiting":"等首样","invalid":"无有效值","stale":"已超时","sample":"已采样"}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def formatted(value):
    if type(value) is int:
        return f"{value:,}"
    if value and abs(value)<.0001:
        return "≈0"
    if abs(value)<10000:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:,.0f}"


def channels(guide, sources):
    config = guide.get("showcase", {})
    if not isinstance(config, dict) or set(config) - {"signals"}:
        raise ValueError("showcase supports only signals")
    signals = config.get("signals")
    if signals is None:
        return [dict(source=k, field=f, label=l, unit=u, max_age_s=a)
                for k,f,l,u,a in DEFAULTS if k in sources][:3]
    if not isinstance(signals, list) or len(signals)>3:
        raise ValueError("showcase signals must be an array of up to three channels")
    for s in signals:
        if not isinstance(s, dict) or set(s)!={"source","field","label","unit","max_age_s"}:
            raise ValueError("showcase signal requires source, field, label, unit, max_age_s")
        if (not isinstance(s["source"],str) or not KEY.fullmatch(s["source"]) or
                not isinstance(s["field"],str) or not 1<=len(s["field"])<=128 or
                any(not (KEY.fullmatch(p) or p.isascii() and p.isdigit() and len(p)<=3) for p in s["field"].split(".")) or
                not isinstance(s["label"],str) or not 1<=len(s["label"])<=32 or
                not isinstance(s["unit"],str) or len(s["unit"])>16 or
                not finite(s["max_age_s"]) or not .01<=s["max_age_s"]<=3600):
            raise ValueError("invalid showcase source, field, label, unit or freshness threshold")
    return signals


def sample_value(row, field):
    if row.get("valid") is False:
        return None
    value = row
    for part in field.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value,list) and part.isascii() and part.isdigit() and int(part)<len(value):
            value = value[int(part)]
        else:
            return None
    return value if finite(value) else None


def identity(row):
    body = row.get("event") if row.get("kind")=="bus" else row
    body = body if isinstance(body,dict) else {}
    return {k:body[k] for k in ("source_event_id","event_id","parent_event_id","operation_id","mission_id",
                                "source_seq","phase","code","module","tool") if isinstance(body.get(k),(str,int))}


def ref(row):
    return {k:row[k] for k in ("kind","seq","event_id","stamp_ns","received_ns","topic","frame_id",
                               "header_stamp_ns","stamp_origin","valid","reason") if k in row}


class Showcase:
    def __init__(self, record, guide, axis):
        self.record = record
        self.axis = axis
        self.sources = set(record.header["sources"])
        if record.header.get("status")=="ACTIVE":
            raise ValueError("showcase requires a closed recording")
        if axis=="stamp_ns":
            domains = set()
            timing = record.header.get("stream_timing",{})
            by_source = {}
            for row in record.all:
                domain=row.get("clock_domain")
                if not isinstance(domain,str) or not domain.strip():
                    raise ValueError("missing source clock domain; choose --axis received_ns")
                domains.add(domain)
                previous = by_source.get(row["kind"])
                if previous is not None and row["stamp_ns"] < previous:
                    raise ValueError("source clock rewound; choose --axis received_ns")
                by_source[row["kind"]]=row["stamp_ns"]
            if (len(domains)!=1 or any(v.get("source_rewinds",0) for v in timing.values()) or
                    any(row.get("stamp_origin")=="receipt" for row in record.all)):
                raise ValueError("different or unknown source clocks; choose --axis received_ns")
        self.signals = channels(guide,self.sources)
        sensor_kinds = {s["source"] for s in self.signals}|{"imu","laser_scan"}
        self.events = [e for e in record.marks if e["kind"] not in sensor_kinds and "metrics" not in e] or list(record.by.get("bus",[]))
        self.event_times = [e["_at"] for e in self.events]
        links = defaultdict(list)
        for row in sorted((r for r in record.all if r["kind"] in ("bus","agent_trace")),key=lambda r:(r["_at"],r["seq"])):
            ids = identity(row)
            links["recent",""].append(row)
            for key in ("event_id","source_event_id","operation_id","mission_id"):
                value = ids.get(key)
                if value and value!=record.header["task_id"]:
                    links[key,value].append(row)
        self.links = {k:([r["_at"] for r in rows],rows) for k,rows in links.items()}

    def activity(self, stamp):
        return self.events[:bisect_right(self.event_times,stamp)]

    def related(self, current, stamp):
        if not current:
            return None
        ids = identity(current)
        parent = ids.get("parent_event_id")
        keys = [("source_event_id",parent,"parent"),("event_id",parent,"parent"),
                ("operation_id",ids.get("operation_id"),"operation"),("mission_id",ids.get("mission_id"),"mission"),
                ("recent","","timeline")]
        for key,value,relation in keys:
            if (key,value) not in self.links:
                continue
            times,rows=self.links[key,value]
            end=bisect_right(times,stamp)
            for index in range(end-1,max(-1,end-3),-1):
                other=rows[index]
                if other["event_id"]==current["event_id"]:
                    continue
                return dict(relation=relation, evidence=ref(other), source=identity(other))
        return None

    def samples(self, stamp, signal):
        source=signal["source"]
        rows=self.record.by.get(source,[])
        times=self.record.times.get(source,[])
        end=bisect_right(times,stamp)
        if not end:
            return dict(status="waiting" if source in self.sources else "unselected",value=None), []
        row=rows[end-1]
        age=(stamp-row["_at"])/1e9
        value=sample_value(row,signal["field"])
        status="invalid" if value is None else "stale" if age>signal["max_age_s"] else "sample"
        first=bisect_left(times,stamp-4_000_000_000,0,end)
        points=[(r["_at"],sample_value(r,signal["field"])) for r in rows[first:end]]
        return dict(status=status,value=value,age_s=age,evidence=ref(row)),points

    def chapter(self, row):
        return dict(event=ref(row),identity=identity(row),related=self.related(row,row["_at"]),
                    samples=[dict(signal=s["source"]+"."+s["field"],**self.samples(row["_at"],s)[0]) for s in self.signals])
