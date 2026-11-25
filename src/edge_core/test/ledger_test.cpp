#include "ledger.hpp"
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <unistd.h>

namespace {
void require(bool condition, const char *description) {
  if (!condition) throw std::runtime_error(description);
}

template<typename F>
void must_refuse(F operation, const char *description) {
  try { operation(); }
  catch (const std::runtime_error &) { return; }
  throw std::runtime_error(description);
}

void exercise(const std::string &path) {
  const std::vector<edge::Position> points{{0.8,0.0},{0.8,0.7}};
  {
    edge::Ledger db(path);
    db.create("complete",points);
    must_refuse([&] { edge::Ledger second(path); }, "simultaneous writer admitted");
    must_refuse([&] { db.create("competing",points); }, "second chassis owner admitted");
    require(!db.find("competing"),"rejected mission was persisted");
    db.intent("complete",0,"one");
    must_refuse([&] { db.intent("complete",0,"one-more"); }, "same step dispatched twice");
    db.result("complete",0,"stopped");
    db.verified("complete",0,{0.78,0.0},100,false);
    require(db.find("complete")->state=="RUNNING","premature completion");
    db.intent("complete",1,"two");
    db.result("complete",1,"stopped");
    db.verified("complete",1,{0.79,0.69},200,true);
    db.create("interrupted",{points.back()},points);
    db.intent("interrupted",0,"unconfirmed");
  }
  {
    edge::Ledger db(path);
    auto complete=db.find("complete");
    require(complete && complete->state=="SUCCEEDED","lost completed mission on restart");
    require(complete->targets==points,"lost target binding");
    require(complete->evidence==std::vector<edge::Position>{{0.78,0.0},{0.79,0.69}},"evidence mismatch");
    require(db.find("interrupted")->state=="RECOVERY_REQUIRED","unconfirmed action became executable");
    require(db.find("interrupted")->reference_path==points && db.find("interrupted")->targets.size()==1,
            "continuous geometry and terminal verification must survive restart");
    require(db.owner()=="interrupted","restart released unresolved resource");
    must_refuse([&] { db.create("after-restart",points); }, "recovery hold ignored");
  }
}
}

int main() {
  char directory[]="/tmp/edge-ledger-test-XXXXXX";
  if (mkdtemp(directory)==nullptr) return 1;
  int status=0;
  try { exercise((std::filesystem::path(directory)/"ledger.db").string()); }
  catch (const std::exception &error) { std::cerr<<error.what()<<'\n'; status=1; }
  std::filesystem::remove_all(directory);
  return status;
}
