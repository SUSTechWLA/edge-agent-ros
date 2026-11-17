# 单机 Edge Agent 实测记录 · 2026-11-17

**一次值得回归的错误**是在工位的机器人报告“已到庭院”。当时任务状态为 `SUCCEEDED`，执行账本也有成功记录，Gazebo 独立测得机器人距庭院 **3.57 m**。旧记录对应另一个目标，却与新任务使用了同一操作 ID。现在重放该冲突时，系统返回 `BLOCKED / core.id_conflict`，不会拿旧记录证明新任务完成。这个例子说明为何团队同时需要检查内部账本和从外部观察实际行为。

**本地验收结论。** Windows 开发机通过 Docker 运行 Linux 容器。干净提交 `c113b9b` 的运行 `20261117T062741Z-49982651` 选中 18 个验收阶段，全部通过，含真实模型，耗时约 5 分 59 秒。旧版对照使用 `d74e71d`。附加的五次制动和八组资源实验使用 `ebb6bbb`，到全套验收版本之间的变更集中在测试、CI 与文档。资源实验包括两次内存耗尽。每批实验的镜像、版本和样本分开记录。[机器可读证据](../evidence/sdd-003-system.json) · [验收规格](../specs/SDD-003-simulation-system-tests.md)

## 一项行为怎样从两侧验证

| 本地验证方式 | 这次测试做了什么 | 测量边界 |
|---|---|---|
| 白盒：直接调用模块或检查其持久状态 | 调用规划器校验候选步骤、加载实际部署清单；检查 Journal 的触发去重、诊断和跟踪；复查已派发动作的原操作 ID 与已持久化证据。 | 擅长定位哪个分支作出了错误决定。函数返回通过还要从公开接口验证运行中的多个进程能配合，并验证实际位移。 |
| 黑盒：通过公开入口提交输入，观察结果 | 通过 HTTP 下发“去庭院巡检”，读取终态；经 ROS 投递不可信扫描，观察速度命令及独立世界位姿；在真实浏览器中查询位姿、工具和事件。 | 能发现跨层失配和错误成功。有些内部原因会产生相同界面现象，要关联任务 ID、操作 ID、事件与原始采样才能定位。 |

以“执行中重启”为例，从外部看到任务能继续只是第一步。三步工具任务在第二步中断后，三个步骤的提供者请求次数分别为 **1、2、1**。原操作 ID 和对应的已执行结果逐一核对，才能确认第二步重试没有再制造一次相同副作用。若替换成空的 ROS 账本，旧版会创建一条新委托；修复版产生 **0** 条，并保留 `RECOVERY_REQUIRED` 供核查。[故障注入与断言](../../tests/system/agent_faults.py)

## 18 个阶段查了什么

每个阶段名都对应本地 `summary.json` 的 `selected` 项。构建镜像、初始化模型缓存和清理容器另有日志。

| 范围 | 验收阶段 | 实际输入与判据 |
|---|---|---|
| 仓库、模型与工具契约 | `repository`、`planner`、`agent-contracts`、`scene-contracts` | 从规格到测试可追溯；加载仓库的工具清单；不合法的计划或清单可识别；地图名称、过渡区域、路线和回执有对应断言。 |
| 控制、停车与恢复 | `core`、`motion-sensors`、`motion-world` | 经过 ROS 执行、取消和断链；检查何时发零速、什么条件允许确认停稳、失去停车证据时能否阻止新动作。仿真位置另取自 `/ground_truth`。 |
| 上层任务与场景 | `agent-faults`、`resilience`、`console-missions`、`places`、`scene-binding`、`scene-outage` | 通过真实服务检验恢复、热插拔、并发、错误回执、室内外往返与旧目标冲突。控制台离线时保留场景事件，服务重启、重试、重新连接后归档只出现一条。 |
| 感知与人机交互 | `observations`、`clock-reset`、`ui` | 16 项观测与归档回归通过；验证时钟回拨和坏记录。1440 px 与 390 px 两种视口都能读取实时位置、打开工具对话框和查看事件，页面无横向溢出。 |
| 推理与板端软件 | `model`、`board` | Ollama 实际运行 `qwen2.5:1.5b`，选择 `status`，ROS 里程计提供结果；该次模型调用 4.47 秒，同一 trace 可导出包含父子任务因果的 16 条事件。另用无网络的硬件镜像验证无里程计和恢复两条路径。 |

## 最能区分修复前后的故障

| 复现条件 | 旧版行为或风险 | 修复后的独立观测 |
|---|---|---|
| 机器人行进中，激光正前方出现连续缺束 | 旧版没有及时触发缺束制动，注入后余行 65.07 cm。十组网关故障中 4 组达到预期。 | 十组全部达到预期，涉及近障、量程、传感器几何、丢帧、源时间冻结、跨时钟偏差、错 frame、回拨。另起五个新世界测中央缺束，发零速延迟 46.2–103.6 ms，中位 63.6 ms；余行 2.49–4.20 cm，中位 3.11 cm。五次停稳末 300 ms 在记录精度内均无位移。 |
| 工具超时、模型漏掉明确的导航要求、同 ID 不同目标、回执截断、账本被替换 | 14 组故障中 8 组达到预期。可观察到错误成功、遗漏用户要求或重复提交。 | 14 组全部达到预期。场景独立验证中，路线预览不发运动；另一个目标占用同一 ID 时返回冲突。 |
| 仿真时钟回零，或者扫描中没有可用点 | 旧观测可能进入新一轮地图和轨迹，无效扫描可能刷新地图时间。 | Gazebo 时间从 101.716 s 回到 0，重置瞬间地图不可用、轨迹为 0 条。再次接收本轮有效输入后，新地图时间为 0.626 s；上一轮地图时间为 100.376 s。 |
| 连续运行时归档接口中途截断，或历史数据带有非有限数 | 后续事件可能停止归档，状态查询可能失败。 | 观测测试验证恢复游标与去重、历史库兼容与继续查询。场景断连试验另验证事件原始负载和原事件 ID 在重试中保持一致。 |

正常路径也要留下独立对照。七项场景任务中，“报告当前位置”、庭院巡检、返回工位和故障后的回程均完成。未知地点与受限维修区在移动前被阻止；朝墙方向的坐标巡检已开始移动，由激光触发停车，世界位姿显示底盘停在墙前。三条完成的运动路线用 Gazebo 世界坐标测得末端误差依次为 **14.72、4.47、6.13 cm**，均低于该测试的 18 cm 判据。模型样例、场景路线和工具故障各自有单独的测量范围。[场景验收与误差计算](../../tests/semantic_acceptance.py)

## 板端能跑到什么程度

在 `c113b9b` 的 AMD64 硬件镜像和 ARM64 指令模拟镜像中，使用实际 core、perception、agent 和 console 四个进程，输入来自合成 ROS 里程计、IMU 和激光。缺少里程计时，状态任务进入 `RECOVERY_REQUIRED / tool.odom_stale`；补入输入后，沿同一操作 `missing-odom/step/1` 恢复至 `SUCCEEDED`，读到 `x=0.375 m`。全过程隔离外部网络，未连接 Gazebo 和执行器。

下表记录 `ebb6bbb` 的独立限额实验。每一格各运行一次，禁用 swap，内存预算覆盖 ROS 输入探针及四个服务，耗时含容器启动与恢复验证。ARM64 列包含在 AMD64 宿主上的 QEMU 指令模拟开销。

| CPU 配额 / 内存上限 | AMD64 原生容器 | ARM64 指令模拟容器 |
|---|---|---|
| 0.5 CPU / 384 MiB | 通过，4.47 s | 内存耗尽，内核终止进程，退出 137 |
| 1 CPU / 512 MiB | 通过，3.06 s | 内存耗尽，内核终止进程，退出 137 |
| 1 CPU / 768 MiB | 通过，3.05 s | 通过，28.34 s，内存峰值 562.04 MiB |
| 1 CPU / 1536 MiB | 通过，3.09 s | 通过，29.56 s，内存峰值 562.33 MiB |

真机验证时，应在 RK3588 或 Jetson 原生系统上重新测量进程、传感器和模型一起运行的峰值，记录控制延迟、温升与物理制动。当前数据只能解释上述负载在当前宿主的表现。板端急停、供电中断后的物理状态、外参及长时间热降频要由现场试验验收。[板卡验收手册](../architecture/board-validation.md)

## 如何复测并判读失败

在仓库根目录执行。白盒可先跑规划器及部署配置，再用已构建的项目镜像运行 Journal 与跟踪契约。修改 `docker/Dockerfile.base` 时，机器上若已存在默认基础镜像，先执行 `docker build -f docker/Dockerfile.base -t edge-agent-ros/base:jazzy-harmonic .`；构建脚本会复用本地已有标签。

```sh
python tests/planning_contract.py -v
pwsh -NoProfile -File scripts/build.ps1
docker run --rm --network none edge-agent-ros:v0.1 python3 /opt/edge/tests/agent_trace_contract.py -v
```

黑盒和集成回归使用一次性 Docker 项目及数据卷。本机已有含相应权重的模型卷时，运行第一条命令可复测这 18 个阶段。日常 PR 用后两条先审查消费者范围，再执行选中的阶段。每次都有自己的 `.ci-results/system/runs/<运行号>/summary.json`；查看其中的版本、具体阶段和相邻日志。

```sh
python tests/system_suite.py --ollama-cache-volume edge-agent-ros_models
python tests/system_suite.py --changed-since origin/main --plan
python tests/system_suite.py --changed-since origin/main
```

以下 PowerShell 例子重放 ARM64、1 CPU、768 MiB 一组。先准备所需硬件镜像，在机器可读证据中核对镜像摘要；示例读取已保存的四服务启动配置。重放低配组时，同步调整两个内存参数以维持禁用 swap 的条件，保留退出码和错误输出。

```powershell
$probe = (Resolve-Path tests/system/board_smoke.py).Path
$services = python -c "import json; print(json.dumps(json.load(open('docs/evidence/sdd-003-system.json', encoding='utf-8'))['resource_fixture']))"
$services | docker run --rm -i --init --network none --platform linux/arm64 --cpus 1 --memory 768m --memory-swap 768m -e ROS_DOMAIN_ID=154 -e RMW_IMPLEMENTATION=rmw_cyclonedds_cpp -v "${probe}:/board_smoke.py:ro" edge-agent-ros/hardware:arm64-check python3 /board_smoke.py --inside
$LASTEXITCODE
```

上述命令还在 `c113b9b` 的 ARM64 镜像单独复演过一次，退出码为 0，四个服务恢复了同一操作，读取 `/fixture/odom` 得到 `x=0.375 m`。这次复演用于检查读者能否由仓库证据重建输入；表中的八个样本保持原始版本与计数。

测试设施也会出错。例如只读模型缓存使 Ollama 无法重建索引，现改为将模型数据复制到独立可写卷，并在任务开始前验证模型已就绪。另一次 Linux 浏览器运行已生成 UI 结果，随后清理临时目录时报 `ENOTEMPTY`。遇到类似情况，同时核对阶段退出码、断言产物及清理日志，将产品行为、环境准备和清理分别定位。如何为下一次改动选择足够的证明见[模块边界与回归选择](single-edge.md)。
