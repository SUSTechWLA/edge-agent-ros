#include "ledger.hpp"
#include <edge_interfaces/action/navigate_to.hpp>
#include <edge_interfaces/srv/cancel_mission.hpp>
#include <edge_interfaces/srv/get_mission.hpp>
#include <edge_interfaces/srv/submit_mission.hpp>
#include <nav_msgs/msg/odometry.hpp>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp_action/rclcpp_action.hpp>
#include <std_msgs/msg/string.hpp>
#include <chrono>
#include <cmath>
#include <memory>
#include <optional>
#include <stdexcept>
#include <utility>

using namespace std::chrono_literals;
namespace edge {
using Submit = edge_interfaces::srv::SubmitMission;
using Get = edge_interfaces::srv::GetMission;
using Cancel = edge_interfaces::srv::CancelMission;
using Navigate = edge_interfaces::action::NavigateTo;
using Client = rclcpp_action::Client<Navigate>;
using Goal = rclcpp_action::ClientGoalHandle<Navigate>;
using Odom = nav_msgs::msg::Odometry;
using Clock = std::chrono::steady_clock;
using Heartbeat = std_msgs::msg::String;

enum class Phase { waiting, requesting, moving, verifying };
struct Active {
  std::string id;
  std::vector<Position> targets;
  std::size_t step = 0;
  std::string operation;
  Goal::SharedPtr goal;
  Phase phase = Phase::waiting;
  Clock::time_point deadline = Clock::now() + 30s;
  bool stopping = false, user_canceled = false;
  std::int64_t verification_stamp = -1;
  std::string frame;
};

struct Observation {
  Position position;
  std::string frame;
  std::int64_t stamp;
  Clock::time_point received;
};

class Core final : public rclcpp::Node {
public:
  explicit Core(std::unique_ptr<PolicyPort> policy = std::make_unique<InspectionPolicy>())
      : Node("edge_core"), db_(declare_parameter<std::string>("db_path","/var/lib/edge-agent/edge.db")),
        policy_(std::move(policy)) {
    if (!policy_) throw std::invalid_argument("edge_core needs a policy");
    actions_ = rclcpp_action::create_client<Navigate>(this,"/edge/navigate_to");
    heartbeats_ = create_publisher<Heartbeat>("/edge/active_operation",1);
    odometry_ = create_subscription<Odom>("/odom",rclcpp::SensorDataQoS().keep_last(1),
      [this](Odom::ConstSharedPtr msg) { on_odometry(*msg); });
    submit_ = create_service<Submit>("/edge/submit_mission",
      [this](Submit::Request::SharedPtr req, Submit::Response::SharedPtr res) { submit(*req,*res); });
    get_ = create_service<Get>("/edge/get_mission",
      [this](Get::Request::SharedPtr req, Get::Response::SharedPtr res) { get(*req,*res); });
    cancel_ = create_service<Cancel>("/edge/cancel_mission",
      [this](Cancel::Request::SharedPtr req, Cancel::Response::SharedPtr res) { cancel(*req,*res); });
    tick_ = create_wall_timer(100ms,[this] { tick(); });
    if (auto owner=db_.owner()) RCLCPP_WARN(get_logger(),"Base held for recovery of %s",owner->c_str());
    RCLCPP_INFO(get_logger(),"Inspection executive ready");
  }
private:
  Ledger db_;
  std::unique_ptr<PolicyPort> policy_;
  std::optional<Active> active_;
  std::optional<Observation> observation_;
  Client::SharedPtr actions_;
  rclcpp::Publisher<Heartbeat>::SharedPtr heartbeats_;
  rclcpp::Subscription<Odom>::SharedPtr odometry_;
  rclcpp::Service<Submit>::SharedPtr submit_;
  rclcpp::Service<Get>::SharedPtr get_;
  rclcpp::Service<Cancel>::SharedPtr cancel_;
  rclcpp::TimerBase::SharedPtr tick_;

  void submit(const Submit::Request &req, Submit::Response &res) {
    res.state = "REJECTED";
    auto reject = [&](const std::string &reason) {
      res.detail = reason;
      db_.record_request(req.mission_id, "request.rejected", reason);
    };
    if (req.mission_id.empty() || req.mission_id.find('\0')!=std::string::npos) {
      reject("invalid mission_id [code:core.mission_id_invalid]"); return;
    }
    if (req.waypoints.empty() || req.waypoints.size()>8) { reject("expected 1 to 8 waypoints [code:core.waypoint_invalid]"); return; }
    std::vector<Position> targets;
    for (const auto &p:req.waypoints) {
      if (!std::isfinite(p.x) || !std::isfinite(p.y) || std::abs(p.x)>4 || std::abs(p.y)>4 || p.z!=0.0) {
        reject("invalid planar waypoint [code:core.waypoint_invalid]"); return;
      }
      targets.push_back({p.x,p.y});
    }
    if (auto existing=db_.find(req.mission_id)) {
      res.state=existing->state;
      res.accepted=existing->targets==targets;
      res.detail=res.accepted?"duplicate: "+existing->detail:"conflict: ID already bound to different waypoints [code:core.id_conflict]";
      db_.record_request(req.mission_id, res.accepted ? "request.duplicate" : "request.rejected", res.detail);
      return;
    }
    if (auto owner=db_.owner()) { reject("base occupied by "+*owner+" [code:core.base_occupied]"); return; }
    db_.create(req.mission_id,targets);
    auto &a=active_.emplace();
    a.id=req.mission_id; a.targets=std::move(targets);
    res.accepted=true; res.state="RUNNING"; res.detail="accepted";
  }
  void get(const Get::Request &req, Get::Response &res) {
    auto m=db_.find(req.mission_id);
    if (!m) { res.detail="mission not found"; return; }
    res.found=true; res.state=m->state; res.detail=m->detail;
    for (const auto &xy:m->evidence) {
      geometry_msgs::msg::Point p; p.x=xy.x; p.y=xy.y; res.evidence.push_back(p);
    }
    res.verified_count=static_cast<std::uint32_t>(res.evidence.size());
  }
  void cancel(const Cancel::Request &req, Cancel::Response &res) {
    auto m=db_.find(req.mission_id);
    if (!m) { res.detail="mission not found"; return; }
    if (!active_ || active_->id!=req.mission_id) {
      res.state=m->state; res.accepted=m->state=="CANCELED"; res.detail=m->detail; return;
    }
    if (!active_->stopping) {
      db_.record_request(req.mission_id,"request.cancel","cancel requested by ROS client");
      stop(true);
    }
    auto state=db_.find(req.mission_id);
    res.accepted=true; res.state=state->state; res.detail=state->detail;
  }
  void finish(const std::string &state, const std::string &why) {
    auto detail=active_->operation.empty()?why:active_->operation+" "+why;
    db_.state(active_->id,state,detail);
    RCLCPP_INFO(get_logger(),"Mission %s -> %s (%s)",active_->id.c_str(),state.c_str(),detail.c_str());
    active_.reset();
  }
  void request_cancel(const Goal::SharedPtr &goal) {
    try { actions_->async_cancel_goal(goal); }
    catch (const std::exception &e) { RCLCPP_WARN(get_logger(),"Cancel send: %s",e.what()); }
    // The result still has to confirm that the platform is quiescent.
  }
  void stop(bool user) {
    auto &a=*active_;
    a.stopping=true; a.user_canceled=user;
    if (a.phase==Phase::waiting || a.phase==Phase::verifying) {
      finish(user?"CANCELED":"FAILED",user?"canceled with no moving action":"no action in flight"); return;
    }
    db_.state(a.id,"CANCELING",user?"user cancel: awaiting stop":"motion timeout: awaiting stop");
    a.deadline=Clock::now()+6s;
    // Withdraw the motion lease even if the independent cancel RPC is lost.
    heartbeat("");
    if (a.goal) request_cancel(a.goal);
  }
  void heartbeat(const std::string &operation) {
    Heartbeat msg; msg.data=operation; heartbeats_->publish(msg);
  }
  bool current(const std::string &op) const { return active_ && active_->operation==op; }
  void decide() {
    auto &a=*active_;
    Proposal proposal;
    try { proposal=policy_->propose({a.targets,a.step}); }
    catch (const std::exception &e) { finish("FAILED",std::string("policy error: ")+e.what()); return; }
    if (!admissible(proposal,a.targets,a.step)) { finish("FAILED","policy proposed an unauthorized step or completion"); return; }
    if (const auto *blocked=std::get_if<Blocked>(&proposal)) { finish("FAILED","policy blocked: "+blocked->reason); return; }
    if (std::holds_alternative<Done>(proposal)) {
      // Only the verifier's durable transaction may grant completion.
      const auto stored=db_.find(a.id);
      if (stored && stored->state=="SUCCEEDED" && stored->evidence.size()==a.targets.size())
        active_.reset();
      else finish("FAILED","policy completion lacks durable final evidence");
      return;
    }
    send(std::get<NavigateStep>(proposal));
  }
  void send(const NavigateStep &next) {
    auto &a=*active_;
    a.operation=a.id+"/waypoint/"+std::to_string(a.step+1);
    db_.intent(a.id,static_cast<int>(a.step),a.operation);
    a.phase=Phase::requesting; a.deadline=Clock::now()+6s;
    a.frame=observation_->frame;
    Navigate::Goal goal;
    goal.operation_id=a.operation; goal.x=next.target.x; goal.y=next.target.y;
    heartbeat(a.operation);
    Client::SendGoalOptions options;
    options.goal_response_callback=[this,op=a.operation](Goal::SharedPtr handle) {
      if (!current(op)) { if (handle) request_cancel(handle); return; }
      auto &active=*active_;
      if (!handle) {
        finish(active.user_canceled?"CANCELED":"FAILED","gateway rejected; operation never accepted [code:navigation.goal_rejected]"); return;
      }
      active.goal=handle; active.phase=Phase::moving;
      if (active.stopping) request_cancel(handle);
      else active.deadline=Clock::now()+90s;
    };
    options.result_callback=[this,op=a.operation](const Goal::WrappedResult &r) { on_result(op,r); };
    try { actions_->async_send_goal(goal,options); }
    catch (const std::exception &e) { finish("RECOVERY_REQUIRED",std::string("dispatch uncertain: ")+e.what()); }
  }
  void on_result(const std::string &op, const Goal::WrappedResult &r) {
    if (!current(op)) return;
    auto &a=*active_;
    if (!r.result) { finish("RECOVERY_REQUIRED","missing action result [code:navigation.outcome_unknown]"); return; }
    const auto &why=r.result->detail;
    db_.result(a.id,static_cast<int>(a.step),why);
    using Code=rclcpp_action::ResultCode;
    if (why.starts_with("UNKNOWN") || r.code==Code::UNKNOWN) {
      finish("RECOVERY_REQUIRED","gateway: "+why); return;
    }
    if (a.stopping) {
      finish(a.user_canceled?"CANCELED":"FAILED","stop confirmed: "+why); return;
    }
    if (r.code!=Code::SUCCEEDED || !r.result->reached) { finish("FAILED","gateway: "+why); return; }
    a.phase=Phase::verifying; a.goal.reset(); a.deadline=Clock::now()+3s;
    a.verification_stamp=observation_?observation_->stamp:-1;
    db_.state(a.id,"RUNNING","verifying: "+op);
  }
  void on_odometry(const Odom &msg) {
    const auto &p=msg.pose.pose.position;
    if (msg.header.stamp.sec<0 || msg.header.stamp.nanosec>=1000000000U) return;
    auto stamp=rclcpp::Time(msg.header.stamp).nanoseconds();
    if (!std::isfinite(p.x) || !std::isfinite(p.y) || msg.header.frame_id.empty()) return;
    if (observation_ && stamp<=observation_->stamp) return;
    observation_=Observation{{p.x,p.y},msg.header.frame_id,stamp,Clock::now()};
    if (!active_ || active_->phase!=Phase::verifying || stamp<=active_->verification_stamp) return;
    auto &a=*active_;
    if (a.frame!=observation_->frame) { finish("FAILED","verification: odometry frame changed [code:core.frame_changed]"); return; }
    const auto target=a.targets[a.step];
    if (std::hypot(p.x-target.x,p.y-target.y)>0.12) {
      finish("FAILED","verification: post-action odometry outside 0.12 m [code:core.arrival_mismatch]"); return;
    }
    bool last=a.step+1==a.targets.size();
    db_.verified(a.id,static_cast<int>(a.step),{p.x,p.y},stamp,last);
    if (last) {
      RCLCPP_INFO(get_logger(),"Mission %s verified %zu waypoints",a.id.c_str(),a.targets.size());
      active_.reset(); return;
    }
    ++a.step; a.operation.clear(); a.phase=Phase::waiting; a.deadline=Clock::now()+30s;
  }
  void tick() {
    if (!active_) return;
    auto &a=*active_;
    auto now=Clock::now();
    if (now>=a.deadline) {
      if (a.stopping || a.phase==Phase::requesting) { finish("RECOVERY_REQUIRED","gateway response or stop unconfirmed [code:navigation.outcome_unknown]"); return; }
      if (a.phase==Phase::moving) { stop(false); return; }
      finish("FAILED",a.phase==Phase::verifying?"verification: fresh odometry timeout [code:core.verification_timeout]":"dispatch: gateway or fresh odometry unavailable [code:core.dispatch_unavailable]");
      return;
    }
    if (a.phase==Phase::moving || a.phase==Phase::requesting) {
      heartbeat(a.stopping ? "" : a.operation);
      return;
    }
    if (a.phase==Phase::waiting && actions_->action_server_is_ready() && observation_ &&
        now-observation_->received<500ms) decide();
  }
};
}  // namespace edge

int main(int argc,char **argv) {
  rclcpp::init(argc,argv);
  try { rclcpp::spin(std::make_shared<edge::Core>()); }
  catch (const std::exception &e) {
    RCLCPP_FATAL(rclcpp::get_logger("edge_core"),"%s",e.what());
    rclcpp::shutdown(); return 1;
  }
  rclcpp::shutdown();
  return 0;
}
