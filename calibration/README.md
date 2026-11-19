# 工厂标定插件

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
