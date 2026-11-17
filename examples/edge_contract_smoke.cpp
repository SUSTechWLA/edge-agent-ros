// Standalone C++20 contract examples. One serialized Edge control loop, one resource.
// Time arguments are simulated monotonic milliseconds. The link has authenticated
// Brain and mapped its validity interval to a conservative local deadline already.
#include <algorithm>
#include <cstdint>
#include <exception>
#include <iostream>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>

using Tick = std::int64_t;
using Revision = std::uint64_t;

struct TeamGrant {
    std::string mission;
    Revision epoch;
    std::set<std::string> members;
    Tick expires_at;
};

struct TeamMessage {
    std::string mission;
    Revision epoch;
    Revision sequence;
    Tick expires_at;
};

class TeamGate {
public:
    explicit TeamGate(std::string self) : self_(std::move(self)) {}

    // One gate per mission. A newer epoch supersedes all previous ready/release state.
    // The authenticated roster is authoritative even when we were removed or
    // its execution window has elapsed. Retain that epoch as a replay fence.
    bool install(TeamGrant grant) {
        if (grant.epoch == 0) return false;
        if (grant_ && (grant_->mission != grant.mission || grant.epoch <= grant_->epoch))
            return false;
        grant_ = std::move(grant);
        ready_.clear();
        release_sequence_ = 0;
        release_expiry_.reset();
        consumed_ = false;
        return true;
    }

    // 'member' is the identity vouched for by the Brain link. Sequence is per
    // member in this epoch. A newer not-ready report revokes a pending release.
    bool observe(const std::string& member, TeamMessage m, bool ready, Tick now) {
        if (!valid(m, now) || !grant_->members.contains(member))
            return false;
        auto seen = ready_.find(member);
        if (seen != ready_.end() && m.sequence <= seen->second.sequence)
            return false;
        ready_[member] = {m.sequence, m.expires_at, ready};
        if (!ready)
            release_expiry_.reset();
        return true;
    }

    // Called only by the authenticated Brain-release input. All active members
    // including this Edge must have supplied fresh readiness for this barrier.
    bool release(TeamMessage m, Tick now) {
        if (!valid(m, now) || consumed_ || m.sequence <= release_sequence_)
            return false;
        release_sequence_ = m.sequence;
        release_expiry_.reset();
        if (!all_ready(now))
            return false;
        release_expiry_ = m.expires_at;
        return true;
    }

    bool permits(Tick now) const {
        return grant_ && grant_->members.contains(self_) && now < grant_->expires_at && !consumed_ &&
               release_expiry_ && now < *release_expiry_ && all_ready(now);
    }

    void consume() { consumed_ = true; }

private:
    struct Readiness {
        Revision sequence;
        Tick expires_at;
        bool ready;
    };

    bool valid(const TeamMessage& m, Tick now) const {
        return grant_ && grant_->members.contains(self_) && grant_->mission == m.mission && grant_->epoch == m.epoch &&
               m.sequence > 0 && now < grant_->expires_at && now < m.expires_at;
    }

    bool all_ready(Tick now) const {
        return std::all_of(grant_->members.begin(), grant_->members.end(),
            [this, now](const std::string& member) {
                auto it = ready_.find(member);
                return it != ready_.end() && it->second.ready && now < it->second.expires_at;
            });
    }

    std::string self_;
    std::optional<TeamGrant> grant_;
    std::map<std::string, Readiness> ready_;
    Revision release_sequence_ = 0;
    std::optional<Tick> release_expiry_;
    bool consumed_ = false;
};

enum class ActionState { running, unknown, stopped };

class Edge {
public:
    struct Binding { Revision generation; };

    Binding attach() {
        if (attached_) throw std::logic_error("detach the current adapter before installing");
        attached_ = true;
        return {++generation_};
    }

    void detach() {
        if (!attached_) return;
        attached_ = false;
        ++generation_;
        if (holder_) operations_.at(*holder_) = ActionState::unknown;
    }

    // Caller provides a unique operation ID. Only an explicitly authorized
    // single-device task may run disconnected.
    bool start_local(const std::string& id, Binding binding, bool locally_authorized,
                     bool local_guard_passed) {
        return locally_authorized && start(id, binding, local_guard_passed);
    }

    bool start_team(const std::string& id, Binding binding, TeamGate& gate, Tick now,
                    bool brain_connected, bool local_guard_passed) {
        if (!brain_connected || !gate.permits(now) || !start(id, binding, local_guard_passed))
            return false;
        gate.consume();
        return true;
    }

    // The controller/evidence adapter supplies quiescence for the exact operation.
    // Acceptance of cancel, link loss and a new plug-in are insufficient evidence.
    bool reconcile_stopped(const std::string& id, bool confirmed_quiescent) {
        if (!confirmed_quiescent || holder_ != id) return false;
        operations_.at(id) = ActionState::stopped;
        holder_.reset();
        return true;
    }

    ActionState state(const std::string& id) const { return operations_.at(id); }
    bool occupied() const { return holder_.has_value(); }

private:
    bool start(const std::string& id, Binding binding, bool local_guard_passed) {
        if (id.empty() || !attached_ || binding.generation != generation_ ||
            !local_guard_passed || holder_ || operations_.contains(id))
            return false;
        operations_.emplace(id, ActionState::running);
        holder_ = id;
        return true;
    }

    Revision generation_ = 0;
    bool attached_ = false;
    std::optional<std::string> holder_;
    std::map<std::string, ActionState> operations_;
};

void check(bool ok, const char* expectation) {
    if (!ok) throw std::runtime_error(expectation);
}

// Defaults target one two-device mission. Use explicit logical time in every test.
TeamMessage message(Revision sequence = 1, Tick expires_at = 70) {
    return {"inventory", 7, sequence, expires_at};
}

TeamGate team() {
    TeamGate gate("rover");
    check(gate.install({"inventory", 7, {"rover", "station"}, 100}), "install grant");
    return gate;
}

void both_ready(TeamGate& gate) {
    check(gate.observe("rover", message(), true, 1), "rover readiness");
    check(gate.observe("station", message(), true, 1), "station readiness");
}

int main() {
    unsigned passed = 0;
    std::string current;
    auto scenario = [&](const char* name, auto run) {
        current = name;
        run();
        std::cout << "[PASS] " << name << '\n';
        ++passed;
    };
    try {
        scenario("replug rejects stale binding", [] {
            Edge e;
            auto old = e.attach();
            e.detach();
            auto fresh = e.attach();
            check(!e.start_local("x", old, true, true), "old generation rejected");
            check(e.start_local("x", fresh, true, true), "fresh generation accepted");
        });
        scenario("unplugged action keeps resource until exact reconciliation", [] {
            Edge e;
            check(e.start_local("a", e.attach(), true, true), "first start");
            e.detach();
            auto fresh = e.attach();
            check(e.state("a") == ActionState::unknown && e.occupied(), "unknown retains resource");
            check(!e.reconcile_stopped("b", true), "unrelated evidence rejected");
            check(!e.reconcile_stopped("a", false), "cancel ACK insufficient");
            check(!e.start_local("b", fresh, true, true), "resource unavailable");
            check(e.reconcile_stopped("a", true), "exact quiescence releases resource");
            check(e.state("a") == ActionState::stopped, "terminal state retained");
            check(!e.start_local("a", fresh, true, true), "old operation cannot start twice");
            check(e.start_local("b", fresh, true, true), "next operation may start");
        });
        scenario("mission, epoch, member, sequence and TTL filter", [] {
            auto gate = team();
            auto wrong = message();
            wrong.mission = "elsewhere";
            check(!gate.observe("rover", wrong, true, 1), "reject another mission");
            wrong = message();
            wrong.epoch = 6;
            check(!gate.observe("rover", wrong, true, 1), "reject obsolete epoch");
            check(!gate.observe("visitor", message(), true, 1), "reject nonmember");
            check(!gate.observe("rover", message(1, 2), true, 2), "reject expired readiness");
            check(gate.observe("rover", message(3), true, 2), "accept fresh sequence");
            check(!gate.observe("rover", message(3), true, 3), "reject duplicate");
            check(!gate.observe("rover", message(2), true, 3), "reject out-of-order report");
        });
        scenario("Brain release requires every member and expires", [] {
            auto gate = team();
            check(gate.observe("rover", message(), true, 1), "first ready");
            check(!gate.release(message(1), 2), "missing peer");
            check(gate.observe("station", message(), true, 3), "peer ready");
            check(!gate.permits(4), "readiness alone does not release");
            check(!gate.release(message(1), 4), "old release cannot be replayed");
            check(gate.release(message(2, 9), 4), "fresh release accepted");
            check(gate.permits(8) && !gate.permits(9), "exclusive release deadline");
        });
        scenario("revocation or epoch change clears readiness and release", [] {
            auto gate = team();
            both_ready(gate);
            check(gate.release(message(), 2), "first release");
            check(gate.observe("station", message(2), false, 3), "peer withdraws readiness");
            check(!gate.permits(3), "withdrawal revokes release");
            check(gate.install({"inventory", 8, {"rover", "station"}, 100}), "next epoch");
            check(!gate.release(message(3), 5), "previous Brain release rejected");
            auto next = message();
            next.epoch = 8;
            check(!gate.release(next, 5), "new epoch needs fresh readiness");
        });
        scenario("authoritative removal revokes an unexpired release", [] {
            auto gate = team();
            both_ready(gate);
            check(gate.release(message(), 2) && gate.permits(3), "release initially active");
            check(gate.install({"inventory", 8, {"station"}, 100}), "accept removal snapshot");
            check(!gate.permits(4), "removal closes release immediately");
            check(!gate.install({"inventory", 7, {"rover", "station"}, 100}), "reject replayed roster");
            auto removed = message();
            removed.epoch = 8;
            check(!gate.observe("station", removed, true, 5) && !gate.release(removed, 5),
                  "removed device cannot participate");
            check(gate.install({"inventory", 9, {"rover", "station"}, 100}), "accept readmission");
            check(!gate.permits(6), "readmission requires fresh readiness and release");
        });
        scenario("team release remains subject to local guard and executes once", [] {
            Edge e;
            auto binding = e.attach();
            auto gate = team();
            both_ready(gate);
            check(gate.release(message(), 2), "release");
            check(!e.start_team("coop", binding, gate, 3, true, false), "local guard veto");
            check(!e.occupied(), "guard prevented side effect");
            check(e.start_team("coop", binding, gate, 3, true, true), "guard allows execution");
            check(e.reconcile_stopped("coop", true), "action settled");
            check(!e.start_team("coop2", binding, gate, 4, true, true), "release consumed");
        });
        scenario("offline preauthorized local task contrasts with team task", [] {
            Edge e;
            auto binding = e.attach();
            auto gate = team();
            both_ready(gate);
            check(gate.release(message(), 2), "cached release");
            check(!e.start_team("team", binding, gate, 3, false, true), "offline team blocked");
            check(!e.start_local("local", binding, false, true), "local authority required");
            check(e.start_local("local", binding, true, true), "offline local task runs");
        });
        scenario("readiness or Brain grant expiration closes a longer release", [] {
            for (auto peer_expiry : {20, 200}) {
                auto gate = team();
                check(gate.observe("rover", message(1, 200), true, 1), "rover ready");
                check(gate.observe("station", message(1, peer_expiry), true, 1), "station ready");
                check(gate.release(message(1, 200), 2), "long release");
                auto deadline = std::min(peer_expiry, 100);
                check(gate.permits(deadline - 1) && !gate.permits(deadline), "earliest deadline wins");
            }
        });
    } catch (const std::exception& err) {
        std::cerr << "[FAIL] " << current << " (" << err.what() << ")\n";
        return 1;
    }
    std::cout << passed << " scenarios passed\n";
    return 0;
}
