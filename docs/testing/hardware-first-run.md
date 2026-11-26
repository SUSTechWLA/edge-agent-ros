# 真机首测预检与第一条只读任务

首次上板的目标是让工作台以这台机器的新鲜里程计回答“报告当前位置”，并留下可复核的回执。全过程保持执行器未使能，在工位专用的 ROS 2 域内操作。

## 入场条件

- 核对机体铭牌、工位、操作员与 ROS 域。机体已经独立确认静止，现场人员能操作物理急停并接管设备。先查旧任务账本，任何在途动作或执行结果不明的操作都要先按原操作 ID 对账。
- 板端需有 Linux、Docker Engine、Compose v2、厂商 BSP 和现场 ROS 2 驱动。记录里程计、IMU、二维激光的真实话题、frame 和时间源。位置查询依赖 `odom → base_link`；地图导航还要验证完整的 `map → odom → base_link`，确认定位源、发布者、变换方向和时间同步。按传感器序列号核对到机身的安装外参、相机内参及标定记录。基础预检只检查选定消息的采样和 frame，TF、外参和标定仍需现场核对。[出厂与标定指南](../rover-commissioning.md)
- 使用**从本轮源码生成、含 `task-facts` 服务的 `deploy/robot` 投影**及同版板端镜像。`ROBOT_SOURCE` 记录对应的完整开发提交。ARM64 离线套件从包含 `SHA256SUMS`、`START-HERE.md` 的包目录开始，在尚无 `edge-agent-ros/` 子目录的位置执行以下命令。已有部署树和镜像的板端可从核对 `ROBOT_SOURCE` 开始。校验失败立即停下。现场排查可参考首次上板指南的输入检查章节；当前 bundle 的加载方法见随包的 `START-HERE.md`。[首次上板的输入检查](../board-first-boot.md) · [部署套件操作手册](../../deploy/robot/README.md)

```sh
set -eu
sha256sum -c SHA256SUMS
git clone --single-branch --branch deploy/robot edge-agent-ros-robot.bundle edge-agent-ros
docker load -i hardware-linux-arm64.tar.gz
cd edge-agent-ros
source_revision=$(cat ROBOT_SOURCE)
export EDGE_HARDWARE_IMAGE="edge-agent-ros/hardware:robot-$(cut -c1-12 ROBOT_SOURCE)"
test "$(uname -m)" = aarch64
test "$(docker image inspect "$EDGE_HARDWARE_IMAGE" --format '{{.Os}}/{{.Architecture}}')" = linux/arm64
test "$(docker image inspect "$EDGE_HARDWARE_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$source_revision"
docker compose -f compose.hardware.yaml -f compose.hardware-observe.yaml config --services | grep -Fx task-facts
```

## 1. 在 observe 模式核对机体

把示例替换成这台机器的现场参数，在同一 shell 中执行。

```sh
export EDGE_ROBOT_ID=rover-01 EDGE_ROS_DOMAIN_ID=36
export EDGE_ODOM_TOPIC=/robot01/odom EDGE_IMU_TOPIC=/robot01/imu EDGE_SCAN_TOPIC=/robot01/scan
export EDGE_ODOM_FRAME=odom EDGE_BASE_FRAME=base_link EDGE_IMU_FRAME=imu_link EDGE_LASER_FRAME=front_laser
sh scripts/robot.sh observe
sh scripts/robot.sh status
```

核对 `perception`、`task-facts`、`console` 的运行状态。工作台默认位于板端回环地址 `http://127.0.0.1:8765/`，可查看传感器和旧任务。观察模式拒绝新建任务和重试。若已有控制服务运行、工作台不可达或机体身份不符，停在这一步；用 `sh scripts/robot.sh logs console perception` 保存现场信息。

## 2. 用 preflight 保存连续采样证据

```sh
receipt_dir="$HOME/edge-field-receipts/$EDGE_ROBOT_ID"
mkdir -p "$receipt_dir"
receipt="$receipt_dir/$(date -u +%Y%m%dT%H%M%SZ)-preflight.json"
sh scripts/robot.sh preflight > "$receipt" || { cat "$receipt"; exit 1; }
cat "$receipt"
```

退出码应为 0，报告应有 `passed=true`、`issues=[]`、`task_proxy_mode=observe`。核对 `revision`、`robot_id`、`ros_domain_id`，再比对 `samples` 与 `previous_stamps`。选定话题和 frame 应与现场配置一致，里程计 `child_frame` 正确，各路采样时间均前进。出现 `.missing`、`.stale`、`.frame_mismatch`、`.clock_mismatch` 或 `.stamp_not_advancing` 时保持观察，修复后再保存新报告。若工位按配置仅接里程计，位置查询可用 `sh scripts/robot.sh preflight odom`，并在回执中注明只验了该输入。

## 3. 提交第一条只读任务“报告当前位置”

确认执行器仍未使能、账本没有待续办的动作，且现场配置中的 `status` 工具只读本机里程计，才运行 `sh scripts/robot.sh start`。它会启动常规的 `core`、`perception`、`agent`、`console`、`session`、`evidence`，工作台随之开放任务提交。此时只提交 **“报告当前位置”**。

打开任务详情，保存任务 ID、trace ID 和步骤回执。应看到 `SUCCEEDED` 和 `status`。将证据中的 `source`、`frame`、`stamp_ns`、`x`、`y` 与现场驱动同一时刻的里程计对照。若出现 `tool.odom_stale`、`RECOVERY_REQUIRED`、来源不符或实际运动，停止发任务并保留原操作 ID。现场人员处置执行器、独立确认停稳后，执行 `sh scripts/deploy.sh hardware stop core agent`，再用 `sh scripts/robot.sh observe` 返回观察。

## 本地导航契约与现场放行条件

工作站可在板端软件镜像内运行 `python3 -B /opt/edge/tests/system/navigation_contract.py --out /evidence/navigation-contract.json`。容器使用 `--network none` 和专用的非零 `ROS_DOMAIN_ID`，只读挂载开发树的 `tests/`，并挂载可写的证据目录。夹具让生产 `edge_core` 连接合成 ROS Action 端点和里程计，检查到达、到达后只有陈旧里程计、取消、停稳未知，以及 core 重启后的操作对账。[本地契约脚本](../../tests/system/navigation_contract.py)

本项未进行真机测试，也未在原生 ARM64 板卡验证。开放真实导航前，现场另需适配该底盘的 `edge_interfaces/action/NavigateTo` 网关。在隔离工位验证机体侧限速、物理急停、失联停车和人工接管，用独立测量记录停稳时间与制动距离。任一项缺少证据，保持执行器未使能。[现场故障与上板验收顺序](edge-field-readiness.md)
