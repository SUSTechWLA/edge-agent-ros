# 无人车出厂、首次进场与板端交付

这套流程把三个问题分开验收。**工厂**回答“这台车装的是哪几件设备，数据和安装是否可信”；**进场**回答“车现在在哪里，已看过哪些地方，还能安全地去哪里”；**部署**回答“这台板子运行的究竟是哪版代码”。同一台车可以在多个场地复用合格的安装档案，每次进新场地建立独立的地图和运行号。

## 1　研发和板端各有一个入口

| 使用者 | 维护位置 | 变更方式 |
|---|---|---|
| 开发、仿真、故障复盘 | 完整研发仓，当前功能分支 `feat/sdd-038-rover-commissioning` | 在此修改代码、契约、测试、教材，按 SDD 合并 |
| 现场部署 | 从研发提交生成的 `deploy/robot` 分支 | 只发布硬件运行源码、ROS 接口、配置与操作说明。文件结构与硬件镜像构建路径一致 |
| 工位工程 | 板外一个工位目录，映射为只读的 `/station` | 保存该车的档案与真实设备身份。每次变更按机号归档 |
| 运行证据 | 标定卷 `edge-factory-<机号>`、任务和观测的 Compose 卷 | 升级前保留卷，地图另按场地和运行号保存 |

发布者在完整仓库执行 `python scripts/release_robot.py <已通过验收的提交>`。生成器直接从 Git 对象选择文件，设备分支有独立历史。仅修改研发文档时不会触发上板更新。`ROBOT_SOURCE` 与镜像标签 `org.opencontainers.image.revision` 可以查回当时的完整实现、规格和测试。新运行目录或新资源类型加入时，维护者更新生成器的允许清单并重新做独立构建。

在线工厂可从已经发布该分支的内部远端单分支克隆。离线工厂使用交付目录中的 bundle 和匹配 CPU 的镜像，按以下命令启动。切换版本应在车已停稳、在途任务完成交接后进行。回退同时选择对应的部署提交和镜像，保留原卷。

维护者执行 `python scripts/package_robot.py` 可从 bundle 单独克隆、构建 amd64 与 arm64 镜像、运行两种架构的板端冒烟并生成离线包。该脚本使用 Python 3.11+、Docker Compose v2、已启用 arm64 仿真或原生构建能力的构建机。交付目录位于 `artifacts/deployment/rover-<源码短哈希>/`。开发机上的独立板端检出可以用 `git clone --no-local --single-branch --branch deploy/robot <完整仓路径> <板端目录>` 建立，之后在板端目录用 `git pull --ff-only` 同步本地生成的发布。

```sh
sha256sum -c SHA256SUMS
git clone --branch deploy/robot edge-agent-ros-robot.bundle edge-agent-ros
docker load -i hardware-linux-arm64.tar.gz
cd edge-agent-ros
export EDGE_ROBOT_ID=rover-001 EDGE_ROS_DOMAIN_ID=72
sh scripts/robot.sh observe
sh scripts/robot.sh preflight
```

## 2　工位布置一次，每台车采一份实测

1. 在底盘固定的工装中放置可重复辨认的目标，在 `base_link` 中独立测量位置（米）。装车后读取或扫码本台雷达、相机和 IMU 的序列号。工位建立“物理设备→驱动实例→ROS 话题”的绑定，可用驱动报告复核。同类型的 ROS 消息本身不包含可靠的设备序列号。
2. 启动 MID-360 驱动，以标准 `PointCloud2` 输出 XYZ；RealSense 发布彩色、对齐到彩色的深度和同一光学帧的 `CameraInfo`；IMU 提供 SI 单位加速度、角速度。选择同一时钟域，记录时钟配置及固定台架的记录编号。示例话题必须按实际配置修改。[Livox ROS Driver 2](https://github.com/Livox-SDK/livox_ros_driver2) · [RealSense ROS](https://github.com/realsenseai/realsense-ros)
3. 从 `calibration/station/` 拷贝两个模板到工位目录。将 `profile.example.json` 命名为 `profile.json`，填写期望序列号、坐标系、已测安装外参、实际分辨率和相机标定内参。将 `bindings.example.json` 命名为 `bindings.json`，填写上一步现场观测到的身份、话题和固定状态。模板中留空的值需要实测。
4. 有三维靶点对应观测时，可运行 `sh scripts/robot.sh fit-targets` 计算一件传感器的外参。工装输出的 `targets.json` 顶层包含 `unit="m"`、`robot_id`、`serial`、`reference`、`parent_frame`、`child_frame`，以及 `fit` 和 `validation` 两组数组。每个元素形如 `{"id":"A1","sensor":[0.23,0.65,1.12],"base":[1.11,0.22,0.92]}`。分别使用至少 4、3 个独立且分散的观测点，安装求解方向为 **底盘坐标 = R × 传感器坐标 + t**。工位核对输出后将整个候选写入对应的 `extrinsics` 条目。当前点对由工装提供，程序没有从原始图像/点云自动识别靶标。IMU 静止采样可估计陀螺零偏，完整安装姿态需要独立测量或运动激励。
5. 固定底盘、启动工位采集。默认采集 8 秒，检查时序、点云有效数、RGB-D 同步/内参、IMU 静稳和安装身份。通过后返回 `revision`、`trace_id` 和两份输入哈希；失败返回具体设备和问题码。故障后可按 `last_verified_revision` 查历史，当前状态取最新检查结果。

```sh
export EDGE_FACTORY_DIR=/srv/edge/station/rover-001
sh scripts/robot.sh factory
sh scripts/robot.sh calibration-status
```

## 3　第一次进场怎样建图

例如无人车刚从仓门进入未知车间。感知模块基于观测生成占据栅格。探索模块取地图、当前位姿、允许范围和车身半径，找出“可达自由空间与未知空间相邻”的边界。它只在已知可通行区域内选短目标，给边界附近留出车身余量。一次只交接一个目标，任务内核负责执行回执和取消。下一步使用移动后的新地图重新选择。结束时归档 PGM 栅格、YAML 坐标参数和带哈希的运行记录。受边界、预算或通行条件限制时保留部分地图及原因。[选点、保存与验收规格](specs/SDD-039-frontier-mapping.md)

| 通用能力 | 本轮实现 | 上新车型时替换或补齐 |
|---|---|---|
| 工厂标定 | 工装点对求解、ROS 静止窗口采集、按机号与环境归档 | 设备身份读取、靶点检测、IMU 安装测量与时间同步配置 |
| 探索决策 | 纯算法读二维栅格和位姿，输出短目标、部分完成、受阻或完成 | 车身半径与底盘通行模型。无人机的三维自由空间需要不同策略 |
| 环境建图 | Gazebo 中用二维激光、里程计与 IMU 验证局部初图 | MID-360 的定位建图、TF、闭环与重定位；全场多次建图一致性需要独立验收 |
| 运动与停机 | 仿真适配器通过已有 ROS 任务接口执行，保留目标 ID、停车及地图证据 | 真车导航动作、独立急停、定位丢失处置和制动距离实测 |

工厂验收只证明指定工装窗口满足采样条件，不能单独证明导航精度。第一次真车进场先在围定场地验证里程计/定位、停车与人工接管，再允许探索提供者提交首个短目标。真机部署分支默认观测；本轮可自动执行探索的适配器在 Gazebo。仿真使用范围有限的二维扫描，初图只证明该范围的可达空间得到更多观测。[Gazebo 操作与测试证据](testing/rover-exploration.md)
