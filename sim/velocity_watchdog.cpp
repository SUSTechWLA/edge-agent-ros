#include <chrono>
#include <cmath>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>

#include <gz/msgs/twist.pb.h>
#include <gz/plugin/Register.hh>
#include <gz/sim/System.hh>
#include <gz/transport/Node.hh>

namespace edge_sim {
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

// DiffDrive holds its last velocity until another velocity arrives. Keep this
// guard in the simulator, on the actuator side of the ROS transport boundary.
class VelocityWatchdog final : public gz::sim::System,
                               public gz::sim::ISystemConfigure,
                               public gz::sim::ISystemPreUpdate {
 public:
  void Configure(const gz::sim::Entity &, const std::shared_ptr<const sdf::Element> &,
                 gz::sim::EntityComponentManager &, gz::sim::EventManager &) override {
    guarded_ = node_.Advertise<gz::msgs::Twist>("/edge_rover/guarded_cmd_vel");
    if (!guarded_ || !node_.Subscribe("/cmd_vel", &VelocityWatchdog::on_command, this)) {
      throw std::runtime_error("could not establish Gazebo velocity watchdog topics");
    }
  }

  void PreUpdate(const gz::sim::UpdateInfo &info, gz::sim::EntityComponentManager &) override {
    std::lock_guard<std::mutex> lock(mutex_);
    if (info.paused || info.dt < Clock::duration::zero()) {
      arrival_.reset();
      last_publish_.reset();
      guarded_.Publish(gz::msgs::Twist{});
      return;
    }
    sim_time_ = info.simTime;
    // The physical model advances with sim time. Wall-time age also prevents
    // an old command from being revived after a suspended simulation.
    if (arrival_ && (sim_time_ - arrival_->sim >= 300ms ||
                     Clock::now() - arrival_->wall >= 300ms)) {
      arrival_.reset();
    }
    if (last_publish_ && sim_time_ - *last_publish_ < 20ms) return;
    last_publish_ = sim_time_;
    guarded_.Publish(arrival_ ? command_ : gz::msgs::Twist{});
  }

 private:
  struct Arrival { Clock::duration sim; Clock::time_point wall; };

  void on_command(const gz::msgs::Twist &message) {
    std::lock_guard<std::mutex> lock(mutex_);
    const double linear = message.linear().x();
    const double angular = message.angular().z();
    if (!std::isfinite(linear) || !std::isfinite(angular)) {
      arrival_.reset();
      return;
    }
    command_.mutable_linear()->set_x(linear);
    command_.mutable_angular()->set_z(angular);
    arrival_ = Arrival{sim_time_, Clock::now()};
  }

  std::mutex mutex_;
  Clock::duration sim_time_{};
  std::optional<Clock::duration> last_publish_;
  std::optional<Arrival> arrival_;
  gz::msgs::Twist command_;
  gz::transport::Node::Publisher guarded_;
  gz::transport::Node node_;
};
}  // namespace edge_sim

GZ_ADD_PLUGIN(edge_sim::VelocityWatchdog, gz::sim::System,
              gz::sim::ISystemConfigure, gz::sim::ISystemPreUpdate)
