# SDD-015 · 独立 Watchdog 进程巡检与恢复

## 背景与价值

Console 进程能检查插件心跳。Console 自己离线时，需要另一个进程持久记录影响，判明哪些组件仍可访问，在恢复连接后核查源端状态。设备制动继续由控制器与执行网关负责。

## 设计与契约

Watchdog 是可选独立进程，在单机仿真和硬件部署分别启停。它以只读 HTTP 直接巡检 Agent 的能力目录和 Console 的归档接口。在启动宽限期后，连续失败超过配置门槛，就为该目标建立持久事故。事故使用稳定 trace 和 operation ID。源库先记录诊断/重连意图，再创建新连接读取目标的第二个公开接口，核对数据并重建本地可用状态。Agent 按已安装服务的 ready、blocked、draining 计数核查目录，Console 比对归档身份、裁剪及游标，报告断档或换代。仅在两条接口均可读且结果一致时确认恢复。

每个事故按预算复查，预算耗尽要留一份带原因和操作步骤的人工介入事件。目标仍离线时只巡检；主入口恢复后再次尝试对账，故障持续时抑制重复的同类告警。一个目标故障时继续处理另一个目标。Watchdog 进程写自身 SQLite，以 GET 读取两个受信任端点。Compose 通过 `volume.subpath` 只向常驻 Watchdog 挂载 `module-sources/watchdog` 子目录。无网络的一次性初始化器在冷启动时创建子目录，Console 使用原有只读挂载自动发现。受理任务、装卸插件和重发设备操作均保留给原有所有者。

Watchdog 拥有一份本机权威规划/执行失败事件订阅。首次连接记下当时游标，此后只镜像来自 `mission_agent/tasks` 的 `agent.blocked` 元数据。Console 离线后以旧检查点重连，按页补取，元数据和游标同事务落盘。每页至多 16 条，保留最近 4096 条摘要；归档替换、裁剪越过检查点要提示复核覆盖范围。

## 验收与证据

- AC-1 宽限、连续故障阈值、短暂抖动；重复巡检不为同一次故障重开事故，相互独立的目标有各自事故。
- AC-2 拒绝非 2xx、超时、截断/畸形 JSON、错误字段类型、回退游标与不相干归档身份。过大或过慢响应有界处理。
- AC-3 正常、故障、重连、处置结果或人工步骤均可定位。模拟请求完成而本地尚未落账时重启，用同一个已保存的操作恢复只读对账。初次重连预算和报告状态跨重启保留。
- AC-4 独立 Docker 验证 Console 离线期间事故仍落盘，Agent 故障独立可见。恢复后通过公开归档查询父链、稳定 ID、去重告警与操作结果。无关任务与副作用计数维持基线。
- AC-5 Compose 的仿真和硬件 profile 可单独装卸，有进程与 HTTP 预算，停用后保留审计。真机物理断电和主机完全失效另做现场验收。
- AC-6 故障窗口产生新的权威受阻事件。恢复后从同一持久游标重读，验明原事件 ID、补收数量、区间以及新检查点。批次中断或重放保持去重，源换代或裁剪需有清晰的介入说明。

## 迁移与回退

按 [Watchdog 操作手册](../watchdog.md)启动 profile。停止该单一服务会结束巡检，数据库保留在 module-sources 卷。恢复同一部署及卷后接续原事故。源存储无法写入时，进程以非零退出并向容器日志输出故障，Docker 的服务重启策略接手。

## 变更记录

2026-11-18 实现前登记 AC-1 至 AC-5。基线 `50b9344`，沿用 ModuleJournal、Console 归档读取和现有 Agent 目录契约。计划用确定性时钟、HTTP 故障夹具和独立 Compose 进行验证，记录软件权限与真机边界。

2026-11-18 增补 AC-6 并实现权威失败事件订阅和断点补收。运行入口为 `client/watchdog.py`。 `python tests/test_watchdog.py -v` 的 6 项契约通过，覆盖宽限、短故障、双目标隔离、告警去重、进程互斥、两页 24 条故障的跨重启补收、在途读操作对账、归档换代与裁剪、倒退游标、错误来源页、重定向、截断、畸形、超时和体积限制。新增 A → B → A 旧卷重挂回归，相同元数据复用，内容冲突保留原证据和检查点。`docker compose --profile watchdog config -q` 与硬件配置验证通过。

2026-11-18 独立 Docker 用例 `python tests/system/watchdog_acceptance.py --image <本次镜像> --out .ci-results/system/watchdog.json` 通过。Console 与 Agent 故障期间观察到独立事故、预算和人工步骤，Watchdog 重启后按原 ID 续办。实际补收前游标 17、后游标 51，中间新增权威受阻事件 #49，逐字段核对本地镜像、源事件和归档回执。对照任务仍保持原状态和调用次数。完整环境、触发条件和测得的范围见[机器可读证据](../evidence/watchdog-acceptance.json)。板端 Compose 配置已检查，物理板卡及断电场景留待真机验收。

2026-11-18 AC-5 收紧常驻进程写权限。 `python tests/system/watchdog_mounts.py --image edge-agent-ros:sdd015-watchdog-check --hardware-image edge-agent-ros/hardware:sdd015-watchdog-local --out .ci-results/system/watchdog-mounts.json` 在本机 Docker 分别通过仿真、硬件 Compose 冷启动与重启。仅启动两个目标服务，初始化器顺利退出，两个常驻实例的实际挂载都带 `Subpath=watchdog`。对相邻目录的可见/可写断言、哨兵内容以及源数据库重启身份均通过。硬件镜像是在本机已有的 AMD64 硬件镜像上增量复制 Watchdog 程序构建。该实验检查挂载和持久性，探针目标限于隔离回环地址。[两份部署的验收结果](../evidence/watchdog-mounts.json)

2026-11-18 AC-2 边界复核发现超出 SQLite 有符号整数上限的 JSON 游标会导致本地写入溢出。协议入口将该响应分类为 `invalid_reply`，保留另一目标的正常巡检。最大合法游标分页时省去不可表示的排他上限，HTTP 故障单测加入 `2**63` 反例，6 项契约复验通过。
