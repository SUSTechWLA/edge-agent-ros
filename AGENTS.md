# Coding agent 的项目入口

工作单位是一条可验证的行为变化。开始前读 [README](README.md)、[贡献约定](CONTRIBUTING.md)，以及 [docs/specs](docs/specs/) 中覆盖相关模块的规格。旧行为和未来设计分别从运行代码与规格验收项核对。

| 需要定位的问题 | 先查什么 | 可用验证 |
|---|---|---|
| 自然语言目标、工具绑定、插件收据 | `agent/planning.py`、`agent/service.py`、`plugins/README.md` | `python3 tests/planning_contract.py -v`，故障/重试见 `tests/console_resilience.py` |
| 运动授权、结果未知、重复调度 | `src/edge_core`、`src/edge_gateway`、`src/edge_interfaces` | `scripts/verify.ps1` 在独立 Gazebo 实例中用世界真值验收 |
| 原始传感器、融合、不新鲜的状态 | `perception/`、`observations/`、`console/server/observer.py` | 容器内运行 `python3 -m unittest discover -v -s /opt/edge/console/server` |
| 保存、回放、事件导出 | `console/server/history.py`、`docs/console.md` | 以同一任务和操作 ID 核对源事件、重启后记录与回放时间 |

1. 找到入口和证据来源，把用户输入、期望终态与成功/失败判据写成 AC。新规格从 [模板](docs/specs/TEMPLATE.md) 开始。
2. 在短期分支实现。新增能力首先定义输入、结果证据、失败语义以及版本变化后的旧任务行为。日志引用 ID，公开证据使用可分发的测试输入。
3. 在规格「变更记录」补充实现路径、复测命令和结果，再运行 `python3 scripts/check_sdd.py --base <目标分支>`。提交正文、PR 都关联规格编号。
4. 排障时从现场版本和任务 ID 开始。用 `git show <sha>:<path>` 读取当时行为，用 `git log --follow -- <规格路径>` 查契约演变。把故障转为可复跑的测试，再记录修复的前后结果。
5. 在架构、界面和代码中沿用 [dsh / SuperAgent 与 ROS 术语对照](docs/harness-extensions.md#团队共用的术语)。引入近义概念时，以状态归属、生命周期和一个可观察的例子说明差别。标明当前实现或目标设计，保持已发布接口可用。
