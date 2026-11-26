# 工厂标定插件

相机和双雷达的举板标定另有独立仿真工位，支持自然语言下发、原始观测自动录制和独立留出验证。[自主标定工位及实车接入条件](../docs/testing/guided-calibration.md)

每台无人车在工厂固定台架上单独验收。工位为该车登记 MID-360、RealSense 和 IMU 的序列号及安装测量，采集一段静止窗口，再发布该装配的标定版本。现场换传感器、改变安装角度或变更 RealSense 视频模式后应重新核对。当前版本输出记录及参数，TF 发布器、IMU 驱动和 SLAM 消费者分别负责应用这些参数。

## 入口

在仓库根目录或带此插件的镜像内运行。输入路径由工位配置。

```sh
python -m calibration.factory verify --profile robot-profile.json --capture capture.json --state factory-state --operation build-0001
python -m calibration.factory status --state factory-state --robot-id rover-01 --environment hardware
```

输出是 JSON。退出码 0 表示最近这次登记的静态检查通过，2 表示需要处理报告中的 `issues`。验收成功的 `revision` 是可复核的 SHA-256。探索程序应按当前机体、环境和版本读取最新状态，启动时核对在线设备健康并拥有自己的运动授权。同一操作 ID 重投会返回历史回执，后续联机判断使用 `status`。在同一工位执行不同采样时使用新的操作 ID。

## 从工装与 ROS 到验收

| 输入 | 谁提供 | 验收什么 |
|---|---|---|
| 机体和传感器身份 | 工厂清单、设备序列号绑定记录、ROS 驱动 | `robot_id/environment`、各设备 `serial/topic/frame` 一致。本插件信任指定的工位和采集适配器。ROS 帧名无法证明物理序列号。 |
| 安装外参 | `calibration.targets` 拟合已知靶点或工厂实测安装记录 | TF 父为底盘、子为传感器，平移单位 m，四元数顺序 xyzw。每个变换带机体/设备和工装参考号。靶点拟合要求至少三个独立验证点、拟合与验证 RMS ≤ 20 mm，验证最大残差 ≤ 40 mm。 |
| RealSense 内参 | 已标定的相机档案与当前模式 CameraInfo | 宽高、彩色光学帧和 K 矩阵需匹配，矩阵元素容差 0.5。深度已注册至彩色帧，三路消息的采集时间跨度 ≤ 50 ms。原始畸变、红外立体基线和深度单位比例仍由驱动/工装标定保证。 |
| 三路静止测量 | ROS 采集或同协议的离线记录 | 每源 5–4096 帧、覆盖 ≥ 1 s，源间公共窗口 ≥ 0.5 s。相邻采集间隙 ≤ 600 ms；同域接收减采集时间在 −20 到 500 ms 内。共同窗口内任一时间戳距最近另一路样本 ≤ 250 ms。 |
| 台架质量 | 清晰的固定目标、机械固定车体 | 每帧至少 32 个有限 XYZ 点且有限比例 ≥ 20%，正有效深度像素 ≥ 20%。IMU 的加速度均值模长 9.3–10.3 m/s²，角速度均值模长 ≤ 0.05 rad/s，单帧距均值的最大偏差分别为 0.45 m/s² 和 0.025 rad/s。 |

上述数值是 v1 的工位验收条件，现场建图有自己的时延、精度和场地标准。采样时间指 ROS 消息提供的时间戳。检查能发现明显迟到和时间不重叠；证明硬件触发同步或估计时钟偏置需要额外同步参考与试验。静止 IMU 的角速度均值只在工装确实固定时可解释为零偏。单一静止姿态无法分开重力与三轴加速度偏置，也无法估计安装平移及航向。

**字段示例。** `profile.sensors.rgbd.topic` 指已对齐彩色坐标的深度主题。对应的 `capture.sensors.rgbd.samples[]` 每帧含 `stamp_ns`、`received_ns`、`color_stamp_ns`、`info_stamp_ns`、`width`、`height`、`k`、`depth_valid_count`。点云样本的测量字段是 `point_count/finite_xyz_count`，IMU 样本是 `accel_mps2/gyro_rad_s` 三轴向量。可读的完整合成输入在测试的 `fixture()` 中。[ROS 采集](ros_capture.py) · [靶点求解](targets.py) · [合成契约测试](../tests/test_factory_calibration.py)

## 失败与回溯

例如，其他车的雷达安装报告会返回 `extrinsic.identity`，更换相机模式导致内参变化会返回 `camera.intrinsics_mismatch`。带有合法机体/环境的失败复核会保留 `last_verified_revision`，并使该机体当前状态变为 `NEEDS_CALIBRATION`。故障处置后用新现场采样复验。无法辨认机体或存储不可写时，调用者按本次失败阻止启动，并保留 CLI 错误。

`factory-state/module-events.sqlite` 包含源归档、历次版本、工厂回执以及现有 ModuleJournal 格式的事件。按 `operation_id` 读 `factory_attempt` 回执；按其中的 `profile_sha256/capture_sha256` 读 `factory_input` 取回实际验收输入。这些是每帧质量统计和 IMU 测量，图像与点云原包可在工位独立保留。SQLite 在一个事务内提交事件和发布状态。其他插件消费只读回执；写库仅由工厂入口负责。历史记录在工厂状态卷中持续保留，定期备份与介质容量由部署者管理。[规格、验收与回退](../docs/specs/SDD-038-rover-factory-calibration.md)

## 开发者复验

### 手持棋盘的相对外参

工作台的 `observe_target` 通过同一 `perception.target_detection` 接口查询目标。例如 `checkerboard` 和 `campus_sign` 均返回图像中的标签、像素框、采集时间、坐标帧、输入摘要和检测器版本。棋盘观测还含角点与相机下的板面测量。标定的点云测量读取这份结果和同一原始观测，以归档摘要和时戳核对配对关系。普通标牌查询只做图像识别。

在部署管理的 [recognizers 目录](station/recognizers) 为新目标安装清单，登记 detector ID、版本、目标标签及镜像内已安装的实现。下一请求会重新发现当前清单，缺少检测器或同一标签重复绑定会拒绝该请求。要接入本站已有的 YOLO 对象模型，可使用 `perception.target_providers:yolo` 适配器，为模型支持的标签登记对应的 `targets`，在 `settings` 填入只读权重路径 `weights` 与 `sha256:` 内容摘要 `digest`，使用含已有推理依赖的计算镜像。当前仿真实际验收了棋盘与按颜色识别的校园标志牌。[同一帧两种目标的 HTTP 验收](../artifacts/verification/autocalibration-20261125/target-detection.json)

`calibration.board_solver` 接收同一时刻的 RGB 图、当前模式的相机内参和每台雷达各自坐标系内的 XYZ 点。板规格包含内角行列数、格边长以及四周白边宽度，单位为米。每一姿态必须在相机图中检出完整棋盘，并在每台雷达中检出唯一尺寸匹配的平面；相机需要提供与图像一致的内参和畸变模型。标准仿真使用无畸变针孔成像。

请把板固定片刻再换角度，在水平、垂直两个方向分别倾斜。程序先用多组板法向求旋转，再用平面偏移求平移，输出从各雷达到相机光学帧的变换。重复姿态和法向跨度不足的样本会被拒绝。留出另一批姿态检验候选参数；报告给出法向角误差与板面距离误差，验收上限为 2° 与 25 mm。板距离传感器过近、多个相似板同时入镜、板点数太少或远处背景与板面连成一片时，应改善采集条件再试。

自由手持的棋盘可用来求传感器间相对外参。底盘坐标系需要已测量的安装参考，IMU 外参需要受控激励或其他可观测约束。仿真工位的朝向、覆盖范围和点密度应在实车复测。[自然语言标定协议](../docs/specs/SDD-059-guided-sensor-calibration.md)

在项目根目录构建包含 OpenCV 的计算镜像，可在隔离容器内运行检测、求解和拒绝路径测试。

```powershell
docker build -f docker/Dockerfile.board-calibration -t edge-agent-ros:board-calibration-1 docker
docker run --rm --network none -v "${PWD}:/workspace:ro" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 edge-agent-ros:board-calibration-1 python3 -m unittest discover -s tests -p test_board_solver.py
```

### 工厂复核

在仓库根目录运行 `python -B tests/test_factory_calibration.py -v` 检查数据契约、失败和事务语义。含当前源码与测试的开发镜像还可运行下面的命令。Docker 隔离网络使 DDS 发现只发生在本次工位内，测试在临时目录建立独立数据库。镜像标签换成自己的开发构建。

```sh
mkdir -p artifacts/verification/rover-factory
docker run --rm --network none -e ROS_DOMAIN_ID=184 -e ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
  -v "$PWD/artifacts/verification/rover-factory:/proof" edge-agent-ros:development \
  python3 -B tests/system/factory_ros_capture.py --out /proof/ros-dds.json
```

[五阶段 DDS 验证记录](../docs/evidence/rover-factory-calibration.json) 给出采样数、对齐误差、静止统计、版本、事件 ID 和具体错误。工厂门控只确认本次窗口与已登记工位档案相符，后续探索的运动许可由运行现场重新判断。
