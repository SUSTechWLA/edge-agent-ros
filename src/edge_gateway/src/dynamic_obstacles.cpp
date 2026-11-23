#include "dynamic_obstacles.hpp"

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

std::vector<ObstacleTrack> clusters(const FrontObstacleReport& scan,const ScanPose& pose) {
  std::vector<ObstacleTrack> result;
  Point lo{},hi{},last{};
  unsigned count=0;
  auto finish=[&]() {
    const double size=norm(delta(hi,lo));
    if (count>=3 && size>=.035 && size<=1.5) {
      ObstacleTrack t;
      t.center={(lo.x+hi.x)/2,(lo.y+hi.y)/2};
      t.half_extent={(hi.x-lo.x)/2,(hi.y-lo.y)/2};
      t.radius_m=size/2+.08; // Visible spread plus allowance for changes of face.
      t.support_rays=count;
      result.push_back(t);
    }
    count=0;
  };
  for (const auto& ray:scan.rays) {
    const double range=norm(ray.endpoint);
    if (!ray.known || !ray.hit || range<.12 || range>4.5) { finish(); continue; }
    const Point p=offset(pose.position,rotate(ray.endpoint,pose.yaw));
    if (count && norm(delta(p,last))>std::max(.12,2.7*range*scan.angle_step_rad)) finish();
    if (!count) lo=hi=p;
    lo={std::min(lo.x,p.x),std::min(lo.y,p.y)};
    hi={std::max(hi.x,p.x),std::max(hi.y,p.y)};
    last=p; ++count;
  }
  finish();
  return result;
}

// Confirm an entire transverse slice at the conflict point is inside the
// measured free-space polygon. Checking a few sampled points would miss
// an obstacle or a shadow edge between those points.
bool visible_slice(const FrontObstacleReport& scan,const ScanPose& pose,Point centre,Point unit,double half_width) {
  const Point left{-unit.y,unit.x};
  auto local=[&](Point p) {
    const auto d=delta(offset(pose.position,rotate(p,pose.yaw)),centre);
    return Point{dot(d,unit),dot(d,left)};
  };
  constexpr double hx=.12;
  bool inside=false;
  Point a=local(scan.rays.back().endpoint);
  for (const auto& ray:scan.rays) {
    if (!ray.known) return false;
    const Point b=local(ray.endpoint);
    if ((a.y>0)!=(b.y>0) && a.x-a.y*(b.x-a.x)/(b.y-a.y)>0.) inside=!inside;
    const auto d=delta(b,a);
    if (std::min(a.x,b.x)<=hx && std::max(a.x,b.x)>=-hx &&
        std::min(a.y,b.y)<=half_width && std::max(a.y,b.y)>=-half_width &&
        std::abs(a.x*d.y-a.y*d.x)<=hx*std::abs(d.y)+half_width*std::abs(d.x)) return false;
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
    histories_.clear();
  source_=scan.source_id; calibration_=scan.calibration_ref; clock_=scan.clock_id; epoch_=scan.clock_epoch;
  last_stamp_=scan.stamp_ns;
  if (!scan.valid || !scan.swept_coverage || scan.rays.size()<3 || !pose ||
      std::abs(scan.stamp_ns-pose->odom_stamp_ns)>90'000'000) {
    histories_.clear(); report.reason="missing_scan_or_paired_odometry"; return report;
  }
  report.pose=*pose;
  auto objects=clusters(scan,*pose);
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
  if (!r || !r->valid || r->scan_ref!=scan.report_ref || r->stamp_ns!=scan.stamp_ns || !scan.swept_coverage)
    return wait("navigation.dynamic_unobserved","unobserved");
  out.dynamic_report_ref=r->report_ref;
  if (!first_scan_) first_scan_=r->stamp_ns;
  const double yaw=r->pose.yaw+in.goal_bearing_rad;
  const Point direction{std::cos(yaw),std::sin(yaw)},left{-direction.y,direction.x};
  const double half_width=std::max(footprint_.left_m,footprint_.right_m)+.55;
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
    if (!track.velocity_valid || supported_speed<.075 || supported_speed*track.span_s<.03) continue;
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
  if (conflict && (!hold_ || (!hold_->confirmed && confirmed))) {
    Hold h; h.id=conflict->id; h.direction=direction; h.radius=conflict->radius_m;
    h.crossing=offset(r->pose.position,direction,dot(delta(conflict->center,r->pose.position),direction));
    h.departure_sign=dot(conflict->velocity,left)>0. ? 1. : -1.;
    h.started=r->stamp_ns; h.confirmed=confirmed; hold_=h;
  }
  if (!hold_) {
    if (candidate_at_start && r->stamp_ns-*first_scan_<900'000'000)
      return wait("navigation.dynamic_observing","observing");
    return true;
  }
  auto& h=*hold_;
  out.dynamic_track_id=h.id;
  const auto object=std::find_if(r->observed.begin(),r->observed.end(),[&](const auto& t){return t.id==h.id;});
  const Point transverse{-h.direction.y,h.direction.x};
  if (object!=r->observed.end()) {
    const double departure=dot(delta(object->center,h.crossing),transverse)*h.departure_sign;
    const double outer_edge=half_width+std::max(h.radius,object->radius_m)+.02;
    if (h.confirmed) {
      h.observed_exit=departure>outer_edge;
    } else {
      // Both identities (a target entering or a changing visible patch)
      // remain possible until this same observed silhouette is stationary
      // wholly outside the reserved lane. A missing sample cannot clear it.
      h.observed_exit=object->velocity_valid && norm(object->velocity)<=.035 && std::abs(departure)>outer_edge;
    }
  } else if (!h.confirmed) {
    h.observed_exit=false;
  }
  const bool stationary=std::abs(in.measured_linear_mps)<=.02 && std::abs(in.measured_angular_radps)<=.04;
  if (!stationary) h.clear_count=0;
  if (h.last_seq!=r->scan_seq) {
    const auto odom=in.measured_odom_stamp_ns;
    const bool paired=odom>h.started && odom>h.last_odom && std::abs(odom-r->stamp_ns)<=175'000'000;
    if (r->scan_seq!=h.last_seq+1 || r->stamp_ns-h.last_scan>200'000'000) h.clear_count=0;
    h.last_seq=r->scan_seq; h.last_scan=r->stamp_ns;
    if (h.observed_exit && !conflict && stationary && paired &&
        visible_slice(scan,r->pose,h.crossing,h.direction,half_width)) {
      if (!h.clear_count) {h.first_clear=r->stamp_ns;h.first_odom=odom;}
      ++h.clear_count; h.last_odom=odom;
    } else h.clear_count=0;
  }
  out.dynamic_clear_samples=h.clear_count;
  if (h.clear_count>=3 && r->stamp_ns-h.first_clear>=240'000'000 &&
      in.measured_odom_stamp_ns-h.first_odom>=240'000'000) {
    out.dynamic_phase=h.confirmed ? "cleared" : "observation_resolved"; hold_.reset(); return true;
  }
  if (!h.confirmed) return wait("navigation.dynamic_observing","checking_motion");
  return wait("navigation.dynamic_yield","yielding");
}
} // namespace edge::local
