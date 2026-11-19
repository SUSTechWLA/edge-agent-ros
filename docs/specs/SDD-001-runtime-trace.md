# SDD-001 · 可以追问原因的端侧运行时

## 背景与价值

原版本已记录任务状态及 ROS 动作。相同的 BLOCKED 可能来自缺少能力、模型返回非法参数或底盘制动。开发者需要知道是谁触发、模型当时看到了什么、哪个模块拒绝、能否重试。过去的工作树没有版本历史，现场发现也无法可靠地指向代码版本。

## 设计与契约

1. Git 的导入基线为 `baseline/v0.1.0`。变更进入带 SDD 的短分支，由 PR、自动检查和验收证据连接到发布分支。运行镜像记录构建时的源码 revision。
2. 语言任务可携带 `trigger.kind/id/source` 以及父任务或事件。复用原触发身份需核对原始意图。触发元数据是来源声明，权限与执行仍由现有入口和能力验证决定。
3. Agent 保存模型交互与工具调用的成对事件，记录调用 ID、契约版本、输入输出摘要和诊断。可热加载提示与诊断目录。旧事件和旧服务仍可读取。
4. 原任务及核心 SQLite 事务继续保存关键结果。外部模块可按版本化 ROS 事件契约报告内部决策、状态转换、watchdog 发现和诊断。控制台按 trace、来源与稳定代码检索，保留迟到父事件和重复投递的含义。

## 验收与证据

- AC-1 可由真实自然语言入口查询到触发、模型请求与回执、工具调用、原始 ROS 证据；相同触发重放不二次执行。
- AC-2 模型不可用、工具返回失败和导航受阻能按层区分。诊断文字可以替换，机器可读码不变。
- AC-3 发布者可在运行中接入和退出；重复、乱序、格式错误的事件有合同测试，事件重放本身不驱动底盘。
- AC-4 仓库有可独立复跑的门禁、分支与发布策略、贡献流程和 PR 记录。

| 条件 | 复测入口 | 本次核对 |
|---|---|---|
| AC-1 | 启动本地 Ollama 和仿真后运行 `python tests/model_trace_acceptance.py` | Qwen2.5:1.5b 从口语请求选中 `status`，读取带时间戳的 `/odom`。同一 watchdog 触发只受理一次；后续 Agent 子任务沿用同一 trace，导出 14 条关联事件。[实测摘要](../evidence/trace-sample.json) |
| AC-2 | 容器中运行 `python3 /opt/edge/tests/agent_trace_contract.py -v`；在空闲仿真运行 `python tests/semantic_acceptance.py` | 6 项契约测试通过，覆盖模型配置切换、请求与回执、诊断文案和旧任务。限制区返回 `scene.place_inaccessible`，实体墙返回 `navigation.path_blocked`。[语义任务实测](../evidence/semantic-sample.json) |
| AC-3 | 容器中运行 `python3 -m unittest discover -v -s /opt/edge/console/server` | ROS 发布者、事件重复、乱序、非法输入、游标消费、掉线恢复与回放均有回归用例。[模块事件](../module-events.md) |
| AC-4 | `python scripts/check_sdd.py --base main`；GitHub 检查 `SDD and contracts` 和 `ROS and Gazebo` | 两份规格和版本化工作流纳入同一 PR；远端规则集的套餐限制记录在[贡献指南](../../CONTRIBUTING.md)。[实施 PR](https://github.com/SUSTechWLA/edge-agent-ros/pull/1) |

## 迁移与回退

部署前保留任务、核心、控制台三个卷的备份。新增字段按兼容方式迁移。旧任务可沿原身份继续执行。回退前停用新增生产者，保留新证据；需要精确恢复旧格式时将旧镜像接到升级前备份卷。禁止在仍有执行中动作时切换到另一个账本。

## 变更记录

2026-11-17 建立原始工作树基线，定义触发、模型、工具、导航与归档的跨模块验收条件。关联室内外语义导航的真实用例见 [SDD-002](SDD-002-semantic-scene.md)。

2026-11-17 接入真实模型和双任务因果回放；补充 ROS 模块事件和独立日志消费者。将 dsh 与 SuperAgent 的上游词义、Edge 当前行为及云边设计目标一并记录到[架构术语对照](../harness-extensions.md#团队共用的术语)，并更新设计模板与 PR 说明。

2026-11-19 更新 AC-4 的源码发布门槛。源码分支检查针对 `main`，板端继续由生成器发布 `deploy/robot`。发行标签必须指向已进入 `origin/main` 的源码提交，保留附注标签、检出提交和 ROS 包版本检查。运行 `python tests/repository_contract.py -v` 可复测主线来源及旧维护线独有提交的拒绝路径。
