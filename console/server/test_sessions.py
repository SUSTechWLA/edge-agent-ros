"""Task stories preserve evidence, state ownership and bounded attention."""
from contextlib import closing
import json
import sqlite3
from urllib.error import HTTPError
from urllib.request import urlopen
import unittest

from sessions import session_view
import test_run_review


class SessionTest(unittest.TestCase):
    setUp = test_run_review.RunReviewTest.setUp
    fetch = test_run_review.RunReviewTest.fetch
    def create_task(self, key="route", state="BLOCKED", trace="work", steps=None):
        with closing(sqlite3.connect(self.tasks)) as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY,text,state,detail,created,updated,provider,steps);
                CREATE TABLE IF NOT EXISTS task_triggers(task_id TEXT PRIMARY KEY,trace_id);""")
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",(
                key,"前往门口巡检",state,"原任务结果","2026-01-01T00:00:00Z",
                "2026-01-01T00:01:00Z","local",json.dumps(steps or [{"tool":"inspect","args":{"points":[[2,1]]},"status":"RUNNING"}])))
            db.execute("INSERT INTO task_triggers VALUES(?,?)",(key,trace))
            db.commit()

    def record(self, kind, task="route", **kwargs):
        trace=kwargs.pop("trace",{})
        return self.history.record({"session_id":"console-instance"},kind=kind,source="mission_agent/tasks",
            detail=kwargs.pop("detail","fixture"),trace={"task_id":task,"trace_id":"work",**trace},**kwargs)

    def test_braking_story_references_values_and_suppresses_polling(self):
        self.create_task()
        self.record("agent.accepted")
        self.record("agent.planned")
        for _ in range(130):
            self.record("agent.ros.response",detail="poll completed",data={"irrelevant":"x"*1000})
        braking=self.record("action.braking",level="warn",trace={"code":"navigation.path_blocked"},
                            data={"front_range_m":0.395})
        stopped=self.record("action.result",trace={"code":"navigation.path_blocked"},
                            detail="stationary odometry confirmed [code:navigation.path_blocked]")
        blocked=self.record("agent.blocked",level="warn",trace={"code":"navigation.path_blocked"},
                            data={"payload":{"diagnostic":{"code":"navigation.path_blocked","origin":"gateway",
                                                         "catalog":{"hint":"检查前方净空"}}}})
        self.record("plugin.author.declined",level="warn",trace={"code":"author.source_ineligible"})
        doc=self.fetch("/api/session",task_id="route")
        self.assertEqual(doc["scope"],{"task_id":"route","trace_id":"work"})
        self.assertIn("0.395",doc["markdown"])
        self.assertIn("停稳",doc["markdown"])
        self.assertNotIn("poll completed",doc["markdown"])
        self.assertEqual(doc["task"]["state"],"BLOCKED")
        self.assertEqual(doc["diagnosis"]["code"],"navigation.path_blocked")
        self.assertIn("导航",doc["messages"][-1]["text"])
        refs={ref["seq"] for message in doc["messages"] for ref in message["refs"]}
        self.assertTrue({braking,stopped,blocked}.issubset(refs))
        self.assertLess(len(doc["messages"]),12)
        self.assertGreater(doc["coverage"]["collapsed_events"],125)
        self.assertLess(doc["coverage"]["summary_bytes"],doc["coverage"]["public_payload_bytes"]*.1)
        with urlopen(self.url+"/api/session?task_id=route&format=markdown") as response:
            self.assertEqual(response.headers.get_content_type(),"text/markdown")
            self.assertEqual(response.read().decode(),doc["markdown"])

    def test_current_success_keeps_prior_fault_historical_and_related_tasks_distinct(self):
        self.create_task(state="SUCCEEDED",steps=[{"tool":"status","status":"SUCCEEDED"}])
        self.create_task(key="followup",state="RUNNING")
        self.record("camera.custom_fault",level="error",detail="镜头观测失败",
                    trace={"code":"camera.smudged","module":"camera.plugin"})
        for _ in range(20):
            self.record("agent.retry",level="warn",trace={"code":"network.retry"})
        self.record("agent.verified")
        doc=self.fetch("/api/session",task_id="route")
        self.assertIn("任务账本已记录成功",doc["headline"])
        self.assertIn("已完成步骤 1 / 1",doc["messages"][-1]["text"])
        self.assertTrue(doc["diagnosis"]["historical"])
        self.assertTrue(any(m["text"]=="镜头观测失败" and m["title"]=="曾发生的问题" for m in doc["messages"]))
        self.assertTrue(any(m.get("occurrences")==20 for m in doc["messages"]))
        self.assertEqual([t["id"] for t in doc["related_tasks"]],["followup"])
        latest=self.fetch("/api/session",trace_id="work")
        self.assertEqual(latest["scope"]["task_id"],"followup")
        self.assertEqual(latest["task"]["state"],"RUNNING")

    def test_audit_self_events_do_not_expand_summary(self):
        self.create_task()
        self.record("agent.accepted")
        self.record("agent.blocked",level="warn",detail="能力尚未安装")
        before=self.fetch("/api/session",task_id="route")
        for i in range(40):
            self.record("evidence.query_failed",level="error",detail="query audit",
                        trace={"module":"evidence.query","code":"evidence.sql_denied"},operation_id=str(i))
        after=self.fetch("/api/session",task_id="route")
        self.assertEqual(before["messages"],after["messages"])
        self.assertEqual(after["coverage"]["audit_events"],40)
        self.assertNotIn("query audit",after["markdown"])

    def test_missing_task_archive_legacy_and_unavailable_journal(self):
        with self.assertRaises(HTTPError) as failure:
            self.fetch("/api/session",task_id="unknown")
        self.assertEqual(failure.exception.code,503)
        self.assertFalse(self.tasks.exists())
        self.record("sensor.custom_failure",task="orphan",level="error",detail="设备未响应")
        doc=self.fetch("/api/session",task_id="orphan")
        self.assertFalse(doc["coverage"]["task_available"])
        self.assertTrue(doc["coverage"]["notes"])
        self.assertIn("设备未响应",doc["markdown"])
        self.create_task(state="CANCELED")
        doc=self.fetch("/api/session",task_id="route")
        self.assertIn("任务账本已记录取消",doc["headline"])
        self.assertEqual(doc["coverage"]["archived_events"],0)
        self.assertTrue(doc["coverage"]["notes"])
        self.assertEqual(self.fetch("/api/sessions")["counts"]["all"],1)
        with self.assertRaises(HTTPError) as failure:
            self.fetch("/api/session",task_id="missing")
        self.assertEqual(failure.exception.code,404)
        for params in ({},{"task_id":"route","trace_id":"work"},{"task_id":"route","format":"html"}):
            with self.assertRaises(HTTPError) as failure:
                self.fetch("/api/session",**params)
            self.assertEqual(failure.exception.code,400)

    def test_long_unknown_faults_have_budget_and_explicit_coverage(self):
        self.create_task(state="SUCCEEDED")
        terminal=self.record("agent.verified")
        for i in range(35):
            self.record("custom.failure."+str(i),level="error",detail="failure"+"x"*10000,operation_id=str(i))
        doc=self.fetch("/api/session",task_id="route")
        self.assertTrue(doc["coverage"]["truncated"])
        self.assertEqual(doc["coverage"]["archived_events"],36)
        self.assertLessEqual(len(doc["messages"]),10)
        self.assertLess(len(doc["markdown"]),4500)
        self.assertTrue(all(r["seq"]<=doc["coverage"]["through_seq"] for m in doc["messages"] for r in m["refs"]))
        self.assertEqual(doc["messages"][-1]["refs"][0]["seq"],terminal)

    def test_repeated_facts_group_only_within_the_same_operation(self):
        self.create_task(state="RUNNING")
        first=self.record("agent.retry",operation_id="route/step/1",level="warn")
        second=self.record("agent.retry",operation_id="route/step/2",level="warn")
        third=self.record("agent.retry",operation_id="route/step/2",level="warn")
        document=self.fetch("/api/session",task_id="route")
        retries=[m for m in document["messages"] if m.get("operation_id")]
        self.assertEqual([m["operation_id"] for m in retries],["route/step/1","route/step/2"])
        self.assertEqual([m["occurrences"] for m in retries],[1,2])
        self.assertEqual(retries[0]["refs"][0]["seq"],first)
        self.assertEqual(retries[1]["refs"][0]["seq"],third)
        self.assertIn("记录 2 次",document["markdown"])

    def test_foreign_module_cannot_replace_owner_terminal_or_plan(self):
        self.create_task()
        owner=self.record("agent.blocked",level="warn",trace={"code":"navigation.path_blocked"})
        foreign=self.history.record({"session_id":"another-console"},kind="agent.blocked",
            source="journal:custom.plugin",level="warn",detail="独立模块另有一个告警",
            trace={"task_id":"route","trace_id":"work","module":"custom.plugin","code":"custom.failure"})
        self.history.record({"session_id":"another-console"},kind="agent.planned",
            source="journal:custom.plugin",detail="插件同名计划",trace={"task_id":"route","trace_id":"work"},
            data={"payload":{"steps":[{"tool":"invented_tool"}]}})
        doc=self.fetch("/api/session",task_id="route")
        self.assertEqual(doc["diagnosis"]["code"],"navigation.path_blocked")
        self.assertEqual(doc["messages"][-1]["refs"][0]["seq"],owner)
        self.assertNotIn("invented_tool",doc["markdown"])
        self.assertTrue(any(foreign in [ref["seq"] for ref in m["refs"]] for m in doc["messages"]))

    def test_distinct_source_warnings_survive_same_kind_code_and_operation(self):
        self.create_task(state="RUNNING")
        def warning(module,detail):
            return self.history.record({"session_id":"run"},kind="device.warning",
                source="journal:"+module,level="warn",detail=detail,operation_id="route/step/1",
                trace={"task_id":"route","module":module,"code":"device.unavailable"})
        warning("gripper","夹爪温度过高")
        gripper=warning("gripper","夹爪温度过高")
        camera=warning("camera","相机遮挡")
        camera_timeout=warning("camera","相机采样超时")
        document=self.fetch("/api/session",task_id="route")
        rows={m["text"]:m for m in document["messages"]}
        self.assertEqual(rows["夹爪温度过高"]["occurrences"],2)
        self.assertEqual(rows["夹爪温度过高"]["refs"][0]["seq"],gripper)
        self.assertEqual(rows["相机遮挡"]["occurrences"],1)
        self.assertEqual(rows["相机遮挡"]["refs"][0]["seq"],camera)
        self.assertEqual(rows["相机采样超时"]["refs"][0]["seq"],camera_timeout)
        self.assertEqual(document["coverage"]["archived_events"],4)


if __name__=="__main__":
    unittest.main()
