# 离线分析任务导出

从仓库根目录运行 `python -B -m analysis`，传入一个或多个已导出的本地文件。工具只使用 Python 3.10+ 标准库，把摘要写到标准输出。拿到副本即可在工作站复核，默认板端包省略此目录。

```sh
python -B -m analysis --task analysis/examples/task.json --console analysis/examples/events.ndjson --evidence analysis/examples/evidence.json --recording analysis/examples/recording.json
```

加 `--format json` 可取得逐来源统计和每个输入文件的 SHA-256，便于和同事确认所用副本。库调用返回相同的字典。

```python
from analysis import analyze

report = analyze(task="analysis/examples/task.json", console="analysis/examples/events.ndjson")
print(report["task"]["state"], report["console"]["by_level"])
```

| 参数 | 提供的原始文件 | 统计口径 |
| --- | --- | --- |
| `--task` | Agent 的 `GET /agent/tasks?id=…` JSON 回应 | 任务所有者报告的当时状态、步骤数、本页事件数和分页标记。 |
| `--console` | Console 的 `GET /api/export` NDJSON，可包含快照 | 逐行统计导出范围内的事件类型、等级和序号窗口。显式标记该任务 ID 的记录单独计数。缺少任务 ID 的旧记录保留在全文件统计中。 |
| `--evidence` | Evidence 的 `query_evidence` 工具回执 JSON | 查询状态、已加载的事件数、SQL 返回行数、截断及归档保留标记。查询受阻时给出错误码。 |
| `--recording` | Recorder 导出目录中的 `recording.json` | 各来源样本数、缺样来源、录制生命周期状态和录制方接到的任务状态。 |

Console 的导出可以包含多项任务。需要单项任务的关联事件时，使用原有的 `/api/export?task_id=…` 获取对应范围。分析器尊重导出顺序，允许筛选造成的序号间隔；没有显式 `task_id` 的事件不会按相邻序号猜测归属。Evidence、Recorder 和任务详情都有任务 ID 时须一致。不同来源的事件可能描述同一事实，各自的数量独立计算。

可以只提供录制或事件文件。这时 JSON 的 `task` 为 `null`，人的摘要会明确写出任务状态未知。传入空的 Console 导出会得到零条事件。无法读取文件、JSON 损坏或记录格式不符时，命令以状态码 2 结束；NDJSON 错误包含文件名和行号。

## 样例

[输入目录](examples/)中的四个文件来自一次合成的软件夹具。Agent 的真实详情接口返回 `SUCCEEDED`；Console 的真实导出接口归档了 2 条观测；Evidence 通过公开 HTTP 查询其中一条告警；Recorder 导出了 1 份阶段样本，RGB 缺样。录制方单独收到的 `STOPPED` 位于 `recording.reported_task_status`。样例中没有使用现场设备。

完整结果见[人读摘要](examples/summary.txt)和[统计 JSON](examples/report.json)。运行 `python -B tests/test_offline_analysis.py -v` 会从仅含分析包和输入的临时目录重现两个结果，并逐一校验输入字节。
