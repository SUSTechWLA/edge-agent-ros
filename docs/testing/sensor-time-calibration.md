# 双 MID-360 与 D435 的软件时间标定

该工位测量每台雷达相对 D435 **彩色光学流**的采集时差。车体固定，操作者在共同视野中沿远近方向移动同一块棋盘，速度和转向时刻要有变化。Agent 选择采集和检查工具，比较独立传感器看到的板面运动。先验空间外参来自当前装配的独立举板标定。估计只覆盖本次采到的时间窗口，深度相机内部对齐、IMU 时间戳和实车制动另行验收。[空间标定方法及靶板](guided-calibration.md)

## 先判断现场是否具备测量条件

- 两台雷达分别输出原生传感器帧下的 `sensor_msgs/msg/PointCloud2`，保留逐点采集时刻。核对数据字段的单位、相对/绝对含义和驱动配置；若只有合并云、XYZ 预览或 Livox `CustomMsg`，先从驱动提供保留逐点时间的标准输出或经已验证的转换器录包。查清 front/rear 对应的实体序列号和安装帧。MID-360 可使用 PTPv2 或 GPS 为时钟提供基准。[Livox 协议与同步](https://www.livoxtech.com/mid-360/downloads) · [官方驱动标准点云字段](https://github.com/Livox-SDK/livox_ros_driver2/blob/master/src/lddc.cpp)
- 取得 D435 彩色图、对应模式的 CameraInfo、时间戳来源和曝光/滚动读出的保守时间上界。RealSense 的 global time 将相机时钟映射到宿主时钟。ROS 到达时间只作诊断。确认雷达和相机时间已经在同一粗时间基准附近，算法当前搜索 ±250 ms。设备开机计数与 Unix 时间混用时应先映射时钟原点。[RealSense 时间域定义](https://github.com/realsenseai/librealsense/blob/master/include/librealsense2/h/rs_frame.h)
- 两台雷达都要能看到足够密集的独立板面。前后雷达若没有共同视野，需要换工装或分别建立能验证空间和时间关系的采集工位。标准工位需要所有已登记传感器都能看见同一块板。静止或匀速单向移动无法提供足够独立的时间特征。

## Linux 或 Jetson 的只读工位

在仓库根目录操作。先用本车的 ROS 环境执行 `ros2 topic list -t`，并在对应 CameraInfo 和点云主题上用 `ros2 topic echo --once` 核对模式、原生帧、字段及头时间。复制 [硬件配置模板](../../calibration/station/hardware-clock.example.json) 到设备本地，填写对应值。模板中的空值必须从本机取得。原始彩色图选 `raw`，已矫正图选 `rectified`。后者使用 CameraInfo 的投影矩阵。硬件配置与 Gazebo 分卷保存。

```sh
export EDGE_CAL_HW_PROFILE="$HOME/installed-sensors.json"
cp -n calibration/station/hardware-clock.example.json "$EDGE_CAL_HW_PROFILE"
# 在文件中填写实测设备身份、话题、坐标帧、时钟域与曝光上界，再执行下面的命令。
python3 -m calibration.hardware_profile "$EDGE_CAL_HW_PROFILE"
export EDGE_CAL_ROBOT_ID="$(python3 -c 'import json,os; print(json.load(open(os.environ["EDGE_CAL_HW_PROFILE"]))["robot_id"])')"
export EDGE_CAL_ROS_DOMAIN="${ROS_DOMAIN_ID:-0}"
export EDGE_CAL_HOST_GATEWAY="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
export EDGE_CALIBRATION_IMAGE=edge-agent-ros:readonly-cal-station
export EDGE_CALIBRATION_COMPUTE_IMAGE="$EDGE_CALIBRATION_IMAGE"
export EDGE_CAL_OLLAMA_URL=http://host.docker.internal:11435
docker build -f docker/Dockerfile.calibration-observer --target observer --build-arg ROS_DISTRO="${ROS_DISTRO:-humble}" -t edge-agent-ros:hardware-calibration-observer .
docker build -f docker/Dockerfile.calibration-observer --target station --build-arg ROS_DISTRO=jazzy -t "$EDGE_CALIBRATION_IMAGE" .
docker compose -p edge-cal-hardware -f compose.calibration-board.yaml -f compose.calibration-console.yaml -f compose.calibration-hardware.yaml up -d
```

模型地址示例使用 Jetson 的本机服务。运行前核对服务端口、模型名和从 Docker 网桥的可达性。监听传感器的两个容器使用 Linux 主机 DDS 和现场 ROS 域，两个只读 HTTP 入口只绑定配置中的 Docker 网桥地址。Agent 与工作台在私有 Compose 网络中运行。默认页面位于工位机器的 `127.0.0.1:8911`，可用 SSH 本地转发查看。合成单测与 Gazebo 验收运行在 amd64；现场的 Jetson ROS、驱动配置、板面密度和时间误差需独立实测。

若本机访问 ROS 软件源缓慢，两条构建命令均支持 `--build-arg ROS_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ros2/ubuntu`，沿用基础镜像的签名密钥。在 Jetson 上指定的镜像源需提供对应 ROS 发行版的 arm64 软件包。

1. 首次或重新装配后，在前端输入「请帮我标定当前传感器」。每个姿态停稳，完成双雷达到彩色相机的空间外参验收。只读成熟装配重启后可复用同一硬件工位保存的有效空间版本。
2. 输入「请帮我标定当前雷达和相机的软件时间偏移」。按工位提示前后移动板，做不等速的往返和方向变化，在整个采集窗口保持可见。采集端保留各自的原始时间戳，按时间前后段拟合与验证。若退回缺少运动或缺少板面，可根据回执调整后重新采集。
3. 工作台会分别显示每台雷达的校正量与估计不确定度。`camera_time_ns = lidar_raw_time_ns + offset_ns`。设备实例、时钟连续性、空间标定版本和采集区间共同限定可用性。重新上电、订阅中断、换装或复测失败时要重新核验。

## 配对数据供地图库使用

从同一站点的 `GET /clock-status` 取得版本、传感器与时钟绑定。消费方通过站点 `POST /assess-clock-pair` 传入观测本身携带的 `robot_id/environment/profile_sha256/spatial_revision/clock_epoch/revision`、雷达 `source`、两路原始时间 `lidar_stamp_ns/camera_stamp_ns`、各自采集时间半径 `lidar_radius_ns/camera_radius_ns` 和任务容差 `budget_ns`。半径要覆盖实际输入点或整帧的扫描/曝光区间。校正后的时差、估计不确定度和两个半径之和必须在任务及站点的容差内。该误差估计来自测量，不能替代导航的安全净距或实车实时性验收。

只有返回 `accepted: true` 的对才具备这里验证的时间关联。区间外或证据失效时，保留原生雷达占用观测和独立 RGB-D 语义观测。该接口用于有证据的缓冲帧或离线地图处理。长期运行的在线纹理融合还需要滚动重验、时钟漂移监测和车辆运动补偿。空间 TF、深度尺度和障碍物安全控制由各自模块负责。

## 重放与 Gazebo 复测

两次工作台任务使用同一句「请为当前两台 MID360 与 D435 自主标定软件时间偏移」。运动靶场景在未读真值的情况下估出 −118 ms 和 +77 ms，两路误差分别为 1 ms、2 ms，经验不确定度均为 18 ms。第二轮保持棋盘静止，Agent 记录三路观测后，因缺少激励阻止发布新时间版本，空间外参保留。[运动与配对审计](../../artifacts/verification/soft-sync-20261125/moving.json) · [静止拒绝审计](../../artifacts/verification/soft-sync-20261125/static.json)

使用带 NumPy/OpenCV 的环境执行 `PYTHONPATH=. python3 tests/system/calibration_clock_replay.py` 可从归档重新求解。复测在线工位，先按[空间标定操作](guided-calibration.md)得到当前工位的空间版本。保留其卷，在原 PowerShell 的 Compose 参数数组追加下列测试工装。它改变测试板姿和观测时间标签，Agent 只能订阅传感器数据。

```powershell
$clockBench = $station + @('-f','compose.calibration-clock-fixture.yaml')
$env:EDGE_CAL_BOARD_MOTION = 'moving'
docker @clockBench up -d
# 通过页面提交上述时间标定任务；完成后再进行静止对照。
$env:EDGE_CAL_BOARD_MOTION = 'static'
docker @clockBench up -d cal-board-holder
```
