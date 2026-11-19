# 插件描述符 v2 与发布协议

本页是 SDD-045 的接口提案。举例时用 `session.default` 表示提供会话查询的实现，用 `session.read` 表示它提供的服务。一次发布可以有多项能力，只有作为 Tool 导出的操作才进入模型目录。以下命令和字段属于 P1–P3 的开发目标。

## 清单格式

开发包使用 `<包目录>/plugin.json` 或 `<id>.plugin.json`。受管清单放在独立的 v2 目录，例如 `plugins/v2/<id>/plugin.json`；旧 Tool 读取器扫描顶层 `*.json`，旧 Service 读取器按文件名校验，两者都不应误读新清单。字段名区分大小写，拒绝重复键、额外未声明字段、非有限数和超限输入。P1 建议沿用普通 Tool 的 128 KiB 清单上限；schema 与实现另行按摘要寻址。

| 字段 | 值、校验与消费者 |
|---|---|
| `schema_version, id, version` | 原生 v2 必须为整数 2、稳定命名 ID、严格 SemVer。包括 prerelease/build 解析。同一 ID 与版本只对应一个内容摘要。旧 Tool 的版本字符串与 derived 的哈希走兼容读取。 |
| `content_digest, provenance` | SHA-256，来源至少含 author、source_commit、代码工件摘要或 OCI image_digest。process 必须绑定镜像 digest（多架构时另留平台 manifest）；inproc 绑定已审核模块包摘要与宿主镜像。author 是声明，安装身份由外部授权凭据确定。 |
| `kind, class, runtime` | kind 取 tool、service、consumer、view，class 取 soft、physical；runtime 取 inproc、process。physical 必须 process；第三方或 AI 自编的任意 Python 只可走受控进程，禁止作为 inproc 代码加载。 |
| `provides, requires` | 数组元素均含 name、type、schema_ref；schema_ref 在离线、只读、随发布固定的契约目录解析，绑定实际 schema 摘要。type 区分 tool、service、event、view。空数组显式允许，例如纯订阅者零导出。禁止循环、重复声明、无法解析的引用。每个未固定版本的服务名只有一个可接新请求的提供者。 |
| `parameters, outcomes, config_schema` | 分别是调用输入、结果、配置的 schema 或指向固定 schema 的引用，保留 required、类型、范围与单位。多 Tool 按名称给出各自的契约；无输入仍需提供空对象契约。独立验收策略按已有规则叠加。 |
| `effects` | read、idempotent 或 actuation；分组发布时填写最高效果，同时保留各导出自己的契约。actuation 只能用于 physical，仍须提供稳定操作 ID 和幂等对账；“幂等”只说明重复请求如何处理。 |
| `hot_reload, drain_required` | 布尔值；hot_reload 特指代码热更且仅限 soft。所有接受调用或持有订阅、设备的实现都须可排空。进程版本、执行器行为、接口变化均不能伪装成配置热更。 |
| `device_lease, stop_protocol, verifier, operation_ledger` | physical 四项非空，引用具体的资源租约、停止/静默证据契约、独立验收服务及原操作的持久查询接口。verifier 解析到不同部署责任与证据来源；同 ID、自指、别名指向同一实现或共享自报结果都拒绝。纯软件无设备字段用 null；独立验收仍可由现场策略要求。 |
| `entrypoint, mounts, network, resources, health` | process 使用镜像内固定 argv 与私有服务路由，inproc 仅引用已安装审核包中的 apply。挂载指定来源、目标及只读性；网络列出允许的服务与方向；资源含 CPU、内存、并发及关闭时限；健康有操作、期限、最近成功和过期判据。声明只是权限请求；部署方以后文的实际资源核验为准。 |
| `policy` | install、enable 主体与是否需要现场批准。运行端与站点策略取交集；插件不能自授发布身份、扩大设备列表或取消强制验收。密钥只在部署侧按引用绑定。 |

下面是评审用结构样本。尖括号须由构建/站点解析成真实固定值，原样输入校验器应拒绝。既有对外名称和 HTTP 消费协议被保留。

```json
{
  "schema_version": 2, "id": "session.default", "version": "1.0.0",
  "content_digest": "sha256:<release-digest>",
  "provenance": {"author":"edge-agent-ros","source_commit":"<git-commit>","image_digest":"sha256:<platform-manifest>"},
  "kind":"service", "class":"soft", "runtime":"process", "effects":"read",
  "provides":[{"name":"session.read","type":"service","schema_ref":"session/read@1"}],
  "requires":[{"name":"task.public.read","type":"service","schema_ref":"task/public-read@1"}],
  "parameters":{"schema_ref":"session/read-input@1"}, "outcomes":{"schema_ref":"session/document@1"},
  "config_schema":{"schema_ref":"session/config@1"},
  "hot_reload":true, "drain_required":true,
  "device_lease":null, "stop_protocol":null, "verifier":null, "operation_ledger":null,
  "entrypoint":["python3","-m","sessions_plugin.provider"],
  "mounts":[], "network":{"ingress":["session.consumer"],"egress":["task.public.read"]},
  "resources":{"memory_mib":128,"cpu":0.5,"max_inflight":4,"shutdown_ms":5000},
  "health":{"probe":"session.readiness","timeout_ms":1000,"ttl_ms":5000},
  "policy":{"install":["release-admin"],"enable":["site-operator"],"site_approval":false}
}
```

样本中 `task.public.read` 和 `session.readiness` 是 **拟建**的受限读接口及就绪探测。现有 Session 通过 Console 读取公开事实。发布前须先能解析 schema、部署读接口、通过探测。调用任务读接口的网络路径只允许只读方法与已批准路径，不能因共用 Console 端口获得任务下发或重试权限。

## 三种不同的摘要

1. **发布内容 `content_digest`**。先校验清单，再按约定的 canonical JSON（RFC 8785，UTF-8，拒绝重复键）序列化。计算 `sha256(canonical({"descriptor":D,"implementation":I,"schemas":S}))`。D 为仅移除顶层 content_digest 的完整静态清单，I 为实际读取并验明的包或平台镜像摘要，S 为按引用名排序的去重 schema 引用与内容摘要。具体按 [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html) 和 [SemVer 2.0.0](https://semver.org/spec/v2.0.0.html) 处理格式与版本。构建期与加载期必须用同一固定向量互验，不能信任启动者回填的 digest 或可变镜像标签。显式挂载的可执行内容必须计入 I，否则拒载。部署审批票、动态健康和排空状态另存，避免摘要自指或排空改变身份。
2. **发布 CAS 修订**。对每个 ID 的受管活动指针做“若当前修订等于 expect 才改”。指针含接纳状态、激活代次及选定版本；同一 request_id 同内容重放原回执，意图不同报冲突。生命周期事务同时持久保存审计事实或 outbox。恢复时对账，不重新执行已经完成的发布动作。
3. **计划冻结的 binding**。v2 步骤持久存 tool/version/content_digest、输入输出 schema 指纹、所用配置修订及不可变提供者身份，并绑定执行所需的传递依赖和验收器。语义配置变更只进入新计划；可热更的纯观测配置按所属实例记录生效序号。v1 已存的 binding 和验收 pin 按原算法逐字节解释，不能补生成 v2 身份写回旧账本。解析原 binding 只得到原代提供者或明确失败。新活动指针不能覆盖旧计划引用。

## 兼容、分类与隔离

构建先生成实现工件，再生成独立的发布描述符。最终描述符存于受管注册目录，与它引用的镜像或代码包分开放置，避免在镜像中写入该镜像自身的 digest。现场从获准构建记录复核 source_commit → 工件摘要 → content_digest 的对应关系；复制一份自称来自可信作者的 JSON 不能取得同等权限。

v1 的 `plugins/*.json`、`runtime/*.json`、受管 derived 声明保留自己的校验、优先级、原版 version 和 binding。legacy 归一化采用**旁路视图**，记录 native version、分类证据与实现身份来源，未知摘要明确记为未证明。原路径不会被写回。新增 `/agent/plugins` 的 v2 字段走显式版本协商（提案 `?schema=2`），旧查询、模型目录、工具返回等保留既有序列化。切换 v2 原生发布时先证明内容和分类，等旧 pin 排空或保留可重建的老版本路径；迁移单一受管入口后撤去该能力的 v1 写权限。双目录同一导出发生冲突时拒接新绑定。迁移矩阵和黄金样本范围见 [SDD-045](specs/SDD-045-plugin-tracks.md) 与[验收计划](testing/plugin-tracks.md)。

校验器按实际能力提问。写执行器、委托运动、持设备句柄属于 physical；读原始设备数据（即使来自只读挂载或 ROS 原始 Image/Scan）也属于 physical；只对经授权的事实做读算存展示才可判 soft。证据不足按 physical 走全套准入。软件需要 CPU、内存、文件或只读业务服务仍可属 soft；独占的相机、底盘、硬件设备句柄走租约。混合包按最高风险分类，或拆成“设备采集”和“已处理事实查询”两个明确边界的提供者。

加载前检查权限闭包，含依赖、传递委托与挂载，而不止清单文字。soft 进程与执行器网络、设备节点、Docker socket、特权/host 配置及可写控制账本隔离。只读代理只公开允许的方法和资源；通过代理调用能令车运动也属于 actuation。审核过的第一方代码在同一**无硬件权限的 Python 执行进程**内 inproc 运行，Agent 与 Core 通过受控协议协作。不能在已持 ROS 控制句柄的 Python 解释器内靠 Context 对任意代码实现隔离。设备容器按被批准节点、所需最小读写权限与允许的网络对端启动。比较部署生效的容器配置和可达性，差异时拒绝加载。旧系统已批准、固定部署的兼容提供者可继续服务旧任务；不明来源的 v1 清单不能绕过 v2 现场准入获取物理权限。

## 管理面与现场读法

以下是目标命令。旧 `--kind runtime|derived` 仍由既有管理器处理。各子命令通过认证的操作者入口执行；审计事件和 LLM 输出均无发布权限。

| 命令 | 读写、前置条件与可观察回执 |
|---|---|
| `plugin-admin validate FILE --kind v2` | 离线只读。解析字段与类型、引用、摘要、class 与 effects；不执行入口。返回 ID、version、实际内容摘要或字段级错误。 |
| `plugin-admin check FILE --kind v2 --root REGISTRY` | 还需校验现场依赖图与版本、运维权限、资源和冲突，列出未能在线确认的条件；检查本身不取得 lease 或启动进程。 |
| `plugin-admin publish FILE --kind v2 --expect REV --request-id R` | soft 可在运行时发布，实装候选通过隔离测试后才激活。physical 仅部署通道，核实批准、固定镜像、只读预检与版本交接。CLI 参数不能伪造部署通道身份。输出旧/新修订、接纳状态及审计关联。 |
| `plugin-admin drain ID --kind v2 --expect REV --request-id R` | 原子关新 pin 并返回冻结引用、在途操作、待验收数与旧版本。可重查；排空超时保留引用及原操作。 |
| `plugin-admin remove ID --kind v2 --expect REV --request-id R` | soft 等待冻结引用处理和清理；physical 仅部署交接、验明已停稳且 lease 已释放后才停进程并撤销。保留审计、历史结果与恢复所需的内容工件。 |

提案中的 `GET /agent/plugins?schema=2` 每行含 id、version、kind、class、content_digest、实例代次、lifecycle、availability、health（含来源及最后探测时间）、draining、在途/冻结引用计数与最近错误。目录通过依赖解析与探测成功分别显示；计数未知也要显示。无资格访问设备的调用者只读公开摘要。管理面记录前后状态、版本、digest、binding、操作者、原因与 request_id/trace_id，关联任务时附 task/operation_id。若候选清单无效且无法得到可信版本，事件保留可计算的原始输入摘要并明确 version 未验证；可识别实例的每次状态迁移都带已验证版本与内容摘要。
