# Coding agent 的项目入口

工作单位是一条可验证的行为变化。开始前读 [README](README.md)、[插件开发规范](docs/plugin-development.md)、[贡献约定](CONTRIBUTING.md)，以及 [docs/specs](docs/specs/) 中覆盖相关模块的规格。当前实现以源码和带版本的验收记录为准；目标架构图中的 `PluginHost` 等设计接口不能当作已存在的 SDK。

## 开发约束

1. **先选择插件入口，再改宿主。** 新业务优先使用已有 Tool、Service、事件消费者、观测或配置扩展点。Session、诊断、恢复策略同样适用。复用现有注册路径，不新建平行的插件框架，不要求所有实现继承一个包含全部职责的基类。确实缺少扩展点时，在 SDD 中说明最小宿主变更、旧行为兼容及受影响消费者，再实现它。
2. **明确 Definition / Provider / Consumer。** 先写输入输出、调用方向、状态所有者、权限和依赖；消费者通过公开契约调用。提供者内部可以复用通用库，但不能让消费方导入其私有实现或直接修改它的账本。插件 ID 不应成为核心中硬编码业务分支的依据。
3. **按契约传递上下文。** 保留调用契约已有的 task、trace、operation、request 和父事件关联，按需传递有来源的事实。ID 用于定位，不等于共享全部模型历史。业务策略放在插件内；工具副作用、任务终态和运动控制权仍由相应所有者决定。发布 Event 不等于授权执行。
4. **热插拔必须有明确边界。** 在规格中写安装、首次发现、依赖缺失、draining、换版、卸载、重启、回退对新请求和在途工作的影响。目录 ready 不等于健康，draining 不等于已排空，删清单不等于设备已交接。按实际 Consumer 验证行为；例如 Session 读取期间进入 draining 会拒绝该次结果。当前不能热替换的旧模块应明确标注。
5. **版本和证据不能省略。** 行为变更升级插件版本并保留源码/镜像身份；binding 是清单与依赖摘要，不是服务二进制摘要。带副作用的重试复用 operation ID，按原操作对账；不能因超时或换版直接再做一遍。接入既有事件契约，记录请求、结果、错误、恢复和发布变更；对持久性、缺样、容量、敏感数据及保留期写清限制。
6. **先落 SDD，再落实现。** 将输入、成功/失败判据与兼容约束写成稳定 AC；从[模板](docs/specs/TEMPLATE.md)开始。沿用同一契约的修复可更新已有规格。短期分支完成后，在「变更记录」追加原因、路径、复测命令、结果和待验范围。提交及 PR 引用 SDD 编号。
7. **按消费者回归。** 对待提交改动运行 `python tests/system_suite.py --changed-since HEAD --plan`，对整条 PR 使用目标分支作基点。新增跨模块依赖时，同时更新[测试选择器](tests/test_selection.py)的规则和断言。除新插件测试外，覆盖在途任务、原副作用次数、无关任务、旧数据和事件读取；未知路径、公共执行契约及发布候选扩大回归。纯文档检查与完整发布门槛见[贡献约定](CONTRIBUTING.md)。
8. **术语和交接可追查。** 沿用 [dsh / SuperAgent / ROS 术语](docs/harness-extensions.md#团队共用的术语)。必要的新概念须给出最接近的原词、不同的所有者或生命周期，以及可观察的例子。结束时交付“SDD → 实现 → AC 对应测试 → 实际证据 → 回退入口”，说明未验证项；不能只写“测试通过”。

## 定位模块与验证入口

| 需要定位的问题 | 先查什么 | 可用验证 |
|---|---|---|
| 能力缺口为什么没有触发设计、编码或发布 | 顺着[运行手册](docs/plugin-evolution-operations.md)查源事件、私有收据、已选接口、独立样本和批准摘要。订阅新事实时复用带持久游标和重试预算的消费者，并由新插件持有自己的状态 | `tests/test_plugin_design_watch.py`、`tests/test_software_author_watch.py` 覆盖中断与重投，`tests/system/software_provider_acceptance.py --automatic` 检验原请求至热装、运行及撤回 |
| 独立巡检报错、观察者重启或归档失联 | [Watchdog 手册](docs/watchdog.md)与 [SDD-015](docs/specs/SDD-015-independent-watchdog.md)。看归档身份、补收区间、原事故和恢复操作身份 | `tests/test_watchdog.py` 覆盖故障窗口，`tests/system/watchdog_acceptance.py` 在 Docker 中核对重启后的真实源事件 |
| AI 编写了新的纯软件 Tool | [SDD-014](docs/specs/SDD-014-authored-software-provider.md)。验明设计来源、批准摘要、独立样本和请求原文字节，再查询受管发布收据 | `tests/test_software_provider.py` 与 `tests/system/software_provider_acceptance.py`。后者支持 `--model-cache` 实测 Ollama 生成的候选 |
| 插件证据有断层，或要开发 AI 提议的软件能力 | [EventBus 缺口与恢复](docs/specs/SDD-011-eventbus-coverage.md)、[数列工具](docs/specs/SDD-012-authored-software-tools.md)、[插件设计草案](docs/specs/SDD-013-plugin-design.md)。从原生受阻任务查作者、受管发布、续办和原操作，私有草案只作开发输入 | 运行 `tests/test_series_tools.py`、`tests/test_plugin_design.py`。隔离系统入口包括 `module_bus_acceptance.py`、`plugin_manager_acceptance.py` 及 `plugin_design_acceptance.py`，均在 `tests/system/`。 |
| 自然语言目标、工具绑定、插件收据 | `agent/planning.py`、`agent/service.py`、`plugins/README.md` | `python3 tests/planning_contract.py -v`，故障/重试见 `tests/console_resilience.py` |
| 运动授权、结果未知、重复调度 | `src/edge_core`、`src/edge_gateway`、`src/edge_interfaces` | `scripts/verify.ps1` 在独立 Gazebo 实例中用世界真值验收 |
| 原始传感器、融合、不新鲜的状态 | `perception/`、`observations/`、`console/server/observer.py` | 容器内运行 `python3 -m unittest discover -v -s /opt/edge/console/server` |
| 保存、回放、事件导出 | `console/server/history.py`、`docs/console.md` | 以同一任务和操作 ID 核对源事件、重启后记录与回放时间 |
| 值班人员想知道“现在在做什么，哪一步出了问题” | [运行复盘](docs/console.md#从运行复盘读懂一台机器人的工作)、[SDD-009](docs/specs/SDD-009-run-review.md)；任务终态由 Agent 持有，实时观测和归档序号由 Console 提供 | 先跑 `console/server/test_run_review.py` 的 HTTP 契约，在独立仿真运行 `tests/system/console_browser.mjs` 核对桌面和移动端任务、异常与回放 |
| 人和模型需要低噪声会话，或按需查询某个调用 | [SDD-010](docs/specs/SDD-010-session-evidence.md)、[会话与证据手册](docs/sessions.md)，提供者位于 `sessions_plugin/` 和 `evidence/`，Console 通过 `session_service.py` 调用注册服务 | 两个插件目录的契约测试核对事实、来源和预算；`session_plugin_acceptance.py` 验证独立提供者替换、卸载和恢复；`session_evidence_acceptance.py` 验证自然语言复盘与源值一致 |
| 现场插件失联、版本突变或 AI 候选发布 | 查 [EventBus 读法与接入](docs/module-eventbus.md)、`agent/module_journal.py`、`console/server/journal_ingress.py`；用源身份区分操作、观察及诊断 | 在独立 Docker 运行 `tests/system/module_bus_acceptance.py`；按 [SDD-007](docs/specs/SDD-007-module-eventbus.md) 对照来源、父事件和副作用计数 |
| 自动安装候选后怎样完成原用户工作 | 从[插件管理模块](docs/plugin-manager.md)查候选、现场规则、受管发布版本及绑定到只读工具的后续任务 | `tests/system/plugin_manager_acceptance.py` 验证无人工逐步推动时的完整链路，按 [SDD-008](docs/specs/SDD-008-plugin-manager.md) 复核 |
| 新任务遇到能力缺口，需要开发工具 | 先查注册目录、原任务的终态事件和本机可用的观测。按[插件作者手册](docs/plugin-authoring.md)选择只读声明或独立 HTTP 提供者 | 从独立期望样本开始，执行草案检查和隔离 Docker 热更新测试，再发布到授权目录 |
| 工具报告完成，现场结果未证实，或插件离线 | 先区分 `tool.response`、`verification_pending` 与步骤终态 `verified`。查看 `steps[].verification` 的要求、绑定和已通过回执，再用 `GET /agent/services` 查询所需原版本。[SDD-006](docs/specs/SDD-006-runtime-plugins.md) | 在独立 Docker 运行 `tests/system/runtime_plugin_acceptance.py`，用提供者侧的操作计数核对重启及重试是否重复执行 |

排障从现场版本和任务 ID 开始。用 `git show <sha>:<path>` 读取当时行为，用 `git log --follow -- <规格路径>` 查契约演变。将故障转成可复跑用例，保留修复前后的结果。公开证据使用可分发的测试输入，不复制现场密钥或私有运行数据。

提交前运行 `python scripts/check_sdd.py --base <目标分支>` 和 `git diff --check`。提供者有独立模块目录与 README，规格在 `docs/specs/`，测试报告在 `docs/testing/`，脱敏摘要在 `docs/evidence/`。完整系统套件产物存入 `.ci-results/system/runs/`。独立验收原始记录统一写入 [artifacts/verification/](artifacts/verification/README.md)，从对应运行目录逐文件选择可分发证据，报告直接链接原件。
