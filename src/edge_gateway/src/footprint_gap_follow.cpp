#include "local_planner.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <numbers>
#include <stdexcept>
#include <utility>

namespace edge::local {
namespace {
constexpr double pi = std::numbers::pi;
constexpr double hard_margin_m = .55;
constexpr double speed_limit = .28;
constexpr double wheel_speed_limit = .32;
constexpr double reaction_time = .175; // One 8 Hz scan and one 20 Hz control period.
constexpr double brake_mps2 = .5, brake_radps2 = 2.;
constexpr double distance_cap = .85; // Report a lower bound beyond the planning band.

struct Pose { Point xy; double yaw; };
struct Segment { Point a, b; double xlo, xhi, ylo, yhi; };
double sq(double a) { return a*a; }
double to_segment2(Point p, Point a, Point b) {
  const double dx=b.x-a.x, dy=b.y-a.y, len2=sq(dx)+sq(dy);
  const double f=len2 ? std::clamp(((p.x-a.x)*dx+(p.y-a.y)*dy)/len2,0.,1.) : 0.;
  return sq(p.x-a.x-f*dx)+sq(p.y-a.y-f*dy);
}
// Segment vs axis-aligned rectangle, expressed about its centre in body coordinates.
double box_distance2(Point a, Point b, double hx, double hy) {
  const double dx=b.x-a.x, dy=b.y-a.y;
  if (std::min(a.x,b.x)<=hx && std::max(a.x,b.x)>=-hx &&
      std::min(a.y,b.y)<=hy && std::max(a.y,b.y)>=-hy &&
      std::abs(a.x*dy-a.y*dx)<=hx*std::abs(dy)+hy*std::abs(dx)) return 0.;
  auto endpoint=[&](Point p) {
    return sq(std::max(0.,std::abs(p.x)-hx))+sq(std::max(0.,std::abs(p.y)-hy));
  };
  double result=std::min(endpoint(a),endpoint(b));
  for (double x:{-hx,hx}) for (double y:{-hy,hy})
    result=std::min(result,to_segment2({x,y},a,b));
  return result;
}
Pose arc(double length, double angle) {
  if (std::abs(angle)<1e-8) return {{length,.5*length*angle},angle};
  return {{length*std::sin(angle)/angle,length*2*sq(std::sin(angle/2))/angle},angle};
}

// Consecutive laser endpoints enclose observed space. Shadow edges are included.
// A long straight sweep is exactly one extended oriented box. Curved sweeps use
// midpoint boxes expanded by a bound on the travel of any corner in a half-step.
class Sweep {
public:
  Sweep(const FrontObstacleReport& r, const VehicleFootprint& f, double horizon)
    : shape(f), corner(std::hypot(std::max(f.front_m,f.rear_m),std::max(f.left_m,f.right_m))) {
    Point a=r.rays.back().endpoint;
    for (const auto& ray:r.rays) {
      const auto b=ray.endpoint;
      if (!ray.known || !std::isfinite(b.x) || !std::isfinite(b.y)) { inside=false; return; }
      if ((a.y>0)!=(b.y>0) && a.x+(b.x-a.x)*(-a.y)/(b.y-a.y)>0.) inside=!inside;
      if (to_segment2({},a,b)<=sq(horizon+corner+distance_cap+.1))
        edges.push_back({a,b,std::min(a.x,b.x),std::max(a.x,b.x),std::min(a.y,b.y),std::max(a.y,b.y)});
      a=b;
    }
  }
  double rectangle(Pose pose, double straight=0.) const {
    if (!inside) return 0.;
    const double co=std::cos(pose.yaw), si=std::sin(pose.yaw);
    const double hx=(shape.front_m+shape.rear_m+straight)/2., hy=(shape.left_m+shape.right_m)/2.;
    const double dx=hx-shape.rear_m, dy=hy-shape.right_m;
    const Point centre{pose.xy.x+co*dx-si*dy,pose.xy.y+si*dx+co*dy};
    const double wx=std::abs(co)*hx+std::abs(si)*hy, wy=std::abs(si)*hx+std::abs(co)*hy;
    auto local=[&](Point p) {
      const double x=p.x-centre.x, y=p.y-centre.y;
      return Point{co*x+si*y,-si*x+co*y};
    };
    double bound=distance_cap*distance_cap;
    for (const auto& e:edges) {
      const double gx=std::max({0.,centre.x-wx-e.xhi,e.xlo-centre.x-wx});
      const double gy=std::max({0.,centre.y-wy-e.yhi,e.ylo-centre.y-wy});
      if (sq(gx)+sq(gy)>=bound) continue;
      bound=std::min(bound,box_distance2(local(e.a),local(e.b),hx,hy));
    }
    return std::sqrt(bound);
  }
  double trajectory(double length, double angle, double straight, double required, double start_yaw=0.) const {
    const double cx=std::cos(start_yaw),sy=std::sin(start_yaw);
    auto pose_at=[&](double t) {
      const auto p=arc(length*t,angle*t);
      return Pose{{cx*p.xy.x-sy*p.xy.y,sy*p.xy.x+cx*p.xy.y},start_yaw+p.yaw};
    };
    const double end_gap=rectangle(pose_at(1.),straight);
    if (end_gap<required || std::abs(angle)<1e-8) {
      if (std::abs(angle)<1e-8 && length>0.) return rectangle({{},start_yaw},length+straight);
      if (std::abs(angle)<1e-8 && length<0.) return rectangle(pose_at(1.),-length+straight);
      return end_gap;
    }
    const int steps=std::max(1,static_cast<int>(std::ceil(std::abs(angle)/.055)));
    const double ds=std::abs(length)/steps, da=std::abs(angle)/steps;
    // Cover translation along the midpoint tangent exactly. The centre arc
    // deviates by at most ds*da/8, and a corner rotates at most corner*da/2.
    const double inflation=ds*da/8+corner*da/2;
    double gap=end_gap;
    for (int i=0;i<steps;++i) {
      const double t=(i+.5)/steps;
      auto mid=pose_at(t);
      mid.xy.x-=ds/2*std::cos(mid.yaw);
      mid.xy.y-=ds/2*std::sin(mid.yaw);
      gap=std::min(gap,rectangle(mid,ds)-inflation);
      if (gap<required) break;
    }
    return gap;
  }
private:
  const VehicleFootprint& shape;
  double corner;
  bool inside=false;
  std::vector<Segment> edges;
};
struct Candidate {
  double v{}, w{}, clearance{};
  int side{};
};
} // namespace

FootprintGapFollow::FootprintGapFollow(VehicleFootprint footprint, std::unique_ptr<LocalMotionPolicyPort> policy)
  : footprint_(std::move(footprint)), policy_(std::move(policy)) {
  const auto& f=footprint_;
  for (double value:{f.front_m,f.rear_m,f.left_m,f.right_m,f.track_m})
    if (!std::isfinite(value) || value<=0.) throw std::invalid_argument("invalid positive footprint extent or track");
  if (f.profile_id.empty() || f.track_m/2>std::min(f.left_m,f.right_m))
    throw std::invalid_argument("footprint requires an identity and must enclose the drive track");
}

// A single isolated range jump cannot establish surface correspondence. The
// very first dangerous estimate still commands zero immediately. Only a fresh
// set of stationary, same-surface observations can release this interlock.
bool FootprintGapFollow::confirm_ttc(const PlanInput& in,const FrontObstacleReport& r,LocalPlan& out) {
  const bool hazard=r.ttc_s && *r.ttc_s<.8;
  if (!ttc_hold_ && !hazard) return true;
  auto terminal=[&](const char* reason) {
    out.reason_code=reason;
    return false;
  };
  auto pending=[&]() {
    out.decision="wait"; out.reason_code="navigation.ttc_confirmation_pending";
    return false;
  };
  if (!ttc_hold_) {
    if (!r.ttc_singleton_pair || !r.ttc_source_range_m || !r.ttc_source_bearing_rad)
      return terminal("navigation.ttc_insufficient");
    TtcHold h;
    h.source=r.source_id; h.calibration=r.calibration_ref; h.clock=r.clock_id; h.epoch=r.clock_epoch;
    h.start_ns=h.last_scan_ns=r.stamp_ns; h.last_seq=r.scan_seq;
    h.range=*r.ttc_source_range_m; h.bearing=*r.ttc_source_bearing_rad;
    ttc_hold_=h;
    out.ttc_hold_stamp_ns=h.start_ns;
    return pending();
  }
  auto& h=*ttc_hold_;
  out.ttc_hold_stamp_ns=h.start_ns;
  out.ttc_clear_samples=h.quiet_samples;
  if (r.source_id!=h.source || r.calibration_ref!=h.calibration || r.clock_id!=h.clock ||
      r.clock_epoch!=h.epoch || r.scan_seq<h.last_seq || r.stamp_ns<h.last_scan_ns ||
      (r.scan_seq==h.last_seq)!=(r.stamp_ns==h.last_scan_ns) ||
      std::max(r.stamp_ns,in.measured_odom_stamp_ns)-h.start_ns>=1'000'000'000)
    return terminal("navigation.ttc_unconfirmed");
  const bool new_scan=r.scan_seq>h.last_seq;
  // A second independent dangerous acquisition (or a supported surface)
  // receives the same terminal brake as an unambiguously dangerous estimate.
  if (hazard && (new_scan || !r.ttc_singleton_pair)) return terminal("navigation.ttc_insufficient");
  const bool stationary=std::abs(in.measured_linear_mps)<=.02 && std::abs(in.measured_angular_radps)<=.04;
  if (!stationary) { h.quiet_samples=0; out.ttc_clear_samples=0; }
  if (!new_scan) return pending();
  if (r.stamp_ns-h.last_scan_ns>200'000'000 || r.scan_seq-h.last_seq!=1) h.quiet_samples=0;
  h.last_seq=r.scan_seq; h.last_scan_ns=r.stamp_ns;
  const bool associated=r.ttc_source_range_m && r.ttc_source_bearing_rad && r.closing_speed_mps &&
    r.ttc_status=="not_closing" && std::abs(*r.closing_speed_mps)<=.025 &&
    std::abs(*r.ttc_source_range_m-h.range)<=.06 &&
    std::abs(std::remainder(*r.ttc_source_bearing_rad-h.bearing,2*pi))<=.10;
  const auto odom=in.measured_odom_stamp_ns;
  const bool paired=odom>h.start_ns && odom>h.last_odom_ns && std::abs(odom-r.stamp_ns)<=175'000'000;
  if (!stationary || !associated || !paired) {
    h.quiet_samples=0; out.ttc_clear_samples=0;
    return pending();
  }
  if (!h.quiet_samples) { h.first_quiet_ns=r.stamp_ns; h.first_quiet_odom_ns=odom; }
  h.last_odom_ns=odom;
  out.ttc_clear_samples=++h.quiet_samples;
  if (h.quiet_samples<3 || r.stamp_ns-h.first_quiet_ns<240'000'000 ||
      odom-h.first_quiet_odom_ns<240'000'000) return pending();
  ttc_hold_.reset();
  return true;
}

LocalPlan FootprintGapFollow::plan(const PlanInput& in,const FrontObstacleReport* report) {
  LocalPlan output;
  output.footprint_ref=footprint_.profile_id;
  if (report) { output.report_ref=report->report_ref; output.ttc_s=report->ttc_s; }
  auto stop=[&](const char* reason) { output.reason_code=reason; return output; };
  if (!in.localization_fresh) return stop("navigation.odom_timeout");
  if (!report || !in.scan_fresh) return stop("navigation.scan_stale");
  if (!report->valid || !report->swept_coverage || report->rays.size()<3 || report->rays.size()>4096)
    return stop("navigation.scan_invalid");
  if (!std::isfinite(in.goal_distance_m) || in.goal_distance_m<0. || !std::isfinite(in.goal_bearing_rad))
    return stop("navigation.goal_invalid");
  if (!std::isfinite(in.measured_linear_mps) || !std::isfinite(in.measured_angular_radps) ||
      std::abs(in.measured_linear_mps)>1. || std::abs(in.measured_angular_radps)>3.)
    return stop("navigation.odom_invalid");
  const double v=in.measured_linear_mps,w=in.measured_angular_radps;
  // Both wheel speeds ramp proportionally to zero. Use the longer linear or
  // angular braking time, so the stopping arc has the current curvature.
  const double stopping_time=reaction_time+.5*std::max(std::abs(v)/brake_mps2,std::abs(w)/brake_radps2);
  const double horizon=std::min(2.4,in.goal_distance_m);
  Sweep sweep(*report,footprint_,std::max(horizon,std::abs(v)*stopping_time));
  const double here=sweep.rectangle({{},0.});
  output.clearance_m=here;
  if (here<hard_margin_m || sweep.trajectory(v*stopping_time,w*stopping_time,0.,hard_margin_m)<hard_margin_m)
    return stop("navigation.no_safe_path");
  if (!confirm_ttc(in,*report,output)) return output;
  if (report->ttc_status=="unobserved") {
    output.decision="wait"; return stop("navigation.ttc_unobserved");
  }
  if (policy_ && !policy_->permit(in,*report,output)) return output;
  // Reserve extra tracking room where available. In a tight corridor leave
  // some of the excess for scan quantization and the rotating corner bound.
  const double required=hard_margin_m+std::min(.032,(here-hard_margin_m)*.5);
  const double goal=std::remainder(in.goal_bearing_rad,2*pi);
  auto corridor=[&](double heading,int side,double preview,double max_speed,bool align=false)->std::optional<Candidate> {
    // Skid steering changes body yaw before its velocity vector has aligned.
    // For substantial heading changes, first rotate about the drive centre;
    // blend the remaining small error with a moving arc and straight tangent.
    const double pivot_yaw=align ? heading : (std::abs(heading)>.28 ? heading-std::copysign(.10,heading) : 0.);
    const bool pivot=std::abs(pivot_yaw)>1e-8;
    const double steering=heading-pivot_yaw;
    const double radius=std::max(.36,footprint_.track_m);
    const double bend=std::max(radius*std::abs(steering),std::min(.32,preview));
    const double length=std::min(bend,preview);
    const double yaw=bend==0. ? 0. : steering*length/bend;
    double speed=pivot ? 0. : std::min(max_speed,1.1*in.goal_distance_m);
    double angular=pivot ? std::clamp(2.3*pivot_yaw,-1.1,1.1) : (bend ? speed*steering/bend : 0.);
    const double wheel=std::abs(speed)+std::abs(angular)*footprint_.track_m/2;
    const double scale=std::max({1.,wheel/wheel_speed_limit,std::abs(angular)/1.1});
    speed/=scale; angular/=scale;
    const double brake=reaction_time+.5*std::max(speed/brake_mps2,std::abs(angular)/brake_radps2);
    if (sweep.trajectory(speed*brake,angular*brake,0.,hard_margin_m)<hard_margin_m) return {};
    const double pivot_gap=pivot ? sweep.trajectory(0.,pivot_yaw,0.,required) : distance_cap;
    if (pivot_gap<required) return {};
    const double gap=std::min(pivot_gap,sweep.trajectory(length,yaw,preview-length,required,pivot_yaw));
    if (gap<required) return {};
    return Candidate{speed,angular,gap,side};
  };
  auto search=[&](int preferred,double preview,double max_speed,bool align=false) -> std::optional<Candidate> {
    for (int step=1;step<=36;++step) {
      std::optional<Candidate> best;
      for (int sign:{1,-1}) {
        if (preferred && preferred!=sign) continue;
        const auto choice=corridor(std::remainder(goal+sign*step*pi/72,2*pi),sign,preview,max_speed,align);
        if (choice && (!best || choice->clearance>best->clearance+1e-5)) best=choice;
      }
      if (best) return best;
    }
    return {};
  };
  auto candidate=corridor(goal,0,horizon,speed_limit);
  if (!candidate) candidate=search(side_,horizon,speed_limit);
  if (!candidate && side_) candidate=search(side_,horizon,speed_limit,true);
  bool restricted=false;
  if (!candidate && side_) {
    // A transient return in the long preview must not reverse the manoeuvre
    // across the obstacle. Count distinct scanner acquisitions. A low-speed
    // continuation may use only the verified pausing distance plus one body
    // length ahead. The full stopping sweep above is independently mandatory.
    if (restricted_seq_!=report->scan_seq) {
      restricted_seq_=report->scan_seq;
      restricted_scans_=std::min(4u,restricted_scans_+1);
    }
    restricted=true;
    constexpr double creep=.10;
    const double short_preview=footprint_.front_m+footprint_.rear_m+creep*(reaction_time+creep/(2*brake_mps2));
    candidate=search(side_,std::min(horizon,short_preview),creep);
    if (!candidate && (restricted_scans_<3 || std::abs(v)>.04 || std::abs(w)>.10)) {
      output.decision="wait"; return stop("navigation.reassessing_corridor");
    }
    if (!candidate) candidate=search(-side_,horizon,speed_limit);
  }
  if (!candidate) return stop("navigation.no_safe_path");
  if (!restricted || side_!=candidate->side) { restricted_scans_=0; restricted_seq_.reset(); }
  side_=candidate->side;
  output.decision=side_>0 ? "avoid_left" : side_<0 ? "avoid_right" : "proceed";
  output.reason_code=side_ ? "navigation.local_avoid" : "navigation.proceed";
  output.clearance_m=candidate->clearance;
  output.linear_mps=candidate->v; output.angular_radps=candidate->w;
  if (candidate->v>0.) output.curvature_per_m=candidate->w/candidate->v;
  return output;
}
} // namespace edge::local
