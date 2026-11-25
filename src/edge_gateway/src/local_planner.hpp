#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <vector>

namespace edge::local {
struct DynamicObstacleReport;
struct ScanFrame {
  std::string source_id, frame_id, calibration_ref;
  std::int64_t stamp_ns{};
  std::uint64_t seq{};
  double angle_min{}, angle_max{}, angle_increment{}, range_min{}, range_max{};
  double sensor_x_m{}, sensor_y_m{}, sensor_yaw_rad{};
  std::span<const float> ranges;
  std::string clock_id{"ros"};
  std::uint64_t clock_epoch{};
  double range_resolution_m{};
};

struct Point { double x{}, y{}; };
// Positive extents about the differential drive reference point (x forward, y left).
struct VehicleFootprint {
  std::string profile_id;
  double front_m{}, rear_m{}, left_m{}, right_m{}, track_m{};
};
struct Ray {
  Point endpoint;
  bool known{}, hit{};
};
struct SurfaceObservation {
  double range_m{}, bearing_rad{}, first_bearing_rad{}, last_bearing_rad{};
  std::size_t support{};
};
struct FrontObstacleReport {
  int schema_version{1};
  std::string source_id, frame_id{"base_link"}, calibration_ref, clock_id, report_ref, reason, ttc_status;
  std::uint64_t clock_epoch{}, scan_seq{};
  std::int64_t stamp_ns{};
  bool valid{};
  std::optional<double> nearest_range_m, nearest_bearing_rad, nearest_width_m;
  std::size_t nearest_support_rays{};
  std::optional<double> front_min_m, left_min_m, right_min_m, left_gap_m, right_gap_m;
  std::optional<double> closing_speed_mps, ttc_s;
  // TTC keeps its original calibrated nearest-range association even when a
  // continuous silhouette provides a better bearing for the spatial report.
  std::optional<double> ttc_source_range_m, ttc_source_bearing_rad;
  std::size_t ttc_source_support_rays{};
  bool nearest_uses_edge_tie{};
  bool ttc_singleton_pair{};
  // Visible front-sector patches for association if a different obstacle becomes nearest.
  std::vector<SurfaceObservation> front_surfaces;
  std::size_t ttc_surface_index{};
  bool ttc_alternate_surface_pair{};
  std::vector<Ray> rays;
  // Known endpoints bound visible space even for max-range (no-return) rays.
  double angle_step_rad{};
  bool swept_coverage{};
};
FrontObstacleReport assess(const ScanFrame&, const FrontObstacleReport* previous = nullptr,
                           const VehicleFootprint* footprint = nullptr);

struct PlanInput {
  double goal_distance_m{}, goal_bearing_rad{};
  bool scan_fresh{true}, localization_fresh{true};
  double measured_linear_mps{}, measured_angular_radps{};
  std::int64_t measured_odom_stamp_ns{};
  const DynamicObstacleReport* dynamic_obstacles{};
  // The applied, rate-limited arc in base_link at the paired odometry pose.
  double tracking_arc_m{}, tracking_curvature_per_m{};
};
// Shared minimum for the shaped sweep and dynamic recovery policy.
inline constexpr double footprint_hard_margin_m = .55;

struct DynamicRecheck {
  bool corridor_clear{}, stationary{}, tracks_clear{};
  std::int64_t scan_stamp_ns{}, odom_stamp_ns{}, dwell_ns{};
  std::string mode;
  std::uint64_t blocking_track_id{};
  std::string blocking_region;
};

struct LocalPlan {
  std::string decision{"stop"}, reason_code, report_ref;
  double linear_mps{}, angular_radps{};
  std::optional<double> clearance_m, ttc_s;
  std::string footprint_ref;
  std::optional<double> curvature_per_m;
  std::string tracking_mode;
  std::optional<double> tracking_arc_m;
  std::optional<Point> tracking_target;
  std::optional<std::int64_t> ttc_hold_stamp_ns;
  unsigned ttc_clear_samples{};
  std::string dynamic_report_ref, dynamic_phase;
  std::uint64_t dynamic_track_id{};
  unsigned dynamic_clear_samples{};
  std::optional<DynamicRecheck> dynamic_recheck;
};
class LocalPlannerPort {
public:
  virtual ~LocalPlannerPort() = default;
  virtual LocalPlan plan(const PlanInput&, const FrontObstacleReport*) = 0;
  virtual void reset() {}
};
// Optional physical planning policy. It may request a zero-speed hold after
// the footprint and braking gates. Only Gateway can actually command motion.
class LocalMotionPolicyPort {
public:
  virtual ~LocalMotionPolicyPort() = default;
  virtual bool permit(const PlanInput&, const FrontObstacleReport&, LocalPlan&) = 0;
  virtual void reset() = 0;
};
class StopOnClearance final : public LocalPlannerPort {
public:
  LocalPlan plan(const PlanInput&, const FrontObstacleReport*) override;
};
class GapFollow final : public LocalPlannerPort {
public:
  LocalPlan plan(const PlanInput&, const FrontObstacleReport*) override;
  void reset() override { side_ = 0; }
private:
  int side_{};
};
enum class TrajectoryTracker { heading, regulated_pursuit };
class FootprintGapFollow final : public LocalPlannerPort {
public:
  explicit FootprintGapFollow(VehicleFootprint footprint, std::unique_ptr<LocalMotionPolicyPort> policy = {},
                              TrajectoryTracker tracker = TrajectoryTracker::heading);
  LocalPlan plan(const PlanInput&, const FrontObstacleReport*) override;
  void reset() override {
    side_=0; restricted_scans_=0; restricted_seq_.reset(); ttc_hold_.reset();
    if (policy_) policy_->reset();
  }
private:
  struct TtcHold {
    std::string source, calibration, clock;
    std::uint64_t epoch{}, last_seq{};
    std::int64_t start_ns{}, last_scan_ns{}, first_quiet_ns{}, first_quiet_odom_ns{}, last_odom_ns{};
    double range{}, bearing{};
    unsigned quiet_samples{};
  };
  VehicleFootprint footprint_;
  std::unique_ptr<LocalMotionPolicyPort> policy_;
  TrajectoryTracker tracker_;
  int side_{};
  unsigned restricted_scans_{};
  std::optional<std::uint64_t> restricted_seq_;
  std::optional<TtcHold> ttc_hold_;
  bool confirm_ttc(const PlanInput&, const FrontObstacleReport&, LocalPlan&);
};
}  // namespace edge::local
