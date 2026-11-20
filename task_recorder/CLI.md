# 在普通电脑上按任务录制

## 地图、车载画面和现场观察画面

`start` 和 `status` 在标准错误输出地图页地址，在标准输出返回原始 JSON。只选地图时可以全程不接入相机。相机选项分别使用独立的归档流和文件目录。

| 使用场景 | 启动参数 | 记录的来源 |
|---|---|---|
| 使用已接入 ROS 的车载彩色画面 | `--camera /front/image` | `rgb`，`image_role=onboard`。原有环境变量 `EDGE_RECORD_CAMERA` 也可用。 |
| 在仿真中看场外摄像机 | `--overview /campus/view/overview/image` | `overview`，`image_role=overview`。从当前 Compose 项目的运行中 sim 读取镜像和 Gazebo partition。选择仿真世界实际发布的话题。 |
| 在实机场地看已获准的外部摄像机 | `--external-overview` | 登记 `overview`，图像由已部署的采集者通过 HTTP 主动上传。录制器不因这个选项订阅原始设备或加入执行器网络。 |

例如在已有校园仿真容器中，下面的命令同时选择两个画面。当前项目只有一个画面时，只写该路参数。已确认该录制器订阅的 ROS 与 Gazebo 消息均使用同一仿真时钟时，可加 `--clock-domain gazebo_sim_ns`。现场只在所有直接订阅均完成同域授时核验后填写相应域；独立 HTTP 生产者自行声明自己的域。

```sh
sh scripts/record-task.sh campus start inspect-01 take-01 --camera /front/image --overview /campus/view/overview/image
sh scripts/record-task.sh campus status take-01
```

现场复测时先让获准的场地相机独立采集，以 `hardware start inspect-01 take-01 --external-overview` 开录。采集者将 PNG 原始字节编码为 Base64，向 `POST /recordings/take-01/events` 发送以下结构。将示例的占位项替换为实测数据。发送端复用同一次采样的 `event_id` 处理网络重试，并在现场部署的受限网络内传输。

```json
{"task_id":"inspect-01","event_id":"site-camera-frame-913","kind":"overview","image_role":"overview","topic":"/site/entrance/image","source":"site.camera.encoder","clock_domain":"site-entrance-camera-boot-03","stamp_origin":"header","stamp_ns":3200000000,"width":640,"height":480,"media":{"name":"overview/frame-913.png","base64":"<实际 PNG 字节的 Base64>"}}
```

先用 `status` 看 `counts.overview` 增长，再打开地图页并确认来源、徽标和画面。暂时断开该场地采集者，地图与任务应继续记录。重新连接后只补发实际缓存帧；同一事件 ID 和内容重复上传只产生一行。停止录制后，用导出文件的 SHA-256 对照源 PNG。两处时钟未经现场授时核验时分别标识，外拍图像不能用于推断机器人位置。旧 `--gz-camera` 录制继续保留在 `rgb` 下。缺少历史角色元数据的图像按原始档案显示。

### 从仿真或现场采集者选择传感器摘要

仿真中可在开录时选择激光和陀螺仪，并限制两路各自的采样频率。

```powershell
.\scripts\record-task.ps1 sim start inspect-01 take-01 --laser-scan /scan --imu /imu --sensor-hz 4
```

Linux 和 macOS 使用 `sh scripts/record-task.sh` 并传入相同参数。留空某一路的话题就不订阅该路。每路可选 0.1～10 Hz，默认 4 Hz。激光量来自 `LaserScan.ranges` 中量程内的有限实测值，`nearest_m` 是最近回波距离。摘要中分别计数有效回波、正无穷无回波和无效量测。IMU 的 `angular_z_rad_s` 是传感器坐标系的 Z 轴实测角速度。数据行包含消息类型、话题、帧 ID、原始 ROS 时间戳和有效标记。没有发布者时该路列入 `missing_sources`。消息存在但测量无效时保留原因。失去有效头部时间时保留原字段，另标注接收时间与 `observer_unix_ns`。

现场使用已部署、具备设备租约与权限的物理生产者发送同格式摘要。下面的命令为既有生产者登记两路输入。现场录制器不会为此建立原始激光或 IMU 订阅。

```sh
sh scripts/record-task.sh hardware start inspect-01 take-01 --external-sensor laser_scan --external-sensor imu
```

生产者通过既有录制 HTTP 接口 `POST /recordings/take-01/events` 上传。端口和网络访问范围由部署负责人限定。以下是协议示例，生产者需填写自己的实际设备、任务、事件 ID 和源时钟域。重发沿用原 `event_id`。已经完成授时校验的来源才能声明相同的 `clock_domain`。独立时钟按接收时间回放。

```json
{"task_id":"inspect-01","event_id":"imu-sample-971","kind":"imu","topic":"/field/imu","source":"sensor_msgs/msg/Imu","frame_id":"imu_link","stamp_ns":3200000000,"header_stamp_ns":3200000000,"stamp_origin":"header","clock_domain":"robot-01-imu-boot-03","valid":true,"metrics":{"angular_z_rad_s":0.125}}
```

录制器记录“发生了什么”。任务仍由原来的 Agent 接受、执行和验收。安装 Python 3 的开发机可以直接控制记录器。选择哪个任务、何时开始、以何种终态结束，都能按录制号查到。

## 录一条软件任务

先启动可选的录制服务。下列操作只需标准库，工作目录为仓库根目录。示例中的 `ticket-07` 是将来要提交给 Agent 的任务 ID，`take-07` 是这次录制 ID，两者可以不同。

```sh
python -m task_recorder.cli start take-07 --task-id ticket-07 --sources agent_state agent_trace
python -m task_recorder.cli watch take-07 --agent http://127.0.0.1:8765 --wait-for-task --finish --timeout 300
```

现在通过原来的 Agent 接口或工作台提交同一 ID 的任务。也可以先提交，再运行上述命令。即使任务一瞬间执行完，或者第一次查询时任务已经结束，只要 Agent 的该任务账本尚在，就能取回已有工具调用。比如第一次查询只看到“成功”，时间线仍可以显示此前的 `tool.request → tool.response → verified`，对同一操作用 `operation_id` 关联。

| 来源 | 何时产生记录 | 可以从中回答的问题 |
|---|---|---|
| `agent_state` | 轮询时观察到状态、步骤数或步骤索引变化。任务尚未提交和服务暂时不可用也有记录。时钟域为观察器的 `observer_unix_ns` | 我什么时候看见任务开始、等待或终止？多次变化落入同一轮询间隔时，可能只观察到最后一次。 |
| `agent_trace` | 从源账本 `after=0` 开始按源序号升序翻页。每次最多读取 32 条，终态且末页读完才结束录制。重读时凭源序号去重 | 实际调用了哪个工具，属于哪一步，有没有收到结果或验证回执？`phase` 是原事件种类，`source_seq` 为原账本序号，`seq` 为归档序号。任务内的源序号可以有间隔。 |
| `agent_request` | 单独声明该来源后，保存用户触发任务的请求文本、创建时间，并复用项目的已知凭证模式脱敏 | 当时用户要求做什么？文本可能包含现场名称、个人信息等，只有确实需要归档原文时才开启。 |

工具调用记录包含任务标识、步骤索引、源 UTC、源序号与操作标识。源提供时，另记工具、提供方、模块、诊断代码、错误类型、耗时、结果状态及关联调用 ID。原参数、工具结果正文、错误正文和模型提示内容留在源系统。混看软件与 ROS 仿真数据时根据接收时间对齐。Agent 的 `agent_utc` 与仿真时钟分属不同的时钟域。

Docker 单命令入口默认申请 `agent_state` 与 `agent_trace`，可用 `EDGE_RECORD_AGENT_TRACE=0` 仅查看状态。要主动保存用户请求，可在开录前设置 `EDGE_RECORD_REQUEST_TEXT=1`，或在上面的底层 CLI 命令中附加 `agent_request`。若 Agent 仅返回状态，现场确实未收到的 `agent_trace` 会列入 `missing_sources`。先开录再执行任务还可以保留实时观察、地图和图像。

`--finish` 在观测到终态后以该终态结束录制并导出。省略它时，watch 命令在终态返回，可继续保留相机等其他来源一段时间，之后手工结束。超时或主动中断观察命令也会保留已记录内容。开第二个终端查询运行状况：

```sh
python -m task_recorder.cli status take-07
python -m task_recorder.cli stop take-07
python -m task_recorder.cli export take-07
```

`status` 返回实际收到的源、样本数、最近一次接收时间及缺失来源。`stop` 结束的是录制。手工结束时记录状态为 `STOPPED`；如果已按任务终态结束，会保持先前的结果。`export` 把关闭的归档展开到录制服务挂载的存储中，返回摘要。数据位于服务端的 `<recording_id>/media/mission/timeline.jsonl` 和 `<recording_id>/recording.json`。将存储映射到主机目录即可直接拷贝或复盘这些文件。

默认录制器地址为 `http://127.0.0.1:8188`，可在子命令前指定 `--recorder`，或使用环境变量 `EDGE_RECORDER_URL`。Agent 地址单独使用 `--agent`。在已发布的 Console 代理与 Agent 具有相同任务 API 时，两者均可作为观察入口。

## 验证场景

### 复盘由模型规划的感知任务

对于保留了客户端 POST、模型请求与响应、任务账本、录制时间线及传感器原始文件的车端任务，可离线生成分章节的录像。

```sh
python -m task_recorder.model_task_review --source ./mission-export --out ./mission-review --font /path/to/chinese-font.ttc
```

输出包括可直接播放的 `task.mp4`、可跳转章节的 `index.html` 及含源时间戳、文件哈希、候选能力、模型选择和实际操作 ID 的 `video.json`。需要 Pillow、FFmpeg 和 FFprobe。模型回放要求部署权限为只读，且注册目录、选用的能力版本、调用和成功回执能在同一任务中互相核对。帧查询的区域统计值由回放程序使用归档的 RGB 和深度像素复算。视频对事件分段停留，实际用时见画面和 `source_elapsed_s`。

已完成任务的分页测试校验 38 条工具事件，两次启动观察后核验源序号、去重结果、跨任务污染拦截、受限字段与用户原文开关。单独 Docker 项目另验真实 ROS 输入与“第一条成功回执随附两页工具历史”的场景。[HTTP 测试](../tests/test_recorder_cli.py) · [Docker 运维实测](../artifacts/verification/recorder-ops/run04/results.json)

四组本机 HTTP 试验启动真实录制服务，由独立软件任务响应端合成“尚未提交、临时 503、执行中、相同执行中、成功”的状态序列，实际归档四条记录，并按源身份核验请求仅有 GET。另覆盖手工停止、重复导出、身份错配，以及运行中手工结束录制时观察进程自动退出。人工结束和任务完成同时发生时，两端取回同一份最先写入的结束回执。测试输入含原文、参数及凭证占位值，逐行核验导出结果中均无这些字段。[复跑测试](../tests/test_recorder_cli.py)
