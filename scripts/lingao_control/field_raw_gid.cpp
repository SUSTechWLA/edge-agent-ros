// Read-only Humble observer. Stream is line buffered. Parent must fail closed
// on FOREIGN, FAULT, EOF, or missing heartbeat. No Twist publisher is created.
#include <algorithm>
#include <array>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <geometry_msgs/msg/twist.hpp>
#include <rclcpp/rclcpp.hpp>
#include <rmw/rmw.h>

using Clock = std::chrono::steady_clock;
using Twist = geometry_msgs::msg::Twist;
using Bytes = std::array<uint8_t, RMW_GID_STORAGE_SIZE>;
using Map = std::map<std::string, Bytes>;
constexpr const char * own = "field_twist_one_second";
constexpr const char * bridge = "lingao_mqtt_bridge";

int64_t mono() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count();
}

std::string hex(const Bytes &gid) {
  std::ostringstream out;
  for (auto b : gid) out << std::hex << std::setw(2) << std::setfill('0') << unsigned(b);
  return out.str();
}

class Observer : public rclcpp::Node {
public:
  Observer() : Node("field_twist_gid_observer", rclcpp::NodeOptions()
      .start_parameter_event_publisher(false).start_parameter_services(false).enable_rosout(false)) {
    sub_ = create_subscription<Twist>("/cmd_vel", rclcpp::QoS(rclcpp::KeepLast(256)).reliable().durability_volatile(),
      [this](Twist::ConstSharedPtr, const rclcpp::MessageInfo &info) {
        if (failed_) return;
        const auto &gid = info.get_rmw_message_info().publisher_gid;
        if (!ready_ || !gid.implementation_identifier) { fail("unknown_sample_before_ready"); return; }
        // Never infer provenance from velocity or from graph order.
        if (matches(pin_.at(own), gid)) { line("OWN"); return; }
        if (matches(pin_.at(bridge), gid)) { failed_=true; line("FOREIGN"); return; }
        fail("sample_gid_not_in_frozen_graph");
      });
    timer_=create_wall_timer(std::chrono::milliseconds(25),[this](){ check_graph(); });
  }

private:
  void line(const std::string &label) {
    std::cout << label << ' ' << mono() << std::endl;
  }
  void fail(const std::string &why) {
    if (!failed_) line("FAULT " + why);
    failed_=true;
  }
  bool matches(const Bytes &expected, const rmw_gid_t &actual) const {
    rmw_gid_t ref{};
    ref.implementation_identifier=rmw_get_implementation_identifier();
    std::copy(expected.begin(),expected.end(),ref.data);
    bool equal=false;
    if (rmw_compare_gids_equal(&ref,&actual,&equal)!=RMW_RET_OK)
      throw std::runtime_error("rmw_gid_comparison_failed");
    return equal;
  }
  void check_graph() {
    if (failed_) return;
    Map current;
    for (const auto &e : get_publishers_info_by_topic("/cmd_vel")) {
      const auto &name=e.node_name(); const auto &gid=e.endpoint_gid();
      if (e.node_namespace() != "/" || (name!=own && name!=bridge) ||
          e.topic_type()!="geometry_msgs/msg/Twist" ||
          e.qos_profile().get_rmw_qos_profile().reliability!=RMW_QOS_POLICY_RELIABILITY_RELIABLE ||
          gid==Bytes{} || !current.emplace(name,gid).second) {
        if (ready_) fail("unexpected_or_duplicate_endpoint");
        last_.clear(); return;
      }
    }
    const bool matched=current.size()==2 && current.at(own)!=current.at(bridge)
                       && sub_->get_publisher_count()==2;
    if (ready_) {
      if (!matched || current!=pin_) { fail("registered_writers_changed"); return; }
      line("ALIVE"); return;
    }
    if (!matched || current!=last_) { last_=current; since_=Clock::now(); return; }
    if (Clock::now()-since_>=std::chrono::milliseconds(600)) {
      pin_=current; ready_=true;
      line("READY " + hex(pin_.at(own)) + " " + hex(pin_.at(bridge)));
    }
  }
  bool failed_{false}, ready_{false};
  Clock::time_point since_{Clock::now()};
  Map last_,pin_;
  rclcpp::Subscription<Twist>::SharedPtr sub_;
  rclcpp::TimerBase::SharedPtr timer_;
};

int main(int argc, char **argv) {
  rclcpp::init(argc,argv);
  try { rclcpp::spin(std::make_shared<Observer>()); }
  catch (const std::exception &err) {
    std::cout << "FAULT " << err.what() << ' ' << mono() << std::endl;
    return 1;
  }
  rclcpp::shutdown();
}
