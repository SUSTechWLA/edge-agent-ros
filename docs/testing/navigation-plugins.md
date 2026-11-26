# Jetson 只读导航插件验收

### 直接下发室内物体测距任务

在车端直接提交“请帮我找到盆栽，量一下它离相机有多远。”。Agent 将原句绑定到只读工具 `see_object`。工具通过 `perception.object_location` 取得同一次调用选择的 D435 彩色图、对齐深度与 CameraInfo。CPU 模型返回整株候选框，几何提供者在候选内找到具有周围深度支持的可见绿色表面，量出相机光心到选定像素的直线距离。存在多个符合描述的候选、模型与原图摘要不匹配或深度不足时，保留阻塞原因。这个读数只对应画面中选定的表面。

2026-11-20 实车直接任务 `jetson-find-plant-20261120-01` 中，整株盆栽位于 640×480 实拍的左侧。模型框为 `[21,0,304,404)`，测距像素为 `(154,42)`，光轴深度 1.439 m，斜距 1.573 m。从客户端 POST 到步骤 verified 历时 2.192 s，任务成功。回放在原始提交、工具派发、读取 RGB-D、二维标注和三维测量各阶段停留，保留车端实际时刻。主片长 21 秒。对应的摘要见[实测记录](../../artifacts/verification/navigation-plugins/jetson-perception.json)。

部署以 `scripts/prepare_perception_site.py --objects` 增加独立站点包，复用原站点锁。`compose.object-perception.yaml` 将计算服务接入事实网络并只读挂载已发布采样；站点 CPU 模型进程通过 `scripts/run_object_model_isolated.sh` 在私有网络、设备和文件命名空间中运行，双方仅以 Unix socket 交换单帧 PNG 和带摘要的识别结果。重建录像时将 `submit.json`、`task.json`、`recording/`、`artifacts/` 和 `sensor-timeline.jsonl` 保存在同一导出目录，运行 `python -m task_recorder.direct_target_demo --source 导出目录 --out 新目录 --font 中文字体文件`。

### 静止融合与自然语言目标定位

在车端可输入 `获取 rover 的静止融合感知`。工具调用 `perception.fused_scene`，从冻结的 `sensing.observations` 取得两台 MID360 的原始来源、收包时间和采样摘要。采样时间段由连续里程计覆盖后，按车上 URDF 固定外参将命中点投到 `base_foot`。栅格保留每个观测源的位掩码，空格表示未知。该图包含近车体回波，不能直接用作规划器的可通行空间。此次实测的两路点云命中 103 格，其中 18 格共享；196 个三维体素中有 11 个共享。

输入 `在二维和三维标注画面右侧的大块浅灰色平面` 可调用 `mark_target`。它分别返回彩色像素框、区域掩膜、选中像素的原始深度、相机光学坐标及三份输入摘要。当前算法支持位置、浅灰色和大面积平面描述，拒绝未知目标。D435 彩色和对齐深度以 ROS 时间选帧，检查两者时间差，CameraInfo 通过标定摘要、尺寸、时效和坐标系绑定。米制比例来自已部署 RealSense ROS 驱动。未接入相机到车体的经验证外参。RTK 无有效发布源，其驱动的默认串口已由底盘占用。实测数据和任务摘要见[板端记录](../../artifacts/verification/navigation-plugins/jetson-perception.json)。

**部署及续办。** `scripts/prepare_perception_site.py` 校验原站点锁，构建仅新增只读 Service 的初始站点和包含可选 Tool 的候选站点。初始站点同时提供融合 Tool。使用 `compose.stationary-perception.yaml` 启动独立计算服务与里程计见证；软宿主持有预置候选的只读信任目录。原任务未派发且确实受阻后，`scripts/evolve_read_projection.sh` 从已有 Service 和部署者预授权的工具模板产生发布提案。在独立感知网络验收真实图像、测点、负例及摘要，通过后事务接纳并绑定原文续办。父任务保留原终态。录制器在父任务保存用户原文，在子任务保存带发布事件的 Agent 受理记录。使用 `python -m task_recorder.target_review --help` 或 `fusion_review --help` 从原始任务、录制和采样导出重建回放。

**回退。** 对需要撤回的单个工具使用现有软件注册表的 drain、待原调用结束、remove 路径。卸载消费者后再处理仅由该消费者使用的 Service，按保留的站点锁重新接纳其余版本。Agent 的任务卷与注册事件卷分别保留父任务、续办和发布来源。仅需故障隔离时，可停止独立的 stationary-perception 软件计算服务，原传感器采集仍可供其他插件读取。

本轮在 Jetson Orin NX 上分别调用了空间观测、定位和路线试算。车端采集器订阅专用镜像域中已有的 `/lingao/odom`，保留驱动提供的坐标系、采样时间和 XY 协方差。采集进程写专用卷，事实网关只读该卷。Agent 通过 Unix socket 使用软插件，软宿主只连接内部事实网络。板端未部署 `nav_execute` 的物理控制器。

## 复现只读站点

在包含本次源码的 Linux ARM64 镜像和对应项目目录运行。以下操作需要现场已有的只读 ROS 话题镜像。示例生成一张绑定 `odom` 的小型合成栅格，地点 `bench-marker` 只属于该测试地图。操作人应选择与现场一致的源话题、机体坐标和时钟。此适配器要求 ROS 消息采用本机 Unix 时钟。

```sh
python3 scripts/navigation_odometry_bench.py --topic /lingao/odom --frame odom \
  --child-frame base_foot --actor rover --out ./nav-bindings
python3 scripts/prepare_navigation.py --image "$IMAGE" --source-commit "$SOURCE_COMMIT" \
  --live-settings ./nav-bindings/settings.json --site ./nav-site >nav-lock.json
export EDGE_NAV_SITE="$PWD/nav-site" EDGE_NAV_CAPTURE_CONFIG="$PWD/nav-bindings/odometry.json"
export EDGE_NAV_DIGEST="$(docker image inspect --platform linux/arm64 --format '{{.Descriptor.digest}}' "$IMAGE")"
export EDGE_NAV_IMAGE="$EDGE_NAV_DIGEST" EDGE_NAV_MIRROR_DOMAIN=140
export EDGE_NAV_LOCK="$(python3 -c 'import json;print(json.load(open("nav-lock.json"))["lock"])')"
docker compose -f compose.navigation-readonly.yaml --profile published up -d
curl -fsS http://127.0.0.1:8794/agent/capabilities
curl -fsS -H 'Content-Type: application/json' http://127.0.0.1:8794/agent/tasks \
  -d '{"id":"nav-smoke-01","text":"为 rover 试算到 bench-marker 的路线"}'
curl -fsS 'http://127.0.0.1:8794/agent/tasks?id=nav-smoke-01'
```

`nav-smoke-01/step/1` 的回执含发布定位、原采集时间、时效窗口、机体与地图摘要、路径及算法。请求“读取 rover 的空间观测”或“查询 rover 的定位”可独立验证前两项能力。回放站点省略 `--live-settings`，并以 Compose 默认 profile 启动，地图与输入来自四种机体的基准样本。现场地点的可达性需要在真实地图、有效 TF 和相应机体 profile 上另行验收。随包内嵌的地图受站点 JSON 大小上限约束，大地图服务可实现相同的目录与路线契约。

第二路校准观测使用可选的 [CameraInfo 部署](../../compose.navigation-camera-info.yaml)。它按站点指定的域、话题、光学帧和主体订阅 ROS 发布的相机内参，把校准文件按内容摘要保存。事实网关可提供带不同原始帧的观测，定位仍只消费策略允许的位置来源。部署可单独撤下该读者，网关将记录缺样。它的状态与航迹控制权各由自己的进程持有。

## 验收与回退

- 现场路线任务产生 16 个路点，总长 2.731 米，起点观测帧龄 76.6 毫秒。第二次录制同时读到实时轮式里程计和 D435 彩色相机校准，内参工件的内容摘要与原回执一致。离线执行 `python tests/system/navigation_recording_check.py EXPORT_DIRECTORY` 可复核两份终态、采样窗口、膨胀栅格上的路径以及校准文件身份。[本次实机摘要](../../artifacts/verification/navigation-plugins/jetson-readonly.json)
- 软件组合、独立装卸、重复调用及四种机体的路线证据见[导航集成用例](../../tests/test_navigation_pack.py)。物理租约、取消、原操作对账和独立到点及停稳见证见[隔离执行用例](../../tests/test_navigation_physical.py)。
- 车端证据按任务 ID 保存。录像是 Agent 操作及证据的时间线。只有另行启用了相应图像或运动源的录制才包含车载画面或运动轨迹。
- 撤回只读现场测试时，用启动时的项目名、环境文件和 Compose 文件调用 `docker compose stop`。保留任务、注册和证据卷。换采集进程或重设坐标后，为新定位代次重新生成绑定与站点，再按新部署启用；旧任务仍可按原站点及原操作回读。物理夹具独立按原操作排空、确认安全和交接。[物理协议](../architecture/plugin-physical-handoff.md)
