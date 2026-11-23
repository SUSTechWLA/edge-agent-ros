# 灵遨避障的现场接入

Agent 的任务、物理执行器、独立核验器各有自己的记录。现场适配从这三个接口接入；现有仿真站点只认 `sim.lingao.base`。相机、额外雷达、目标检测和用户工作流可以提供观测，执行权限始终绑定具体底盘。[仿真站点契约](../../capabilities/rover-maneuver/sim.json) · [规划与执行设计](../specs/SDD-056-rover-local-plan.md)

| 现场接口 | 接入条件 | 实验前保留的证据 |
|---|---|---|
| 机体与传感器 | 用四轮中心定义 `base_link`，量出带双 MID-360、RealSense、RTK 支架、线束及轮胎的四向最大外缘。提供带负载的低速制动距离、反应延迟及最小可控速度。 | 实测足迹、版本、标定和测量日期；核对预留能够覆盖延迟与制动。 |
| 只读观测 | 将双 MID-360 点云在各自采样时刻转换到统一水平面并处理自车、地面、悬挂物、遮挡及盲区。规划器接收经过标定的 `sensor_msgs/LaserScan`；未知区域保持未知。 | 同钟的原始点云、投影、TF、里程计、障碍报告，用已量位置的目标检查距离和方位。 |
| 独立定位与停稳 | 主定位供 Gateway 与 Core 使用。核验进程接入另一路有实际独立来源的 `nav_msgs/Odometry`，验证时间、起止姿态、路径和停稳。 | 记录两种位姿的来源、误差、延迟和故障特性。在两者不一致或核验证据丢失时检查任务状态。 |
| 底盘动作 | 现场已有 Nav2、`nav_bringup` 或底盘驱动时，先查明哪个进程持有输出及抢占权。通过该机体的唯一动作入口承接 Core 任务，可使用 Body Runtime 或现有有租约及停车语义的适配器。 | 操作、租约、限速、超时、急停以及通讯中断后独立停车的台架回执。 |

## 台架前先观察

在目标板部署同版本 ARM64 观察镜像后，使用该机体的 ROS domain 和话题配置。运行已有观察和预检命令；下列附加探针只订阅扫描、里程计和 TF，读取命令话题发布者列表。参数中的外参应由现场标定提供，探针会将其与采样时刻的 TF 对照。观察过程保持车辆静止。[首次上板说明](../board-first-boot.md) · [探针实现](../../client/rover_input_probe.py)

```sh
sh scripts/deploy.sh hardware observe
sh scripts/deploy.sh hardware preflight
docker compose -f compose.hardware.yaml exec -T console /usr/local/bin/edge-entrypoint \
  python3 -B /opt/edge/client/rover_input_probe.py \
  --scan "$EDGE_SCAN_TOPIC" --odom "$EDGE_ODOM_TOPIC" --command "$EDGE_CMD_TOPIC" \
  --scan-frame "$EDGE_LASER_FRAME" --base-frame "$EDGE_BASE_FRAME" --odom-frame "$EDGE_ODOM_FRAME" \
  --scan-xy-yaw "$EDGE_LIDAR_X" "$EDGE_LIDAR_Y" "$EDGE_LIDAR_YAW"
```

探针列出全周可观测性、连续采样的最大时间间隔、扫描外参和同一时戳是否可取得定位变换。发布者列表用于人工确认控制所有权。返回 `input_checks_passed` 仅表明本次输入检查通过，结果中的 `motion_authorized` 始终为 false。

## 现场站点和审批绑定

物理提供者有独立的控制器、Core 适配器、核验器。站点配置接受 ROS 话题、服务和坐标系。现场使用 `commissioned_rover_bench`、本机 `hardware.*` 资源、已测足迹和标定。主定位提供 `nav_msgs/Odometry`；另一套定位提供独立的位姿，制动核验还要接入独立采集的 `geometry_msgs/TwistStamped` 实际速度。对应的来源、帧和实测记录须能追溯。独立核验器在现场需要同时收到合格的停稳位姿和制动速度序列。[站点验证](../../agent/rover_site.py) · [ROS 执行和核验](../../agent/rover_maneuver_ros.py)

部署使用只读站点目录及另行批准的现场材料目录。批准内容固定资源、ROS domain、镜像平台摘要和配置摘要，引用实测足迹、传感器标定、独立位姿、制动四份证据文件及其哈希。硬件叠加配置会单独校验批准文件摘要，模拟站点无法取得现场资源。部署绑定成立之后，任务仍需针对资源的操作员授权。本轮验收使用仿真身份；合成的台架证据只用于验证协议。参见 [DSH 提供者](../../compose.rover-maneuver.yaml) 和 [台架叠加配置](../../compose.rover-maneuver-bench.yaml)。

## 实验晋级

1. 静止车辆上录制 rosbag，把同一份扫描、TF 和里程计送入隔离 ROS domain。分别遮挡、停发或错配输入，确认观测会失效；回放域中的所有运动输出只接记录器。按现场传感器帧率验证跟踪和停止判据。
2. 为现场资源提供独立的物理站点批准与核验器。冻结版本、实测机型、已确认的本地坐标范围、最大路程、速度、超时和独立证据源。当前 Core 的目标范围为本地坐标各轴 ±4 m；若现场里程计使用全局大坐标，需要经核验的任务局部坐标适配。现场失联、动力系统急停和正在执行的旧 Nav2 目标要在台架上联测。
3. 人员在场、具备独立急停并完成台架制动验收后，另行进行实车低速试验。先测固定软障碍，再在隔离区域用可控移动靶验证横穿、停留、遮挡、移出与二次闯入。使用相同的事件关联和独立净距标准保留失败结果。

本地 Gazebo 提供水平激光、四轮物理与世界真值；现场点云投影、实装外廓、独立定位源、实际制动、板端实时性以及现场运动授权均须在该机体验收。
