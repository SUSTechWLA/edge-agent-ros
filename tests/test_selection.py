"""Select consumer tests by the changed producer. Unknown paths require the full suite."""
import fnmatch
import subprocess
import unittest

STAGES = ("repository", "planner", "agent-contracts", "observations", "scene-contracts",
          "core", "motion-sensors", "motion-world", "agent-faults", "resilience",
          "console-missions", "places", "scene-binding", "scene-outage", "clock-reset", "ui", "model", "board")
R = "repository"
AGENT = ("planner", "agent-contracts", "agent-faults", "resilience", "console-missions",
         "places", "scene-binding", "ui", "model")
SCENE = ("scene-contracts", "places", "scene-binding", "scene-outage")
OBS = ("observations", "console-missions", "places", "scene-binding", "scene-outage", "clock-reset", "ui", "model")
MOTION = ("core", "motion-sensors", "motion-world", "resilience", "agent-faults", "console-missions",
          "places", "scene-binding", "scene-outage")
RULES = [
    (("agent/plugin_manager.py", "client/plugin_manager_watch.py","policies/plugin-recovery.json"),
     ("planner","observations","agent-faults","board")),
    (("tests/test_plugin_manager.py","tests/test_plugin_manager_watch.py"), ("planner",)),
    (("tests/system/plugin_manager_acceptance.py",), ("agent-faults",)),
    (("agent/module_journal.py","agent/runtime_plugins.py", "plugins/runtime/*.json", "contracts/verification-policy.json"),
     (*AGENT,"observations","board")),
    (("tests/test_module_journal.py","tests/test_author_eventbus.py"), ("planner",)),
    (("tests/system/module_bus_acceptance.py","tests/system/author_bus_acceptance.py"), ("observations",)),
    (("examples/result-verifiers/*",), ("planner","agent-contracts","agent-faults")),
    (("tests/system/runtime_plugin_acceptance.py", "tests/system/runtime_plugin_fixture.py"), ("agent-faults",)),
    (("tests/agent_verification_contract.py",), ("agent-contracts",)),
    (("tests/test_runtime_plugins.py",), ("planner",)),
    (("agent/derived_tools.py","plugins/derived/*.json"), (*AGENT,"board")),
    (("agent/author_tool.py",), ("planner","agent-faults","observations")),
    (("tests/test_derived_tools.py","tests/test_author_tool.py"), ("planner",)),
    (("tests/system/verify_sample_provider.py","tests/system/provider_contract.py","examples/timestamp-audit/*",
      "examples/derived-speed/*"), ("planner","agent-faults")),
    (("tests/test_plugin_author_watch.py",), ("planner",)),
    (("client/plugin_author_watch.py",), ("planner","observations")),
    (("tests/system/derived_tool_acceptance.py",), ("agent-faults",)),
    (("agent/contracts.json", "contracts/*.json"), ("planner","agent-contracts","agent-faults","model")),
    (("agent/service.py",), (*AGENT,"observations","board")),
    (("agent/planning.py", "agent/tracing.py"), (*AGENT,"board")),
    (("agent/*.py", "plugins/*.json", "tests/agent*_contract.py"), AGENT),
    (("scene/plugins/*.json",), (*SCENE, "planner", "agent-faults", "ui", "model")),
    (("scene/start_world.py", "scene/campus.json", "scene/layout.py"), (*SCENE,"core","motion-world","ui","clock-reset")),
    (("scene/provider.py",), (*SCENE,"clock-reset")),
    (("scene/*",), SCENE),
    (("console/server/*", "perception/*", "observations/*.json"), (*OBS,"board")),
    (("console/web/review.*","tests/system/review_browser.mjs"), ("ui","console-missions","places")),
    (("console/web/*", "tests/system/console_browser.mjs"), ("ui","console-missions")),
    (("client/event_sink.py",), ("observations","planner")),
    (("sim/bridge.yaml","sim/witness.yaml","sim/world.sdf"), (*MOTION,*OBS)),
    (("src/edge_core/*",), (*MOTION,"board")),
    (("src/edge_gateway/*","sim/*"), MOTION),
    (("compose.hardware.yaml","tests/system/board_smoke.py"), ("board",)),
    (("tests/planning_contract.py",), ("planner",)),
    (("tests/test_scene*","tests/semantic_acceptance.py","tests/scene_*acceptance.py","tests/system/scene_binding_acceptance.py"), SCENE),
    (("tests/system/motion_faults.py",), ("motion-sensors","motion-world")),
    (("tests/verify.py","tests/stop_witness.py","tests/export_ledger.py","scripts/verify.ps1"), ("core",)),
    (("tests/system/agent_faults.py","tests/system/lifecycle_acceptance.py"), ("agent-faults",)),
    (("tests/console_resilience.py",), ("resilience",)),
    (("tests/console_acceptance.py",), ("console-missions",)),
    (("tests/system/clock_reset_acceptance.py",), ("clock-reset",)),
    (("tests/model_trace_acceptance.py",), ("agent-contracts","model")),
    (("tests/repository_contract.py","tests/document_contract.py","scripts/check_*.py"), (R,)),
]


def changed_paths(base, root):
    ancestor = subprocess.check_output(["git","merge-base",base,"HEAD"],cwd=root,text=True).strip()
    def paths(*args):
        return set(subprocess.check_output(["git", *args], cwd=root).decode().strip("\0").split("\0")) - {""}
    return sorted(paths("diff","--no-renames","--name-only","-z",ancestor,"--") |
                  paths("ls-files","--others","--exclude-standard","-z"))


def selection(files=None):
    if files is None:
        return list(STAGES), {"*":"full"}
    chosen = {R}
    reasons = {}
    for name in files:
        # Documentation data and archive samples have their own evidence check.
        if (name.startswith(("docs/", "artifacts/")) or name in ("README.md","CONTRIBUTING.md","AGENTS.md")
            or name.endswith("/README.md")):
            reasons[name] = [R]
            continue
        stages = next((group for patterns,group in RULES
                       if any(fnmatch.fnmatchcase(name,p) for p in patterns)), STAGES)
        chosen.update(stages)
        reasons[name] = list(stages)
    return [stage for stage in STAGES if stage in chosen], reasons


class TestSelection(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(selection(["docs/specs/example.md"])[0], [R])
        self.assertEqual(selection(["agent/contracts.json"])[0], [R,"planner","agent-contracts","agent-faults","model"])
        for path in ("src/edge_interfaces/srv/GetMission.srv","unclassified/kernel.rs",
                     ".github/workflows/ci.yml","tests/system_suite.py","compose.yaml"):
            self.assertEqual(selection([path])[0],list(STAGES))
        self.assertIn("motion-world", selection(["src/edge_gateway/src/node.cpp"])[0])
        self.assertIn("places", selection(["perception/node.py"])[0])
        self.assertIn("motion-world", selection(["scene/campus.json"])[0])
        self.assertIn("ui", selection(["agent/planning.py"])[0])
        for path in ("agent/planning.py", "agent/tracing.py"):
            self.assertIn("board", selection([path])[0])
        for path in ("contracts/planner-profile.json", "contracts/site.json", "plugins/new-tool.json",
                     "scene/plugins/preview-route.json"):
            self.assertIn("planner", selection([path])[0], path)
        self.assertEqual(selection(["src/new-prompt.md"])[0],list(STAGES))
        self.assertIn("scene-binding", selection(["agent/service.py","scene/provider.py"])[0])
        self.assertEqual(selection(["tests/system/lifecycle_acceptance.py"])[0], [R,"agent-faults"])
        self.assertEqual(selection(["examples/timestamp-audit/provider.py"])[0], [R,"planner","agent-faults"])
        self.assertEqual(selection(["examples/result-verifiers/verifier.py"])[0], [R,"planner","agent-contracts","agent-faults"])
        self.assertIn("board",selection(["plugins/runtime/image-check.json"])[0])
        self.assertEqual(selection(["agent/author_tool.py"])[0], [R,"planner","observations","agent-faults"])
        self.assertEqual(selection(["tests/system/module_bus_acceptance.py"])[0], [R,"observations"])
        for path in ("agent/service.py", "agent/module_journal.py","agent/runtime_plugins.py","client/plugin_author_watch.py"):
            self.assertIn("observations",selection([path])[0])
        for path in ("agent/plugin_manager.py","client/plugin_manager_watch.py","policies/plugin-recovery.json"):
            self.assertEqual(selection([path])[0], [R,"planner","observations","agent-faults","board"])
        self.assertIn("board",selection(["plugins/derived/derived.speed_kmh.json"])[0])
        for path in ("console/web/review.js","tests/system/review_browser.mjs"):
            self.assertEqual(selection([path])[0], [R,"console-missions","places","ui"])


if __name__=="__main__":
    unittest.main()
