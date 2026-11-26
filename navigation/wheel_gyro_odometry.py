"""Integrate wheel forward speed with a body-frame yaw gyro on the acquisition clock."""
from collections import deque
import math

from navigation.planar_pose import wrap_yaw


class WheelGyroOdometry:
    def __init__(self, on_sample, on_reset):
        self.emit = on_sample
        self.on_reset = on_reset
        self.epoch = 0
        self.reset()

    def reset(self):
        self.epoch += 1
        self.gyro = deque()
        self.wheels = deque()
        self.previous = None
        self.pose = (0.,0.,0.)
        self.on_reset()

    def push(self, source, stamp_ns, value):
        if not math.isfinite(value) or stamp_ns <= 0:
            self.reset()
            return
        stream = self.gyro if source=="gyro" else self.wheels
        previous = stream[-1] if stream else (self.previous if source=="wheel" else None)
        if previous and stamp_ns == previous[0]:
            return
        if previous and (stamp_ns < previous[0] or stamp_ns-previous[0] > 300_000_000):
            self.reset()
            stream = self.gyro if source=="gyro" else self.wheels
        stream.append((stamp_ns,float(value)))
        if len(self.gyro)>400 or len(self.wheels)>120:
            self.reset()
            return
        while self.wheels and self.gyro and self.wheels[0][0] <= self.gyro[-1][0]:
            wheel = self.wheels.popleft()
            if wheel[0] < self.gyro[0][0]:
                continue
            if self.previous is not None:
                start,v0 = self.previous
                end,v1 = wheel
                if start < self.gyro[0][0]:
                    self.reset()
                    return
                total = 0.
                for a,b in zip(self.gyro,list(self.gyro)[1:]):
                    lo,hi = max(start,a[0]),min(end,b[0])
                    if hi <= lo:
                        continue
                    slope = (b[1]-a[1])/(b[0]-a[0])
                    total += (a[1]+slope*((lo-a[0])+(hi-a[0]))*.5)*(hi-lo)*1e-9
                distance = .5*(v0+v1)*(end-start)*1e-9
                x,y,yaw=self.pose
                self.pose = (x+distance*math.cos(yaw+total*.5),y+distance*math.sin(yaw+total*.5),wrap_yaw(yaw+total))
            self.previous=wheel
            self.emit(wheel[0],self.pose)
            while len(self.gyro)>1 and self.gyro[1][0]<=wheel[0]:
                self.gyro.popleft()
