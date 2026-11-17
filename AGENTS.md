# Coding agent 的项目入口

工作单位是一条可验证的行为变化。开始前读 [README](README.md)、[贡献约定](CONTRIBUTING.md)，以及 [docs/specs](docs/specs/) 中覆盖相关模块的规格。旧行为和未来设计分别从运行代码与规格验收项核对。

| 需要定位的问题 | 先查什么 | 可用验证 |
|---|---|---|
| 自然语言目标、工具绑定、插件收据 | `agent/planning.py`、`agent/service.py`、`plugins/README.md` | `python3 tests/planning_contract.py -v`，故障/重试见 `tests/console_resilience.py` |
| 运动授权、结果未知、重复调度 | `src/edge_core`、`src/edge_gateway`、`src/edge_interfaces` | `scripts/verify.ps1` 在独立 Gazebo 实例中用世界真值验收 |
| 原始传感器、融合、不新鲜的状态 | `perception/`、`observations/`、`console/server/observer.py` | 容器内运行 `python3 -m unittest discover -v -s /opt/edge/console/server` |
| 保存、回放、事件导出 | `console/server/history.py`、`docs/console.md` | 以同一任务和操作 ID 核对源事件、重启后记录与回放时间 |
| 值班人员想知道“现在在做什么，哪一步出了问题” | [运行复盘](docs/console.md#从运行复盘读懂一台机器人的工作)、[SDD-009](docs/specs/SDD-009-run-review.md)；任务终态由 Agent 持有，实时观测和归档序号由 Console 提供 | 先跑 `console/server/test_run_review.py` 的 HTTP 契约，在独立仿真运行 `tests/system/console_browser.mjs` 核对桌面和移动端任务、异常与回放 |
| 现场插件失联、版本突变或 AI 候选发布 | 查 [EventBus 读法与接入](docs/module-eventbus.md)、`agent/module_journal.py`、`console/server/journal_ingress.py`；用源身份区分操作、观察及诊断 | 在独立 Docker 运行 `tests/system/module_bus_acceptance.py`；按 [SDD-007](docs/specs/SDD-007-module-eventbus.md) 对照来源、父事件和副作用计数 |
| 自动安装候选后怎样完成原用户工作 | 从[插件管理模块](docs/plugin-manager.md)查候选、现场规则、受管发布版本及绑定到只读工具的后续任务 | `tests/system/plugin_manager_acceptance.py` 验证无人工逐步推动时的完整链路，按 [SDD-008](docs/specs/SDD-008-plugin-manager.md) 复核 |
| 新任务遇到能力缺口，需要开发工具 | 先查注册目录、原任务的终态事件和本机可用的观测。按[插件作者手册](docs/plugin-authoring.md)选择只读声明或独立 HTTP 提供者 | 从独立期望样本开始，执行草案检查和隔离 Docker 热更新测试，再发布到授权目录 |
| 工具报告完成，现场结果未证实，或插件离线 | 先区分 `tool.response`、`verification_pending` 与步骤终态 `verified`。查看 `steps[].verification` 的要求、绑定和已通过回执，再用 `GET /agent/services` 查询所需原版本。[SDD-006](docs/specs/SDD-006-runtime-plugins.md) | 在独立 Docker 运行 `tests/system/runtime_plugin_acceptance.py`，用提供者侧的操作计数核对重启及重试是否重复执行 |

1. 找到入口和证据来源，把用户输入、期望终态与成功/失败判据写成 AC。新规格从 [模板](docs/specs/TEMPLATE.md) 开始。
2. 在短期分支实现。新增能力首先定义输入、结果证据、失败语义以及版本变化后的旧任务行为。日志引用 ID，公开证据使用可分发的测试输入。
3. 在规格「变更记录」补充实现路径、复测命令和结果，再运行 `python3 scripts/check_sdd.py --base <目标分支>`。提交正文、PR 都关联规格编号。
4. 排障时从现场版本和任务 ID 开始。用 `git show <sha>:<path>` 读取当时行为，用 `git log --follow -- <规格路径>` 查契约演变。把故障转为可复跑的测试，再记录修复的前后结果。
5. 在架构、界面和代码中沿用 [dsh / SuperAgent 与 ROS 术语对照](docs/harness-extensions.md#团队共用的术语)。引入近义概念时，以状态归属、生命周期和一个可观察的例子说明差别。标明当前实现或目标设计，保持已发布接口可用。
6. 对待提交改动运行 `python tests/system_suite.py --changed-since HEAD --plan`，对整条 PR 使用目标分支作基点。如果新增消费方，在 [测试选择器](tests/test_selection.py) 增加依赖和断言。完整发布回归、独立真机门槛和证据存放规则见[系统测试报告](docs/testing/single-edge.md)。
