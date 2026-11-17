# 模块四 · Docker、仿真与验收客户端

Gazebo Harmonic 根据 SDF 和场地标注生成室内外环境。ROS 桥传递速度命令、里程计、IMU、激光与仿真时钟。另一个真值桥发布实际模型位姿供独立验收。运动控制器和任务内核的实现均不订阅 `/ground_truth`；它们与验收进程处在同一测试 DDS 域。模型从原点出发，初始航向为零。[场地标注](../../scene/campus.json)

Compose 使用统一应用镜像，同时启动仿真、桥、网关、内核。服务通过内部容器网络做 ROS 发现，数据库挂独立项目的持久卷。测试工具可以用 Python/rclpy；板载核心与执行网关使用 C++20。

验收客户端通过公开 ROS 服务提交巡检，订阅独立真值轨迹并检查依次到达两点。用例还应覆盖输入验证、幂等、冲突、取消停稳、核查重启后持久化和在途动作的保守恢复。生成 JSON，包括各目标、实测及误差、真值样本、任务状态、时间和通过/失败原因。仿真计时与宿主超时分别记录。默认运行与回归测试可使用不同 Compose 项目，防止旧数据库或世界姿态干扰结果。

Gazebo 差速系统与 ROS 桥参考 [Gazebo Harmonic 移动机器人说明](https://gazebosim.org/docs/harmonic/moving_robot/) 与 [ROS–Gazebo 桥](https://github.com/gazebosim/ros_gz/tree/ros2/ros_gz_bridge)。板卡验证另见[部署与真机验收规程](../architecture/board-validation.md)。

## 经过故障试验后的部署

控制桥传递 `/cmd_vel`、`/odom`、`/clock`、`/scan` 与 `/imu`，独立的 `witness` 桥转发 `/ground_truth`。里程计来自轮轴，真值来自 Gazebo 实体世界位姿。关闭控制桥后真值仍在更新，可用来独立测量失联后的停距。[控制桥](../../sim/bridge.yaml) · [真值桥](../../sim/witness.yaml) · [机器人基础模型](../../sim/world.sdf)

故障复现表明，DiffDrive 会保持收到的最后一条速度。因此在 Gazebo 进程内增加速度看门狗，对外接收速度，将经校验的命令送至底盘私有 topic。距最近命令 300 ms（仿真或本机单调时钟任一到期）即持续写零速，暂停和时钟回退会丢弃原命令。验收会在真实运动中强制终止 ROS 控制桥，独立观测从触发停机前开始持续到确认停稳。[看门狗](../../sim/velocity_watchdog.cpp) · [运动中故障观测器](../../tests/stop_witness.py)

## 在新开发机重建基础镜像

启动脚本默认使用官方 ROS 和 Ubuntu APT 源。本次 x86-64 发布验证用下列清华镜像源成功构建，保留发行版签名校验。该命令在项目根目录执行；ARM64 软件源和真机硬件支持需另行验证。

```powershell
docker build -f docker/Dockerfile.base -t edge-agent-ros/base:jazzy-harmonic --build-arg UBUNTU_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ubuntu --build-arg ROS_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ros2/ubuntu .
.\scripts\start.ps1
```
