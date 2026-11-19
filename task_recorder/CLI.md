# 在普通电脑上按任务录制

录制器记录“发生了什么”。任务仍由原来的 Agent 接受、执行和验收。安装 Python 3 的开发机可以直接控制记录器。选择哪个任务、何时开始、以何种终态结束，都能按录制号查到。

## 录一条软件任务

先启动可选的录制服务。下列操作只需标准库，工作目录为仓库根目录。示例中的 `ticket-07` 是将来要提交给 Agent 的任务 ID，`take-07` 是这次录制 ID，两者可以不同。

```sh
python -m task_recorder.cli start take-07 --task-id ticket-07 --sources agent_state agent_trace
python -m task_recorder.cli watch take-07 --agent http://127.0.0.1:8765 --wait-for-task --finish --timeout 300
```

现在通过原来的 Agent 接口或工作台提交同一 ID 的任务。也可以先提交，再运行上述命令。即使任务一瞬间执行完，或者第一次查询时任务已经结束，只要 Agent 的该任务账本尚在，就能取回已有工具调用。比如第一次查询只看到“成功”，时间线仍可以显示此前的 `tool.request → tool.response → verified`，对同一操作用 `operation_id` 关联。

| 来源 | 何时产生记录 | 可以从中回答的问题 |
|---|---|---|
| `agent_state` | 轮询时观察到状态、步骤数或步骤索引变化。任务尚未提交和服务暂时不可用也有记录。时钟域为观察器的 `observer_unix_ns` | 我什么时候看见任务开始、等待或终止？多次变化落入同一轮询间隔时，可能只观察到最后一次。 |
| `agent_trace` | 从源账本 `after=0` 开始按源序号升序翻页。每次最多读取 32 条，终态且末页读完才结束录制。重读时凭源序号去重 | 实际调用了哪个工具，属于哪一步，有没有收到结果或验证回执？`phase` 是原事件种类，`source_seq` 为原账本序号，`seq` 为归档序号。任务内的源序号可以有间隔。 |
| `agent_request` | 单独声明该来源后，保存用户触发任务的请求文本、创建时间，并复用项目的已知凭证模式脱敏 | 当时用户要求做什么？文本可能包含现场名称、个人信息等，只有确实需要归档原文时才开启。 |

工具调用记录包含任务标识、步骤索引、源 UTC、源序号与操作标识。源提供时，另记工具、提供方、模块、诊断代码、错误类型、耗时、结果状态及关联调用 ID。原参数、工具结果正文、错误正文和模型提示内容留在源系统。混看软件与 ROS 仿真数据时根据接收时间对齐。Agent 的 `agent_utc` 与仿真时钟分属不同的时钟域。

Docker 单命令入口默认申请 `agent_state` 与 `agent_trace`，可用 `EDGE_RECORD_AGENT_TRACE=0` 仅查看状态。要主动保存用户请求，可在开录前设置 `EDGE_RECORD_REQUEST_TEXT=1`，或在上面的底层 CLI 命令中附加 `agent_request`。若 Agent 仅返回状态，现场确实未收到的 `agent_trace` 会列入 `missing_sources`。先开录再执行任务还可以保留实时观察、地图和图像。

`--finish` 在观测到终态后以该终态结束录制并导出。省略它时，watch 命令在终态返回，可继续保留相机等其他来源一段时间，之后手工结束。超时或主动中断观察命令也会保留已记录内容。开第二个终端查询运行状况：

```sh
python -m task_recorder.cli status take-07
python -m task_recorder.cli stop take-07
python -m task_recorder.cli export take-07
```

`status` 返回实际收到的源、样本数、最近一次接收时间及缺失来源。`stop` 结束的是录制。手工结束时记录状态为 `STOPPED`；如果已按任务终态结束，会保持先前的结果。`export` 把关闭的归档展开到录制服务挂载的存储中，返回摘要。数据位于服务端的 `<recording_id>/media/mission/timeline.jsonl` 和 `<recording_id>/recording.json`。将存储映射到主机目录即可直接拷贝或复盘这些文件。

默认录制器地址为 `http://127.0.0.1:8188`，可在子命令前指定 `--recorder`，或使用环境变量 `EDGE_RECORDER_URL`。Agent 地址单独使用 `--agent`。在已发布的 Console 代理与 Agent 具有相同任务 API 时，两者均可作为观察入口。

## 验证场景

已完成任务的分页测试校验 38 条工具事件，两次启动观察后核验源序号、去重结果、跨任务污染拦截、受限字段与用户原文开关。单独 Docker 项目另验真实 ROS 输入与“第一条成功回执随附两页工具历史”的场景。[HTTP 测试](../tests/test_recorder_cli.py) · [Docker 运维实测](../artifacts/verification/recorder-ops/run04/results.json)

四组本机 HTTP 试验启动真实录制服务，由独立软件任务响应端合成“尚未提交、临时 503、执行中、相同执行中、成功”的状态序列，实际归档四条记录，并按源身份核验请求仅有 GET。另覆盖手工停止、重复导出、身份错配，以及运行中手工结束录制时观察进程自动退出。人工结束和任务完成同时发生时，两端取回同一份最先写入的结束回执。测试输入含原文、参数及凭证占位值，逐行核验导出结果中均无这些字段。[复跑测试](../tests/test_recorder_cli.py)
