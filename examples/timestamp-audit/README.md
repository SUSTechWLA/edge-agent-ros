# 检查一组采样时间戳

给 Agent 一句话，例如 `检查这组采样时间戳的重复与回拨：[100,110,110,95,110,140]`。加载此工具后，Agent 将用户给出的序列交给独立 HTTP 进程，工具返回可逐项复算的证据。这个例子演示如何给机器人增加新的软件算法；运行只需 Python 标准库。

| 收据字段 | 对应本例的事实 |
|---|---|
| `sample_count = 6` | 收到六个有序的样本。 |
| `duplicate_count = 2` | 数值 110 的第二、三次出现各计一次，非相邻的重复也计入。 |
| `backward_count = 1` | 相邻的 110 到 95 是一次回拨。分析保留原始接收顺序。 |
| `max_forward_gap_ms = 30` | 最大前向差为 140 − 110；序列中没有正向差时返回 0。 |

[工具清单](tool.json)约定参数 `timestamps_ms` 为字符串，其中包含 1 至 256 个有符号 64 位整数毫秒值组成的 JSON 数组。提供者原样返回 `operation_id`，相同输入可重复计算。[验收样本](cases.json)覆盖乱序、跨位置重复、负时间、单样本及四项错误输入。非法序列返回 `BLOCKED` 和 `timestamp.invalid_series`，版本不匹配返回 `timestamp.version_mismatch`。

从项目根目录运行以下两条命令。第一条用独立预期与 HTTP 实际回执逐项比对，第二条在专用 Docker 内网启动真实 Agent 和提供者，再实际提交上面的自然语言请求。它还验证挂载前后、重复提交、卸载后查询原任务和 Agent 的启动时间。[两层验收入口](../../tests/system/verify_sample_provider.py)

```sh
python tests/system/verify_sample_provider.py --out .ci-results/timestamp-contract.json
python tests/system/verify_sample_provider.py --image edge-agent-ros:v0.1 --out .ci-results/timestamp-agent.json
```

在项目根目录执行以下命令可单独验证提供者的 HTTP 回执。候选容器的源码只读挂载，回环请求由同容器内的临时客户端发出。第一条命令适用于 PowerShell。

```powershell
$example = (Resolve-Path examples/timestamp-audit).Path
docker run --name timestamp-audit-preview --rm -d --network none --read-only --cap-drop ALL -e PYTHONDONTWRITEBYTECODE=1 -v "${example}:/candidate:ro" edge-agent-ros:v0.1 python3 /candidate/provider.py --bind 127.0.0.1
docker exec timestamp-audit-preview python3 -c "import json, urllib.request; body=json.dumps({'operation_id':'preview/step/1','version':'1','arguments':{'timestamps_ms':'[100,110,110,95,110,140]'}}).encode(); print(urllib.request.urlopen('http://127.0.0.1:8124/invoke',body).read().decode())"
docker stop timestamp-audit-preview
```

接入 Agent 时，在与其互通的部署网络启动[提供者](provider.py)，清单默认使用服务名 `timestamp-audit`、端口 8124 和路径 `/invoke`。将通过验收的清单原子发布到部署的插件目录即可供后续规划使用。撤下清单后，已保存的任务证据仍应能回查。新算法改变收据含义时同步升级清单与服务版本，并为旧委托保留原实现。[插件约定](../../plugins/README.md)
