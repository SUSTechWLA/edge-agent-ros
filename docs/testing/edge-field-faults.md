# 上真机前的故障演练与任务回溯

本轮在本地 Docker 的独立 ROS 域和 Gazebo 测试项目里注入故障。在日常校园工作台另外保存了四个用户请求，供值班人员点击复盘。真实板卡、飞控、雷达与急停链路应分别在台架上验证。完整原件保存于 `artifacts/verification/edge-field/faults/`，随仓库留存的[机器可读摘要](../evidence/edge-field-faults.json)含镜像身份、关键回执与文件哈希。

## 从一条用户请求进入回溯

| 日常校园任务 | 实际执行与结果 | 从工作台打开 |
|---|---|---|
| 查询机器人状态 | status 读取当前里程计，成功，7 条归档事件 | [会话](http://127.0.0.1:8766/#sessions?task=edge-field-c25ecfd01f-status) |
| 查询场景地图 | describe_scene 返回地图描述，成功，9 条事件 | [会话](http://127.0.0.1:8766/#sessions?task=edge-field-c25ecfd01f-scene) |
| 只规划到教学楼门外的路线 | preview_route 返回路径，成功，9 条事件 | [会话](http://127.0.0.1:8766/#sessions?task=edge-field-c25ecfd01f-route) · [运行复盘](http://127.0.0.1:8766/#review?task=edge-field-c25ecfd01f-route) |
| 拍一张照片 | 当时任务目录缺少能交付 image 的已注册工具，规划受阻，0 个执行步骤，3 条事件 | [受阻原因](http://127.0.0.1:8766/#review?task=edge-field-c25ecfd01f-photo) |

以上数据来自部署升级前的校园。前三项的 operation ID 均为任务 ID 加 `/step/1`，照相请求在派发前结束。这四次下发在同一次仿真会话完成，车体真实位置首尾变化小于一厘米。如果新环境更改了能力目录，相同的拍照请求可能得到不同结果。日常四项原始任务与事件 `daily-requests.json`

## 在哪一层找到失败原因

| 故障注入 | 独立核验的行为 | 原始记录 |
|---|---|---|
| 14 项 Agent 黑盒，包括并发重复触发、模型遗漏导航、虚构工具、回执截断、慢响应、非有限测量、不同目标复用同一 ID | 请求冲突返回 409。三步任务中途杀进程后，各步调用次数为 1、2、1，原操作收据只对应一个效果。替换内核账本后进入 RECOVERY_REQUIRED，新账本创建运动任务数为 0 | Agent `agent.json` |
| 移动中杀 Agent、第二任务争抢底盘、只读插件首调 503、卸载后继续请求 | 重启后网关记录一次启动。原任务释放时代号为 10，第二个在 11 获取底盘。插件重试沿用原操作 ID，响应依次为 503、200 | 恢复与资源争用 `resilience.json` |
| 32 个正常触发和 2 个故障触发，另投递 68 次重复请求 | 完成 34 个原始任务。短时故障自动续办；持续缺少里程计触发 tool.odom_stale，跨重启保留待恢复状态，恢复驱动后显式重试成功，第二次重复重试返回 409 | 连续运行 `lifecycle/5eed4f3c6d21/summary.json` |
| 十类轮式车雷达/时间故障，另在 Gazebo 行进中移除前向有效扇区 | 正常网关判定阻塞、扫描无效、超时或停止待确认。Gazebo 个案在 0.1042 秒观测到零速，世界真值测得故障后最远移动 0.042 米，最后 300 毫秒漂移为 0 米 | ROS 注入 `motion-sensors.json` · Gazebo 真值 `motion-world.json` |
| Console 断开、场景服务重启、时钟回到零点、事件源缺段 | 对同一未知地点的拒绝只归档一次。时钟由 18.596 秒回退后，重置点的旧地图失效，旧轨迹清空；0.626 秒时出现新地图。事件总线明确记录源序号 1 至 6 缺失以及随后的第 7 条 | 掉线补传 `outage.json` · 时钟换代 `clock.json` · 事件总线 `bus.json` |
| Watchdog 独立运行时 Agent 或 Console 失联 | 每次事故有独立 trace、一次逻辑核对操作、原因和干预步骤。预算用尽只报一次人工处理，恢复后补齐已确认与恢复事件 | Watchdog `watchdog.json` |

### 两轮修复解决了什么

用 Go1、G1 与 Crazyflie 输入回调注入 NaN 或缺项时，第一轮看到两个错误。飞行器拿到无效高度，旧控制逻辑仍请求上升 0.35 m/s。四足编码器到了一帧不完整的数据，旧角度仍可通过准入。修复在每台机体的缓存边界立即撤销无效输入，收到新鲜证据后恢复。无人机缺高度时发送零垂向速度，等待预算耗尽后保留本次失败的来源与回执，重投同一操作仍得到原结果。[规格与通过条件](../specs/SDD-033-invalid-body-feedback.md)

第二轮从单进程扩展到独立提供者进程，走真实 Gazebo Transport 位姿、ROS 2 关节/IMU、HTTP 请求以及独立 ROS 速度订阅者。三种机体的错误可同时检测，Go1/G1 仍可发布位姿。测试还发现 SIGTERM 原来先销毁 ROS 发布上下文，最后一次零速发布抛错。调整退出顺序后，在新一轮飞行请求中向提供者发送 SIGTERM，订阅端收到新零速，进程以 0 退出。本项验证到发布/订阅边界，飞控收到指令后的制动距离与进程被断电后的失联行为需实机测量。修复前断言 `feedback-before.txt` · 修复后断言 `feedback-after.txt` · 中间件回执 `middleware-feedback.json`

## 能自动补全什么能力

在与工作台隔离的本地网络，用本机缓存的 **Qwen2.5:1.5b** 作为作者模型，实际执行了两项能力补全。第一次请求按 km/h 报告前向速度，原始任务缺少工具而受阻。系统留下原记录，提议只读 Tool，经 3 个独立样例和部署策略校验后安装，创建继承同一 trace 和发布事件的续办任务。续办读到测试 ROS 里程计的 −0.5 m/s，返回 **−1.8 km/h**。另一次纯软件请求要求给出 `[100,103,111,115]` 的最大相邻采样间隔，续办返回 **8**。两项均只下发一次用户请求。本地模型原始验收 `evolution-qwen.json`

采用确定性作者协议替身，单独复测了权限与容错边界。未获批准的请求进入人工复核，撤下传感器后无法取得测量，新旧插件版本冲突时保留原发布记录。事件和回执证明的是可审核的只读能力安装；未知执行结果、动力硬件和任意源码升级各有独立授权和验收要求。策略与撤回原始验收 `evolution.json`

## 跨重启的任务回溯是否完整

两次从空 Docker 项目启动 ROS 2 和 Gazebo，用 HTTP 交替提交“报告当前位置”和“只规划到庭院的路线”。另外各暂停、恢复 ROS 桥接两次，分别制造短暂和持续断流，在持续故障耗尽重试后重启 Agent。每个请求在受理后和本轮结束时分别重投一次。两轮使用独立的触发 ID 前缀，任务种类和故障顺序固定，规划器使用确定性的本地实现。[逐轮计数与告警证据](../evidence/edge-field-lifecycle.json) · [可重复运行的黑盒和审计入口](../../tests/system/lifecycle_acceptance.py)

| 项目 | 5eed4f3c6d21 / 5064f2f | 3b51c6905a88 / 9983c89 |
|---|---:|---:|
| 普通任务 + 含故障任务 | 32 + 2 | 80 + 2 |
| 额外重复投递 / 独立 trace / 独立 operation | 68 / 34 / 34 | 164 / 82 / 82 |
| 任务事件 / 工具请求 / 最终成功工具响应 | 286 / 39 / 34 | 670 / 87 / 82 |
| Agent 重启 / 独立订阅者收到的耗尽告警 | 1 / 1 | 1 / 1 |
| 用时（含容器启动与清理） | 49.031 秒 | 51.890 秒 |

116 个原始任务均有一次受理、一次派发、一次成功响应和一次验收。956 条任务事件的 ID 逐条核验，各自的父事件都能在同一任务中找到，跨两轮也没有 ID 碰撞。每轮只在两次故障中增加了工具请求。短时断流第 2 次调用成功，未请求人工续办。持续断流经历首次调用及 3 次失败重试，保留错误码 `tool.odom_stale`；重启后保持 `RECOVERY_REQUIRED`。恢复驱动并显式续办时第 5 次在原操作 ID 下成功，再次续办返回 HTTP 409。实验覆盖只读任务、传感器恢复及有界重试。连续运行总计约 101 秒，板载数小时运行、温升、内存增长以及对运动执行器的重复副作用仍有各自的实机验收。

日常校园的工作台还能直接查询巡检插件记录。取样时按模块查到 19 条事件，其中 4 条为超时复查，4 条为恢复。轮式车彩色图像的一次恢复记录为序号 2062，来源 `journal:sensor.health`，关联 `sensor.health.rgb`。按父事件 ID 找到同一话题的超时复查，按序号回放得到与列表一致的测量、3 份恢复样本和阈值。[日常巡检事件](http://127.0.0.1:8766/api/events?module=sensor.health&limit=100) · [本机恢复事件回放](http://127.0.0.1:8766/api/replay?at=2062) · [可在新环境查看的摘录](../evidence/edge-field-lifecycle.json)

## 三种机体如何区分视野变化、传感器故障和恢复

从运行中的校园 Gazebo 只读采集 Crazyflie 前视图、Go1 三维点云、G1 深度图，每台各五帧。保存订阅到的原始 ROS 2 序列化消息、话题、坐标系、仿真时间和 SHA-256。在与校园断网的 Docker 容器中，三个独立的 `sensor.health` 进程分别订阅各自机体的话题，独立写日志。回放时保留测量和坐标系，将消息时间戳换为回放时刻；故障阶段只修改指定机体的数据。恢复阶段重新发布校园原始测量。[验收脚本](../../tests/system/campus_sensor_replay.py) · [三条事件链及原帧哈希](../evidence/edge-field-sensor-replay.json)

| 机器人与信号 | 正常校园采样 | 人工故障与插件判定 | 恢复结果 | 回放升级事件 ID |
|---|---|---|---|---|
| Crazyflie 前视 RGB | 832 个采样像素中黑暗占比 0%，纹理边缘占比 13.51% | 模拟镜头完全遮挡，黑暗占比升至 100%，出现 `black_image`。持续 0.821 秒后请求关注 | 三帧后黑暗占比恢复至 0% | `7e1cbb7799c642d8b687d73208bee503` |
| Go1 三维激光 | 一帧 2,880 点，600 点抽样中 405 个有效 | 保留 8 个有效点时记录 `few_points`，保持观察。将整帧 XYZ 换成 NaN 后，600 个抽样全失效，0.846 秒后升级 `invalid_cloud_points` | 三帧后恢复到 405/600 有效 | `c9c8bf915aaa4c19944e6f3660132fa6` |
| G1 深度相机 | 浮点深度图 832 个采样点，原有空洞占 48.8%，低于插件 60% 阈值 | 全帧零深度使空洞占比升至 100%，0.831 秒后升级 `depth_holes` | 三帧后空洞占比回到 48.8% | `43470e0dc7ec4d99a60c1aa38a2cda2c` |
| G1 彩色相机 | 832 个像素采样中饱和占比 0%，纹理边缘占比 9.96% | 模拟全帧过曝，饱和占比升至 100%，0.802 秒后升级 `overexposed` | 连续三帧后饱和占比回到 0%，纹理回到 9.96% | `810873a057fd44b0a18596ccf9b2f154` |

重放两轮，每轮均通过 11 个阶段。各自的日志总计 13 条观测状态变更，每条都核验父事件 ID、原始输入文件、话题和机体归属。在单机型故障期间另外两台保持各自应有状态。这里将持续异常的复查窗口缩短为 0.8 秒以反复测量状态迁移，部署默认值为 6 秒。这项实验验证巡检插件能够定位输入退化及识别恢复；实际镜头污染、激光材质反射和深度相机失效的报警阈值还须在实机上标定。

表中前三项来自使用 G1 深度图的第二轮回放。随后对另一批校园采样执行完整的三机实验，G1 改订阅同一 RGB-D 相机的彩色图 `/model/g1/rgbd/image_raw`，另两台仍使用本机传感器。该轮同样通过 11 个阶段，复查窗口和三帧恢复判据一致。在后面的采集、回放两条命令中都加入 `--g1-color`，配合一个新输出目录即可复现。[G1 曝光测量、关联事件和帧哈希](../evidence/edge-field-g1-exposure.json)

## 复测

在项目根目录运行。将 `<镜像>` 替换为对应代码构建的本地镜像，产物存入新的运行目录，以便与本次基线比较。

```sh
python tests/system/agent_faults.py --image <镜像> --out artifacts/verification/field-repeat/agent.json
python tests/system/module_bus_acceptance.py --image <镜像> --out artifacts/verification/field-repeat/bus.json
python tests/system/watchdog_acceptance.py --image <镜像> --out artifacts/verification/field-repeat/watchdog.json
python tests/system/plugin_manager_acceptance.py --image <镜像> --ollama-cache-volume edge-agent-ros_models --out artifacts/verification/field-repeat/evolution-qwen.json
python tests/system/field_readonly_acceptance.py --url http://127.0.0.1:8766 --out artifacts/verification/field-repeat/requests.json
python tests/system/lifecycle_acceptance.py --image <镜像> --tasks 80 --out-root artifacts/verification/field-repeat/lifecycle
python tests/system/lifecycle_acceptance.py --audit-run artifacts/verification/field-repeat/lifecycle/<运行号>
```

中间件故障测试已纳入系统套件的 `campus-world` 阶段。测试容器独立挂载空的执行器账本、测试布局，使用 `--network none`。试验生成的速度消息只能在该容器内被观察。[故障工装](../../tests/system/campus_invalid_feedback.py) · [套件入口](../../tests/system_suite.py)

有正在发布三种传感器话题的校园环境时，在项目根目录使用 PowerShell 执行以下两步。第一步只读获取现场场景；随后可用同一份原件独立反复重放，每次给 `--out` 一个新目录。完整证据保存在基准目录下的 `campus-sensor-capture`、`campus-sensor-replay`、`campus-sensor-replay-repeat`。

```powershell
$root = (Get-Location).Path
$image = docker inspect edge-agent-campus-sensor-health-1 --format '{{.Config.Image}}'
$digest = docker image inspect $image --format '{{.Id}}'
$dir = Join-Path $root ('artifacts/verification/sensor-repeat-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $dir | Out-Null
$scriptMount = "type=bind,source=$root/tests/system/campus_sensor_replay.py,target=/tmp/check.py,readonly"
$runMount = "type=bind,source=$dir,target=/run-data"
docker run --rm --network edge-agent-campus_default -e ROS_DOMAIN_ID=73 -e RMW_IMPLEMENTATION=rmw_cyclonedds_cpp --mount $scriptMount --mount $runMount $image python3 -B /tmp/check.py --capture /run-data/capture
docker run --rm --network none -e ROS_DOMAIN_ID=182 -e ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST -e RMW_IMPLEMENTATION=rmw_cyclonedds_cpp -e "EDGE_VERIFICATION_IMAGE=$digest" --mount $scriptMount --mount $runMount $image python3 -B /tmp/check.py --replay /run-data/capture --out /run-data/replay
```

首次运行恢复脚本时宿主 Python 在首次 HTTP 查询处报告 `LookupError: unknown encoding: idna`，尚未下发任务。单独验证宿主 codec 可加载，并用同一命令重跑，四个业务故障用例均通过。初次异常保留在首次运行记录 `resilience-initial.json`。
