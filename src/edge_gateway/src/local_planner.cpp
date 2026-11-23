#include "local_planner.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <numbers>
#include <utility>

namespace edge::local {
namespace {
constexpr double pi = std::numbers::pi;
constexpr double infinity = std::numeric_limits<double>::infinity();
constexpr double body_radius = .31;
constexpr double margin = .55;
constexpr double fit_error = .015;
constexpr double probe_range = 1.5;
double norm(Point p) { return std::hypot(p.x, p.y); }
double dot(Point a, Point b) { return a.x*b.x + a.y*b.y; }
Point sub(Point a, Point b) { return {a.x-b.x, a.y-b.y}; }
Point polar(double d, double a) { return {d*std::cos(a), d*std::sin(a)}; }
double wrap(double a) { return std::remainder(a, 2*pi); }
void lower(std::optional<double>& value, double x) {
  if (!value || x < *value) value = x;
}
Point closest(Point a, Point b, Point target) {
  const auto axis = sub(b,a);
  const auto length2 = dot(axis,axis);
  const double t = length2 ? std::clamp(dot(sub(target,a),axis)/length2,0.,1.) : 0.;
  return {a.x+t*axis.x, a.y+t*axis.y};
}

// Split non-collinear surfaces before fitting their visible lines. The endpoints
// constrain each fit: no extension through a corner or an occluded interval.
struct Surface {
  double distance,bearing,width;
  std::size_t support;
  double edge_step_m{};
};
double median(std::span<double> values) {
  const auto middle=values.begin()+values.size()/2;
  std::nth_element(values.begin(),middle,values.end());
  if (values.size()%2) return *middle;
  return .5*(*middle+*std::max_element(values.begin(),middle));
}

// Across a silhouette edge a range beam can mix the near and far surface.
// At short visible spans a single edge return can rotate an ordinary line
// fit substantially. Use a median-pair direction only when a terminal point
// disagrees with the bulk of its own segment, preserving its visible extent.
void refine_mixed_edge(std::span<const Point> points,Point& axis,Point& center) {
  if (points.size()<5) return;
  constexpr std::size_t limit=13;
  const auto n=std::min(limit,points.size());
  std::array<Point,limit> anchors;
  std::array<double,limit*(limit-1)/2> slopes;
  std::size_t used=0;
  const Point normal{-axis.y,axis.x};
  for (std::size_t i=0;i<n;++i) {
    anchors[i]=points[i*(points.size()-1)/(n-1)];
    for (std::size_t j=0;j<i;++j) {
      const auto d=sub(anchors[i],anchors[j]);
      const double along=dot(d,axis);
      if (std::abs(along)>1e-6) slopes[used++]=dot(d,normal)/along;
    }
  }
  if (used<3) return;
  const double slope=median(std::span(slopes).first(used));
  const double length=std::hypot(1.,slope);
  const Point direction{(axis.x+slope*normal.x)/length,(axis.y+slope*normal.y)/length};
  const Point perpendicular{-direction.y,direction.x};
  std::array<double,limit> offsets;
  for (std::size_t i=0;i<n;++i) offsets[i]=dot(anchors[i],perpendicular);
  auto fit=std::span(offsets).first(n);
  const double intercept=median(fit);
  for (double& value:fit) value=std::abs(value-intercept);
  const double deviation=median(fit);
  const double edge_error=std::max(std::abs(dot(points.front(),perpendicular)-intercept),
                                   std::abs(dot(points.back(),perpendicular)-intercept));
  if (edge_error<=std::max(fit_error/6.,3*deviation)) return;
  axis=direction;
  const double correction=intercept-dot(center,perpendicular);
  center={center.x+correction*perpendicular.x,center.y+correction*perpendicular.y};
}
void visible_surface(std::span<const Point> points, double width, double range_resolution,
                     std::vector<Surface>& surfaces, bool first_edge=true, bool last_edge=true) {
  if (points.empty()) return;
  const Point first = points.front(), last = points.back();
  // With fewer than three interior samples the two silhouette endpoints can
  // dominate a fitted normal. A span of just a few range bins is similarly
  // ill-conditioned. Report the nearest point on the observed contiguous
  // polyline in either case, without extending or joining through a gap.
  if (points.size()<5 || norm(sub(last,first))<=4*range_resolution) {
    Point best=first;
    for (std::size_t i=1;i<points.size();++i) {
      const Point p=closest(points[i-1],points[i],{});
      if (dot(p,p)<dot(best,best)) best=p;
    }
    surfaces.push_back({norm(best),std::atan2(best.y,best.x),width,points.size()});
    return;
  }
  double worst = fit_error;
  std::size_t split = 0;
  for (std::size_t i=1; i+1<points.size(); ++i) {
    const auto dev = norm(sub(points[i], closest(first,last,points[i])));
    if (dev>worst) { worst=dev; split=i; }
  }
  if (split) {
    visible_surface(points.first(split+1),width,range_resolution,surfaces,first_edge,false);
    visible_surface(points.subspan(split),width,range_resolution,surfaces,false,last_edge);
    return;
  }
  Point center{};
  for (auto p: points) { center.x+=p.x; center.y+=p.y; }
  center.x/=points.size(); center.y/=points.size();
  double xx=0, yy=0, xy=0;
  for (auto p: points) {
    const auto d=sub(p,center);
    xx+=d.x*d.x; yy+=d.y*d.y; xy+=d.x*d.y;
  }
  Point axis=polar(1,.5*std::atan2(2*xy,xx-yy));
  refine_mixed_edge(points,axis,center);
  double lo=0, hi=0;
  for (auto p:points) {
    const auto t=dot(sub(p,center),axis);
    lo=std::min(lo,t); hi=std::max(hi,t);
  }
  const auto t=std::clamp(-dot(center,axis),lo,hi);
  const Point at{center.x+t*axis.x, center.y+t*axis.y};
  const double distance=norm(at), bearing=std::atan2(at.y,at.x);
  // At the end of a contiguous run the closest point can fall between two
  // laser bearings. Bound the range change over one angular sample from the
  // last two observed radial steps. An internal fitted corner has no gap.
  auto radial_step=[&](std::size_t edge,std::size_t next,std::size_t third) {
    if (std::abs(dot(sub(points[edge],center),axis)-t)>1e-6) return 0.;
    const double a=norm(points[edge]),b=norm(points[next]),c=norm(points[third]);
    return std::max(0.,std::min(b-a,c-b));
  };
  const auto n=points.size();
  const double edge_step=std::max(first_edge ? radial_step(0,1,2) : 0.,
                                  last_edge ? radial_step(n-1,n-2,n-3) : 0.);
  surfaces.push_back({distance,bearing,width,points.size(),edge_step});
}

void flush(std::vector<Point>& points, double range_resolution, std::vector<Surface>& surfaces) {
  if (points.empty()) return;
  const auto [lo,hi]=std::minmax_element(points.begin(),points.end(),[](Point a,Point b){return a.y<b.y;});
  visible_surface(points,hi->y-lo->y,range_resolution,surfaces);
  points.clear();
}

void select_surface(const std::vector<Surface>& surfaces, double resolution, FrontObstacleReport& out) {
  if (surfaces.empty()) return;
  const auto nearest=std::min_element(surfaces.begin(),surfaces.end(),
                                     [](auto a,auto b){return a.distance<b.distance;});
  auto select=[&](bool allow_edge_tie) {
    const Surface* best=&*nearest;
    for (const auto& s:surfaces) {
      const double angular_uncertainty=allow_edge_tie && nearest->support==1 ? s.edge_step_m : 0.;
      if (s.distance>nearest->distance+resolution+angular_uncertainty) continue;
      const bool supported=s.support>=2, best_supported=best->support>=2;
      if (supported!=best_supported) {
        if (supported) best=&s;
      } else if (s.distance<best->distance-1e-6 || (std::abs(s.distance-best->distance)<=1e-6 &&
                 std::abs(s.bearing)<std::abs(best->bearing))) best=&s;
    }
    return best;
  };
  const Surface* safety=select(false);
  const Surface* best=nearest->support==1 ? select(true) : safety;
  out.ttc_source_range_m=safety->distance; out.ttc_source_bearing_rad=safety->bearing;
  out.ttc_source_support_rays=safety->support; out.nearest_uses_edge_tie=(safety!=best);
  out.nearest_range_m=best->distance; out.nearest_bearing_rad=best->bearing;
  out.nearest_width_m=best->width; out.nearest_support_rays=best->support;
}

void estimate_ttc(FrontObstacleReport& report, const FrontObstacleReport* prev, const VehicleFootprint* shape) {
  report.ttc_status=report.ttc_source_range_m ? "unobserved" : "no_front_return";
  if (!report.ttc_source_range_m || !prev || !prev->valid || !prev->ttc_source_range_m ||
      prev->clock_id!=report.clock_id || prev->clock_epoch!=report.clock_epoch ||
      prev->source_id!=report.source_id || prev->calibration_ref!=report.calibration_ref) return;
  const double dt=(report.stamp_ns-prev->stamp_ns)*1e-9;
  if (dt<=0 || dt>.5 || std::abs(*report.ttc_source_bearing_rad-*prev->ttc_source_bearing_rad)>.16) return;
  const double speed=(*prev->ttc_source_range_m-*report.ttc_source_range_m)/dt;
  // Large range jumps under a turn indicate changed correspondence.
  if (std::abs(speed)>2.) return;
  report.closing_speed_mps=speed;
  report.ttc_singleton_pair=(report.ttc_source_support_rays==1 && prev->ttc_source_support_rays==1);
  report.ttc_status=speed>.025 ? "closing" : "not_closing";
  if (speed>.025) {
    double extent=body_radius;
    if (shape) {
      const auto direction=polar(1.,*report.ttc_source_bearing_rad);
      extent=std::max(shape->front_m*direction.x,-shape->rear_m*direction.x)+
             std::max(shape->left_m*direction.y,-shape->right_m*direction.y);
    }
    report.ttc_s=std::max(0.,*report.ttc_source_range_m-extent)/speed;
  }
}

bool finite_pose(const ScanFrame& s) {
  return std::isfinite(s.sensor_x_m) && std::isfinite(s.sensor_y_m) && std::isfinite(s.sensor_yaw_rad);
}
LocalPlan stopped(std::string code, const FrontObstacleReport* report, std::string decision="stop") {
  LocalPlan out;
  out.decision=std::move(decision); out.reason_code=std::move(code);
  if (report) { out.report_ref=report->report_ref; out.ttc_s=report->ttc_s; }
  return out;
}

// The consecutive ray endpoints bound observed free space (including the
// shadow edge at a jump in range). Test the swept circular footprint against
// the complete boundary, not just the obstacle returns at its vertices.
double cross(Point a, Point b) { return a.x*b.y-a.y*b.x; }
double swept_clearance(const FrontObstacleReport& report, double heading, double length) {
  const auto end=polar(length,heading);
  double nearest2=infinity;
  Point prev=report.rays.back().endpoint;
  for (const auto& ray:report.rays) {
    const Point p=ray.endpoint, edge=sub(p,prev);
    const double denominator=cross(end,edge);
    if (std::abs(denominator)>1e-12) {
      const double t=cross(prev,edge)/denominator, u=cross(prev,end)/denominator;
      if (t>=0 && t<=1 && u>=0 && u<=1) return -body_radius;
    }
    const Point a=sub(p,closest({},end,p));
    const Point b=closest(prev,p,{});
    const Point c=sub(end,closest(prev,p,end));
    nearest2=std::min({nearest2,dot(a,a),dot(b,b),dot(c,c)});
    prev=p;
  }
  return std::sqrt(nearest2)-body_radius;
}
}  // namespace

FrontObstacleReport assess(const ScanFrame& s, const FrontObstacleReport* previous, const VehicleFootprint* footprint) {
  FrontObstacleReport out;
  out.source_id=s.source_id; out.calibration_ref=s.calibration_ref; out.stamp_ns=s.stamp_ns;
  out.scan_seq=s.seq; out.clock_id=s.clock_id; out.clock_epoch=s.clock_epoch;
  out.report_ref=s.source_id+"/"+s.calibration_ref+"/"+s.clock_id+"/"+std::to_string(s.clock_epoch)+"/"+
                 std::to_string(s.stamp_ns)+"/"+std::to_string(s.seq);
  out.reason="invalid_geometry"; out.ttc_status="unobserved";
  const auto n=s.ranges.size();
  if (s.source_id.empty() || s.frame_id.empty() || s.calibration_ref.empty() || s.clock_id.empty() ||
      s.stamp_ns<0 || n<2 || n>4096 || !finite_pose(s) ||
      !std::isfinite(s.angle_min) || !std::isfinite(s.angle_max) || !std::isfinite(s.angle_increment) ||
      s.angle_increment<=0 || s.angle_increment>.12 || !std::isfinite(s.range_min) ||
      !std::isfinite(s.range_max) || s.range_min<0 || s.range_max<=s.range_min ||
      !std::isfinite(s.range_resolution_m) || s.range_resolution_m<0 || s.range_resolution_m>=s.range_max ||
      std::abs(s.angle_min+(n-1)*s.angle_increment-s.angle_max)>.5*s.angle_increment ||
      s.angle_max-s.angle_min>2*pi+.5*s.angle_increment) return out;
  if (previous && previous->source_id==s.source_id && previous->clock_id==s.clock_id &&
      previous->clock_epoch==s.clock_epoch && (s.stamp_ns<=previous->stamp_ns || s.seq<=previous->scan_seq)) {
    out.reason="nonmonotonic_scan"; return out;
  }
  out.angle_step_rad=s.angle_increment;
  out.swept_coverage=2*pi-(s.angle_max-s.angle_min)<=1.6*s.angle_increment;
  out.rays.reserve(n);
  std::vector<Point> cluster;
  std::vector<Surface> surfaces;
  double spans[2]{}, widest[2]{};
  bool front[10]{};
  for (std::size_t i=0;i<n;++i) {
    const double beam=wrap(s.angle_min+i*s.angle_increment+s.sensor_yaw_rad), d=s.ranges[i];
    const bool known=std::isinf(d) || (std::isfinite(d) && d>=s.range_min && d<=s.range_max);
    const bool hit=known && d!=infinity;
    const double bound=hit ? std::max(0.,d) : known ? s.range_max : 0.;
    Point p=polar(bound,beam); p.x+=s.sensor_x_m; p.y+=s.sensor_y_m;
    out.rays.push_back({p,known,hit});
    out.swept_coverage &= known;
    if (known && std::abs(beam)<=.42) front[std::clamp(int((beam+.42)/.084),0,9)]=true;
    const double distance=norm(p), bearing=std::atan2(p.y,p.x);
    if (hit && std::abs(bearing)<=.42) lower(out.front_min_m,distance);
    if (hit && std::abs(bearing)<=pi/2) lower(bearing>=0 ? out.left_min_m : out.right_min_m,distance);
    for (int side=0;side<2;++side) {
      const double direction=side==0 ? beam : -beam;
      spans[side]=(known && distance>=probe_range && direction>=0 && direction<=pi/2)
                 ? spans[side]+s.angle_increment : 0.;
      widest[side]=std::max(widest[side],spans[side]);
    }
    const bool forward=hit && std::abs(bearing)<=pi/4;
    if (!forward || (!cluster.empty() && norm(sub(cluster.back(),p))>std::max(.08,3*s.angle_increment*distance)))
      flush(cluster,s.range_resolution_m,surfaces);
    if (forward) cluster.push_back(p);
  }
  flush(cluster,s.range_resolution_m,surfaces);
  select_surface(surfaces,s.range_resolution_m,out);
  out.valid=std::all_of(std::begin(front),std::end(front),[](bool v){return v;});
  out.reason=out.valid ? "ok" : "insufficient_front_coverage";
  if (out.swept_coverage) {
    out.left_gap_m=2*probe_range*std::sin(.5*std::max(0.,widest[0]-s.angle_increment));
    out.right_gap_m=2*probe_range*std::sin(.5*std::max(0.,widest[1]-s.angle_increment));
  }
  if (out.valid) estimate_ttc(out,previous,footprint);
  return out;
}

LocalPlan StopOnClearance::plan(const PlanInput& in, const FrontObstacleReport* report) {
  auto out=stopped("navigation.proceed",report,"proceed");
  const auto error=wrap(in.goal_bearing_rad);
  out.angular_radps=std::clamp(2.3*error,-1.1,1.1);
  out.linear_mps=std::abs(error)>.35 ? 0. : std::min(.32,1.1*in.goal_distance_m);
  return out;
}

LocalPlan GapFollow::plan(const PlanInput& in, const FrontObstacleReport* r) {
  if (!in.localization_fresh) return stopped("navigation.odom_timeout",r);
  if (!r || !in.scan_fresh) return stopped("navigation.scan_stale",r);
  if (!r->valid || !r->swept_coverage || r->rays.size()<3) return stopped("navigation.scan_invalid",r);
  if (!std::isfinite(in.goal_distance_m) || in.goal_distance_m<0 || !std::isfinite(in.goal_bearing_rad))
    return stopped("navigation.goal_invalid",r);
  if (r->ttc_s && *r->ttc_s<.8) return stopped("navigation.ttc_insufficient",r);
  if (r->ttc_status=="unobserved") return stopped("navigation.ttc_unobserved",r,"wait");
  const double goal=wrap(in.goal_bearing_rad), horizon=std::min(2.4,in.goal_distance_m);
  // Prefer one control interval of extra tracking room. A swept path includes
  // its start, so a safe starting footprint cannot retrospectively acquire
  // the extra reserve. In that band, accept only corridors that preserve its
  // current clearance (within numerical precision) throughout the sweep.
  const double start_clearance=swept_clearance(*r,0.,0.);
  if (start_clearance<margin) return stopped("navigation.no_safe_path",r);
  const double planned_clearance=std::max(margin,std::min(margin+.045,start_clearance)-1e-7);
  double heading=goal, clearance=swept_clearance(*r,goal,horizon);
  int chosen=0;
  if (clearance<planned_clearance) {
    auto choose_corridor=[&](int preferred_side) {
      for (int step=1;step<=30;++step) {
        double best=-infinity;
        for (int side:{1,-1}) {
          if (preferred_side && side!=preferred_side) continue;
          const double candidate=wrap(goal+side*step*pi/60.);
          const double available=swept_clearance(*r,candidate,horizon);
          if (available>=planned_clearance && available>best+1e-6) {
            heading=candidate; clearance=available; chosen=side; best=available;
          }
        }
        if (chosen) return true;
      }
      return false;
    };
    if (!choose_corridor(side_) && (!side_ || !choose_corridor(-side_)))
      return stopped("navigation.no_safe_path",r);
  }
  side_=chosen;
  auto out=stopped(chosen ? "navigation.local_avoid" : "navigation.proceed",r,
                   chosen>0 ? "avoid_left" : chosen<0 ? "avoid_right" : "proceed");
  out.clearance_m=clearance;
  out.angular_radps=std::clamp(2.3*heading,-1.1,1.1);
  if (std::abs(heading)<.14 && swept_clearance(*r,0.,std::min(.2,horizon))>=margin)
    out.linear_mps=std::min(.28,1.1*in.goal_distance_m);
  return out;
}
}  // namespace edge::local
