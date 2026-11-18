# 从一次能力缺口到一个可热加载的工具

用户说“用千米每小时报告本机前向速度”。里程计已经给出了沿机体前向的有符号速度；机器人后退时，原值可能是 −0.5 m/s，对应 −1.8 km/h。要让 Agent 以后能直接回答这句话，可以增加一份 JSON 工具定义。读完本页，你能判断这类需求是否适合派生工具、从未派发的任务生成候选、用独立样本验收，并在运行中发布或撤回它。[行为规格](specs/SDD-005-authored-tools.md)

## 怎样选择扩展点

运行中的 Agent 已经拒绝了一个超现有能力的请求时，可以启用独立设计订阅者。它从作者已核对的拒绝事实接手。设计模型收到当前目录中实际安装的扩展契约，包括 ROS 观测、独立结果验收和受限文本处理。模型若选中现场允许的文本处理接口，独立编码插件可以进一步产出代码候选和工具清单。现场独立样本、正式发布和设备授权由相应的所有者持有。[自动设计](specs/SDD-017-plugin-design-watch.md) · [热更新设计上下文](specs/SDD-018-design-contracts.md) · [自动编码与交接](specs/SDD-019-event-driven-software-author.md)

纯软件例子是“计算相邻采样间隔的最大值 [100,103,111,115]”。按输入顺序作差得到 3、8、4，答案为 8。作者选择已有数列算子，正式目录保留按内容绑定的声明。测试使用独立期望值，核对换版和撤回，再从用户任务查原始输入、版本和结果。[软件工具协议与回退](specs/SDD-012-authored-software-tools.md)

需要新算法、设备适配或服务时，可从作者的拒绝事件和私有决定生成带材料来源的开发草案，供 Coding Agent 接续开发。草案中的依赖、权限和测试在隔离环境验收后才能作为正式发布依据。[插件设计交接](specs/SDD-013-plugin-design.md)

例如“忽略大小写，统计完整单词 robot 的出现次数”。编码 Agent 或本地模型可提出文本表达式，在资源受限的独立提供者中运行。发布者用独立文本样本核验候选，部署者批准与该设计、样本和代码绑定的摘要后热装。运行中的 Agent 按原任务正文与版本核对回执。[操作示例](../examples/software-provider/README.md) · [验收记录](evidence/authored-software-provider.json)

已有现场契约的能力缺口，例如已授权的前向速度换算，可以由独立管理插件自动完成发布与任务续办。以下步骤适合开发新候选、人工检查或调试作者。[插件管理与自主恢复](plugin-manager.md)

| 用户需要什么 | 当前可以怎样实现 | 由谁证明有效 |
|---|---|---|
| 换算坐标、朝向或前向速度；判断是否越过某个阈值 | AI 或开发者编写 `derived.*` 声明。运行时解释同一帧里程计，支持一个字段、一次单位换算和可选的一次比较。 | 发布者提供独立输入和预期值；Agent 为每次调用记录 ROS 来源、frame、采样时刻、原值、结果及定义版本。 |
| 读取电池温度、图像识别、运行新算法 | 使用现有 HTTP Tool 清单连接独立提供者。准备相应传感器、模型或软件服务，定义输入与回执，再发布清单。 | 提供者验证设备、输入、结果和自身部署；调用方检查绑定与回执。 |
| 移动、抓取、机械复位或修复驱动 | 设计相应的设备适配、前置条件和动作授权，经过仿真及机体验收后接入。 | 执行端对账、取消和停稳；实际物理效果需独立观测。 |

作者有两个入口。开发时可以把一项已知需求交给作者 CLI。需要系统自己发现困难时，可以启用仿真 Compose 的 `plugin-author` profile，由独立进程订阅任务故障。两个入口复用同一个生成器。普通任务 Agent 读取已发布目录，作者将候选写入自己的卷，发布者持有正式目录的写权限。[HTTP 插件契约](../plugins/README.md) · [现有扩展入口](harness-extensions.md)

### 借鉴 dsh 的部分

dsh 的 Cordis 插件通过上下文取得服务，声明依赖，注册工具、适配器和事件处理器，并在卸载时释放注册。它区分服务定义、提供者和使用方。官方的作者指南也指导 Agent 查询现有扩展点、在工作区写 bundle、通过 Plugin Manager 安装并验证运行结果。配置能否热应用取决于 profile 的 HMR 设置；安装后的 Host 代码可在宿主进程运行。[Cordis 插件与清理](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/cordis-primer.md#cordis-in-five-ideas) · [dsh 的 AI 作者指南](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/preset/agent-preset/skills/dsh-doc/references/plugin-development.md) · [Plugin Manager 的生效与权限条件](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/boot/plugin-manager/README.md#use-this-package)

Edge 沿用同样的职责划分。**作者**选取领域字段与单位并写候选；**发布者**核对样本和旧版本，修改已发布目录；**运行中的 Agent**在规划时获取当前目录，执行时复核原绑定。定义存放于 `plugins/derived/`，仿真和硬件 Compose 均以只读方式把整个插件目录挂载进 Agent。带设备副作用的跨进程操作另外受 ROS 内核及设备契约约束。[作者与发布者实现](../agent/author_tool.py) · [只读解释器](../agent/derived_tools.py)

## 顺着一个实测任务读证据

打开[本版实验记录](evidence/authored-tools-sample.json)，从 `event_to_model_to_tool` 开始。先查 `blocked_task.event_id`，它应与 `watcher.source.event_id` 和 `first.trigger.parent_event_id` 一致。这样就知道哪一次困难触发了作者、哪一次新任务验证了成果。再看 `model.reply` 的 `speed/km/h`，对照 `first.step.evidence` 中的 −0.5 m/s 和 −1.8 km/h。最后比对 `in_flight.resumed_version` 与 `first.step.version`，确认在途操作恢复到了原版本。这次原任务使用了离线规划器，发布后的任务按描述精确匹配；模型实际参与的环节是编写工具。系统回归、独立 HTTP 算法和两种 CPU 架构的运行记录也保存在同一文件。

## 认识工具定义与证据

```json
{"schema_version":1,"id":"derived.speed_kmh","description":"用千米每小时报告本机前向速度","field":"speed","unit":"km/h"}
```

`id` 是长期稳定的工具标识，`description` 是模型看到的语义说明，也是本地确定性规划器可匹配的完整表达。内容摘要标识具体版本，更改描述同样会生成新版本。

| 字段 | 测量含义 | 输出单位 |
|---|---|---|
| `speed` | 里程计 `twist.twist.linear.x`，沿机体前向的有符号速度。这个分量为零也可能存在转动。 | `m/s`、`km/h`、`cm/s` |
| `yaw` | 从里程计姿态得到的平面偏航角 | `rad`、`deg` |
| `x`、`y` | 里程计参考系内的坐标，解释结果时同时查看 `frame` | `m`、`cm`、`mm` |

对于“前向速度是否大于等于 1 km/h”，可在定义中加入 `"comparison":{"op":"ge","threshold":1}`，先换算、再比较。可用运算符为 `lt/le/gt/ge`。成功结果另有布尔字段 `matches`，样本需覆盖命中和未命中，最好加入等于阈值的边界。比较结果供任务判断；触发通知、派发新动作是独立功能。

## 在 Windows 工作站复现

以下命令在项目根目录的 PowerShell 7 中执行。首次引入新解释器时，在仿真项目运行 `./scripts/start.ps1` 构建并部署包含 SDD-005 的镜像。用 `docker compose --profile model exec -T model ollama list` 确认已运行的模型服务有 `qwen2.5:1.5b`。例子使用默认工作台端口、模型容器的 Compose 网络和插件目录；自定义部署可替换对应值。一次试验的候选、模型记录、样本、发布回执均保存在 `.ci-results/plugin-authoring/`，运行中的 Agent 只能读取正式目录。

```powershell
$root = (Get-Location).Path
$work = (New-Item -ItemType Directory -Force (Join-Path $root '.ci-results/plugin-authoring')).FullName
$registry = (New-Item -ItemType Directory -Force (Join-Path $root 'plugins/derived')).FullName
$image = if ($env:EDGE_IMAGE) { $env:EDGE_IMAGE } else { 'edge-agent-ros:v0.1' }
$cli = '/opt/edge/agent/author_tool.py'
$url = 'http://127.0.0.1:8765'
$toolId = 'derived.speed_kmh'
$modelNetwork = 'edge-agent-ros_default'
Copy-Item examples/derived-speed/cases.json (Join-Path $work 'cases.json')
```

**1. 选择可以交给作者的请求。** 在工作台查看任务及 `GET /agent/capabilities`。已有工具能交付结果时，先检查规划或路由。若任务已派发而结果未知，按原操作 ID 对账。`planner.not_configured` 说明本地规则没有处理该输入且模型规划器不可用；仅凭此错误无法断定系统缺少相应工具。核查后，记录确实要补的交付物。

```powershell
$taskId = Read-Host '输入已有、尚未派发且状态为 BLOCKED 的任务 ID'
$taskUri = "$url/agent/tasks?id=$([uri]::EscapeDataString($taskId))&limit=500"
Invoke-WebRequest -NoProxy -Uri $taskUri -OutFile (Join-Path $work 'task.json')
$modelArgs = @('run','--rm','--network',$modelNetwork,'--entrypoint','python3',
    '-e','EDGE_OLLAMA_URL=http://model:11434','--mount',"type=bind,source=$work,target=/work")
$proposal = docker @modelArgs $image $cli draft --id $toolId --from-task /work/task.json --record /work/model.json
if ($LASTEXITCODE) { throw '草案生成失败，检查诊断与模型记录' }
$proposal | Set-Content -Encoding utf8NoBOM (Join-Path $work 'definition.json')
```

作者读取带终态事件的查询结果，接受 `BLOCKED`、空步骤，且有同一 trace 的 `planner.capability_missing` 或 `planner.not_configured` 的未派发任务。模型收到需求、错误码和支持的单位。回执 `model.json` 关联原任务、trace、模型和提示摘要。若从一条显式需求开始，可将 `--from-task /work/task.json` 替换为 `--request '用千米每小时报告本机前向速度'`；这种草案没有来源任务。模型可以返回 `unsupported`，发布者也会拒绝语义不符合样本的候选。

**2. 验收并发布。** 从需求独立得到期望值，例如 −0.5 m/s 必须转换为 −1.8 km/h。不要由候选实现计算自己的预期。以下默认样本适合这一条速度需求。换任务就要更换样本；比较工具还要为每个样本指定 `matches`。[可复制的样本](../examples/derived-speed/cases.json)

```powershell
$checkArgs = @('run','--rm','--network','none','--entrypoint','python3',
    '--mount',"type=bind,source=$work,target=/work,readonly")
$publishArgs = $checkArgs + @('--mount',"type=bind,source=$registry,target=/registry")
docker @checkArgs $image $cli check /work/definition.json --cases /work/cases.json
if ($LASTEXITCODE) { throw '样本未通过，保留候选并修正' }
$published = docker @publishArgs $image $cli publish /work/definition.json --cases /work/cases.json --registry /registry --expect absent
if ($LASTEXITCODE) { throw '发布失败，重新查看当前版本和候选' }
$published | Set-Content -Encoding utf8NoBOM (Join-Path $work 'published.json')
$v1 = ($published -join "`n" | ConvertFrom-Json).version
(Invoke-RestMethod -NoProxy "$url/agent/capabilities").capabilities | Where-Object id -eq $toolId
```

`check` 与 `publish` 都执行验收，回执含 `case_count`、`cases_sha256` 和 `version`。第二次执行同一条 `--expect absent` 将因名称已存在而失败。不同作者不得各自覆盖对方刚发布的版本。共用一个发布端统一写该目录，读取方可持续在线。

**3. 通过普通任务入口验证。** 在工作台提交定义里的完整描述，查看实际选中工具、`steps[0].operation_id` 和证据。证据中 `raw_value/raw_unit` 是原始读数，`value/unit` 是换算结果，同时检查 `source/frame/stamp_ns/definition_version`。任务恢复后只能使用新鲜观测。原先的未派发任务保留失败原因；后续任务可使用 `parent_task_id`、`parent_event_id` 关联原故障。[公开入口和事件查询](../agent/README.md)

### 如何更新、撤下和回滚

保存通过验收的定义及样本。下面的命令各自对应不同的维护动作，先核对运行目录的当前版本。如果替换影响了在途任务，Agent 会保留原绑定；对不存在或不同的版本退避，耗尽预算后保留任务和故障原因。恢复旧定义以后，再按任务状态及 `can_retry` 决定是否续办。

| 维护动作 | 对照版本和预期结果 |
|---|---|
| 更新 | 新候选与独立样本放在工作目录，如 `next.json`、`next-cases.json`。执行 `docker @publishArgs $image $cli publish /work/next.json --cases /work/next-cases.json --registry /registry --expect $v1`。成功后从回执取得新的 `version` 作为 `$v2`。 |
| 恢复到 v1 | 若当前是 v2，执行 `docker @publishArgs $image $cli publish /work/definition.json --cases /work/cases.json --registry /registry --expect $v2`。同样的规范化定义会恢复原摘要。 |
| 撤下当前 v1 | 执行 `docker @publishArgs $image $cli remove $toolId --registry /registry --expect $v1`。随后检查公开目录和历史任务，旧回执仍应可读。 |
| 撤下后重装 | 确认目录缺少该工具，以原定义和样本 `publish ... --expect absent`。归档于 `plugins/derived/.history/<id>/<版本>.json` 的定义可用于找回原配方；独立样本随发布记录保存。 |

## 启用可选 profile，让系统发现缺口

在已启动的仿真栈中执行以下命令。示例使用本地 Ollama；`EDGE_OLLAMA_URL`、`EDGE_OLLAMA_MODEL` 可改为作者容器能访问的 Ollama 服务和实际模型名。作者向控制台发起两个只读请求，先从 `/api/events?kind=agent.blocked` 读归档，再从 `/agent/tasks?id=...` 取原任务。它只为 `mission_agent/tasks` 的原生事件工作，核对任务 ID、trace、终态事件、错误码和未派发状态，通过后向模型提交需求。模型可决定当前声明语言无法表达这项需求。[监听器实现](../client/plugin_author_watch.py) · [仿真服务配置](../compose.yaml)

```powershell
docker compose --profile model up -d model
docker compose --profile model exec -T model ollama list
docker compose --profile plugin-author up -d --no-deps plugin-author
docker compose --profile plugin-author logs --tail 20 plugin-author
```

进程从保留下来的归档开始消费。已有断点时从断点继续。每轮最多导入 32 条并处理 32 条已到重试时间的事件；旧积压可能需要多轮。日志中的 `proposed` 是本轮生成的候选数，`declined` 是终态拒绝数，`deferred` 是等待网络恢复的项数，`recovered` 是发现回执已落盘、补记消费状态的项数。首次进程尚未写出决定时，`decisions/` 目录可能尚未创建。

Compose 的 `author-proposals` 命名卷挂载到 `/proposals`。`decisions/*.json` 保存候选或拒绝，`watch.sqlite` 保存归档副本、游标和逐事件退避状态。决定用 `(source.archive_id, source.seq)` 定位原始归档行，用 `source.task_id/trace_id/event_id` 定位任务。`status=proposed` 含 `definition` 和 `model`；`status=declined` 含 `reason`，若已取得可解析的模型判断还会保留模型记录。提案 ID 形如 `derived.task_<32位摘要>`。

```powershell
$decisions = (New-Item -ItemType Directory -Force (Join-Path $work 'watch-decisions')).FullName
docker compose --profile plugin-author cp plugin-author:/proposals/decisions/. $decisions
if ($LASTEXITCODE) { throw '尚未取得候选目录，请查看监听器日志' }
Get-ChildItem $decisions -Filter '*.json' | ForEach-Object {
    $d = Get-Content -Raw -Encoding utf8 $_.FullName | ConvertFrom-Json
    [pscustomobject]@{ File = $_.Name; Status = $d.status; Task = $d.source.task_id
        Event = $d.source.event_id; Request = $d.model.request; Reason = $d.reason }
} | Format-List
```

挑选需要验收的文件后，在同一 PowerShell 会话执行下段命令。把 `definition` 单独导出，保留收据原件。检查语义与当前工具目录、为这条请求准备独立样本，再执行前文的 `check` 和 `publish` 命令。即使发布成功，原任务仍保留其失败历史；如需再执行，提交有来源关联的新任务。

```powershell
$decisionName = Read-Host '输入上面选中的候选文件名'
$decision = Get-Content -Raw -Encoding utf8 (Join-Path $decisions $decisionName) | ConvertFrom-Json
if ($decision.status -ne 'proposed') { throw "该决定没有候选。$($decision.reason)" }
$toolId = $decision.definition.id
$decision.definition | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8NoBOM (Join-Path $work 'definition.json')
```

**停机、重试与留存。** `docker compose --profile plugin-author stop plugin-author` 只停止作者。复用同一个命名卷重启会跳过已经记录的事件。网络故障的退避从 1 秒倍增到 60 秒；模型明确拒绝、任务不存在或已经派发会保存终态。模型已答复、回执还没写盘时进程中止，恢复后可能再次请求模型。候选落盘后、SQLite 尚未记账就中止，恢复时复用原件。同一个工作目录同一时间只允许一个消费者。

只想处理一轮时，先停止常驻作者，再用相同卷执行下面的命令。`--once` 的计数只描述这一轮；有网络暂缓时退出码为 2，归档不可访问会以错误退出。离线期间若控制台归档的裁剪水位越过游标，`watch.sqlite` 的 `retention_notices` 会记录该区间。控制台默认的 7 天、10,080 条上限针对普通遥测采样，任务故障受独立的留存规则保护。裁剪提示本身无法断定某条规划故障已丢失。候选和作者游标没有自动过期策略，`stop` 后仍保留；删除命名卷会同时删除其断点与候选，应先导出需要的决定。[控制台裁剪逻辑](../console/server/history.py) · [归档复制与断点](../client/event_sink.py)

```powershell
docker compose --profile plugin-author stop plugin-author
docker compose --profile plugin-author run --rm --no-deps plugin-author --url http://console:8080 --out-dir /proposals --ollama-url http://model:11434 --model qwen2.5:1.5b --once
```

作者镜像以只读根文件系统运行，输出写入候选卷，临时文件可写入 `/tmp`。这个进程没有挂载正式插件目录、宿主 Docker socket 或设备，代码只读取控制台和调用模型。控制台本身仍提供任务提交 API；若要在多个租户间强制只读，需要在网络或认证层约束该服务身份。

## 分开验证模型、发布与 ROS

纯函数与任务账本用例验证字段、单位、无效值及绑定。黑盒使用独立 Docker Compose 项目和 Gazebo 的世界位姿，向 Agent 的真实 ROS 订阅端发送可控的里程计。它先观察缺口，发布后核实 −0.5 m/s 得到 −1.8 km/h，再注入断流、更新定义、恢复原版本、撤下和重读旧任务，同时比较 Agent 启动时间。可以先用固定输入复测，再用真正的 Ollama 作者验证同一条链路。宿主的 Python 只负责编排 Docker。

```powershell
python tests/test_derived_tools.py -v
python tests/test_author_tool.py -v
python tests/test_plugin_author_watch.py -v
python tests/system/derived_tool_acceptance.py --image $image --out "$work/fixed-run.json"
python tests/system/derived_tool_acceptance.py --image $image --model-network $modelNetwork --out "$work/model-run.json"
python tests/system/derived_tool_acceptance.py --image $image --watch-with-model --model-network $modelNetwork --out "$work/watcher-run.json"
```

两条带 `--model-network` 的命令都使用实际 Ollama。第一条显式导出阻断任务并调用作者，第二条让独立订阅者在归档中发现阻断、产生候选，再断开其模型网络重启，检查零重复决策。无模型路径检验发布与运行行为；`--draft` 可重放预先保存的草案。ROS 路径使用合成里程计和只读派生操作，结论适用于该仿真、模型和测试输入。真机标定、其他模型准确率、设备动作授权及代码安装需要独立验收。[黑盒实现](../tests/system/derived_tool_acceptance.py)

## 给下一位实现通用插件的开发者

时间戳审查是一个只需软件的代码型插件。对 `[100,110,110,95,110,140]`，每个已经出现过的值再次出现算一次重复，所以重复 2 次。按收到顺序比较，相邻时间回拨 1 次，最大正向间隔为 30 毫秒。只用里程计字段换算无法表达这个算法。coding agent 可以按现有 HTTP 工具契约编写独立提供者并注册清单。[样板源码、清单及样本](../examples/timestamp-audit/) · [通用契约探针](../tests/system/provider_contract.py)

```powershell
python tests/system/verify_sample_provider.py --out "$work/timestamp-contract.json"
python tests/system/verify_sample_provider.py --image $image --out "$work/timestamp-agent.json"
```

第一条命令在宿主回环地址临时启动仓库内的固定样板，并运行 `cases.json` 中的 8 组输入。报告中的 `cases[].attempts` 记录同一 `operation_id` 两次调用的 HTTP 状态及回执，校验成功证据、业务错误码和重投一致性。新增提供者时，把它部署在测试地址，直接运行 `python tests/system/provider_contract.py --manifest <tool.json> --cases <cases.json> --url <提供者测试地址/invoke> --out <报告.json>`。探针只接受 `effect=read`，对真实设备的效果需另行验证。

第二条命令在私有 Docker 网络启动 Agent 和只读提供者，先提交同一句自然语言并观察工具缺席，再发布清单、核对任务证据、重投触发、撤下清单和回查旧任务。报告可检查 `before`、`after`、`evidence`、`event_kinds`、`replay_same_task` 和 `agent_restarted`。这条链路用本地确定性规划器匹配清单的 `utterances`，无须 Ollama 或机器人。样板的测试范围包括 HTTP 回执重投和清单热挂载，跨提供者重启的持久化能力须依据具体实现另外验证。[自动化入口](../tests/system/verify_sample_provider.py)

若要让机器人在发现困难后自行交付一个新的代码型提供者，下一阶段可以按以下顺序实现。

1. 给作者提供已安装能力、版本、现有接口、失败事件及代表性输入；要求交付候选源码、依赖、所需权限、独立测试及对应工件。作者可以生成实现，发布决定由部署策略做出。
2. 在隔离构建环境中运行白盒、契约和仿真黑盒。核对输出语义、超时、资源开销、进程崩溃和不兼容输入，固化通过验收的镜像摘要、契约版本、许可证与来源。
3. 注册前进行健康探测，先允许新任务选择新版本。在途任务按原绑定对账，待其完成后停旧提供者。需要访问新硬件或扩大授权时，把需要的操作、原因和证据交给实际有权批准的主体。
4. 记录从来源任务到插件工件、版本、执行证据和撤回操作的关联。热替换能否成立，要看旧提供者的执行状态是否可查询、占用资源能否有序交接。机体级动作还须完成现场验收。

上述代码型插件的自动构建、接纳、分阶段启动及审批是后续开发项目。当前发布者只处理这里列出的声明；通用 HTTP 代码提供者仍由部署方独立启动。[生命周期与人工介入规划](specs/SDD-004-single-edge-lifecycle.md)
