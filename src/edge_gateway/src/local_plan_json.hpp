#pragma once

#include <cmath>
#include <json-c/json.h>
#include "local_planner.hpp"
#include "dynamic_obstacles.hpp"

namespace edge::local {
inline void text_field(json_object *obj, const char *key, const std::string &value) {
  json_object_object_add(obj, key, json_object_new_string(value.c_str()));
}
inline void metric(json_object *obj, const char *key, const std::optional<double> &value) {
  json_object_object_add(obj, key, value && std::isfinite(*value) ? json_object_new_double(*value) : nullptr);
}
inline json_object *encode_footprint(const VehicleFootprint &f) {
  auto *obj=json_object_new_object();
  text_field(obj,"profile_id",f.profile_id);
  text_field(obj,"frame_id","base_link");
  text_field(obj,"reference_point","drive_center");
  metric(obj,"front_m",f.front_m); metric(obj,"rear_m",f.rear_m);
  metric(obj,"left_m",f.left_m); metric(obj,"right_m",f.right_m);
  metric(obj,"track_m",f.track_m);
  return obj;
}
inline json_object *encode_report(const FrontObstacleReport &r, const std::string &source_frame,
                                  const VehicleFootprint* footprint=nullptr) {
  auto *obj = json_object_new_object();
  json_object_object_add(obj, "schema_version", json_object_new_int(1));
  text_field(obj, "source_id", r.source_id);
  text_field(obj, "source_frame_id", source_frame);
  text_field(obj, "frame_id", r.frame_id);
  text_field(obj, "clock_id", r.clock_id);
  json_object_object_add(obj, "clock_epoch", json_object_new_uint64(r.clock_epoch));
  text_field(obj, "report_ref", r.report_ref);
  text_field(obj, "calibration_ref", r.calibration_ref);
  json_object_object_add(obj, "stamp_ns", json_object_new_int64(r.stamp_ns));
  json_object_object_add(obj, "scan_seq", json_object_new_uint64(r.scan_seq));
  json_object_object_add(obj, "valid", json_object_new_boolean(r.valid));
  json_object_object_add(obj, "swept_coverage", json_object_new_boolean(r.swept_coverage));
  text_field(obj, "reason", r.reason);
  metric(obj, "nearest_range_m", r.nearest_range_m);
  metric(obj, "nearest_bearing_rad", r.nearest_bearing_rad);
  metric(obj, "nearest_width_m", r.nearest_width_m);
  json_object_object_add(obj, "nearest_support_rays", json_object_new_uint64(r.nearest_support_rays));
  json_object_object_add(obj, "nearest_uses_edge_tie", json_object_new_boolean(r.nearest_uses_edge_tie));
  metric(obj, "front_min_m", r.front_min_m);
  metric(obj, "left_min_m", r.left_min_m);
  metric(obj, "right_min_m", r.right_min_m);
  metric(obj, "left_gap_m", r.left_gap_m);
  metric(obj, "right_gap_m", r.right_gap_m);
  metric(obj, "closing_speed_mps", r.closing_speed_mps);
  metric(obj, "ttc_s", r.ttc_s);
  metric(obj, "ttc_source_range_m", r.ttc_source_range_m);
  metric(obj, "ttc_source_bearing_rad", r.ttc_source_bearing_rad);
  json_object_object_add(obj,"ttc_source_support_rays",json_object_new_uint64(r.ttc_source_support_rays));
  if (footprint) {
    text_field(obj,"ttc_footprint_ref",footprint->profile_id);
    json_object_object_add(obj,"ttc_singleton_pair",json_object_new_boolean(r.ttc_singleton_pair));
  }
  text_field(obj, "ttc_status", r.ttc_status);
  return obj;
}
inline json_object *encode_plan(const LocalPlan &p) {
  auto *obj = json_object_new_object();
  json_object_object_add(obj, "schema_version", json_object_new_int(1));
  text_field(obj, "decision", p.decision);
  if (!p.footprint_ref.empty()) {
    text_field(obj, "footprint_ref", p.footprint_ref);
    metric(obj, "curvature_per_m", p.curvature_per_m);
    if (!p.tracking_mode.empty()) {
      text_field(obj,"tracking_mode",p.tracking_mode);
      metric(obj,"tracking_arc_m",p.tracking_arc_m);
    }
    if (p.ttc_hold_stamp_ns) {
      json_object_object_add(obj,"ttc_hold_stamp_ns",json_object_new_int64(*p.ttc_hold_stamp_ns));
      json_object_object_add(obj,"ttc_clear_samples",json_object_new_uint64(p.ttc_clear_samples));
    }
  }
  if (p.tracking_target) {
    auto* xy=json_object_new_array();
    json_object_array_add(xy,json_object_new_double(p.tracking_target->x));
    json_object_array_add(xy,json_object_new_double(p.tracking_target->y));
    json_object_object_add(obj,"tracking_target_xy",xy);
    text_field(obj,"tracking_target_frame","odom");
  }
  text_field(obj, "reason_code", p.reason_code);
  if (!p.dynamic_report_ref.empty() || !p.dynamic_phase.empty()) {
    text_field(obj,"dynamic_report_ref",p.dynamic_report_ref);
    text_field(obj,"dynamic_phase",p.dynamic_phase);
    json_object_object_add(obj,"dynamic_track_id",json_object_new_uint64(p.dynamic_track_id));
    json_object_object_add(obj,"dynamic_clear_samples",json_object_new_uint64(p.dynamic_clear_samples));
    if (p.dynamic_recheck) {
      const auto& check=*p.dynamic_recheck;
      auto* evidence=json_object_new_object();
      text_field(evidence,"mode",check.mode);
      if (!check.blocking_region.empty()) {
        text_field(evidence,"blocking_region",check.blocking_region);
        json_object_object_add(evidence,"blocking_track_id",json_object_new_uint64(check.blocking_track_id));
      }
      json_object_object_add(evidence,"corridor_clear",json_object_new_boolean(check.corridor_clear));
      json_object_object_add(evidence,"stationary",json_object_new_boolean(check.stationary));
      json_object_object_add(evidence,"tracks_clear",json_object_new_boolean(check.tracks_clear));
      json_object_object_add(evidence,"scan_stamp_ns",json_object_new_int64(check.scan_stamp_ns));
      json_object_object_add(evidence,"odom_stamp_ns",json_object_new_int64(check.odom_stamp_ns));
      json_object_object_add(evidence,"dwell_ns",json_object_new_int64(check.dwell_ns));
      json_object_object_add(obj,"dynamic_recheck",evidence);
    }
  }
  text_field(obj, "report_ref", p.report_ref);
  metric(obj, "linear_mps", p.linear_mps);
  metric(obj, "angular_radps", p.angular_radps);
  metric(obj, "clearance_m", p.clearance_m);
  metric(obj, "ttc_s", p.ttc_s);
  return obj;
}
inline json_object *encode_dynamic(const DynamicObstacleReport& r) {
  auto* obj=json_object_new_object();
  json_object_object_add(obj,"schema_version",json_object_new_int(1));
  text_field(obj,"observer_id",r.observer_id);text_field(obj,"report_ref",r.report_ref);
  text_field(obj,"scan_ref",r.scan_ref);text_field(obj,"frame_id","odom");text_field(obj,"reason",r.reason);
  json_object_object_add(obj,"valid",json_object_new_boolean(r.valid));
  json_object_object_add(obj,"stamp_ns",json_object_new_int64(r.stamp_ns));
  json_object_object_add(obj,"scan_seq",json_object_new_uint64(r.scan_seq));
  json_object_object_add(obj,"odom_stamp_ns",json_object_new_int64(r.pose.odom_stamp_ns));
  auto* rows=json_object_new_array();
  for(const auto& t:r.observed) {
    auto* row=json_object_new_object(); json_object_object_add(row,"track_id",json_object_new_uint64(t.id));
    metric(row,"x_m",t.center.x);metric(row,"y_m",t.center.y);metric(row,"visible_radius_m",t.radius_m);
    metric(row,"visible_half_extent_x_m",t.half_extent.x);metric(row,"visible_half_extent_y_m",t.half_extent.y);
    if (t.observed_face_normal) text_field(row,"motion_evidence","cropped_face_normal_flow");
    json_object_object_add(row,"matches_stationary_surface",json_object_new_boolean(t.matches_stationary_surface));
    json_object_object_add(row,"velocity_valid",json_object_new_boolean(t.velocity_valid));
    if (t.velocity_valid) {
      metric(row,"vx_mps",t.velocity.x); metric(row,"vy_mps",t.velocity.y);
      metric(row,"half_extent_rate_x_mps",t.half_extent_rate.x);
      metric(row,"half_extent_rate_y_mps",t.half_extent_rate.y);
      metric(row,"early_dx_m",t.early_displacement.x); metric(row,"early_dy_m",t.early_displacement.y);
      metric(row,"late_dx_m",t.late_displacement.x); metric(row,"late_dy_m",t.late_displacement.y);
    }
    metric(row,"span_s",t.span_s);metric(row,"fit_error_m",t.fit_error_m);
    json_object_object_add(row,"samples",json_object_new_int64(t.samples));
    json_object_object_add(row,"support_rays",json_object_new_int64(t.support_rays));
    json_object_array_add(rows,row);
  }
  json_object_object_add(obj,"tracks",rows);
  return obj;
}
}  // namespace edge::local
