#pragma once
#include "edge_core/policy.hpp"
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

struct sqlite3;

namespace edge {
struct Mission {
  std::string id, state, detail;
  std::vector<Position> targets, evidence, reference_path;
};

// Single writer. All public mutations either commit fully or leave the last
// durable state intact. ROS callbacks invoke them on the same executor thread.
class Ledger {
public:
  explicit Ledger(const std::string &path);
  ~Ledger();
  Ledger(const Ledger &) = delete;
  Ledger &operator=(const Ledger &) = delete;
  std::optional<Mission> find(const std::string &id) const;
  std::optional<std::string> owner() const;
  void record_request(const std::string &id, const std::string &kind, const std::string &detail);
  void create(const std::string &id, const std::vector<Position> &targets,
              const std::vector<Position> &reference_path = {});
  void intent(const std::string &id, int step, const std::string &operation);
  void result(const std::string &id, int step, const std::string &detail);
  void verified(const std::string &id, int step, Position observed, std::int64_t stamp, bool last);
  void state(const std::string &id, const std::string &state, const std::string &detail);
private:
  sqlite3 *db_ = nullptr;
  int lock_ = -1;
};
}  // namespace edge
