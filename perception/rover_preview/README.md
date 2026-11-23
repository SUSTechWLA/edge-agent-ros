# 轮式车局部路径预览

同一常驻软件进程向 Agent 提供只读工具 `preview_rover_obstacle_path`，向内部模块提供 `planning.local_path`。两者使用站点的车身轮廓、净距和同一规划算法。机型、策略及观测适配器由部署配置决定。ROS Gateway 的 `LocalPlannerPort` 独立负责授权运动期间的逐拍决策。

## 任务与快照入口

| 入口 | 输入及所有者 | 结果 |
| --- | --- | --- |
| `POST /invoke` | Agent 的标准 DSH Tool 请求，空参数和稳定的 `operation_id`。源由站点 `--facts` 固定 | 保存开始快照的规划结果和随后采样窗的首尾实测位姿。重复调用返回原始收据。位姿采样窗至少 650 ms |
| `POST /evaluate` | 内部感知或导航模块直接推送同一时刻的车体快照、静态局部目标、可选偏侧。内部 DSH v1 服务目录的 `planning.local_path` 解析出端点和版本 | 同步返回该快照的曲线、净距和覆盖，读操作只在内存计算。没有任务采样等待或账本开销 |

机器客户端先 pin 目录绑定，再将 `provider_version` 设为绑定中的 `plugin_version`。请求为 JSON，传输上限 1 MiB。下例中的 `snapshot` 使用变量表示上游刚采集的完整观测。

```python
request = {
    "schema_version": 1,
    "provider_version": binding["plugin_version"],
    "profile_id": "lingao.archive.planar.v1",
    "clock_id": "unix",
    "objective": {
        "kind": "static_local_path",
        "frame_id": snapshot["base_frame"],
        "goal_body_m": [2.4, 0.15],
    },
    "snapshot": snapshot,
    "budget_ms": 50,
}
```

- 快照需要 `source_id`、`base_frame`、`calibration_ref`、`scan_stamp_ns` 和车体坐标射线 `rays`，可带 `hits_body_m`。每次最多 2048 条射线、4096 个离散回波。快口需要机型配置明确声明 `supported_body_frames`。调用方省略 `profile_id` 时使用端点所绑定站点的机型。
- 局部目标由调用方转换到同一个车体坐标系，轮式策略消费向前 0.6 m 至该机型规划距离、横向不超过 1.5 m 的目标。省略目标时使用站点前向距离。可传 `preferred_side` 为 `left` 或 `right`，调用方按任务记录自己的偏侧。感知适配器适配坐标和来源，探索插件选择前沿目标，预测插件需要能够处理时空轨迹的新服务契约。
- 输入与完成时均检查 Unix 采样时间，新鲜度为 250 ms，允许 50 ms 向前时钟偏差。同一来源、车体帧和标定身份拒绝较老的源时刻；最多保留 32 个来源水位。消费者在使用前检查返回的 `observation_valid_until_ns`、帧、源时间和状态；`partial` 是沿途观测不足的几何候选。
- 单个进程有一个计算槽。忙时立即返回 429。计算预算为 1–250 ms，默认 50 ms，从进入计算接口到算法完成后检查。逾期结果被丢弃，已开始的计算占用槽直至返回。HTTP、序列化和消费排队另计。该过程为软截止，物理控制的 50 ms 循环留在本地 Gateway。源陈旧、乱序、机型不匹配、超预算、不支持的跟踪数据或目标类型都返回错误、空路径，超时后不复用旧路径。
- `/invoke` 在程序更新后可读旧收据，重试带原版本并复用原操作号；在新版本执行新的旧绑定操作会被拒绝。新发服务调用先按 DSH 目录发现版本，既有版本交接遵循站点 draining、等待在途操作结束、替换进程和更新目录的顺序。服务运行时可以通过部署受信的同契约提供者替换，单次请求不能载入代码。现有 v2 安装准备脚本输出的是 Agent 工具描述符；直接快照服务通过同镜像内的 `runtime/planning.rover_preview.json` 发布。
