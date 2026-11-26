# 在 Jetson 上用程序控制 Lingao 底盘

本手册适用于现场已部署的 Lingao 轮式底盘。SSH 是执行入口。通过 SSH 登录 Jetson 后，ROS 进程和 DDS 发布器在车端运行。硬件 ROS 网络使用 Humble、Domain 61 和 localhost-only。

```bash
ssh cas@192.168.0.144
source /opt/ros/humble/setup.bash
source ~/humble_ws/install/setup.bash
export ROS_DOMAIN_ID=61 ROS_LOCALHOST_ONLY=1
```

## 控制与反馈

| 接口 | 类型 | 用途 |
| --- | --- | --- |
| `/cmd_vel` | `geometry_msgs/msg/Twist` | 程序的普通速度入口，经过 `vehicle_mode_mux` |
| `/mode_mux/selected_cmd_vel` | `geometry_msgs/msg/Twist` | 原厂仲裁器输出，供诊断 |
| `/cmd_vel_safe` | `geometry_msgs/msg/Twist` | `vehicle_web_remote` 仲裁后的底盘驱动输入，开发者只订阅 |
| `/lingao/odom` | `nav_msgs/msg/Odometry` | 驱动回读，车体坐标系 `base_foot`，参考坐标系 `odom` |
| `/stop_cmd_vel` | `std_msgs/msg/String` | 原厂软件停车锁。收到 `stop` 后安全出口保持零速 |
| `http://127.0.0.1:7070/api/status` | HTTP GET | 软件停车锁、人工接管、遥控输入和轮速 |

`linear.x` 单位为 m/s，`angular.z` 单位为 rad/s。车头为正 X，俯视逆时针为正 Z。其余四个分量设为零。

| 动作 | `linear.x` | `angular.z` | 验收 |
| --- | ---: | ---: | --- |
| 停车 | 0 | 0 | 实车验证普通速度归零与软件停车锁 |
| 前进 | 正 | 0 | 实车验证 0.10 m/s |
| 后退 | 负 | 0 | 原厂接口支持，实车需单独完成后方覆盖与停车验收 |
| 原地左转 | 0 | 正 | 实车验证 +0.12 rad/s |
| 原地右转 | 0 | 负 | 原厂接口支持，实车需独立验收 |
| 弧线 | 非零 | 非零 | 车端接口支持组合输入。弧线中心半径约为 `abs(linear.x / angular.z)`，实车扫掠和制动待验 |

人工接管优先于普通速度，软件停车锁优先于两者。底盘实体急停独立于上述软件。原厂 MQTT 桥与普通程序共用 `/cmd_vel`，程序需要监测来源竞争。停车锁的解除恢复原厂速度仲裁，解除前先确认底盘静止、普通速度为零、没有活动任务，并由现场人员确认。设备重启或服务重启后重新检查停车状态。

## 可直接运行的试车工具

把整个 `scripts/lingao_control` 目录复制到 Jetson，包含 `control_profile.py` 和 `lingao-chassis-control.json`。这份 JSON 同时供仿真运动执行器、现场试车脚本、现场独立制动进程读取，定义话题、消息类型、车体坐标轴、速度单位与上限、停车锁和静止观察时长。每次现场操作回执中的 `chassis_profile_sha256` 可与仿真见证回执比对。现场工具在建 ROS 连接前核对已验收配置的内容，主进程在发令前还核对制动助手读到的文件摘要。[共享底盘配置](../../scripts/lingao_control/lingao-chassis-control.json)

现场专用检查仍包括 Jetson 身份、原厂进程归属、软件停车锁、人工接管、实车试车档位和独立截止。待测动作的授权和验收按现场流程取得。以下在 Jetson 上执行，已加载前述 ROS 环境。

```bash
cd /path/to/edge-agent-ros/scripts/lingao_control
cmake -S . -B build -DPython3_EXECUTABLE=/usr/bin/python3
cmake --build build -j2
python3 chassis.py --help
python3 chassis.py preflight forward-50cm
```

`preflight` 用带可识别符号位的零速消息验证三段话题，检查底盘当前回读、原厂急停订阅者、活动导航及人工接管。试车档位为 `forward-50cm` 和 `left-10deg`。在现场确认扫掠范围和实体急停可用后，选择一条命令执行。每次生成一个操作 UUID。中断后先按原 UUID 查回执，避免重复动作。

```bash
op="$(cat /proc/sys/kernel/random/uuid)"
python3 chassis.py run forward-50cm --operation-id "$op" --attended
cat "$HOME/.local/state/edge-lingao-control/$op.json"
```

要执行原地左转，将上面的档位改为 `left-10deg`。试车后软件停车锁保持生效。随后要重新试车时，由现场按原厂流程解除。程序有独立截止进程，检测到竞争速度、里程计过期、人工接管、停止信号或异常运动即停。控制接口一秒归零后的机械制动拖尾在本次实车观测中约 1.7 cm，留出额外净空。

其他车端程序可以用子进程复用同一入口。把调用方生成的 UUID 与业务任务一起保存，以便中断后对账。

```python
import json
from pathlib import Path
import subprocess
import sys

def attend_trial(script_dir: Path, profile: str, operation_id: str):
    result = subprocess.run(
        [sys.executable, str(script_dir / "chassis.py"), "run", profile,
         "--operation-id", operation_id, "--attended"],
        check=False,
    )
    receipt = Path.home() / ".local/state/edge-lingao-control" / f"{operation_id}.json"
    evidence = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else None
    return result.returncode, evidence
```

仅在操作者已经核对现场条件时调用此函数。判断物理结果时检查 `verified`、`mcu_stopped`、`final_hmi.estop` 和起止位姿。父进程或 SSH 在运行中断开后，沿原 UUID 确认结果。已武装却缺少停稳回执时，先用实体急停和实车状态复核。

## ROS CLI 与程序集成

以下命令可核对话题接线、底盘反馈和停车状态。

```bash
ros2 topic info /cmd_vel -v
ros2 topic info /cmd_vel_safe -v
ros2 topic echo /lingao/odom --once
curl -fsS http://127.0.0.1:7070/api/status
ros2 topic pub --once /stop_cmd_vel std_msgs/msg/String "{data: stop}"
```

原厂相对运动程序支持正负距离、正负相对航向。安装目录中的帮助可直接核对参数。

```bash
ros2 run lingao_base move_relative.py --help
```

程序参数 `--distance` 用米，`--speed` 用 m/s，`--angle` 用度，`--yaw-rate` 用度每秒。距离和角度二选一。原厂程序默认向 `/cmd_vel` 写入并订阅 `/lingao/odom`。场地程序调用时使用上面的受控工具。词义或任务调用保持在 Agent 层，控制器接收明确的操作 UUID、方向、限制和部署授权。DDS 消息到达与真实动作完成分别用安全出口和 MCU 验证。

| 常见问题 | 先核对 |
| --- | --- |
| `/cmd_vel` 有指令，原厂切换口始终为零 | 仲裁器模式、活动导航、跟随模式、DDS 图、同级控制来源 |
| 切换口有指令，安全口始终为零 | `/api/status` 中的 `takeover.estop`、`takeover.active` 和 `control.active` |
| 安全口有非零指令，轮式里程计没有变化 | 核对实体急停、驱动运行、供电和地面起步阻力。保留原请求与回执 |
| 停车后里程计过期或丢失 | 按未确认停稳处理，现场核对并对账原操作 |
| Nav2 或 MQTT 已经接手 | 从对应控制端取消或退出该任务，复核停稳后再提交新的试车操作 |

## 实车验收

| 试验 | 轮式里程计结果 | 停车 |
| --- | --- | --- |
| 0.10 m/s 直行 1 秒 | 9.39 cm，峰值 0.0995 m/s | 已停稳 |
| 目标前进 50 cm | 49.80 cm，峰值 0.1010 m/s | 约在发令后 5.06 s 请求停车，随后已停稳 |
| 目标原地左转 10° | 10.38°，中心最终偏移 0.84 mm，角速度峰值 0.1334 rad/s | 约在发令后 1.58 s 请求停车，随后已停稳 |

原始回执均含起止 `odom` 位姿、三段速度话题和轮式里程计序列。位移和转角来自轮式里程计。实车验证使用现场监护的单次命令。标定任务的无人自主换位还需物理运行时取得经过验证的碰撞制停、独占控制和短时授权。[实车结果与归档摘要](../evidence/lingao-chassis-commissioning-20261125.md) · [物理运行时](../specs/SDD-050-body-runtime.md)
