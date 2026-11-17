# Edge Agent ROS v0.1 · 端侧任务与机器人观测台

在浏览器中输入“依次巡检 (0.8,0) 和 (0.8,0.7)，然后报告状态”，观察机器人实际移动、雷达扫描、融合轨迹与地图更新，再按一个任务 ID 回看每个动作的执行证据。运动控制与巡检内核使用 C++20 和 ROS 2 Jazzy，语言代理、感知与观测台独立运行。日常开发使用 Docker 和 Gazebo Harmonic。

[打开本机观测台](http://127.0.0.1:8765/) · [观测、下任务与排错](docs/console.md) · [跨平台部署与硬件接入](docs/deployment.md) · [能力插件契约](plugins/README.md)

## 第一次运行

在 Windows PowerShell 进入项目。启动脚本构建镜像、启动八个服务并检查 ROS 和 HTTP 接口。浏览器打开上方观测台即可下任务。Linux/macOS 使用 `sh scripts/deploy.sh sim up`，Linux 实机使用 `sh scripts/deploy.sh hardware up`。

默认基础镜像由项目的 [Dockerfile.base](docker/Dockerfile.base) 从公开的 `ros:jazzy-ros-base` 构建。新机器第一次需要能够访问 Docker Hub 及 Ubuntu、ROS 的软件源；可使用 Docker Desktop 已配置的 VPN/代理。已缓存底图时，应用重新编译和仿真任务无需下载地图、模型或密钥。也可通过 `EDGE_BASE_IMAGE` 指定含相同 SDK 的预装镜像。

```powershell
cd D:\project\edge-agent-ros
.\scripts\start.ps1
.\scripts\mission.ps1 run my-first-inspection "0.8,0.0" "0.8,0.7" --wait
.\scripts\mission.ps1 status my-first-inspection
```

成功响应包含 `state=SUCCEEDED`、`verified_count=2` 和两个按顺序排列的实测位置。坐标是米。同 ID、同目标重投返回原任务；想重新走一遍，换一个 ID。可以提交一至八个点，x、y 均在 [-4,4] 米范围内。此版适配平坦空旷场地，会按航向和距离调节速度。

```powershell
.\scripts\mission.ps1 run cancel-example "-2,1" "0,0"
.\scripts\mission.ps1 cancel cancel-example --wait
docker compose ps
docker compose logs --tail 30 core gateway
docker compose stop
docker compose start
```

客户端等候超时只会结束本地等待。用同一任务 ID 查询结果，明确取消请调用 cancel。状态保存在 Compose 命名卷 `state` 中；普通 stop/start 或 down/up 会保留账本。重新创建整套仿真会将 Gazebo 场地复位，账本里的历史位置代表过去那次运行。

## 运行完整验收

```powershell
.\scripts\verify.ps1
```

验收使用临时、独立命名的 Compose 项目，和日常运行的机器人隔离。脚本会注入两次进程级故障，完成后清理当次临时容器和卷。JSON、实际轨迹、SQLite 只读导出、单元测试输出和容器日志保存在 `artifacts/verification/<UTC时间-运行ID>/`。已构建同一版镜像时可以传 `-SkipBuild`。查看[本版验收报告](docs/v1/validation.md)。

| 想弄清的问题 | 从哪里看 |
|---|---|
| 这次到底测了什么，失败案例与复测证据在哪？ | [联合验收与复测命令](docs/v1/console-validation.md) |
| 自然语言怎么变成动作？中途重启怎么知道从哪继续？ | [例子、接口和重试边界](docs/console.md)、[实测记录](artifacts/console/missions.json) |
| 如何测试插件热插拔、占用重试和恢复？ | [独立 Compose 故障测试](tests/console_resilience.py)、[实际执行证据](artifacts/console/resilience.json) |
| 本版做到了什么，哪些要求属于后续版本？ | [PRD 与验收标准](docs/v1/PRD.md) |
| Brain 或外部系统如何下任务？ | [公开 ROS 接口](docs/v1/interfaces.md)、[命令行客户端](client/mission.py) |
| 为什么每一步要单独验收？什么时候会拒绝重试？ | [任务策略与持久化](docs/v1/core.md)、[实现](src/edge_core/src/core_node.cpp) |
| 如何移动、确认取消、在失联时停稳？ | [执行网关](docs/v1/gateway.md)、[网关实现](src/edge_gateway/src/edge_gateway_node.cpp)、[仿真执行器看门狗](sim/velocity_watchdog.cpp) |
| 怎样独立证明机器人真的动过？ | [Docker 与 Gazebo 验收模块](docs/v1/simulation.md)、[黑盒测试](tests/verify.py) |
| 单 Edge 将来怎样接模型和多机器人？ | [目标架构教材](docs/architecture/index.html)、[板卡验收](docs/architecture/board-validation.md) |

## 运行中断后怎么判断

- `SUCCEEDED` 要求每个观测点都有一条到达后的新鲜里程计证据。查询命令返回这些坐标；网关还单独确认了低速样本。
- `CANCELED` 表示当前移动已核实停止，或取消时没有在途移动。接受取消时可能会先看到 `CANCELING`。
- `FAILED` 的详情说明是派发、执行或验收阶段失败。检查日志与动作记录后可决定是否发起新的任务。
- `RECOVERY_REQUIRED` 会保留底盘占用。先检查独立世界真值和旧执行器状态，保留原卷用于追查。若要开始新的仿真实例，停止原栈，在新的 Compose 项目名下部署；本版没有在线解除未知占用的运维接口。

## 首版的部署边界

Windows Docker Desktop 上已验证完整仿真；Linux/ARM64 硬件镜像在指令模拟环境验证过 ROS 输入、感知、任务与观测台链路。当前运动网关适用于空旷平面。复杂地形需接入避障导航，其他机体需实现对应的动作验收。跨区域 Brain 协作、RK3588/Jetson 上的驱动、加速器与制动距离仍需现场验收。[部署矩阵](docs/deployment.md)
