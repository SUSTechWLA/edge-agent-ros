from functools import partial
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
import uuid

from calibration.lingao_attended_io import LingaoAttendedIO, PROFILE, SPEED, Window


class IsolatedRobot:
    def __init__(self,enabled,path):
        self.path=Path(path)

    def update(self):
        pass

    def status(self,_):
        return {"eligible":True,"detail":"isolated","observed_monotonic":time.monotonic(),
                "odom":None,"profiles":[PROFILE]}

    def begin(self):
        pass

    def write(self,v):
        with self.path.open("a",encoding="utf8") as f:
            f.write(json.dumps([time.monotonic(),v])+"\n")

    def close(self):
        pass


class IsolatedIO(LingaoAttendedIO):
    evidence_scope="isolated"

    def __init__(self,path):
        super().__init__()
        self._backend=partial(IsolatedRobot,path=str(path))


def held_client(path,pipe):
    io=IsolatedIO(path)
    io.start()
    try:
        until=time.monotonic()+3.
        while not io.inspect()["eligible"] and time.monotonic()<until:
            time.sleep(.01)
        op=str(uuid.uuid4())
        deadline=time.monotonic()+.1
        pipe.send((deadline, io.begin(op,deadline) and io.forward(op,1,.1)))
        if pipe.poll(4):
            pipe.recv()
        pipe.send((io.stop(op,3),io.forward(op,4,.1)))
    finally:
        io.close()


class AttendedIOTest(unittest.TestCase):
    def setUp(self):
        self.id=str(uuid.uuid4())

    def test_early_cancellation_disallows_late_begin(self):
        g=Window(lambda _:self.fail("unexpected output"),lambda:0.)
        self.assertFalse(g.request(dict(method="stop",op=self.id,seq=2),True))
        self.assertFalse(g.request(dict(method="begin",op=self.id,deadline=.4),True))

    def test_expiry_refuses_late_forward_and_prevents_reopening(self):
        at=[10.];events=[];g=Window(events.append,lambda:at[0]);op=self.id
        self.assertTrue(g.request(dict(method="begin",op=op,deadline=10.1),True))
        self.assertTrue(g.request(dict(method="forward",op=op,vx=.1,seq=3),True))
        self.assertFalse(g.request(dict(method="forward",op=op,vx=.1,seq=2),True))
        at[0]=10.1
        self.assertFalse(g.request(dict(method="forward",op=op,vx=.1,seq=4),True))
        self.assertFalse(g.request(dict(method="begin",op=op,deadline=10.4),True))
        self.assertEqual(events,[.1,0.])

    def test_hold_renewal_does_not_extend_half_second(self):
        at=[0.];events=[];g=Window(events.append,lambda:at[0])
        self.assertTrue(g.request(dict(method="begin",op=self.id,deadline=.5),True))
        for i in range(10):
            at[0]=i*.05
            self.assertTrue(g.request(dict(method="forward",op=self.id,seq=i+1,vx=.1),True))
        at[0]=.5
        g.tick(True)
        self.assertEqual(g.operation,None)
        self.assertEqual(events,[.1]*10+[0.])
        self.assertTrue(g.request(dict(method="stop",op=self.id,seq=11),True))

    def test_takeover_and_invalid_commands(self):
        events=[];g=Window(events.append,lambda:1.)
        self.assertFalse(g.request(dict(method="begin",op=self.id,deadline=2.),True))
        for value in (.02,-.1,False):
            op=str(uuid.uuid4());self.assertTrue(g.request(dict(method="begin",op=op,deadline=1.4),True))
            self.assertFalse(g.request(dict(method="forward",op=op,seq=1,vx=value),True))
        op=str(uuid.uuid4());self.assertTrue(g.request(dict(method="begin",op=op,deadline=1.4),True))
        self.assertTrue(g.request(dict(method="forward",op=op,seq=1,vx=.1),True))
        g.tick(False)
        self.assertFalse(g.request(dict(method="forward",op=op,seq=2,vx=.1),True))
        self.assertEqual(events,[.1,0.])

    def test_owner_stops_without_browser_callbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            logfile=Path(tmp)/"isolated-events.jsonl"
            io=IsolatedIO(logfile)
            self.assertFalse(io.inspect()["eligible"])
            io.start()
            try:
                until=time.monotonic()+4
                while not io.inspect()["eligible"] and time.monotonic()<until:time.sleep(.025)
                self.assertEqual(io.inspect()["evidence_scope"],"isolated")
                self.assertFalse(logfile.exists())
                deadline=time.monotonic()+.095
                self.assertTrue(io.begin(self.id,deadline))
                self.assertTrue(io.forward(self.id,7,SPEED))
                time.sleep(.2)
                self.assertFalse(io.forward(self.id,8,SPEED))
                events=[json.loads(l) for l in logfile.read_text().splitlines()]
                self.assertEqual([v for _,v in events],[.1,0.])
                self.assertLess(events[-1][0],deadline+.035)
            finally:
                io.close()

    @unittest.skipUnless(hasattr(signal,"SIGSTOP"), "requires process suspension on the Jetson")
    def test_frozen_operator_process_cannot_extend_or_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"isolated-events.jsonl"
            ctx=multiprocessing.get_context("spawn")
            parent,child=ctx.Pipe()
            caller=ctx.Process(target=held_client,args=(path,child))
            caller.start();child.close()
            frozen=False
            try:
                self.assertTrue(parent.poll(4))
                deadline,accepted=parent.recv()
                self.assertTrue(accepted)
                os.kill(caller.pid,signal.SIGSTOP)
                frozen=True
                time.sleep(.21)
                events=[json.loads(l) for l in path.read_text().splitlines()]
                self.assertEqual([v for _,v in events],[.1,0.])
                self.assertLess(events[1][0],deadline+.035)
                os.kill(caller.pid,signal.SIGCONT);frozen=False
                parent.send("release")
                self.assertTrue(parent.poll(3))
                self.assertEqual(parent.recv(),(True,False))
                caller.join(2)
                self.assertEqual(caller.exitcode,0)
                print(json.dumps({"case":"isolated_operator_SIGSTOP","scope":"isolated","deadline_monotonic":deadline,
                                  "accepted_outputs":events,"stop_ack_after_expiry":True,"late_nonzero_accepted":False}))
            finally:
                if frozen:
                    os.kill(caller.pid,signal.SIGCONT)
                if caller.is_alive():
                    caller.terminate();caller.join(2)
                parent.close()
