# 模块一 · ROS 2 接口

包名 `edge_interfaces`。房间为平坦空场，初始机器人位于世界原点，朝向 +X，里程计与世界轴初始对齐。单位为米、弧度、秒。ID 在一个 Edge 的持久卷内唯一。

| 名称 | 类型与请求 | 响应或反馈 |
|---|---|---|
| `/edge/submit_mission` | `SubmitMission.srv`：`string mission_id`, `geometry_msgs/Point[] waypoints` | `bool accepted`, `string state`, `string detail` |
| `/edge/get_mission` | `GetMission.srv`：`string mission_id` | `bool found`, `string state`, `uint32 verified_count`, `geometry_msgs/Point[] evidence`, `string detail` |
| `/edge/cancel_mission` | `CancelMission.srv`：`string mission_id` | `bool accepted`, `string state`, `string detail` |
| `/edge/navigate_to` | `NavigateTo.action`：`string operation_id`, `float64 x`, `float64 y` | 结果 `bool reached, float64 final_x, float64 final_y, string detail`；反馈 `float64 distance_remaining` |
| `/edge/active_operation` | `std_msgs/msg/String`，仅内核发布当前获准的 operation_id，有活跃动作时定期刷新 | 网关按本机单调时钟检查同一操作的心跳；失效停止输出运动 |
| `/cmd_vel`、`/odom`、`/clock` | ROS→Gazebo 的 Twist、Gazebo→ROS 的 Odometry 和 Clock | 执行网关唯一发布速度；内核与网关独立订阅里程计 |

请求接受 1–8 个有限平面点，每轴范围 [-4,4]，z 必须为零。固定巡检策略以顺序为契约，输出证据按目标顺序排列。任务状态包含 RUNNING、CANCELING、SUCCEEDED、FAILED、CANCELED、RECOVERY_REQUIRED；输入拒绝不创建任务。测试客户端以独立任务 ID 和自己的超时检查，忽略无关任务的回执。

部署就绪检查会等待三个任务服务、导航 Action、两条新鲜的 /odom 和 /ground_truth 样本。真值订阅只在验收和部署检查使用。首版约定一个隔离 ROS 网络一台 Edge，根 topic 名为单机固定名。将来在同一 ROS 域同时挂载多台机器人时，要引入一致的命名空间和动作授权；当前验收不会跨机器人寻址。
