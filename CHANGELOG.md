# Edge Agent ROS 变更记录

## SDD-062 · 仓库根目录收敛、Compose 分层与全仓文档同步

- **Compose 拓扑收敛。** 仓库根目录仅保留 `compose.yaml`（仿真默认主栈）与 `compose.hardware.yaml`（板端硬件主栈），其余 43 个特性与实验台站覆盖配置统一归入 [`compose/`](compose/README.md)，根目录受管文件由 52 个精简为 9 个；`scripts/release_robot.py`、`scripts/calibration_station.py`、`tests/test_selection.py` 与 CI 同步更新。[规格与回退契约](docs/specs/SDD-062-repo-layout-and-doc-sync.md)
- **核心文档对齐。** 同步 `README.md`、`CONTRIBUTING.md`、`docs/` 及各模块 `README.md` 至 `SDD-062` 代码状态，更新仓库地址为 `ChaosOrderAI/dsh-edge-agent-ros.git`。

## SDD-061 · ROAM 语义空间代价图与边缘端 Qwen3 推理加速

- **语义空间代价走廊。** `edge_contracts/semantic_costmap.py` 定义带来源、坐标系与 TTL 的 `risk`、`traversable`、`preferred` 栅格契约；`navigation/grid_corridor.py` 与 `exploration/survey_route.py` 在保持物理障碍单调硬否决的前提下引入条件虚拟偏好代价，超时自动降级回纯几何 A\*（`semantic_prior_stale`）；`calibration/view_planner.py` 升级至 `v6`（`board.view-plan` `v1.5.0`）。[设计与实测](docs/specs/SDD-061-semantic-costmap-corridor.md) · [Gazebo 自标定回执](artifacts/verification/sdd061-roam-autocal-20261127/summary.json)
- **边缘端 Ollama / Qwen3 低延迟推理。** `agent/planning.py` 与 `model_provider/` 对 Qwen3 混合模型（`qwen3`、`qwen3:8b`、`qwen3:4b`）默认注入 `"think": false` 并支持 `keep_alive` 常驻、紧凑 `reason` schema 与 Flash Attention / KV Cache `q8_0` 配置，`scripts/calibration_station.py` 增加 `--ollama-model` 与在线模型标签预检。[模型适配规格](docs/specs/SDD-021-model-provider.md)

## SDD-059 & SDD-060 · 传感器闭环自标定工位与语义场景子图

- **引导式与移动换位标定。** 统一棋盘与 FAST-Calib 兼容四孔板的观测、稳定录制、外参/联合内参求解与留出核验工具链；提供 `scripts/calibration_station.py` 覆盖 `sim-stationary`、`sim-mobile` 与 `hardware` 三种工位。[移动自标定验收](docs/testing/fixed-rover-calibration.md) · [四孔板指南](docs/testing/four-hole-calibration.md) · [时间偏移标定](docs/testing/sensor-time-calibration.md)
- **有来源的语义场景子图。** `edge_contracts/semantic_scene.py` 为规划器提供带观测帧、定位代次、拓扑附着（`attached_to` / `guards`）的紧凑场景投影，避免模型直接消费原始像素或硬编码坐标。[规格说明](docs/specs/SDD-060-semantic-scene-queries.md)

## SDD-058 · 校园室内外连续轨迹跟踪与动态让行

- **连续路径与局部安全。** 校园导航站点启用 `continuous_path`、车体扫掠净距与移动目标 TTC 制动，支持穿门、行人横穿让行后自动复查续走及持续堵塞安全停车，配套 10 服务日用校园站（`compose/campus-core.yaml`）。[校园平滑导航报告](docs/testing/campus-smooth-navigation.md) · [规格说明](docs/specs/SDD-058-rover-indoor-mapping-navigation.md)

## SDD-052 – SDD-055 · 自主只读感知规划、有界数值算子与多实现切换

- **只读感知与算子演进。** 支持在只读站点上由模型组合单帧 RGB-D 区域统计、按契约生成并复用纯数值区域算子，以及在同一语义契约下对内置插件、站点工作流与模型组合图进行优先级选择和 DSH 原生 `drain` 故障切换。[RGB-D 复核实验](docs/testing/rgbd-review-comparison.md) · [只读导航与物体测距](docs/testing/navigation-plugins.md)

## SDD-048 – SDD-051 · 探索地图、内核契约分层、机体运行时与统一导航

- **架构边界固化。** `SDD-049` 抽出零依赖 `edge_contracts/` 与 `agent/kernel/`，通过 `tests/test_package_boundaries.py` 静态守护模块导入方向；`SDD-048` 交付分段时间线累计探索地图；`SDD-050` 交付单底盘控制权仲裁与掉线停车的 Body Runtime；`SDD-051` 统一多机体只读空间观测、逆方差融合定位与路线规划契约。[内核边界规格](docs/specs/SDD-049-kernel-plugin-boundaries.md) · [统一导航插件](navigation/README.md)

## SDD-046 · 任务现场、传感器和决策的同步录像

按任务开启录制，归档完成后可选择 `--layout showcase`。观众可在同一时间轴观察现场、地图、实测传感器与任务事件，点击章节回看停车和恢复。仿真通过显式选项读取激光与 IMU；实机接收经批准的设备插件提交的摘要。旧归档可用原先的命令回放。[录制与导出](task_recorder/README.md) · [设计与验收](docs/specs/SDD-046-showcase-recording.md)

## SDD-045 · 插件双轨与板端组合

- **软件插件。** 三类试点覆盖经 HTTP 查询任务证据、提供 Session 服务、在隔离宿主内执行 Python 数列工具。Context 管理注册与清理，Fiber 记录每次装载和卸载。部署管理员可按版本和内容摘要查在途引用；既有计划持有原操作与配置的快照。[运行时接口](docs/plugin-runtime-mapping.md)
- **物理插件。** 相机采集在私有传感域运行。一个持久操作对应一个设备租约、采样产物与独立见证。操作未知时进入待核查。升级先排空，在源端仍然在线时证明停止订阅，再释放租约并核对原容器退出。底盘的 Core/Gateway 继续控制运动。[相机验收与现场契约](docs/testing/physical-camera.md)
- **部署。** 现场 profile 选取插件组合，站点锁固定制品和策略。部署分支由已提交的研发源码生成，`ROBOT_SOURCE` 可追到来源。首次启动用 observe 检查设备与时间域。[板端手册](deploy/robot/README.md)
- **回滚。** 软件先排空旧引用，再恢复通过验收的插件版本。对设备按原操作完成交接后，使用对应部署提交、已批准站点锁及镜像。保留原任务、证据卷和未知结果，具体复测入口见[验收计划](docs/testing/plugin-tracks.md)。
