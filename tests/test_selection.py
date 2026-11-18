"""Select consumer tests by the changed producer. Unknown paths require the full suite."""
import fnmatch
import subprocess
import unittest

STAGES = ("repository", "planner", "agent-contracts", "observations", "scene-contracts",
          "core", "motion-sensors", "motion-world", "agent-faults", "resilience",
          "console-missions", "places", "scene-binding", "scene-outage", "clock-reset", "ui", "model", "board",
          "plugin-isolation")
R = "repository"
AGENT = ("planner", "agent-contracts", "agent-faults", "resilience", "console-missions",
         "places", "scene-binding", "ui", "model")
SCENE = ("scene-contracts", "places", "scene-binding", "scene-outage")
OBS = ("observations", "console-missions", "places", "scene-binding", "scene-outage", "clock-reset", "ui", "model")
MOTION = ("core", "motion-sensors", "motion-world", "resilience", "agent-faults", "console-missions",
          "places", "scene-binding", "scene-outage")
RULES = [
    (("perception/map3d/*", "perception/sim_station.py", "map-inputs/*", "tests/test_map3d_ros.py", "tests/test_map3d.py", "tests/test_lidar_inputs_ros.py",
      "perception/camera/*", "compose.camera*.yaml", "perception/rgbd/*", "compose.rgbd*.yaml", "tests/test_rgbd.py"),
     ("planner", "observations", "board", "plugin-isolation")),
    (("model_provider/*", "compose.model-provider*.yaml", "tests/test_planner_model_selection.py"),
     ("planner", "model", "plugin-isolation")),
    (("tests/system/map_model_isolation.py", "tests/system/map3d_browser.mjs", "tests/system/camera_acceptance.py",
      "tests/system/sensor_plugin_isolation.py", "tests/system/rgbd_acceptance.py", "tests/system/rgbd_browser.mjs"),
     ("plugin-isolation",)),
    (("console/web/view-plugins.js","console/web/view-plugins/*"), ("ui","plugin-isolation")),
    (("agent/series_tools.py",), (*AGENT,"observations","board")),
    (("client/plugin_design_watch.py","client/design_contracts.py","client/plugin_design.py",
      "client/software_author_watch.py","client/bounded_http.py","contracts/design/*"),
     ("planner","observations","agent-faults","board")),
    (("client/software_provider.py","client/software_release.py","examples/software-provider/*"), ("planner","agent-faults")),
    (("client/watchdog.py",), ("planner","observations","board")),
    (("tests/test_plugin_design.py","tests/test_plugin_design_watch.py","tests/test_software_author_watch.py",
      "tests/test_series_tools.py","tests/test_watchdog.py","tests/test_software_provider.py"), ("planner",)),
    (("tests/system/watchdog_acceptance.py",), ("observations",)),
    (("tests/system/watchdog_mounts.py","tests/system/evolution_mounts.py"), ("observations","board")),
    (("tests/system/event_backlog_probe.py",), ("observations",)),
    (("tests/system/software_provider_acceptance.py","tests/system/software_author_fixture.py"), ("agent-faults",)),
    (("tests/system/series_author_acceptance.py","tests/system/plugin_design_acceptance.py"), ("agent-faults",)),
    (("sessions_plugin/*", "client/register_session.py"), ("planner","observations","ui","console-missions","board")),
    (("tests/system/session_plugin_acceptance.py",), ("ui","console-missions","places")),
    (("evidence/*", "client/register_evidence.py"), ("planner","observations","ui","console-missions","board")),
    (("tests/system/session_evidence_acceptance.py", "tests/system/session_browser.mjs"), ("ui","console-missions","places")),
    (("tests/system/evidence_model_acceptance.py",), ("model","ui","places")),
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
    (("client/event_sink.py",), ("planner","observations","agent-faults","board")),
    (("sim/bridge.yaml","sim/witness.yaml","sim/world.sdf"), (*MOTION,*OBS)),
    (("src/edge_core/*",), (*MOTION,"board")),
    (("src/edge_gateway/*","sim/*"), MOTION),
    (("compose.hardware.yaml","tests/system/board_smoke.py","tests/system/realsense_board.py"), ("board",)),
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
    # Browser and evidence acceptance consume successful and blocked Gazebo tasks.
    # Include their fixture producers even when only web/query code changed.
    if "ui" in chosen:
        chosen.update(("console-missions", "places"))
    return [stage for stage in STAGES if stage in chosen], reasons


class TestSelection(unittest.TestCase):
    def test_map_and_model_plugin_consumers(self):
        self.assertEqual(selection(["perception/map3d/provider.py"])[0],
                         [R,"planner","observations","board","plugin-isolation"])
        self.assertEqual(selection(["model_provider/provider.py"])[0],[R,"planner","model","plugin-isolation"])
        self.assertEqual(selection(["tests/system/map_model_isolation.py"])[0],[R,"plugin-isolation"])
        for plugin in ("compose.rgbd-hardware.yaml","perception/rgbd/pairs.py","map-inputs/points.json","perception/camera/frames.py"):
            self.assertEqual(selection([plugin])[0],[R,"planner","observations","board","plugin-isolation"])

    def test_evolution_consumers(self):
        for name in ("client/plugin_design.py","client/plugin_design_watch.py","client/event_sink.py",
                     "contracts/design/ros-observation.json","client/design_contracts.py","client/bounded_http.py",
                     "client/software_author_watch.py"):
            self.assertEqual(selection([name])[0],[R,"planner","observations","agent-faults","board"])
        self.assertEqual(selection(["client/software_provider.py"])[0],[R,"planner","agent-faults"])
        self.assertEqual(selection(["client/watchdog.py"])[0],[R,"planner","observations","board"])
        self.assertEqual(selection(["tests/system/watchdog_mounts.py"])[0],[R,"observations","board"])
        self.assertEqual(selection(["tests/system/event_backlog_probe.py"])[0],[R,"observations"])
        chosen = selection(["agent/series_tools.py"])[0]
        for stage in ("planner","agent-contracts","agent-faults","observations","model","board"):
            self.assertIn(stage,chosen)

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
        for path in ("console/web/sessions.js", "tests/system/session_browser.mjs",
                     "tests/system/session_evidence_acceptance.py"):
            self.assertEqual(selection([path])[0], [R,"console-missions","places","ui"])
        for path in ("evidence/provider.py", "evidence/public_source.py", "client/register_evidence.py"):
            selected = selection([path])[0]
            for consumer in ("planner", "observations", "console-missions", "places", "ui", "board"):
                self.assertIn(consumer, selected, path)
        for path in ("sessions_plugin/provider.py", "client/register_session.py"):
            for consumer in ("observations", "ui", "places", "board"):
                self.assertIn(consumer, selection([path])[0], path)


if __name__=="__main__":
    unittest.main()
