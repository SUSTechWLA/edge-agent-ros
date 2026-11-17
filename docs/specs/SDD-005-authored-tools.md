# SDD-005 · 让 AI 定义、验证并热加载只读工具

## 背景与价值

用户要求“用千米每小时报告本机前向速度”。机器人已经有带时间戳的里程计，现有 `status` 返回米每秒。再增加一个驱动或修改运动内核都会扩大改动范围。同样的需求也可能涉及机器人没有的传感器，例如电池温度，此时系统需要识别缺口并保留依据。

## 设计与契约

保留两个插件入口。通用 HTTP 提供者可以实现新的软件算法、设备和长程技能，独立部署，按操作 ID 返回可核对的结果和证据。带副作用的长程能力还需要设计对账、取消与资源交接。对仅使用本机里程计的换算及阈值判断，AI 可提出小型 JSON 定义。只读解释器校验字段、单位和运算。定义中没有程序、端点或设备控制权限。内容摘要作为版本，发布时对照独立输入样本和预期结果。普通任务只读取已发布目录。发布方可以是有权限的本机程序或 CI。

## 验收与证据

- AC-1 模型根据实际需求提出草案；不支持的输入要能表达缺口。草案本身不改变运行能力。字段错误、越界运算、错误单位和不符合验收样本的结果不能发布。
- AC-2 在 Agent 持续运行时发布，新语言任务能使用新工具。结果携带采样来源、采样时间、换算前后值和定义版本；里程计失效时沿用现有的任务恢复语义。
- AC-3 更新/撤下时校验调用方期望的旧版本。保存原定义可回滚。已规划任务不能悄悄转到另一版，同一 ID/版本的旧自定义步骤缺少可证明的绑定时要进入待核查。
- AC-4 本地复测包含纯函数、实际任务账本、并发发布和独立 Docker 黑盒。留存真实模型草案及热点切换证据；独立区别模型生成、纯函数验收和机器人执行。
- AC-5 可选作者进程从归档自动认领未派发任务的规划缺口，按事件保存模型决定。重启后续读，已处理事件不重复产出候选。机器人在作者离线时继续运行。候选进程不挂载生效目录、执行器或 Docker socket。
- AC-6 独立软件提供者允许 coding agent 编写新的算法。以时间戳审查为例，使用已有任务 API 和工具清单，核对成功、坏输入、回执身份和重投结果。部署代码型提供者的权限独立于任务规划。

## 运行与发布职责

| 角色 | 输入、输出与权限 |
|---|---|
| 任务 Agent | 从正式目录读取能力，规划并绑定工具，把采样和结果写回原任务。Agent 的插件挂载为只读。 |
| 候选作者 | 可用 `author_tool.py draft --from-task` 读取导出的原任务，也可启用仿真 Compose 的 `plugin-author` profile。两者复用相同的验证与生成函数。监听器只向控制台发 GET，向 Ollama 请求结构化候选，将候选或拒绝写入独立卷。 |
| 发布者 | 将 `proposed` 记录内的 `definition` 取出，用与实现独立的 2–256 组样本执行 `check`。首次发布使用 `publish --expect absent`，更新使用 `publish --expect <旧摘要>`。撤下和回滚同样核对当前完整版本。正式定义目录为 `plugins/derived/`，作者服务没有此目录、设备或 Docker socket 的挂载。 |
| 代码型提供者的部署方 | 编写并独立启动 HTTP 服务。`effect=read` 是工具清单的效果声明，服务本身的网络、文件和设备访问仍由部署约束。通用探针与样板验收不授予安装代码或控制机器人的权限。 |

**作者认领规则。** 从 `/api/events?kind=agent.blocked` 读取 `mission_agent/tasks` 的原生记录，通过 `/agent/tasks?id=<id>` 核对原任务、trace、终态事件和诊断。输入必须为 `BLOCKED`、空步骤、完整的未派发事件，错误码只接受 `planner.capability_missing` 或 `planner.not_configured`。后者表示配置的规划路径未处理输入，发布者仍要查现有能力是否已能交付。已有执行步骤、原任务不存在、来源不符以及模型确定无法表达的输入都应留拒绝原因。

**启动与留存。** 已运行控制台并配置可用 Ollama 时，执行 `docker compose --profile plugin-author up -d --no-deps plugin-author`。日志提供每轮 `proposed/declined/recovered/deferred` 计数。`author-proposals` 命名卷中，`/proposals/decisions/*.json` 记录 `source.archive_id/seq/task_id/trace_id/event_id/reason_code` 及 `status`，成功记录带 `definition/model`，拒绝记录带 `reason`。`/proposals/watch.sqlite` 保存源游标、工作项、退避及 `retention_notices`。同一输出目录只允许一个消费进程；每轮最多处理 32 条，网络异常从 1 秒倍增退避到 60 秒，单项失败后其他到期项可继续。控制台普通遥测有保留窗口，裁剪水位超过游标时记录缺口。作者卷没有自动清理，停止服务保留断点和决定，删除卷须先备份。若模型返回后回执尚未写盘就中断，允许再次请求模型；已有回执的事件应直接恢复消费状态。[启动、查看、单轮复查和停止命令](../plugin-authoring.md)

**自动验收入口。** 以下命令从项目根目录运行，宿主 Python 3 负责驱动本地测试。`<镜像>` 使用已构建的候选镜像，`<模型网络>` 是可以解析 `model:11434` 的 Docker 网络。

| 目标 | 命令与证据 |
|---|---|
| 检验订阅与恢复 | `python tests/test_plugin_author_watch.py -v`。覆盖归档来源、已派发任务、模型拒绝、恢复去重、另一消费者争用、503 后重试与裁剪通知。 |
| 检验真实模型与 ROS | `python tests/system/derived_tool_acceptance.py --image <镜像> --watch-with-model --model-network <模型网络> --out .ci-results/plugin-authoring/watcher.json`。隔离栈检查真实订阅、提案、独立发布、传感器数值与版本绑定。 |
| 检验代码型样板 | `python tests/system/verify_sample_provider.py --out .ci-results/plugin-authoring/timestamp-contract.json` 在回环地址测试 8 组样本和同操作重投。加 `--image <镜像>` 并换一个报告名，在私有 Docker 网络验证自然语言、工具上线撤下、任务证据与原任务回查。两种报告回答不同问题。[样板与领域样本](../../examples/timestamp-audit/) |
| 验收其他只读提供者 | `python tests/system/provider_contract.py --manifest <tool.json> --cases <cases.json> --url <测试地址/invoke> --out <报告.json>`。使用部署方指定的隔离端点和独立样本，核对 `operation_id`、终态、证据、稳定错误码与重复提交。 |

## 迁移与回退

沿用工具清单已有的挂载目录，新增 `derived/` 子目录。已有 HTTP 插件及其接口继续工作。部署本版镜像后，同类工具的安装、更新和卸载无需重启 Agent。回退单个定义使用上一个内容摘要对应的文件和独立样本；不可确定的旧操作保留原任务以供核查。

## 变更记录

2026-11-17 基于 `5294dd5` 登记作者、发布者、运行者三者的接口和上述验收条件。
