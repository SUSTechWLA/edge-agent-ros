# Session 阅读规则也是插件

`session.default` 是 `session.read` 服务的默认提供者。Console 负责公布事实和查找当前服务提供者；本进程负责选择证据、合并重复事实、生成中文会话和 Markdown。替换阅读规则只需部署另一个兼容提供者、发布它的运行时清单。实现契约见 [SDD-010](../docs/specs/SDD-010-session-evidence.md)。

这里沿用 dsh 的服务定义、提供者、消费者与可撤销注册概念。`Session` 在本项目中表示根据一项任务生成的阅读视图，身份为 `task_id`，关联任务使用 `trace_id`。它不是新的事实所有者，也不是模型的私有推理记录；没有实现 dsh SessionStore 或 Cordis SDK 的完整兼容。

## 模块与数据归属

| 文件 | 作用 | 输入与输出 |
|---|---|---|
| `provider.py` | HTTP 服务、身份核对、并发预算和查询审计 | 绑定到 `session.read` 的请求 → 同身份回执或明确错误 |
| `source.py` | 经公开 HTTP 收集有界事实 | task/trace → 任务上下文、归档统计、事件窗口和权威任务锚点 |
| `projection.py` | 可独立替换的默认阅读规则 | 公开事实 → 有来源引用的结构化会话和同源 Markdown |
| `test_provider.py` | 真实 HTTP 契约和叙事回归 | 独立事实服务器与临时审计库 → 可复跑断言 |

默认规则从原 `console/server/sessions.py` 迁出。运行时不导入这个旧模块，不查询 Console 或 Agent 的数据库。唯一的 SQLite 是插件自己的 ModuleJournal。网络读取只使用以下公开 GET 接口：

- `/api/task-context?task_id=...` 或 `trace_id=...`：权威任务、已记录的步骤、关联任务与可用性说明。trace 入口选中同链中最新的任务，文档仍明确实际 `task_id`。
- `/api/event-stats?...`：原始归档的身份、截止序号、记录总数及公开载荷大小。
- `/api/events?...`：取得截止位置之前的事实，使用通用筛选参数并在分页前排除 `evidence.query_`、`session.query_` 审计类型。

审计的原始记录和全量统计仍然保留。原始查询、导出和设备执行流程不依赖本插件；卸载本插件后，由 Console 的服务消费者明确显示当前没有可用的会话提供者。

## 请求与回执

默认入口为 `POST /session`，协议身份与绑定摘要来自运行时服务目录。请求必须只含一个任务或调用链范围：

```json
{
  "schema_version": 1,
  "request_id": "read-session-17",
  "plugin_id": "session.default",
  "plugin_version": "1",
  "binding": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "task_id": "inspection-17"
}
```

示例摘要仅说明字段格式；实际值必须使用目录选中清单的绑定摘要。成功返回原 `schema_version`、`request_id`、`plugin_id`、`plugin_version`、`binding` 和 `document`。`document` 保留现有字段：`scope`、`task`、`headline`、`messages`、`diagnosis`、`related_tasks`、`related_tasks_has_more`、`coverage`、`links`、`markdown`。Console 在验证回执后追加 `service` 来源元数据，不需要默认提供者自行推测部署清单。

请求编号用于关联一次读取，不是机器人操作的幂等编号。本服务不缓存旧文档；后续读取可以反映新的权威状态。`GET /health` 只报告进程的提供者身份和服务名，不代表上游事实源已经可读。

| HTTP | 含义 |
|---|---|
| 200 | 已生成可核对来源的文档；不代表机器人任务成功 |
| 400 | 输入、范围、协议或提供者身份不符合契约 |
| 404 | `code=session.not_found`，任务和相应归档都没有记录 |
| 503 | 上游不可用、归档换代、预算耗尽或审计无法持久保存 |

正常协议错误回显相同元数据，提供稳定的根 `code` 与 `error.code/message`，不附带半成品文档。完整 JSON 尚未解析成功时只能返回输入错误，不能宣称已校验请求身份。

## 为什么大量刷新不会挤掉重要事实

读取先固定归档身份和截止序号，再倒序加载最多 480 条非审计事件。每页最多 100 条、响应最多 2 MiB。随后在同一截止范围和归档身份下，单独补取任务所有者 `mission_agent/tasks` 的最早接收、最新计划及当前状态对应的最后终态，总计不超过 500 条。

这解决两种不同的噪声：一千次阅读审计不会占用事实窗口；大量晚到插件告警也不会抹掉权威任务的成功或失败回执。无法无限保留任意久远的全部事实，因此 `coverage.loaded_events`、`loaded_window_events`、`window_has_more` 和 `truncated` 明确说明本次读了多少、还有哪些范围没有逐条读取。`archived_events` 与 `public_payload_bytes` 仍来自全范围统计。

`audit_events` 只表示加载窗口里观察到的审计数，`audit_count_scope=loaded_window_only` 明确它不是全量审计计数。正常公共筛选会使它为零；不要据此声称系统没有执行其他查询。归档清理位置通过 `pruned_through_seq` 保留。任务快照与归档窗口来自独立 HTTP 请求，文档明确说明它们不是同一事务快照。

## 默认叙事规则的边界

- 用户目标、当前终态及已记录步骤由任务所有者提供。只有 `source=mission_agent/tasks` 的接收、计划、终态事件可充当对应权威引用。插件发布同名 `agent.blocked` 或 `agent.planned` 仍只是该插件的独立事实。
- 相同来源、模块、源库身份、类型、原因、操作和内容的重复事件才可合并。夹爪过热、相机遮挡和相机超时分别保留，跨操作的重试也不合成同一事实。
- 未知插件的 warn/error 依然可进入摘要；周期采样与普通轮询不会变成“新的执行进展”。会话与证据查询的审计自事件被折叠，避免自我放大。
- 任务后来成功，之前的错误标注为历史问题。独立模块告警不能自动覆盖任务所有者的结果；因果关系未得到证明时明确要求继续核对。
- 每个叙事事实带归档引用，文本不生成未记录的模型内心推理。缺少权威任务但仍有公开事实时展示状态未知；两者都缺失时区分真正未找到与来源不可读。

摘要预算主要通过限制消息数量、文本长度和相关任务数量执行。插件整个响应上限为 128 KiB；工作预算 6 秒，同时最多两个读取。网络沿用公共 HTTP 读取辅助的单次超时和绝对截止处理。它不是硬实时控制进程。

## 审计与独立生命周期

每次读取先持久保存 `session.query_requested`，随后保存 `session.query_completed` 或 `session.query_failed`，通过父事件引用连接。审计包含请求编号、提供者身份、绑定、请求摘要、范围及覆盖信息；不重复保存整包会话或模型提示。审计写入失败时不交付文档。

默认配置：

| 环境变量 | 默认值 |
|---|---|
| `EDGE_SESSION_SOURCE` | `http://console:8080` |
| `EDGE_SESSION_BIND` / `EDGE_SESSION_PORT` | `0.0.0.0` / `8126` |
| `EDGE_SESSION_JOURNAL` | `/var/lib/edge-session/session.default/module-events.sqlite` |
| `EDGE_SESSION_PLUGIN_ID` / `EDGE_SESSION_PLUGIN_VERSION` | `session.default` / `1` |

入口为 `python3 /opt/edge/sessions_plugin/provider.py`。此服务复用仓库中的公共 HTTP 辅助、事件信封与 ModuleJournal；自建镜像也需包含这些共享模块。仅给它自己的审计目录写权限，不挂载任务、模型原文或 Console 历史数据库。运行时清单的安装、撤回、绑定选择与验证由消费者及部署管理入口处理。

## 验证

```sh
python3 -B -m unittest discover -s sessions_plugin -p test_provider.py -v
```

9 项真实 HTTP 契约覆盖：超过 1,000 条审计后的制动与未知故障可读；超过窗口的告警下补取权威计划和终态；来源、原因和操作的分组边界；同名外来事件不替代任务所有者；取消、缺失、未知及 trace 关联任务；错归档、协议错误、超时和历史清理说明；上游畸形响应及审计写失败；独立提供者替换、关闭与重启后的持久审计；重复查询缺失任务时，查询审计不会被误认为任务存在的证据。字段与叙事断言沿用原会话测试的关键行为，Console 新测试另行验证公共源接口和插件消费者。
