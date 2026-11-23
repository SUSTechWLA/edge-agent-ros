"""Only observable translation axes may correct wheel odometry."""
import math
import unittest

from campus_scan_translation import Scan, estimate


def scan_at(x,y,scene):
    ranges=[]
    step=math.tau/360
    for i in range(360):
        angle=-math.pi+i*step
        u,v=math.cos(angle),math.sin(angle)
        hits=[]
        if scene in ("room","wall") and v>.001:
            hits.append((2-y)/v)
        if scene=="room":
            if v<-.001: hits.append((-2-y)/v)
            if u>.001: hits.append((3-x)/u)
            if u<-.001: hits.append((-3-x)/u)
        ranges.append(min(hits,default=math.inf))
    return Scan(ranges,-math.pi,step,.1,8.,0.)


class TranslationTest(unittest.TestCase):
    guess=(.012,.002)

    def measured(self,scene,dt=.125):
        return estimate(scan_at(0.,0.,scene),scan_at(.020,.018,scene),self.guess,0.,dt)

    def test_room_has_two_observable_axes(self):
        displacement,mode=self.measured("room")
        self.assertEqual(mode,"matched")
        self.assertLess(math.dist(displacement,(.020,.018)),.001)

    def test_wall_corrects_normal_only(self):
        (x,y),mode=self.measured("wall")
        self.assertEqual(mode,"one_axis")
        self.assertAlmostEqual(x,self.guess[0])
        self.assertAlmostEqual(y,.018)

    def test_missing_points_and_bad_interval(self):
        self.assertEqual(self.measured("unknown"),(self.guess,"degenerate"))
        self.assertEqual(self.measured("room",2.),(self.guess,"invalid_interval"))

    def test_impossible_scan_jump_rejected(self):
        old=scan_at(0.,0.,"room")
        new=scan_at(.08,0.,"room")
        self.assertEqual(estimate(old,new,self.guess,0.,.125),(self.guess,"innovation_rejected"))


if __name__=="__main__":
    unittest.main()
