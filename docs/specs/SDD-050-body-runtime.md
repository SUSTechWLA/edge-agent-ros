# SDD-050 Body Runtime 单底盘交接

## 背景与价值

在开发机隔离 Gazebo 世界验证写权限能否交接。原速度看门狗按包到达时间续期，延迟的速度包可能在换版后重新产生输出。

## 设计与契约

Core 持有原任务与恢复账本，Gateway 为获准导航的每个目标生成独立持有者标识。Gazebo 速度看门狗独占本模型的受控速度入口，签发写授权。执行器启动时生成 128 位随机启动标识（boot），每次撤权递增本次启动内的代次（epoch）。所有命令绑定二者。网关进程或执行端重启后，未决任务仍由 Core 按原操作查询。

| 设备状态 | 进入条件 | 能接受的操作 |
|---|---|---|
| `quiet` | 启动、停止、暂停、时钟回退、授权超时或交接证据过期 | 输出零速度，等待本轮轮速里程计 |
| `ready` | 样本采集时钟与接收时钟都连续静止至少 300 ms，样本间隔小于 200 ms | 第一个匹配 boot 和 epoch 的 `open` 可取得授权 |
| `held` | 本轮授权已被领取 | 同一持有者可停止，或发送序号严格递增、线速度绝对值 ≤ 0.32 m/s、角速度绝对值 ≤ 1.1 rad/s 的命令 |

运行时在 Gazebo 内检查授权。有效速度命令间隔达到 300 ms 即撤权并输出零速度。随后以撤权时刻为界重新采样，线速度低于 0.025 m/s 且角速度低于 0.05 rad/s 才累计静止时间。网关报告停车还需收到新一代的 `ready` 和连续静止里程计。验收脚本另从独立的世界位姿 `/ground_truth` 观察车体运动。

两个桥接话题均使用字符串 v1 协议。状态为 `v1 BOOT EPOCH PHASE OWNER`，空持有者用 `-`。命令为 `v1 BOOT EPOCH OWNER open`、`v1 BOOT EPOCH OWNER drive SEQ LINEAR ANGULAR` 和 `v1 BOOT EPOCH OWNER stop`。隔离 Gazebo 域内的参与者相互信任。该实验覆盖意外失联、延迟和重放，现场驱动还需独占物理总线、认证持有者，以及验证制动和最终安全状态。

## 验收与证据

- AC-1 从原导航入口完成领取、运动、停止；既有接口保留。
- AC-2 延迟旧命令、重复命令、错代和跨重启命令不能恢复旧输出。
- AC-3 失联在本地终止授权。停止见证缺失、陈旧或中断期间封锁下一代写权限。

固定提交 `b55da86` 的 Linux/amd64 镜像通过两个 CTest（`body_authority_contract`、`body_runtime_endpoint`）和完整的单底盘交接实验。观测值来自同一次隔离运行。[原始 Gazebo 报告](../../artifacts/verification/body-runtime/gazebo.json)

| 验收点 | 观测结果 |
|---|---|
| AC-1 导航与交接 | 首个任务完成，世界位姿记录位移 0.539 m，随后同一执行端进入下一代可领取状态。 |
| AC-2 旧包和重启 | 原代的开通与速度包重投期间，40 帧世界位姿的最大位置漂移为 0 m。重启执行端后用旧 boot 重投，41 帧位置漂移仍为 0 m。恢复 Core 后，再提交原任务返回 `RECOVERY_REQUIRED`，已验证路点数为 0。 |
| AC-3 撤权与静止 | 第二个任务运动中发出 SIGKILL 请求。探针在 0.574 s 后收到 `quiet`，该时刻后的第一个世界样本仍有 0.317 m/s；在 0.921 s 后收到 `ready`。两值均从探针发起杀进程请求计时。独立端点测试还验证见证掉线后收回可领取授权、跨空窗不累计静止时间。 |

## 迁移与回退

Windows 开发机可在仓根按下列步骤复测。验收脚本使用随机项目名、临时卷和独立的世界位姿观察器。每次选择一个新报告路径。

```powershell
$env:EDGE_IMAGE = 'edge-agent-ros:body-runtime-050'
./scripts/build.ps1
& (uv python find) -B tests/system/body_runtime_acceptance.py --image $env:EDGE_IMAGE --out .ci-results/body-runtime/run-01.json
```

手动试用时以 `docker compose -p body-demo -f compose.yaml -f compose.body-runtime.yaml up -d sim bridge witness gateway core` 启动专用仿真项目，按原入口提交导航。回退时先完成任务或取消并确认停车，关闭该项目，下次仅用 `compose.yaml` 启动。板端部署由原有现场控制链管理。

## 变更记录

2026-11-19，从 `refactor/kernel-boundaries` 的 `3e42ee6` 实现仿真端授权、Gateway 消费者、部署配置和故障验收。系统套件的 `motion-world` 阶段复用同一组测试。上述记录覆盖隔离仿真，实体台架的制动时间、实机驱动独占与现场联锁仍待测量。

同一实现镜像另通过默认配置的 `./scripts/verify.ps1 -SkipBuild` 和 `tests/system/motion_faults.py`。前者覆盖既有任务、取消、Core 重启及速度桥中断，后者覆盖感知故障。此处记录本次实际运行的定向回归。
