# Edge Agent ROS 文档目录

第一次运行从仓库首页的“快速运行”开始。要知道机器人为什么停下，可以依次读任务会话、对应的现场错误码和该版本的故障实测。要新增能力，则从插件接口、规格和消费者测试开始。[快速运行](../README.md#快速运行) · [Coding Agent 开发入口](../AGENTS.md)

| 我想做什么 | 从这里读 |
|---|---|
| 看机器人第一次进入陌生环境怎样选点、在哪儿复查、出故障怎样恢复 | [校园录像与逐步核验](testing/unknown-campus.md) |
| 给仿真、真机或软件任务按需打开和关闭录制，导出时间线与视频 | [通用录制插件](../task_recorder/README.md) |
| 看一条真实仿真任务如何下发、运动、遇错、恢复与停车 | [完整任务录像与可点击章节](../artifacts/verification/rover-long-mission/continuous-02/video/index.html) · [原始观测与复现方法](testing/rover-mission-video.md) |
| 给一台新车交付精简代码、工厂标定，验证第一次进场建图 | [无人车出厂、首次进场与板端交付](rover-commissioning.md) |
| 准备真机首测，复盘多机体故障，判断哪些问题能自动恢复 | [首测预检与只读任务](testing/hardware-first-run.md) · [现场验收与升级记录](testing/edge-field-readiness.md) · [首次上板](board-first-boot.md) |
| 启动校园，查看四种机体、具名任务、关节测量和连通步道 | [校园部署](../scene/campus_large/README.md) · [机器人近景与验收](testing/campus-robot-view.md) · [插件边界](testing/campus-plugin.md) |
| 给工作台加 3D 地图、相机、MID-360 或 RealSense 输入，切换本地/云端模型 | [全栈快速上手与逐项撤下](perception-model-quickstart.md) · [隔离实验](testing/map-and-model-plugins.md) · [真实模型选工具记录](evidence/workbench-model-routing.json) |
| 部署仿真、模型或板端，知道哪些平台已有实测 | [部署](deployment.md) · [模型配置](model.md) · [板卡验收](architecture/board-validation.md) |
| 在观测台下发任务，检查激光与地图，追查失败步骤 | [观测台](console.md) · [语义地图](semantic-scene.md) · [会话与按需取证](sessions.md) |
| 看懂任务执行、资源占用、停车与重启 | [ROS 接口](v1/interfaces.md) · [任务内核](v1/core.md) · [执行网关](v1/gateway.md) |
| 给已有系统增加 Tool、Service 或事件消费者 | [插件开发指南](plugin-development.md) · [术语和已实现的扩展点](harness-extensions.md) |
| 让 Agent 提出工具，检查谁能发布、如何续办原任务 | [插件作者](plugin-authoring.md) · [插件管理](plugin-manager.md) |
| 让运行中的 Agent 把能力缺口交给设计与编码插件，查每一步为何继续或停止 | [Compose 安装、暂停与取证](plugin-evolution-operations.md) · [设计订阅](specs/SDD-017-plugin-design-watch.md) · [接口快照](specs/SDD-018-design-contracts.md) · [软件编码候选](specs/SDD-019-event-driven-software-author.md) |
| 让新插件记录异常和版本变化，按来源追查事实 | [模块事件总线](module-eventbus.md) · [ROS 消息与归档读取](module-events.md) |
| 运行巡检、查看人工介入原因或核对失联后的补收 | [Watchdog 手册](watchdog.md) · [故障窗口的实际记录](evidence/watchdog-acceptance.json) |
| 把 AI 提出的新算法做成可以热装的文本 Tool | [设计草案](specs/SDD-013-plugin-design.md) · [候选、验收和批准发布](specs/SDD-014-authored-software-provider.md) |
| 对照语言模型实际写对、写错、修正、交付和撤回的案例 | [系统验收与积压复现](testing/event-driven-evolution.md) · [模型正反样本](evidence/software-author-acceptance.json) |
| 评估下一步的架构与开发范围 | [单机能力评价、EventBus 与插件演进](architecture/plugin-evolution.md) · [目标架构教材](architecture/index.html) · [首版 PRD](v1/PRD.md) · [各次变更的 SDD](specs/) |
| 找到一次测试的输入、运行环境和结论 | [单机故障与选测](testing/single-edge.md) · [Watchdog 与 AI 工具集成实测](testing/plugin-evolution.md) · [实测报告](testing/) · [首版验收](v1/validation.md) · [精选证据](evidence/) |

## 文档与证据放在哪里

仓库根目录的 `evidence/` 是可运行的取证服务源码。`docs/evidence/` 是随代码版本发布的样本，新人可以直接打开。`plugins/derived/` 是 Docker 挂载的现场工具注册目录，清单由受管发布流程写入，空目录也有运行用途。源码目录中的 Python `__pycache__/` 由解释器按需生成，可以清理。

| 内容 | 维护位置 | 何时更新 |
|---|---|---|
| 使用手册、架构和公共契约 | `docs/*.md`、`docs/architecture/`；首版模块手册在 `docs/v1/` | 入口、行为或设计改变时更新对应手册。模块目录的 README 维护实现和安装细节 |
| 开发规格与验收标准 | `docs/specs/SDD-*.md` | 开工前写输入和预期，验收后补实测与变更记录 |
| 对测试结果的解释 | `docs/testing/` | 标出版本、运行号、环境、命令、结果及待验范围，链接对应样本 |
| 随版本交付的证据 | `docs/evidence/` 保存界面、发布证据和验收摘要；独立验收的精选原件保存在 [artifacts/verification/](../artifacts/verification/README.md) 对应运行号下 | 报告直接引用原件。界面截图和数值来自同一次实验时放在一起 |
| 教材插图和示意图 | `docs/assets/` 或所属教材目录 | 图与正文同步维护 |
| 本机的完整日志、轨迹、镜像包 | 系统套件写入 `.ci-results/system/runs/<运行号>/`，独立验收写入 `artifacts/verification/<运行号>/`，手工导出镜像写入 `artifacts/images/` | 运行工具生成，本地默认保留。需要分发独立验收原件时按运行号逐文件纳入 Git |

例如复盘 v0.1 的一次停车回弹，先读[联合验收报告](v1/console-validation.md)，再打开[原失败样本](../artifacts/verification/20261117-031103-3a9cc1/cancel.json)和[修复后的对照](../artifacts/verification/20261117-031532-ae63c8/cancel.json)。同一运行号的其他本机日志就在原件旁边。

早期版本的观测台与发布材料现分别位于 [evidence/console/](evidence/console/) 和 [evidence/releases/](evidence/releases/)。运行号与文件名沿用原记录。查看旧提交时，使用该提交对应的路径。
