#pragma once

namespace edge {
// SQLite runs these triggers in the transaction that changed the mission. Observers
// can disconnect without losing intermediate transitions. cause_seq links the
// preceding committed event of the same mission for chronological reconstruction.
inline constexpr char audit_schema[] = R"sql(
CREATE TABLE IF NOT EXISTS audit_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL
);
INSERT OR IGNORE INTO audit_identity VALUES(1,lower(hex(randomblob(16))));
CREATE TABLE IF NOT EXISTS mission_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    mission_id TEXT NOT NULL,
    operation_id TEXT,
    kind TEXT NOT NULL,
    prior_state TEXT,
    state TEXT,
    detail TEXT NOT NULL,
    cause_seq INTEGER
);
CREATE INDEX IF NOT EXISTS mission_events_by_mission ON mission_events(mission_id,seq);
CREATE TRIGGER IF NOT EXISTS audit_mission_created AFTER INSERT ON missions BEGIN
    INSERT INTO mission_events (mission_id,kind,state,detail)
    VALUES (NEW.mission_id,'mission.created',NEW.state,NEW.detail);
END;
CREATE TRIGGER IF NOT EXISTS audit_mission_changed AFTER UPDATE OF state,detail ON missions
WHEN NEW.state IS NOT OLD.state OR NEW.detail IS NOT OLD.detail BEGIN
    INSERT INTO mission_events (mission_id,operation_id,kind,prior_state,state,detail,cause_seq)
    VALUES (NEW.mission_id,
        (SELECT operation_id FROM steps WHERE mission_id=NEW.mission_id AND operation_id IS NOT NULL ORDER BY step DESC LIMIT 1),
        CASE WHEN NEW.state IS OLD.state THEN 'mission.detail' ELSE 'mission.transition' END,
        OLD.state,NEW.state,NEW.detail,(SELECT max(seq) FROM mission_events WHERE mission_id=NEW.mission_id));
END;
CREATE TRIGGER IF NOT EXISTS audit_step_dispatch AFTER UPDATE OF operation_id ON steps
WHEN NEW.operation_id IS NOT OLD.operation_id BEGIN
    INSERT INTO mission_events (mission_id,operation_id,kind,detail,cause_seq)
    VALUES (NEW.mission_id,NEW.operation_id,'step.dispatch',json_object('step',NEW.step,'target_x',NEW.x,'target_y',NEW.y),
        (SELECT max(seq) FROM mission_events WHERE mission_id=NEW.mission_id));
END;
CREATE TRIGGER IF NOT EXISTS audit_step_result AFTER UPDATE OF action_result ON steps
WHEN NEW.action_result IS NOT OLD.action_result BEGIN
    INSERT INTO mission_events (mission_id,operation_id,kind,detail,cause_seq)
    VALUES (NEW.mission_id,NEW.operation_id,'step.result',NEW.action_result,
        (SELECT max(seq) FROM mission_events WHERE mission_id=NEW.mission_id));
END;
CREATE TRIGGER IF NOT EXISTS audit_step_verified AFTER UPDATE OF odom_stamp_ns ON steps
WHEN NEW.odom_stamp_ns IS NOT OLD.odom_stamp_ns BEGIN
    INSERT INTO mission_events (mission_id,operation_id,kind,detail,cause_seq)
    VALUES (NEW.mission_id,NEW.operation_id,'step.verified',
        json_object('step',NEW.step,'observed_x',NEW.evidence_x,'observed_y',NEW.evidence_y,'odom_stamp_ns',NEW.odom_stamp_ns),
        (SELECT max(seq) FROM mission_events WHERE mission_id=NEW.mission_id));
END;
)sql";
}
