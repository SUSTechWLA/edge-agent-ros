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

### WP1 共享契约与反向依赖

2026-11-19，AC-1、AC-7。描述符与绑定摘要、模块事件、Evidence HTTP 读取，以及 Console 使用的 v1 服务发现、软宿主通信和公开 trace 脱敏逻辑移入 `edge_contracts/`。原脚本入口沿用已有函数和错误类型。板端投影、Docker 构建上下文和两种镜像同步携带契约包，测试选择器将契约变更送入全量消费方回归。此项为实现迁移，无行为开关。

沿用 WP0 的 BASE 和全部必跑命令。Windows 的文档 3、仓库 5、规划 12、选择器 12、描述符 9、soft runtime 17、runtime plugins 8 项通过；ROS 容器 task trace 12 项通过。两份 Compose config、check_sdd、diff --check 通过。新增 `python tests/test_package_boundaries.py -v` 2 项通过，覆盖生产模块的包导入、平铺入口与路径注入。Console 的 ROS 镜像测试 43 项，Evidence 15 项，Session 9 项通过。板端投影测试 `python tests/test_robot_release.py -v` 3 项通过。

`python tests/system_suite.py --changed-since BASE --plan` 选中 21 个阶段。使用本地 SDK、已安装校园资产及只读模型卷运行同一套件，测试项目的清理保留命名卷。首轮 `.ci-results/kernel-runner/wp1/20261119T065914Z-1352a50b/summary.json` 有 30 条通过、3 条失败、1 条阻塞。事件回补、任务重投/重启、插件换版、物理运动和停车、真实模型、浏览器以及冻结任务指标通过。三项差异分别完整复测：

- 板端离线 apt 冷构建失败（exit 100）。复用基点镜像的精确 SDK 层，对照 9 项包版本，再使用当前 COPY 和 colcon 编译。首次启动检查发现 Compose 重设 PYTHONPATH 覆盖 ROS 路径，移除该覆盖并保留镜像 ENV。原 board 阶段在干净候选 43891930 上运行通过，见 `.ci-results/kernel-runner/board-verify/runs/20261119T071420Z-f2039c24/summary.json` 和 `board-verify/sdk-proof.json`（相对于 kernel-runner）。验证平台 Linux/amd64、合成 ROS 输入。
- 插件隔离首次被本地测试工具拦下只读 image save，开放其 stdout 导出后 12/12 命令通过，见 `.ci-results/kernel-runner/wp1-plugin-r1/stage-results.json`。
- 校园首次因探针拟读取已有仿真时钟被拦截；在独立小型仿真上按相同断言验证时钟、校园重载、贴图与机载观测，全部 16/16 命令通过，见 `.ci-results/kernel-runner/wp1-campus-r1/stage-results.json`。

共享帧编码、栅格导出和预览 HTTP 接口供原插件独立使用；保留原导出名字。上述迁移与真实仿真验收均未改变 operation_id、绑定、终态、SQLite 事务或授权。

### WP2 任务内核

2026-11-19，AC-2。Agent 入口组装 ROS、HTTP 和 worker。任务库、迁移与一次提交内的写入移入 ledger，保留 POSIX 独占锁，Windows 独立账本用非阻塞文件锁。worker 通过注入的规划函数处理队列，verification 持久化候选及独立验收决定，authorization 统一执行原效果检查与预算。catalog 复用 v1/v2 快照；host 访问现有软宿主和物理控制器。公开请求与回执沿用原摘要和错误规范。部署侧批准与 CAS、core 的运动准入和停稳协议继续使用原所有者。

**AC-2 独立证据。** `python tests/test_kernel.py -v` 在 Windows 和 Linux 各通过 3 项。用旧表结构打开真实 SQLite，第二个进程争用账本写锁时被拒绝。用 SQLite 触发器拒绝写入后，接受事件与新任务同时回滚，候选与 tool.response 也同时回滚。独立 Python 进程可在未加载 rclpy、规划器、Console 和感知插件时导入六个 kernel 模块。ROS 镜像中的 `python3 -B tests/agent_trace_contract.py -v` 与 `python3 -B tests/agent_verification_contract.py -v` 分别通过 12、11 项。真实 ROS 节点收到 /odom (1.25, 2.5)，恢复的旧 status 和新 HTTP status 各派发一次，均 SUCCEEDED。SIGKILL 后重启，两个操作号、事件与 GET 字段逐项一致。原镜像及当前入口收到 SIGTERM 后均观测过 8 秒内 HTTP 仍能响应；本次验证的重启使用进程崩溃路径。

基线表中的命令在 WP2 重跑通过，选择器为 12 项。另运行 `python tests/test_author_tool.py -v`（10 项）、`python tests/test_package_boundaries.py -v`（2 项）和 `python tests/test_robot_release.py -v`（3 项）。在候选树上调用 `scripts.release_robot.projection`，208 个板端文件包含全部 19 个 kernel 与 contracts 源文件。两个 Compose config 校验通过。

`python tests/system_suite.py --changed-since 89a9622 --plan` 选中全部 21 个阶段。离线验收实际入口为 `python .ci-results/kernel-runner/board-verify/board.py src-19387672 --full`，使用相同硬件 SDK、源码标签、预装模型和独立校园仿真。干净测试快照 `19387672f9dd887e248862e8cb6ec39834bf87d4` 的实现与提交候选逐文件核对。首次报告 `.ci-results/kernel-runner/wp2/20261119T074007Z-06d5068b/summary.json` 中 33 条阶段记录通过，Session 的两个后续检查因 registry 位于该快照的 .ci-results 之外被夹具拒绝。改用快照内的输出目录，session-evidence 通过，session-plugin 在 Windows 263 字符的原子替换路径触发 WinError 3。保留了两次结果。

在较短的同 SHA worktree `D:/project/kb2t` 内重跑 repository、console-missions、places、UI（含两个 Session 子阶段）与本地模型。实际报告 `.ci-results/kernel-runner/wp2-session-repeat/20261119T082651Z-9ccd42fb/summary.json` 有 14 条通过、零失败或跳过。两次同版本实测合起来覆盖全部 21 个选中阶段。Session 恢复了原 binding，12 条既有机器人任务的状态一致。验收使用 Linux/amd64 容器、真实本地模型和合成 ROS/Gazebo 输入，未运行 ARM64 或实机试验。

**首轮交付边界（WP0–WP2）。** 按任务约定收在全绿的 WP2。WP3 的统一能力字段及 status 提供者尚未实现，旧 status 和 inspect 仍走现有入口。WP4 的 Observer/History 仍读取任务及插件账本，analysis 离线 CLI 和 recorder 的独立契约验收待实现。WP5 的 `edge_contracts.conformance` CLI 和最终覆盖选择器待完成；板端投影已覆盖目前交付的包。WP0–WP2 没有需要切换的运行时行为开关，按工作包逆序 revert 或部署基线镜像即可回退。

### WP3 统一目录与状态查询

在同一目录快照中为公开能力投影增加 records 与冲突诊断。原 capabilities 字段继续供旧客户端读取。status 沿用内置实现，Agent 持有唯一的里程计订阅，独立 soft host 的 ctx.require 只能读取该宿主中已冻结的依赖。跨进程迁移需先建立只读事实接口，并约定软宿主离线时如何处理旧任务。inspect 委托 Core 执行运动，继续保留原授权与对账入口。

在 Linux/amd64 的隔离 ROS 域，以合成里程计、真实 HTTP 和 SQLite 验证 status 的旧句式、证据字节、缺样、过期数据和无 binding 任务恢复。只读工具 track_distance 使用 v1 清单、独立提供者和 8 组输入验证，沿用原注册与调用接口。

AC-3 验收使用 `python3 tests/test_capability_catalog.py --http -v`（真实 HTTP、两个 Unix socket 与 4 项投影测试）、`python3 tests/test_status_plugin.py -v`（4 项）、`python3 tests/agent_trace_contract.py -v`（12 项）和 `python3 tests/agent_verification_contract.py -v`（11 项）。Windows 快速门逐条结果见 `.ci-results/k2/wp3/gate.json`，样例工具的 8 个用例及重复投递回执见 `.ci-results/sdd-049/wp3/track-1795082409853305700.json`。旧清单没有声明的契约字段取 null，未探测的健康状态取 unknown。

### WP4 Console 通过 Agent 查询事实

任务与相关步骤由 Agent 在同一只读 SQLite 快照中查询。Console 使用 `EDGE_AGENT_URL` 访问以下 GET。既有任务详情、复盘和 Session 客户端继续使用原有 URL。

| Agent 路径 | 读者与续读方式 |
|---|---|
| `/agent/task-context` | 按 task_id 或 trace_id 取得任务、旧步骤、关联任务及覆盖窗口。 |
| `/agent/task-index` | 复盘和 Session 列表共用分类计数，before 绑定原账本、状态筛选和行号。 |
| `/agent/task-events` | Observer 以持久化的源身份和原始 seq 续读。源更换后按新身份在 Console 归档中查找对应检查点。 |
| `/agent/registry-events` | 读取 runtime.registry 原有事件及心跳，保留生产者身份、序号和原始字节。 |

Console 拥有自己的 `history.db`，先收录 Agent 派发事件，再关联同批 core 事件。部署通过 `EDGE_JOURNAL_SOURCES` 指定可公开的模块日志，聚合卷按模块目录发现来源。`console/server/legacy_ledger.py` 中的 `LegacyLedgerReader` 是 core 的过渡性只读适配器，待 core 提供事实接口后迁移。

AC-4 在线实测使用 `python3 -B tests/test_console_facts.py -v`，3 项均通过。旧任务的游标 7 可接续获得序号 13 与 21，查询前后任务、触发与事件表相同。使用 `python3 -m unittest discover -v -s /opt/edge/console/server` 通过 44 项，同一离线 ROS 镜像中的 Evidence 15 项和 Session 9 项通过。Windows 快速门及 Compose 配置见 `.ci-results/k2/wp4/gate.json`。在独立双容器中，Console 无任务卷，Edge 浏览器实际打开会话、复盘和旧任务详情，并收到 SUCCEEDED 和原 status 步骤，证据见 `.ci-results/k2/wp4/811128efb5/acceptance.json`。

板端离线兼容性单独验收。在隔离容器内设置 `EDGE_CONSOLE_OBSERVE_ONLY=1`、`EDGE_AGENT_URL=http://127.0.0.1:1`，用 `.ci-results/k2/wp4/offline_observe_probe.py` 查询同一份旧任务。旧版复盘索引返回 HTTP 200，候选返回 503；任务上下文的 task_available 从 true 变为 false。两个版本的事件归档查询均为 HTTP 200。逐项回执在 `.ci-results/k2/wp4/observe-legacy.json` 与 `.ci-results/k2/wp4/observe-candidate.json`。板端 observe 启动脚本明确禁止随之启动控制 Agent，仅启动 perception 与 console。

为保留上述旧任务，板端观察模式增加独立 `task-facts` 进程。服务复用 Agent 的只读投影，读取原任务卷；Console 通过 `EDGE_FACTS_URL` 访问。常规任务模式沿用 Agent 的 HTTP 入口。已存在的 `EDGE_CONSOLE_OBSERVE_ONLY` 默认为 0，观察配置将它设为 1；任务提交与重试仍受原代理约束。台架切换任务模式前，在事实服务处检查原账本中的未结束任务。

修复后，原 `sh scripts/deploy.sh hardware observe` 在隔离网络启动上述三个容器，旧任务列表和详情恢复为 HTTP 200。见 `.ci-results/k2/wp4/edge-wp4-observe-98fde60b9d/summary.json`。在正常关闭和最新事务仍在 WAL 的两种库上，Edge 均实际打开离线会话、步骤证据与先前归档。WAL 中的第 34 条事件可读，库、WAL、SHM 的文件集合、大小、哈希和修改时间逐项一致。Console 的任务提交返回 403，事实服务相应写请求返回 405。两次完整报告在 `.ci-results/k2/wp4/observe-e4cda4b9/{clean,wal}/acceptance.json`。最终 Console 套件 45 项、`tests/test_console_facts.py` 5 项、Agent trace 12 项、verification 11 项通过；范围均为 Linux/amd64 软件夹具。

### WP5 独立契约验收与离线分析

只读 Tool 的探针使用清单、公开 HTTP 和独立期望值核对回执。显式托管的测试进程可执行崩溃和重启验证。对原生 status 和 recorder，还需按实际公开读接口验证其事实；录制的启停与导出属于 recorder 自己的生命周期。默认部署与录制 profile 应对相同输入产生相同的任务终态、操作号及派发次数。

工作站上的 `analysis/` 读取导出的副本。板端发布包含 `agent/kernel/`、`agent/facts_server.py` 和 `edge_contracts/`；离线分析及其样例只在开发分支分发。选择器将公共契约改动送入全量阶段，分析改动运行仓库级离线测试，板端回归在实际镜像中检查三类提供者。

AC-5/6 在 Linux/amd64 隔离 ROS 域用 `python3 -B tests/test_plugin_conformance.py --out /evidence/plugin-conformance.json` 实测。录制器启用和停机时，输入均为一个缺里程计任务和两个收到合成里程计的任务。两组均得到一个 `RECOVERY_REQUIRED`、两个 `SUCCEEDED`，每个原操作的 SQLite 派发数均为 1。夹具在每组开始发送数据前先验缺样，提交两个正例前从本轮 Agent 确认已收到各自的时间戳。独立录制得到一个状态样本、七个 trace 样本；Evidence 重投和提供者重启后原操作只有一份持久化终态回执。

三个提供者的健康、坏版本、坏参数、超时恢复和托管进程崩溃/重启共 15 项通过。`status`、`task.record` 的读契约验证公开的任务和录制资源，Evidence 使用部署清单中的 `evidence_schema` v1 Tool。逐次 HTTP 回应、固定操作号、SQLite 计数和两组任务终态见 `.ci-results/k2/wp5/plugin-conformance.json`。`python -B tests/test_conformance.py -v` 的 6 项与 `python -B tests/test_offline_analysis.py -v` 的 5 项通过，后者另在仅挂分析包与样例的 Linux 环境复现。原 `tests/system/verify_sample_provider.py` 的 8 个用例各验证两份回执，轨迹样例的托管进程验收见 `.ci-results/k2/wp5/track-managed.json`。选择器 16 项、部署投影 4 项及其余快速门见 `.ci-results/k2/wp5/gate.json`。
