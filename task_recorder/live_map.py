"""Read-only, bounded projection of committed map events from one recorder archive."""
from collections import OrderedDict

import json
import sqlite3
from threading import Lock

from task_recorder.global_map import MapHistory, decode_observation


class MapCursor:
    def __init__(self, recording_id):
        self.history = MapHistory(recording_id)
        self.after = 0
        self.accepted_seq = None
        self.received_ns = None
        self.fault_count = 0
        self.last_fault = None
        self.delta = {}


class LiveMaps:
    """The archive remains authoritative after eviction or service restart."""

    def __init__(self, database, *, batch=8, max_sessions=3):
        self.uri = database.resolve().as_uri() + "?mode=ro"
        self.cursors = OrderedDict()
        self.lock = Lock()
        self.batch = batch
        self.max_sessions = max_sessions

    @staticmethod
    def consume(cursor, row):
        seq, received, body, blob = row
        try:
            event = json.loads(body)
            grid = decode_observation(event,blob)
            delta = cursor.history.advance(dict(event, seq=seq, received_ns=received), grid)
        except (UnicodeError, ValueError, RecursionError) as exc:
            cursor.fault_count += 1
            cursor.last_fault = {"seq": seq, "detail": str(exc)[:250]}
        else:
            cursor.delta = {k: delta[k] for k in ("segment", "revision", "known_cells", "new_cells",
                             "changed_cells", "frame", "scope", "reason") if k in delta}
            cursor.accepted_seq = seq
            cursor.received_ns = str(received)
        cursor.after = seq

    def view(self, recording_id, segment=None, *, status, previous_etag=None):
        with self.lock:
            cursor = self.cursors.pop(recording_id, None)
            if cursor is None:
                if len(self.cursors) >= self.max_sessions:
                    self.cursors.popitem(last=False)
                cursor = MapCursor(recording_id)
            self.cursors[recording_id] = cursor
            db = sqlite3.connect(self.uri, uri=True)
            try:
                head = db.execute("SELECT max(seq) FROM events WHERE rid=? AND kind='map'",
                                  (recording_id,)).fetchone()[0] or 0
                # Stream one attachment at a time, apart from the recorder's writer connection.
                rows = db.execute("SELECT seq,rx,body,blob FROM events WHERE rid=? AND kind='map' "
                                  "AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                                  (recording_id, cursor.after, head, self.batch))
                for row in rows:
                    self.consume(cursor, row)
            finally:
                db.close()
            segments = cursor.history.segments()
            if segment is not None and not 1 <= segment <= len(segments):
                raise ValueError("unknown map segment")
            selected = segment or len(segments)
            etag = f'"{recording_id}.{status}.{head}.{cursor.after}.{selected}"'
            if previous_etag == etag:
                return etag, None
            snapshot = cursor.history.snapshot(selected) if selected else None
            return etag, dict(schema="task.record.global-map.v1", cursor=cursor.after, observed_head=head, catching_up=cursor.after<head,
                        accepted_seq=cursor.accepted_seq, received_ns=cursor.received_ns,
                        latest=dict(cursor.delta), selected_segment=selected, segments=segments, grid=snapshot,
                        faults=cursor.fault_count, last_fault=cursor.last_fault)
