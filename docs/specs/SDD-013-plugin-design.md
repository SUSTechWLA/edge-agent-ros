# SDD-013 · 从作者拒绝记录生成可交付的插件设计草案

## 背景与价值

插件作者会拒绝超出现有声明语言的任务。若用户的目标确实需要新算法或适配器，开发者应能接续同一任务的证据，选择现有插件接口，明确待决问题，再实施、验收和部署。

## 设计与契约

离线作者 CLI 接收从受信 Console 导出的单条 `journal:plugin.author` 拒绝事件和对应的私有决定。逐项校验原始文件摘要、收据编号、来源归档/序号、原生故障事件父引用、task、trace、拒绝原因及决定中已有的模型请求记录。只接受原生规划能力缺口、作者已验证目标且无法生成定义的决定。私有请求用于设计，公开日志只留来源标识、摘要、状态及稳定原因码。

默认生成供 Coding Agent 使用的设计输入，显式配置本地 Ollama 后可生成带语义的 Tool、Service 或事件消费者设计建议。结构校验限制类型、字段、长度和数量。建议不具有发布权限。工件包含实际接口和实现入口、所需依赖与权限、测试、回退以及待验证项。每次调用使用请求 ID 关联开始、生成、拒绝或失败事件，独立私有目录保存原始工件与版本。部署者可随时启停该 CLI。

## 验收与证据

- AC-1 由真实作者格式的拒绝决定和关联公开事件生成草案，输入摘要、原计划缺口、task、trace、事件父链可以复查。
- AC-2 来源错误、改过的私有文件、无模型请求记录、序号或父引用冲突均拒绝生成，公开事件不泄漏任务原文。
- AC-3 固定结构的模型建议记录模型名、提示和响应身份。模型超时、损坏输出、额外字段、未知插件类型等保留诊断。
- AC-4 使用独立工作目录，无 ROS、注册目录或设备权限即可运行。草案与公开事件关联，产生草案不派发任务或安装插件。

## 迁移与回退

CLI 使用独立的私有输出目录、独立模块事件源及已有模型服务。上线前仅分配导出文件的读权限以及两个输出位置的写权限。停用 CLI 即可停止新设计工作，按收据和请求 ID 保留审计、私有草案。原始来源不完整时，需重新取得可信作者证据。

### 本机运行和接入

1. 从可信 Console 的 `GET /api/events?source=journal%3Aplugin.author&kind=plugin.author.declined&task_id=<任务ID>` 取单条 `code=author.proposal_rejected` 事件对象，将原对象保存在私有工作目录。作者的 `decisions/<data.receipt_id>.json` 使用原始文件字节。两个输入来自本机受信任的导出渠道，摘要用于发现错配或更改，两份被一起伪造的文件不提供来源认证。
2. 在项目根目录以 Python 3 运行下方命令。示例中的本地文件名为 `event.json`、`decision.json`。按需传入 `--ollama-url <本地受信模型地址> --model <模型名>`，模型会收到作者已记录的目标和项目的公开接口定义。默认模式生成带原始目标的开发交接简报。个别作者流程留存的是经脱敏或裁剪的目标，如数列任务只留操作描述，需从授权数据源补充数值样本。
3. CLI 返回 `path`、`design_id`、`draft_sha256`、`request_id`。Coding Agent 从私有草案的 `development` 读取项目入口、独立验收和回退要求，再审查 `model_design.proposal` 建议，补齐依赖、权限和独立输入/期望。开发者经 SDD 验收后走现有插件发布入口。

```sh
python client/plugin_design.py --event .ci-results/design-input/event.json --decision .ci-results/design-input/decision.json --out-dir .ci-results/design-private --journal .ci-results/design-public/plugin.design/module-events.sqlite
```

若需 Console 汇集证据，把公开源目录 `plugin.design/` 提供给 Console 的 `/var/lib/edge-module-sources/plugin.design/` 只读挂载。私有草案和私有输入仅提供给获准的开发者、作者进程。公开事件中的 `call_id` 对应一次 CLI 调用；成功链为原始 `agent.blocked`、`plugin.author.declined`、`plugin.design.requested`、`plugin.design.drafted`。输入未通过归属校验时，拒绝事件使用本次请求自身的 trace。模型失败事件保留输入摘要，收到畸形响应时也保留响应摘要。

匹配的来源、作者实现与模型配置复用同一私有工件。生成成功后重复调用会复核归档中的工件摘要，遇到丢失或修改会留下 `draft_missing`、`draft_conflict`。在写草案后、写完成事件前退出的情况，重新调用可继续登记。所有模型建议都按 `unverified` 交付；明显的占位字段列在 `unresolved_fields`。字段格式和占位检查只能说明结构情况，语义正确性需要独立验收。

成功与拒绝响应均返回 `request_id` 和终态 `event_id`，可在公共归档中直接定位本次调用。如果事件源本身无法打开或写入，CLI 以非零状态和 `design.journal_unavailable` 返回；此时现场需保留进程退出记录并检查源数据库。超出模型响应体积上限时只记录已读前缀的长度及摘要。

## 变更记录

2026-11-18 在实现前登记 AC-1 至 AC-4。开发基线为 `50b9344`。测试计划覆盖作者真实收据、独立输入、故障注入、真实模型可用时的本地调用及原生事件归档协议。

2026-11-18 AC-1 至 AC-4。实现位于 [client/plugin_design.py](../../client/plugin_design.py)。`python tests/test_plugin_design.py -v` 3 项通过，覆盖实际作者私有回执、来源、父引用与 trace 错配、原始字节摘要、缺目标、模型响应额外字段、连接失败、缓存复用、草案篡改/丢失及公共事件脱敏。`python tests/system/plugin_design_acceptance.py --cache edge-agent-ros_models --out .ci-results/system/sdd013-design-model.json` 在隔离 Docker 通过。原生 Agent 对 PNG 尺寸请求报告能力缺口，原生作者收到可重复的超范围答复并拒绝，独立作者调用真实 `qwen2.5:1.5b` 起草新插件。Console 查到归档 #4 → #7 → #8 → #9 的父引用和相同 trace，原任务保持 BLOCKED、派发零步。完整样本包含源码与模型摘要、草案及四条原事件。[机器可读的隔离测试样本](../evidence/plugin-design-sample.json)

模型建议的质量单独评估。首轮建议虚构 `sensor_msgs/ImageDimension`；收紧模型输入并将测试样本和期望分列后，模型正确选择软件 Tool，仍把输出写成 `None`，未给 PNG 的已知像素尺寸或有效回退步骤。最终记录保留原建议并标出未解决的 `outputs`。例如可用一张已知 3 × 2 像素的 PNG，要求返回宽 3、高 2，并检查损坏文件的错误回执。草案不授予读取任意路径、运行代码或发布能力，此次实验验证的是提案、审计与隔离，插件代码尚需由后续开发和测试证明。
