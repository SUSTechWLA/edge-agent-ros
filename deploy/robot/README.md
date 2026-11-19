# Edge Agent ROS · 机器人部署版

在装有 Docker Engine 和 Compose v2 的 Linux 计算单元运行。该分支由研发仓库生成。`ROBOT_SOURCE` 记录对应的完整研发提交。本地标定和任务数据分别保存在每台车的工厂卷和 Compose 卷。

## 第一次启动

ARM64 离线包可在 RK3588 或 Jetson 的 Linux 系统上按下列步骤展开。镜像包含 CPU 运行时；GPU 加速与具体板卡驱动按现场配置。已有部署代码与镜像的板子直接运行下一组启动命令。

```sh
sha256sum -c SHA256SUMS
git clone -b deploy/robot edge-agent-ros-robot.bundle edge-agent-ros
docker load -i hardware-linux-arm64.tar.gz
cd edge-agent-ros
```

```sh
export EDGE_ROBOT_ID=rover-001 EDGE_ROS_DOMAIN_ID=72
sh scripts/robot.sh observe
sh scripts/robot.sh preflight
```

第一条启动命令在本地镜像缺失时构建它，仅开启观测服务；首次构建需要可用的 ROS 基础镜像与软件源。离线工位可事先加载**同一源码版本**对应架构的镜像并设置 `EDGE_HARDWARE_IMAGE`。工作台默认在板端回环地址的 8765 端口。ROS 驱动运行在板端或兼容的 host-network 容器内，设备话题、frame 和 ROS 域按车配置。基础预检默认检查二维扫描、里程计和 IMU；三维雷达和相机由工厂采集程序分别验证。车上尚无二维扫描时可先运行 `sh scripts/robot.sh preflight odom imu`。

## 工厂标定

把 `calibration/station/*.example.json` 复制到独立工位目录，分别命名为 `profile.json` 和 `bindings.json`。用实际测量和实物序列号填写空值，给 `EDGE_FACTORY_DIR` 设置该目录的绝对路径。工装适配器可选提供 `targets.json`，运行 `sh scripts/robot.sh fit-targets` 求一个传感器的安装外参并交由工位核对。随后执行

```sh
sh scripts/robot.sh factory
sh scripts/robot.sh calibration-status
```

标定版本与机体、设备身份和工装观测绑定。更换支架或传感器后重新采集。查阅同一研发提交的 `docs/rover-commissioning.md` 可以获得工位文件格式、探索仿真及真机接入说明。首次运动需安装并验收本车定位、导航和急停适配。此版本默认交付硬件只读启动。

## 启动完整 Agent

现场验收通过后，`sh scripts/robot.sh start` 启动 Core、Agent、工作台、Session 与取证等服务。可用 `sh scripts/robot.sh status` 和 `sh scripts/robot.sh logs` 查看所选部署。停稳并完成任务交接后，可执行 `sh scripts/robot.sh stop` 停止服务，数据卷保留。切换回只读观测时重新执行 `observe`。工厂标定结果需要由具体的探索适配器检查，完整 Agent 的通用任务入口没有全局标定联锁。

## 目录和更新

| 路径 | 现场用途 |
|---|---|
| `scripts/robot.sh`、`compose.hardware*.yaml`、`docker/` | 板端入口、服务编排、容器启动环境 |
| `src/`、`agent/`、`client/` | ROS 接口、任务与执行记录、插件管理 |
| `calibration/`、`exploration/`、`perception/` | 标定、通用探索算法与传感器处理 |
| `console/`、`sessions_plugin/`、`evidence/` | 本机工作台、会话与取证 |
| `contracts/`、`policies/`、`model_provider/` | 运行契约、恢复策略和模型适配 |

每次上板固定部署提交和镜像标签；SDK、研发资料与仿真验收留在研发分支。待设备停稳并完成在途操作交接后切换版本。回退选用先前的部署提交及配套镜像。现场配置在板外保存备份，保留设备数据卷。
