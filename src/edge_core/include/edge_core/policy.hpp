#pragma once
#include <cstddef>
#include <span>
#include <string>
#include <variant>
#include <vector>

namespace edge {
struct Position {
  double x, y;
  bool operator==(const Position &) const = default;
};

struct PolicyObservation {
  std::vector<Position> targets;
  std::size_t verified_count;
};
struct NavigateStep { std::size_t step; Position target; };
struct Blocked { std::string reason; };
struct Done {};
using Proposal = std::variant<NavigateStep, Blocked, Done>;

class PolicyPort {
public:
  virtual ~PolicyPort() = default;
  virtual Proposal propose(const PolicyObservation &observed) const = 0;
};

class InspectionPolicy final : public PolicyPort {
public:
  Proposal propose(const PolicyObservation &observed) const override {
    if (observed.targets.empty() || observed.verified_count > observed.targets.size())
      return Blocked{"inconsistent inspection plan"};
    if (observed.verified_count == observed.targets.size()) return Done{};
    return NavigateStep{observed.verified_count, observed.targets[observed.verified_count]};
  }
};

// Admission is owned by the executive. Check a proposal against its own
// authorized mission and verified prefix, independently of the policy snapshot.
inline bool admissible(const Proposal &proposal, std::span<const Position> authorized,
                       std::size_t verified_count) {
  if (std::holds_alternative<Blocked>(proposal)) return true;
  if (authorized.empty() || verified_count > authorized.size()) return false;
  if (std::holds_alternative<Done>(proposal)) return verified_count == authorized.size();
  const auto &next = std::get<NavigateStep>(proposal);
  return verified_count < authorized.size() && next.step == verified_count &&
         next.target == authorized[verified_count];
}
} // namespace edge
