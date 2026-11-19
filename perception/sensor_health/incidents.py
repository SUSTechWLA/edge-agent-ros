"""One transaction couples a public transition to its private open-incident pointer."""
import json


def open_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS sensor_health_open(
        input TEXT PRIMARY KEY, identity TEXT NOT NULL, event_id TEXT NOT NULL,
        phase TEXT NOT NULL, code TEXT NOT NULL, frame TEXT NOT NULL, stamp_ns INTEGER NOT NULL)""")


def scope(row):
    return json.dumps(row["identity"],sort_keys=True,separators=(",",":"))


def remember(db,key,row):
    db.execute("""INSERT OR REPLACE INTO sensor_health_open(input,identity,event_id,phase,code,frame,stamp_ns)
        VALUES(?,?,?,?,?,?,?)""",(key,scope(row),row["event_id"],row["state"],row["code"],
        row["latest"].get("frame_id") or row.get("prior_frame",""),row["last_stamp"]))


def forget(db,key):
    db.execute("DELETE FROM sensor_health_open WHERE input=?",(key,))


def boundary(journal,key,parent,kind,detail,data):
    return journal.emit({"module":"sensor.health","layer":"perception","kind":"sensor."+kind,"code":"sensor."+kind,
        "level":"warn","trace_id":"sensor.health."+key,"parent_event_id":parent,"detail":detail,"data":data})


def restore(journal,states,now,boot_event):
    db=journal.db
    open_table(db)
    entries=db.execute("SELECT input,identity,event_id,phase,code,frame,stamp_ns FROM sensor_health_open").fetchall()
    for key,identity,event,phase,code,frame,stamp in entries:
        row=states.get(key)
        previous=json.loads(identity)
        data={"input":key,"prior_identity":previous,"prior_frame":frame,"prior_stamp_ns":stamp,
              "prior_phase":phase,"prior_code":code,"boot_event_id":boot_event,"observation_gap":"unobserved"}
        if row is None or identity!=scope(row):
            data["current_identity"]=row["identity"] if row else None
            boundary(journal,key,event,"source_detached","来源配置改变或移除，旧事故止于最后一次已知观测。",data)
            forget(db,key)
            continue
        row.update(state=phase,code=code,since=now,prior_frame=frame,last_stamp=stamp)
        row["event_id"]=boundary(journal,key,event,"incident_resumed","进程重启后续查，重新计时并从零采集恢复样本。",data)
        remember(db,key,row)
