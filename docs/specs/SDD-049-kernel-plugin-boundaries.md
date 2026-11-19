# SDD-049 单仓内核与插件的运行边界

## 背景与价值

在基点 `89a9622b134ac5ab02549cb13205f20bba7166a8`，一次状态查询经过 `agent/planning.py` 和 `agent/service.py` 中的专用逻辑。部署其中的模块事件写入器还需要 Console 源码，部署 Session 的读取器需要 Evidence 插件源码。增加一个只读工具应当只需描述符、提供者和契约用例。地图探索使用 SDD-048，本规格沿用它的基点，另用 SDD-049 跟踪内核边界。

## 设计与契约

### 扩展点与所有者

| 所有者 | 接口与职责 |
|---|---|
| `edge_contracts` | 只依赖 Python 标准库。集中描述符校验、请求与回执、稳定错误码、事件信封、公开读客户端和可独立执行的契约探针。保留旧 import、字段、绑定摘要的计算和错误语义。 |
| Agent 进程 | `service.py` 组装 HTTP、ROS 订阅和 kernel。`kernel/ledger.py` 独占任务账本，任务、步骤、意图、候选沿用原 SQLite 事务。worker 按原操作续办，verification 按已冻结的绑定裁决；authorization 检查效果、风险、预算和批准。规划器显式取得目录和模型访问入口。 |
| 目录与发布 | catalog 汇集 v1 Tool/Service、v2 soft、physical 和 derived 的只读记录。host 复用现有 soft runtime 与 v2 registry。API 只新增字段。可发现、健康、排空分别有来源。部署方按原 CAS 发布，插件无权写注册表。 |
| execution 进程 | 保有设备租约、运动准入、取消与停稳证据。`inspect` 继续使用已验证的 ROS 委托，保留 core/gateway 所有权和现有 ROS IDL。 |
| 插件与应用 | 调用方按 `operation_id/version/arguments` 调用提供者并核对回执。插件返回候选和事件，kernel 根据效果、授权与独立证据推进步骤。Console 通过公开读 API 获取任务上下文、事件和游标，保有自己的 `history.db`。Session 和 Evidence 共用公开读客户端及其截止时间、响应大小和查询预算。 |
| 可选消费者 | recorder 通过 `task.record` 启动，录制结果归提供者；analysis 仅读取导出文件并输出统计。按 profile 启动，两者均无权改写任务状态。 |

kernel 不依赖插件实现，插件间通过公开协议交互。部署 `edge_contracts` 时须同时覆盖镜像、Compose 入口和板端部署树。事件沿用来源身份、操作号、请求号和父事件关联，保留旧记录的缺省字段。

### 生命周期与旧行为

部署方先启动提供者并检验健康，再发布清单。新任务按获准目录绑定能力；重投、重启与对账复用原 `operation_id`、版本和 binding。依赖断开或冲突时保留既有错误码和重试预算。排空时关闭新绑定，部署方核对在途引用和设备交接，完成后撤清单并停止原进程。换版不能改变已冻结步骤的执行对象。Session 在读取期间仍复核绑定。验收服务离线时保留候选并按原操作续验，保持工具侧副作用次数。未知物理结果继续由 execution 对账。

## 验收与证据

以下为交付条件。各工作包的实测范围见变更记录。

| AC | 输入与独立判据 | 验证入口 |
|---|---|---|
| AC-1 | 旧 v1/v2 描述符与事件样本经新旧入口产生同样的字段、绑定及拒绝码。独立导入契约包，无 agent/console 互引、插件间实现互引或借 `sys.path` 加载 Console。 | 描述符固定样本、依赖检查、两种 Compose 配置 |
| AC-2 | 独立导入 kernel 时无需 Console、感知或外部规划器。启动 Agent，打开旧任务库，对无 `goal_id`、旧格式 steps、原事件与已派发操作核对 ID、状态、游标和重启后候选；对应提供者调用次数相同。 | agent trace、planning、运行时重启/重试契约、真实 SQLite |
| AC-3 | `GET /agent/capabilities` 保留旧字段，统一记录带 `id/version/class/effects/parameters/outcomes/health/binding/source/lifecycle`。重名输入有确定优先级与拒绝码；旧 status 句式和旧任务仍返回原回执。添加一个只读样例仅改清单、provider、用例。 | HTTP 目录、status 正反例、样例 provider 契约 |
| AC-4 | 不挂载 `tasks.db` 或插件 journal 时，Console 仍能追查与任务 API 相符的上下文和源事件；旧事件游标可续读。Session 独立部署，查询超时及 binding 改变仍可见。`edge.db` 的遗留只读适配器集中管理，待 core 提供读 API 后迁移。 | Console/Session 消费者回归和真实 HTTP 查询 |
| AC-5 | 同一组有明确预期的任务分别在默认部署和启用 recorder/analysis 时运行，逐项比对终态；`task.record` 可独立验收，analysis 可脱离 Agent 读取导出样例且输入字节不变。 | recorder conformance、离线 CLI 用例、Compose profiles |
| AC-6 | 对 evidence/status/recorder 执行独立探针，覆盖同操作重投、错版、坏参、健康、超时与进程崩溃后重启。核对操作次数、回执和来源。在途运行中关闭行为开关，冻结绑定仍可读取并对账。 | `python -m edge_contracts.conformance --manifest <json> --url <endpoint> --cases <json>`、provider_contract 旧入口 |
| AC-7 | 变更共享契约可选中所有受影响消费者，生成的部署树可由需要契约包的入口导入。离线分析若不上板，发布清单说明原因。 | `tests/test_selection.py`、`tests/system_suite.py --changed-since <base> --plan`、`scripts/release_robot.py` 部署树 |

## 迁移与回退

WP1 先增加契约包及兼容入口，再迁移消费者。WP2 按现有事务和调用边界移动实现，保留旧名字，schema 迁移只增加兼容项。WP3–WP4 中改变行为的路径使用默认值为 `0` 的开关；启用前对照原回执和实际调用次数。遇到边界与回归冲突时，记录具体命令和阻塞点，停在当前工作包。WP5 以复用既有测试入口、选择器和发布投影收口。

关闭相应行为开关并复查旧路径即可回退行为。纯代码迁移可从最新提交起逐个 `git revert <工作包提交>`。部署回退时恢复原镜像、清单版本与依赖，保留 SQLite 账本和命名卷。核对旧任务、已绑定操作、待验候选及源事件，在原操作完成对账后恢复接单。规格与基线提交单独撤销对运行时无影响。

## 变更记录

2026-11-19，WP0。按仓库模板建立 AC-1 至 AC-7，确定契约、核验与部署所有者。基线固定为 `89a9622b134ac5ab02549cb13205f20bba7166a8`。

在 Windows 使用指定的 CPython 3.11.18，Docker Engine 29.9.3。下列命令中的 BASE 为上述完整提交，python 为该解释器。

| 实测命令 | WP0 结果 |
|---|---|
| `python scripts/check_sdd.py --base BASE`，`git diff --check` | 通过 |
| `python tests/document_contract.py -v`，`python tests/repository_contract.py -v` | 3 + 5 项通过 |
| `python tests/planning_contract.py -v`，`python tests/test_selection.py -v` | 12 + 11 项通过 |
| `python tests/test_plugin_descriptor.py -v`，`python tests/test_soft_runtime.py -v`，`python tests/test_runtime_plugins.py -v` | 9 + 17 + 8 项通过 |
| `python tests/agent_trace_contract.py -v` | Windows 导入 service 时缺少 POSIX fcntl。按测试入口的 ROS 要求用以下容器命令运行当前源码，12 项通过 |
| `docker compose -f compose.yaml config --quiet`，`docker compose -f compose.hardware.yaml config --quiet` | 两份配置均通过 |
| `python tests/system_suite.py --changed-since BASE --plan` 及同命令去掉 `--plan` | 规格选中 repository，实跑通过。报告位于本地 `.ci-results/system/runs/20261119T065144Z-08e716ed/summary.json` |

ROS 复测使用本地镜像与只读工作树，在 PowerShell 的仓库根目录执行：

```powershell
docker run --pull=never --rm --network none --mount "type=bind,source=$PWD,target=/checkout,readonly" -w /checkout edge-agent-ros/hardware:robot-89a9622b134a-amd64 python3 -B tests/agent_trace_contract.py -v
```
