# 按任务录制与回放

想保留一次故障、一次新场地测试或一次纯软件任务时，可以在下发任务前打开录制。日常启动不会启动录制服务、采集相机或生成视频。每次录制都有自己的 `recording_id`，绑定已有或即将创建的 `task_id`。对同一任务可以做多次录制。

## 从开始到拿到视频

在运行 Edge Agent 的同一个 Docker Compose 项目中操作。先选定任务 ID，再开始录制，随后按原有 Agent 接口用相同 ID 下发任务。下面的例子将任务 `patrol-07` 录为 `take-07`。Windows PowerShell 在项目根目录运行以下命令。

```powershell
# 按需选填已发布的 ROS sensor_msgs/Image 话题；省略时只录任务与 ROS 状态
$env:EDGE_RECORD_CAMERA = '/camera/color/image_raw'
.\scripts\record-task.ps1 sim start patrol-07 take-07
.\scripts\record-task.ps1 sim status take-07
# 使用默认 Console 端口向已有 Agent 提交同一个 ID；这里仅查询状态
$body = @{ id = 'patrol-07'; text = '查询机器人当前位置' } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8765/agent/tasks -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
# 观察到任务终态后自动结束录制，随后导出
.\scripts\record-task.ps1 sim export take-07 artifacts/recordings/take-07
```

在 Linux 板端将入口写作 `sh scripts/record-task.sh hardware`，后面使用相同的操作和参数。纯软件任务设置 `EDGE_RECORD_SOFTWARE_ONLY=1` 并清除 `EDGE_RECORD_CAMERA`。多套系统并存时使用原系统的 `COMPOSE_PROJECT_NAME`、镜像、插件目录与 ROS 域配置。相机话题名随驱动和现场命名空间配置。

在本项目已有的**校园工作台**，把操作中的 `sim` 换成 `campus`，向校园 Console 提交任务（本机当前端口为 8766）。校园入口会读取运行中的主 Agent 的镜像、ROS 域和实际插件卷。要看车载画面，可以将 `EDGE_RECORD_CAMERA` 设为 `/model/edge_rover/rgbd/image_raw`。RGB 接受 rgb8、bgr8 和 mono8。设置 `EDGE_RECORD_REQUEST_TEXT=1` 可将自然语言原话的脱敏版本也保存到录制中。适合演示，默认关闭。

| 要做什么 | 操作 | 产生的效果 |
|---|---|---|
| 提前开始 | `start TASK_ID RID` | 启用服务，安装现有插件管理器可识别的 `task.record` 服务描述，启动这一次的只读采集 |
| 查看是否正在采集 | `status RID` | 查看来源、已收条数、最近接收时间、一直未到达的来源 |
| 提前结束 | `stop RID` | 只关闭这次录制，任务继续，其他录制独立运行 |
| 取出数据 | `export RID NEW_DIR` | 将已经结束的录制从 Docker 存储复制到指定的新目录 |
| 准备卸载 | `drain`，然后 `disable` | 拒绝新录制，已有录制可以完成。活动数量降为零后才允许卸载 |
| 再次使用 | `enable` | 重新开放接收新录制 |

在开发机离线成片，需要 Python 3.11+、Pillow、FFmpeg、FFprobe 和可显示中文的字体。先导出，再执行下列命令。输出目录含 `index.html`、`task.mp4`、封面和可核查的视频索引。纯软件任务也能生成状态时间线视频。

两段真实回放展示不同的数据源。校园片能看到小车、地图和故障停车。软件片刻意在任务结束后才查询，Agent 单次状态查询只看到成功，录制器仍从持久账本恢复了九条事件，可查看工具发起、响应和验收。[校园任务](../artifacts/verification/unknown-campus/mission-02/video/index.html) · [纯软件工具回放](../artifacts/verification/task-recorder/fast-tool-01/video/index.html)

```powershell
python -m task_recorder.render --recording artifacts/recordings/take-07 --out artifacts/recordings/take-07/video --font C:/Windows/Fonts/msyh.ttc
```

## 把现场、传感器与任务事件剪进同一张画面

选择 `--layout showcase`，一边看真实相机和地图，一边观察最近四秒的采样。校园片优先显示激光最近回波、IMU 实测角速度和下发线速度。速度指令、融合位置和实际传感器回波各有自己的单位与时间戳。感知暂停时，保留的旧地图会显示帧龄，超时的通道会断线。事件卡注明可查的父事件、同一操作或同一任务段；仅在时间上相邻的事件单独标明。

```sh
python -m task_recorder.render --recording <封存目录> --out <新的成片目录> --layout showcase --font <中文字体路径>
```

每条事件都显式标注相同仿真时钟域的校园录像可附加 `--axis stamp_ns --guide tests/system/unknown_campus_video.json`。旧归档缺少域标识，或混录了软件日志和仿真时钟时，使用默认接收时间。打开输出目录的 `index.html` 可以跳章，`opening.png` 和 `cover.png` 分别是片头和中段原帧。核对时间线或引用的源样本时查看 `video.json` 的 `showcase.chapters`。实机的传感器事实由已获设备权限的采集进程提交，视频工作站只读取归档。

### 接入自己的数值通道

以下例子用一个软件任务计算 SHA-256，每处理一块数据便提交累计字节数。这样可以在开发机验证第三方来源如何进入录像。有现成录制服务时直接使用它的地址。独立试用可在第一个终端执行 `python -m task_recorder.service --state artifacts/recordings/showcase-demo`，在第二个终端的 Python 中运行以下代码。

```python
import hashlib, json, time, uuid
from pathlib import Path
from urllib.request import Request, urlopen

rid = "hash-" + uuid.uuid4().hex[:8]
def post(path, body):
    req = Request("http://127.0.0.1:8188/recordings/" + path,
                  data=json.dumps(body).encode(), headers={"Content-Type":"application/json"})
    with urlopen(req, timeout=5) as response:
        return json.load(response)

post("start", {"task_id":rid, "recording_id":rid, "sources":["hash_progress", "mission"]})
def emit(kind, **fields):
    post(rid + "/events", dict(task_id=rid, event_id=uuid.uuid4().hex, stamp_ns=time.time_ns(),
                               clock_domain="demo_unix", kind=kind, **fields))

digest, total = hashlib.sha256(), 0
emit("mission", phase="started", title="开始计算摘要")
for block in [b"edge" * 4096] * 6:
    digest.update(block)
    total += len(block)
    emit("hash_progress", metrics={"bytes":total})
    time.sleep(.3)
emit("mission", phase="finished", title="已完成摘要", detail=digest.hexdigest())
post(rid + "/finish", {"task_id":rid, "status":"COMPLETE", "reason":"hash loop finished"})
folder = Path("artifacts/recordings/showcase-demo") / rid
guide = {"showcase":{"signals":[{"source":"hash_progress", "field":"metrics.bytes",
          "label":"已处理数据", "unit":"B", "max_age_s":0.7}]}}
(folder / "guide.json").write_text(json.dumps(guide, ensure_ascii=False), encoding="utf8")
print(folder)
```

用打印的目录作为 `--recording`，加入 `--guide <打印目录>/guide.json` 即可成片。展示配置最多选择三条数值通道。字段可以使用 `metrics.temperature_c` 等键路径；单位和超时阈值由来源提供方指定。选择过但没有收到样本会显示“等首样”。显式配置的未选来源会显示“未选录”。证据索引保留样本原值、消息时间和接收时间。[设计与验收](../docs/specs/SDD-046-showcase-recording.md)

## 插件怎样接入系统

| 组件 | 输入 | 输出与用途 |
|---|---|---|
| 录制服务 | 显式开始命令、已声明来源、绑定任务的事件与附件 | 保存 SQLite 事务记录。按 `GET /recordings?task_id=…` 找回录制，按游标读取事件 |
| Agent 观察器 | 原系统的任务状态查询与按序事件分页接口 | 记录看到的状态和步骤数；从序号 0 分页补收已留存的工具调用、响应、验收等事件的类型与标识。可按源序号回到任务账本查原件 |
| ROS 来源 | 选择的相机、在线地图、融合位姿、速度指令、当前任务相关的事件总线消息 | 写入带源时戳的抽样。地图和位姿是当前机器人数据；并行任务共用机器人的物理观测 |
| 其他模块 | HTTP POST 到 `/recordings/RID/events`，携带 `task_id`、`event_id`、`kind`、`stamp_ns` 和业务字段 | 可加入新的阶段、测量或诊断来源。相同事件 ID 重投去重，内容冲突有显式错误 |
| 离线渲染 | 导出的 `recording.json`、`media/mission/timeline.jsonl`、图像与栅格 | 生成视频和可点击的章节；不参与运动控制和任务验收 |

相机、地图等现场观测从启用时开始。要保留完整过程，请先开始录制再提交任务。已结束任务仍可补收 Agent 账本里留存的工具事件，使用同样的开始命令。托管入口默认开启 `agent_trace`，可用 `EDGE_RECORD_AGENT_TRACE=0` 关闭。请求原文、工具参数与返回正文各有不同的保留范围，参见 [CLI 记录字段](CLI.md)。

未选择相机时仍有状态和地图证据；选择相机却一直收不到帧，会在 `missing_sources` 中看到 `rgb`。已收到过的来源可按时间线和 `stream_timing` 查中途断流。记录既保留各来源的采集时间，也保留服务接收时间。软件观察器与 ROS 仿真时钟混用时默认按接收时间渲染；所有来源都使用同一时钟才选择 `--axis stamp_ns`。

服务每次录制默认限额 256 MiB，可用 `EDGE_RECORDING_MAX_MIB` 配置。达到限额标记 `LIMIT_REACHED`，进程重启后把此前仍活动的录制标记为 `INTERRUPTED`。两种情况都能导出已提交的数据。导出和视频另占空间，部署者需根据相机码率、任务时长与保留周期准备磁盘。本版长时间任务需提前规划容量，尚无分段滚动归档。精确回放全部原始激光、IMU 包或零丢帧录像，需要专门的数据源和相应的存储预算。[HTTP 和纯软件观察器用法](CLI.md) · [设计和验收契约](../docs/specs/SDD-044-task-recorder.md)
