# 从能力缺口到插件候选的单机运行手册

用户下发本机暂时无法完成的任务时，`plugin-author` 检查是否能用现有声明表达目标。有依据的拒绝决定会触发 `plugin-design` 生成私有设计。设计选择了现场登记的 `text-transform` 且输入满足编码条件时，`software-author` 可再生成一个受限文本函数及其工具清单。**这条链的自动交付物是待验收的候选包。** 部署者用自己的测试样本核验并批准具体摘要后，才能向 Agent 注册这个工具。原用户任务、实际续办与最终结果各有独立记录。[设计作者](specs/SDD-017-plugin-design-watch.md) · [编码作者](specs/SDD-019-event-driven-software-author.md)

## 安装与启停

此次工作站已保存验证用标签 `edge-agent-ros:event-evolution-20b212a` 和 `edge-agent-ros/hardware:event-evolution-20b212a`。PowerShell 仿真部署时可设置 `$env:EDGE_IMAGE='edge-agent-ros:event-evolution-20b212a'`。首次启用的新消费者会从可见历史开始查收，已有私有工作卷的消费者使用原游标。启用前可用下文的拒绝事实查询估计历史待处理量。

从项目根目录执行。先按[部署手册](deployment.md)构建对应本次代码的镜像并启动 Agent 和 Console。若机上保留了多个候选镜像，启动前将 `EDGE_IMAGE`（仿真）或 `EDGE_HARDWARE_IMAGE`（板端）设为已验收的标签。两份 Compose 都使用 Docker 卷子目录挂载，Engine 和 Compose 需要支持 `volume.subpath`。

**仿真或工作站。** 默认模型地址为同一 Compose 网络内的 `http://model:11434`，默认模型为 `qwen2.5:1.5b`。使用已缓存该模型的 Ollama 卷时，以下命令只启动列出的服务及其初始化器。

```sh
docker compose --profile model up -d model
docker compose exec model ollama list
docker compose --profile plugin-author --profile plugin-design --profile software-author up -d plugin-author plugin-design software-author
docker compose ps -a plugin-author plugin-design-init plugin-design software-author-init software-author
```

**Linux 板端。** 在部署机上准备可达的 Ollama 兼容服务，默认访问 `127.0.0.1:11434`。三个作者可分别用 `EDGE_OLLAMA_URL`、`EDGE_DESIGN_MODEL_URL`、`EDGE_CODE_MODEL_URL` 指定模型服务，对应模型名为 `EDGE_OLLAMA_MODEL`、`EDGE_DESIGN_MODEL`、`EDGE_CODE_MODEL`。设计服务会向该端点提交私有目标和接口快照，编码服务会提交目标和已选设计，应选择有权读取这些材料的服务。

```sh
docker compose -f compose.hardware.yaml --profile plugin-author --profile plugin-design --profile software-author up -d plugin-author plugin-design software-author
docker compose -f compose.hardware.yaml ps -a plugin-design-init plugin-design software-author-init software-author
```

两个 `-init` 服务各自执行一次固定的 `mkdir -p` 后正常退出，常驻消费者显示为运行中。首次安装需要保留初始化依赖。Agent、Console、模型的启动和就绪状态由各自服务管理。现场已有 `plugin-author` 时，也可以在第一组命令中只选择新需要的 `plugin-design` 或 `software-author`。

仿真暂停自动编码可执行 `docker compose stop software-author`，继续处理使用 `docker compose start software-author`。暂停设计交接对应 `docker compose stop plugin-design`。板端在 `compose` 后加 `-f compose.hardware.yaml`。保留原工程名与数据卷才能续接已有游标、重试预算和历史工件。模型或只读目录挂载变更时用 `up -d <服务名>` 按配置重新创建相应消费者。已注册工具的撤回由受管发布入口独立执行。

## 追查一次交付

默认 Console 为 `http://127.0.0.1:8765`。在浏览器打开下列查询。可用 URL 编码后的实际任务 ID 或 trace 替换示例参数，`limit` 和 `after` 用于分页；按返回的 `archive_id` 区分归档世代。

| 想知道什么 | 可直接使用的本机查询 |
|---|---|
| 作者是否有一份可信的拒绝决定 | [查看作者的拒绝事实](http://127.0.0.1:8765/api/events?source=journal%3Aplugin.author&kind=plugin.author.declined&limit=30) |
| 设计是否已落盘 | [查看设计及其摘要](http://127.0.0.1:8765/api/events?source=journal%3Aplugin.design&kind=plugin.design.drafted&limit=30) |
| 哪些设计需要人工复核 | [查看设计介入记录](http://127.0.0.1:8765/api/events?source=journal%3Aplugin.design&kind=plugin.design_watch.intervention_required&limit=30) |
| 编码插件看到了什么，决定生成、跳过还是上报 | [查看编码作者源](http://127.0.0.1:8765/api/events?source=journal%3Aplugin.software_author&limit=50) |
| 从同一任务查触发、尝试和结果 | [按示例任务查询](http://127.0.0.1:8765/api/events?task_id=png-dimensions&limit=100) |

先核对 `source`、`event_id`、`parent_event_id` 和 `task_id`。在设计完成事件读取 `data.design_id`、`data.draft_sha256`、`data.contracts_sha256`；候选交付时再读包及版本摘要。查看 `data.reason` 和 `steps` 可定位错误及处置步骤。Console 离线期间，消费者在自己的事件源和待投递表留存状态，恢复后还需查覆盖水位、缺口和有效页是否重新提交。[事件语义与覆盖范围](module-eventbus.md)

| 内容 | 卷、所有者与容器内路径 |
|---|---|
| 决定与方案 | `plugin-author` 拥有 `author-proposals` 中的 `decisions/`。设计作者只读挂载这个子目录为 `/decisions`，从只读 `/contracts` 读取可选接口。 |
| 设计与工作状态 | `plugin-design` 拥有 `design-private`，设计位于 `/private/drafts/<design_id>.json`。其工作数据库、游标和尝试次数位于同卷 `/private/intake.sqlite`。编码作者只读挂载其中的 `drafts/` 为 `/drafts`。 |
| 代码候选 | `software-author` 拥有 `software-author-private`。本地包位于 `/private/packages/<design_id>/`，包含受限源码、工具清单和来源材料。该消费者读取 `/contract.json` 校验允许的接口。 |
| 公共事件 | 设计作者和编码作者分别将 `module-sources` 中自己的子目录挂载到 `/journal`，事件库为 `/journal/module-events.sqlite`。Console 读取已提交的事实。模型交互可按请求、回复的摘要核对，私有草案保存模型给出的设计，候选包保存获选的工具清单和源码。 |

Docker 将工程名作为默认卷名前缀。向开发者交付来源材料时，先用 `docker compose cp plugin-design:/private/drafts/<design_id>.json <私有目录>` 和 `docker compose cp software-author:/private/packages/<design_id> <私有目录>` 取出必要的文件，再按公开摘要核对原始字节。注册前需在独立、受限的提供者容器内执行外部样本。通过后用 `software_release.py publish` 指定批准摘要、期望旧版和幂等请求身份；失败或版本不符时保持现行版本。[独立验收、批准与撤回命令](../examples/software-provider/README.md)

在开发机同时验证两份部署清单，可执行 `python tests/system/evolution_mounts.py --image <仿真镜像> --hardware-image <板端镜像的本机版本> --out .ci-results/system/evolution-mounts.json`。测试使用独立的临时工程冷启动和停启作者，实测卷的读写范围，保留已有部署。板端 Compose 使用主机网络，实际模型与归档服务的网络访问权限还取决于现场配置。上板前还需实测目标芯片、存储和传感器。

例如，在文本工具黑盒实验里，`word-gap` 提出开发能力的目标，安装前和安装后都能查到其原始受阻状态。新工具获准后，`word-active` 才携带具体正文执行，`word-empty` 验证空输入。要判断是否自动续办了最初任务，应核对后续任务与原请求的关联和实际输入。SDD-012 的预授权数列工具已有这种续办；SDD-019 的软件候选交付由独立客户端显式发起后续调用。[文本验收程序](../tests/system/software_provider_acceptance.py) · [数列工具续办](specs/SDD-012-authored-software-tools.md)

## 失败怎样处置

编码阶段发生 `candidate_rejected` 时，在下一条 `model_requested` 的数据中检查 `feedback_event_id`、`feedback_attempt` 和 `feedback_sha256`。它们指向上一次静态校验，原表达式与诊断保存在该作者的私有工作状态，并仅向同一受信设计的后续模型请求提供。输出 token 到上限、读网络超时或根本没有可校验表达式时，应分别查询预算和模型连接。独立验收方持有的样本与期望不会进入这条反馈。

| 看到的证据 | 现场处置 |
|---|---|
| `subscription.blocked` 或长时间没有新工作 | 查该消费者的 `source` 与原因码、Console 地址及其归档身份。恢复后核对 `subscription.restored` 和下一页游标。归档重建或裁剪越过检查点时，调出原作者账本与备份核实期间覆盖。 |
| 当次尝试因模型离线或输出不合规而重试 | 查同一操作的尝试数和预算，核实模型地址、版本、是否支持结构化输出。工作已达介入状态时，保留原事实，再按对应 SDD 的人工流程提交复核。 |
| 设计可读但编码阶段跳过草案，或提示来源/摘要/契约不匹配 | 先比对公开设计摘要与原始私有草案，再比对旧快照、`EDGE_CODE_CONTRACT` 和当前部署身份。其他类型的设计交给对应开发流程。有未解决的输入、回退或验收要求时，由研发者补齐并保留新版本与旧版本的关联。 |
| 源库、工作卷或候选包丢失，归档仍显示曾经完成 | 暂停相应消费者，备份可读取的原工件，用原设计、源 ID 与摘要逐项对账。恢复到一致版本后再启用；关键工件未知时保留人工项。涉及真实设备的受理或副作用需另查执行器账本。 |
