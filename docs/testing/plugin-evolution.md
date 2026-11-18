# 单机 Watchdog、EventBus 与 AI 工具验收

本轮使用 Windows x86-64、Docker Linux 容器、ROS 2 Jazzy、Gazebo 及本机 Ollama 缓存。隔离系统实验的代码基点为 `49ee6f2`，运行号为 `20261118T024655Z-1faabe83`。运行开始时已有一份实测证据正在更新，因此 runner 记录 `dirty=true`。另逐文件比较了镜像与工作区的 12 个运行模块，包括 Agent 执行、软件发布、Watchdog 和 Console 归档，SHA-256 全部一致。

## 哪些故障已经复现和核实

这次完整系统回归耗时 12 分 24 秒，18 个选定测试组全部通过。连同构建、额外会话验收和清理共记录 30 个通过阶段，跳过 0 项。Gazebo 实测包含取消、失联、真实位姿验收、语义地点和时钟重置。受管发布与模型测试使用真实本地 Ollama。原始摘要及各项主要回执的文件哈希、镜像身份与实测数值见[机器可读的集成结果](../evidence/plugin-evolution-system.json)。

| 要回答的问题 | 实际注入与观察 | 依据 |
|---|---|---|
| Console 自己停掉后，谁记故障？ | 独立停止 Console 与 Agent，并在事故期间重启 Watchdog。两个目标各有自己的事故、稳定操作身份和人工步骤。恢复后归档读到 24 条 Watchdog 源事实。三项对照任务各调用工具一次。 | [Watchdog 故障实录](../evidence/watchdog-acceptance.json) |
| 重新连上能修复什么？ | Console 停机期间 Agent 写入了一条新的任务失败事实。恢复前本地镜像缺这一条。Watchdog 以原事务身份从游标 17 补到 51，补收第 49 号事实，原事件 ID、任务、trace 和原因码吻合。另用白盒测试验证旧归档重新挂载及同位置内容冲突。 | [处理过程和证据字段](../watchdog.md) |
| 插件能否影响其他模块的数据？ | 在仿真、硬件两份 Compose 中用空卷启动一次性初始化器和 Watchdog，放入相邻服务的哨兵文件。常驻进程只能挂载 watchdog 子目录，无法读取哨兵或创建同级目录。重启后源 ID 保留。 | [两种挂载实测](../evidence/watchdog-mounts.json) |
| EventBus 会怎样呈现无从补回的事实？ | 源从序号 7 才可读取，归档明确登记缺失 1–6，保留第 7 条。归档重启能接续上一条健康变化。无关任务照常完成，提供者只执行一次。 | [来源与恢复实验](../evidence/eventbus-coverage-sample.json) |
| 用户任务能否推动软件能力扩展？ | 在里程计停用时仅提交一次“计算相邻采样间隔的最大值 [100,103,111,115]”。有明确授权与独立期望时，系统从受阻进入提案、发布、续办，最终验证结果 8。负差样本能拒绝错误的绝对值算法。 | [作者、策略及续办](../specs/SDD-012-authored-software-tools.md) |
| 模型新写的代码能否成为 Tool？ | 真实 Qwen 生成字符串处理函数，通过四组独立样本后由部署者批准注册。正常任务得到 2，空正文得到 0。更改样本后旧批准失效，卸载后新任务受阻，原任务仍可查。 | [代码、批准和执行证据](../evidence/authored-software-provider.json) |

## 本机工作站

本次只向日常 Compose 项目增加了 `watchdog` 和一次性 `watchdog-init`。前后核对 14 个既有容器的 ID 与状态均一致。旧 Console 通过公开查询返回了启动和订阅建立两条事实，序号为 3466 和 3467。初次现场核对时 Agent 和 Console 都有成功的巡检基线、连续失败数为零。这里的“恢复”覆盖对应 HTTP 接口和 Watchdog 维护的失败事件订阅，机体是否可运动仍须读取执行内核与真实传感器。

本机的集成镜像已保留为 `edge-agent-ros:plugin-evolution-49ee6f2`。已有容器可用 `docker compose stop watchdog` 停用，`docker compose start watchdog` 再启用。需要使用这个镜像重新创建单独的插件时，在当前 PowerShell 会话设置 `$env:EDGE_IMAGE='edge-agent-ros:plugin-evolution-49ee6f2'`，执行 `docker compose --profile watchdog up -d watchdog`。仿真与板端新装统一见[操作手册](../watchdog.md)。

## 复跑

```powershell
python tests/system_suite.py --ollama-cache-volume edge-agent-ros_models
python tests/document_contract.py -v
```

系统套件在 `.ci-results/system/runs/<运行号>/` 写入所有阶段的日志与 JSON；单独运行 `scripts/verify.ps1` 时使用[独立验收目录](../../artifacts/verification/README.md)。本轮硬件 Compose 验证运行在 AMD64 Docker。物理板卡断电、驱动恢复、长期磁盘增长、温控和整机失效仍需现场实验。
