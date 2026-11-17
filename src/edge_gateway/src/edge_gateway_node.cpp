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
#include <json-c/json.h>

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
using GoalHandle = rclcpp_action::ServerGoalHandle<Navigation>;

class EdgeGateway final : public rclcpp::Node {
public:
  EdgeGateway() : rclcpp::Node("edge_gateway") {
    events_=create_publisher<std_msgs::msg::String>("/edge/events",100);
    std::random_device random;
    std::ostringstream instance;
    for (int i=0;i<4;++i) instance<<std::hex<<random()<<'-';
    identity_=instance.str();
    require_scan_=declare_parameter<bool>("require_scan",false);
    if (require_scan_) scan_=create_subscription<sensor_msgs::msg::LaserScan>("/scan",
        rclcpp::SensorDataQoS().keep_last(1),[this](const sensor_msgs::msg::LaserScan &s){observe_scan(s);});
    velocity_ = create_publisher<geometry_msgs::msg::Twist>("/cmd_vel", 1);
    odometry_ = create_subscription<nav_msgs::msg::Odometry>(
        "/odom", rclcpp::SensorDataQoS().keep_last(1),
        [this](const nav_msgs::msg::Odometry &sample) { observe(sample); });
    heartbeat_ = create_subscription<std_msgs::msg::String>(
        "/edge/active_operation", rclcpp::QoS(1).best_effort(),
        [this](const std_msgs::msg::String &message) {
          last_heartbeat_id_ = message.data;
          last_heartbeat_at_ = Clock::now();
        });
    action_ = rclcpp_action::create_server<Navigation>(
        this, "/edge/navigate_to",
        [this](const rclcpp_action::GoalUUID &,
               const std::shared_ptr<const Navigation::Goal> goal) {
          if (reserved_ || inhibited_ || !fresh(Clock::now()) ||
              goal->operation_id.empty() || seen_.contains(goal->operation_id) ||
              !std::isfinite(goal->x) || !std::isfinite(goal->y) ||
              std::abs(goal->x) > 4.0 || std::abs(goal->y) > 4.0) {
            RCLCPP_WARN(get_logger(), "Refused navigation goal %s", goal->operation_id.c_str());
            event(goal->operation_id,"navigation.goal_rejected","action.rejected","warn","gateway admission rejected");
            return rclcpp_action::GoalResponse::REJECT;
          }
          reserved_ = true;
          return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
        },
        [this](const std::shared_ptr<GoalHandle> handle) {
          if (handle != active_) return rclcpp_action::CancelResponse::REJECT;
          begin_stop(Outcome::canceled, "cancel requested");
          return rclcpp_action::CancelResponse::ACCEPT;
        },
        [this](const std::shared_ptr<GoalHandle> handle) {
          active_ = handle;
          seen_.insert(handle->get_goal()->operation_id);
          started_ = Clock::now();
          moving_authorized_ = false;
          stop_outcome_.reset();
          event(handle->get_goal()->operation_id,"navigation.accepted","action.started","info","gateway accepted target");
          RCLCPP_INFO(get_logger(), "Tracking %s to (%.3f, %.3f)",
                      handle->get_goal()->operation_id.c_str(), handle->get_goal()->x,
                      handle->get_goal()->y);
        });
    timer_ = create_wall_timer(50ms, [this] { tick(); });
  }

private:
  void event(const std::string &op,const char *code,const char *kind,const char *level,const std::string &detail) {
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
    if(require_scan_ && std::isfinite(front_))json_object_object_add(data,"front_range_m",json_object_new_double(front_));
    json_object_object_add(body,"data",data);
    std_msgs::msg::String msg;msg.data=json_object_to_json_string_ext(body,JSON_C_TO_STRING_PLAIN);
    json_object_put(body);
    events_->publish(msg);
    last_event_id_=id;last_event_op_=op;
  }
  void observe_scan(const sensor_msgs::msg::LaserScan &s) {
    const auto stamp=std::int64_t(s.header.stamp.sec)*1000000000LL+s.header.stamp.nanosec;
    if(s.header.frame_id!="laser_link" || stamp<=scan_stamp_ || !(s.angle_increment>0) ||
       !(s.range_max>0.42) || !std::isfinite(s.range_min))return;
    double nearest=std::numeric_limits<double>::infinity();
    unsigned valid=0;
    for(std::size_t i=0;i<s.ranges.size();++i){
      double angle=s.angle_min+i*double(s.angle_increment);
      double distance=s.ranges[i];
      if(std::abs(angle)>0.42 || std::isnan(distance) || distance<s.range_min)continue;
      ++valid;nearest=std::min(nearest,distance);
    }
    if(valid>=3){scan_received_=Clock::now();scan_stamp_=stamp;front_=nearest;}
  }
  enum class Outcome { reached, canceled, failed };
  struct Pose {
    double x, y, yaw, linear_speed, angular_speed;
    std::int64_t stamp;
    Clock::time_point arrived;
  };
  static constexpr double target_tolerance = 0.055;
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
      // A few quiet wheel samples can occur while the body is still settling.
      // Count a continuous interval in both acquisition time and arrival time.
      if (!pose_ || now - pose_->arrived > 200ms || stamp - pose_->stamp > 200'000'000)
        still_samples_ = 0;
      if (speed < 0.025 && yaw_rate < 0.05) {
        if (still_samples_ == 0) {
          still_since_ = now;
          still_stamp_ = stamp;
        }
        ++still_samples_;
      } else still_samples_ = 0;
    }
    pose_ = Pose{p.x, p.y, yaw, speed, yaw_rate, stamp, now};
  }

  void drive(double linear, double angular) {
    geometry_msgs::msg::Twist command;
    command.linear.x = linear;
    command.angular.z = angular;
    velocity_->publish(command);
  }

  void begin_stop(Outcome outcome, const std::string &reason,const char *code="navigation.cancel_requested") {
    if (!stop_outcome_) {
      stop_started_ = Clock::now();
      still_samples_ = 0;
    }
    stop_outcome_ = outcome;
    stop_reason_ = reason;
    stop_code_=code;
    event(active_->get_goal()->operation_id,code,"action.braking",outcome==Outcome::failed?"warn":"info",reason);
    drive(0.0, 0.0);
  }

  double distance() const {
    const auto goal = active_->get_goal();
    return std::hypot(goal->x - pose_->x, goal->y - pose_->y);
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
    event(active_->get_goal()->operation_id,terminal_code.c_str(),"action.result",stopped?"info":"error",result->detail);
    RCLCPP_INFO(get_logger(), "%s: %s", active_->get_goal()->operation_id.c_str(),
                result->detail.c_str());
    if (active_->is_canceling()) active_->canceled(result);
    else if (result->reached) active_->succeed(result);
    else active_->abort(result);
    active_.reset();
    reserved_ = false;
    stop_outcome_.reset();
  }

  void tick() {
    if (!active_) {
      drive(0.0, 0.0);
      return;
    }
    const auto now = Clock::now();
    if (active_->is_canceling() && stop_outcome_ != Outcome::canceled)
      begin_stop(Outcome::canceled, "cancel requested");
    if (stop_outcome_) {
      drive(0.0, 0.0);
      if (fresh(now) && still_samples_ >= 3 && pose_->arrived - still_since_ >= settle_window &&
          std::chrono::nanoseconds(pose_->stamp - still_stamp_) >= settle_window) finish(true);
      else if (now - stop_started_ > 3s) finish(false);
      return;
    }
    if (!fresh(now)) {
      begin_stop(Outcome::failed, "odometry timeout","navigation.odom_timeout");
      return;
    }
    if (now - started_ > 75s) {
      begin_stop(Outcome::failed, "navigation deadline","navigation.deadline");
      return;
    }
    const auto goal = active_->get_goal();
    if (last_heartbeat_id_ != goal->operation_id || now - last_heartbeat_at_ >= heartbeat_timeout) {
      drive(0.0, 0.0);
      if (moving_authorized_ || now - started_ >= heartbeat_timeout)
        begin_stop(Outcome::failed, "operation heartbeat expired","navigation.heartbeat_expired");
      return;
    }
    moving_authorized_ = true;
    const double remaining = distance();
    auto feedback = std::make_shared<Navigation::Feedback>();
    feedback->distance_remaining = remaining;
    active_->publish_feedback(feedback);
    if (remaining <= target_tolerance) {
      begin_stop(Outcome::reached, "braked at goal","navigation.arrived");
      return;
    }
    const double desired = std::atan2(goal->y - pose_->y, goal->x - pose_->x);
    const double delta = desired - pose_->yaw;
    const double error = std::atan2(std::sin(delta), std::cos(delta));
    const double angular = std::clamp(2.3 * error, -1.1, 1.1);
    const double linear = std::abs(error) > 0.35 ? 0.0 : std::min(0.32, 1.1 * remaining);
    if(require_scan_ && (scan_stamp_<0 || now-scan_received_>1s)){
      begin_stop(Outcome::failed,"laser scan unavailable","navigation.scan_stale");return;
    }
    if(require_scan_ && linear>0 && front_<0.42){
      begin_stop(Outcome::failed,"forward clearance below 0.42 m","navigation.path_blocked");return;
    }
    drive(linear, angular);
  }

  bool reserved_ = false;
  bool require_scan_=false;
  std::string identity_,last_event_id_,last_event_op_,stop_code_;
  std::uint64_t seq_=0;
  std::int64_t scan_stamp_=-1;
  double front_=std::numeric_limits<double>::infinity();
  Clock::time_point scan_received_;
  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr scan_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr events_;
  bool inhibited_ = false;
  bool moving_authorized_ = false;
  unsigned still_samples_ = 0;
  Clock::time_point still_since_;
  std::int64_t still_stamp_ = 0;
  std::set<std::string> seen_;
  std::string last_heartbeat_id_, stop_reason_;
  Clock::time_point last_heartbeat_at_, started_, stop_started_;
  std::optional<Pose> pose_;
  std::optional<Outcome> stop_outcome_;
  std::shared_ptr<GoalHandle> active_;
  rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr velocity_;
  rclcpp::Subscription<nav_msgs::msg::Odometry>::SharedPtr odometry_;
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr heartbeat_;
  rclcpp_action::Server<Navigation>::SharedPtr action_;
  rclcpp::TimerBase::SharedPtr timer_;
};

int main(int argc, char **argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<EdgeGateway>());
  rclcpp::shutdown();
  return 0;
}
