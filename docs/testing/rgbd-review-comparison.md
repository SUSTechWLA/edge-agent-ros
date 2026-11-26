# RGB-D 按列复核实验与历史回放

本实验使用同一对已录制的 RGB 和对齐深度图。四次通过前端提交完全相同的文本，前三次由部署人员分别将站点工作流、Agent 工具组合和系统插件列为首选。第四次保持第三次的优先级与只读授权，先对内置插件调用 DSH 原生 drain，再提交任务。部署账号执行 drain。Agent 从仍可接单的实现中选用备用工具。

## 2026-11-23 Jetson 实测

四个独立的原生 Agent 任务均为 `SUCCEEDED`。输入为同一份 D435 录制帧，分辨率 640 × 480，`depth_scale_m=0.001`，封存清单为 `sha256:e49c2999234052487b331bf1a1d3e030900f54fbd849c9fc5956b06d2f07642c`。独立 PNG 解码得到以下测量值，四次任务均与其一致。

| 从左到右 | 有效像素 / 全部像素 | 覆盖率 | 深度 P10 |
| --- | ---: | ---: | ---: |
| 左 | 102118 / 102240 | 99.88% | 0.941 m |
| 中 | 92082 / 102240 | 90.06% | 0.556 m |
| 右 | 93134 / 102720 | 90.67% | 0.367 m |

三列都满足 60% 覆盖率和 1.2 m 阈值，人工复核顺序为右、中、左。第二次任务的模型回执来自 `ollama/qwen3:4b-instruct`，它在提供的五个只读算子中选择了 P10 测量、阈值分类和近距优先排序。模型产物是本次任务的数据流图。原子服务与本次用来代表站点工作流的示例代码由开发者预先提供。

第三次和第四次使用逐字节相同的能力索引。DSH 的 drain 事件为 `fa0aa0622dba46b695ae1bc14188f101`，随后内置插件退出接单，第四次复用了第一次的工作流绑定。六个冻结的插件或服务版本均与宿主清单相符。启动时六个容器均无设备映射、未启用 privileged、丢弃全部 Linux capabilities。两个感知进程只读挂载帧。录像和回执证明的范围为这四个静止感知任务。

## 在独立实验站复现旧任务

这些插件用于检验 DSH 对多种实现的管理。按列深度统计不能给出物体边界或身份。普通启动仅提供 Agent、宿主、前端与历史记录；重现实验需显式启用 Compose 的 `--profile grid-replay`，并在独立实验注册表安装原始包。

1. 准备独立的站点目录、固定的 ARM64 平台镜像清单原始字节、其摘要和构建所用源码提交。将配对的 `frame.json` 和两张原图放入该站点的 `inputs/`。使用 `frame.json` 的文件 SHA-256 作为 `REVIEW_FRAME`，带上 `sha256:` 前缀。
2. 参考 `perception/review055/site_workflow.py` 实现 `review(frame, depth_raster, columns, distance_m)`，返回 `{"analysis": ..., "review": ...}`。运行时提供只读帧元数据与 `(bytes, width, height, stride)` 的深度栅格，逐次核验输出结构、来源、阈值与排序。站点部署者审核 Python 源文件，按该文件的原始字节计算 `sha256:<hex>`。外部工作流会以该感知容器的进程权限运行，可执行的源码由站点部署者控制。
3. 生成新站点和新策略目录。命令中的 `REVIEW_IMAGE` 为平台 manifest 的 SHA-256，`REVIEW_WORKFLOW_SHA` 为工作流文件的 SHA-256。配置会将该工作流摘要与帧摘要一起写入宿主启动探针。

```sh
python3 scripts/prepare_review_site.py --site release/site --policy-dir release/config \
  --image-manifest platform-manifest.json --image-digest "$REVIEW_IMAGE" \
  --source-commit "$SOURCE_COMMIT" --frame-ref "$REVIEW_FRAME" \
  --workflow-digest "$REVIEW_WORKFLOW_SHA"
```

4. 使用仓库的 `compose.review-comparison.yaml`。部署外部工作流时可在站点的 Compose override 中为 `review-workflow` 加入以下字段。基础 Compose 保留 `./inputs:/inputs:ro` 及 `EDGE_REVIEW_WORKFLOW_DIGEST` 的绑定，服务在装载时检查实际字节，摘要不符则启动失败。

```yaml
services:
  review-workflow:
    entrypoint: [python3, -B, -m, perception.review055.server, --role, workflow, --workflow, /workflow/review.py]
    volumes:
      - ./user/review.py:/workflow/review.py:ro
```

5. 启动该独立站点的原生 Agent、soft-host、两个感知工作进程及前端，使用已有站点模型和授权网关配置。主机通过 `/health` 核对工作流和帧摘要后才发布可用工具。从前端提交任务，保留完整模型、工具和注册表回执。更换已上线的代码时使用新的发布版本/站点，按 DSH 执行 drain、交接与新绑定准入。文档中的实验使用镜像自带的工作流示例，替换真实用户工作流属于另一项实验。

## 导出

在一个目录内保存下列文件。保留配置和工件的原始字节，包括行尾与编码。

| 文件 | 来源和内容 |
| --- | --- |
| `frame.json` 和两张 `<sha256>.png` | 感知工作进程实际挂载的 RGB-D 清单和 PNG，文件名取清单中 `rgb_ref` / `depth_ref` 的 64 位十六进制摘要。 |
| `01.json` 至 `04.json` | 各次提交后的原生 `GET /agent/tasks?id=<id>&after=0&limit=500` 终态。若 `events_has_more` 为真，用最后一个 `seq` 作为下一页的 `after`，合并全部 `events`。保留任务触发、步骤、冻结绑定、证据及原始模型请求和响应。开启 `EDGE_TRACE_CONTENT=redacted`，并让模型执行范围只含可以留档的任务数据。 |
| `index-before.json`、`index-after.json` | 第三次提交时与第四次提交时正在使用的 `EDGE_READ_CAPABILITY_INDEX`。分别在 DSH drain 前后复制。 |
| `drain.json` | 站点部署端 DSH v2 drain 返回的完整 JSON，含版本、修订和注册表事件 ID。 |
| `ops.jsonl` | 站点运维事件流。drain 行包含 `kind=site.drain`、`provider=review055.builtin`、注册表 `event_id` 对应的 `source_ref` 和带时区的 `utc`。 |
| `workflow.py` | 建议复制本次运行实际加载的用户工作流源码，以便复核两个工作流运行回执中的 `workflow_digest`。报告注明是否拿到了源码。 |
| `approved.json`、`pack.json`、`plugins-before.json`、`plugins-after.json`、`isolation-before.json` | 可选的原生准入、打包结果、drain 前后两个 DSH 宿主快照及启动时的容器隔离快照。提供后将额外交叉校验，结果见 `site_evidence`。 |

## 离线检查

下例对应三列、1.2 米。如果提交的自然语言任务使用其他数值，传入该任务明确指定的参数。验证脚本只读取上述文件并写入报告。Linux 使用以下命令，Windows 项目环境将 `python3` 替换为 `.\scripts\project-python.ps1`。

```sh
python3 scripts/verify_review_comparison.py --input /path/to/export --columns 3 --distance-m 1.2
```

退出码 0 表示本次检查通过，结果保存在导出目录的 `verification.json`。可通过 `--output` 指定新的结果文件。脚本独立解码 RGB8 和 16 位单通道深度 PNG，按像素复算每列的有效数、P10、覆盖率、状态和人工复核顺序，并逐次核对结果。覆盖率小于 60% 归为未知。达到覆盖率要求且光轴深度小于或等于阈值的列，按深度递增排序。

脚本把 Ollama 的输出图、冻结参数、DSH 服务调用回执逐边对齐，也会复核相同备用实现绑定、停用前后的策略文件和 DSH 修订。报告中的零物理步骤与零 ROS 调用事件仅覆盖这四条任务记录。整车级控制活动需要独立的观测证据。此实验使用相机视野内的光轴深度，最终产出是人工优先复核顺序。
