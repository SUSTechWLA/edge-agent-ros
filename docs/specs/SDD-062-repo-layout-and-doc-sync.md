# SDD-062 仓库根目录收敛、Compose 分层与核心文档同步

## 背景与价值

基线 `3496c941970612bc57b71bd8ec2209e7497d744c`（`main`）在持续演进至 `SDD-061` 后，仓库根目录累积了 **52 个受管文件**，其中除 `.dockerignore`、`.gitattributes`、`.gitignore`、`AGENTS.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`README.md` 与两个主部署入口（`compose.yaml`、`compose.hardware.yaml`）外，其余 **43 个均为 `compose.<feature>.yaml` 覆盖与实验台站配置**，导致根目录浏览拥挤、首次上手难以区分主栈与专用工位 overlay。

同时，多份核心文档与 `SDD-048` 至 `SDD-061` 的真实代码实现产生了偏差：
- `README.md` 与 `CONTRIBUTING.md` 仍保留旧仓库克隆说明，未完整反映 `edge_contracts/` 零依赖契约层、`agent/kernel/` 拆分、四孔板移动闭环自标定（`SDD-059` / `SDD-061`）、Qwen3 混合思考自动关闭与 `keep_alive` 加速（`SDD-021` 扩展）、ROAM 语义空间代价层（`SDD-061`）以及云端确定性 CI 与按需 `full_gazebo` 的分层策略。
- `CHANGELOG.md` 停留在 `SDD-045` / `SDD-046`，缺少 `SDD-048`–`SDD-061` 的关键行为里程碑。
- `docs/plugin-development.md`、`docs/README.md`、`agent/README.md`、`navigation/README.md`、`deploy/robot/README.md` 部分章节仍沿用内核拆分前或纯二值栅格规划时的旧表述。

本次交付解决根目录配置文件杂乱与全仓核心文档失步问题；明确**不改变**任何 ROS 2 消息/Action、HTTP API、插件 `binding` 计算、SQLite 账本结构或 `docs/evidence/*.json` 中的历史固化回执。

## 设计与契约

### 扩展点与所有者

| 项目 | 本次设计 |
|---|---|
| 扩展入口 | 部署配置入口（`compose.yaml`、`compose.hardware.yaml`、`compose/*.yaml`）、车端运行时投影（`scripts/release_robot.py`）、测试阶段选择器（`tests/test_selection.py`）与文档索引体系 |
| Definition | 根目录仅保留 **9 个文件**：3 个 Git/Docker 点文件、4 份顶层治理文档、2 个主 Compose 入口（`compose.yaml` 仿真默认栈、`compose.hardware.yaml` 硬件主机栈）；其余 43 个特性/台站 Compose 文件统一迁入 `compose/<name>.yaml`（去掉 `compose.` 前缀），并新增 `compose/README.md` 分类索引 |
| Provider | 仓库配置与启动脚本（`scripts/deploy.sh`、`scripts/robot-plugins.sh`、`scripts/record-task.sh`、`scripts/calibration_station.py`、`scripts/field-perception-compose.sh`、`scripts/campus-navigation.ps1`、`scripts/campus-core.ps1`、`scene/campus_large/run.py`、`scripts/package_robot.py`、`scripts/release_robot.py`） |
| Consumer | 本地开发者、车端运维人员、GitHub Actions CI（`.github/workflows/ci.yml`）、系统验收套件（`tests/system_suite.py`、`tests/system/*.py`）以及 `deploy/robot` 纯净部署分支 |
| 状态所有者 | Git 源码树持有 Compose 拓扑与文档；运行期任务账本、注册表与证据卷的挂载目标路径、卷名与权限保持零变更 |
| 上下文与证据 | 通过 `git mv` 迁移全部 43 个 Compose overlay 以保留 `git log --follow` 追踪链；`docs/evidence/*.json` 与 `artifacts/verification/` 历史实测原件保持字节不变 |
| 宿主变更 | 无稳定核心代码变更；仅更新启动脚本、发布投影、测试选择规则与文档引用 |

### Compose v2 路径解析契约

1. **以根入口为首文件的叠加栈**（如 `docker compose -f compose.yaml -f compose/camera.yaml` 或 `docker compose -f compose.hardware.yaml -f compose/rgbd.yaml -f compose/rgbd-hardware.yaml`）：Docker Compose v2 默认将首个 `-f` 文件所在目录（仓库根目录 `.`）作为 `--project-directory`，因此 `compose/*.yaml` 内的 `./perception/...`、`./model_provider/...` 等相对挂载继续解析到仓库根目录，无需修改卷源路径。
2. **不带根入口的独立台站栈**（如 `compose/calibration-board.yaml`、`compose/calibration-gazebo.yaml`、`compose/navigation-readonly.yaml`）：首个 `-f` 位于 `compose/` 子目录，启动器（`scripts/calibration_station.py`、`scripts/field-perception-compose.sh`、`.github/workflows/ci.yml`）与操作文档统一显式传入 `--project-directory .`（或仓库根路径 `"$root"`），确保相对挂载依然解析到仓库根目录；独立复核站点模板 `compose/review-comparison.yaml` 继续配合 `--project-directory <站点目录>` 使用。
3. **`extends.file` 相对 `--project-directory`（仓库根目录）解析**：
   - `compose.yaml` 与 `compose.hardware.yaml`：`extends.file` 更新为 `compose/sensor-health.yaml`。
   - `compose/body-runtime.yaml`：保持 `compose.yaml`（配合根入口叠加时相对项目根目录解析）。
   - `compose/calibration-extra-source.yaml` 与 `compose/campus-core.yaml`：更新为 `compose/calibration-passive-livox.yaml` 与 `compose/campus-dynamic.yaml`。

### 生命周期与旧行为

| 场景 | 新请求 | 已受理/在途工作 | 证据、资源与责任方 |
|---|---|---|---|
| 安装 / 首次发现 | 默认 `docker compose up`、`sh scripts/deploy.sh`、`python scripts/calibration_station.py` 直接读取新路径启动 | 不受影响（容器内 `/opt/edge` 与命名卷路径未变） | `compose/README.md` 与 `deploy/robot/README.md` 记录全部入口 |
| 依赖缺失 / 冲突 / 断连 | 若旧外部脚本仍传 `-f compose.<name>.yaml`，Docker Compose 在启动前即报文件不存在并退出码非 0，不会半启动容器 | 已运行容器继续服务直至显式交接 | `scripts/calibration_station.py preflight` 在启动前校验 `compose/*.yaml` 存在性 |
| draining / 卸载 | `scripts/robot-plugins.sh`、`scripts/record-task.sh`、`scene/campus_large/run.py` 使用同一项目名与新 overlay 路径执行排空与停止 | 保持原有 DSH v2 drain 与运动静止确认协议 | 项目名（`COMPOSE_PROJECT_NAME` / `-p`）与卷名完全不变 |
| 换版 / 旧 binding | `scripts/release_robot.py` 生成的 `deploy/robot` 延续 `Robot-Format: runtime-only-v3`，仅将投影集合中的根目录 overlay 映射为 `compose/*.yaml` | 插件 `content_digest`、`binding` 与镜像摘要零变化 | `tests/test_robot_release.py` 校验 `deploy/robot` 投影边界与历史祖先关系 |
| 超时 / 崩溃 / 重启 | 容器 `restart` 策略、健康探针与账本恢复路径与 `SDD-061` 一致 | 原 `operation_id` 对账不变 | 由 Agent 内核与 Core 节点继续持有 |

## 验收与证据

| AC | 输入 / 故障 | 可独立观察的预期 | 测试入口 | 实测结果 / 证据 |
|---|---|---|---|---|
| AC-1 | 检查仓库根目录受管文件与全部 Compose 组合配置解析 | 根目录受管文件由 52 个收敛为 9 个；43 个 overlay 位于 `compose/*.yaml` 且含 `compose/README.md`；仿真、硬件、相机/RGB-D/模型/录制、标定三模式（`sim-stationary`、`sim-mobile`、`hardware` + `extra-source`）及校园栈（含 `campus-core`、`body-runtime`）的 `docker compose ... config --quiet` 全部退出码 0 | `tests/system_suite.py --stages repository planner`、`tests/test_campus_deployment.py`、`scripts/calibration_station.py preflight` | 通过（根目录 9 文件，`compose/` 下 43 个 `.yaml` + `README.md`；系统套件运行号 `.ci-results/system/runs/20261127T025629Z-13224fa8/summary.json` `repository` + `planner` 全绿，`test_campus_deployment.py` 17/17 通过） |
| AC-2 | 生成并校验 `deploy/robot` 纯净车端分支投影 | `release_robot.projection("HEAD")` 包含车端所需的 `compose/*.yaml` 运行时文件，排除 `compose.yaml`、`compose/calibration-gazebo.yaml`、`compose/calibration-fixed-rover.yaml`、`compose/physical-camera-test.yaml` 等仿真夹具，且分支内唯一 `.md` 文件仍为根 `README.md` | `tests/test_robot_release.py`、`tests/test_calibration_station_cli.py`、CI 投影与 `docker compose` 校验脚本 | 通过（`test_robot_release.py` 4/4、`test_calibration_station_cli.py` 4/4 通过；投影树 `compose.hardware.yaml` 与 `compose/calibration-*.yaml` 解析全部退出码 0） |
| AC-3 | 修改 `compose/*.yaml` 或顶层 `CHANGELOG.md` 时运行消费者测试选择器与包边界检查 | `tests/test_selection.py` 精确命中对应消费者阶段（`CHANGELOG.md` 与 `compose/README.md` 仅触发 `repository`），不因路径迁移误退回全量未知路径；Python 包导入边界无违规 | `tests/test_selection.py`、`tests/test_package_boundaries.py`、`tests/planning_contract.py` | 通过（`test_selection.py` 21/21、`test_package_boundaries.py` 2/2、`planning_contract.py` 12/12、`node --test` 地图单测 8/8 全部通过） |
| AC-4 | 审计全仓 Markdown 相对链接、SDD 规范完整性与核心文档代码一致性 | `README.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`docs/README.md`、`docs/plugin-development.md`、`agent/README.md`、`navigation/README.md`、`model_provider/README.md`、`deploy/robot/README.md` 与当前代码及 `ChaosOrderAI/dsh-edge-agent-ros.git` 一致；全部本地相对链接存在且 `git diff --check` 无空白错误 | `tests/document_contract.py`、`tests/repository_contract.py`、`scripts/check_sdd.py`、`git diff --check` | 通过（`document_contract.py` 3/3、`repository_contract.py` 5/5、`check_sdd.py --base main` 通过，`git diff --check` 零告警） |

## 迁移与回退

1. **命令迁移对照**：
   - 主栈叠加：将 `-f compose.<name>.yaml` 改为 `-f compose/<name>.yaml`（例如 `docker compose -f compose.yaml -f compose/camera.yaml up -d`）。
   - 独立标定与只读导航台站：优先通过统一入口 `python scripts/calibration_station.py <plan|preflight|up|down|status> --mode ...` 与 `sh scripts/field-perception-compose.sh ...` 执行；手工直接调用 `docker compose` 且首个 `-f` 位于 `compose/` 时，加上 `--project-directory .`。
2. **车端部署分支更新**：合并入 `main` 后在仓库根目录执行 `python scripts/release_robot.py HEAD`，生成新的 `deploy/robot` 提交并推送远端。已在车端运行的容器与命名卷无需重建，下次拉取 `deploy/robot` 后直接使用 `sh scripts/robot.sh` 或 `python3 scripts/calibration_station.py`。
3. **回退方式**：若外部自动化脚本尚未适配 `compose/` 目录，可在主分支对本次合并提交执行 `git revert <merge-sha>`，随后重新运行 `python scripts/release_robot.py HEAD` 恢复旧根目录平铺布局；由于未修改任何运行期状态格式或卷名，回退无需迁移历史数据库。

## 变更记录

- **2026-11-27（设计立项）**：基于 `main@3496c941970612bc57b71bd8ec2209e7497d744c` 建立 `SDD-062`，规划将根目录 43 个 `compose.<name>.yaml` 覆盖配置收敛至 `compose/<name>.yaml`（保留 `compose.yaml` 与 `compose.hardware.yaml` 两个根入口），并全面同步 `README.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`docs/` 及各核心模块 `README.md` 至 `SDD-061` 最新代码状态。
- **2026-11-27（实现与全量契约验证）**：
  - **原因**：清理根目录 43 个平铺 Compose overlay 造成的浏览与维护噪声，消除顶层与模块核心文档相对 `SDD-048`–`SDD-061` 代码实现的滞后。
  - **改动路径**：`compose/*.yaml`（`git mv` 迁移 43 个配置并新增 `compose/README.md`）、`compose.yaml`、`compose.hardware.yaml`、`.github/workflows/ci.yml`、`scripts/release_robot.py`、`scripts/calibration_station.py`、`scripts/field-perception-compose.sh`、`scripts/deploy.sh`、`scripts/robot-plugins.sh`、`scripts/record-task.sh`、`scripts/package_robot.py`、`scripts/campus-navigation.ps1`、`scripts/campus-core.ps1`、`scene/campus_large/run.py`、`tests/system_suite.py`、`tests/test_selection.py`、`tests/test_robot_release.py`、`tests/test_calibration_station_cli.py`、`tests/test_campus_deployment.py`、`tests/system/*.py`（15 项验收夹具），以及 `README.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`docs/`、`deploy/robot/README.md`、`agent/README.md`、`navigation/README.md`、`model_provider/README.md`、`perception/*/README.md`、`scene/campus_large/README.md`、`task_recorder/README.md`。
  - **复测命令与结果**：运行 `tests/repository_contract.py`、`tests/document_contract.py`、`tests/test_package_boundaries.py`、`tests/test_selection.py`、`tests/test_robot_release.py`、`tests/test_calibration_station_cli.py`、`tests/test_campus_deployment.py`、`tests/planning_contract.py`、`node --test tests/map_planar.test.mjs tests/map_spatial.test.mjs`、`tests/system_suite.py --stages repository planner`（`.ci-results/system/runs/20261127T025629Z-13224fa8/summary.json`）、全套 `docker compose ... config --quiet`、`scripts/check_sdd.py --base main` 及 `git diff --check`，全部通过。
  - **待验范围**：本次未改动任何容器运行期业务代码或控制协议，未重跑 21 阶段长耗时 3D Gazebo 物理运动套件及 Jetson 实车上电试验；实车拉取新版 `deploy/robot` 后按 `deploy/robot/README.md` 执行 `preflight` 核验。
- **2026-11-27（补充 README 核心架构全景图与分层拓扑）**：在 `docs/architecture/core-architecture.svg` 纳入按最新源码绘制的六分区矢量核心架构蓝图（同步 `SDD-062` Compose 分层、`feedback_plan`、Qwen3 `think=false` 与 `SDD-061` 语义代价层状态），并在 `README.md` 新增「系统核心架构」章节，同时提供矢量全景图与五层控制/事实流 Mermaid 拓扑图，运行 `tests/document_contract.py`、`tests/repository_contract.py`、`scripts/check_sdd.py --base main` 与 `git diff --check` 全部通过。
