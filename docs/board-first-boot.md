# ARM64 首次上板

现场包包含运行于 CPU 的 linux/arm64 硬件镜像、同一提交的源码、`IMAGE`（镜像标签）、`REVISION`（完整提交号）和 `SHA256SUMS`。在装有 Linux Docker Engine 的 RK3588 和 Jetson Orin 上使用相同镜像。板端需有厂商 BSP 和可发布 ROS 2 消息的驱动。x86-64 工控机可在本机从源码构建 amd64 镜像。[板型与加速器说明](deployment.md)

## 1　在板端的包目录加载

把整个包目录复制到板端，进入一个还没有 `edge-agent-ros/` 子目录的位置。以下命令在一个 shell 中运行，`cd` 之前的位置就是 `SHA256SUMS` 所在目录。可先检查 `uname -m` 显示 `aarch64` 并确认 Docker 使用本地 Linux 引擎。

```sh
docker compose version
sha256sum -c SHA256SUMS
export EDGE_HARDWARE_IMAGE="$(cat IMAGE)"
expected_revision="$(cat REVISION)"
docker load -i hardware-linux-arm64.tar.gz
test "$(docker image inspect "$EDGE_HARDWARE_IMAGE" --format '{{.Os}}/{{.Architecture}}')" = linux/arm64
test "$(docker image inspect "$EDGE_HARDWARE_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$expected_revision"
tar -xzf edge-agent-ros-source.tar.gz
cd edge-agent-ros
docker compose -f compose.hardware.yaml -f compose/hardware-observe.yaml config --quiet
```

校验、镜像检查或 Compose 解析任何一项失败，先修复后再继续。源码包是固定提交的快照，离线模式直接使用 `IMAGE` 中的标签即可。后续命令都在刚解压的 `edge-agent-ros/` 下执行；新开 shell 时重新导出上面的镜像标签和下面的现场配置。

## 2　确认输入后再开放任务

用板端实际参数替换下面的示例，读取状态页的是板端回环地址。可在开发电脑运行 `ssh -L 8765:127.0.0.1:8765 用户名@板端地址`，再打开 `http://127.0.0.1:8765/`。

```sh
export EDGE_ROBOT_ID=rover-01 EDGE_ROS_DOMAIN_ID=36
export EDGE_ODOM_TOPIC=/robot01/odom EDGE_IMU_TOPIC=/robot01/imu EDGE_SCAN_TOPIC=/robot01/scan
export EDGE_ODOM_FRAME=odom EDGE_BASE_FRAME=base_link EDGE_IMU_FRAME=base_imu EDGE_LASER_FRAME=front_laser
sh scripts/deploy.sh hardware observe
mkdir -p artifacts/verification/field
sh scripts/deploy.sh hardware preflight > "artifacts/verification/field/$(date -u +%Y%m%dT%H%M%SZ)-inputs.json"
```

观察模式启动感知、Console 和 `task-facts`。控制 Agent 停机时，仍可从工作台的任务列表和运行复盘读取这台机器已保存的任务与步骤。只有事实服务以只读方式挂载任务卷。它在板端回环地址监听，默认端口 8091，可通过 `EDGE_FACTS_PORT` 调整。

退出码 0 表示选定话题、坐标系和连续采样达到现场预检条件。只接里程计时可以先运行 `sh scripts/deploy.sh hardware preflight odom`。查看故障与时钟偏差的解释，请从解压后的源码打开 `docs/deployment.md`。

如果 `observe` 最后显示 `Connection refused`，记下当时的端口、机体 ID 和版本，保留容器与数据卷。在同一目录、同一组环境变量下运行以下指令，区分容器退出、工作台仍在启动、端口未监听和传感器缺失。

```sh
sh scripts/deploy.sh hardware status
sh scripts/deploy.sh hardware logs console perception
sh scripts/deploy.sh hardware observe
sh scripts/deploy.sh hardware preflight
```

看到 `Console ready` 后，再查看预检报告中的 `task_proxy_mode`、`robot_id`、`ros_domain_id` 与 `issues`。例如在未连接驱动的台架上，工作台就绪后仍应报告 `odom.missing`、`imu.missing` 和 `scan.missing`。在确认身份和输入前保持只读。

## 3　启用任务与退回观察

1. 核对机器人 ID、在途任务账本和实际 ROS 域。保持机体执行器未使能，运行 `sh scripts/deploy.sh hardware start`，向工作台下发“报告当前位置”。比对 ROS 里程计与任务 Session 的采样时间、frame、位置和 trace。
2. 真机移动需另行安装与机体匹配的控制驱动，实现 `edge_interfaces/action/NavigateTo` 的网关，接入机体侧限速、通信失联停车和物理急停。在批准的现场条件验证取消、停稳和人工接管，再开放短距离运动。上板包没有为所有底盘提供通用运动控制器。
3. 退回观察时，先用机体控制器取消运动并独立确认停稳，再执行 `sh scripts/deploy.sh hardware stop core agent` 和 `sh scripts/deploy.sh hardware observe`。设备制动及急停由机体负责；关闭容器只会停止软件进程。
