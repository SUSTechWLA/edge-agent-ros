"""Select consumer tests by the changed producer. Unknown paths require the full suite."""
import fnmatch
import subprocess
import unittest

STAGES = ("repository", "planner", "agent-contracts", "observations", "scene-contracts",
          "core", "motion-sensors", "motion-world", "agent-faults", "resilience",
          "console-missions", "places", "scene-binding", "scene-outage", "clock-reset", "ui", "model", "board",
          "plugin-isolation", "campus-world", "rover-commissioning")
R = "repository"
AGENT = ("planner", "agent-contracts", "agent-faults", "resilience", "console-missions",
         "places", "scene-binding", "ui", "model")
SCENE = ("scene-contracts", "places", "scene-binding", "scene-outage")
OBS = ("observations", "console-missions", "places", "scene-binding", "scene-outage", "clock-reset", "ui", "model")
MOTION = ("core", "motion-sensors", "motion-world", "resilience", "agent-faults", "console-missions",
          "places", "scene-binding", "scene-outage")
RULES = [
    (("analysis/*","tests/test_offline_analysis.py","tests/test_conformance.py"), (R,)),
    (("tests/test_plugin_conformance.py","tests/fixtures/plugin-conformance/*"), ("board",)),
    (("agent/facts_server.py",), ("observations","board","ui")),
    (("tests/test_console_facts.py",), ("observations",)),
    (("tests/test_capability_catalog.py", "tests/test_status_plugin.py"), ("agent-contracts",)),
    (("compose.body-runtime.yaml", "sim/body-bridge.yaml", "tests/system/body_runtime*.py"), ("motion-world",)),
    (("edge_contracts/*",), STAGES),
    (("navigation/*.py", "perception/spatial_facts/*.py", "scripts/prepare_navigation.py",
      "scripts/prepare_perception_site.py", "examples/stationary-perception/*", "compose.stationary-perception.yaml",
      "scripts/navigation_odometry_bench.py", "compose.navigation-*.yaml",
      "tests/test_navigation_pack.py"), ("planner", "observations", "board", "plugin-isolation")),
    (("tests/test_navigation_planner.py", "tests/test_navigation_physical.py", "tests/test_spatial_facts.py",
      "tests/test_spatial_gateway.py"),
     ("planner", "board")),
    (("tests/test_spatial_image_capture.py", "tests/test_livox_capture.py", "tests/test_stationary_odom.py",
      "tests/test_stationary_fusion.py", "tests/test_visual_grounding.py", "tests/test_perception_server.py",
      "tests/test_perception_site.py", "task_recorder/spatial_capture.py", "tests/test_spatial_capture_archive.py",
      "task_recorder/review_media.py", "task_recorder/evolution_live.py", "task_recorder/sensor_review.py", "tests/test_sensor_review.py",
      "task_recorder/fusion_review.py", "tests/test_fusion_review.py", "task_recorder/target_review.py",
      "tests/test_target_review.py", "scripts/verify_perception_candidate.py", "tests/test_verify_perception_candidate.py",
      "tests/test_soft_projection_evolution.py", "scripts/evolve_read_projection.py",
      "scripts/evolve_read_projection.sh", "tests/test_evolve_read_projection.py"),
     ("planner", "board")),
    (("client/soft_projection_evolution.py",), ("planner", "observations", "agent-faults", "board")),
    (("tests/test_reference_utterances.py",), ("planner",)),
    (("tests/system/navigation_recording_check.py",), ("board",)),
    (("tests/test_package_boundaries.py","tests/test_kernel.py"), (R,)),
    (("agent/kernel/*",), (*AGENT,"observations","board","plugin-isolation")),
    (("agent/site_pack.py","profiles/*.json","capabilities/*/pack.json","scripts/robot-plugins.sh",
      "scripts/check_board_plugins.py","compose.soft-runtime-hardware.yaml"), ("planner","board","plugin-isolation")),
    (("tests/test_site_pack.py","tests/test_board_plugins.py","tests/system/board_pack_acceptance.py",
      "tests/system/deploy_projection.py"), ("board",)),
    (("agent/physical_*.py","agent/plugin_tracks.py","scripts/prepare_physical_camera.py","perception/camera/physical*.py",
      "compose.physical-camera*.yaml","tests/system/physical_camera*.py"), (*AGENT,"observations","board","plugin-isolation")),
    (("tests/test_physical_*.py","tests/test_plugin_tracks.py"), ("planner","plugin-isolation")),
    (("tests/system/model_readiness.py","tests/test_model_readiness.py"), ("planner","agent-faults")),
    (("tests/test_plugin_tracks_metrics.py",), ("planner","places","resilience","agent-faults")),
    (("tests/system/plugin_tracks_metrics.py","tests/fixtures/plugin-tracks/frozen-missions.json"),
     ("places","resilience","agent-faults")),
    (("agent/soft_*.py","agent/v2_registry.py","agent/plugin_descriptor.py","compose.soft-runtime.yaml",
      "scripts/prepare_soft_pilots.py","console/server/session_service.py"), (*AGENT,"observations","board","plugin-isolation")),
    (("tests/test_soft_*.py","tests/test_v2_registry.py","tests/test_plugin_descriptor.py",
      "tests/fixtures/plugin-tracks/*"), ("planner",)),
    (("tests/system/soft_runtime_acceptance.py",), ("plugin-isolation",)),
    (("tests/system/recording_board_smoke.py","scripts/package_robot.py"), ("board",)),
    (("task_recorder/global_map.py","task_recorder/live_map.*","task_recorder/service.py",
      "tests/test_live_exploration_map.py","tests/test_recording_global_map.py"), ("board","campus-world")),
    (("exploration/frontier.py","exploration/map_artifact.py"), ("board","campus-world","rover-commissioning")),
    (("task_recorder/map_view.py","task_recorder/map-view.html","tests/test_recording_map_view.py",
      "tests/system/recording_map_acceptance.py","tests/system/recording_map_browser.py",
      "tests/system/recorder_campus_binding.py","tests/system/recording_camera_fixture.py"), ("campus-world",)),
    (("task_recorder/collect.py","task_recorder/ros_source.py","compose.task-recorder.yaml",
      "scripts/record-task.*","tests/test_task_recording_source.py","tests/test_recorder_cli.py"), ("board","campus-world")),
    (("task_recorder/render.py","task_recorder/showcase.py",
      "compose.task-recorder-campus.yaml","tests/test_recording_trace_view.py"), ("campus-world",)),
    (("task_recorder/*",), ("board",)),
    (("compose.task-recorder*.yaml","tests/test_recorder*.py",
      "tests/test_task_record*.py","tests/test_recording_trace_view.py","tests/system/recorder*","tests/system/task_recorder*"),(R,)),
    (("tests/system/record_unknown_campus.py","tests/system/unknown_campus*",
      "tests/system/campus_survey*","tests/system/campus_revisit.py",
      "tests/test_campus_survey_worker.py","tests/test_campus_revisit.py"),(R,"campus-world")),
    (("tests/test_mission_recording.py",), ("rover-commissioning",)),
    (("tests/system/commission_recording.py", "tests/system/long_mission_world.py",
      "tests/system/record_long_mission.py", "tests/system/render_rover_video.py", "tests/system/mission_watch_page.py"),
     (R,)),
    (("scripts/release_robot.py", "tests/test_robot_release.py"), (R,"board")),
    (("scripts/robot.sh", "calibration/ros_capture.py", "calibration/targets.py", "calibration/station/*",
      "tests/test_factory_targets.py", "tests/system/factory_ros_capture.py"), ("board",)),
    (("calibration/*", "tests/test_factory_calibration.py"), ("board","rover-commissioning")),
    (("exploration/*", "tests/test_exploration_frontier.py", "tests/system/commission_exploration.py"),
     ("rover-commissioning",)),
    (("perception/sensor_health/*","compose.sensor-health.yaml","tests/system/sensor_health_acceptance.py",
      "tests/system/sensor_health_restart.py","tests/test_sensor_incidents.py"),
     ("plugin-isolation",)),
    (("tests/test_map_texture_pack.py","tests/system/campus_texture_browser.mjs"),("plugin-isolation","campus-world")),
    (("scene/campus_large/*","compose.campus.yaml","tests/system/campus_*.py","tests/test_campus_*.py",
      "tests/system/field_sensor_task.py","tests/system/field_observation_soak.py"),
     ("campus-world",)),
    (("perception/map3d/web/*","perception/map3d/texture.py","perception/map3d/provider.py"),
     ("planner","observations","board","plugin-isolation","campus-world")),
    (("perception/map3d/*", "perception/sim_station.py", "map-inputs/*", "tests/test_map3d_ros.py", "tests/test_map3d.py", "tests/test_lidar_inputs_ros.py",
      "perception/camera/*", "compose.camera*.yaml", "perception/rgbd/*", "compose.rgbd*.yaml", "tests/test_rgbd.py"),
     ("planner", "observations", "board", "plugin-isolation")),
    (("model_provider/*", "compose.model-provider*.yaml", "tests/test_planner_model_selection.py"),
     ("planner", "model", "plugin-isolation")),
    (("tests/map_planar.test.mjs", "tests/map_spatial.test.mjs", "tests/system/map_model_isolation.py", "tests/system/map3d_browser.mjs", "tests/system/camera_acceptance.py",
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
    (("agent/module_journal.py",), (*AGENT,"observations","board","rover-commissioning")),
    (("agent/runtime_plugins.py", "plugins/runtime/*.json", "contracts/verification-policy.json"),
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
    (("perception/node.py",), (*OBS,"board","rover-commissioning")),
    (("console/server/*", "perception/*", "observations/*.json"), (*OBS,"board")),
    (("console/web/review.*","tests/system/review_browser.mjs"), ("ui","console-missions","places")),
    (("console/web/*", "tests/system/console_browser.mjs"), ("ui","console-missions")),
    (("client/event_sink.py",), ("planner","observations","agent-faults","board")),
    (("sim/bridge.yaml","sim/witness.yaml","sim/world.sdf"), (*MOTION,*OBS,"rover-commissioning")),
    (("src/edge_core/*",), (*MOTION,"board","rover-commissioning")),
    (("src/edge_gateway/*",), (*MOTION,"rover-commissioning")),
    (("sim/*",), MOTION),
    (("compose.hardware.yaml","compose.hardware-observe.yaml","client/hardware_preflight.py","tests/test_board_preflight.py",
      "tests/system/board_smoke.py","tests/system/realsense_board.py","tests/system/navigation_contract.py"), ("board",)),
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
    def test_navigation_route_observation_and_body_consumers(self):
        for path in ("navigation/planner.py", "navigation/route_service.py", "perception/spatial_facts/gateway.py",
                     "scripts/prepare_navigation.py", "scripts/navigation_odometry_bench.py",
                     "compose.navigation-camera-info.yaml", "compose.navigation-readonly.yaml", "tests/test_navigation_pack.py",
                     "scripts/prepare_perception_site.py", "examples/stationary-perception/jetson-rover.json",
                     "compose.stationary-perception.yaml"):
            self.assertEqual(selection([path])[0], [R, "planner", "observations", "board", "plugin-isolation"], path)
        for path in ("tests/test_navigation_planner.py", "tests/test_navigation_physical.py", "tests/test_spatial_facts.py",
                     "tests/test_spatial_gateway.py"):
            self.assertEqual(selection([path])[0], [R, "planner", "board"], path)
        for path in ("edge_contracts/navigation.py", "edge_contracts/spatial_facts.py", "edge_contracts/occupancy.py"):
            self.assertEqual(selection([path])[0], list(STAGES), path)
        for path in ("agent/physical_navigation.py", "agent/kernel/catalog.py"):
            self.assertTrue({"planner", "board", "plugin-isolation"} <= set(selection([path])[0]), path)
        self.assertEqual(selection(["tests/test_reference_utterances.py"])[0], [R, "planner"])
        for file in ("tests/test_spatial_image_capture.py", "tests/test_livox_capture.py", "tests/test_stationary_odom.py",
                     "tests/test_stationary_fusion.py", "tests/test_visual_grounding.py", "tests/test_perception_server.py",
                     "tests/test_perception_site.py", "task_recorder/spatial_capture.py", "tests/test_spatial_capture_archive.py",
                     "task_recorder/review_media.py", "task_recorder/evolution_live.py", "task_recorder/sensor_review.py", "tests/test_sensor_review.py",
                     "task_recorder/fusion_review.py", "tests/test_fusion_review.py", "task_recorder/target_review.py",
                     "tests/test_target_review.py", "scripts/verify_perception_candidate.py",
                     "tests/test_verify_perception_candidate.py", "tests/test_soft_projection_evolution.py",
                     "scripts/evolve_read_projection.py", "scripts/evolve_read_projection.sh", "tests/test_evolve_read_projection.py"):
            self.assertEqual(selection([file])[0], [R, "planner", "board"], file)
        self.assertEqual(selection(["client/soft_projection_evolution.py"])[0],
                         [R, "planner", "observations", "agent-faults", "board"])
        self.assertEqual(selection(["tests/system/navigation_recording_check.py"])[0], [R, "board"])

    def test_console_facts_run_observations(self):
        self.assertEqual(selection(["tests/test_console_facts.py"])[0], [R, "observations"])
        for stage in ("observations","board","ui"):
            self.assertIn(stage, selection(["agent/facts_server.py"])[0])

    def test_status_and_catalog_use_agent_contracts(self):
        for path in ("tests/test_status_plugin.py", "tests/test_capability_catalog.py"):
            self.assertIn("agent-contracts", selection([path])[0])

    def test_body_runtime_exercises_the_motion_world(self):
        for path in ("compose.body-runtime.yaml", "sim/body-bridge.yaml", "tests/system/body_runtime_acceptance.py"):
            self.assertEqual(selection([path])[0], [R, "motion-world"], path)

    def test_contract_package_runs_all_consumers(self):
        self.assertEqual(selection(["edge_contracts/events.py"])[0], list(STAGES))
        self.assertEqual(selection(["tests/test_package_boundaries.py"])[0], [R])
        self.assertEqual(selection(["tests/test_kernel.py"])[0], [R])
        self.assertEqual(set(selection(["agent/kernel/ledger.py"])[0]),
                         {R, *AGENT, "observations", "board", "plugin-isolation"})

    def test_navigation_contract_runs_against_the_board_image(self):
        self.assertEqual(selection(["tests/system/navigation_contract.py"])[0], [R, "board"])
        self.assertIn("board", selection(["src/edge_core/src/core_node.cpp"])[0])
        self.assertIn("board", selection(["src/edge_interfaces/action/NavigateTo.action"])[0])

    def test_offline_analysis_and_provider_conformance(self):
        self.assertEqual(selection(["edge_contracts/conformance.py"])[0], list(STAGES))
        for path in ("analysis/__main__.py", "analysis/examples/expected.json",
                     "tests/test_offline_analysis.py", "tests/test_conformance.py"):
            self.assertEqual(selection([path])[0], [R], path)
        for path in ("task_recorder/manage.py", "tests/test_plugin_conformance.py",
                     "tests/fixtures/plugin-conformance/status.cases.json"):
            self.assertEqual(selection([path])[0], [R,"board"], path)

    def test_packs_run_their_board_consumers(self):
        for path in ("agent/site_pack.py","profiles/bench-camera.json","capabilities/camera/pack.json",
                     "scripts/robot-plugins.sh","scripts/check_board_plugins.py","compose.soft-runtime-hardware.yaml"):
            self.assertEqual(selection([path])[0], [R,"planner","board","plugin-isolation"], path)
        for name in ("tests/system/board_pack_acceptance.py","tests/system/deploy_projection.py"):
            self.assertEqual(selection([name])[0], [R,"board"], name)

    def test_cold_model_readiness_runs_its_consumers(self):
        for path in ("tests/system/model_readiness.py","tests/test_model_readiness.py"):
            self.assertEqual(selection([path])[0],[R,"planner","agent-faults"])

    def test_frozen_mission_witnesses(self):
        self.assertEqual(selection(["tests/test_plugin_tracks_metrics.py"])[0],
                         [R,"planner","agent-faults","resilience","places"])
        for name in ("tests/system/plugin_tracks_metrics.py","tests/fixtures/plugin-tracks/frozen-missions.json"):
            self.assertEqual(set(selection([name])[0]),{R,"places","resilience","agent-faults"})

    def test_physical_capture_consumers(self):
        for path in ("agent/physical_controller.py","agent/plugin_tracks.py","perception/camera/physical.py",
                     "compose.physical-camera.yaml","tests/system/physical_camera_acceptance.py"):
            self.assertIn("board", selection([path])[0])
            self.assertIn("plugin-isolation", selection([path])[0])

    def test_soft_release_consumers(self):
        for path in ("agent/soft_host.py","agent/plugin_descriptor.py","agent/soft_client.py",
                     "agent/v2_registry.py","compose.soft-runtime.yaml","scripts/prepare_soft_pilots.py",
                     "console/server/session_service.py"):
            for consumer in ("planner","observations","agent-contracts","agent-faults","ui","board","plugin-isolation"):
                self.assertIn(consumer,selection([path])[0],path)
        self.assertEqual(selection(["tests/fixtures/plugin-tracks/v1-expected.json"])[0],[R,"planner"])
        self.assertEqual(selection(["tests/system/soft_runtime_acceptance.py"])[0],[R,"plugin-isolation"])

    def test_optional_recorder_and_campus_consumers(self):
        for path in ("task_recorder/ros_source.py","task_recorder/collect.py","compose.task-recorder.yaml",
                     "scripts/record-task.sh","scripts/record-task.ps1","tests/test_task_recording_source.py"):
            self.assertEqual(selection([path])[0], [R,"board","campus-world"], path)
        for path in ("scripts/package_robot.py","tests/system/recording_board_smoke.py"):
            self.assertEqual(selection([path])[0], [R,"board"], path)
        for path in ("task_recorder/service.py","task_recorder/live_map.html","task_recorder/live_map.py",
                     "task_recorder/global_map.py","tests/test_live_exploration_map.py","tests/test_recording_global_map.py"):
            self.assertEqual(selection([path])[0], [R,"board","campus-world"], path)
        for path in ("tests/system/recording_map_acceptance.py","tests/system/recording_camera_fixture.py",
                     "tests/system/recorder_campus_binding.py","tests/system/recording_map_browser.py",
                     "task_recorder/map_view.py","task_recorder/map-view.html","tests/test_recording_map_view.py",
                     "task_recorder/render.py","task_recorder/showcase.py",
                     "tests/test_recording_trace_view.py","tests/system/record_unknown_campus.py"):
            self.assertEqual(selection([path])[0], [R,"campus-world"], path)

    def test_recording_fixtures_stay_off_board(self):
        for name in ("commission_recording", "long_mission_world", "record_long_mission", "render_rover_video", "mission_watch_page"):
            self.assertEqual(selection([f"tests/system/{name}.py"])[0], [R], name)
        self.assertEqual(selection(["tests/test_mission_recording.py"])[0], [R,"rover-commissioning"])
        self.assertEqual(selection(["tests/system/record_long_mission.py","exploration/frontier.py"])[0],
                         [R,"board","campus-world","rover-commissioning"])

    def test_rover_commissioning_consumers(self):
        self.assertEqual(selection(["deploy/robot/README.md"])[0], [R])
        for name in ("scripts/release_robot.py", "tests/test_robot_release.py"):
            self.assertEqual(selection([name])[0], [R,"board"], name)
        for name in ("scripts/robot.sh", "calibration/targets.py", "calibration/ros_capture.py",
                     "calibration/station/bindings.example.json", "tests/test_factory_targets.py",
                     "tests/system/factory_ros_capture.py"):
            self.assertEqual(selection([name])[0], [R,"board"], name)
        for name in ("calibration/checks.py", "calibration/factory.py", "tests/test_factory_calibration.py"):
            self.assertEqual(selection([name])[0], [R,"board","rover-commissioning"], name)
        for name in ("exploration/frontier.py","exploration/map_artifact.py"):
            self.assertEqual(selection([name])[0], [R,"board","campus-world","rover-commissioning"], name)
        for name in ("exploration/ros_sim.py",
                     "tests/test_exploration_frontier.py", "tests/system/commission_exploration.py"):
            self.assertEqual(selection([name])[0], [R,"rover-commissioning"], name)
        for name in ("perception/node.py", "agent/module_journal.py", "sim/bridge.yaml", "sim/world.sdf",
                     "src/edge_core/src/node.cpp", "src/edge_gateway/src/node.cpp", "client/mission.py"):
            self.assertIn("rover-commissioning", selection([name])[0], name)

    def test_map_and_model_plugin_consumers(self):
        self.assertEqual(selection(["perception/map3d/provider.py"])[0],
                         [R,"planner","observations","board","plugin-isolation","campus-world"])
        self.assertIn("campus-world", selection(["perception/map3d/web/mesh.js"])[0])
        self.assertEqual(selection(["model_provider/provider.py"])[0],[R,"planner","model","plugin-isolation"])
        self.assertEqual(selection(["tests/system/map_model_isolation.py"])[0],[R,"plugin-isolation"])
        self.assertEqual(selection(["tests/map_planar.test.mjs","tests/map_spatial.test.mjs"])[0],[R,"plugin-isolation"])
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
        self.assertEqual(selection(["client/hardware_preflight.py"])[0],[R,"board"])
        self.assertEqual(selection(["tests/system/campus_texture_browser.mjs"])[0],[R,"plugin-isolation","campus-world"])
        self.assertEqual(selection(["scene/campus_large/build.py","compose.campus.yaml"])[0],[R,"campus-world"])
        for name in ("campus_route_geometry","campus_station_probe","campus_forms","campus_round_trip"):
            self.assertEqual(selection(["tests/system/"+name+".py"])[0],[R,"campus-world"])
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
