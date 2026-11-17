"""Bounded public HTTP facts for a replaceable Session reading provider."""
from evidence.public_source import EvidenceError, PublicSource
from .projection import FINAL_KINDS, select_events


AUDIT_PREFIXES = "evidence.query_,session.query_"
WINDOW_LIMIT = 480


class SessionSource(PublicSource):
    def invalid(self, message):
        raise EvidenceError("session.source_invalid",message,503)

    def context(self, scope, deadline):
        context = self.get("/api/task-context",scope,deadline)
        target,task = context.get("scope"),context.get("task")
        if (context.get("schema_version")!=1 or not isinstance(target,dict)
                or task is not None and not isinstance(task,dict)
                or not isinstance(context.get("related_tasks"),list)
                or type(context.get("related_tasks_has_more")) is not bool
                or type(context.get("task_available")) is not bool
                or not isinstance(context.get("notes"),list)
                or not all(isinstance(note,str) for note in context["notes"])):
            self.invalid("任务上下文结构不符合公开契约。")
        name,value = next(iter(scope.items()))
        if target.get(name)!=value:
            self.invalid("任务上下文不属于请求范围。")
        if task:
            if (task.get("id")!=target.get("task_id") or not isinstance(task.get("state"),str)
                    or not isinstance(task.get("steps"),list)
                    or not all(isinstance(task.get(key),str) for key in ("id","text","detail"))):
                self.invalid("权威任务字段不完整或身份不一致。")
        for item in context["related_tasks"]:
            if not isinstance(item,dict) or not all(isinstance(item.get(key),str) for key in ("id","text","state")):
                self.invalid("关联任务条目无效。")
        if len(context["related_tasks"])>8:
            self.invalid("关联任务超出公开接口数量预算。")
        return context

    def collect(self, context, deadline):
        scope = context["scope"]
        filters = {"task_id":scope["task_id"]} if scope.get("task_id") else {"trace_id":scope["trace_id"]}
        stats = self.get("/api/event-stats",filters,deadline)
        for field in ("cursor","count","pruned_through_seq","public_payload_bytes"):
            if type(stats.get(field)) is not int or stats[field]<0:
                self.invalid("归档统计缺少有效数量或游标。")
        identity,cutoff = stats.get("archive_id"),stats["cursor"]
        if not isinstance(identity,str) or not identity:
            self.invalid("归档缺少身份。")
        for field in ("first_seq","last_seq"):
            if stats.get(field) is not None and (type(stats[field]) is not int or not 0<stats[field]<=cutoff):
                self.invalid("归档首尾位置无效。")
        rows = {}
        pruned = stats["pruned_through_seq"]

        def page(extra, limit):
            nonlocal pruned
            query = {**filters,"before":cutoff+1,"limit":limit,**extra}
            reply = self.get("/api/events",query,deadline)
            if reply.get("archive_id")!=identity:
                raise EvidenceError("session.archive_changed","读取期间归档身份发生变化，请重新读取会话。",503)
            events = reply.get("events")
            if (not isinstance(events,list) or len(events)>limit or type(reply.get("has_more")) is not bool
                    or type(reply.get("cursor")) is not int or reply["cursor"]<cutoff):
                self.invalid("归档页结构或截止游标无效。")
            page_pruned = reply.get("pruned_through_seq",0)
            if type(page_pruned) is not int or page_pruned<0:
                self.invalid("归档清理边界无效。")
            pruned = max(pruned,page_pruned)
            previous = query.get("after",query["before"])
            for row in events:
                if (not isinstance(row,dict) or type(row.get("seq")) is not int or not 0<row["seq"]<=cutoff
                        or not isinstance(row.get("data"),dict)
                        or not all(isinstance(row.get(key),str) for key in ("utc","kind","source","detail","level"))
                        or row["level"] not in ("info","warn","error")
                        or any(row.get(key) is not None and not isinstance(row[key],str)
                               for key in ("event_id","source_id","module","code","operation_id"))):
                    self.invalid("归档事件字段无效。")
                if not (previous<row["seq"]<query["before"] if "after" in query else row["seq"]<previous):
                    self.invalid("归档分页顺序无效。")
                previous = row["seq"]
                rows[row["seq"]] = row
            if reply["has_more"] and not events:
                self.invalid("归档声明存在后续记录，但没有继续位置。")
            return events,reply["has_more"]

        before,window_count,window_more = cutoff+1,0,False
        while window_count<WINDOW_LIMIT:
            events,window_more = page({"before":before,"exclude_kind_prefixes":AUDIT_PREFIXES},min(100,WINDOW_LIMIT-window_count))
            window_count += len(events)
            if not window_more:
                break
            before = events[-1]["seq"]
        state = (context.get("task") or {}).get("state")
        # Pin the owner result even if later plugin warnings filled the window.
        # Every request remains a generic public fact filter, never a DB read.
        anchors = [("agent.accepted",True),("agent.planned",False)]
        anchors.extend((kind,False) for kind in FINAL_KINDS.get(state,("agent.transition",)))
        for kind, oldest in anchors:
            extra = {"source":"mission_agent/tasks","kind":kind}
            if oldest:
                extra["after"] = 0
            page(extra,1)
        selected,collapsed,audit_count = select_events(list(rows.values()),state)
        notes = ["会话由公开任务快照及固定归档截止位置投影；两个来源不是同一事务快照。",
                 "审计自事件不进入阅读窗口，原始归档及统计仍保留这些记录。"]
        if window_more:
            notes.append("仅加载最近的有界非审计窗口，并补取权威任务的接收、计划和终态；窗口外其他事实未逐条读取。")
        if pruned:
            notes.append("归档已经执行历史清理，不能将本次读取视为原始全量记录。")
        coverage = {"archive_id":identity,"through_seq":cutoff,"first_seq":stats.get("first_seq"),
                    "last_seq":stats.get("last_seq"),"archived_events":stats["count"],
                    "public_payload_bytes":stats["public_payload_bytes"],"pruned_through_seq":pruned,
                    "loaded_events":len(rows),"loaded_window_events":window_count,"window_has_more":window_more,
                    "audit_events":audit_count,"audit_count_scope":"loaded_window_only",
                    "selected_events":0,"collapsed_events":stats["count"],
                    "truncated":window_more or collapsed,"notes":notes}
        return selected,coverage
