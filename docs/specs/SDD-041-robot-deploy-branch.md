# SDD-041 单独交付可追溯的机器人部署分支

## 背景与价值

工厂和现场需要 Docker 镜像、驱动接入配置和启动入口。研发仓库同时维护教材、Gazebo 场景、测试与实验原件。手选文件上板容易漏依赖，手工在两个分支维护相同代码又会产生版本分歧。

## 设计与契约

维护者从已提交的源码快照生成 `deploy/robot`。分支按原路径保存构建输入、运行源码、硬件及现场标定配置、顶层 `README.md`（源自研发提交的 `deploy/robot/README.md`）及 `ROBOT_SOURCE` 源码指针；离线工具包同样复用该指南生成 `START-HERE.md`。该分支有自己的 Git 历史，由生成器单向更新。每次更新均检查上次发布的文件树及分支占用，使用 Git 引用的比较并更新防止并发覆盖。新增设备插件在研发分支完成 SDD 和测试，再纳入发布。配置与现场观测保存在板端独立目录和 Docker 卷。

## 验收与证据

- AC-1 独立检出的部署分支可以解析硬件与实机标定 Compose、构建镜像、进入只读观测或启动现场标定工位。分支根目录仅保留单份面向边缘部署的 `README.md`；研发规格、模块内部说明、实验产物、Gazebo 场景及测试源码留在 `main` 研发分支。
- AC-2 生成器读取已提交的 Git 对象。重复发布同一来源复用部署提交。显式发布更新的来源时，即使运行文件相同，也要记录所选提交的 `ROBOT_SOURCE`。任何人工改动的部署分支必须显式解决。
- AC-3 运行镜像可沿 OCI 标签和源码指针追溯。可用单分支 Git bundle 或直接单分支克隆在边缘端交接，既有任务数据库留在 Docker 卷中。

## 迁移与回退

研发工作树维持原目录。在设备空闲并确认停稳时切换到先前发布提交和对应镜像。生产数据由现场运维保管，分支切换不自动迁移数据库或撤销已执行的动作。

## 变更记录

2026-11-26，升级部署格式至 `runtime-only-v3`：将 `deploy/robot/README.md` 直接投影为部署分支根目录 `README.md`（继续拦截所有 `docs/`、子目录 `*.md`、`tests/`、`sim/`、`scene/` 与 `artifacts/`），使边缘端直接单分支检出 `deploy/robot` 即可获得纯净运行代码、必要配置与上机说明；同时将硬件传感器自动标定工位（`scripts/calibration_station.py`、`compose.calibration-board.yaml`、`compose.calibration-console.yaml`、`compose.calibration-hardware.yaml`、`compose.calibration-passive-livox.yaml`、`compose.calibration-extra-source.yaml`、`compose.calibration-model.yaml` 及标定容器构建输入）、感知在线服务与灵遨底盘预检脚本纳入白名单，并保持对历史 `v1`/`v2` 部署提交的向后兼容校验。回归入口为 `tests/test_robot_release.py` 与 `tests/system/deploy_projection.py`。

2026-11-19，部署格式 `runtime-only-v2` 将操作指南移至离线套件，发布提交以既有部署版本为父提交。验证器按提交中的格式核对树，历史归档可使用原指南重新打包。回归入口为 `tests/test_robot_release.py`。

2026-11-19，按无人车工厂交付需求建立可重复生成的精简部署分支。

首次发布指向研发快照 `0977a543d2e4`，部署提交 `a8f610c99939` 有独立根提交和 146 个文件。单分支 bundle 在独立目录克隆后，两种架构的镜像均完成构建、运行时导入及板端冒烟；ARM64 在仿真指令集下运行。另从 `deploy/robot` 独立检出执行 `sh scripts/robot.sh observe`，只启动感知和工作台，返回模式为 `observe`。没有驱动时预检分别返回 `odom.missing`、`imu.missing`、`scan.missing`，退出码为 1。校验离线包内全部八项哈希，回读 ARM64 镜像。发布脚本在仅增加测试后再次执行，部署提交保持为同一值。[版本、构建、启动与哈希证据](../evidence/robot-deployment-release.json)
