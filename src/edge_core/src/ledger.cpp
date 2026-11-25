#include "ledger.hpp"
#include "audit_schema.hpp"
#include <sqlite3.h>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <stdexcept>

namespace edge {
namespace {
void check(sqlite3 *db, int result) {
  if (result != SQLITE_OK) throw std::runtime_error(sqlite3_errmsg(db));
}
void sql(sqlite3 *db, const char *s) { check(db, sqlite3_exec(db, s, nullptr, nullptr, nullptr)); }
struct Transaction {
  sqlite3 *db;
  bool committed = false;
  explicit Transaction(sqlite3 *db) : db(db) { sql(db, "BEGIN IMMEDIATE"); }
  void commit() { sql(db, "COMMIT"); committed = true; }
  ~Transaction() { if (!committed) sqlite3_exec(db, "ROLLBACK", nullptr, nullptr, nullptr); }
};
struct Query {
  sqlite3 *db;
  sqlite3_stmt *q = nullptr;
  Query(sqlite3 *db, const char *s) : db(db) {
    check(db, sqlite3_prepare_v2(db, s, -1, &q, nullptr));
  }
  ~Query() { sqlite3_finalize(q); }
  void bind(int n, const std::string &s) {
    check(db, sqlite3_bind_text(q, n, s.data(), static_cast<int>(s.size()), SQLITE_TRANSIENT));
  }
  void bind(int n, double x) { check(db, sqlite3_bind_double(q, n, x)); }
  void bind(int n, int x) { check(db, sqlite3_bind_int(q, n, x)); }
  void bind(int n, std::int64_t x) { check(db, sqlite3_bind_int64(q, n, x)); }
  bool row() {
    int rc = sqlite3_step(q);
    if (rc == SQLITE_ROW) return true;
    if (rc == SQLITE_DONE) return false;
    throw std::runtime_error(sqlite3_errmsg(db));
  }
  std::string text(int col) { return reinterpret_cast<const char *>(sqlite3_column_text(q, col)); }
  double number(int col) { return sqlite3_column_double(q, col); }
};
void update_state(sqlite3 *db, const std::string &id, const std::string &state, const std::string &detail) {
  Query update(db, "UPDATE missions SET state=?,detail=? WHERE mission_id=?");
  update.bind(1, state); update.bind(2, detail); update.bind(3, id); update.row();
}
}  // namespace

Ledger::Ledger(const std::string &path) {
  auto parent = std::filesystem::path(path).parent_path();
  if (!parent.empty()) std::filesystem::create_directories(parent);
  lock_ = ::open((path + ".lock").c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
  if (lock_ < 0) throw std::runtime_error("open database lock: " + std::string(std::strerror(errno)));
  try {
    if (::flock(lock_, LOCK_EX | LOCK_NB) != 0) throw std::runtime_error("another edge_core owns this database");
    const int open_result=sqlite3_open_v2(path.c_str(), &db_, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr);
    check(db_, open_result);
    sql(db_, "PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;");
    Transaction tx(db_);
    sql(db_, "CREATE TABLE IF NOT EXISTS missions(mission_id TEXT PRIMARY KEY,state TEXT NOT NULL,detail TEXT NOT NULL);"
             "CREATE UNIQUE INDEX IF NOT EXISTS exclusive_base ON missions((1)) "
             "WHERE state IN ('RUNNING','CANCELING','RECOVERY_REQUIRED');"
             "CREATE TABLE IF NOT EXISTS steps(mission_id TEXT REFERENCES missions,step INTEGER NOT NULL,"
             "x REAL NOT NULL,y REAL NOT NULL,operation_id TEXT UNIQUE,action_result TEXT,"
             "evidence_x REAL,evidence_y REAL,odom_stamp_ns INTEGER,PRIMARY KEY(mission_id,step));");
    sql(db_, "CREATE TABLE IF NOT EXISTS reference_path(mission_id TEXT REFERENCES missions,idx INTEGER NOT NULL,"
             "x REAL NOT NULL,y REAL NOT NULL,PRIMARY KEY(mission_id,idx));");
    sql(db_, audit_schema);
    sql(db_, "UPDATE missions SET state='RECOVERY_REQUIRED',detail='restart: unresolved mission; inspect physical state' "
             "WHERE state IN ('RUNNING','CANCELING');");
    tx.commit();
  } catch (...) {
    if (db_) sqlite3_close(db_);
    ::close(lock_);
    throw;
  }
}
Ledger::~Ledger() { sqlite3_close(db_); ::close(lock_); }

std::optional<Mission> Ledger::find(const std::string &id) const {
  Query m(db_, "SELECT state,detail FROM missions WHERE mission_id=?");
  m.bind(1,id);
  if (!m.row()) return std::nullopt;
  Mission mission{id, m.text(0), m.text(1), {}, {}, {}};
  Query p(db_, "SELECT x,y,evidence_x,evidence_y FROM steps WHERE mission_id=? ORDER BY step");
  p.bind(1,id);
  while (p.row()) {
    mission.targets.push_back({p.number(0),p.number(1)});
    if (sqlite3_column_type(p.q,2)!=SQLITE_NULL) mission.evidence.push_back({p.number(2),p.number(3)});
  }
  Query path(db_, "SELECT x,y FROM reference_path WHERE mission_id=? ORDER BY idx");
  path.bind(1,id);
  while (path.row()) mission.reference_path.push_back({path.number(0),path.number(1)});
  return mission;
}
std::optional<std::string> Ledger::owner() const {
  Query q(db_, "SELECT mission_id FROM missions WHERE state IN ('RUNNING','CANCELING','RECOVERY_REQUIRED')");
  if (q.row()) return q.text(0);
  return std::nullopt;
}
void Ledger::create(const std::string &id, const std::vector<Position> &targets, const std::vector<Position> &reference_path) {
  Transaction tx(db_);
  Query m(db_, "INSERT INTO missions VALUES(?,'RUNNING','accepted')");
  m.bind(1,id); m.row();
  for (std::size_t i=0;i<targets.size();++i) {
    Query p(db_, "INSERT INTO steps(mission_id,step,x,y) VALUES(?,?,?,?)");
    p.bind(1,id); p.bind(2,static_cast<int>(i)); p.bind(3,targets[i].x); p.bind(4,targets[i].y); p.row();
  }
  for (std::size_t i=0;i<reference_path.size();++i) {
    Query p(db_, "INSERT INTO reference_path VALUES(?,?,?,?)");
    p.bind(1,id); p.bind(2,static_cast<int>(i)); p.bind(3,reference_path[i].x); p.bind(4,reference_path[i].y); p.row();
  }
  tx.commit();
}
void Ledger::record_request(const std::string &id, const std::string &kind, const std::string &detail) {
  Query q(db_, "INSERT INTO mission_events(mission_id,kind,detail,cause_seq) "
               "VALUES(?1,?2,?3,(SELECT max(seq) FROM mission_events WHERE mission_id=?1))");
  q.bind(1,id); q.bind(2,kind); q.bind(3,detail); q.row();
}
void Ledger::intent(const std::string &id, int step, const std::string &operation) {
  Transaction tx(db_);
  Query p(db_, "UPDATE steps SET operation_id=? WHERE mission_id=? AND step=? AND operation_id IS NULL");
  p.bind(1,operation); p.bind(2,id); p.bind(3,step); p.row();
  if (sqlite3_changes(db_)!=1) throw std::runtime_error("attempted to dispatch a step twice");
  update_state(db_,id,"RUNNING","dispatch: "+operation);
  tx.commit();
}
void Ledger::result(const std::string &id, int step, const std::string &detail) {
  Query q(db_, "UPDATE steps SET action_result=? WHERE mission_id=? AND step=?");
  q.bind(1,detail); q.bind(2,id); q.bind(3,step); q.row();
}
void Ledger::verified(const std::string &id, int step, Position observed, std::int64_t stamp, bool last) {
  Transaction tx(db_);
  Query p(db_, "UPDATE steps SET evidence_x=?,evidence_y=?,odom_stamp_ns=? WHERE mission_id=? AND step=?");
  p.bind(1,observed.x); p.bind(2,observed.y); p.bind(3,stamp); p.bind(4,id); p.bind(5,step); p.row();
  update_state(db_,id,last?"SUCCEEDED":"RUNNING","verified waypoint "+std::to_string(step+1));
  tx.commit();
}
void Ledger::state(const std::string &id, const std::string &state, const std::string &detail) {
  update_state(db_,id,state,detail);
}
}  // namespace edge
