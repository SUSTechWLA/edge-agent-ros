# 本地 Qwen 与 DeepSeek Model Adapter

Agent、工具作者、设计作者和编码作者原来都能使用非流式 Ollama chat。这个进程提供同一 HTTP 接口，可以将明确选择 DeepSeek 的请求转换为官方 HTTPS chat。Agent 自己解析并验证计划，ROS 内核继续决定能否执行。默认路由中，`qwen2.5:1.5b` 只调用本地 Ollama，`deepseek-flash` 只调用官方 API。更换模型不需要修改提示、执行器或 Agent 代码。[开发规格](../docs/specs/SDD-021-model-provider.md)

## 在 Docker 中启动

在仓库根目录运行。Compose 将插件目录及路由目录只读挂入 Edge 运行镜像。模型服务与旧的 Agent 使用同一 Compose 工程网络。已有合适运行镜像时，`EDGE_MODEL_ADAPTER_IMAGE` 可指定该镜像标签。

```powershell
docker compose --profile model up -d model
docker compose exec -T model ollama list
docker compose -f compose.yaml -f compose.model-provider.yaml up -d model-provider
Invoke-RestMethod http://127.0.0.1:8134/api/tags
```

本地 Ollama 需要预先安装要用的权重。可用 `docker compose exec model ollama pull qwen2.5:1.5b` 下载。首次拉取需要能访问模型仓库。本地路由信任部署者指定的 `EDGE_MODEL_LOCAL_URL`，请将其指向本机或受控内网的 Ollama 进程。真实云调用还需追加一个只读凭据挂载，例如已有凭据文件的 Windows 工作站可以执行：

```powershell
$env:EDGE_DEEPSEEK_KEY_FILE = Join-Path $env:LOCALAPPDATA 'edge-agent-ros/secrets/deepseek-api-key'
docker compose -f compose.yaml -f compose.model-provider.yaml -f compose.model-provider-cloud.yaml up -d model-provider
$body = @{model='deepseek-flash';messages=@(@{role='user';content='Reply OK'});stream=$false} | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType application/json -Body $body -Uri http://127.0.0.1:8134/api/chat
```

容器内只有模型适配器持有 `/run/secrets/deepseek-api-key`。云请求固定发往 `https://api.deepseek.com/chat/completions`，直接使用系统 CA 校验 TLS，不跟随重定向。单个请求至多访问所选上游一次；切换到云端会把该角色的消息和响应 schema 发送至官方服务。宿主发布端口仅绑定回环地址，Compose 网络中的其他受信服务也能经 `http://model-provider:8134` 请求推理。现场部署要控制谁可以加入该网络。DeepSeek 官方将 `deepseek-flash` 列为 Flash API ID，底层服务的具体模型可以由供应商更新。[官方模型说明](https://api-docs.deepseek.com/) · [JSON 输出说明](https://api-docs.deepseek.com/guides/json_mode/)

在原生 Linux 板卡上，按顺序使用 `-f compose.hardware.yaml -f compose.model-provider.yaml -f compose.model-provider-hardware.yaml`。如需云调用再加 cloud overlay。指定 `EDGE_MODEL_ADAPTER_IMAGE` 为本机已有、匹配架构且含 ModuleJournal 的板端镜像。板端插件采用 host 网络，仅监听 `127.0.0.1:${EDGE_MODEL_PORT:-8134}`，本地 Ollama 默认从 `127.0.0.1:11434` 连接。Docker Desktop 或仿真工程使用第一组命令。

## 对接运行中的角色

| 调用者 | 接入方法 | 何时生效 |
|---|---|---|
| Agent 规划器 | 在已有契约目录的 planner-profile.json 中写入 `schema_version: 1`、`protocol: ollama`、`version`、`url`、`model`。仿真容器 URL 为 `http://model-provider:8134`，板端 host 网络为 `http://127.0.0.1:8134` | 规划器每次推理重新读取 profile，以原有 Task、Trace、工具治理与执行流程接收模型结果。已经由确定性规则处理的任务可能没有模型调用。 |
| 常驻作者、设计与编码插件 | 分别配置 `EDGE_OLLAMA_URL/MODEL`、`EDGE_DESIGN_MODEL_URL/EDGE_DESIGN_MODEL`、`EDGE_CODE_MODEL_URL/EDGE_CODE_MODEL` | 首次切换地址需重新创建所选作者容器。已经指向插件的角色可通过原子替换路由切换目标模型。已进入研发的工作仍受其自身配置锁定规则约束。 |
| 手工软件候选生成 CLI | 指定 `--ollama-url` 和 `--model` | 当前命令。 |
| 其他独立 HTTP 客户端 | POST /api/chat 提交 messages 或 POST /api/generate 提交 prompt 和可选 system | 每次请求按 model 精确选择路由；本插件提供非流式文本协议。 |

`EDGE_MODEL_ROUTES_DIR` 默认为 `model_provider/config`。新增或更换别名时，在该目录写入临时文件并原子替换 `routes.json`。路由文件映射别名到 provider 和上游 model，不接受 URL 或凭据。只有前缀为 `deepseek-` 的别名可连接云端。每次受理固定路由内容摘要；已受理任务按旧快照完成。配置失效返回 `model.config_invalid`，未知别名返回 `model.route_missing`，均可用旧文件原子回退。已获授权的云别名在没有云凭据时会明确失败。撤下插件前先将调用者切回旧地址并等待已观察到的在途请求结束，然后使用相同 Compose 文件执行 `stop model-provider`，保留日志卷以供复盘。

## 响应与诊断

`GET /health` 检查配置可读与路由摘要，`GET /api/tags` 列出已配置别名；两者均不发起上游推理。模型是否已安装、账号余额是否足够，以真实请求为准。请求支持 temperature、num_predict，并允许本地 Ollama 消费自己的 num_ctx 设置。云端使用服务商上下文窗口，输出预算映射为 max_tokens。DeepSeek 的 JSON object 模式会附加调用者的 schema 提示；本地插件对云端返回值再次检查项目正在使用的对象、数组、枚举、oneOf、长度与必填字段约束。超出当前云转换范围的 schema 返回明确错误。

上游调用最多 75 秒，包含 DNS / TLS 与接收正文。每个请求最多 128 KiB，上游正文最多 256 KiB，同时最多受理 4 个 HTTP 连接；过载连接会被关闭。密钥、提示、输出正文及上游错误正文不会写入 ModuleJournal。可检查 GET `/api/events?source=journal%3Amodel.provider`。对于成功请求可由 `adapter.event_id` 找到完成事实，按父事件回到开始事实，以 `request_sha256` 与规划器保存的规范化输入摘要比对。相同正文会产生相同摘要，精确区分重复调用应再核对 call_id 与时间。作者保有自己的重试预算，适配器不自动重试。HTTP 认证 / 余额错误分别记录 auth_failed / balance，限流为 rate_limit，网络离线或超时分别为 upstream_unavailable / timeout，云端 JSON 违反声明为 schema_mismatch。上游达到 token 限额时保留 `done_reason=length` 和可用计数，清空未确认内容以供作者按原规则决定。进程中断时，重启从持久日志找回未确认请求，生成 outcome_unknown 并引用原开始事件。

测试使用合成凭据与独立 HTTP 上游。可在根目录运行 `python model_provider/test_provider.py -v`，并对照真实模型实测判断现场授权、网络和模型质量。
