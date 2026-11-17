# 轮式里程计、IMU 与二维激光感知

这个节点将轮编码里程计的平移量与陀螺仪 Z 轴角速度结合，发布平面位姿。激光扫描按采样时间匹配位姿，生成本地占用栅格。Gazebo 和实机均通过同一组 ROS 2 消息接入。真值桥只供其他模块对照。

| 输入 topic | ROS 2 类型 | 需要满足的契约 |
|---|---|---|
| /odom | nav_msgs/Odometry | odom → base_link，米、弧度；单位四元数；递增采集时间戳 |
| /imu | sensor_msgs/Imu | imu_link 的 +Z 与 base_link 的 +Z 对齐，角速度 rad/s；设备必须提供有效陀螺数据 |
| /scan | sensor_msgs/LaserScan | laser_link，米，逆时针为正，range_min/range_max 有限，水平扫描 |
| /clock | rosgraph_msgs/Clock | 可选。仿真时间向后跳变会清空位姿历史与地图，新一轮观测从空图开始 |

输出 `/edge/perception/pose` 是 `nav_msgs/Odometry`，`/edge/perception/map` 是 `nav_msgs/OccupancyGrid`，`/edge/perception/status` 是包含融合模式、已匹配/丢弃扫描数和坐标系的 JSON 字符串。

## 位姿如何得到

节点在每段轮式里程计采样间隔内按采集时间积分对应的 IMU Z 轴角速度。以一秒时间常数将预报航向向轮式里程计航向校正，再用校正后的中点航向旋转平移增量。两次定位之间仅使用已收到的 IMU 样本，陀螺样本间隙超过 100 ms 时用轮式增量更新并报告 `wheel_only`。正常融合报告 `wheel_imu`。本机单调时钟超过一秒未收到有效 /odom 后，模式为 `stale_odom`。节点只在收到新里程计时发布新位姿。

## 地图如何得到

默认地图为以里程计原点为中心的 12 m × 12 m、分辨率 0.1 m 的本地栅格。扫描到达后等待两侧的融合位姿进行插值，位姿间距上限 150 ms，等待上限 500 ms。坐标系错误、重复/倒序扫描或无法匹配时间时记录丢弃原因。每束激光从标定的发射点投射，经过的格子累积自由证据，命中格累积占用证据。地图编码 -1 为未知、0 为自由、100 为占用，同一扫描内命中优先。默认正无穷和有效上限读数表示量程内无回波。如驱动用正无穷表示无效读数，设置 `clear_positive_infinity:=false`。NaN、低于量程等无效读数跳过。

格网输出保留最后一次成功融合的激光时间戳，最长每秒一次；无新扫描时停止更新。地图使用随里程计漂移的 odom 坐标系，适用于调试和可视化。全局定位、回环、每束激光运动补偿与精度协方差标定需要独立实现。本节点未参与底盘授权或运动控制。

当一整帧没有提供任何格子的自由或占用证据时，节点增加 `dropped_scans`，保留最后一个有效地图时间。仿真 `/clock` 回退或长里程计间隙导致重新定位时，节点立即发布全未知格网，将 DDS 保留的旧图替换掉。此重置消息带回退或重新定位的时间；有效扫描重新积累计数。启用 `/clock` 时，传感输入最多允许领先已收到的仿真时间 1 秒，以容纳独立 DDS 流交付的正常偏差。时域相互重叠时，仅凭时间戳不能识别所有旧包；严格区分每次运行需要生产端携带运行标识。

## 接真实机器人

使用有 ROS 2 Jazzy 和项目镜像的环境，ROS 网络及 QoS 与传感器发布方匹配。接入前确认所有采集时间来自同一时域。当前局部算法假设水平平面与水平雷达；有明显滚转、俯仰或倾斜安装时需先将数据变换到兼容平面。机体 IMU 倾斜安装时须将角速度旋转到约定轴向。传感器解释或传输层应在驱动/适配器完成这项校准。

```bash
python3 /opt/edge/perception/node.py --ros-args \
  -p odom_topic:=/robot01/wheel/odom -p imu_topic:=/robot01/imu/data \
  -p scan_topic:=/robot01/lidar/scan -p laser_frame:=front_laser \
  -p imu_frame:=base_imu -p lidar_x:=0.23 -p lidar_y:=0.01 -p lidar_yaw:=0.0
```

部署中替换 `odom_frame`、`base_frame`、`pose_topic`、`map_topic`、`status_topic`、`grid_cells` 和 `resolution_m` 可选择新的参考系、输出名和覆盖范围。若接入设备的位姿、IMU 或扫描采样频率过低，应结合实测时延重新评估时间匹配门限。重新定位或变更传感器标定时，应重启本节点，用新观测重建地图。
