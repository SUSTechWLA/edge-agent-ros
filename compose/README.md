# Docker Compose 分层配置目录

仓库根目录仅保留两个主入口：
- [`../compose.yaml`](../compose.yaml)：工作站与 Gazebo 仿真默认主栈（`docker compose up -d` 或 `sh scripts/deploy.sh sim up`）。
- [`../compose.hardware.yaml`](../compose.hardware.yaml)：Linux / ARM64 边缘板端硬件主栈（`sh scripts/deploy.sh hardware ...` 或 `sh scripts/robot.sh ...`）。

其余 **43 个特性叠加层（overlay）与独立实验台站配置**集中在本目录，按职责分为七组：

## 1. 板端观察与传感器健康

| 文件 | 用途与组合方式 |
|---|---|
| `hardware-observe.yaml` | 板端只读观察模式（启动 `perception`、`task-facts`、`console`，拒绝任务写入）；配合 `../compose.hardware.yaml` 由 `sh scripts/deploy.sh hardware observe` 调用 |
| `sensor-health.yaml` | 传感器质量与退化诊断服务基础定义，由 `../compose.yaml` 与 `../compose.hardware.yaml` 通过 `extends` 复用 |

## 2. 相机与 RGB-D 感知插件

| 文件 | 用途与组合方式 |
|---|---|
| `camera.yaml` | 多路 ROS 2 `Image` / `CompressedImage` 只读预览插件（端口 `8771`）及仿真相机桥 |
| `camera-hardware.yaml` | 原生 Linux 板端相机 host 网络与系统时钟覆盖层（追加在 `../compose.hardware.yaml -f compose/camera.yaml` 之后） |
| `rgbd.yaml` | 对齐深度与彩色像素测距插件（端口 `8772`） |
| `rgbd-sensor.yaml` | Gazebo 固定 RGB-D 台站传感器桥 |
| `rgbd-hardware.yaml` | RealSense D435i / D455 硬件 host 网络与系统时钟覆盖层 |
| `physical-camera.yaml` | DSH v2 物理相机控制器、采集器与独立停止核验器 |
| `physical-camera-test.yaml` | 物理相机隔离验收仿真源覆盖层（仅测试，不进入 `deploy/robot`） |

## 3. 本地与云端模型路由

| 文件 | 用途与组合方式 |
|---|---|
| `model-provider.yaml` | 兼容 Ollama `/api/chat` 的本地 Qwen / 云端 DeepSeek 模型路由适配器（端口 `8134`） |
| `model-provider-hardware.yaml` | 板端 host 网络模型适配器覆盖层 |
| `model-provider-cloud.yaml` | DeepSeek 官方 HTTPS API 只读密钥挂载覆盖层（读取宿主 `EDGE_DEEPSEEK_KEY_FILE`） |

## 4. 传感器标定工作站

推荐通过统一启动与预检入口 [`../scripts/calibration_station.py`](../scripts/calibration_station.py)（支持 `--mode sim-mobile | sim-stationary | hardware`）调用：

| 文件 | 用途与组合方式 |
|---|---|
| `calibration-board.yaml` | 标定工位核心计算、Agent 闭环规划与会话插件栈 |
| `calibration-console.yaml` | 标定工作台前端与状态聚合服务 |
| `calibration-model.yaml` | 标定专用本地 Ollama GPU 服务（启用 `OLLAMA_FLASH_ATTENTION=1`、`OLLAMA_KV_CACHE_TYPE=q8_0`） |
| `calibration-gazebo.yaml` | 静止工位棋盘/四孔板 Gazebo 仿真源（仅仿真） |
| `calibration-fixed-rover.yaml` | 固定地面四孔板 + 灵遨差速车 Gazebo 移动自标定世界（仅仿真） |
| `calibration-view-motion.yaml` | 自主取景走廊规划（`plan_capture_views` v6）、RGB 目标跟踪与受限底盘换位服务 |
| `calibration-hardware.yaml` | Jetson / Linux 实车只读标定观察器与时钟偏移校准（`--profile time-calibration`） |
| `calibration-passive-livox.yaml` | 实车无驱动环境下的 Livox MID-360 被动 UDP 点云接收器 |
| `calibration-extra-source.yaml` | 多路被动雷达扩展模板（继承 `calibration-passive-livox.yaml`） |
| `calibration-clock-fixture.yaml` | 软件时间偏移标定运动/静止对照测试工装（仅测试） |

## 5. 大型校园、动态避障与机体运行时

| 文件 | 用途与组合方式 |
|---|---|
| `campus.yaml` | 大型校园多机体 Gazebo 世界（`edge-agent-campus`，由 `python scene/campus_large/run.py` 管理） |
| `campus-dynamic.yaml` | 教学楼室内外连续轨迹跟踪、行人动态让行与语义导航台站（由 `scripts/campus-navigation.ps1` 管理） |
| `campus-core.yaml` | 日用十服务精简校园站（由 `scripts/campus-core.ps1` 管理，继承 `campus-dynamic.yaml`） |
| `body-runtime.yaml` | 多机体物理控制桥隔离覆盖层（继承 `../compose.yaml` 的 `bridge` 服务） |

## 6. 只读空间事实、软件插件宿主与算子演进

| 文件 | 用途与组合方式 |
|---|---|
| `soft-runtime.yaml` | DSH v2 `soft-host`（Context / Fiber）、`soft-gateway` 与 SQLite 受管注册表 |
| `soft-runtime-hardware.yaml` | 板端软件插件宿主覆盖层（由 `sh scripts/robot.sh plugins` 调用） |
| `navigation-readonly.yaml` | 只读空间观测、逆方差加权定位与路线试算站点栈 |
| `navigation-camera-info.yaml` | 只读 `CameraInfo` 内参摘要订阅器 |
| `navigation-camera-image.yaml` | 只读彩色图像采样与摘要归档订阅器 |
| `navigation-camera-depth.yaml` | 只读 `16UC1` 对齐深度采样订阅器 |
| `navigation-livox.yaml` | 只读 Livox 点云摘要采集器 |
| `stationary-perception.yaml` | 静止多雷达/RGB-D 融合与目标标注计算服务 |
| `object-perception.yaml` | 室内物体检测与可见表面三维测距服务 |
| `frame-query.yaml` | 单帧 RGB-D 有界区域统计查询服务 |
| `autonomous-read.yaml` | 自主只读感知规划器配置挂载覆盖层 |
| `perception-operators.yaml` | 纯数值区域算子求值服务 |
| `perception-autostart.yaml` | 现场空间事实网关与里程计采集自启动覆盖层 |
| `field-plugin-author.yaml` | 现场受限只读插件作者与复核订阅覆盖层 |
| `review-comparison.yaml` | 封存 RGB-D 帧多实现对比与 DSH drain 故障切换实验栈（配合 `--project-directory <站点目录>` 使用） |

## 7. 按任务录制与复盘

| 文件 | 用途与组合方式 |
|---|---|
| `task-recorder.yaml` | 按任务按需启停的录制服务与采集器（`recording` profile，由 `scripts/record-task.sh` / `record-task.ps1` 调用） |
| `task-recorder-campus.yaml` | 校园工程专用录制端口与插件卷绑定覆盖层 |

## 路径解析约定

1. 当命令以根入口开头（如 `docker compose -f compose.yaml -f compose/camera.yaml ...`）时，Docker Compose v2 自动以仓库根目录作为 `--project-directory`，无需额外参数。
2. 当命令直接以 `compose/*.yaml` 作为第一个 `-f` 文件（未包含 `compose.yaml` 或 `compose.hardware.yaml`）时，请显式传入 `--project-directory .`（例如 `docker compose --project-directory . -f compose/navigation-readonly.yaml ...`），或直接使用仓库提供的封装脚本（`scripts/calibration_station.py`、`scripts/field-perception-compose.sh`）。
