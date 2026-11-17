# 从外部触发回到模型输入与执行证据

任务账本将“谁触发了这次工作”“模型当时看到了哪些能力”“执行端实际怎么回答”关联起来。客户端从任务响应取得 `id`、`trace_id` 和 `trigger`。按任务 ID 查归档或导出 NDJSON，可沿 `parent_event_id` 追溯前因。

## 用户、Watchdog 和其他 Agent 用同一任务入口

```json
{
  "text": "报告机器人当前位置",
  "trigger": {"kind":"watchdog","source":"robot-17-localizer","id":"lost-fix-37"}
}
```

向 `POST /agent/tasks` 发送上面的对象。触发三元组 `(kind, source, id)` 在数据库中唯一。客户端超时后重发相同来源、文本及因果元数据，会得到原任务。相同来源试图提交不同内容返回 HTTP 409。普通浏览器请求可只传任务 ID 和文本，系统会生成 `user` 触发。来源是调用者声明的标签，跨网络接入时应由认证网关校验。

子 Agent 可设置 `trigger.parent_task_id` 和/或 `trigger.parent_event_id`。若父记录位于本地账本，子任务继承原 `trace_id`；跨账本接力可传 `trigger.trace_id`。事件 ID 用于因果引用，来源 ID 用于重投去重，两者可以不同。旧版任务没有历史触发元数据，接口返回 `trigger:null`。

## 事件和状态怎么对应

| 日志中的 kind | 能回答的问题 |
|---|---|
| `accepted`, `planned`, `dispatch`, `verified` | 何时接受目标、冻结哪个计划、派发了哪个操作、依赖什么证据完成。任务状态和对应状态事件在同一 SQLite 事务提交。 |
| `planner.context` | 本次规划尝试的完整可见能力目录及绑定摘要，包括本地句式匹配的任务。 |
| `model.request / response / error` | Ollama 请求中实际使用的 system 和 user messages、模型、选项、工具目录、约束 schema 及原始响应。自定义 HTTP 规划器保存其自身协议的输入输出；远端模型的内部提示只有提供者能提供。 |
| `tool.request / response / error` | 语义调用使用的能力、版本、参数、操作 ID 及验收结果。在途步骤可多次查询，沿用稳定的操作 ID，每次查询各有调用 ID。 |
| `adapter.request / response / error` | 独立插件收到的 HTTP 负载与回执。可用来区分“提供者报错”和“它声称成功，但本地验收失败”。 |
| `ros.request / response / error` | 内置巡检实际查询和提交的 ROS 服务委托及核心回执。更低层动作和停稳验收另见巡检内核的账本。 |
| `retry`, `retry_exhausted`, `blocked`, `error`, `retry_requested` | 谁发现了错误、是否自动重试、哪些情况需要操作员核对。诊断文案不改变现有重试或取消规则。 |

以上数据位于 Agent 账本的 `events.payload.trace`。通用字段为 `schema_version, event_id, parent_event_id, trace_id, trigger, module, layer, code`。每个真实交互的来回记录共用 `call_id`，持续时间使用本机单调时钟。进程在外部调用过程中崩溃时，可能只留下一条已提交的 request。应先用操作 ID 对账。历史事实与适配器实际运动的证明需要分别查看。

可以通过 `GET /agent/tasks?id=<ID>&after=0&limit=100` 正序读 Agent 事件。下一页使用上一页最后一个 `seq`。默认返回最近 128 条且限制单页日志体积，`events_has_more=true` 表示有更多记录。

## 运行中切换模型和提示

将 `EDGE_CONTRACT_DIR` 指向 Agent 可读的配置目录，默认 `/etc/edge/contracts`。在宿主写好临时文件后原子改名，使正在运行的进程在下一次模型规划开始时取得一致的配置。已接受的计划继续使用持久步骤。

`planner-profile.json` 可采用以下两类结构之一。

```json
{"schema_version":1,"version":"lab-2","protocol":"ollama","url":"http://model:11434","model":"qwen2.5:1.5b","options":{"temperature":0,"num_ctx":4096}}
```

```json
{"schema_version":1,"version":"external-1","protocol":"http-planner","url":"http://planner:8092/plan"}
```

无该文件时使用原环境变量 `EDGE_PLANNER_URL` 或 `EDGE_OLLAMA_URL/MODEL`。无效的显式 profile 令当次规划进入 `BLOCKED`，代码为 `planner.profile_invalid`。每次调用记录协议、模型及 profile 的版本与内容摘要。URL 的认证信息、查询串和未知配置字段会被拒绝。实际可用模型及加载状态由提供者管理。

默认提示和诊断在 [contracts.json](contracts.json)。本地可添加其他命名的 JSON 覆盖，例如：

```json
{
  "schema_version":1,
  "prompts":{"planner.system":{"version":"field-2","text":"根据已提供的能力生成完整计划。无法满足任务时返回空 steps 和原因。"}},
  "diagnostics":{"navigation.path_blocked":{"version":"2","hint":"查看同一操作 ID 的地图版本和控制器最后一次可行路径。"}}
}
```

建议每个键只由一个文件提供。匹配键按文件名排序读取，最后一个有效覆盖生效。删除覆盖后回到内置版本。提示的版本、SHA-256 和当次实际请求一起记录；后续修改不会改写历史。诊断目录仅改变说明与排查提示，执行是否可以重试仍由动作状态和原有资源规则决定。

插件失败可返回 `{"operation_id":"task/step/1","state":"FAILED","error":{"code":"navigation.path_blocked","message":"前方道路占用","details":{"map_version":"yard-v3"}}}`。既有 `detail` 文本仍可使用。ROS 原有 detail 若以 `[code:navigation.odom_timeout]` 等稳定代码结尾，记录器会提取该代码并保留原消息。每次诊断快照包含代码、origin、触发错误的模块、当时目录版本及排查提示。

## 内容与隐私

`EDGE_TRACE_CONTENT=redacted` 默认对新交互留存受限预览，过滤常见 token、Authorization、密码等字段与文本模式。每条交互记录原 JSON 的规范化 SHA-256、UTF-8 字节数和内容模式；摘要用于版本比对。编码固定为按键排序、紧凑分隔符的 UTF-8 JSON。脱敏后超过 16 KiB 的内容标记 `truncated`，保留前缀。归档不能从截断或脱敏内容重建逐字原文。模型用户文本和证据可能仍包含人名、地点等信息。

`EDGE_TRACE_CONTENT=metadata` 可对新交互只保存摘要、大小和版本标识。任务工作库仍需保存原始目标和执行中的步骤，以支持重投去重和恢复。已存在的数据不随配置变化而被改写。分发归档和开发验证时应使用适当的访问范围。

## 为新技能提供不依赖模型的固定句式

主清单目录优先；设定 `EDGE_EXTRA_PLUGIN_DIR` 时还读取第二目录，未配置时只读取主目录。对于只有一个字符串参数的可信能力，清单可写 `"parameters":{"place":"string"},"utterances":["去{place}巡检","前往{place}"]`。固定前后缀按字面完整匹配，捕获非空地点。多义匹配会拒绝派发。地点是否合法、可达和已经完成由对应场景提供者验收。

回归命令为 `docker run --rm --network none edge-agent-ros:v0.1 python3 /opt/edge/tests/agent_trace_contract.py -v`。[测试实现](../tests/agent_trace_contract.py)

## 如何核对一次工具调用是否真的成功

内置 `inspect` 在首次绑定或接续旧版任务时向 ROS 内核提交原 `mission_id` 与原有序点列。内核核对绑定，重复的相同委托沿用原结果，点位不同返回 `core.id_conflict`。已发送但在内核账本内消失的委托进入 `RECOVERY_REQUIRED`。每个请求点均需有距离目标不超过 0.12 m 的有限测量，Agent 才能写入完成。要求前往明确点位并读取仪器时，规划校验会拒绝漏掉点位的测量方案。模型服务启动中或工具连接中断时，账本保留具体失败，幂等工具用原 `operation_id` 重试。回执身份错误或成功证据非法时进入 `RECOVERY_REQUIRED`，可先向提供者查询原操作。

旧版曾允许提供者用 NaN 充当读数。升级后首次打开旧账本时，将含此类步骤的任务转为 `RECOVERY_REQUIRED`，保留原事件，在步骤内标记 `invalid_number`，并增加 `evidence_corrected` 记录原状态及变更前步骤内容的 SHA-256。复核时对照原提供者数据。本地测试覆盖提供者丢回执、超时、多步骤中途重启和两种任务标识冲突。[仿真故障验收](../tests/system/agent_faults.py)
