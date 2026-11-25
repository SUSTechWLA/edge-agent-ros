#include "dynamic_obstacles.hpp"
#include "swept_geometry.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace edge::local {
namespace {
double dot(Point a,Point b) { return a.x*b.x+a.y*b.y; }
double norm(Point a) { return std::hypot(a.x,a.y); }
Point delta(Point a,Point b) { return {a.x-b.x,a.y-b.y}; }
Point offset(Point a,Point b,double t=1.) { return {a.x+t*b.x,a.y+t*b.y}; }
Point rotate(Point a,double yaw) {
  const double c=std::cos(yaw),s=std::sin(yaw);
  return {c*a.x-s*a.y,s*a.x+c*a.y};
}

using Surface = std::vector<std::pair<Point,Point>>;
using Returns = std::vector<std::optional<Point>>;
Returns world_returns(const FrontObstacleReport& scan,const ScanPose& pose) {
  Returns points;
  for (const auto& ray:scan.rays) {
    const double range=norm(ray.endpoint);
    if (!ray.known || !ray.hit || range<.12 || range>4.5) points.push_back(std::nullopt);
    else points.push_back(offset(pose.position,rotate(ray.endpoint,pose.yaw)));
  }
  return points;
}
Surface surface(const Returns& points) {
  Surface lines;
  for (std::size_t i=1;i<points.size();++i) {
    if (points[i-1] && points[i] && norm(delta(*points[i-1],*points[i]))<=.125)
      lines.emplace_back(*points[i-1],*points[i]);
  }
  return lines;
}
bool anchored(Point p,const std::vector<Surface>& reference) {
  if (reference.size()!=3) return false;
  return std::all_of(reference.begin(),reference.end(),[&](const Surface& lines){
    return std::any_of(lines.begin(),lines.end(),[&](const auto& line){
      return geometry::to_segment2(p,line.first,line.second)<=geometry::sq(.025);
    });
  });
}
// A range discontinuity at grazing incidence can truncate an otherwise
// stationary anchored face. Accept its newly revealed end only while two
// returns beyond the cluster continue that same plane and three anchored
// supports on its inside establish its orientation. An emerging silhouette
// endpoint has no such continuation and remains eligible for full motion.
void extend_anchored_end(const Returns& points,std::size_t begin,std::vector<bool>& known,bool reverse) {
  auto index=[&](std::size_t i) {return reverse ? known.size()-1-i : i;};
  std::size_t missing=0;
  while (missing<known.size() && !known[index(missing)]) ++missing;
  if (!missing || missing+3>known.size()) return;
  const int outer=static_cast<int>(begin+index(0)),step=reverse ? -1 : 1;
  const int outside=outer-2*step;
  if (outside<0 || outside>=static_cast<int>(points.size()) || !points[outside] || !points[outer-step]) return;
  const Point a=*points[outside];
  for (std::size_t i=missing;i<known.size() && known[index(i)];++i) {
    if (i<missing+2) continue;
    const Point end=*points[begin+index(i)],axis=delta(end,a);
    const double span=norm(axis);
    if (span<.20 || norm(delta(end,*points[begin+index(missing)]))<.10) continue;
    const Point normal{-axis.y/span,axis.x/span};
    bool same=true;
    for (int k=outside;k!=static_cast<int>(begin+index(i))+step;k+=step) {
      if (!points[k] || std::abs(dot(delta(*points[k],a),normal))>.026) {same=false;break;}
    }
    if (same) {for (std::size_t j=0;j<missing;++j) known[index(j)]=true;return;}
  }
}
// When a flat face is clipped by foreground returns or continues on both
// sides, its visible endpoints are not object edges. Only normal range flow
// is observable. A silhouette edge exposed against free space restores the
// original full velocity immediately, including motion along a wall.
std::optional<Point> cropped_face(const Returns& points,std::size_t begin,std::size_t end,Point origin) {
  if (end-begin<3 || begin<2 || end+1>=points.size()) return {};
  const Point first=*points[begin],last=*points[end-1];
  if (norm(delta(last,first))<.10) return {};
  std::optional<Point> best;
  double best_span=0.;
  const auto low=begin>5 ? begin-5 : 0,high=std::min(points.size()-1,end+4);
  for (std::size_t lo=low;lo<=begin;++lo) for (std::size_t hi=end-1;hi<=high;++hi) {
    if (!points[lo] || !points[hi]) continue;
    const Point a=*points[lo],axis=delta(*points[hi],a);
    const double span=norm(axis);
    if (span<=best_span || span<.24) continue;
    const Point n{-axis.y/span,axis.x/span};
    auto error=[&](Point p) {return std::abs(dot(delta(p,a),n));};
    // All returns between the two supports must belong to this face.
    bool face=true;
    for (std::size_t i=lo;i<=hi && face;++i) face=points[i] && error(*points[i])<=.026;
    if (!face) continue;
    auto boundary=[&](int i,int step,Point edge) {
      if (!points[i]) return 0;
      if (error(*points[i])<=.026 && points[i+step] && error(*points[i+step])<=.026) return 2;
      if (norm(delta(*points[i],origin))+.06<norm(delta(edge,origin))) return 1;
      return 0;
    };
    const int left=boundary(static_cast<int>(begin)-1,-1,first),right=boundary(static_cast<int>(end),1,last);
    if (left && right && std::max(left,right)==2) {best_span=span;best=n;}
  }
  return best;
}
std::vector<ObstacleTrack> clusters(const FrontObstacleReport& scan,const ScanPose& pose,
                                    const Returns& points,const std::vector<Surface>& reference) {
  std::vector<ObstacleTrack> result;
  std::vector<Point> patch;
  std::size_t begin=0;
  auto emit=[&](std::span<const Point> pts,std::size_t first,bool stationary) {
    if (pts.size()<3) return true;
    Point lo=pts.front(),hi=lo;
    for (Point p:pts) {
      lo={std::min(lo.x,p.x),std::min(lo.y,p.y)};
      hi={std::max(hi.x,p.x),std::max(hi.y,p.y)};
    }
    const double size=norm(delta(hi,lo));
    if (size<.035) return true;
    if (size>1.5) return false;
    ObstacleTrack t;
    t.center={(lo.x+hi.x)/2,(lo.y+hi.y)/2};
    t.half_extent={(hi.x-lo.x)/2,(hi.y-lo.y)/2};
    t.radius_m=size/2+.08;
    t.support_rays=static_cast<unsigned>(pts.size());
    t.matches_stationary_surface=stationary;
    t.observed_face_normal=cropped_face(points,first,first+pts.size(),pose.position);
    result.push_back(t);return true;
  };
  auto finish=[&]() {
    if (patch.size()<3 || reference.size()!=3) {emit(patch,begin,false);patch.clear();return;}
    // Keep the original silhouette and its velocity history across departure
    // from the reference. A novel edge immediately restores prediction.
    std::vector<bool> matched;
    for (Point p:patch) matched.push_back(anchored(p,reference));
    extend_anchored_end(points,begin,matched,false);
    extend_anchored_end(points,begin,matched,true);
    bool supported=matched.front() && matched.back();
    for (std::size_t i=1;supported && i+1<patch.size();++i) {
      if (matched[i]) continue;
      // A single interior beam may straddle an anchor corner. Both neighboring
      // returns must agree and the point must lie on the segment between them.
      supported=matched[i-1] && matched[i+1] &&
                geometry::to_segment2(patch[i],patch[i-1],patch[i+1])<=geometry::sq(.022);
    }
    if (!emit(patch,begin,supported) && !supported &&
        std::count(matched.begin(),matched.end(),true)>=3) {
      // A person emerging against a long facade can share a laser cluster
      // with the building. Preserve supported novel subclusters even though
      // the whole facade exceeds the existing object association size.
      const std::span<const Point> all(patch);
      std::size_t start=0;
      while (start<matched.size()) {
        while (start<matched.size() && matched[start]) ++start;
        std::size_t end=start;
        while (end<matched.size() && !matched[end]) ++end;
        if (end>start) emit(all.subspan(start,end-start),begin+start,false);
        start=end;
      }
    }
    patch.clear();
  };
  for (std::size_t i=0;i<points.size();++i) {
    if (!points[i]) {finish();continue;}
    const Point p=*points[i];
    const double range=norm(delta(p,pose.position));
    if (!patch.empty() && norm(delta(p,patch.back()))>std::max(.12,2.7*range*scan.angle_step_rad)) finish();
    if (patch.empty()) begin=i;
    patch.push_back(p);
  }
  finish();
  return result;
}

struct Corridor {
  Point centre, unit;
  double half_length, half_width, margin{};

  Point coordinates(Point p) const {
    const auto d=delta(p,centre);
    return {dot(d,unit),dot(d,{-unit.y,unit.x})};
  }
};

// Cover the actual moving body between successive arc midpoints. Translation
// deviates from its midpoint tangent by at most ds*da/8; a rotating corner
// can move a further corner_radius*da/2 transverse to the tangent.
std::vector<Corridor> tracking_corridors(const PlanInput& in,const ScanPose& pose,const VehicleFootprint& f) {
  std::vector<Corridor> boxes;
  const double k=in.tracking_curvature_per_m;
  if (in.tracking_arc_m<=0. || (std::abs(k)<1e-8 && std::abs(in.goal_bearing_rad)<1e-8)) return boxes;
  const int count=static_cast<int>(std::ceil(std::max(in.tracking_arc_m/.20,std::abs(k)*in.tracking_arc_m/.15)));
  const double ds=in.tracking_arc_m/count,da=std::abs(ds*k);
  const double hx=(f.front_m+f.rear_m)/2,hy=(f.left_m+f.right_m)/2;
  const double corner=std::hypot(std::max(f.front_m,f.rear_m),std::max(f.left_m,f.right_m));
  const double margin=footprint_hard_margin_m+ds*da/8.+corner*da/2.;
  const Point body_offset{hx-f.rear_m,hy-f.right_m};
  for (int i=0;i<count;++i) {
    const double s=(i+.5)*ds,angle=k*s;
    const Point mid=std::abs(k)<1e-8 ? Point{s,0.} :
      Point{std::sin(angle)/k,2.*std::pow(std::sin(angle/2),2)/k};
    const double yaw=pose.yaw+angle;
    const auto centre=offset(offset(pose.position,rotate(mid,pose.yaw)),rotate(body_offset,yaw));
    boxes.push_back({centre,rotate({1.,0.},yaw),hx+ds/2.,hy,margin});
  }
  return boxes;
}

bool forecast_intersects(const Corridor& box,const ObstacleTrack& t) {
  const Point start=box.coordinates(t.center), finish=box.coordinates(offset(t.center,t.velocity,2.5));
  return geometry::box_distance2(start,finish,box.half_length,box.half_width)<=geometry::sq(box.margin+t.radius_m);
}

bool overlaps(const Corridor& corridor,const ObstacleTrack& track) {
  const auto p=corridor.coordinates(track.center);
  if (std::hypot(std::max(0.,std::abs(p.x)-corridor.half_length),
                 std::max(0.,std::abs(p.y)-corridor.half_width))>corridor.margin+track.radius_m) return false;
  // Confirm a stationary, consistently visible silhouette before using its
  // observed world AABB to refine its circumscribed disc. Lost motion, poor
  // support or missing extents keep the full disc. The independent scan
  // visibility proof still covers the entire body sweep on every hold tick.
  const auto e=track.half_extent;
  const double speed=norm(track.velocity);
  constexpr double face_uncertainty=.08;
  if (!track.velocity_valid || speed>.035 || track.samples<5 || track.support_rays<3 || track.span_s<.65 ||
      norm(track.early_displacement)>.018 || norm(track.late_displacement)>.018 ||
      norm(track.half_extent_rate)>.035 || e.x<0. || e.y<0. ||
      std::abs(norm(e)+face_uncertainty-track.radius_m)>1e-6) return true;
  const double margin=corridor.margin+face_uncertainty+2.5*speed;
  const auto centre=delta(corridor.centre,track.center);
  if (std::abs(centre.x)<=e.x && std::abs(centre.y)<=e.y) return true;
  const Point vertices[]={{-e.x,-e.y},{e.x,-e.y},{e.x,e.y},{-e.x,e.y}};
  auto a=corridor.coordinates(offset(track.center,vertices[3]));
  for (const auto& vertex:vertices) {
    const auto b=corridor.coordinates(offset(track.center,vertex));
    if (geometry::box_distance2(a,b,corridor.half_length,corridor.half_width)<=geometry::sq(margin)) return true;
    a=b;
  }
  return false;
}

// The whole rectangle must lie in the measured free-space polygon. Include
// its shadow edges so a partly hidden corridor cannot qualify.
bool visible_corridor(const FrontObstacleReport& scan,const ScanPose& pose,const Corridor& corridor) {
  auto local=[&](Point p) {
    return corridor.coordinates(offset(pose.position,rotate(p,pose.yaw)));
  };
  const auto hx=corridor.half_length,hy=corridor.half_width;
  bool inside=false;
  Point a=local(scan.rays.back().endpoint);
  for (const auto& ray:scan.rays) {
    if (!ray.known || !std::isfinite(ray.endpoint.x) || !std::isfinite(ray.endpoint.y)) return false;
    const Point b=local(ray.endpoint);
    if ((a.y>0)!=(b.y>0) && a.x-a.y*(b.x-a.x)/(b.y-a.y)>0.) inside=!inside;
    if (geometry::box_distance2(a,b,hx,hy)<=geometry::sq(corridor.margin)) return false;
    a=b;
  }
  return inside;
}
} // namespace

DynamicObstacleReport ScanClusterTracker::observe(const FrontObstacleReport& scan,const std::optional<ScanPose>& pose) {
  DynamicObstacleReport report;
  report.observer_id="scan_clusters.v1"; report.scan_ref=scan.report_ref;
  report.report_ref=scan.report_ref+"/scan_clusters.v1"; report.stamp_ns=scan.stamp_ns; report.scan_seq=scan.scan_seq;
  if (scan.source_id!=source_ || scan.calibration_ref!=calibration_ || scan.clock_id!=clock_ ||
      scan.clock_epoch!=epoch_ || scan.stamp_ns<=last_stamp_ || scan.stamp_ns-last_stamp_>350'000'000)
    { histories_.clear(); reference_.clear(); first_pose_.reset(); }
  source_=scan.source_id; calibration_=scan.calibration_ref; clock_=scan.clock_id; epoch_=scan.clock_epoch;
  last_stamp_=scan.stamp_ns;
  if (!scan.valid || !scan.swept_coverage || scan.rays.size()<3 || !pose ||
      std::abs(scan.stamp_ns-pose->odom_stamp_ns)>90'000'000) {
    histories_.clear(); report.reason="missing_scan_or_paired_odometry"; return report;
  }
  report.pose=*pose;
  const auto points=world_returns(scan,*pose);
  if (!reference_frozen_ && reference_.size()<3) {
    if (!first_pose_ || norm(delta(first_pose_->position,pose->position))>.005 ||
        std::abs(std::remainder(first_pose_->yaw-pose->yaw,2*std::acos(-1.)))>.005) {
      first_pose_=pose;reference_.clear();
    }
    if (reference_.empty() || scan.stamp_ns-reference_stamp_>=450'000'000) {
      reference_.push_back(surface(points)); reference_stamp_=scan.stamp_ns;
    }
  }
  auto objects=clusters(scan,*pose,points,reference_);
  std::erase_if(histories_,[&](const History& h){return scan.stamp_ns-h.points.back().stamp>650'000'000;});
  if (objects.size()>96 || histories_.size()>160) { histories_.clear(); report.reason="track_capacity"; return report; }

  struct Match { std::size_t object, history; double error; };
  std::vector<Match> candidates;
  for(std::size_t i=0;i<objects.size();++i) for(std::size_t j=0;j<histories_.size();++j) {
    const auto& history=histories_[j]; const auto& t=history.track;
    const double dt=(scan.stamp_ns-history.points.back().stamp)*1e-9;
    const auto predicted=t.velocity_valid ? offset(t.center,t.velocity,dt) : t.center;
    const double error=norm(delta(objects[i].center,predicted));
    if (error<=.12+1.8*dt && std::abs(t.radius_m-objects[i].radius_m)<.20)
      candidates.push_back({i,j,error+std::abs(t.radius_m-objects[i].radius_m)*.25});
  }
  std::stable_sort(candidates.begin(),candidates.end(),[](auto a,auto b){return a.error<b.error;});
  std::vector<int> matches(objects.size(),-1); std::vector<bool> used(histories_.size());
  for(const auto& m:candidates) if(matches[m.object]<0 && !used[m.history]) {
    matches[m.object]=static_cast<int>(m.history); used[m.history]=true;
  }
  for(std::size_t i=0;i<objects.size();++i) {
    if(matches[i]<0) {
      histories_.emplace_back();
      matches[i]=static_cast<int>(histories_.size()-1);
      histories_.back().track.id=next_id_++;
    }
    auto& h=histories_[matches[i]];
    auto t=objects[i]; t.id=h.track.id;
    if (!h.points.empty() && scan.stamp_ns-h.points.back().stamp>230'000'000) h.points.clear();
    h.points.push_back({scan.stamp_ns,t.center,t.half_extent});
    while (h.points.size()>8 || scan.stamp_ns-h.points.front().stamp>1'000'000'000) h.points.pop_front();
    t.samples=static_cast<unsigned>(h.points.size());
    t.span_s=(scan.stamp_ns-h.points.front().stamp)*1e-9;
    if(t.samples>=3 && t.span_s>=.22) {
      const auto middle=h.points[h.points.size()/2].position;
      t.early_displacement=delta(middle,h.points.front().position);
      t.late_displacement=delta(h.points.back().position,middle);
      double mean_time=0.; Point mean{};
      for(const auto& p:h.points) { mean_time+=(p.stamp-scan.stamp_ns)*1e-9; mean=offset(mean,p.position); }
      mean_time/=t.samples; mean={mean.x/t.samples,mean.y/t.samples};
      double denominator=0.; Point numerator{},extent_numerator{};
      for(const auto& p:h.points) {
        const double dt=(p.stamp-scan.stamp_ns)*1e-9-mean_time;
        numerator=offset(numerator,delta(p.position,mean),dt);
        extent_numerator=offset(extent_numerator,p.half_extent,dt);
        denominator+=dt*dt;
      }
      t.velocity={numerator.x/denominator,numerator.y/denominator};
      t.half_extent_rate={extent_numerator.x/denominator,extent_numerator.y/denominator};
      for(const auto& p:h.points) {
        const double dt=(p.stamp-scan.stamp_ns)*1e-9-mean_time;
        t.fit_error_m=std::max(t.fit_error_m,norm(delta(p.position,offset(mean,t.velocity,dt))));
      }
      t.velocity_valid=t.fit_error_m<=.045 && norm(t.velocity)<=2.;
      if (t.observed_face_normal) {
        const Point n=*t.observed_face_normal;
        auto observable=[&](Point v) {const double speed=dot(v,n);return Point{speed*n.x,speed*n.y};};
        t.velocity=observable(t.velocity);
        t.early_displacement=observable(t.early_displacement);
        t.late_displacement=observable(t.late_displacement);
      }
    }
    h.track=t; report.observed.push_back(t);
  }
  report.valid=true; report.reason="ok"; return report;
}

bool CrossingYield::permit(const PlanInput& in,const FrontObstacleReport& scan,LocalPlan& out) {
  auto wait=[&](const char* reason,const char* phase) {
    out.reason_code=reason; out.dynamic_phase=phase; out.decision="wait"; return false;
  };
  const auto* r=in.dynamic_obstacles;
  if (!r || !r->valid || r->scan_ref!=scan.report_ref || r->stamp_ns!=scan.stamp_ns ||
      r->scan_seq!=scan.scan_seq || !scan.swept_coverage || scan.rays.size()<3 ||
      std::abs(r->stamp_ns-r->pose.odom_stamp_ns)>90'000'000) {
    if (hold_) hold_->clear_count=0;
    return wait("navigation.dynamic_unobserved","unobserved");
  }
  out.dynamic_report_ref=r->report_ref;
  if (!first_scan_) first_scan_=r->stamp_ns;
  const double yaw=r->pose.yaw+in.goal_bearing_rad;
  const Point direction{std::cos(yaw),std::sin(yaw)},left{-direction.y,direction.x};
  const double half_width=std::max(footprint_.left_m,footprint_.right_m)+footprint_hard_margin_m;
  const double lookahead=std::min(in.goal_distance_m+footprint_.front_m,2.7);
  const ObstacleTrack* conflict=nullptr;
  bool confirmed=false;
  bool candidate_at_start=false; double closest=std::numeric_limits<double>::infinity();
  for (const auto& track:r->observed) {
    const auto rel=delta(track.center,r->pose.position);
    const double ahead=dot(rel,direction), lateral=dot(rel,left), vy=dot(track.velocity,left);
    if (ahead+track.radius_m<=0. || ahead-track.radius_m>lookahead) continue;
    const double band=half_width+track.radius_m;
    candidate_at_start|=std::abs(lateral)<=band;
    // Occluding just one end of a static object moves its apparent midpoint.
    // The two lateral support bounds move at vy +/- the projected change in
    // visible half-width. Require sustained displacement from both bounds.
    const double width_rate=std::abs(left.x)*track.half_extent_rate.x+
                            std::abs(left.y)*track.half_extent_rate.y;
    const double supported_speed=std::abs(vy)-std::abs(width_rate);
    if (track.matches_stationary_surface || !track.velocity_valid || supported_speed<.075 || supported_speed*track.span_s<.03) continue;
    const double future=lateral+vy*2.5;
    if (std::min(lateral,future)>band || std::max(lateral,future)<-band) continue;
    // A jump to a different patch on the same face followed by a stationary
    // sample can fit a large velocity, particularly with only three returns.
    // Require actual travel in two disjoint time intervals before latching
    // a crossing. An ambiguous prediction still requests an immediate hold.
    const double sign=std::copysign(1.,vy);
    const bool sustained=std::min(sign*dot(track.early_displacement,left),
                                  sign*dot(track.late_displacement,left))>=.015;
    if ((sustained && !confirmed) || (sustained==confirmed && ahead<closest)) {
      closest=ahead; conflict=&track; confirmed=sustained;
    }
  }
  const auto arc_boxes=tracking_corridors(in,r->pose,footprint_);
  Point conflict_direction=direction;
  Point crossing_point{};
  if (conflict)
    crossing_point=offset(r->pose.position,direction,dot(delta(conflict->center,r->pose.position),direction));
  for (const auto& track:r->observed) {
    if (confirmed) break;
    const double speed=norm(track.velocity);
    if (track.matches_stationary_surface || !track.velocity_valid || speed<.075) continue;
    const Point travel{track.velocity.x/speed,track.velocity.y/speed};
    const double spread=std::abs(travel.x*track.half_extent_rate.x)+std::abs(travel.y*track.half_extent_rate.y);
    if (speed-spread<.075 || (speed-spread)*track.span_s<.03) continue;
    const auto hit=std::find_if(arc_boxes.begin(),arc_boxes.end(),
                                [&](const Corridor& box){return forecast_intersects(box,track);});
    if (hit==arc_boxes.end()) continue;
    const bool sustained=std::min(dot(track.early_displacement,travel),dot(track.late_displacement,travel))>=.015;
    if (!conflict || sustained) {
      confirmed=sustained; conflict=&track;
      // Keep the original conflict region in odom while the rover is held.
      // Align departure with the observed motion, including tracks parallel to the goal chord.
      conflict_direction={travel.y,-travel.x};
      crossing_point=hit->centre;
    }
  }
  if (conflict && (!hold_ || (!hold_->confirmed && confirmed))) {
    Hold h; h.id=conflict->id; h.direction=conflict_direction; h.radius=conflict->radius_m;
    h.crossing=crossing_point;
    h.departure_sign=dot(conflict->velocity,{-conflict_direction.y,conflict_direction.x})>0. ? 1. : -1.;
    h.started=r->stamp_ns; h.confirmed=confirmed;
    h.source=scan.source_id; h.calibration=scan.calibration_ref; h.clock=scan.clock_id; h.epoch=scan.clock_epoch;
    hold_=h;
  }
  if (!hold_) {
    if (candidate_at_start && r->stamp_ns-*first_scan_<900'000'000)
      return wait("navigation.dynamic_observing","observing");
    return true;
  }
  auto& h=*hold_;
  out.dynamic_track_id=h.id;
  if (scan.source_id!=h.source || scan.calibration_ref!=h.calibration || scan.clock_id!=h.clock ||
      scan.clock_epoch!=h.epoch || r->scan_seq<h.last_seq || r->stamp_ns<h.last_scan ||
      (h.last_seq && (r->scan_seq==h.last_seq)!=(r->stamp_ns==h.last_scan))) {
    h.clear_count=0;
    return wait("navigation.dynamic_recheck_source_changed","unobserved");
  }

  const auto odom=in.measured_odom_stamp_ns;
  const bool stationary=std::abs(in.measured_linear_mps)<=.02 && std::abs(in.measured_angular_radps)<=.04;
  const double forward=std::min(2.4,in.goal_distance_m)+footprint_.front_m;
  const double rear=footprint_.rear_m;
  const Corridor next{offset(r->pose.position,direction,(forward-rear)/2.),direction,(forward+rear)/2.,
                      std::max(footprint_.left_m,footprint_.right_m),footprint_hard_margin_m};
  const Corridor crossing{h.crossing,h.direction,.12,half_width};
  const bool corridor_clear=visible_corridor(scan,r->pose,next) && visible_corridor(scan,r->pose,crossing) &&
    std::all_of(arc_boxes.begin(),arc_boxes.end(),[&](const Corridor& box){return visible_corridor(scan,r->pose,box);});
  const ObstacleTrack* blocker=conflict;
  std::string region=conflict ? "forecast" : "";
  for (const auto& track:r->observed) {
    if (blocker) break;
    if (overlaps(next,track)) region="next";
    else if (overlaps(crossing,track)) region="original_crossing";
    else if (std::any_of(arc_boxes.begin(),arc_boxes.end(),[&](const Corridor& box){return overlaps(box,track);}))
      region="applied_arc";
    if (!region.empty()) blocker=&track;
  }
  const bool tracks_clear=blocker==nullptr;

  const auto object=std::find_if(r->observed.begin(),r->observed.end(),[&](const auto& t){return t.id==h.id;});
  const Point transverse{-h.direction.y,h.direction.x};
  std::string mode="tracking_in_lane";
  if (object!=r->observed.end()) {
    const double departure=dot(delta(object->center,h.crossing),transverse)*h.departure_sign;
    const double outer_edge=half_width+std::max(h.radius,object->radius_m)+.02;
    if (h.confirmed && departure>outer_edge) mode="tracked_exit";
    else if (object->velocity_valid && norm(object->velocity)<=.035 && std::abs(departure)>outer_edge)
      mode="stationary_exterior";
  } else {
    // A missing ID starts a longer recheck. Fresh visibility and independent
    // stationary odometry are what accumulate evidence over that window.
    mode="observed_corridor";
  }

  const bool qualifies=stationary && corridor_clear && tracks_clear && mode!="tracking_in_lane";
  if (!qualifies || h.mode!=mode) h.clear_count=0;
  h.mode=mode;
  if (h.last_seq!=r->scan_seq) {
    const bool paired=odom>h.started && odom>h.last_odom && std::abs(odom-r->stamp_ns)<=175'000'000;
    if (r->scan_seq!=h.last_seq+1 || r->stamp_ns-h.last_scan>200'000'000) h.clear_count=0;
    h.last_seq=r->scan_seq; h.last_scan=r->stamp_ns;
    if (qualifies && paired) {
      if (!h.clear_count) {h.first_clear=r->stamp_ns;h.first_odom=odom;}
      ++h.clear_count; h.last_odom=odom;
    } else h.clear_count=0;
  }
  const auto dwell=h.clear_count ? std::min(r->stamp_ns-h.first_clear,h.last_odom-h.first_odom) : 0;
  out.dynamic_recheck=DynamicRecheck{corridor_clear,stationary,tracks_clear,r->stamp_ns,odom,dwell,h.mode,
                                      blocker ? blocker->id : 0,region};
  out.dynamic_clear_samples=h.clear_count;
  const bool lost=h.mode=="observed_corridor";
  if (h.clear_count>=(lost ? 8u : 3u) && dwell>=(lost ? 875'000'000 : 240'000'000)) {
    out.dynamic_phase=h.confirmed ? "cleared" : "observation_resolved"; hold_.reset(); return true;
  }
  if (!h.confirmed) return wait("navigation.dynamic_observing","checking_motion");
  return wait("navigation.dynamic_yield","yielding");
}
} // namespace edge::local
