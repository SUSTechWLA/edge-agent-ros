# 传感器巡检

遇到“任务仍在执行，但机器人看不到路”，先看巡检中哪个输入出问题、异常始于哪一帧、后续是否收到连续三份正常测量。每个输入独立判断。巡检结果可以进入 Agent 的任务会话，也可以在工作台按时间反查。

## 在运行中的校园使用

在仓库根目录执行 `python scene/campus_large/run.py sensor-health --image <已构建且包含本插件的镜像>`。命令启动一个 ROS 订阅者，登记当前校园的只读工具。打开巡检页面，或在工作台输入“检查导航传感器”“检查本机传感器”。校园默认订阅轮式车的 RGB、深度、水平激光与 IMU。页面将未装配的通道列为“未配置”。[实时巡检](http://127.0.0.1:8136/) · [任务会话](http://127.0.0.1:8766/#sessions)

| 如果要确认 | 看哪里 |
|---|---|
| 哪路出错 | 在卡片“测量与证据”中核对话题、`frame_id`、`stamp_ns` 和当前原因。HTTP `GET /status` 还返回距最后一次接收的单调时间 `silence_s`。 |
| 最近画面是什么 | 校园用 `run.py sensors` 启用独立的机载彩色与车载 RGB-D 页面。巡检保留指标。需要复原历史像素时，应另行保存原始帧或 ROS bag。 |
| 故障多久、有没有好 | 按同一 `trace_id`（例如 `sensor.health.rgb`）及时间读 `sensor.rechecking`、`sensor.needs_attention`、`sensor.recovered`。同源前一次转换的 ID 在 `parent_event_id`。初始观测没有父事件。 |
| 这次任务实际看到什么 | 用任务 ID 找 `diagnose_sensors` 的返回证据。每个输入最多一条，包含最近样本、原因、指标和事件 ID。要判定具体任务的失败原因，还要核对执行器回执、定位和当时的时间窗。 |

工作台接收本模块的持久 EventBus 记录。每条观测转换带来源、UTC、ROS 采样时间与 frame（有样本时），可通过 `journal:sensor.health` 过滤。未收到首帧与后续断流都按单调时间处理。断流事件里的 `observation` 是最后实际收到的样本，事件顶层 `code` 给出后续断流原因。[校园原始巡检事件](http://127.0.0.1:8766/api/events?source=journal%3Asensor.health&limit=20)

## 各种测量能说明什么

| 输入 | 实际检查与当前默认阈值 | 解读 |
|---|---|---|
| RGB 图像 | 最多 32 列 × 26 行抽样；亮度 ≤12 的比例 ≥93% 报黑图，≥245 的比例 ≥93% 报过曝。相邻抽样亮差 ≥12 的比例低于 2.5% 报弱纹理。 | 这些现象影响视觉观测条件。采样可能漏掉小范围异常；匹配是否成功需要前端证据。 |
| 对齐深度 | 抽样中非有限数或 ≤0 的比例 ≥60% 报 `depth_holes`，支持 16UC1、32FC1。 | 看向天空、近于盲区或远于量程也可能有空洞。结合 RGB 与像素深度判断原因。 |
| LaserScan | 最多 720 束；+∞ 与等于最大量程视为无近场回波，合法命中不足 5% 记 `few_returns` 信息状态；非法距离超过 50% 进入复核。 | 区分开阔地与坏测量。具体驱动对无回波的编码应在接入时核对。 |
| PointCloud2 | 最多 600 个 XYZ 样本，合法点要求有限且 XYZ 不全为零，合法比例低于 10% 报异常，空云单列。合法点云总点数 <24 记 `few_points` 信息状态。 | 稀疏不直接决定定位成败。传感器覆盖范围和移动速度仍需结合具体机体评估。 |
| Imu | 检查三轴角速度、三轴加速度是否有限，报告两个模长。 | 当前没有偏置、噪声或绝对姿态认证。 |
| 可选视觉/激光前端 | `std_msgs/String` 的 JSON 含 `stamp_ns`、`frame_id`、`matches` 和 `inliers`。内点 <6 或内点率 <25% 触发 `low_match_inliers`。 | 指标来自接入的匹配算法。没有该数据时保持“未配置”。 |

## 更换输入、控制复核预算

设置 `EDGE_HEALTH_<输入>_TOPIC`，输入键可选 `RGB`、`DEPTH`、`SCAN`、`POINTS`、`IMU`、`MATCH`、`LIDAR_MATCH`。可用同名 `_FRAME` 指定期望坐标系。显式空话题关闭该输入。车、飞行器、四足和人形可以在各自的 Compose 工程、ROS 域和端口装同一插件，读取对应话题。当前一实例一个配置机器人，多机请隔离实例和事件卷。

仿真使用 ROS 仿真时钟，Linux 实机 Compose 使用本机时钟、host 网络和本机回环 HTTP。应先让数据源与观察者使用一致的时间基准。默认样本龄上限 `EDGE_HEALTH_MAX_AGE_S=1.5` 秒，容许最多 0.1 秒未来时间。默认无样本超时 `EDGE_HEALTH_TIMEOUT_S=3` 秒。发现异常进入采样复核，持续至 `EDGE_HEALTH_RECHECK_S=6` 秒提示人工核查。连续三份被接受的新鲜独立样本可结束一次故障。调参和运动操作由对应提供者接收独立授权。[实机部署](../../docs/deployment.md)

~~~sh
# Linux 板载实例。使用设备实测话题，可通过环境变量替换 /scan 与 /imu。
EDGE_HEALTH_RGB_TOPIC=/camera/color/image_raw EDGE_HEALTH_POINTS_TOPIC=/lidar/points \
  docker compose -f compose.hardware.yaml up -d sensor-health
~~~

## 复测与证据

通用测试使用一个无外部网络的 Docker，在两个进程间传输实际 ROS 2 消息。调短时限后，14 类异常均进入持续告警并在恢复输入后确认。另有 2 个稀疏信息状态和 1 个正常基线，共 31 个检查点。视觉前端测试先在小图像上计算补丁匹配，再用 ROS 上报实测内点数。三种机体的独立回放使用从运行中 Gazebo 捕获的原始帧。实机曝光控制、现场阈值和生产级 SLAM 重定位仍需上机实验。[通用注入结果](../../docs/evidence/sensor-health.json) · [三机原始采样回放](../../docs/evidence/edge-field-sensor-replay.json)

~~~sh
# 在仓库根目录执行，使用含当前测试和插件的本地镜像。
mkdir -p artifacts/verification/sensor-health
docker run --rm --network none --read-only --tmpfs /tmp -e ROS_LOCALHOST_ONLY=1 \
  -e ROS_LOG_DIR=/tmp/ros -v "$PWD/artifacts/verification/sensor-health:/proof" \
  edge-agent-ros:v0.1 python3 -B /opt/edge/tests/system/sensor_health_acceptance.py --out /proof/result.json
~~~
