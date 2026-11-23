#include "local_planner.hpp"
#include "dynamic_obstacles.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <iostream>
#include <limits>
#include <numbers>
#include <stdexcept>

using namespace edge::local;
namespace {
constexpr double pi=std::numbers::pi;
struct Box { double xmin,ymin,xmax,ymax; };
struct Pose { double x{},y{},yaw{}; };
void check(bool pass,const char* message) {
  if (!pass) throw std::runtime_error(message);
}
void near(double actual,double expected,double tol,const char* message) {
  check(std::isfinite(actual) && std::abs(actual-expected)<=tol,message);
}
double first_hit(double x,double y,double dx,double dy,const Box& b) {
  double enter=0, leave=8.;
  auto slab=[&](double start,double direction,double low,double high) {
    if (std::abs(direction)<1e-12) return low<=start && start<=high;
    const double a=(low-start)/direction, c=(high-start)/direction;
    enter=std::max(enter,std::min(a,c)); leave=std::min(leave,std::max(a,c));
    return enter<=leave;
  };
  return slab(x,dx,b.xmin,b.xmax) && slab(y,dy,b.ymin,b.ymax) ? enter : INFINITY;
}
struct Sensor {
  std::array<float,360> rays{};
  ScanFrame scan;
  Sensor() {
    scan.source_id="sim.scan"; scan.frame_id="laser_link"; scan.calibration_ref="sim.laser.r1";
    scan.sensor_x_m=.08; scan.angle_min=-pi; scan.angle_increment=pi/180;
    scan.angle_max=scan.angle_min+359*scan.angle_increment;
    scan.range_min=.1; scan.range_max=8.; scan.ranges=rays;
    scan.range_resolution_m=.01;
  }
  void sample(Pose p,const std::vector<Box>& boxes) {
    ++scan.seq; scan.stamp_ns+=125000000;
    const double x=p.x+.08*std::cos(p.yaw), y=p.y+.08*std::sin(p.yaw);
    for (std::size_t i=0;i<rays.size();++i) {
      const double heading=p.yaw+scan.angle_min+i*scan.angle_increment;
      double d=INFINITY;
      for (const auto& b:boxes)
        d=std::min(d,first_hit(x,y,std::cos(heading),std::sin(heading),b));
      rays[i]=static_cast<float>(std::round(d*100)/100);
    }
  }
};
FrontObstacleReport steady(Sensor& s,const std::vector<Box>& boxes) {
  s.sample({},boxes); const auto first=assess(s.scan);
  s.sample({},boxes); return assess(s.scan,&first);
}
void observations() {
  Sensor s; constexpr Box box{1.65,-.25,2.15,.25};
  s.sample({}, {box}); const auto first=assess(s.scan);
  check(first.valid && first.swept_coverage && first.stamp_ns==s.scan.stamp_ns &&
        first.scan_seq==s.scan.seq && first.source_id==s.scan.source_id &&
        first.calibration_ref==s.scan.calibration_ref && first.frame_id=="base_link","source or coverage");
  near(first.nearest_range_m.value(),1.65,.01,"base-frame nearest surface");
  near(first.nearest_bearing_rad.value(),0,2*pi/180,"surface bearing");
  near(first.nearest_width_m.value(),.5,.06,"observed box width");
  check(first.ttc_status=="unobserved" && !first.ttc_s,"single-frame TTC");
  s.sample({.025,0,0},{box}); const auto approaching=assess(s.scan,&first);
  check(approaching.report_ref!=first.report_ref && approaching.ttc_status=="closing" &&
        approaching.ttc_s.value()>3 && approaching.closing_speed_mps.value()>.12,"approaching surface TTC");
  check(!assess(s.scan,&approaching).valid,"duplicate source sequence");
  s.scan.clock_epoch++; check(assess(s.scan,&approaching).valid,"new clock epoch");
  s.scan.sensor_x_m=NAN; check(!assess(s.scan).valid,"unknown extrinsics");
  Sensor clear; auto free=steady(clear,{});
  check(free.valid && !free.nearest_range_m && free.ttc_status=="no_front_return" &&
        free.left_gap_m.value()>1 && free.right_gap_m.value()>1,"no-return range or width");
  clear.rays[15]=NAN; auto blind=assess(clear.scan);
  check(blind.valid && !blind.swept_coverage && !blind.left_gap_m,"unknown rear coverage");
  clear.rays.fill(NAN); check(!assess(clear.scan).valid,"unknown front");
}
void choices() {
  Sensor s;
  auto free=steady(s,{});
  GapFollow gap;
  const PlanInput goal{3.8,0.};
  const auto straight=gap.plan(goal,&free);
  check(straight.decision=="proceed" && straight.linear_mps>0 && straight.linear_mps<=.32 &&
        straight.report_ref==free.report_ref,"clear goal");
  for (int side:{1,-1}) {
    gap.reset();
    auto obstacle=steady(s,{{1.45,.32*side-.25,1.95,.32*side+.25}});
    auto plan=gap.plan(goal,&obstacle);
    check(plan.decision==(side==1 ? "avoid_right" : "avoid_left"),"offset side");
    check(plan.clearance_m.value()>=.55 && std::abs(plan.angular_radps)<=1.1,"planning margin or angular limit");
  }
  gap.reset(); auto central=steady(s,{{1.65,-.25,2.15,.25}});
  const auto choice=gap.plan(goal,&central).decision;
  check(choice=="avoid_left" || choice=="avoid_right","symmetric centered obstacle");
  check(gap.plan(goal,&central).decision==choice,"stable tie");
  // Close the previously selected side while leaving a safe path around the other side.
  const Box wall=choice=="avoid_left" ? Box{-3,.98,4,1.1} : Box{-3,-1.1,4,-.98};
  auto changed=steady(s,{{1.65,-.25,2.15,.25},wall});
  check(gap.plan(goal,&changed).decision==(choice=="avoid_left" ? "avoid_right" : "avoid_left"),
        "alternate safe corridor after preferred side closes");
  check(gap.plan(goal,&free).decision=="proceed","obstacle removed");
  auto narrow=steady(s,{{.7,.4,4,4},{.7,-4,4,-.4},{-.9,.4,.7,4},{-.9,-4,.7,-.4}});
  check(gap.plan(goal,&narrow).reason_code=="navigation.no_safe_path","narrow gap");
  // A parallel wall is within the preferred reserve but leaves over 0.55 m
  // of body clearance throughout. Progress must not require more clearance
  // at the start than the robot actually has.
  gap.reset(); auto near_wall=steady(s,{{-4,.89,4,1.0}});
  const auto alongside=gap.plan(goal,&near_wall);
  check((alongside.decision=="proceed" || alongside.decision=="avoid_right") && alongside.clearance_m.value()>=.55 &&
        alongside.clearance_m.value()<.595,"maintain safe clearance inside preferred reserve");
  auto too_close=steady(s,{{-4,.84,4,1.0}});
  check(gap.plan(goal,&too_close).reason_code=="navigation.no_safe_path","never relax mandatory margin");
  check(gap.plan({3,0,false,true},&free).reason_code=="navigation.scan_stale","stale scan");
  check(gap.plan({3,0,true,false},&free).reason_code=="navigation.odom_timeout","stale odometry");
  central.ttc_s=.2; check(gap.plan(goal,&central).reason_code=="navigation.ttc_insufficient","unsafe TTC");
  central.ttc_s.reset(); central.ttc_status="unobserved";
  check(gap.plan(goal,&central).decision=="wait","unknown TTC");
  StopOnClearance legacy;
  near(legacy.plan(goal,nullptr).linear_mps,.32,1e-9,"legacy speed");
  near(legacy.plan({3,.4},nullptr).linear_mps,0.,1e-9,"legacy turn");
}

void competing_surfaces() {
  // One isolated return lies within a calibrated range bin of a visible wall.
  // Mirror the geometry to test scan-order independence.
  Sensor s;
  s.scan.sensor_x_m=0.;
  for (int sign:{1,-1}) {
    s.sample({},{});
    const int face=180+30*sign, other=180-30*sign;
    for (int d=-4;d<=4;++d) s.rays[face+d]=1.5/std::cos(d*pi/180.);
    s.rays[other]=1.495;
    auto r=assess(s.scan);
    near(r.nearest_bearing_rad.value(),sign*pi/6,.003,"supported face resolves range tie");
    check(r.nearest_support_rays==9 && r.rays[other].hit,"preserve singleton as occupancy");
    s.rays[other]=1.45;
    r=assess(s.scan);
    near(r.nearest_bearing_rad.value(),-sign*pi/6,.003,"clearly closer isolated obstacle");
    check(r.nearest_support_rays==1,"singleton support");
    for (int d=-4;d<=4;++d) s.rays[other+d]=1.495/std::cos(d*pi/180.);
    r=assess(s.scan);
    near(r.nearest_bearing_rad.value(),-sign*pi/6,.003,"use true nearest for equal support");
  }
}
void mixed_edge_bearing() {
  // Regress the six-beam front face from the four-wheel campus capture. The
  // silhouette beam is 8 mm beyond the plane; the independent visible near
  // point is 1.1881 m at 36.57 degrees. The opposite face must mirror it.
  Sensor sensor;
  const std::array<float,7> depths{1.40471f,1.13296f,1.12594f,1.12752f,1.12941f,1.13158f,1.13400f};
  for(int side:{1,-1}) {
    sensor.sample({},{});
    for(std::size_t i=0;i<depths.size();++i) sensor.rays[180+side*(38+static_cast<int>(i))]=depths[i];
    const auto r=assess(sensor.scan);
    near(r.nearest_bearing_rad.value(),side*.638290836,2*pi/180,"mixed edge skewed the front face");
    near(r.nearest_range_m.value(),1.188104,.005,"mixed edge range");
    check(r.nearest_support_rays==6 && r.rays[180+side*39].hit,"retain the silhouette beam in the sweep");
  }
}
void sampled_edge_competing_with_isolated_echo() {
  // Captured sloping visible face ending one beam before a mixed return.
  // Both left- and right-handed scenes must return the observed face point.
  Sensor s;
  constexpr std::array<float,8> face{1.8066f,1.76149f,1.7405f,1.70131f,
                                    1.66549f,1.63269f,1.60255f,1.57482f};
  for (int side:{1,-1}) {
    const int isolated=180-45*side;
    auto frame=[&](float shift,float isolated_range) {
      s.sample({},{});
      for (std::size_t i=0;i<face.size();++i) s.rays[180+side*(37+int(i))]=face[i]+shift;
      s.rays[180+45*side]=6.47749f;
      s.rays[180+46*side]=1.58403f+shift;
      s.rays[isolated]=isolated_range;
      return s.scan;
    };
    const auto prev=assess(frame(.22f,1.76923f));
    const auto report=assess(frame(0.f,1.54923f),&prev);
    near(*report.nearest_bearing_rad,side*.75024855,2*pi/180,"localize observed sloping edge");
    check(report.nearest_uses_edge_tie && report.nearest_support_rays>=5,"edge ambiguity provenance");
    check(report.rays[isolated].hit && report.ttc_source_support_rays==1 &&
          *report.ttc_source_bearing_rad*side<0 && report.ttc_singleton_pair &&
          report.ttc_s && *report.ttc_s<.8,"a closer isolated closing echo still brakes");
    const auto nearer=assess(frame(0.f,1.49f));
    check(!nearer.nearest_uses_edge_tie && nearer.nearest_support_rays==1 &&
          *nearer.nearest_bearing_rad*side<0,"do not extend the silhouette beyond one sampled bin");
  }
}
void short_observed_faces() {
  Sensor s;
  const std::array<float,4> returns{1.12740f,1.11271f,1.11477f,1.11709f};
  for (int side:{-1,1}) for (int count:{2,3,4}) {
    s.sample({},{});
    for(int i=0;i<count;++i) s.rays[180+side*(41+i)]=returns[i];
    const auto r=assess(s.scan);
    near(r.nearest_bearing_rad.value(),side*.670815685,2*pi/180,"short visible silhouette angle");
    near(r.nearest_range_m.value(),1.17273,.005,"short visible silhouette range");
    check(r.nearest_support_rays==static_cast<std::size_t>(count),"keep all observed silhouette rays");
  }
}
void kinematic_pass() {
  const Box box{1.65,-.25,2.15,.25};
  Sensor s; GapFollow planner; Pose p; FrontObstacleReport report; bool sampled=false,avoided=false;
  double clearance=INFINITY;
  for (int i=0;i<1400;++i) {
    // 40 Hz integration, 20 Hz decisions, independent 8 Hz input.
    if (i%5==0) {
      s.sample(p,{box}); report=assess(s.scan,sampled?&report:nullptr); sampled=true;
    }
    const double range=std::hypot(3.8-p.x,p.y);
    if (range<=.055) {check(avoided && clearance>=.45,"trajectory clearance/avoidance"); return;}
    if (i%2) continue;
    const auto plan=planner.plan({range,std::atan2(-p.y,3.8-p.x)-p.yaw},&report);
    if (plan.decision=="stop") {
      std::cerr<<"stop at "<<p.x<<","<<p.y<<" yaw="<<p.yaw<<" "<<plan.reason_code<<"\n";
      check(false,"blocked kinematic bypass");
    }
    avoided|=plan.decision.starts_with("avoid");
    p.yaw+=plan.angular_radps*.05;
    p.x+=plan.linear_mps*.05*std::cos(p.yaw); p.y+=plan.linear_mps*.05*std::sin(p.yaw);
    clearance=std::min(clearance,std::hypot(p.x-std::clamp(p.x,box.xmin,box.xmax),
                                          p.y-std::clamp(p.y,box.ymin,box.ymax))-.31);
  }
  std::cerr<<"timeout "<<p.x<<","<<p.y<<" yaw="<<p.yaw<<"\n";
  check(false,"kinematic target missed");
}
VehicleFootprint lingao() { return {"test.lingao",.2335,.2335,.1655,.1655,.281}; }
void vehicle_checks() {
  Sensor s;
  for (auto f:{VehicleFootprint{"",.23,.23,.17,.17,.28},
               VehicleFootprint{"bad",.23,.23,.12,.17,.28},
               VehicleFootprint{"bad",NAN,.23,.17,.17,.28}}) {
    bool rejected=false;
    try { FootprintGapFollow p(f); } catch(const std::invalid_argument&) { rejected=true; }
    check(rejected,"invalid physical profile");
  }
  const auto shape=lingao();
  FootprintGapFollow planner(shape);
  const PlanInput goal{3.8,0.};
  auto aisle=steady(s,{{-5,.75,5,.9},{-5,-.9,5,-.75}});
  auto path=planner.plan(goal,&aisle);
  if (path.decision!="proceed") std::cerr<<"aisle "<<path.reason_code<<" "<<aisle.reason<<" "<<aisle.ttc_status<<" gap "<<path.clearance_m.value_or(-1)<<"\n";
  check(path.decision=="proceed" && path.footprint_ref==shape.profile_id &&
        path.clearance_m.value()>=.55 && path.linear_mps>0.,"parallel body fits aisle");
  auto wide=shape; wide.left_m=.22;
  check(FootprintGapFollow(wide).plan(goal,&aisle).decision=="stop","wider overhang cannot fit same aisle");
  auto blind=aisle; blind.swept_coverage=false;
  check(planner.plan(goal,&blind).decision=="stop","unknown sides forbid a vehicle sweep");
  // A rod behind a rear overhang lies outside the narrower footprint.
  auto rear=steady(s,{{-.94,-.6,-.93,.6}});
  auto long_tail=shape; long_tail.rear_m=.41;
  check(planner.plan(goal,&rear).decision=="proceed" &&
        FootprintGapFollow(long_tail).plan(goal,&rear).decision=="stop","rear overhang");
  auto nose=steady(s,{{.91,-.6,.92,.6}});
  auto long_nose=shape; long_nose.front_m=.39;
  check(FootprintGapFollow(long_nose).plan(goal,&nose).decision=="stop","front overhang");
  // The body at rest fits beside the wall; the far rear corner would sweep
  // into its buffer when braking the measured clockwise rotation.
  auto rotating=shape; rotating.rear_m=.53;
  auto wall=steady(s,{{-5,.75,5,1.}});
  FootprintGapFollow turning(rotating);
  check(turning.plan(goal,&wall).decision=="proceed","asymmetric footprint at rest");
  PlanInput spinning=goal; spinning.measured_angular_radps=-1.;
  check(turning.plan(spinning,&wall).decision=="stop","swept tail while braking");
  auto close_wall=steady(s,{{.85,-3,.96,3}});
  auto braking=goal; braking.goal_bearing_rad=pi; braking.measured_linear_mps=.28;
  check(planner.plan(braking,&close_wall).decision=="stop","measured forward stopping distance");
  const auto free=steady(s,{});
  for(double heading:{-pi,-1.,-.4,0.,.3,.8,pi}) {
    const auto cmd=planner.plan({2.,heading},&free);
    check(cmd.decision=="proceed" && std::abs(cmd.linear_mps)+std::abs(cmd.angular_radps)*shape.track_m/2<=.320001,
          "wheel speed limit");
    check(cmd.curvature_per_m.has_value()==(cmd.linear_mps>0.),"pivot curvature");
  }
}
void shaped_kinematic_pass() {
  Sensor s; FootprintGapFollow planner(lingao()); Pose p; FrontObstacleReport report;
  bool sampled=false,avoided=false,advanced_on_turn=false;
  double v=0.,w=0.,max_y=0.;
  for (int i=0;i<6000;++i) {
    if (i%5==0) {
      s.sample(p,{{1.65,-.25,2.15,.25}});
      report=assess(s.scan,sampled?&report:nullptr); sampled=true;
    }
    const double distance=std::hypot(3.8-p.x,p.y);
    if (distance<=.055) {
      check(avoided && advanced_on_turn && max_y<1.14,"compact moving turn around obstacle");
      return;
    }
    if (i%2==0) {
      const auto plan=planner.plan({distance,std::atan2(-p.y,3.8-p.x)-p.yaw,true,true,v,w},&report);
      if (plan.decision=="stop") {
        std::cerr<<"shape stopped at "<<p.x<<","<<p.y<<" yaw="<<p.yaw<<" "<<plan.reason_code<<" gap="<<plan.clearance_m.value_or(-1)<<" v="<<v<<" w="<<w<<"\n";
        check(false,"blocked vehicle trajectory");
      }
      v=plan.linear_mps; w=plan.angular_radps;
      avoided|=plan.decision.starts_with("avoid"); advanced_on_turn|=v>.01 && std::abs(w)>.15;
    }
    p.x+=.025*v*std::cos(p.yaw+.0125*w); p.y+=.025*v*std::sin(p.yaw+.0125*w);
    p.yaw+=w*.025; max_y=std::max(max_y,std::abs(p.y));
  }
  std::cerr<<"shape timeout "<<p.x<<","<<p.y<<" yaw "<<p.yaw<<"\n";
  check(false,"shape goal timeout");
}

void transient_side_observation() {
  // On the campus failure trace a previously clear side acquired one nearby
  // return: only the long preview was lost. Replaying the same scan must not
  // force a side reversal or consume another acquisition's reassessment.
  Sensor sensor;
  const Box box{1.65,-.25,2.15,.25};
  auto shape=lingao(); shape.front_m=.259;
  FootprintGapFollow planner(shape);
  const auto clear_side=steady(sensor,{box});
  const auto first=planner.plan({3.8,0.},&clear_side);
  check(first.linear_mps==0. && std::abs(first.angular_radps)>.1,
        "large initial change of heading aligns four-wheel chassis before advancing");
  const int side=first.decision=="avoid_left" ? 1 : -1;
  const Pose pose{.349,side*.056,side*.52};
  sensor.sample(pose,{box});
  sensor.rays[180+side*45]=1.27428;
  auto frame=assess(sensor.scan); frame.ttc_status="not_closing";
  const PlanInput in{3.46,-side*.557,true,true,.278,-side*.247};
  for (int tick=0;tick<8;++tick) {
    if (tick>=4) ++frame.scan_seq;
    const auto held=planner.plan(in,&frame);
    check(held.decision==first.decision && held.linear_mps<=.100001 &&
          held.clearance_m.value()>=.55,"retain physically verified side across repeated or persistent returns");
  }
}

void singleton_ttc_confirmation() {
  Sensor s;
  // Two isolated returns generate precisely the weak association reported by
  // the simulated laser during the four-wheel campus repeat.
  const auto shape=lingao();
  auto frame=[&](float depth) {
    s.sample({},{}); s.rays[225]=depth; return s.scan;
  };
  const auto first=assess(frame(1.55f),nullptr,&shape);
  auto urgent=assess(frame(1.36f),&first,&shape);
  check(urgent.ttc_singleton_pair && urgent.ttc_s.value()<.8,"singleton association provenance");
  FootprintGapFollow planner(shape);
  PlanInput input{3.8,0.};
  auto tick=[&](const FrontObstacleReport& r) { return planner.plan(input,&r); };
  auto waiting=[&](LocalPlan q) {
    check(q.decision=="wait" && q.linear_mps==0. && q.angular_radps==0. &&
          q.reason_code=="navigation.ttc_confirmation_pending" && q.ttc_hold_stamp_ns==urgent.stamp_ns,
          "hold commands zero and exposes trigger sampling time");
  };
  input.measured_odom_stamp_ns=urgent.stamp_ns;
  waiting(tick(urgent)); waiting(tick(urgent));
  auto settled=assess(frame(1.36f),&urgent,&shape);
  auto advance=[&]() { settled.scan_seq++; settled.stamp_ns+=125'000'000; };
  input.measured_linear_mps=.07; input.measured_odom_stamp_ns=settled.stamp_ns;
  auto p=tick(settled); waiting(p); check(!p.ttc_clear_samples,"moving receipt counted as quiet");
  advance(); input.measured_linear_mps=0.; input.measured_odom_stamp_ns=settled.stamp_ns;
  p=tick(settled); waiting(p); check(p.ttc_clear_samples==1,"fresh quiet scan and odometry");
  for (int i=0;i<3;++i) {p=tick(settled);waiting(p);check(p.ttc_clear_samples==1,"duplicate scan counted");}
  advance(); p=tick(settled); waiting(p); check(!p.ttc_clear_samples,"reused odometry counted");
  for (int i=1;i<=3;++i) {
    advance(); input.measured_odom_stamp_ns=settled.stamp_ns; p=tick(settled);
    check(p.ttc_clear_samples==static_cast<unsigned>(i),"stationary distinct sample count");
    if (i<3) waiting(p);
  }
  check(p.decision!="stop" && p.decision!="wait","fresh stationary evidence must allow replanning");
  planner.reset(); input.measured_odom_stamp_ns=urgent.stamp_ns;
  waiting(tick(urgent));
  auto again=urgent; again.scan_seq++; again.stamp_ns+=125'000'000;
  p=tick(again);
  check(p.decision=="stop" && p.reason_code=="navigation.ttc_insufficient","persistent closing singleton");
  planner.reset(); waiting(tick(urgent));
  auto lost=urgent; lost.nearest_range_m.reset(); lost.ttc_s.reset(); lost.ttc_status="no_front_return";
  for (int i=1;i<=8;++i) {
    lost.scan_seq++; lost.stamp_ns+=125'000'000; input.measured_odom_stamp_ns=lost.stamp_ns;
    p=tick(lost);
    if(i<8) waiting(p);
  }
  check(p.decision=="stop" && p.reason_code=="navigation.ttc_unconfirmed","disappearing return cannot release motion");
  planner.reset(); auto supported=urgent; supported.ttc_singleton_pair=false;
  p=tick(supported); check(p.decision=="stop" && p.reason_code=="navigation.ttc_insufficient","supported hazard");
  planner.reset(); input.measured_odom_stamp_ns=urgent.stamp_ns; waiting(tick(urgent));
  input.scan_fresh=false;
  check(tick(settled).reason_code=="navigation.scan_stale","hold cannot override stale scan");
  input.scan_fresh=true;
  planner.reset(); check(tick(settled).decision!="wait","operation reset clears pending hold");
}

void dynamic_observer_and_policy() {
  const VehicleFootprint shape=lingao();
  for(int direction:{-1,1}) {
    Sensor sensor; ScanClusterTracker observer;
    FootprintGapFollow planner(shape,std::make_unique<CrossingYield>(shape));
    PlanInput in{3.8,0.};
    std::optional<FrontObstacleReport> previous;
    bool yielded=false,cleared=false,missing_held=false;
    std::uint64_t id=0;
    for(int i=0;i<140;++i) {
      // Real raycasting fixture: initially static, then a slow crossing,
      // a pause in the lane, and a brief occlusion before departure.
      const double y=direction*(-.38+std::max(0,i-5)*.025-std::clamp(i-30,0,8)*.025);
      const bool hidden=i==26 || i==27;
      std::vector<Box> boxes; if (!hidden) boxes.push_back({1.45,y-.25,1.85,y+.25});
      sensor.sample({},boxes);
      auto scan=assess(sensor.scan,previous?&*previous:nullptr,&shape); previous=scan;
      auto dynamic=observer.observe(scan,ScanPose{{},0.,scan.stamp_ns});
      in.measured_odom_stamp_ns=scan.stamp_ns;
      in.dynamic_obstacles=&dynamic;
      auto result=planner.plan(in,&scan);
      if (result.dynamic_phase=="yielding") {
        check(!cleared && result.linear_mps==0. && result.angular_radps==0.,"crossing must command zero");
        if(!yielded) check(i<=14,"late yielding");
        yielded=true;
        if (!id) id=result.dynamic_track_id;
        check(result.dynamic_track_id==id && id>0,"changed crossing target identity");
        if (hidden) missing_held=true;
      }
      if (result.dynamic_phase=="cleared") {
        check(std::abs(y)>1. && result.dynamic_clear_samples>=3 && yielded,"premature restart on crossing");
        cleared=true; break;
      }
      if(yielded && (hidden || i<55))
        check(result.linear_mps==0. && result.angular_radps==0.,"lost or stopped track cannot release crossing");
    }
    check(yielded && missing_held && cleared,"failed crossing-yield closure");
  }
  Sensor s; ScanClusterTracker observer;
  for (int i=0;i<12;++i) {
    const Pose body{.012*i,.002*i,.018*i};
    s.sample(body,{{1.45,-.25,1.85,.25}});
    const auto r=assess(s.scan); const auto d=observer.observe(r,ScanPose{{body.x,body.y},body.yaw,r.stamp_ns});
    check(d.valid && d.observed.size()==1,"missing static object under ego-motion");
    if(i>=4) check(d.observed[0].velocity_valid && std::hypot(d.observed[0].velocity.x,d.observed[0].velocity.y)<.07,
                    "ego motion appeared as obstacle motion");
    if (i==11) check(!observer.observe(r,ScanPose{{},0.,r.stamp_ns-100'000'000}).valid,"stale odometry accepted");
  }
}

void one_sided_static_occlusion() {
  // A fixed front face progressively disappears behind a sight-line edge.
  // Its centre moves at crossing speed while the opposite edge stays fixed.
  for (const int side:{-1,1}) {
    Sensor s; ScanClusterTracker tracker; CrossingYield policy(lingao());
    PlanInput input{3.8,0.};
    bool saw_centroid_motion=false;
    for (int i=0;i<35;++i) {
      s.sample({},{{1.65,-.38,2.,.38}});
      const double visible_edge=.38-std::clamp(i-8,0,18)*.032;
      for (std::size_t j=0;j<s.rays.size();++j) {
        const double angle=s.scan.angle_min+j*s.scan.angle_increment;
        if (std::isfinite(s.rays[j]) && side*s.rays[j]*std::sin(angle)>visible_edge)
          s.rays[j]=INFINITY;
      }
      const auto report=assess(s.scan);
      const auto tracks=tracker.observe(report,ScanPose{{},0.,report.stamp_ns});
      check(tracks.observed.size()==1,"retain the partially occluded wall");
      const auto& t=tracks.observed.front();
      saw_centroid_motion|=t.velocity_valid && std::abs(t.velocity.y)>.075;
      input.dynamic_obstacles=&tracks; input.measured_odom_stamp_ns=report.stamp_ns;
      LocalPlan plan; policy.permit(input,report,plan);
      check(plan.dynamic_phase!="yielding","one shrinking edge is not evidence of body translation");
    }
    check(saw_centroid_motion,"fixture must actually shift the observed centroid");
  }
}

void ambiguous_patch_and_reentry() {
  for (int side:{-1,1}) {
    Sensor s; CrossingYield policy(lingao()); PlanInput in{3.8,0.};
    ObstacleTrack object; object.id=8; object.radius_m=.18; object.samples=4;
    object.support_rays=3; object.span_s=.375; object.velocity_valid=true;
    auto tick=[&](double y,double vy,double early,double late,bool visible=true) {
      s.sample({},visible ? std::vector<Box>{{1.45,side*y-.12,1.65,side*y+.12}} : std::vector<Box>{});
      auto scan=assess(s.scan); DynamicObstacleReport frame;
      frame.valid=true; frame.scan_ref=scan.report_ref; frame.stamp_ns=scan.stamp_ns;
      frame.scan_seq=scan.scan_seq; frame.pose.odom_stamp_ns=scan.stamp_ns;
      object.center={1.45,side*y}; object.velocity={0.,side*vy};
      object.early_displacement={0.,side*early}; object.late_displacement={0.,side*late};
      if (visible) frame.observed.push_back(object);
      in.dynamic_obstacles=&frame; in.measured_odom_stamp_ns=scan.stamp_ns;
      LocalPlan out; bool permitted=policy.permit(in,scan,out);
      if (!permitted) check(out.decision=="wait" && !out.linear_mps && !out.angular_radps,"ambiguous motion requires zero");
      return out;
    };
    check(tick(-.5,.2,.04,.04).dynamic_phase=="yielding","first crossing must latch");
    for (int i=0;i<2;++i) check(tick(1.4,0,0,0).dynamic_phase=="yielding","exit evidence must accumulate");
    check(tick(1.4,0,0,0).dynamic_phase=="cleared","first crossing cleared");
    ++object.id;
    check(tick(1.44,-.51,-.129,.001).dynamic_phase=="checking_motion","one sudden patch change cannot prove translation");
    for (int i=0;i<4;++i)
      check(tick(1.44,0,0,0,false).dynamic_phase=="checking_motion","missing ambiguous track cannot clear hold");
    for (int i=0;i<3;++i)
      check(tick(.31,0,0,0).dynamic_phase=="checking_motion","ambiguous stationary object inside lane cannot clear");
    for (int i=0;i<2;++i) check(tick(1.44,0,0,0).dynamic_phase=="checking_motion","wait for free stationary observations");
    check(tick(1.44,0,0,0).dynamic_phase=="observation_resolved","seen stationary exterior object with verified clear lane");
    check(tick(1.35,-.30,-.04,-.04).dynamic_phase=="yielding","a real second incursion must latch");
    for (int i=0;i<4;++i) check(tick(.4,0,0,0,i%2==0).dynamic_phase=="yielding","stopped or obscured intruder remains latched");
    for (int i=0;i<2;++i) check(tick(-1.3,0,0,0).dynamic_phase=="yielding","reentry needs independent departure evidence");
    check(tick(-1.3,0,0,0).dynamic_phase=="cleared","second verified departure restores planning");
  }
}
}  // namespace
int main() {
  observations(); competing_surfaces(); sampled_edge_competing_with_isolated_echo(); mixed_edge_bearing();
  short_observed_faces(); choices(); kinematic_pass();
  vehicle_checks(); shaped_kinematic_pass(); transient_side_observation(); singleton_ttc_confirmation();
  dynamic_observer_and_policy(); one_sided_static_occlusion(); ambiguous_patch_and_reentry();
}
