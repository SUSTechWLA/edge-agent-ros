#pragma once

#include <charconv>
#include <chrono>
#include <cstdint>
#include <iomanip>
#include <limits>
#include <locale>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <utility>

// Consumer of the single trusted actuator's v1 state stream. A goal only ever
// opens one (boot, epoch). A delayed open or stop always names the original grant.
class BodyAuthority {
public:
  using Clock = std::chrono::steady_clock;
  enum class Access { waiting, opening, held, lost };

  void observe(const std::string& wire, Clock::time_point now) {
    if (wire.size() > 512) return;
    std::istringstream input(wire);
    std::string version, epoch, name, extra;
    State next{};
    if (!(input >> version >> next.boot >> epoch >> name >> next.owner) || input >> extra ||
        version != "v1" || next.boot.size() != 32 ||
        next.boot.find_first_not_of("0123456789abcdef") != std::string::npos) return;
    auto [end, err] = std::from_chars(epoch.data(), epoch.data()+epoch.size(), next.epoch);
    if (err != std::errc{} || end != epoch.data()+epoch.size() || next.epoch == 0) return;
    if (name == "quiet") next.phase = Phase::quiet;
    else if (name == "ready") next.phase = Phase::ready;
    else if (name == "held") next.phase = Phase::held;
    else return;
    if ((next.phase == Phase::held) == (next.owner == "-")) return;

    if (retired_boots_.contains(next.boot)) return;
    if (state_) {
      if (state_->boot != next.boot) retired_boots_.insert(state_->boot);
      else {
        // A generation advances quiet -> ready -> held. Revocation must
        // advance the epoch, including on watchdog expiration.
        if (next.epoch < state_->epoch ||
            (next.epoch == state_->epoch && next.phase < state_->phase)) return;
        if (next.epoch == state_->epoch && next.phase == Phase::held &&
            state_->phase == Phase::held && state_->owner != next.owner) {
          disputed_ = true;
          if (grant_) grant_lost_ = true;
          return;
        }
      }
      if (next.boot != state_->boot || next.epoch != state_->epoch) disputed_ = false;
    }
    if (grant_ && (!fresh(now) || next.boot != grant_->boot || next.epoch != grant_->epoch ||
                   (next.phase == Phase::held && next.owner != owner_))) grant_lost_ = true;
    next.arrived = now;
    state_ = std::move(next);
  }

  void start(const std::string& owner) {
    owner_ = owner;
    grant_.reset();
    grant_lost_ = false;
    sequence_ = 0;
  }

  Access access(Clock::time_point now) {
    if (grant_ && (!fresh(now) || disputed_)) grant_lost_ = true;
    if (grant_lost_) return Access::lost;
    if (!fresh(now) || disputed_) return Access::waiting;
    if (!grant_) {
      if (state_->phase != Phase::ready) return Access::waiting;
      grant_ = *state_;
    }
    if (state_->boot != grant_->boot || state_->epoch != grant_->epoch) return Access::lost;
    if (state_->phase == Phase::ready) return Access::opening;
    if (state_->phase == Phase::held && state_->owner == owner_) return Access::held;
    return Access::lost;
  }

  bool released(Clock::time_point since, Clock::time_point now) const {
    if (!fresh(now) || disputed_ || state_->arrived < since || state_->phase != Phase::ready) return false;
    // A new boot cannot attest that the old endpoint relinquished this goal.
    return !grant_ || (state_->boot == grant_->boot && state_->epoch > grant_->epoch);
  }

  std::string open() const { return prefix() + "open"; }
  std::string stop() const { return grant_ ? prefix() + "stop" : std::string{}; }
  std::string drive(double linear, double angular) {
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out << prefix() << "drive " << ++sequence_ << ' '
        << std::setprecision(std::numeric_limits<double>::max_digits10) << linear << ' ' << angular;
    return out.str();
  }

  const std::string& owner() const { return owner_; }
  std::string boot() const { return grant_ ? grant_->boot : std::string{}; }
  std::uint64_t epoch() const { return grant_ ? grant_->epoch : 0; }

private:
  enum class Phase { quiet, ready, held };
  struct State {
    std::string boot, owner;
    std::uint64_t epoch{};
    Phase phase{};
    Clock::time_point arrived;
  };
  bool fresh(Clock::time_point now) const {
    return state_ && now - state_->arrived < std::chrono::milliseconds(300);
  }
  std::string prefix() const {
    return "v1 " + grant_->boot + " " + std::to_string(grant_->epoch) + " " + owner_ + " ";
  }
  std::optional<State> state_, grant_;
  std::set<std::string> retired_boots_;
  bool disputed_ = false;
  bool grant_lost_ = false;
  std::string owner_;
  std::uint64_t sequence_{};
};
