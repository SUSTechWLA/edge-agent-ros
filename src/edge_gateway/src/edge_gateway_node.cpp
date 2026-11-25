#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
#include <set>
#include <random>
#include <sstream>
#include <string>
#include <string_view>
#include <json-c/json.h>

#include "reference_path.hpp"
#include <edge_interfaces/navigation_site.hpp>
#include "edge_interfaces/action/navigate_path.hpp"
#include <functional>
#include "body_authority.hpp"
#include "local_plan_json.hpp"
#include "vehicle_profile.hpp"
#include "edge_interfaces/action/navigate_to.hpp"
#include "geometry_msgs/msg/twist.hpp"
#include "nav_msgs/msg/odometry.hpp"
#include "sensor_msgs/msg/laser_scan.hpp"
#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"
#include "std_msgs/msg/string.hpp"

using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
using Navigation = edge_interfaces::action::NavigateTo;
using PathAction = edge_interfaces::action::NavigatePath;
using Point = edge::local::Point;
struct Motion {
  std::string operation_id;
  Point target;
  std::optional<edge::local::ReferencePath> path;
  std::function<bool()> is_canceling;
  std::function<void(double)> feedback;
  std::function<void(const Navigation::Result&)> complete;
};

class EdgeGateway final : public rclcpp::Node {
public:
  EdgeGateway() : rclcpp::Node("edge_gateway"), site_(edge::NavigationSite::load(*this, 75)) {
    events_=create_publisher<std_msgs::msg::String>("/edge/events",100);
    std::random_device random;
    std::ostringstream instance;
    for (int i=0;i<4;++i) instance<<std::hex<<random()<<'-';
    identity_=instance.str();
    require_scan_=declare_parameter<bool>("require_scan",false);
    const auto planner_name=declare_parameter<std::string>("local_planner","stop_on_clearance");
    const auto tracker_name=declare_parameter<std::string>("trajectory_tracker","heading");
    if (tracker_name!="heading" && (tracker_name!="regulated_pursuit" || planner_name!="footprint_follow"))
      throw std::invalid_argument("regulated_pursuit requires footprint_follow");
    const auto observer_name=declare_parameter<std::string>("dynamic_observer","none");
    const auto policy_name=declare_parameter<std::string>("dynamic_policy","none");
    if (observer_name!="none") {
      if (observer_name!="scan_clusters" || (planner_name!="gap_follow" && planner_name!="footprint_follow"))
        throw std::invalid_argument("scan_clusters requires a calibrated scan provider");
      tracker_=std::make_unique<edge::local::ScanClusterTracker>();
      dynamic_pub_=create_publisher<std_msgs::msg::String>("/edge/perception/dynamic_obstacles",10);
    }
    if (policy_name!="none" && (policy_name!="yield_crossing" || planner_name!="footprint_follow" || !tracker_))
      throw std::invalid_argument("yield_crossing requires footprint_follow and a dynamic observation provider");
    if (planner_name=="gap_follow" || planner_name=="footprint_follow") {
      sensor_id_=declare_parameter<std::string>("scan_source_id","rover.scan");
      sensor_frame_=declare_parameter<std::string>("scan_frame_id","laser_link");
      calibration_=declare_parameter<std::string>("scan_calibration","");
      sensor_x_=declare_parameter<double>("scan_origin_x_m",std::numeric_limits<double>::quiet_NaN());
      sensor_y_=declare_parameter<double>("scan_origin_y_m",0.0);
      sensor_yaw_=declare_parameter<double>("scan_origin_yaw_rad",0.0);
      range_resolution_=declare_parameter<double>("scan_range_resolution_m",0.0);
      if (!require_scan_ || calibration_.empty() || sensor_id_.empty() || sensor_frame_.empty() ||
          !std::isfinite(sensor_x_) || !std::isfinite(sensor_y_) || !std::isfinite(sensor_yaw_) ||
          !std::isfinite(range_resolution_) || range_resolution_<0.0)
        throw std::invalid_argument("obstacle planning requires scan identity and reviewed base_link extrinsics");
      if (planner_name=="footprint_follow") {
        footprint_=edge::local::load_vehicle_profile(declare_parameter<std::string>("vehicle_profile",""));
        std::unique_ptr<edge::local::LocalMotionPolicyPort> policy;
        if (policy_name=="yield_crossing") policy=std::make_unique<edge::local::CrossingYield>(*footprint_);
        const auto tracker=tracker_name=="regulated_pursuit" ? edge::local::TrajectoryTracker::regulated_pursuit
                                                             : edge::local::TrajectoryTracker::heading;
        planner_=std::make_unique<edge::local::FootprintGapFollow>(*footprint_,std::move(policy),tracker);
      } else {
        planner_=std::make_unique<edge::local::GapFollow>();
      }
      report_pub_=create_publisher<std_msgs::msg::String>("/edge/perception/front_obstacle",10);
    } else if (planner_name=="stop_on_clearance") {
      planner_=std::make_unique<edge::local::StopOnClearance>();
    } else throw std::invalid_argument("unknown local_planner");
    if (require_scan_) scan_=create_subscription<sensor_msgs::msg::LaserScan>("/scan",
        rclcpp::SensorDataQoS().keep_last(1),[this](const sensor_msgs::msg::LaserScan &s){observe_scan(s);});
    if (declare_parameter<bool>("body_runtime", false)) {
      body_.emplace();
      body_command_ = create_publisher<std_msgs::msg::String>("/edge/body/command", 5);
      body_state_ = create_subscription<std_msgs::msg::String>("/edge/body/state", rclcpp::QoS(5).best_effort(),
          [this](const std_msgs::msg::String &state) { body_->observe(state.data, Clock::now()); });
    } else {
      velocity_ = create_publisher<geometry_msgs::msg::Twist>("/cmd_vel", 1);
    }
    odometry_ = create_subscription<nav_msgs::msg::Odometry>(
        "/odom", rclcpp::SensorDataQoS().keep_last(1),
        [this](const nav_msgs::msg::Odometry &sample) { observe(sample); });
    heartbeat_ = create_subscription<std_msgs::msg::String>(
        "/edge/active_operation", rclcpp::QoS(1).best_effort(),
        [this](const std_msgs::msg::String &message) {
          last_heartbeat_id_ = message.data;
          last_heartbeat_at_ = Clock::now();
        });
    action_=server<Navigation>("/edge/navigate_to");
    if (tracker_name=="regulated_pursuit" && policy_name=="yield_crossing")
      path_action_=server<PathAction>("/edge/navigate_path");
    timer_ = create_wall_timer(50ms, [this] { tick(); });
  }

private:
  const edge::NavigationSite site_;

  static std::vector<Point> points(const Navigation::Goal& goal) { return {{goal.x,goal.y}}; }
  static std::vector<Point> points(const PathAction::Goal& goal) {
    std::vector<Point> out;
    if (goal.path.size()<2 || goal.path.size()>256) return out;
    for (const auto& p:goal.path) {
      if (p.z!=0.) return {};
      out.push_back({p.x,p.y});
    }
    return out;
  }
  template<typename Action>
  typename rclcpp_action::Server<Action>::SharedPtr server(const char* name) {
    using Handle=rclcpp_action::ServerGoalHandle<Action>;
    return rclcpp_action::create_server<Action>(this,name,
      [this](const rclcpp_action::GoalUUID&,std::shared_ptr<const typename Action::Goal> goal) {
        const auto route=points(*goal);
        const bool valid=!route.empty() && std::all_of(route.begin(),route.end(),[this](Point p) {
          return site_.contains(p.x, p.y);
        });
        if (reserved_ || inhibited_ || !fresh(Clock::now()) || !valid ||
            (route.size()>1 && std::hypot(route.front().x-pose_->x,route.front().y-pose_->y)>.25) ||
            goal->operation_id.empty() || seen_.contains(goal->operation_id)) {
          event(goal->operation_id,"navigation.goal_rejected","action.rejected","warn","gateway admission rejected");
          return rclcpp_action::GoalResponse::REJECT;
        }
        reserved_=true;
        return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
      },
      [this](std::shared_ptr<Handle> h) {
        if (!active_ || active_->operation_id!=h->get_goal()->operation_id) return rclcpp_action::CancelResponse::REJECT;
        begin_stop(Outcome::canceled,"cancel requested");
        return rclcpp_action::CancelResponse::ACCEPT;
      },
      [this](std::shared_ptr<Handle> h) {
        auto route=points(*h->get_goal());
        auto &motion=active_.emplace();
        motion.operation_id=h->get_goal()->operation_id;
        motion.target=route.back();
        if (route.size()>1) motion.path.emplace(std::move(route));
        motion.is_canceling=[h] {return h->is_canceling();};
        motion.feedback=[h](double remaining) {
          auto fb=std::make_shared<typename Action::Feedback>();
          fb->distance_remaining=remaining; h->publish_feedback(fb);
        };
        motion.complete=[h](const Navigation::Result& r) {
          auto result=std::make_shared<typename Action::Result>();
          result->reached=r.reached; result->final_x=r.final_x; result->final_y=r.final_y; result->detail=r.detail;
          if (h->is_canceling()) h->canceled(result);
          else if (r.reached) h->succeed(result);
          else h->abort(result);
        };
        seen_.insert(motion.operation_id);
        started_ = Clock::now();
        moving_authorized_ = false;
        planner_->reset();
        if (tracker_) tracker_->freeze_reference();
        last_plan_seq_.reset();
        last_decision_.clear();
        last_dynamic_phase_.clear();
        last_dynamic_recheck_ns_.reset();
        stop_outcome_.reset();
        if (body_) body_->start("gw-" + identity_ + std::to_string(++goal_sequence_));
        event(motion.operation_id,"navigation.accepted","action.started","info","gateway accepted target");
      });
  }
  void event(const std::string &op,const char *code,const char *kind,const char *level,const std::string &detail,
             const char *record=nullptr,json_object *value=nullptr) {
    if(op.empty()) return;
    auto mission=op.substr(0,op.rfind("/waypoint/"));
    auto task=mission.substr(0,mission.rfind("/step/"));
    auto id="gateway-"+identity_+std::to_string(++seq_);
    auto *body=json_object_new_object();
    json_object_object_add(body,"schema_version",json_object_new_int(1));
    auto field=[body](const char *key,const std::string &v){json_object_object_add(body,key,json_object_new_string(v.c_str()));};
    field("event_id",id);field("module","edge.gateway");field("code",code);field("level",level);
    field("kind",kind);field("operation_id",op);field("mission_id",mission);field("task_id",task);
    field("trace_id",task);field("detail",detail);
    if(last_event_op_==op)field("parent_event_id",last_event_id_);
    auto *data=json_object_new_object();
    if(record) json_object_object_add(data,record,value);
    if (footprint_ && std::string_view(kind)=="action.started")
      json_object_object_add(data,"vehicle_footprint",edge::local::encode_footprint(*footprint_));
    if (body_ && active_ && op == active_->operation_id) {
      json_object_object_add(data,"body_owner",json_object_new_string(body_->owner().c_str()));
      if (body_->epoch()) {
        json_object_object_add(data,"body_boot",json_object_new_string(body_->boot().c_str()));
        json_object_object_add(data,"body_epoch",json_object_new_uint64(body_->epoch()));
      }
    }
    if(require_scan_ && std::isfinite(front_))json_object_object_add(data,"front_range_m",json_object_new_double(front_));
    if (require_scan_ && scan_stamp_ >= 0) {
      json_object_object_add(data,"scan_stamp_ns",json_object_new_int64(scan_stamp_));
      json_object_object_add(data,"scan_geometry_valid",json_object_new_boolean(std::isfinite(forward_gap_)));
      if (std::isfinite(forward_gap_))
        json_object_object_add(data,"forward_gap_rad",json_object_new_double(forward_gap_));
    }
    if (pose_) json_object_object_add(data,"odom_stamp_ns",json_object_new_int64(pose_->stamp));
    if (pose_ && footprint_ && std::string_view(kind)=="navigation.local_plan") {
      json_object_object_add(data,"measured_linear_mps",json_object_new_double(pose_->forward_velocity));
      json_object_object_add(data,"measured_angular_radps",json_object_new_double(pose_->yaw_velocity));
    }
    if (stop_outcome_ && std::string_view(kind)=="action.result") {
      auto *stop=json_object_new_object();
      auto number=[stop](const char *name,std::int64_t v){json_object_object_add(stop,name,json_object_new_int64(v));};
      number("accepted_samples",stop_samples_);
      if (body_) json_object_object_add(stop,"authority_released",
          json_object_new_boolean(body_->released(stop_started_,Clock::now())));
      number("quiet_samples",still_samples_);
      number("sample_gaps",stop_gaps_);
      number("wall_elapsed_ms",std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now()-stop_started_).count());
      number("quiet_acquisition_ms",still_samples_ ? (pose_->stamp-still_stamp_)/1000000 : 0);
      number("quiet_arrival_ms",still_samples_ ?
             std::chrono::duration_cast<std::chrono::milliseconds>(pose_->arrived-still_since_).count() : 0);
      if (pose_) {
        number("last_odom_age_ms",std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now()-pose_->arrived).count());
        json_object_object_add(stop,"last_linear_mps",json_object_new_double(pose_->linear_speed));
        json_object_object_add(stop,"last_angular_rad_s",json_object_new_double(pose_->angular_speed));
      }
      json_object_object_add(data,"stop_evidence",stop);
    }
    json_object_object_add(body,"data",data);
    std_msgs::msg::String msg;msg.data=json_object_to_json_string_ext(body,JSON_C_TO_STRING_PLAIN);
    json_object_put(body);
    events_->publish(msg);
    last_event_id_=id;last_event_op_=op;
  }
  void observe_scan(const sensor_msgs::msg::LaserScan &s) {
    const auto stamp=std::int64_t(s.header.stamp.sec)*1000000000LL+s.header.stamp.nanosec;
    if (report_pub_ && (s.header.frame_id!=sensor_frame_ || stamp<scan_stamp_)) {
      // A changed frame or regressing clock invalidates the previous motion observation.
      report_.reset();
      if (active_ && !stop_outcome_)
        begin_stop(Outcome::failed,"scan frame or acquisition clock changed","navigation.scan_invalid");
      return;
    }
    if (s.header.frame_id != sensor_frame_ || stamp <= scan_stamp_) return;
    scan_stamp_ = stamp;
    scan_received_ = Clock::now();
    if (report_pub_) {
      edge::local::ScanFrame frame;
      frame.source_id=sensor_id_; frame.frame_id=s.header.frame_id; frame.calibration_ref=calibration_;
      frame.stamp_ns=stamp; frame.seq=++scan_seq_;
      frame.angle_min=s.angle_min; frame.angle_max=s.angle_max; frame.angle_increment=s.angle_increment;
      frame.range_min=s.range_min; frame.range_max=s.range_max; frame.ranges=s.ranges;
      frame.sensor_x_m=sensor_x_; frame.sensor_y_m=sensor_y_; frame.sensor_yaw_rad=sensor_yaw_;
      frame.range_resolution_m=range_resolution_;
      report_=edge::local::assess(frame,report_ ? &*report_ : nullptr,footprint_ ? &*footprint_ : nullptr);
      report_->report_ref=identity_+report_->report_ref;
      auto *wire=edge::local::encode_report(*report_,s.header.frame_id,footprint_ ? &*footprint_ : nullptr);
      std_msgs::msg::String msg; msg.data=json_object_to_json_string_ext(wire,JSON_C_TO_STRING_PLAIN);
      report_pub_->publish(msg);
      if (active_)
        event(active_->operation_id,"perception.front_obstacle","perception.front_obstacle",
              report_->valid?"info":"warn",report_->reason,"report",json_object_get(wire));
      json_object_put(wire);
      if (tracker_) pair_dynamic_scan();
    }
    front_ = std::numeric_limits<double>::infinity();
    forward_gap_ = std::numeric_limits<double>::infinity();
    if (s.ranges.empty() || !std::isfinite(s.angle_min) || !std::isfinite(s.angle_max) ||
        !std::isfinite(s.angle_increment) || s.angle_increment <= 0 ||
        !std::isfinite(s.range_min) || !std::isfinite(s.range_max) ||
        s.range_min < 0 || s.range_min >= clearance_m || s.range_max <= clearance_m) return;
    const double last_angle = s.angle_min + (s.ranges.size()-1) * double(s.angle_increment);
    if (std::abs(last_angle - s.angle_max) > .5 * s.angle_increment) return;

    double previous = -front_half_angle;
    double largest_gap = 0;
    for(std::size_t i=0;i<s.ranges.size();++i){
      const double angle = s.angle_min + i * double(s.angle_increment);
      if (std::abs(angle) > front_half_angle) continue;
      const double distance = s.ranges[i];
      // REP-117: +Inf is no return, -Inf means too close, NaN means unknown.
      if (std::isnan(distance) ||
          (std::isfinite(distance) && (distance < s.range_min || distance > s.range_max))) continue;
      front_ = std::min(front_, std::max(0., distance));
      largest_gap = std::max(largest_gap, angle - previous);
      previous = angle;
    }
    forward_gap_ = std::max(largest_gap, front_half_angle - previous);
  }
  enum class Outcome { reached, canceled, failed };
  struct Pose {
    double x, y, yaw, linear_speed, angular_speed, forward_velocity, yaw_velocity;
    std::int64_t stamp;
    Clock::time_point arrived;
  };
  static constexpr double target_tolerance = 0.055;
  static constexpr double clearance_m = .42;
  static constexpr double front_half_angle = .42;
  static constexpr double max_ray_gap = .12;
  static constexpr auto heartbeat_timeout = 1500ms;
  static constexpr auto settle_window = 300ms;

  bool fresh(Clock::time_point now) const {
    return pose_ && now - pose_->arrived < 900ms;
  }

  void observe(const nav_msgs::msg::Odometry &sample) {
    const auto &p = sample.pose.pose.position;
    const auto &q = sample.pose.pose.orientation;
    const auto &twist = sample.twist.twist;
    const auto quaternion_norm = q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w;
    const double speed = std::hypot(twist.linear.x, twist.linear.y);
    const double yaw_rate = std::abs(twist.angular.z);
    const auto stamp = std::int64_t(sample.header.stamp.sec) * 1000000000LL +
                       sample.header.stamp.nanosec;
    if (sample.header.frame_id != "odom" || sample.child_frame_id != "base_link" ||
        !std::isfinite(p.x) || !std::isfinite(p.y) || !std::isfinite(quaternion_norm) ||
        std::abs(quaternion_norm - 1.0) > 0.05 || !std::isfinite(speed) ||
        !std::isfinite(yaw_rate) || (pose_ && stamp <= pose_->stamp)) return;
    const double yaw = std::atan2(2.0 * (q.w*q.z + q.x*q.y), 1.0 - 2.0 * (q.y*q.y + q.z*q.z));
    const auto now = Clock::now();
    if (stop_outcome_) {
      ++stop_samples_;
      // A few quiet wheel samples can occur while the body is still settling.
      // Count a continuous interval in both acquisition time and arrival time.
      if (!pose_ || now - pose_->arrived > 200ms || stamp - pose_->stamp > 200'000'000) {
        ++stop_gaps_;
        still_samples_ = 0;
      }
      if (speed < 0.025 && yaw_rate < 0.05) {
        if (still_samples_ == 0) {
          still_since_ = now;
          still_stamp_ = stamp;
        }
        ++still_samples_;
      } else still_samples_ = 0;
    }
    pose_ = Pose{p.x, p.y, yaw, speed, yaw_rate, twist.linear.x, twist.angular.z, stamp, now};
    if (tracker_) {
      pose_history_.push_back(*pose_);
      while (pose_history_.size()>40 || stamp-pose_history_.front().stamp>600'000'000) pose_history_.pop_front();
      pair_dynamic_scan();
    }
  }

  void pair_dynamic_scan() {
    if (!report_ || (dynamic_report_ && dynamic_report_->scan_ref==report_->report_ref)) return;
    const auto stamp=report_->stamp_ns;
    const Pose *before=nullptr,*after=nullptr;
    for (const auto& p:pose_history_) {
      if (std::abs(p.stamp-stamp)>90'000'000) continue;
      if (p.stamp<=stamp) before=&p;
      if (p.stamp>=stamp) {after=&p;break;}
    }
    if (!before && !after) return; // A mismatched dynamic_report cannot authorize motion.
    const Pose& p=!before ? *after : !after ? *before :
      stamp-before->stamp<=after->stamp-stamp ? *before : *after;
    edge::local::ScanPose projected{{p.x,p.y},p.yaw,p.stamp};
    if (before && after && before!=after) {
      const double t=double(stamp-before->stamp)/(after->stamp-before->stamp);
      projected.position={std::lerp(before->x,after->x,t),std::lerp(before->y,after->y,t)};
      projected.yaw=before->yaw+t*std::remainder(after->yaw-before->yaw,2.*std::acos(-1.));
    } else {
      const double dt=(stamp-p.stamp)*1e-9,dyaw=dt*p.yaw_velocity,mid=p.yaw+dyaw/2.;
      projected.position={p.x+dt*p.forward_velocity*std::cos(mid),p.y+dt*p.forward_velocity*std::sin(mid)};
      projected.yaw=p.yaw+dyaw;
    }
    dynamic_report_=tracker_->observe(*report_,projected);
    auto* observation=edge::local::encode_dynamic(*dynamic_report_);
    std_msgs::msg::String value; value.data=json_object_to_json_string_ext(observation,JSON_C_TO_STRING_PLAIN);
    dynamic_pub_->publish(value);
    if (active_) event(active_->operation_id,"perception.dynamic_obstacles","perception.dynamic_obstacles",
                       dynamic_report_->valid?"info":"warn",dynamic_report_->reason,"dynamic",json_object_get(observation));
    json_object_put(observation);
  }

  void publish_body(const std::string& wire) {
    if (wire.empty()) return;
    std_msgs::msg::String msg;
    msg.data = wire;
    body_command_->publish(msg);
  }

  void legacy_velocity(double linear, double angular) {
    geometry_msgs::msg::Twist command;
    command.linear.x = linear;
    command.angular.z = angular;
    velocity_->publish(command);
  }

  void stop_output() {
    if (body_) publish_body(body_->stop());
    else legacy_velocity(0.0, 0.0);
  }

  void begin_stop(Outcome outcome, const std::string &reason,const char *code="navigation.cancel_requested") {
    if (!stop_outcome_) {
      stop_started_ = Clock::now();
      still_samples_ = 0;
      stop_samples_ = stop_gaps_ = 0;
      if (report_pub_) {
        edge::local::LocalPlan halt{};
        halt.decision="stop"; halt.reason_code=code;
        if (footprint_) halt.footprint_ref=footprint_->profile_id;
        if (report_) { halt.report_ref=report_->report_ref; halt.ttc_s=report_->ttc_s; }
        event(active_->operation_id,"navigation.local_stop","navigation.local_stop",
              outcome==Outcome::failed?"warn":"info",reason,"plan",edge::local::encode_plan(halt));
      }
    }
    stop_outcome_ = outcome;
    stop_reason_ = reason;
    stop_code_=code;
    event(active_->operation_id,code,"action.braking",outcome==Outcome::failed?"warn":"info",reason);
    stop_output();
  }

  double distance() const {
    return std::hypot(active_->target.x-pose_->x,active_->target.y-pose_->y);
  }

  void finish(bool stopped) {
    auto result = std::make_shared<Navigation::Result>();
    result->final_x = pose_ ? pose_->x : std::numeric_limits<double>::quiet_NaN();
    result->final_y = pose_ ? pose_->y : std::numeric_limits<double>::quiet_NaN();
    result->reached = stopped && *stop_outcome_ == Outcome::reached &&
                      !active_->is_canceling() && distance() <= target_tolerance;
    if (!stopped) {
      inhibited_ = true;
      result->detail = "UNKNOWN: stop unconfirmed after " + stop_reason_ + "; gateway inhibited";
    } else if (result->reached) result->detail = "reached; stationary odometry confirmed";
    else result->detail = stop_reason_ + "; stationary odometry confirmed";
    const auto terminal_code=stopped?stop_code_:"navigation.stop_unconfirmed";
    result->detail+=" [code:"+terminal_code+"]";
    event(active_->operation_id,terminal_code.c_str(),"action.result",stopped?"info":"error",result->detail);
    RCLCPP_INFO(get_logger(), "%s: %s", active_->operation_id.c_str(),
                result->detail.c_str());
    active_->complete(*result);
    if (stopped && tracker_) tracker_->begin_idle_reference();
    active_.reset();
    reserved_ = false;
    stop_outcome_.reset();
  }

  void tick() {
    if (!active_) {
      if (!body_) legacy_velocity(0.0, 0.0);
      return;
    }
    const auto now = Clock::now();
    if (active_->is_canceling() && stop_outcome_ != Outcome::canceled)
      begin_stop(Outcome::canceled, "cancel requested");
    if (stop_outcome_) {
      stop_output();
      if ((!body_ || body_->released(stop_started_, now)) &&
          fresh(now) && still_samples_ >= 3 && pose_->arrived - still_since_ >= settle_window &&
          std::chrono::nanoseconds(pose_->stamp - still_stamp_) >= settle_window) finish(true);
      else if (now - stop_started_ > 3s) finish(false);
      return;
    }
    if (!fresh(now)) {
      begin_stop(Outcome::failed, "odometry timeout","navigation.odom_timeout");
      return;
    }
    if (now - started_ > site_.motion_deadline) {
      begin_stop(Outcome::failed, "navigation deadline","navigation.deadline");
      return;
    }
    if (last_heartbeat_id_ != active_->operation_id || now - last_heartbeat_at_ >= heartbeat_timeout) {
      if (!body_) legacy_velocity(0.0, 0.0);
      if (moving_authorized_ || now - started_ >= heartbeat_timeout)
        begin_stop(Outcome::failed, "operation heartbeat expired","navigation.heartbeat_expired");
      return;
    }
    moving_authorized_ = true;
    if (body_) {
      const auto access = body_->access(now);
      if (access == BodyAuthority::Access::lost) {
        begin_stop(Outcome::failed, "actuator authority lost", "navigation.body_authority_lost");
        return;
      }
      if (access != BodyAuthority::Access::held) {
        if (now - started_ >= 3s)
          begin_stop(Outcome::failed, "actuator authority unavailable", "navigation.body_authority_timeout");
        else if (access == BodyAuthority::Access::opening)
          publish_body(body_->open());
        return;
      }
    }
    const double remaining = distance();
    auto target=active_->target;
    double path_remaining=remaining;
    if (active_->path) {
      const auto reference=active_->path->target({pose_->x,pose_->y});
      target=reference.point; path_remaining=reference.remaining;
    }
    const bool arrived=remaining<=target_tolerance && path_remaining<=target_tolerance;
    active_->feedback(std::max(remaining,path_remaining));
    if (!report_pub_ && arrived) {
      begin_stop(Outcome::reached, "braked at goal","navigation.arrived");
      return;
    }
    const double desired = std::atan2(target.y-pose_->y,target.x-pose_->x);
    const double delta = desired - pose_->yaw;
    const double error = std::atan2(std::sin(delta), std::cos(delta));
    if(require_scan_ && (scan_stamp_<0 || now-scan_received_>1s)){
      begin_stop(Outcome::failed,"laser scan unavailable","navigation.scan_stale");return;
    }
    if (require_scan_ && std::abs(scan_stamp_ - pose_->stamp) > 1'000'000'000LL) {
      begin_stop(Outcome::failed,"laser and odometry acquisition times differ by over one second",
                 "navigation.scan_time_mismatch");return;
    }
    const bool wants_forward=std::abs(error)<=0.35 && !arrived;
    if(require_scan_ && wants_forward && front_<clearance_m){
      begin_stop(Outcome::failed,"forward clearance below 0.42 m","navigation.path_blocked");return;
    }
    if (require_scan_ && forward_gap_ > max_ray_gap) {
      begin_stop(Outcome::failed,"invalid scan geometry or insufficient forward coverage",
                 "navigation.scan_invalid");return;
    }
    if (report_pub_ && (!report_ || !report_->valid)) {
      begin_stop(Outcome::failed,"local obstacle observation invalid","navigation.scan_invalid");return;
    }
    if (report_pub_ && arrived) {
      begin_stop(Outcome::reached,"braked at goal","navigation.arrived");return;
    }
    edge::local::PlanInput input{};
    input.goal_distance_m=std::hypot(target.x-pose_->x,target.y-pose_->y); input.goal_bearing_rad=error;
    input.measured_linear_mps=pose_->forward_velocity; input.measured_angular_radps=pose_->yaw_velocity;
    input.measured_odom_stamp_ns=pose_->stamp;
    input.dynamic_obstacles=dynamic_report_ ? &*dynamic_report_ : nullptr;
    auto plan=planner_->plan(input,report_ ? &*report_ : nullptr);
    if (active_->path) plan.tracking_target=target;
    if (!std::isfinite(plan.linear_mps) || !std::isfinite(plan.angular_radps)) {
      begin_stop(Outcome::failed,"invalid planner output","navigation.local_plan_invalid");return;
    }
    if (require_scan_ && plan.linear_mps>0.0 && front_<clearance_m) {
      begin_stop(Outcome::failed,"forward clearance below 0.42 m","navigation.path_blocked");return;
    }
    const double linear=std::clamp(plan.linear_mps,0.,0.32);
    const double angular=std::clamp(plan.angular_radps,-1.1,1.1);
    if (report_pub_ && (!last_plan_seq_ || *last_plan_seq_!=report_->scan_seq || plan.decision!=last_decision_)) {
      plan.linear_mps=linear; plan.angular_radps=angular;
      event(active_->operation_id,"navigation.local_plan","navigation.local_plan","info",
            plan.reason_code,"plan",edge::local::encode_plan(plan));
      if (plan.dynamic_phase=="yielding" && last_dynamic_phase_!="yielding")
        event(active_->operation_id,"navigation.dynamic_yield","navigation.dynamic_yield","info",
              plan.reason_code,"plan",edge::local::encode_plan(plan));
      const bool dynamic_released=plan.dynamic_phase=="cleared" || plan.dynamic_phase=="observation_resolved";
      if (plan.dynamic_recheck) {
        const auto stamp=plan.dynamic_recheck->scan_stamp_ns;
        if (!last_dynamic_recheck_ns_ || stamp-*last_dynamic_recheck_ns_>=1'000'000'000LL ||
            dynamic_released || plan.dynamic_clear_samples==1) {
          event(active_->operation_id,"navigation.dynamic_recheck","navigation.dynamic_recheck","info",
                "stationary odometry and observed corridor recheck","plan",edge::local::encode_plan(plan));
          last_dynamic_recheck_ns_=stamp;
        }
      }
      if (dynamic_released && plan.dynamic_phase!=last_dynamic_phase_)
        event(active_->operation_id,"navigation.dynamic_clear","navigation.dynamic_clear","info",
              "observed corridor clear with stationary odometry and no conflicting tracks","plan",edge::local::encode_plan(plan));
      if (!plan.dynamic_phase.empty()) last_dynamic_phase_=plan.dynamic_phase;
      if (plan.decision!=last_decision_ && (plan.decision=="avoid_left" || plan.decision=="avoid_right"))
        event(active_->operation_id,"navigation.local_avoid","navigation.local_avoid","info",
              plan.reason_code,"plan",edge::local::encode_plan(plan));
      last_plan_seq_=report_->scan_seq; last_decision_=plan.decision;
    }
    if (report_pub_ && plan.decision=="stop") {
      begin_stop(Outcome::failed,plan.reason_code,plan.reason_code.c_str());return;
    }
    if (body_) publish_body(body_->drive(linear, angular));
    else legacy_velocity(linear, angular);
  }

  bool reserved_ = false;
  bool require_scan_=false;
  std::unique_ptr<edge::local::LocalPlannerPort> planner_;
  std::unique_ptr<edge::local::DynamicObserverPort> tracker_;
  std::optional<edge::local::DynamicObstacleReport> dynamic_report_;
  std::string last_dynamic_phase_;
  std::optional<std::int64_t> last_dynamic_recheck_ns_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr dynamic_pub_;
  std::optional<edge::local::VehicleFootprint> footprint_;
  std::optional<edge::local::FrontObstacleReport> report_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr report_pub_;
  std::string sensor_id_,sensor_frame_="laser_link",calibration_,last_decision_;
  double sensor_x_=0.,sensor_y_=0.,sensor_yaw_=0.,range_resolution_=0.;
  uint64_t scan_seq_=0;
  std::optional<uint64_t> last_plan_seq_;
  std::optional<BodyAuthority> body_;
  std::uint64_t goal_sequence_ = 0;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr body_command_;
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr body_state_;
  std::string identity_,last_event_id_,last_event_op_,stop_code_;
  std::uint64_t seq_=0;
  std::int64_t scan_stamp_=-1;
  double front_=std::numeric_limits<double>::infinity();
  double forward_gap_=std::numeric_limits<double>::infinity();
  Clock::time_point scan_received_;
  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr scan_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr events_;
  bool inhibited_ = false;
  bool moving_authorized_ = false;
  unsigned still_samples_ = 0;
  unsigned stop_samples_ = 0, stop_gaps_ = 0;
  Clock::time_point still_since_;
  std::int64_t still_stamp_ = 0;
  std::set<std::string> seen_;
  std::string last_heartbeat_id_, stop_reason_;
  Clock::time_point last_heartbeat_at_, started_, stop_started_;
  std::optional<Pose> pose_;
  std::deque<Pose> pose_history_;
  std::optional<Outcome> stop_outcome_;
  std::optional<Motion> active_;
  rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr velocity_;
  rclcpp::Subscription<nav_msgs::msg::Odometry>::SharedPtr odometry_;
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr heartbeat_;
  rclcpp_action::Server<Navigation>::SharedPtr action_;
  rclcpp_action::Server<PathAction>::SharedPtr path_action_;
  rclcpp::TimerBase::SharedPtr timer_;
};

int main(int argc, char **argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<EdgeGateway>());
  rclcpp::shutdown();
  return 0;
}
