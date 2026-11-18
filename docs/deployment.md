# Edge Agent ROS 快速部署

开发电脑用 Docker 启动 Gazebo、传感器、运动网关、任务内核、感知、任务代理和浏览器工作台。实体机器人可只运行需要的服务；本机或同一 ROS 网络的驱动提供传感输入，机体的执行器实现导航 Action。两种部署各有独立的 Compose 文件和数据卷。

| 平台 | 入口 | 运行方式 |
|---|---|---|
| Windows x86-64 开发机 | `.\scripts\start.ps1` | Docker Desktop 的 Linux 容器，浏览器打开 localhost:8765 |
| macOS Intel / Apple Silicon、Linux 开发机或服务器 | `sh scripts/deploy.sh sim up` | 按 Docker Engine 本机架构构建 Gazebo 环境；无桌面服务器同样可通过浏览器访问 |
| RK3588、Jetson Orin 等 ARM64 Linux 边缘机及 x86-64 Linux 工控机 | `hardware build` 后运行 `sh scripts/deploy.sh hardware observe` | 首次只启动工作台和感知，以 host 网络接入现有 ROS。传感器驱动和机体执行器单独部署 |

## 仿真或工作站

运行中需要持续巡检时，在仿真或板端构建包含 Watchdog 的镜像，再单独启用 `watchdog` profile。Watchdog 的持久订阅使用 Docker 卷的子目录挂载。两份 Compose 的实际安装、停止、查事件与卷权限验证见[巡检手册](watchdog.md)。

安装可运行 Linux 镜像的 Docker Engine 或 Docker Desktop，启用 Docker Compose 插件。本文命令从项目根目录执行。Windows 可以直接使用已提供的 PowerShell 脚本，不需要在宿主使用原生 WSL 开发环境。首次构建需要镜像和软件源网络，可使用 Docker Desktop 配置的 VPN/代理。基础镜像缓存后，常规任务可以离线运行。

```sh
sh scripts/deploy.sh sim up
sh scripts/deploy.sh sim status
# 已构建或 docker load 导入镜像时，可跳过编译
sh scripts/deploy.sh sim start
# 查看近期日志，停止服务会保留任务和观测数据库
sh scripts/deploy.sh sim logs core console
sh scripts/deploy.sh sim stop
```

默认访问 `http://127.0.0.1:8765/`。远程 Linux 服务器保留本机回环监听，在开发电脑执行 `ssh -L 8765:127.0.0.1:8765 user@edge-host` 再访问同一地址。更换端口时设置 `EDGE_CONSOLE_PORT` 并相应调整 SSH。Docker Desktop 使用 Linux 虚拟机，Mac 和 Windows 调试完整链路时优先使用此仿真网络。

## 在真实 Linux 设备接入 ROS

**第一阶段，在板上看真实传感器。** 使用 Linux Docker Engine、Compose v2 和本项目此次提交。ROS 2 Jazzy、工作台和 Agent 运行在容器内。先在板端原生构建硬件镜像；已有同架构镜像时可用后文的离线导入。镜像内入口与代码应来自同一提交，示例中的设备标识、话题与标定数值需要替换为现场值。

```sh
# 示例：按真实网络、设备标识、驱动的 topic/frame 和标定结果填写
export EDGE_ROS_DOMAIN_ID=36 EDGE_ROBOT_ID=rover-01
export EDGE_ODOM_TOPIC=/robot01/odom EDGE_SCAN_TOPIC=/robot01/scan EDGE_IMU_TOPIC=/robot01/imu
export EDGE_ODOM_FRAME=odom EDGE_BASE_FRAME=base_link EDGE_LASER_FRAME=front_laser EDGE_IMU_FRAME=base_imu
export EDGE_LIDAR_X=0.20 EDGE_LIDAR_Y=0.0 EDGE_LIDAR_Z=0.23 EDGE_LIDAR_YAW=0.0
export EDGE_HARDWARE_IMAGE=edge-agent-ros/hardware:field-$(git rev-parse --short HEAD)
sh scripts/deploy.sh hardware build
sh scripts/deploy.sh hardware observe
docker compose -f compose.hardware.yaml exec -T perception /usr/local/bin/edge-entrypoint ros2 topic list
```

`observe` 启动 `perception` 和 `console`。通过此工作台提交或重试会得到 HTTP 403 和 `console.observe_only`。已在同一项目运行的控制服务会使模式切换停止，并保留原服务。它只约束本工作台，已有现场 ROS 驱动、外部任务入口和物理急停仍由机体负责。

**第二阶段，用数据验收。** 工作台打开后执行下面的命令。本命令检查当前运行容器内的配置。在另一窗口更改环境变量后，需要重新执行 `hardware observe` 让配置生效。SSH 转发方式见上文。

```sh
mkdir -p artifacts/verification/field
sh scripts/deploy.sh hardware preflight > "artifacts/verification/field/$(date -u +%Y%m%dT%H%M%SZ)-inputs.json"
# 首次只接里程计时，明确缩小此次验收范围
sh scripts/deploy.sh hardware preflight odom
```

退出码 0 表示选中的每个输入至少出现两组推进的时间戳，且通过话题、frame、新鲜度和身份校验。默认每 0.25 秒读一次，最长等 6 秒，可加 `--timeout 20` 等待慢启动驱动。驱动应以 ROS 系统时钟给采样打戳。样本晚于工作台系统时钟超过 0.25 秒，或早于该源的新鲜度预算，会产生 `.clock_mismatch`。预检报告保留每个源的 `wall_minus_stamp_s`，适合检查明显的时域错误；估测多传感器亚毫秒级同步需要专用测量。

| 预检现象 | 到现场核对 |
|---|---|
| `.missing` 或 `.topic_mismatch` | 核对报告的机器人 ID、ROS 域和完整话题。用 `ros2 topic info -v` 检查发布者、消息类型和 QoS。 |
| `.frame_mismatch` | 核对驱动 header.frame_id，轮式里程还要核对 child_frame_id，检查实际安装和 TF。 |
| `.stale`、`.stamp_not_advancing` 或 `.clock_mismatch` | 核实设备与板端时钟、掉线、驱动重发旧帧和采样频率，再运行同一预检。 |
| `console.session_changed` | 检查工作台是否重启，以完整的新会话重新采样。 |

**第三阶段，开放任务。** 在保持执行器未使能的情况下，运行 `sh scripts/deploy.sh hardware start`。完整入口启动 core、perception、agent、console、session 和 evidence，并注册后两项读取服务。下发“报告当前位置”，比对回执中的话题、frame、ROS 时间与原始里程计，在 Session 页面查回相同 task 与 trace。上板验收通过后再接入机体自己的导航 Action，依次测试停止与取消、低速短程、失联停车及恢复。恢复路径和接管原因随任务证据归档。

**定位与导航能力边界。** 当前具名任务依赖注册地点、航点执行与结果反馈。里程计、激光和 IMU 均新鲜，只能证明这些输入可用。毫米波、激光或视觉 SLAM 还需自己的定位器，提供 `map→odom→base_link` 坐标关系、时间戳、跟踪状态及算法支持的匹配内点比例、残差等质量观测。把质量观测接入独立的感知健康插件后，单独验证退化、重定位和运动放行策略。[健康信号与插件契约](specs/SDD-035-sensor-health.md)

**离线交付。** 可在同架构 Linux 构建机上运行上述 build，把同一代码版本和下述归档一起复制到板端。在构建机和板端均设置相同的 `EDGE_HARDWARE_IMAGE` 标签；板端保存任务账本的 Docker 卷独立于镜像。

本开发机的版本化现场包位于 `artifacts/deployment/`。按本轮交付的目录名选择版本，把整个目录复制到板端，按包内 `START-HERE.md` 操作。包内含硬件镜像、固定提交源码与 SHA-256 清单。当前运行结果记录在版本化证据中。[首次上板步骤](board-first-boot.md) · [已核验的镜像身份与结果](evidence/board-preflight.json)

```sh
docker image inspect "$EDGE_HARDWARE_IMAGE" --format '{{.Os}}/{{.Architecture}} {{.Id}}'
docker save "$EDGE_HARDWARE_IMAGE" | gzip > edge-agent-hardware.tar.gz
sha256sum edge-agent-hardware.tar.gz > edge-agent-hardware.tar.gz.sha256
# 板端在归档所在目录执行；随后从代码仓库运行 observe 和 preflight
sha256sum -c edge-agent-hardware.tar.gz.sha256
docker load -i edge-agent-hardware.tar.gz
```

浏览器能看到新到达的原始样本。在回环接口查询 `/api/state` 可以核实 `environment=hardware`，消息的原始时间戳、参考系、观测来源和更新年龄用于诊断驱动或 DDS 配置。请在所有输入使用同一可信采样时域后再依赖栅格坐标。ROS 控制器、驱动和容器应使用同一 `ROS_DOMAIN_ID`，网络和 QoS 配置须允许发现、订阅。硬件模式没有 Gazebo 世界真值，参考里程计和融合位姿可单独检查。感知支持水平雷达和与车体 Z 轴对齐的角速度，其他安装姿态需通过驱动或适配器变换。[感知参数与坐标约定](../perception/README.md)

| 要替换的部件 | ROS 契约或注入点 |
|---|---|
| 雷达、IMU、轮式编码器 | `sensor_msgs/LaserScan`、`sensor_msgs/Imu`、`nav_msgs/Odometry`。上例的 topic 名同时配置感知和控制台。若替换整套感知节点，可只启动 `console`，由外部提供者发布 `/edge/perception/pose` 和 `/edge/perception/map`。 |
| 导航执行器 | 在设备网关实现 `edge_interfaces/action/NavigateTo`；默认监听 `/edge/navigate_to`，可用 `EDGE_NAV_ACTION` 改名。取消、超时、操作心跳和停稳的实测证据是网关契约。机体保护在底层控制器落实。硬件镜像不启动仿真速度控制器。 |
| 机器人能力 | 内置巡检和状态之外，可将能力的 JSON 清单放在 `plugins/` 或 `EDGE_PLUGINS_DIR` 指定的宿主目录，注册一个已部署的 HTTP 执行服务。清单描述参数、effect、版本和 endpoint，适配器负责操作幂等与证据。 |

接入实际导航网关后，执行 `sh scripts/deploy.sh hardware start core agent perception console` 即可通过浏览器提交任务。四个服务共享宿主的 Linux 网络。console 仅监听 `127.0.0.1:${EDGE_CONSOLE_PORT:-8765}`，agent 仅监听 `127.0.0.1:8090`。从宿主检查 `curl http://127.0.0.1:8090/agent/capabilities` 可以看到实际能力；物理到达和停稳仍需要用该机体完成验收。[执行网关契约](v1/gateway.md) · [Linux host 网络](https://docs.docker.com/engine/network/drivers/host/)

## 板卡、镜像和推理

为板卡准备镜像前，可以先在开发机重放缺失里程计与恢复任务的场景。报告给出了同一程序分配 384、512、768 和 1536 MiB 时的原生 AMD64 与模拟 ARM64 结果，以及上机仍需测的项目。[单机实测与资源限额复演](testing/sdd-003-results.md)

**选哪一种镜像。** 在板端依次执行 `uname -m` 和 `docker info --format '{{.OSType}}/{{.Architecture}}'`。aarch64 的 RK3588 或 Jetson/Orin 使用同一份 linux/arm64 CPU 镜像。x86_64 Linux 工控机应构建 linux/amd64 镜像。两种镜像均通过 ROS 或 HTTP 接入设备。板型特定的内核、传感器驱动、GPU/NPU 运行时由板端提供。软件验收在 amd64 原生容器和 arm64 指令模拟中完成，真实板卡仍须验证采样、散热、负载与时延。

**从任务模式退回观察。** 先在工作台检查未完成/待恢复的任务，让机体控制器执行取消或停机，再用机体速度、执行器使能状态及现场反馈独立确认停稳。随后在仓库根目录执行 `sh scripts/deploy.sh hardware stop core agent`，再执行 `sh scripts/deploy.sh hardware observe`。二次开机再次确认设备标识与历史账本归属，避免恢复另一机体的在途任务。单独关闭 Docker 服务无法替代机体的制动与急停。

- RK3588 使用整板厂商提供的 BSP、内核和设备驱动。当前工作台、地图与任务内核可以在 CPU 上工作。要使用 NPU，在独立推理服务中安装与驱动配套的 RKNN/RKLLM 用户态组件并验证模型支持范围。[Rockchip RKNN 工具链](https://github.com/airockchip/rknn-toolkit2)
- Jetson Orin 使用与载板兼容的 JetPack/L4T。需要 GPU 推理时，在单独的推理容器中配置匹配宿主驱动的 NVIDIA 运行时与库；例如 JetPack 6.2.1 对应 Jetson Linux 36.4.4。主项目容器通过 ROS 或 HTTP 与这些服务交互。[NVIDIA JetPack 6.2.1](https://developer.nvidia.com/embedded/jetpack-sdk-621)
- 板端只为硬件驱动进程映射它需要的相机、串口或加速器设备节点。工作台和任务内核通过 ROS 读数据，默认无需 GPU 或直接访问设备。需根据具体板型、传感器频率、热状态、RAM 与历史日志容量测试部署预算。[板卡验收手册](architecture/board-validation.md)

部分网络可通过可达的软件镜像缩短构建时间。构建 ARM64 Ubuntu 软件包要选择 `ubuntu-ports`，amd64 使用 `ubuntu`。例如 ARM64 可先执行 `export UBUNTU_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports ROS_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ros2/ubuntu`。项目保留发行版签名校验。

模型规划器可使用独立 Ollama 服务。在硬件 host 网络中本机模型可填 `EDGE_OLLAMA_URL=http://127.0.0.1:11434`；其他设备上的服务填可达地址，同时设 `EDGE_OLLAMA_MODEL` 为服务已有模型，再运行 `sh scripts/deploy.sh hardware start agent` 使配置生效。仿真使用 Compose 网络，若已启用 `model` profile 并已拉取所选模型，服务地址为 `http://model:11434`。模型首次下载需要网络。端侧内存或算力有限时可以外置模型；本地解析已支持的明确坐标巡检和状态查询无需启动模型。[Ollama Chat API](https://docs.ollama.com/api/chat)

## 已经验证过哪些环节

| 环境 | 输入与检验方法 | 观测到的结果 |
|---|---|---|
| Windows x86-64，Docker Desktop 的 Linux 容器 | 在独立 Compose 项目运行 shell 仿真启动脚本，通过指定端口访问工作台 | 八个服务均启动，控制台返回实时状态。Gazebo 的实际激光与 IMU 可在软件渲染环境发布。 |
| 同一开发机的原生 Linux/AMD64 容器 | 构建 `edge-agent-ros/hardware:v0.1`，运行硬件配置四个服务，设置 `/robot37/scan`、`/robot37/imu` 与 `/robot37/odom`，从外部 ROS 节点发布里程 | 控制台 API 返回重映射后的三个 topic；轮式里程和融合位置均为样本提供的 `x=0.39 m`。 |
| 同一开发机上的 Linux/ARM64 指令模拟 | 从 ARM64 官方 ROS 镜像编译 `edge_interfaces` 和 `edge_core`，启动硬件部署中的四个服务。用 ROS 发布带采样时间与坐标系的轮式里程、IMU 与激光，查询 `/edge/get_mission`，通过工作台提交 `status` 任务 | 容器内 `uname -m` 为 `aarch64`。本轮匹配 48 帧扫描，丢弃 0 帧，得到 8 个占用格和 226 个空闲格。代理读取 `/robot37/odom` 的 `x=0.192 m`，状态为 `SUCCEEDED`，任务留下 `accepted → planned → dispatch → verified` 四项记录。 |
| RK3588 / Jetson Orin 实机 | 接入板载驱动、真实时钟与控制器，重复观测和任务验收，采集温度、功耗和延迟 | 待实机测量。上述 ARM64 试验包含软件链路、合成 ROS 输入与指令模拟；传感器标定、物理运动或加速器驱动需在目标机验证。 |

两种架构还执行了两次故障检验。没有相机能力时，请求“汇报状态并拍照”在 `capability_check` 阶段终止，执行步骤为零。将自动重试次数在测试中设为零，断开里程计后查询状态，任务进入 `RECOVERY_REQUIRED`；恢复里程计并续办后，两个平台都沿用同一个 `operation_id` 取得结果，各自只派发了一次。可以用镜像摘要和源码 SHA-256 核对这轮测试的采集 topic、融合结果、任务事件和原始时间戳。[跨平台实测记录](evidence/console/platforms.json)

## 留档与恢复

工作台还支持在运行时挂接额外的 ROS 传感器。将含有 `id`、`name`、`topic`、`type` 和 `ttl_s` 的 JSON 放入 `observations/`，例如 `{"id":"front_range","name":"前向测距","topic":"/front/range","type":"sensor_msgs/msg/Range","ttl_s":0.7}`。类型支持包需安装在控制台镜像内。额外传感器会显示等待、在线和过期状态；删除 JSON 后释放订阅。双架构实测记录了加入、在线、过期、恢复和移除的五项事件，并从回放读取了恢复后的距离 `1.125 m`。

使用 `docker compose -f compose.hardware.yaml ps` 与 `sh scripts/deploy.sh hardware logs agent core perception console` 查看服务状态。观察历史、任务账本、自然语言任务记录分别保存在项目的命名卷，`sh scripts/deploy.sh hardware down` 会保留它们。工作台提供筛选、关联任务、回放和 NDJSON 导出。开发与现场应记录镜像摘要、软件配置、板型和现场时间同步状态，才能重放问题上下文。
