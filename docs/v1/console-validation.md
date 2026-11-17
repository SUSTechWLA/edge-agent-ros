# Edge Agent ROS v0.1 联合验收 · 2026-11-17

这轮测试分别回答四个问题：自然语言任务是否真的完成、故障后是否重复操作、当时的数据是否还能找回，以及相同软件能运行在哪些计算单元上。

| 验收对象 | 实际施加的输入或故障 | 通过条件与证据 |
|---|---|---|
| 中文巡检和查询 | 提交两点巡检后报告状态、负坐标巡检、返回原点、读取当前位置。每个请求用同一 ID 重投。再提交自由表述的单点巡检，强制验收它经过模型规划。 | 全部成功。四个运动任务均用独立 Gazebo 真值检查终点误差小于 0.18 m。每个目标、实测坐标、误差和步骤均保存在 [missions.json](../../artifacts/console/missions.json)。 |
| 模型任务的完整性 | 让同一 Qwen 模型读姿态、拍照、同时读姿态并拍照。 | 首次曾误把状态读数作为照片。修复后的纯状态任务读取一次 `/odom`；两个需要照片的请求均报告缺少 `image`，零次工具派发。[模型对照实验](../model.md) |
| 进程恢复与资源互斥 | 独立仿真中运动时强杀 Agent；另一任务同时申请底盘。 | 网关对原机器人动作只记录一次启动。第二项任务退避并在第一项释放底盘之后取得所有权，两个都完成。账本包含同一资源上的先后序号。[故障实测](../../artifacts/console/resilience.json) |
| 工具热拔插与续办 | 运行中注册只读插件，首次响应 HTTP 503；移除清单。另外将重试预算设为零，等待故障后由操作员续办。 | 自动和人工重试均沿用原操作 ID，保留第一次失败原因。移除后新请求被阻止。[回归脚本](../../tests/console_resilience.py) |
| 机器人停车 | 首次重复测试见到轮速短暂变低、车体回弹。修复后从四个行进方向分别取消任务，再分别注入内核进程和控制桥失联。 | 在 Gazebo 的独立世界位姿上检查终态后的速度和位移，保留 0.05 m/s、0.025 m 和角速度 0.10 rad/s 的验收门限。四次取消均取得停稳证据；失联时内核保留未决状态，阻止新任务复用底盘。[原始失败](../../artifacts/verification/20261117-031103-3a9cc1/cancel.json) · [修复后的四次取消](../../artifacts/verification/20261117-031532-ae63c8/cancel.json) · [全套结果](../../artifacts/verification/20261117-031532-ae63c8/result.json) |
| 传感器、回放与归档 | 输入持续到包但采集时间冻结的位姿，暂停感知状态；热插拔 ROS 测距话题并恢复断流；回放历史和清理周期快照。 | 失效与恢复独立留档。移除测距源后可读历史，老事件不会混入后来的测量或成功状态，重要事件在滚动清理中保留。[容器内测试输出](../../artifacts/console/runtime-tests.txt) · [样例任务事件](../../artifacts/console/sample-trace.ndjson) |
| 浏览器及硬件镜像 | 桌面和手机宽度交互；AMD64、ARM64 硬件部署中注入带时间戳的 ROS 输入，实际调用状态任务。 | 可浏览地图、原始读数、额外传感器、事件与回放。ARM64 在 QEMU 中运行，实板驱动、时延和长期热稳定性需在目标机测量。[平台证据](../../artifacts/console/platforms.json) · [界面用例](../../artifacts/console/sensors-ui.json) |

## 复测入口

本页表格保留早期工作台与基线测试的原始结果。当前版本的测试入口、失败复现和通过范围见[单机系统测试报告](../testing/single-edge.md)。

Windows 在项目根目录执行。第一项使用隔离的 Compose 项目。第二项会移动日常仿真中的机器人，应在空闲的 Gazebo 工作台执行。`--model` 要求已配置并启动模型。第三项会另建并清理专用仿真容器、插件和卷。Linux/macOS 可通过相同的 Python 3 脚本访问运行中的仿真端口。

```powershell
.\scripts\verify.ps1
python tests/console_acceptance.py --model
python tests/console_resilience.py
docker run --rm --network none edge-agent-ros:v0.1 python3 -m unittest discover -v -s /opt/edge/console/server
docker run --rm --network none edge-agent-ros:v0.1 python3 /opt/edge/tests/planning_contract.py -v
```

这些结果覆盖巡检、只读任务、传感器失效、进程故障与恢复。连续运行数日的资源增长、真实底盘制动、网络跨域协同以及其他机器人形态仍需各自验收。
