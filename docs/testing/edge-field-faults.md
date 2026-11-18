# 上真机前的故障演练与任务回溯

本轮在本地 Docker 的独立 ROS 域和 Gazebo 测试项目里注入故障。在日常校园工作台另外保存了四个用户请求，供值班人员点击复盘。真实板卡、飞控、雷达与急停链路应分别在台架上验证。完整原件保存于 `artifacts/verification/edge-field/faults/`，随仓库留存的[机器可读摘要](../evidence/edge-field-faults.json)含镜像身份、关键回执与文件哈希。

## 从一条用户请求进入回溯

| 日常校园任务 | 实际执行与结果 | 从工作台打开 |
|---|---|---|
| 查询机器人状态 | status 读取当前里程计，成功，7 条归档事件 | [会话](http://127.0.0.1:8766/#sessions?task=edge-field-c25ecfd01f-status) |
| 查询场景地图 | describe_scene 返回地图描述，成功，9 条事件 | [会话](http://127.0.0.1:8766/#sessions?task=edge-field-c25ecfd01f-scene) |
| 只规划到教学楼门外的路线 | preview_route 返回路径，成功，9 条事件 | [会话](http://127.0.0.1:8766/#sessions?task=edge-field-c25ecfd01f-route) · [运行复盘](http://127.0.0.1:8766/#review?task=edge-field-c25ecfd01f-route) |
| 拍一张照片 | 当时任务目录缺少能交付 image 的已注册工具，规划受阻，0 个执行步骤，3 条事件 | [受阻原因](http://127.0.0.1:8766/#review?task=edge-field-c25ecfd01f-photo) |

以上数据来自部署升级前的校园。前三项的 operation ID 均为任务 ID 加 `/step/1`，照相请求在派发前结束。这四次下发在同一次仿真会话完成，车体真实位置首尾变化小于一厘米。如果新环境更改了能力目录，相同的拍照请求可能得到不同结果。[日常四项原始任务与事件](../../artifacts/verification/edge-field/faults/daily-requests.json)

## 在哪一层找到失败原因

| 故障注入 | 独立核验的行为 | 原始记录 |
|---|---|---|
| 14 项 Agent 黑盒，包括并发重复触发、模型遗漏导航、虚构工具、回执截断、慢响应、非有限测量、不同目标复用同一 ID | 请求冲突返回 409。三步任务中途杀进程后，各步调用次数为 1、2、1，原操作收据只对应一个效果。替换内核账本后进入 RECOVERY_REQUIRED，新账本创建运动任务数为 0 | [Agent](../../artifacts/verification/edge-field/faults/agent.json) |
| 移动中杀 Agent、第二任务争抢底盘、只读插件首调 503、卸载后继续请求 | 重启后网关记录一次启动。原任务释放时代号为 10，第二个在 11 获取底盘。插件重试沿用原操作 ID，响应依次为 503、200 | [恢复与资源争用](../../artifacts/verification/edge-field/faults/resilience.json) |
| 32 个正常触发和 2 个故障触发，另投递 68 次重复请求 | 完成 34 个原始任务。短时故障自动续办；持续缺少里程计触发 tool.odom_stale，跨重启保留待恢复状态，恢复驱动后显式重试成功，第二次重复重试返回 409 | [连续运行](../../artifacts/verification/edge-field/faults/lifecycle/5eed4f3c6d21/summary.json) |
| 十类轮式车雷达/时间故障，另在 Gazebo 行进中移除前向有效扇区 | 正常网关判定阻塞、扫描无效、超时或停止待确认。Gazebo 个案在 0.1042 秒观测到零速，世界真值测得故障后最远移动 0.042 米，最后 300 毫秒漂移为 0 米 | [ROS 注入](../../artifacts/verification/edge-field/faults/motion-sensors.json) · [Gazebo 真值](../../artifacts/verification/edge-field/faults/motion-world.json) |
| Console 断开、场景服务重启、时钟回到零点、事件源缺段 | 对同一未知地点的拒绝只归档一次。时钟由 18.596 秒回退后，重置点的旧地图失效，旧轨迹清空；0.626 秒时出现新地图。事件总线明确记录源序号 1 至 6 缺失以及随后的第 7 条 | [掉线补传](../../artifacts/verification/edge-field/faults/outage.json) · [时钟换代](../../artifacts/verification/edge-field/faults/clock.json) · [事件总线](../../artifacts/verification/edge-field/faults/bus.json) |
| Watchdog 独立运行时 Agent 或 Console 失联 | 每次事故有独立 trace、一次逻辑核对操作、原因和干预步骤。预算用尽只报一次人工处理，恢复后补齐已确认与恢复事件 | [Watchdog](../../artifacts/verification/edge-field/faults/watchdog.json) |

### 两轮修复解决了什么

用 Go1、G1 与 Crazyflie 输入回调注入 NaN 或缺项时，第一轮看到两个错误。飞行器拿到无效高度，旧控制逻辑仍请求上升 0.35 m/s。四足编码器到了一帧不完整的数据，旧角度仍可通过准入。修复在每台机体的缓存边界立即撤销无效输入，收到新鲜证据后恢复。无人机缺高度时发送零垂向速度，等待预算耗尽后保留本次失败的来源与回执，重投同一操作仍得到原结果。[规格与通过条件](../specs/SDD-033-invalid-body-feedback.md)

第二轮从单进程扩展到独立提供者进程，走真实 Gazebo Transport 位姿、ROS 2 关节/IMU、HTTP 请求以及独立 ROS 速度订阅者。三种机体的错误可同时检测，Go1/G1 仍可发布位姿。测试还发现 SIGTERM 原来先销毁 ROS 发布上下文，最后一次零速发布抛错。调整退出顺序后，在新一轮飞行请求中向提供者发送 SIGTERM，订阅端收到新零速，进程以 0 退出。本项验证到发布/订阅边界，飞控收到指令后的制动距离与进程被断电后的失联行为需实机测量。[修复前断言](../../artifacts/verification/edge-field/faults/feedback-before.txt) · [修复后断言](../../artifacts/verification/edge-field/faults/feedback-after.txt) · [中间件回执](../../artifacts/verification/edge-field/faults/middleware-feedback.json)

## 能自动补全什么能力

在与工作台隔离的本地网络，用本机缓存的 **Qwen2.5:1.5b** 作为作者模型，实际执行了两项能力补全。第一次请求按 km/h 报告前向速度，原始任务缺少工具而受阻。系统留下原记录，提议只读 Tool，经 3 个独立样例和部署策略校验后安装，创建继承同一 trace 和发布事件的续办任务。续办读到测试 ROS 里程计的 −0.5 m/s，返回 **−1.8 km/h**。另一次纯软件请求要求给出 `[100,103,111,115]` 的最大相邻采样间隔，续办返回 **8**。两项均只下发一次用户请求。[本地模型原始验收](../../artifacts/verification/edge-field/faults/evolution-qwen.json)

采用确定性作者协议替身，单独复测了权限与容错边界。未获批准的请求进入人工复核，撤下传感器后无法取得测量，新旧插件版本冲突时保留原发布记录。事件和回执证明的是可审核的只读能力安装；未知执行结果、动力硬件和任意源码升级各有独立授权和验收要求。[策略与撤回原始验收](../../artifacts/verification/edge-field/faults/evolution.json)

## 复测

在项目根目录运行。将 `<镜像>` 替换为对应代码构建的本地镜像，产物存入新的运行目录，以便与本次基线比较。

```sh
python tests/system/agent_faults.py --image <镜像> --out artifacts/verification/field-repeat/agent.json
python tests/system/module_bus_acceptance.py --image <镜像> --out artifacts/verification/field-repeat/bus.json
python tests/system/watchdog_acceptance.py --image <镜像> --out artifacts/verification/field-repeat/watchdog.json
python tests/system/plugin_manager_acceptance.py --image <镜像> --ollama-cache-volume edge-agent-ros_models --out artifacts/verification/field-repeat/evolution-qwen.json
python tests/system/field_readonly_acceptance.py --url http://127.0.0.1:8766 --out artifacts/verification/field-repeat/requests.json
```

中间件故障测试已纳入系统套件的 `campus-world` 阶段。测试容器独立挂载空的执行器账本、测试布局，使用 `--network none`。试验生成的速度消息只能在该容器内被观察。[故障工装](../../tests/system/campus_invalid_feedback.py) · [套件入口](../../tests/system_suite.py)

首次运行恢复脚本时宿主 Python 在首次 HTTP 查询处报告 `LookupError: unknown encoding: idna`，尚未下发任务。单独验证宿主 codec 可加载，并用同一命令重跑，四个业务故障用例均通过。初次异常保留在[首次运行记录](../../artifacts/verification/edge-field/faults/resilience-initial.json)。
