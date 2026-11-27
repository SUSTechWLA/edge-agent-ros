# 固定地面靶标的自主换位标定

本地工作台向 Edge Agent 提交一句任务。启动时板在共同视野外，模拟操作员按提示把板放到车头正前方，随后板保持固定。小车通过轮胎与地面接触运动，相机和前雷达在各次停车后观测同一块板。

## 工作台验收结果

自主换位联合标定任务 `c1895118-27ee-49e5-a080-bf750895610c` 由本地 Qwen3-4B 从工作台提交原文后执行。Gazebo 操作员首次摆板后始终保留原地平面坐标。底盘自主完成 8 次换位，最大到点误差 2.84 cm。机器人独立补齐 RGB 九宫格后采齐配对及留出组。取景服务确认当前可达视点无法补齐指定角度时，Agent 才请求操作员在原板位补拍俯仰和滚转。实际训练俯仰范围 −22.2° 至 21.8°，滚转范围 −13.6° 至 13.5°。13 份原始观测的摘要均与发布记录一致，10 组参与拟合，3 组只用于独立验证。联合优化得到留出重投影 RMS 0.805 px，最大孔心残差 10.30 mm。安装真值误差为平移 12.70 mm、旋转 0.062°。[前端录屏](../../artifacts/verification/fixed-rover-gazebo-20261125/mobile-joint-nov26-r9/workbench-mobile-joint-calibration.mp4) · [独立验收](../../artifacts/verification/fixed-rover-gazebo-20261125/mobile-joint-nov26-r9/summary.json) · [样本划分与联合结果](../../artifacts/verification/fixed-rover-gazebo-20261125/mobile-joint-nov26-r9/publication.json)

联合标定任务 `a76f63af-ec45-48cb-aa12-094244760887` 从工作台提交「请帮我标定当前D435的内参和前MID360的外参」。本地 Qwen3-4B 逐步选择采集、检查、拟合和验证工具，共 22 步。第一组角点覆盖 2/9 格，底部三格均缺。操作员仿真按工作台提示移动并倾斜标定板，最终覆盖 9/9 格。训练组俯仰角覆盖 −17.5° 至 30.2°，滚转角覆盖 −11.1° 至 10.6°。底盘的初末世界坐标相同。

相机用 7 组训练姿态，前雷达外参用其中 6 组同步观测。独立验收用另外 3 组姿态，轮换留出一块方码后预测了 48 个角点，重投影 RMS 为 0.981 px，最大 2.455 px。外参留出最大孔心残差为 15.14 mm，低于 25 mm 阈值。相对 Gazebo 安装真值误差为平移 34.51 mm、旋转 0.254°，平移接近 35 mm 的验收上限。本轮求解的是仿真零畸变针孔内参和前 MID360 到 D435 彩色光学帧的外参。[录像](../../artifacts/verification/fixed-rover-gazebo-20261125/joint-intrinsics-nov26/workbench-joint-calibration.mp4) · [独立验收](../../artifacts/verification/fixed-rover-gazebo-20261125/joint-intrinsics-nov26/summary.json) · [求解与数据划分](../../artifacts/verification/fixed-rover-gazebo-20261125/joint-intrinsics-nov26/publication.json)

多方向取景版本通过工作台任务 `731213ce-b70c-48ab-a6dd-678fdb8f49a9`。Gazebo 独立真值显示，收录的五组姿态中底盘到板中心线的左右端点分别为 29.4 cm 和 15.5 cm，相机光心左右端点分别为 31.0 cm 和 19.5 cm。六次换位累计 2.807 m，所有 1447 条非零速度采样均与新鲜有效的板框匹配。与前一版相比，光心跨度从 10.4 cm 增到 50.5 cm，RGB 九宫格两次均覆盖上排及中排六格。留出孔位最大误差从 6.31 mm 到 9.41 mm，均在 25 mm 门槛内。增加侧向基线的收益由轨迹直接验证，单次结果不足以证明绝对标定误差随之下降。[新版覆盖](../../artifacts/verification/fixed-rover-gazebo-20261125/lateral-margin/coverage.json) · [前一版覆盖](../../artifacts/verification/fixed-rover-gazebo-20261125/lateral-margin/baseline-coverage.json) · [外参验证](../../artifacts/verification/fixed-rover-gazebo-20261125/lateral-margin/summary.json)

启用 RGB 跟踪后的任务 `59341d6f-0421-46a0-87c6-1d8286c6e1e6` 在 21 步完成四次换位、五姿态取样以及独立留出验证。运动档案的 1,035 条非零速度采样均有新鲜完整 RGB 目标框，其中 223 条为倒行。最紧时板外沿距画边仍有对应图像尺寸的 6.74%。实际行程 0.765 m，四次均经独立验收到点停稳。留出最大孔位误差 6.31 mm，对照 Gazebo 安装真值误差为平移 6.79 mm、旋转 0.061°。工作台预览有短时帧龄超限，运动见证使用相机端实时检测流。[倒行画面](../../artifacts/verification/fixed-rover-gazebo-20261125/tracking-nov26/workbench-reverse.jpg) · [跟踪与运动](../../artifacts/verification/fixed-rover-gazebo-20261125/tracking-nov26/motion/summary.json) · [外参复核](../../artifacts/verification/fixed-rover-gazebo-20261125/tracking-nov26/summary.json)

任务 `0888b7c3-573c-4f53-8e9e-239ab61836b2` 由浏览器表单提交一次。工位在两帧都识别到四码四孔后发出摆板回执，Qwen3-4B 在工作站本地逐步选择工具，23 步完成。每个姿态保存 4 帧配对数据，共 20 帧。采集使用仿真相机已有内参，发布的外参将前 MID360 的点变换到 D435 彩色光学坐标系。[任务与原始操作](../../artifacts/verification/fixed-rover-gazebo-20261125/fifth-run/task.json) · [板放置记录](../../artifacts/verification/fixed-rover-gazebo-20261125/fifth-run/operator-placement.json)

| 验收项 | 观测结果 |
| --- | --- |
| 换位与停车 | 4 次换位，均经独立位姿见证确认停稳。Gazebo 真值累计行程 1.574 m。起点约为 (0, 0, 0°)，终点为 (−1.200 m, 0.651 m, −8.02°)。 |
| 速度 | 命令、仲裁输出及安全输出的线速度峰值均为 0.10 m/s，角速度峰值均为 0.12 rad/s。 |
| 样本验证 | 3 姿态拟合，2 个独立姿态留出。留出孔位 RMS 5.91 mm，最大 9.69 mm，门槛为 25 mm。 |
| 安装真值 | 平移误差 20.85 mm，旋转误差 0.292°，独立验收上限分别为 35 mm、1.5°。 |

任务、采集片段和已发布版本可以按同一个任务号回查。工作台录像以实际页面记录用户输入、摆板提示、换位、采样和验证。当前实机装配须使用实采数据重新求解并验收。[工作台录像](../../artifacts/verification/fixed-rover-gazebo-20261125/fifth-run/workbench-timelapse.mp4) · [独立验收](../../artifacts/verification/fixed-rover-gazebo-20261125/fifth-run/summary.json) · [外参与留出结果](../../artifacts/verification/fixed-rover-gazebo-20261125/fifth-run/spatial-publication.json) · [底盘轨迹](../../artifacts/verification/fixed-rover-gazebo-20261125/fifth-run-motion/summary.json)

## 复现

联合内外参验收使用 `compose/calibration-board.yaml`、`compose/calibration-console.yaml`、`compose/calibration-model.yaml` 和 `compose/calibration-fixed-rover.yaml` 启动空取景的 `edge-fixed-cal`。将 `tests/system/intrinsic_board_operator.py` 复制到 `edge-fixed-cal-fixed-sim-1:/tmp/intrinsic_board_operator.py`。先在本机启动只读任务见证与标定板操作员仿真，再用浏览器脚本打开表单和提交原文。仿真操作员只有在同一任务的工位发出取样提示时才调整 Gazebo 里的板。

```powershell
$out = 'artifacts/verification/fixed-rover-gazebo-20261125/joint-replay'
$goal = '请帮我标定当前D435的内参和前MID360的外参'
pwsh -NoProfile -File scripts/project-python.ps1 tests/system/gazebo_joint_calibration.py --output $out --task $goal
# 在另一个终端，用相同的 $out 和 $goal
node tests/system/fixed_rover_browser.cjs --submit "$out/frames" http://127.0.0.1:8916 $out $goal
```

自主换位与按需补角的联合验收还加载 `compose/calibration-view-motion.yaml`。工位从板在视野外、车体静止的 Gazebo 实例启动。沿用上述见证与工作台命令，把任务原文改为「请帮我自主标定当前D435的内参和前MID360的外参」，在 Python 命令增加 `--mobile`。见证只响应首次摆板及有可达性证据的补角请求，所有底盘目标由 Agent 从取景插件输出选择。用 `node tests/system/fixed_rover_timelapse.cjs "$out/frames" "$out/workbench-joint-calibration.mp4" joint` 生成录像。

在具备本项目 Gazebo、标定计算镜像及 `qwen3:4b-instruct` 模型缓存的工作站，从仓库根目录运行以下命令。模型卷由 `EDGE_CAL_MODEL_VOLUME` 指定，默认值见 `compose/calibration-model.yaml`。

```powershell
pwsh scripts/project-python.ps1 scripts/calibration_station.py up --mode sim-mobile
# 等价的底层五文件命令：
# docker compose --project-directory . -p edge-fixed-cal -f compose/calibration-board.yaml -f compose/calibration-console.yaml -f compose/calibration-model.yaml -f compose/calibration-fixed-rover.yaml -f compose/calibration-view-motion.yaml up -d
```

打开[工作台](http://127.0.0.1:8916/#sessions)，提交「请帮我标定当前传感器」。页面提示摆板后，在本机另一个终端执行下面的命令一次，模拟人将唯一的实体板放到车头正前方，板面朝向车头，底边触地。车和板保持静止，直到相机和前雷达分别取得两次完整观测。工位将摆板回执交给模型。每次换位后，旧视点引用失效，模型可依据新观测选择下一步。

```powershell
docker exec edge-fixed-cal-fixed-sim-1 bash -lc 'source /opt/ros/jazzy/setup.bash && python3 /fixture/fixed_rover_world.py place-front /tmp/fixed-calibration/world.sdf --profile /fixture/profile.json'
```

复现带前端录像的验收时，在提交任务前启动旁路见证，在另一个终端运行浏览器脚本。先为浏览器脚本配置 `puppeteer-core` 和指向本机 Chromium 或 Chrome 的 `CHROME_PATH`。使用新的输出目录。脚本在真实工作台输入原句、点击一次并保存原始画面，见证程序读取页面提交生成的任务编号。看到摆板提示后执行上述摆板命令。

```powershell
pwsh scripts/project-python.ps1 tests/system/fixed_rover_calibration_witness.py --observe-ui --staged-board --task-text '请帮我标定当前传感器' --out artifacts/verification/fixed-board-trial
node tests/system/fixed_rover_browser.cjs --submit artifacts/verification/fixed-board-trial/frames http://127.0.0.1:8916 artifacts/verification/fixed-board-trial
```

`request.json` 保存任务原文。`events.jsonl` 保存 Agent 决策和原操作号。`physical-owner.json` 保存底盘回执，`world-poses.jsonl` 保存独立真值轨迹。成功的 `summary.json` 同时要求真实模型、实际换位并停稳、独立留出验证和安装真值误差达标。`captures` 包含按哈希校验的 RGB、点云、相机模型、时间戳及 AVI。命名卷持久保存原始任务、停车状态与外参版本。

## 仿真对应的装配

| 部件 | 仿真配置及依据 |
| --- | --- |
| 底盘 | 灵遨四轮差速无人车，按项目留存安装模型设置轮半径 75 mm、轮距 281 mm、轴距 240 mm，保留原地转向和转向滑移。 |
| 靶标 | 采用现场登记的 1400 × 1000 mm 四孔板，四孔直径约 240 mm，黑码边长约 200 mm。左右码中心距约 1100 mm 经现场卷尺核对。上下码中心距 700 mm、横纵孔中心距 500 × 400 mm 取自按卷尺比例复核的相片和 D435 观测。前雷达独立估得下排孔距 494.29 mm。[实物登记依据](../../artifacts/verification/jetson-four-hole-20261125/board-geometry-provenance.json) |
| 前 MID360 | 按项目安装模型取前置、下倾约 40° 的点云传感器及其 IMU 安装方向。以有界视场的规则激光束模拟点云。实机的非重复扫描、噪声、时差和丢包需要另行实测。 |
| D435 | 640 × 480 RGB-D 渲染和同帧 CameraInfo，使用标称相机安装位置。四孔法以 RGB 码角点、流内参和原生雷达孔位求外参。 |

## 控制与真值的边界

取景插件按标定板完整外沿评估起步转向、行车和末端转向的 RGB 投影。启用后方覆盖的 Gazebo 工位允许以最高 0.07 m/s 倒行，兼顾下倾前雷达的孔位视野与相机朝向。现有软件停车和限速仍由独立仲裁器执行，后方扫描缺失、失效或发现障碍会停止倒行。

`view-target-source` 复用已登记的目标检测器，发布带光学帧和采集时间的归一化像素框。执行器根据边距约束速度并有限修正航向，过期或失去目标即归零。持续失去目标时保留原操作的恢复状态。独立见证程序可用 `--tracking` 核对安全出口的非零速度与像素框时间，原始观测保存在 `motion/trajectory.jsonl.gz`。移除跟踪站点配置要先排空并按物理运行时重新绑定。实车后向观察和制停需要单独验收。

启用 `capture_navigation` 的站点把启动时车头设为局部 X 正方向，左侧为 Y 正方向，上方为 Z 正方向。车体位置和板位均带观测时间与定位代次。雷达周界按车体圆形包络筛选候选走廊，运动期间仍由独立仲裁链路判定制停。模型只读取最新有效的路线、终点位姿、净空余量与尚未覆盖的板侧和 RGB 区域。差速车通过斜向行驶变换侧向位置，仍以同一套 `linear.x` 和 `angular.z` 执行。

用 `tests/system/fixed_view_coverage.py <验收目录>` 可从独立世界轨迹、仿真相机安装和采集的原始图像复核拍摄时车体及相机的侧向分布、采样航向和 RGB 标记角点九宫格覆盖。RGB 网格统计实际落点，固定直立板和平面车产生的角度范围由现场装配及两传感器共同视野约束。独立相机内参任务仍需要俯仰、滚转和足够的画面边缘观测。

Agent 选取 `move_to_pose(goal_ref)`。原操作号绑定观测时间、机器人相对目标位置、到达朝向及物理插件版本。控制器按 `linear.x` 和 `angular.z` 下发 `geometry_msgs/msg/Twist`，限速 0.10 m/s、0.12 rad/s。命令、仲裁输出、安全输出、软件停车锁、里程计消息与车体轴向统一登记在[底盘协议](../../scripts/lingao_control/lingao-chassis-control.json)。部署选取具体的驱动、观测和验收提供者。[现场控制记录](../evidence/lingao-chassis-commissioning-20261125.md)

仿真执行器按轮速和前 IMU 的车体偏航角速度估计运动。独立进程读取 Gazebo 世界位姿，核对位置、最终朝向和停稳窗口。标定算法只读取按任务归档的观测，外部验收器在结果发布后才读取 SDF 安装变换。安全适配器使用仿真专用周界扫描。现场独立避碰和停车见证的来源须由实机部署提供。

本站点的部署者可为明确的标定请求配置限定的目标授权。普通标定意图需取得原任务的正面摆板回执，随后才可为这一任务申请已登记的 `move_to_pose`。实际动作继续经过冻结绑定、仲裁、限速和独立验收。只读查询及手动举板工位沿用各自权限。部署侧通过只读文件 `EDGE_FEEDBACK_GOAL_MOTION_GRANTS` 选择是否启用该授权。[授权范围](../../calibration/station/autonomous-board-motion.json)

地图与模型通过[有来源的语义子图](../specs/SDD-060-semantic-scene-queries.md)交换当前目标、观测坐标系和可见关系。部署名称、传输地址和独立世界真值留在部署与审计侧。模型不需要场地专用分支或预先编好的拍摄路线。
