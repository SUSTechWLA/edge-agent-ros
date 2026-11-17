# 按需读取任务证据

本插件让当前 Agent 和外部模型使用相同的工具：先阅读短会话，再写一条 SELECT 查需要的公开事实。它只通过 Console 的公共 HTTP 接口取证，独立保存查询审计；没有 ROS 控制入口，也不挂载任务所有者的数据库。实现与验收契约见 [SDD-010](../docs/specs/SDD-010-session-evidence.md)。

## 从短会话到具体测量

1. 调用 `read_session`，提供 `task_id`，得到与观测台同源的 Markdown、归档引用和覆盖说明。例如先知道“巡检因障碍受阻，机器人已经制动”。
2. 调用 `evidence_schema`，无参数。返回 `events` 表公开字段、可用 SQL、输入和输出预算及例子。也可直接 `GET /schema` 发现契约。
3. 调用 `query_evidence`，按 `kind`、`code` 或父事件进一步查证。输入参数都是当前 Agent 支持的 string/number：

```json
{
  "operation_id": "diagnose-17/query-1",
  "version": "1",
  "arguments": {
    "task_id": "inspection-17",
    "sql": "SELECT seq,kind,code,detail,data_json FROM events WHERE level IN ('warn','error') ORDER BY seq",
    "after_seq": 0,
    "limit": 10,
    "archive_id": ""
  }
}
```

请求发送到 `POST /invoke?tool=query_evidence`。其他工具分别使用 `tool=read_session` 和 `tool=evidence_schema`，兼容相同请求体的 `/query`、`/session`、`/schema` POST 别名。`read_session.arguments` 只有 `task_id`，`evidence_schema.arguments` 为 `{}`。

成功回执保持既有工具契约：`operation_id`、`state=SUCCEEDED`、`evidence`。查询证据包含 `query_id`、`columns`、`rows`、`markdown`、`refs`、`coverage` 和 `truncated`。**这里的成功表示取证成功；目标机器人的任务仍可能失败。** 原始数值不会因显示预算被截成另一数值；超预算会减少整行并明确标记。

`data_json` 来自该事件公开的 `data`。字段随生产者变化，先查看相应事件，再用 `json_extract(data_json,'$.实际路径')` 取需要的部分。可以使用 `:task_id`、`:after_seq`、`:limit` 绑定参数。例如：

```sql
SELECT seq,kind,code,json_extract(data_json,'$.nearest_m') AS nearest_m
FROM events WHERE code='navigation.obstacle' ORDER BY seq
```

这个路径只在生产者确实发布 `nearest_m` 时有值；NULL 不代表距离为零。证据引用是归档定位入口，SQL 可以计算派生值，核实原值仍应打开对应事件。模型不能将相邻事件推断成因果；使用 `parent_event_id` 和 `event_id` 关联，父事件不在窗口里时继续查早期窗口。

首次探索建议执行 `SELECT kind,count(*) FROM events GROUP BY kind`，再按真实事件名查看一条 `data_json`。SQLite 的 `->` / `->>` JSON 操作符也可用。表已经限定了任务范围；底层委托可能只有 `mission_id` 而没有 `task_id`，再次添加 `WHERE task_id=...` 会把这些真实执行证据排除。

## 先理解范围，再解释结果

- 每次仅取得指定任务及 Console 已明确关联的委托事件；不接受数据库路径、任意 URL 或跨任务裸序号访问。
- 每次最多加载 500 个事件，每页最多 100 个、响应最多 2 MiB。首响应固定归档身份和截止序号，后续页不会混入新写入的记录。已公开的大模型记录保留在有界内存中，查看邻近的错误或 `COUNT(*)` 不会因为未选中的大字段被阻断。
- `coverage.has_more=true` 时，用 `next_after` 和同一个 `archive_id` 继续。首次 `after_seq=0,archive_id=""`；续页缺身份或归档换代会拒绝。`COUNT(*)` 只计算当前窗口，不能作为整个任务总量。
- `pruned_through_seq` 和 `retention_gap_possible` 说明已知的历史清理边界。固定截止游标不等于跨页数据库快照；源在查询期间清理历史也可能造成缺口。覆盖信息明确保留这种限制。
- `truncated=true` 表示结果行数、文本或短会话被缩减。源分页和结果分页是两件事：若只返回了窗口内前几行，应在相同窗口缩小 SELECT 或追加 WHERE；不要直接跳到 `next_after` 丢掉尚未查看的结果。
- 未找到事件意味着当前公开范围无记录，不能证明机器人没有执行。归档断连、协议错误和未知任务会分别返回诊断。

## 查询执行与审计

SQL 只对新建的内存 `events` 表执行。SQLite 授权器拒绝写入、外部数据库、内部元数据、PRAGMA、文件函数和扩展；仅开放常用读取、聚合和 JSON 函数。单次最多 50 行，整个工具回执最多 12 KiB；SQL 最多 16 KiB、20 万 VM 指令、4 秒工作预算，最多两个并发查询。网络还有独立读超时。查询进程可以独立重启，不占用机器人控制线程。

每次实际尝试先把 `evidence.query_requested` 写入自己的 ModuleJournal，完成或失败再写 `evidence.query_completed` / `evidence.query_failed`，通过父事件相连。记录工具名、目标任务、请求摘要、查询 ID、调用 trace、覆盖和结果数量；不记录 SQL 原文或整包模型提示。审计不可保存时不交付查询结果。来自调用者的 `trace.task_id` 与被查询的 `data.target_task_id` 分开，诊断另一个任务时不混淆两项工作。

同一 `operation_id` 和同样输入重试，会在提供者重启后返回已保存的同一回执。不同查询必须使用新 ID；修改 SQL 却沿用旧 ID 会收到 `evidence.operation_conflict`。上游暂不可用返回 HTTP 503，并允许按原 ID 再次尝试；SQL 拒绝等确定性错误返回 HTTP 200、`state=BLOCKED` 和稳定错误码。错误消息不会回显完整 SQL 或宿主路径。

主要诊断码包括 `evidence.sql_denied`、`evidence.invalid_sql`、`evidence.query_budget`、`evidence.result_budget`、`evidence.source_budget`、`evidence.archive_changed`、`evidence.source_unavailable`、`evidence.source_invalid`、`evidence.audit_unavailable`。不同查询之间互不共享内存表。查询审计及回执仍需要部署者管理磁盘容量；本版没有自动删除审计的策略。

## 部署与验证

服务入口为 `python3 /opt/edge/evidence/provider.py`。环境变量：

| 配置 | 默认值 |
|---|---|
| `EDGE_EVIDENCE_SOURCE` | `http://console:8080` |
| `EDGE_EVIDENCE_JOURNAL` | `/var/lib/edge-evidence/module-events.sqlite` |
| `EDGE_EVIDENCE_BIND` / `EDGE_EVIDENCE_PORT` | `0.0.0.0` / `8125` |

部署网络和认证边界由运维配置；本服务面向受控的本机/容器网络，没有独立多租户身份系统。工具目录安装和撤回由[清单模板](tools/)及项目部署命令管理，审计接入沿用[模块 EventBus](../docs/module-eventbus.md)。

```sh
python3 -B -m unittest discover -s evidence -p test_provider.py -v
```

测试使用真实 HTTP 服务和独立临时审计库，覆盖范围隔离、SQL 写入和外部读取拒绝、递归预算、分页身份、巨字段不阻断小诊断、输出截断、缺证据、传输断连、并发、审计失败和重启后的回执复用。Docker/Gazebo 集成验收由项目系统测试继续核对自然语言入口、热挂载和实际事件链。
