# 在普通电脑上按任务录制

录制器记录“发生了什么”。任务仍由原来的 Agent 接受、执行和验收。安装 Python 3 的开发机可以直接控制记录器。选择哪个任务、何时开始、以何种终态结束，都能按录制号查到。

## 录一条软件任务

先启动可选的录制服务。下列操作只需标准库，工作目录为仓库根目录。示例中的 `ticket-07` 是将来要提交给 Agent 的任务 ID，`take-07` 是这次录制 ID，两者可以不同。

```sh
python -m task_recorder.cli start take-07 --task-id ticket-07 --sources agent_state
python -m task_recorder.cli watch take-07 --agent http://127.0.0.1:8765 --wait-for-task --finish --timeout 300
```

现在通过原来的 Agent 接口或工作台提交同一 ID 的任务。也可以先提交，再运行上述命令。CLI 从 `GET /agent/tasks?id=…` 读取最新进度。未提交时记录等待，服务暂时无应答时记录不可用。状态未变化的重复响应只保留一份。采集采用轮询，间隔内连续发生的多次状态变化可能只看到最后一次。精确的源事件仍由任务账本持有。

`watch` 记录任务状态、步骤数、当前步骤索引和观测时间。工具参数、任务原文、工具结果和错误原文保留在原系统。时间为客户端观测到响应时的 Unix 纳秒，时钟域 `observer_unix_ns`。用 ROS 仿真数据合成视频时，应通过共同的仿真时钟事件或接收时间做对齐。

`--finish` 在观测到终态后以该终态结束录制并导出。省略它时，watch 命令在终态返回，可继续保留相机等其他来源一段时间，之后手工结束。超时或主动中断观察命令也会保留已记录内容。开第二个终端查询运行状况：

```sh
python -m task_recorder.cli status take-07
python -m task_recorder.cli stop take-07
python -m task_recorder.cli export take-07
```

`status` 返回实际收到的源、样本数、最近一次接收时间及缺失来源。`stop` 结束的是录制。手工结束时记录状态为 `STOPPED`；如果已按任务终态结束，会保持先前的结果。`export` 把关闭的归档展开到录制服务挂载的存储中，返回摘要。数据位于服务端的 `<recording_id>/media/mission/timeline.jsonl` 和 `<recording_id>/recording.json`。将存储映射到主机目录即可直接拷贝或复盘这些文件。

默认录制器地址为 `http://127.0.0.1:8188`，可在子命令前指定 `--recorder`，或使用环境变量 `EDGE_RECORDER_URL`。Agent 地址单独使用 `--agent`。在已发布的 Console 代理与 Agent 具有相同任务 API 时，两者均可作为观察入口。

## 验证场景

四组本机 HTTP 试验启动真实录制服务，由独立软件任务响应端合成“尚未提交、临时 503、执行中、相同执行中、成功”的状态序列，实际归档四条记录，并按源身份核验请求仅有 GET。另覆盖手工停止、重复导出、身份错配，以及运行中手工结束录制时观察进程自动退出。人工结束和任务完成同时发生时，两端取回同一份最先写入的结束回执。测试输入含原文、参数及凭证占位值，逐行核验导出结果中均无这些字段。[复跑测试](../tests/test_recorder_cli.py)
