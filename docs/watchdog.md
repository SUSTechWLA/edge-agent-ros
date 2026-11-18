# 单机 Watchdog 的巡检、断线补收与人工介入

Watchdog 在独立容器持续读取 Agent 与 Console 的公开接口。Console 停止时，它仍能把故障写入自己的持久事件源。连接恢复后，它会补齐自己订阅的受阻任务记录，保存新游标，再报告链路恢复。

## 添加、查看和停用

在项目根目录使用包含 `/opt/edge/client/watchdog.py` 的本次版本镜像。已有单机服务运行时可以单独添加或停止 Watchdog。

```sh
docker compose --profile watchdog up -d watchdog
docker compose ps watchdog
docker compose logs --tail 30 watchdog
docker compose stop watchdog
```

Linux 板端使用 `docker compose -f compose.hardware.yaml --profile watchdog up -d watchdog`，停止时运行 `docker compose -f compose.hardware.yaml stop watchdog`。启动命令先运行一次 `watchdog-init` 创建卷内的 `watchdog/` 目录，成功后再启动独立巡检进程。已经就绪的目录可用 `--no-deps` 单独重建 Watchdog。硬件镜像须与本次源码版本对应。仿真 profile 通过 Compose 服务名分别访问 Console 和 Agent，板端通过本机回环访问二者。

正常巡检不会逐次打印快照。通过 Console 的 `GET /api/events?source=journal%3Awatchdog&limit=50` 读取运行事件。用 `kind=watchdog.intervention_required` 可以单独查人工待办，按同一 `trace_id` 查询已有恢复结果。Console 自身离线时，Watchdog 写入其挂载卷的 `watchdog/module-events.sqlite`；恢复后的 Console 从上次导入位置续读。

## 一次事故如何处理

| 阶段 | 实际操作 | 可核查的记录 |
|---|---|---|
| 正常 | Agent 读取 `GET /agent/capabilities`，检查能力 ID、版本和 effect。Console 读取 `GET /api/event-stats?kind=watchdog.detected`，检查归档身份和游标。 | 心跳、最近的有效状态。刚安装时保存首次成功取到的归档游标。 |
| 达到故障阈值 | 每个目标独立计算连续失败数。短暂失败后成功会清零，持续失败建立一个事故。 | `watchdog.detected` 包含目标、原因、之前的状态和探测路径。 |
| 主动对账 | 在源库中原子提交事故、意图和稳定的 `operation_id`，再重新连接源端。Agent 从 `GET /agent/plugins` 刷新本地服务状态计数，Console 从 `GET /api/history` 核对归档与游标。 | `watchdog.action_requested`、`watchdog.action_attempt` 和失败时的 `watchdog.action_unavailable`。重启重用原操作和已记 attempt。 |
| 修复订阅 | 从持久检查点调用 `GET /api/events`，限定 `source=mission_agent/tasks`、`kind=agent.blocked`，以已观察到的上限从旧到新分页。 | 镜像表与检查点同事务提交。确认事件中的 `repair.from_seq`、`through_seq`、`rows_copied` 和 `archive_id` 记录实际补收范围。 |
| 需要人处理 | 达到初次尝试预算后报告一次原因和操作步骤。主入口仍离线时持续观察；主入口恢复后按较低频率复核第二接口。换代或裁剪越过检查点时另记覆盖风险。 | `watchdog.intervention_required` 含 `data.target`、`reason`、`steps`。例如归档换代时需查旧备份与源任务账本。 |
| 恢复 | 两个读接口可用，必要的补收批次已提交，更新本地核验视图。 | `watchdog.action_confirmed` 保存观测与实际范围，`watchdog.recovered` 引用确认事件。 |

在一次隔离实验中，Watchdog 在双方断开前保存了游标 17。源端后来记录的受阻事件序号为 49。恢复连接后，镜像中新增这一条权威事实，游标推进到 51。测试比较事件 ID、任务、trace 和原因，并核对事故只有一个稳定的读取操作。此处的修复对象是 Watchdog 自己的订阅和已验证视图。控制器、容器管理和设备重启由有相应权限的所有者处理。[完整黑盒样本](evidence/watchdog-acceptance.json)

## 预算、权限与运行边界

默认启动宽限 10 秒，连续 3 次失败建立事故，每轮后等待 5 秒。初次主动对账预算为 3 次，人工项上报后，主入口恢复可读时以 60 秒间隔复核。已经持久化的事故在重启后直接接续。HTTP 默认连接超时 2 秒，连接建立后为整个响应再设 2 秒限时，每个响应最多 256 KiB，HTTP 重定向会作为错误处理。域名解析时延取决于操作系统。每个目标顺序处理，上一目标读超时会相应延迟下一目标的本轮采样。容器限制 32 个进程/线程和 96 MiB 内存。

每轮最多提交一页 16 条权威受阻事件，镜像最多保留最近 4096 条摘要。新安装从第一次成功读取的当前游标开始。首次连接前已有的事件、传感器原文、模型提示和未公开的异常属于订阅覆盖范围以外。源归档可能滚动，水位越过检查点时会给出人工核查提示。模块事故账本可随事件累积，按现场留存策略备份，保持卷有足够空间。源存储无法写入时，容器错误出口提供 `watchdog.unavailable` 和错误类型，Docker 按重启策略拉起服务。

Watchdog 的 HTTP 客户端只发固定 GET，工具清单、模型和事件内容均不能指定新端点或操作。容器不映射 Docker socket、设备、控制器、宿主根目录或任务卷。其可写持久挂载使用 Compose 的 `volume.subpath: watchdog`，进程只能看到模块卷自己的子目录。一次性初始化器以无网络模式挂整卷执行固定的 `mkdir -p /state/watchdog`，随后退出。Console 原有只读挂载可以自动发现该子目录。此约束限定 Watchdog 自身的挂载，其他持有整卷写权限的受信生产者仍可访问其中的数据。

两份真实 Compose 均用空测试卷启动、重启并检查过。Watchdog 能写入自己的事件源，但看不到预先放在相邻 Session 目录中的哨兵文件，也无法创建同级目录。通过整卷只读核对，重启前后源身份一致，哨兵内容完整。运行这一部署方式需使用支持卷子路径挂载的 Engine 和 Compose；本次版本与镜像在[挂载隔离实测](evidence/watchdog-mounts.json)中列出。

本文的主机 Docker 故障测试验证应用层的失联、进程重启、对账及恢复。主机整体掉电时该进程也停止，独立制动和外部主机监督仍分别由网关及部署环境承担。[实现](../client/watchdog.py) · [SDD 与复测入口](specs/SDD-015-independent-watchdog.md)
