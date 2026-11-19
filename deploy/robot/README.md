# Edge Agent ROS · 机器人部署版

在装有 Docker Engine 和 Compose v2 的 Linux 计算单元运行。该分支由研发仓库生成。`ROBOT_SOURCE` 记录对应的完整研发提交。本地标定和任务数据分别保存在每台车的工厂卷和 Compose 卷。

## 第一次启动

ARM64 离线包可在 RK3588 或 Jetson 的 Linux 系统上按下列步骤展开。镜像包含 CPU 运行时；GPU 加速与具体板卡驱动按现场配置。已有部署代码与镜像的板子直接运行下一组启动命令。

```sh
sha256sum -c SHA256SUMS
git clone -b deploy/robot edge-agent-ros-robot.bundle edge-agent-ros
docker load -i hardware-linux-arm64.tar.gz
cd edge-agent-ros
```

```sh
export EDGE_ROBOT_ID=rover-001 EDGE_ROS_DOMAIN_ID=72
sh scripts/robot.sh observe
sh scripts/robot.sh preflight
```

第一条启动命令在本地镜像缺失时构建它，仅开启观测服务；首次构建需要可用的 ROS 基础镜像与软件源。离线工位可事先加载**同一源码版本**对应架构的镜像并设置 `EDGE_HARDWARE_IMAGE`。工作台默认在板端回环地址的 8765 端口。ROS 驱动运行在板端或兼容的 host-network 容器内，设备话题、frame 和 ROS 域按车配置。基础预检默认检查二维扫描、里程计和 IMU；三维雷达和相机由工厂采集程序分别验证。车上尚无二维扫描时可先运行 `sh scripts/robot.sh preflight odom imu`。

## 在台架验证插件组合

默认的 `sh scripts/robot.sh observe` 只启动 Console 和感知。需要验收软件插件时，可在同版硬件镜像上增加下列步骤。`profiles/observe.json` 选择零个插件，`profiles/bench.json` 选择 `capabilities/soft-pilots/pack.json` 中的 Session、证据查询和数列计算。先确定部署分支的提交和 `ROBOT_SOURCE`，再将工位目录指定在代码仓外。以下命令使用已验收的本地镜像、Docker Compose v2 和 Python 3 标准库。

```sh
export EDGE_HARDWARE_IMAGE="edge-agent-ros/hardware:robot-$(cut -c1-12 ROBOT_SOURCE)"
sh scripts/robot.sh build
export EDGE_SOFT_SITE="$HOME/edge-sites/bench-001"
python3 scripts/prepare_soft_pilots.py --image "$EDGE_HARDWARE_IMAGE" --source-commit "$(cat ROBOT_SOURCE)" \
  --profile profiles/bench.json --site "$EDGE_SOFT_SITE"
# 将上一条命令输出的 lock 与版本记录一起保存；以下使用该工位批准的 64 位十六进制值。
export EDGE_PACK_LOCK='<approved lock>'
sh scripts/robot.sh plugins check
sh scripts/robot.sh plugins bench
sh scripts/robot.sh plugins status
```

软件台架验收完成后，执行 `sh scripts/robot.sh plugins start`。该命令核对活动插件、锁定清单和任务账本，在同一配置中启动 Agent 并开启 Console 的任务提交接口。可在工作台下发“求输入数列的平均值 [2,4,6]”，检查任务终态、`derived.series_mean` 的执行证据和数值 4。该启动路径只拉起 Console、Agent 与只读事实桥，软件提供者沿用台架实例，底盘 Core 和设备驱动保持原有状态。发现已有 Agent、Core 或未交接任务时，应核对原任务与设备状态，再在原部署完成交接。

锁定清单包含每个插件的 semver、实际代码／镜像摘要、配置摘要和策略。可通过 `--override /站点目录/override.json` 对同一 profile 派生新的工位目录。例如 `{"disable":["evidence.query_evidence"]}` 会关闭 SQL 插件。配置按描述符校验，禁止新增未知 ID、扩大效果类别、替换执行入口或取消强制审批。应先核对新旧锁并确认原任务完成；已有活动版本不一致时，整批准入失败并保留原来活动的版本。变更以新目录发布，旧目录、原镜像和任务数据卷保留，方便回退。回退到上版锁同样需完成原任务、排空、确认卸载后再按上版清单接纳。

台架启动器在既有 Core 或 Agent 运行时拒绝切换。台架只启动感知、只读 Console、经过锁定的软件提供者和 GET 事实网关；不能通过 Console 提交机器人任务。调用注册表的 `soft-admin` 无外网且不挂载设备。软件提供者仅加入内部网络，GET allowlist 通过 Unix socket 读取板端回环 Console 的公开会话/事实。台架上用户看到“已接纳”后还应检查 Fiber 就绪、独立健康探测和本机观测。实际车上需要运动或读取原始设备的能力，应在相应 physical 提供者部署后，通过现场独立验收、停止协议、设备租约和原操作对账交接。为软件插件签发锁不会授予设备访问权。

### 相机与独立验收器

相机台架使用单独的站点目录。相机进程订阅站点批准的 ROS Image 话题，验收器通过独立证据核验，控制器保存租约、停止记录和原操作账本。板端交付包不附模拟相机发布器。首先用实际板端镜像生成独立的相机部署批准并留下打印的摘要。

```sh
CAMERA_APPROVAL="$HOME/edge-sites/camera-approval-001"
python3 scripts/prepare_physical_camera.py --image "$EDGE_HARDWARE_IMAGE" --source-commit "$(cat ROBOT_SOURCE)" \
  --source-id camera.front --topic /physical/camera/image --ros-domain-id 161 --site "$CAMERA_APPROVAL"
export EDGE_SOFT_SITE="$HOME/edge-sites/combined-bench-001"
python3 scripts/prepare_soft_pilots.py --image "$EDGE_HARDWARE_IMAGE" --source-commit "$(cat ROBOT_SOURCE)" \
  --physical-site "$CAMERA_APPROVAL" --profile profiles/bench-camera.json --site "$EDGE_SOFT_SITE"
export EDGE_PACK_LOCK='<approved combined pack lock>'
sh scripts/robot.sh plugins check
sh scripts/robot.sh plugins bench
sh scripts/robot.sh plugins camera-check
sh scripts/robot.sh plugins camera-bench
sh scripts/robot.sh plugins camera-status
```

需要让 Agent 使用这个相机时，在同一组合站点和锁上完成上述两组台架核查，然后执行 `sh scripts/robot.sh plugins camera-start`。此入口把软件与相机的 Unix socket 一起交给 Agent。它会检查原相机控制器处于 `ACTIVE`、旧操作已对账，且切换完成后原控制器容器身份一致。启动命令本身不会提交采集或运动任务。站点缺驱动、独立见证或停稳确认时，先检查 `camera-status` 的状态与原操作；保留原进程和数据卷进行核对。

`camera-check` 对照实际 Docker 平台镜像、部署提交、相机和验收器入口、传感器标识、ROS 话题、域、挂载及网络隔离。只有 `camera-bench` 明确下达后才启动相机台架，并再次核对已启动容器。站点须在板端提供正确的设备驱动、时间线和独立见证；缺项会使采集保持未证实。未装驱动时可以执行只读检查并查看台架状态。镜像不一致或活动控制服务占用当前项目时，请先查原部署与未完成租约。回退先执行 `sh scripts/robot.sh plugins camera-drain`，等待 `camera-status` 显示已确认停止，再执行 `sh scripts/robot.sh plugins camera-stop`。该命令依次释放租约、停止原相机进程、登记 Docker 退出证据。原操作保持原绑定。已锁定的 physical 条目只交给 P2 部署通道；soft 管理器不能安装它。

## 工厂标定

把 `calibration/station/*.example.json` 复制到独立工位目录，分别命名为 `profile.json` 和 `bindings.json`。用实际测量和实物序列号填写空值，给 `EDGE_FACTORY_DIR` 设置该目录的绝对路径。工装适配器可选提供 `targets.json`，运行 `sh scripts/robot.sh fit-targets` 求一个传感器的安装外参并交由工位核对。随后执行

```sh
sh scripts/robot.sh factory
sh scripts/robot.sh calibration-status
```

标定版本与机体、设备身份和工装观测绑定。更换支架或传感器后重新采集。查阅同一研发提交的 `docs/rover-commissioning.md` 可以获得工位文件格式、探索仿真及真机接入说明。首次运动需安装并验收本车定位、导航和急停适配。此版本默认交付硬件只读启动。

## 启动完整 Agent

现场验收通过后，`sh scripts/robot.sh start` 启动 Core、Agent、工作台、Session 与取证等服务。可用 `sh scripts/robot.sh status` 和 `sh scripts/robot.sh logs` 查看所选部署。停稳并完成任务交接后，可执行 `sh scripts/robot.sh stop` 停止服务，数据卷保留。切换回只读观测时重新执行 `observe`。工厂标定结果需要由具体的探索适配器检查，完整 Agent 的通用任务入口没有全局标定联锁。

## 在板端录制一个任务

若相机或激光已经由现场批准的设备插件管理，录制器可以接收该插件提供的观测摘要。开启方式示例为 `sh scripts/record-task.sh hardware start visit-lobby lobby-01 --external-sensor laser_scan --external-sensor imu`。采集插件向该录制的 `/recordings/lobby-01/events` 提交与任务匹配的时间、来源和物理量。核对 `counts` 和 `missing_sources` 后再导出。部署账号需为生产者提供受控的归档连接。`--laser-scan` 与 `--imu` 是 Gazebo 实验专用选项。视频在开发工作站从已封存的导出目录渲染，板上可专注于采集。

先按现场配置启动 Agent，取一个你准备提交或已经提交的任务 ID。例如，以下命令为 `visit-lobby` 创建独立录制 `lobby-01`。录制器从第一个命令开始观察。提交任务仍使用现有工作台或 Agent 接口。

```sh
EDGE_RECORD_CAMERA=/camera/color/image_raw sh scripts/record-task.sh hardware start visit-lobby lobby-01
sh scripts/record-task.sh hardware status lobby-01
sh scripts/record-task.sh hardware export lobby-01 /data/exports/lobby-01
```

相机为可选的 `sensor_msgs/Image` 话题。去掉第一个命令的环境变量，仍会采集时钟、融合地图、位姿、速度命令、该任务的总线事件和 Agent 状态。纯软件任务用 `EDGE_RECORD_SOFTWARE_ONLY=1` 开录。传入与板端部署相同的 `EDGE_ROS_DOMAIN_ID`，容器会将其设置为 `ROS_DOMAIN_ID`。同时录制时，位姿、地图、相机等是同一物理平台的观察，任务状态和带任务标识的事件才可归因到指定任务。

默认部署关闭录制。第一次 `start` 会发布 `task.record` 服务并为指定 ID 开启一个采集器。返回的 `sources` 是申请的来源；`counts` 和 `missing_sources` 说明哪些实际有数据。观察到任务终态后录制自动完成。也可 `sh scripts/record-task.sh hardware stop lobby-01` 提前停录；同车其他录制继续工作。对已关闭的录制使用 `export` 导出到新目录，查看 `recording.json`、`media/mission/timeline.jsonl` 与实际采集的图像。

下线时 `sh scripts/record-task.sh hardware drain` 会拒绝新录制。所有录制收尾后执行 `sh scripts/record-task.sh hardware disable` 撤销服务并停止归档进程，证据卷保留。在途任务会使 `disable` 返回代码 2，可等待自动收尾或按 ID 手工停录后重试。重新开放准入使用 `sh scripts/record-task.sh hardware enable`。复盘时分别查看 `status`（如 `FINISHED`）与 `task_status`（如 `SUCCEEDED` 或人工停录时的 `STOPPED`）。

## 目录和更新

| 路径 | 现场用途 |
|---|---|
| `scripts/robot.sh`、`compose.hardware*.yaml`、`docker/` | 板端入口、服务编排、容器启动环境 |
| `src/`、`agent/`、`client/` | ROS 接口、任务与执行记录、插件管理 |
| `calibration/`、`exploration/`、`perception/` | 标定、通用探索算法与传感器处理 |
| `console/`、`sessions_plugin/`、`evidence/` | 本机工作台、会话与取证 |
| `task_recorder/`、`scripts/record-task.sh`、`compose.task-recorder.yaml` | 可选的按任务录制、独立采集与归档 |
| `contracts/`、`policies/`、`model_provider/` | 运行契约、恢复策略和模型适配 |

每次上板固定部署提交和镜像标签；SDK、研发资料与仿真验收留在研发分支。待设备停稳并完成在途操作交接后切换版本。回退选用先前的部署提交及配套镜像。现场配置在板外保存备份，保留设备数据卷。
