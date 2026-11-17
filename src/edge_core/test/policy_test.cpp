#include "edge_core/policy.hpp"
#include <limits>
#include <stdexcept>

using namespace edge;

void require(bool check, const char *reason) {
  if (!check) throw std::runtime_error(reason);
}

int main() {
  const std::vector<Position> targets{{0.8,0},{0.8,0.7}};
  const InspectionPolicy policy;
  for (std::size_t verified=0; verified<targets.size(); ++verified) {
    auto proposal=policy.propose({targets,verified});
    require(std::holds_alternative<NavigateStep>(proposal),"next waypoint required");
    require(admissible(proposal,targets,verified),"default policy proposed wrong step");
    require(!admissible(Done{},targets,verified),"early completion admitted");
  }
  require(!admissible(NavigateStep{1,targets[1]},targets,0),"skipped prerequisite");
  require(!admissible(NavigateStep{0,targets[0]},targets,1),"reissued verified step");
  require(!admissible(NavigateStep{0,targets[1]},targets,0),"wrong coordinate authorized");
  require(!admissible(NavigateStep{0,{std::numeric_limits<double>::quiet_NaN(),0}},targets,0),
          "non-finite proposal admitted");
  require(!admissible(NavigateStep{2,targets[0]},targets,2),"navigation after final evidence");
  require(std::holds_alternative<Done>(policy.propose({targets,2})) && admissible(Done{},targets,2),
          "all verified waypoints should satisfy completion preconditions");
  require(std::holds_alternative<Blocked>(policy.propose({targets,3})), "invalid evidence count ignored");
  require(!admissible(Done{},targets,3) && !admissible(Done{}, {},0),"invalid evidence granted completion");
}
