# 无人车第一次进入场地的 Gazebo 验收

本实验承接 SDD-038 与 SDD-039。输入是仿真机体的已验证标定版本、探索边界、独立的 ROS 2 域及 Gazebo 分区。小车从空的局部占据图开始。验收采用真实 Gazebo 轮式里程计、IMU 和二维激光扫描。算法仅能读取观测生成的地图及融合位姿，每次向任务内核提交一个短目标。世界真值只供独立验收读取。工厂标定的合成工装输入单独标记，真机工程另行验收。

## 验收约定

1. 独立启动场景、桥、任务内核、网关、感知。建立新工作目录，确认机体、标定版本、仿真时间与地图时效后才接收目标。硬件配置一律只读。
2. 至少两个由未知边界推导的目标取得任务内核的到点及停车回执，地图的已知栅格增加。地图以带坐标系与标定版本的栅格文件保存。达到步数预算时报告部分完成。
3. 观测中断、取消或传输失败时停止选点，有在途操作时按原 ID 请求取消及对账。未取得可确认的物理结果时保留任务号，要求人工介入。重复运行号不能覆盖原图或再次发起移动。

## 从零复验

在研发仓库根目录使用具备 Docker Compose 的主机和可构建仓库镜像的基础镜像。每次执行选择一个全新的输出目录。

```sh
docker build -f docker/Dockerfile -t edge-agent-ros:commission-exploration .
python tests/system/commission_exploration.py --image edge-agent-ros:commission-exploration --packaged --out-dir artifacts/verification/commission-exploration/local-01
```

验收程序建立专用 Compose 网络、随机 ROS 域、Gazebo 分区、场地和任务数据库，不使用宿主网络和物理串口。它将仿真激光的有效量程设为 2.1 米，车停在原地只能看到 6 × 6 米试验区的一部分。程序只向规划器提供来自仿真传感器的观测。它另起独立订阅者验证 Gazebo 位姿和速度指令。测试结束会释放这次建立的容器和卷，保留输出目录。

| 启动对象 | 输入 | 输出和复盘路径 |
|---|---|---|
| 工厂验收 | 对应模拟机体编号的合成工装测量与窗口观测 | `factory/module-events.sqlite` 保存校验、版本和原始输入；不能直接用于真实机体 |
| 感知与探索 | `/clock`、`/edge/perception/pose`、`/edge/perception/map`、边界和版本 | 每轮从新图规划第一段可达直线，向 `/edge/submit_mission` 发送一个短目标 |
| 任务内核及网关 | 独立的 `<run-id>/step/<n>`、坐标 | `report.json` 将选点时位姿、地图时间、目标、实际到点和内核状态保存在同一条记录 |
| 地图归档 | 最后一个已校验的地图快照及停止原因 | `<run-id>/map/map.yaml`、`map.pgm`、`occupancy.json` 和 `manifest.json`。中途取消时检查快照时间，它可能早于停车 |

每次运行的 `module-events.sqlite` 将工厂校验、开始、逐段分派、收到回执和终态按事件 ID 串联。跳步或出现异常时，从相应步骤的 `mission_id` 查询内核。若进程被强制结束，保留已有 `report.json`，用相同任务号查询和对账；重新提交同一运行会被拒绝。正常使用 SIGINT/SIGTERM 会触发在途任务取消并等候停车回执。

## 看懂结果

- `COMPLETE / roi_observed` 表示配置范围内的栅格均有观测。`PARTIAL / goal_budget` 表示消耗完允许的步数，未知区仍保留为 -1。`PARTIAL / no_observed_progress` 表示虽然上一段收到到点回执，但位姿与地图均未取得新进展，需要调整导航容差、分辨率或重新观察。
- `BLOCKED / map_or_pose_unavailable` 表示收不到对时且同坐标系的地图和位姿，任务没有启动。运动中观测丢失会尝试按原 ID 取消，恢复策略可从 `reason` 和 `cancel` 分辨原因与停车结果。结果不明时状态为 `RECOVERY_REQUIRED`。
- `report.json.final_known_cells / total_cells` 是给定探索区域的覆盖比例。`map/manifest.json` 中相同名称的字段统计整张局部地图。试验区域为 3,600 格，完整局部栅格为 14,400 格。
- 仿真场地以轮速和 IMU 估计的 `odom` 为参考系，使用二维激光建图。无回环优化。将来接入激光惯导 SLAM、视觉 SLAM 或 Nav2，需要分别验证姿态质量、坐标变换、制动距离与急停，不应将此局部探索试验作为真机移动授权。
