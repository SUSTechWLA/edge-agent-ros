# 让模型把一个文本任务做成可热装的 Tool

试着下发 `统计 robot 词频【ｒｏｂｏｔ  ROBOT robot   】`。目标按空白分词、忽略大小写、只算完整的 ASCII 单词 robot，所以应返回 2。全角词、单词的一部分和随词附带的标点都有各自的含义。这个案例从一次软件能力缺口开始，经设计、生成候选、验收、注册，直到已有 Agent 调用新能力。

从仓库根目录运行，下列命令各自建立和清理一个隔离 Compose 项目。第一条采用仓库内编码 Agent 提供的候选，第二条从本机已有模型卷复制模型到临时只属于本次测试的卷，让 Ollama 生成函数。

```sh
python tests/system/software_provider_acceptance.py --image edge-agent-ros:v0.1 --out .ci-results/software-provider.json
python tests/system/software_provider_acceptance.py --image edge-agent-ros:v0.1 --model-cache edge-agent-ros_models --out .ci-results/software-provider-model.json
```

镜像需包含本规格的两个客户端和支持 `verbatim_argument` 的规划器。构建单独测试标签可用 `docker build -f docker/Dockerfile -t edge-agent-ros:software-test .`，运行命令的 `--image` 相应改成该标签。测试会向自己的 Agent 提交任务，用确定性模型协议夹具制造旧作者的拒绝，经实际 Console 归档获取关联的设计草案。选择真实模型模式时，由指定的 Ollama 执行**候选实现**生成。独立期望保存在 [cases.json](cases.json)，提供者源码示例见 [candidate.py](candidate.py)。

| 步骤 | 输入和实际操作 | 产物与权限 |
|---|---|---|
| 设计 | SDD-013 提供私有设计 JSON、对应的 `plugin.design.drafted` 导出事件；运维配置受信 Console URL | 后续每次验收、发布均回查归档事件的源、ID、trace 和摘要。归档访问权限决定这里的信任边界 |
| 编码 | Coding Agent 提供 `candidate.py`，或调用 `software_release.py generate`，结合草案和 [contract.json](contract.json) 向受信本地模型请求表达式 | 作者只有候选目录和自身审计的写权限。模型接触目标和契约 |
| 试运行 | 以 `python3 -IB /opt/edge/client/software_provider.py --package /package` 启动独立容器，挂载只读候选包 | 示例 UID 65534、只读根目录、96 MiB、0.5 CPU、16 PID，去除 Linux capabilities。提供者仅在测试内网提供 HTTP，使用单线程和有界请求 |
| 验收 | `software_release.py verify` 向真实 HTTP 端点发送独立样本，并检查原操作 ID、结果、原输入和实现版本 | 私有 store 用摘要保存设计、源码、契约和样本。输出 `approval_sha256` 绑定整份工件，`version` 是注册清单的 CAS 内容摘要，`declared_version` 是执行时绑定的实现版本 |
| 发布 | 获准的发布者指定验收摘要 `--approve`、预期旧清单摘要 `--expect` 及稳定的 `--request-id`。首次创建的旧版本为 `absent` | 新发布会再验证端点。沿用已有发布锁、意图、历史和完成/拒绝收据。同一已提交请求重投时返回原收据 |
| 执行 / 卸载 | Agent 下一次读取目录即可使用 Tool。按原版本排空使用方，发布者 `remove --expect <当前清单摘要>`，然后停止提供者 | 原始请求、后续任务和旧收据仍可查。恢复旧实现时按私有工件重建包，启动匹配版本，重新验收并发布 |

部署方可以运行不同的编码 Agent，只需交付约定的单函数源文件。提供者解析 AST 后只解释以参数 `text` 为起点的表达式。允许 0–8 步字符串 `casefold/lower/upper/strip/split` 操作及字符串或词列表的 `count('固定词')`。最终结果为文本或整数。函数签名固定为 `def transform(text)`，一行 return，输入/输出文本各最多 4096 UTF-8 字节，候选源码至多 2048 字节。这个扩展可以改变算法而无需修改 Agent，但适用面仍是有限的纯文本程序。文件、网络、模型推理、ROS 设备等能力应通过相应的独立插件契约提供。

发布 CLI 由部署者在已准备的管理容器中执行。各参数及检查使用 [software_release.py](../../client/software_release.py)。下列 Bash 命令假定管理容器可访问自己的只读输入、私有工件库、注册目录和模块事件源；项目测试夹具给出了实际 Docker 挂载配置。把 `plugin.software/module-events.sqlite` 以既有模块源方式给 Console 只读发现，使所有插件开发事实可以按 trace 查询。

```bash
common=(--event /inputs/event.json --design /inputs/design.json --archive http://console:8080
        --package /package --journal /e/module-events.sqlite)
target=(--endpoint http://provider:8125/invoke)
cases=(--cases /inputs/cases.json --store /store)
python3 /opt/edge/client/software_release.py verify "${common[@]}" "${target[@]}" "${cases[@]}"
# 将上一步输出的两个实际摘要分别赋给 APPROVAL 与 MANIFEST_SHA。
python3 /opt/edge/client/software_release.py publish "${common[@]}" "${target[@]}" "${cases[@]}" \
  --registry /registry --approve "$APPROVAL" --expect absent --request-id site-install-01
python3 /opt/edge/client/software_release.py remove "${common[@]}" "${target[@]}" \
  --registry /registry --expect "$MANIFEST_SHA" --request-id site-remove-01
```

为每个注册目录配对一个持久的发布事件库并保留历史。升级代码、修改目标、切换端点或调整样本时重新验收和批准。独立案例可以证实测试输入的行为，对开放世界任务的正确性还要另行验证。完整复测入口、实现边界和记录见 [SDD-014](../../docs/specs/SDD-014-authored-software-provider.md)。
