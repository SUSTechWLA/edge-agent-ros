# 校园场景里的四类机器人

教学楼里的差速车沿用基础世界的 `edge_rover`、底盘和激光雷达。门外的 Crazyflie、Go1、G1 使用各自的原型网格、米制尺寸和单独的 Gazebo 实体。三种机体均发布独立 IMU 和三维里程计。正常启动只需仓库中的 `models.zip`，约 2.4 MB。

| 实体与实际执行部件 | 可验证的动作 | 反馈 |
|---|---|---|
| `crazyflie`，Bitcraze Crazyflie 2 | 发送 `gz.msgs.Twist` 到 `/model/crazyflie/cmd_vel`，Gazebo 的 VelocityControl 控制刚体的竖直速度。模型约 10.6 厘米长，初始中心高度 0.08 米。 | `/model/crazyflie/odometry` 及世界位姿给出高度。这个模型关闭重力，适合检验任务调度、升降反馈和停止行为；旋翼空气动力、推力闭环、电池和姿态飞控尚待仿真。 |
| `go1`，Unitree Go1 | 四个物理髋部俯仰关节 FR、FL、RR、RL 分别响应 `/model/go1/joint/{前缀}_thigh_joint/cmd_pos`。消息为 `gz.msgs.Double`，单位弧度。第二级关节保持上游站姿。 | `/model/go1/joint_state` 报告四个关节角。机身固定在台架上，验证屈伸与四路反馈。自由行走、接触动力学和全身平衡需另行开发。 |
| `g1`，Unitree G1 | `/model/g1/joint/left_shoulder_pitch_joint/cmd_pos` 接受弧度。负角度抬起整条左臂；零位回到上游站姿。 | `/model/g1/joint_state` 报告左肩关节角。米制模型站姿约 1.32 米高，躯干固定，其他部位保持姿势。 |

对每个 `id`，原始传感器位于 `/model/{id}/imu`（`gz.msgs.IMU`）和 `/model/{id}/odometry`（`gz.msgs.Odometry`）；关节台架另有 `gz.msgs.Model` 反馈。比较反馈时要检查机器人 ID、关节名、时间戳和坐标系。一次工具调用可以通过多个采样点确认动作结束。仅收到执行器消息无法证明动作已完成。

## 可复核的网格来源

三个资产目录来自 MuJoCo Menagerie 同一固定提交。Go1 和 G1 采用 BSD-3-Clause，Crazyflie 2 采用 MIT。对应原文随仓库分发于 `licenses/`。转换保留原始模型各部件的相对位置与颜色，在站姿下合并固定部位，将有控制需求的关节分成独立可动物理连杆。接触和惯量使用简化代理；应根据实验范围选择对应的高保真动力学模型。[固定源码](https://github.com/google-deepmind/mujoco_menagerie/tree/370c43965323c0944b433b202aac5058b5f907a9) · [转换清单及 SHA-256](assets.json)

维护者可用以下命令复建离线包。输入目录应包含 `bitcraze_crazyflie_2`、`unitree_go1` 和 `unitree_g1`。对照 `assets.json` 可核查原始 53 个文件及各派生 OBJ、SDF。Gazebo、地图渲染和 Agent 工具消费打包产物，运行阶段不需要 MuJoCo 或 Python 网格处理库。

```sh
uv run --python 3.11 --with numpy==2.4.6 --with scipy==1.17.1 --with trimesh==5.2.1 --with fast-simplification==0.2.0 python scene/campus_large/robots/prepare_models.py /path/to/pinned/mujoco_menagerie
python -m unittest discover -s tests -p test_campus_robot_assets.py
```
