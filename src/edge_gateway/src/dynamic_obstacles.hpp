#pragma once

#include "local_planner.hpp"
#include <deque>

namespace edge::local {
struct ScanPose {
  Point position;
  double yaw{};
  std::int64_t odom_stamp_ns{};
};
struct ObstacleTrack {
  std::uint64_t id{};
  Point center, velocity; // metres and metres per second in odom
  Point half_extent, half_extent_rate; // observed odom-aligned silhouette
  Point early_displacement, late_displacement; // disjoint acquisition intervals
  double radius_m{}, span_s{}, fit_error_m{};
  unsigned samples{}, support_rays{};
  bool velocity_valid{};
};
struct DynamicObstacleReport {
  std::string observer_id, report_ref, scan_ref, reason;
  std::uint64_t scan_seq{};
  std::int64_t stamp_ns{};
  bool valid{};
  ScanPose pose;
  std::vector<ObstacleTrack> observed;
};
class DynamicObserverPort {
public:
  virtual ~DynamicObserverPort() = default;
  virtual DynamicObstacleReport observe(const FrontObstacleReport&,const std::optional<ScanPose>&) = 0;
};
class ScanClusterTracker final : public DynamicObserverPort {
public:
  DynamicObstacleReport observe(const FrontObstacleReport&,const std::optional<ScanPose>&) override;
private:
  struct Sample { std::int64_t stamp; Point position, half_extent; };
  struct History { ObstacleTrack track; std::deque<Sample> points; };
  std::vector<History> histories_;
  std::string source_, calibration_, clock_;
  std::uint64_t epoch_{}, next_id_=1;
  std::int64_t last_stamp_{};
};
class CrossingYield final : public LocalMotionPolicyPort {
public:
  explicit CrossingYield(const VehicleFootprint& footprint) : footprint_(footprint) {}
  bool permit(const PlanInput&,const FrontObstacleReport&,LocalPlan&) override;
  void reset() override { hold_.reset(); first_scan_.reset(); }
private:
  struct Hold {
    std::uint64_t id{}, last_seq{};
    Point crossing, direction;
    double radius{}, departure_sign{};
    bool confirmed{}, observed_exit{};
    unsigned clear_count{};
    std::int64_t first_clear{}, first_odom{}, last_odom{}, last_scan{}, started{};
  };
  VehicleFootprint footprint_;
  std::optional<Hold> hold_;
  std::optional<std::int64_t> first_scan_;
};
} // namespace edge::local
