#include "body_authority.hpp"

#include <chrono>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace std::chrono_literals;
using Access = BodyAuthority::Access;
using Time = BodyAuthority::Clock::time_point;
const std::string boot(32, 'a');
const std::string other_boot(32, 'b');

void require(bool ok, const char* explanation) {
  if (!ok) throw std::runtime_error(explanation);
}

void ordered_handoff() {
  BodyAuthority client;
  Time t{};
  client.start("first");
  require(client.access(t) == Access::waiting && client.stop().empty(), "no state at startup");
  client.observe("v1 " + boot + " 8 ready - trailing", t);
  require(client.access(t) == Access::waiting, "malformed state ignored");
  client.observe("v1 " + boot + " 8 ready -", t);
  require(client.access(t) == Access::opening, "offer captured");
  const auto prefix = "v1 " + boot + " 8 first ";
  require(client.open() == prefix + "open" && client.open() == prefix + "open", "idempotent open");
  client.observe("v1 " + boot + " 8 held first", t += 10ms);
  require(client.access(t) == Access::held, "endpoint confirmed owner");
  require(client.drive(.25, 0) == prefix + "drive 1 0.25 0", "first drive frame");
  client.observe("v1 " + boot + " 7 held other", t += 10ms);
  client.observe("v1 " + boot + " 8 ready -", t);
  require(client.access(t) == Access::held, "late state cannot reverse a grant");
  require(client.drive(0, -.5) == prefix + "drive 2 0 -0.5", "monotonic drive sequence");
  const auto stop = t;
  require(client.stop() == prefix + "stop" && !client.released(stop,t), "stop request precedes release");
  client.observe("v1 " + boot + " 9 quiet -", t += 10ms);
  require(client.access(t) == Access::lost && !client.released(stop,t), "quiet still awaits witness");
  client.observe("v1 " + boot + " 9 ready -", t += 10ms);
  require(client.released(stop,t) && client.access(t) == Access::lost, "release cannot regrant original goal");
  require(!client.released(t + 1ms,t + 1ms), "release predating this stop is rejected");
  require(!client.released(stop,t + 300ms), "stale release is rejected");
  client.start("second");
  require(client.access(t) == Access::opening && client.epoch() == 9, "next goal captures new generation");
  client.observe("v1 " + boot + " 9 held first", t += 10ms);
  require(client.access(t) == Access::lost, "different owner answered the open");
}

void freshness_and_restart() {
  BodyAuthority client;
  Time t{};
  client.start("goal");
  client.observe("v1 " + boot + " 1 ready -", t);
  require(client.access(t) == Access::opening, "open before freshness test");
  client.observe("v1 " + boot + " 1 held goal", t);
  require(client.access(t + 299ms) == Access::held, "fresh grant usable");
  // The next state can run before the timer sees the gap. It cannot renew the goal.
  client.observe("v1 " + boot + " 1 held goal", t += 300ms);
  require(client.access(t) == Access::lost, "lost grant stays lost on heartbeat resumption");
  client.observe("v1 " + other_boot + " 1 ready -", t += 1ms);
  require(!client.released(t,t) && client.access(t) == Access::lost, "new boot cannot settle old grant");
  client.observe("v1 " + boot + " 2 ready -", t += 1ms);
  require(!client.released(t,t), "retired boot cannot supply evidence");
  client.start("next");
  require(client.access(t) == Access::opening && client.boot() == other_boot, "new boot survives old replay");
  client.observe("v1 " + other_boot + " 1 held next", t);
  require(client.access(t + 300ms) == Access::lost, "silent endpoint expires at deadline");
}

void conflicting_owners() {
  BodyAuthority client;
  Time t{};
  client.start("one");
  client.observe("v1 " + boot + " 4 ready -", t);
  require(client.access(t) == Access::opening, "open before dispute test");
  client.observe("v1 " + boot + " 4 held one", t);
  client.observe("v1 " + boot + " 4 held two", t);
  client.observe("v1 " + boot + " 4 held one", t);
  require(client.access(t) == Access::lost && !client.released(t,t), "conflicting same-epoch ownership");
}

int main() {
  try {
    ordered_handoff();
    freshness_and_restart();
    conflicting_owners();
  } catch (const std::exception& error) {
    std::cerr << "body authority contract failed: " << error.what() << '\n';
    return 1;
  }
  return 0;
}
