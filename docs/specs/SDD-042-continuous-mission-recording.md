# SDD-042 用一条连续录像验收巡检任务的失败与恢复

## 背景与价值

单看最后的成功状态，现场人员无法判断车辆是否实际走过整条路线、短时故障时谁重新接手、重试是否重复驱动底盘。新人需要从同一句自然语言一直跟到最终的设备证据。

## 设计与契约

仅在隔离的 Gazebo、私有 Compose 项目、独立 ROS 域中运行，使用已发布的 Agent、Core、Gateway、感知与工作台。输入一次中文巡检和回报请求。外部测试程序制造服务短时中断与运动中 Agent 退出，记录注入时间、恢复所有者、同任务的原始操作号。录像见证进程连续采集世界相机、占据栅格、真值轨迹和事件。相机属于观察方，规划器读取自己的传感器。播放时间与仿真时间可互查。

## 验收与证据

- AC-1 一次提交，一个任务及 trace，巡检出门到庭院再返回，在同一运行中读取最终状态。记录七个有序目标和七个真实测量点。
- AC-2 短时无回执引起真实的 Agent 错误和退避重试；解除注入后由系统自动续办。运动中故障后 Docker 自动重启 Agent，记录容器启动次数及同一任务在恢复前后的状态。
- AC-3 网关每个路点只启动一次。以独立世界真值验证移动、过门、返回、终点误差和停稳。保留自然语言、任务回执、错误、原始画面、地图、时间线与可播放的 MP4，注明播放速度和观测来源。
- AC-4 修改录像脚本，常规回归在主机临时目录核对录像配置和证据契约。完整录像是开发者显式启动的独立 Gazebo 实验，只接受本次生成的任务与输出目录。板端部署和板端测试无需安装录像软件。

## 复跑

在研发仓根目录、Docker Engine 与 Compose 可用的主机，准备包含本次运行时代码的本地镜像。以下 PowerShell 命令发起一次完整的采集与故障恢复验收，自动生成新目录。可将镜像名改为要验收的候选标签。

```powershell
python tests/system/record_long_mission.py --image edge-agent-ros:commission-exploration --out ($run = "artifacts/verification/rover-long-mission/" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"))
```

`$run/acceptance.json` 给出本次 `task_id` 与验收结果，`$run/media/mission/` 保存原始时间线和画面。核对每个路点回执与独立真值的时间对齐，并用本次真实任务号渲染视频，可在相同终端接着执行下列命令。渲染主机需要 Pillow、中文字体、可执行的 FFmpeg 与 FFprobe；两者若未加入 PATH，可分别传 `--ffmpeg` 和 `--ffprobe` 的完整路径。

```powershell
python tests/system/long_mission_world.py --audit $run
python tests/system/render_rover_video.py --mode mission --run $run --out "$run/video" --task-id (Get-Content -Raw "$run/acceptance.json" | ConvertFrom-Json).task_id --font "$env:WINDIR/Fonts/msyh.ttc"
python tests/system/mission_watch_page.py $run
```

仅改录像辅助程序时，`python tests/test_mission_recording.py -v` 即可复核选测所运行的契约。该检查覆盖相机视图与碰撞模型的隔离、Compose 作用域、截断时间线和 RGB 完整性。完整任务是否实际恢复由上述独立验收判定。

## 迁移与回退

观测相机与注入脚本只用于实验室专用任务，退出后清理本次隔离栈。录像、原始证据和说明留在研发侧；板端部署提交与正在运行的其他机器人独立。

## 变更记录

2026-11-19，为完整长程任务的演示补充连续采集、独立故障注入和证据核对。人体现场、实车制动、长时间耐久性另按现场工况验收。

常规 `repository` 阶段新增录像配置与证据契约两项，主机 Python 通过；消费者选测五组通过。修改录像脚本仅选中该轻量阶段，混合修改仍会叠加实际运行时消费者。对当前开发树逐项核对 145 个板端投影条目，内容和模式均与 `deploy/robot` 相同。录像、测试脚本与开发资料位于研发侧。
