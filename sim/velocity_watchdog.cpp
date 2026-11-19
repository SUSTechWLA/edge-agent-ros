#include <chrono>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>

#include <gz/msgs/odometry.pb.h>
#include <gz/msgs/stringmsg.pb.h>
#include <gz/msgs/twist.pb.h>
#include <gz/plugin/Register.hh>
#include <gz/sim/System.hh>
#include <gz/transport/Node.hh>
#include <sdf/Element.hh>

namespace edge_sim {
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

// DiffDrive holds its last velocity until another velocity arrives. Keep this
// guard in the simulator, on the actuator side of the ROS transport boundary.
class VelocityWatchdog final : public gz::sim::System,
                               public gz::sim::ISystemConfigure,
                               public gz::sim::ISystemPreUpdate {
 public:
  void Configure(const gz::sim::Entity &, const std::shared_ptr<const sdf::Element> &config,
                 gz::sim::EntityComponentManager &, gz::sim::EventManager &) override {
    guarded_ = node_.Advertise<gz::msgs::Twist>("/edge_rover/guarded_cmd_vel");
    if (!guarded_) throw std::runtime_error("could not advertise guarded velocity");
    body_runtime_ = config->Get<bool>("body_runtime", false).first;
    if (body_runtime_) {
      std::random_device random;
      std::ostringstream id;
      id << std::hex << std::setfill('0');
      for (unsigned i = 0; i < 4; ++i) id << std::setw(8) << static_cast<std::uint32_t>(random());
      boot_ = id.str();
      state_ = node_.Advertise<gz::msgs::StringMsg>("/edge/body/state");
      if (!state_ || !node_.Subscribe("/odom", &VelocityWatchdog::on_odometry, this) ||
          !node_.Subscribe("/edge/body/command", &VelocityWatchdog::on_body_command, this)) {
        throw std::runtime_error("could not establish Gazebo body runtime topics");
      }
    } else if (!node_.Subscribe("/cmd_vel", &VelocityWatchdog::on_command, this)) {
      throw std::runtime_error("could not establish Gazebo velocity watchdog topics");
    }
  }

  void PreUpdate(const gz::sim::UpdateInfo &info, gz::sim::EntityComponentManager &) override {
    std::lock_guard<std::mutex> lock(mutex_);
    const bool suspended = info.paused || info.dt < Clock::duration::zero() || info.simTime < sim_time_;
    sim_time_ = info.simTime;
    if (suspended) {
      if (body_runtime_) {
        if (!suspended_) close_epoch();
        witness_floor_ = sim_time_;
      }
      suspended_ = true;
      arrival_.reset();
      last_publish_.reset();
      guarded_.Publish(gz::msgs::Twist{});
      if (body_runtime_) broadcast_state();
      return;
    }
    suspended_ = false;
    if (body_runtime_) {
      expire();
      broadcast_state();
    }
    // The physical model advances with sim time. Wall-time age also prevents
    // an old command from being revived after a suspended simulation.
    if (!body_runtime_ && arrival_ && (sim_time_ - arrival_->sim >= 300ms ||
                     Clock::now() - arrival_->wall >= 300ms)) {
      arrival_.reset();
    }
    if (last_publish_ && sim_time_ - *last_publish_ < 20ms) return;
    last_publish_ = sim_time_;
    guarded_.Publish(arrival_ ? command_ : gz::msgs::Twist{});
  }

 private:
  struct Arrival { Clock::duration sim; Clock::time_point wall; };
  enum class Phase { quiet, ready, held };

  static bool unsigned_number(const std::string &token, std::uint64_t &number) {
    const auto end = token.data() + token.size();
    const auto parsed = std::from_chars(token.data(), end, number);
    return parsed.ec == std::errc{} && parsed.ptr == end;
  }

  void broadcast_state(bool changed = false) {
    const auto now = Clock::now();
    if (!changed && last_state_ && now - *last_state_ < 50ms) return;
    const char *phase = phase_ == Phase::held ? "held" : phase_ == Phase::ready ? "ready" : "quiet";
    gz::msgs::StringMsg state;
    state.set_data("v1 " + boot_ + " " + std::to_string(epoch_) + " " + phase + " " + owner_);
    state_.Publish(state);
    last_state_ = now;
  }

  void close_epoch() {
    if (phase_ != Phase::quiet) ++epoch_;
    phase_ = Phase::quiet;
    owner_ = "-";
    sequence_ = 0;
    arrival_.reset();
    quiet_start_.reset();
    last_sample_.reset();
    // Samples acquired before the fence cannot witness the next grant.
    witness_floor_ = sim_time_;
    guarded_.Publish(gz::msgs::Twist{});
    broadcast_state(true);
  }

  void expire() {
    const auto now = Clock::now();
    if (phase_ == Phase::held && arrival_ &&
        (sim_time_ - arrival_->sim >= 300ms || now - arrival_->wall >= 300ms)) {
      close_epoch();
    } else if (last_sample_ && (sim_time_ - last_sample_->sim >= 200ms || now - last_sample_->wall >= 200ms)) {
      if (phase_ == Phase::ready) close_epoch();
      else {
        last_sample_.reset();
        quiet_start_.reset();
      }
    }
  }

  void on_body_command(const gz::msgs::StringMsg &message) {
    if (message.data().size() > 1024) return;
    std::lock_guard<std::mutex> lock(mutex_);
    if (suspended_) return;
    // Validate the deadline even if the transport callback wins the race with PreUpdate.
    expire();
    std::istringstream input(message.data());
    std::string version, boot, epoch, owner, verb, extra;
    std::uint64_t generation{};
    if (!(input >> version >> boot >> epoch >> owner >> verb) || version != "v1" ||
        boot != boot_ || !unsigned_number(epoch, generation) || generation != epoch_ || owner == "-") return;
    if (verb == "open") {
      if (phase_ != Phase::ready || (input >> extra)) return;
      owner_ = owner;
      phase_ = Phase::held;
      command_.Clear();
      arrival_ = Arrival{sim_time_, Clock::now()};
      quiet_start_.reset();
      last_sample_.reset();
      broadcast_state(true);
      return;
    }
    if (phase_ != Phase::held || owner != owner_) return;
    if (verb == "stop" && !(input >> extra)) {
      close_epoch();
      return;
    }
    std::string sequence;
    std::uint64_t next{};
    double linear{}, angular{};
    if (verb != "drive" || !(input >> sequence >> linear >> angular) || (input >> extra) ||
        !unsigned_number(sequence, next) || next <= sequence_ ||
        !std::isfinite(linear) || !std::isfinite(angular) ||
        std::abs(linear) > 0.32 || std::abs(angular) > 1.1) return;
    sequence_ = next;
    command_.mutable_linear()->set_x(linear);
    command_.mutable_angular()->set_z(angular);
    arrival_ = Arrival{sim_time_, Clock::now()};
  }

  void on_odometry(const gz::msgs::Odometry &message) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (suspended_ || phase_ == Phase::held) return;
    expire();
    const auto &time = message.header().stamp();
    const auto &twist = message.twist();
    bool valid = message.header().has_stamp() && time.sec() >= 0 && time.nsec() >= 0 &&
                 time.nsec() < 1000000000 &&
                 time.sec() <= (std::numeric_limits<std::int64_t>::max() - time.nsec()) / 1000000000 &&
                 twist.has_linear() && twist.has_angular();
    const auto stamp = valid ? std::chrono::seconds(time.sec()) + std::chrono::nanoseconds(time.nsec())
                             : std::chrono::nanoseconds::zero();
    // The trusted witness shares the simulation clock with this system.
    valid = valid && stamp > witness_floor_ && stamp <= sim_time_ && sim_time_ - stamp < 200ms;
    const auto &v = twist.linear();
    const auto &w = twist.angular();
    const bool stationary = std::hypot(v.x(), v.y(), v.z()) < 0.025 &&
                            std::hypot(w.x(), w.y(), w.z()) < 0.05;
    if (!valid || !stationary) {
      if (phase_ == Phase::ready) close_epoch();
      else quiet_start_.reset();
      return;
    }
    if (last_sample_ && stamp <= last_sample_->sim) return;
    const auto now = Clock::now();
    if (!last_sample_ || stamp - last_sample_->sim >= 200ms || !quiet_start_) {
      quiet_start_ = Arrival{stamp, now};
    }
    last_sample_ = Arrival{stamp, now};
    if (phase_ == Phase::quiet && stamp - quiet_start_->sim >= 300ms && now - quiet_start_->wall >= 300ms) {
      phase_ = Phase::ready;
      broadcast_state(true);
    }
  }

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
  bool body_runtime_ = false, suspended_ = true;
  Phase phase_ = Phase::quiet;
  std::string boot_, owner_ = "-";
  std::uint64_t epoch_ = 1, sequence_ = 0;
  Clock::duration witness_floor_{};
  std::optional<Arrival> quiet_start_, last_sample_;
  std::optional<Clock::time_point> last_state_;
  std::optional<Clock::duration> last_publish_;
  std::optional<Arrival> arrival_;
  gz::msgs::Twist command_;
  gz::transport::Node::Publisher guarded_;
  gz::transport::Node::Publisher state_;
  gz::transport::Node node_;
};
}  // namespace edge_sim

GZ_ADD_PLUGIN(edge_sim::VelocityWatchdog, gz::sim::System,
              gz::sim::ISystemConfigure, gz::sim::ISystemPreUpdate)
