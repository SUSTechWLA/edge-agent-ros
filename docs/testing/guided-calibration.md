# 静止工位上的自主传感器标定

在工作台输入「请帮我标定当前传感器」。当相机和双雷达之间缺少本装配的已验证外参，首页会在提交任务前提示举板。机器人保持静止，操作者只负责移动标定板。每次改变倾角或距离后短暂停住，让相机角点和两路点云来自同一板姿。水平和竖直两个方向都需要改变倾角；只让板左右平移不能充分约束平移外参。

## 在本地仿真复测

使用四个 ArUco 码和四个通孔的标定板时，可按[四孔板与 Jetson 实车采集指南](four-hole-calibration.md)设置标定方法、查看取景及点云覆盖、录制多个稳定板姿并检验外参。

装配已验证后，可以通过新的自然语言任务估计采集时间差。[双 MID-360 与 D435 的时间标定及 Jetson 入口](sensor-time-calibration.md)

### 工具如何复用

`observe_target(target)` 是 Agent 的只读入口。传 `checkerboard` 可得到棋盘的像素范围和计量特征，传 `campus_sign` 可得到校园标志牌的像素范围。底层的 `perception.target_detection` 接收带来源的图像观测，并按部署目录选择检测器。回执记录观测帧、时间戳、检测器版本、清单和实现摘要。本站的标志牌检测器识别约定的品红色标牌；接入其他物体类别时，应安装具有相应词表的检测器。检测器撤下后，其他目标可以继续识别。[检测器清单](../../calibration/station/recognizers/) · [同一帧的两类检测结果](../../artifacts/verification/autocalibration-20261125/target-detection.json)

普通识别任务可以在获得目标观测后结束。标定任务另有保存同步样本、拟合外参和验证候选的工具。模型每次根据目标、可用工具和最近回执作一个选择。采集端在写入前检查相机与双雷达的时间、帧和板姿，求解端检查数据量与可观测性。原始像素、点云和角点归档在工具一侧，规划器使用有界的状态摘要，完整测量可从回执追溯。

### 启动工位

在仓库根目录使用以下 PowerShell 命令。该独立工位与校园导航使用不同的 ROS 域和 Gazebo 分区。

新工作站可从源码构建仿真基座，再创建下面使用的默认本地标签。已有这些镜像时可以跳过这组命令。

```powershell
docker build -f docker/Dockerfile.base -t edge-agent-ros/base:jazzy-harmonic .
docker build -f docker/Dockerfile -t edge-rover-local:sdd058-dynamic-recovery .
docker build -f docker/Dockerfile.campus-lidar -t edge-rover-local:sdd058-campus-lidar-r1 .
```

```powershell
docker build -f docker/Dockerfile.board-calibration -t edge-agent-ros:board-calibration-1 .
$station = @('compose', '-p', 'edge-board-cal', '-f', 'compose.calibration-gazebo.yaml', '-f', 'compose.calibration-board.yaml', '-f', 'compose.calibration-model.yaml', '-f', 'compose.calibration-console.yaml')
docker @station up -d
```

模型服务在工作站 GPU 上使用已有 Ollama 模型缓存，默认加载 `qwen3:4b-instruct`。当前配置所用的缓存卷为 `p0-author-replay-model`，可用 `EDGE_CAL_MODEL_VOLUME` 指向同一目录结构的本地缓存。打开工作台，等待标定状态出现并下发任务。仿真操作者可以用以下命令改变工装中板的位置，按页面提示选用不同姿态。索引 0 将板收回，1～12 是独立工装姿态。Agent 的工具目录没有这些操作者命令。[打开标定工位](http://127.0.0.1:8911/)

```powershell
docker @station exec cal-sim /usr/local/bin/edge-entrypoint python3 /fixture/board.py present 1
```

标定数据卷与 Agent 任务卷分别保存原始样本及任务选择记录。停止工位可使用 `docker @station stop`。重新启动后使用原卷可读取先前结果。

## 换到真实车辆前

1. 将棋盘图案按 100% 尺寸制作在刚性平板上。当前配置为 7 × 5 个黑白格，每格 80 mm，四边留白各 40 mm，板面外尺寸 640 × 480 mm。打印后用尺复核方格和外轮廓。尺寸同时参与相机姿态求解和雷达平面筛选。[等比例制作图](../../calibration/station/checkerboard-80mm.svg)
2. 为 D435 和两台 MID-360 分别登记序列号、坐标系和话题，记录安装变更。相机使用与当前视频模式匹配的去畸变彩色图和 CameraInfo，两台雷达各提供传感器本地坐标系下的三维点云。首先用真实录包复核时间域、两雷达可见点密度及板边界。当前仿真的 GPU 激光雷达和现场 MID-360 的扫描分布不同。
3. 牵涉地图或底盘坐标时，需要另有可追溯的 `base_link` 到参考传感器的变换。静止举板能够估计两台雷达各自到相机光学帧的变换。完整 IMU 安装外参、时间偏移和实车运动授权分别验收。
4. 在实车工位复测留出板姿的角度、点面距离和重复装夹精度，再由安装标定的所有者决定是否采用该版本。当前工位限定为仿真，求解和验证不会自动修改实车 TF 或 SLAM 配置。[已有工厂验收入口](../../calibration/README.md) · [设计与验收边界](../specs/SDD-059-guided-sensor-calibration.md)

### 两台 MID-360、内置 IMU 与 D435 的现场标定

| 要得到的关系 | 采集和验证方法 |
|---|---|
| 每台 MID-360 的点云与本机 IMU | 厂家 v1.2 手册给出两者轴向一致，IMU 原点在本机原始点云坐标中为 `(+11.00, +23.29, −44.12) mm`。在该坐标约定下，`T_lidar←imu` 的旋转是单位阵。若算法要求 `T_imu←lidar`，应取逆。每台雷达按自身序列号绑定，核对驱动是否已做坐标变换及 IMU 的输出单位。[Livox 手册，IMU Data](https://terra-1-g.djicdn.com/851d20f7b9f64838a34cd02351370894/Livox/Livox_Mid-360_User_Manual_EN.pdf#page=17) · [Livox ROS Driver 2](https://github.com/Livox-SDK/livox_ros_driver2) |
| D435 深度光学帧与彩色光学帧 | 从设备读取当前模式下两路内参、畸变、深度单位和深度到彩色的出厂变换。用已知距离的平板检查深度误差，再把原始深度点投到彩色图，检查物体边缘。对于已经对齐到彩色图的深度，反投影使用对应的对齐后标定信息，保留输入的帧和模式。[RealSense SDK 投影与外参](https://github.com/realsenseai/librealsense/wiki/Projection-in-RealSense-SDK-2.0) |
| 两台 MID-360 与 D435 彩色光学帧 | 在各路都看得到的位置摆板，改变距离、横向与纵向倾角，每个姿态停稳。建议现场采 12–20 个不同姿态，预先留出一批覆盖近远和不同倾角的位置。相机用棋盘角点算板面，各雷达独立提取板面，根据法向和距离求两组变换。将留出姿态的点云投到相机图，检查板面偏差、板边和重叠区域；再把两台雷达的共同墙面、墙角叠在一起，独立核对相对安装。如果现场没有三者公共视野，需要按传感器对单独采集，或用已测量的多面靶作为共同参考。当前仿真工位要求三者同见。 |
| 时间对齐、惯性零偏和装车基准 | 单独记录每台雷达的逐点时间和 IMU、相机帧的采集时间，保留时钟域。MID-360 支持 PTPv2 或 GPS 同步，D435 的映射要单独复核。静置取陀螺零偏，核对重力方向和加速度单位。要独立估计六自由度惯性外参和时差，应在静态、有三维结构的环境中，对刚性固定的整套设备施加多轴转动和变速平移，再用另一段运动录包复核。只开平地直线或原地水平转向会留下弱约束。车体 `base_link` 则通过已测工装或装车测量连接到选定参考传感器。[Livox 同步说明](https://terra-1-g.djicdn.com/851d20f7b9f64838a34cd02351370894/Livox/Livox_Mid-360_User_Manual_EN.pdf#page=16) · [LI-Init 的激励与时间偏移估计](https://github.com/hku-mars/LiDAR_IMU_Init#3-run-your-own-data) |

这里 `T_A←B` 将 B 系中的点变换到 A 系。两组板标定结果共用彩色光学帧 C，左右雷达间的变换为 `T_L←R = inverse(T_C←L) × T_C←R`。左侧 IMU 到彩色相机的变换为 `T_C←I = T_C←L × T_L←I`，右侧同理。发布 TF 时每个子帧只保留一个父帧。装车验收记录应注明物理序列号、变换方向、参考系、时钟来源、所用录包和留出误差。实车首次建图建议先用一台主 IMU 验证去畸变与定位，另一台独立记录，作为转弯角速度与时差的交叉检查。

## 本轮仿真结果

2026-11-25，前端提交「请帮我标定当前传感器」。本地模型在同一任务中选择工具，遇到重复板姿和两路雷达无法唯一辨认板面时请求换角度，最终用 6 个姿态拟合、2 个姿态验证。仿真安装真值独立于求解，左侧误差为 3.92 mm / 0.353°，右侧为 10.33 mm / 0.458°。本轮采集 RGB、相机内参和两路 XYZ 点云；实机深度尺度、IMU 和跨设备时间偏差仍待测量。[独立真值](../../artifacts/verification/autocalibration-20261125/run-04/gazebo-truth.json) · [八组采集与标定结果](../../artifacts/verification/autocalibration-20261125/run-04/recordings.zip)
