# 模块事件接入与回溯

当驱动、感知插件或巡检器发现一次值得追踪的变化，可向 ROS 2 话题 `/edge/events` 发布一条 `std_msgs/msg/String`。字符串内容采用下表中的 v1 JSON。新模块可以独立增加事件种类及诊断码并热接入。建议在模块的 SDD 里写出每个码的产生条件、证据字段、处理方和复测方法。

| 字段 | 约定 |
|---|---|
| `schema_version` | 必填整数 1。 |
| `event_id` | 必填，全局唯一。推荐 UUID。重发使用相同 ID 和内容。 |
| `trace_id` | 必填，同一链路的根标识。链路可以始于一次硬件告警，也可以始于自然语言任务。 |
| `module`、`kind`、`code` | 必填。分别为生产模块、变化种类、机器可读的诊断码。可按领域分配，如 `power.monitor`、`power.threshold_crossed` 和 `power.battery_low`。 |
| `parent_event_id` | 有明确前因时填写其 ID。先收到子事件会先保存该引用，收到父事件后补上可点击的本地 `cause_seq`。 |
| `task_id`、`mission_id`、`operation_id`、`call_id`、`layer` | 可选，分别关联自然语言任务、ROS 委托、执行操作、一次具体调用和所在层。 |
| `trigger` | 可选对象，含 `kind`、`id` 和 `source`。同一次触发沿用同一标识。类型可从 user、watchdog、agent 扩展到新的来源。 |
| `utc` | 可选，带时区的 ISO 8601 发生时间。缺省时按接收时间记录。提供源时间时，回放会寻找源时刻的历史观测，观测缺失时保留空值。 |
| `level`、`detail`、`data` | 等级可为 info、warn、error。简短说明面向读者，结构化对象保存能定位问题的证据和量纲。整条消息上限 64 KiB，data 嵌套上限 16 层。 |

## 让低电量告警进入观测台

以下为仿真中的例子。在一台与观测台处于相同 ROS 域的发布者上运行。该操作只写一条观测记录。网关继续按现有任务授权执行。

```sh
ros2 topic pub --once /edge/events std_msgs/msg/String "{data: '{\"schema_version\":1,\"event_id\":\"lab-alert-42\",\"trace_id\":\"lab-power-42\",\"module\":\"power.monitor\",\"kind\":\"power.threshold_crossed\",\"code\":\"power.battery_low\",\"level\":\"warn\",\"trigger\":{\"kind\":\"watchdog\",\"id\":\"scan-42\",\"source\":\"power.monitor\"},\"data\":{\"voltage_v\":11.7,\"threshold_v\":12.0}}'}"
```

在观测台「事件记录」中展开「按调用链、模块或触发来源筛选」，输入本例的 `lab-power-42` 或 `power.battery_low` 并查询。在详情里可以读到产生该事件的模块、实际采集来源、触发类型、前因和当时的传感器状态。需要离线分析时，可下载 `/api/export?trace_id=lab-power-42&snapshot=1`。同一事件的重投只保留一份，相同 ID 的冲突会保留首次内容并记录 `module.event_conflict`。

触发器如要启动工作，须另行向任务接口提交带 trigger 的任务。归档端观察事件，能区分模块自述的 `module` 和实际传输 `source=ros:/edge/events`。此 topic 使用 RELIABLE、VOLATILE、KEEP_LAST 100。需要完整记录离线期间事件的模块应把关键事实持久化，连接恢复后重发原 event_id。Agent 和巡检内核通过各自持久账本补录。

公开接口部署在受信 ROS 域。生产者只发送需要共享的测量和原因。接收器会过滤常见认证字段、令牌赋值和 HTTP 认证串，自由文本和设备特有机密仍须生产者在发送前处理。记录能证明发布者报告了哪些内容，实际任务完成还需执行器回执和对应结果证据。

## 可随时接入的日志消费者

在默认仿真 Compose 运行期间，另开终端执行以下命令。示例消费者单独运行、只挂载自己的目标卷，通过只读的事件查询接口复制告警。它可以由日志平台或离线分析 Agent 替换。

```sh
docker run --rm --network edge-agent-ros_default -v edge-example-sink:/sink edge-agent-ros:v0.1 python3 /opt/edge/client/event_sink.py --url http://console:8080 --db /sink/events.db --module power.monitor
```

停止消费者后重新执行同一命令即可续传。首次测试可增加 `--once`，追到当前末尾后退出。源归档 ID 和本地序号共同标识事件，记录与读到的位置写入同一个 SQLite 事务。同一过滤条件恢复同一个游标，改变过滤条件会从可用历史重新扫描，已复制事件按标识去重。源归档更换后自动识别新归档。用户主动询问、巡检器等可各自使用独立目标卷。将副本导出为 NDJSON 的命令如下。

```sh
docker run --rm --network none -v edge-example-sink:/sink:ro edge-agent-ros:v0.1 python3 /opt/edge/client/event_sink.py --db /sink/events.db --export
```

实现其他消费者时，调用 `GET /api/events?after=<cursor>&limit=32`。依序处理 `events`；分页还没结束时保存最后处理的序号，已追到尾页时可保存响应的 `cursor`。此例选择按 module、code、kind、level、trigger_kind 过滤，这些字段在写入时即确定。归档返回 `pruned_through_seq`，表示这一版本开始记录的、已被滚动删除的普通遥测样本的最高序号。读取位置落后于此值时，目标库在 `retention_notices` 表保存一个可能缺样的区间。该值无法重建升级前已经删除的样本。先收到子事件的消费者可按 `parent_event_id` 对齐稍后到达的父事件。[消费者实现](../client/event_sink.py)
