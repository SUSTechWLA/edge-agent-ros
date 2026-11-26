# 用四孔板标定 D435 与 MID-360

在标定工作台提交「请帮我标定当前传感器」。本方法输出从所选雷达原生帧到相机彩色光学帧的空间变换。车保持静止，使用者只需出示标定板。工作台使用车载相机与雷达自行录像与采样。手机单独拍摄的视频缺少对应点云、相机内参和同一采集时间，不能作为该方法的完整输入。

## 如何举板

1. 在页面核对车名、方法、雷达及板编号。用毫米填入黑色方码外边长、同排与同列的码中心距、同排与同列的孔中心距、孔直径。中心距均为完整中心距；选用厂家标称值时，请确认它们属于手上的板。标定过的板再次使用会读取已登记尺寸。四孔布局与第一代 FAST-Calib 相符，其公开配置还列有不同大小的方码。[原板配置与尺寸定义](https://github.com/hku-mars/FAST-Calib/blob/main/config/qr_params.yaml)
2. 把板正面朝向 D435 并放在所选 MID-360 能看到的位置，四个码及孔留在画面内。可从约两米、接近正面开始，再根据实际取景调整。手拿板边，孔后留出空间，避开紧贴的墙面、镜面反光和遮挡。相机显示实际读到的码；雷达独立从原生点云检验四孔，能看到一面墙或板面都不等于看到了四个孔。
3. 当前姿态保持静止，等页面显示已收下该姿态后再换。可在共同视野内依次改变左右倾斜、俯仰和前后距离。程序检查一段时间内多帧的板姿，以及两个分开时间段内激光孔位是否稳定。采集不足、图像变糊、角码被裁切、某雷达缺孔、重复姿态都会显示原因。为补足雷达密度，可调整朝向、背景与距离。
4. 至少取得五个不同姿态，其中三个用于拟合，两个独立留出验证。按页面处理补采要求。只有当前装配显示「已验证」并有版本号后，才可把该版本交给下游作进一步实车验收。姿态片段可下载为 AVI；完整归档另存各帧的相机内参、原始 RGB、点云、时间戳和软件绑定。后向雷达应另行建立有共同视野的工位。

等待现场配合超过三分钟时，工位会暂停当前任务。把板摆好、核对并保存板件尺寸，再点「续办原步骤」即可沿用任务编号和已经收下的板姿。工位如提供带来源的建议尺寸，可点「填入参考规格」填入空栏；这一步只填写表单，核对并保存后尺寸才参与计算。

## 接入硬件

复制[现场配置模板](../../calibration/station/hardware-four-hole.example.json)到设备私有目录，从本机填写传感器身份、ROS 话题、帧、成像模式、时间基准及对应关系。用 `python3 -m calibration.hardware_profile --spatial-only installed-sensors.json` 检查，再用实际消息检验时间戳和帧。支持原生 `PointCloud2`，选择 `livox_ros_driver2/msg/CustomMsg` 时，采集容器内须有现场兼容的 ROS 消息包。未经配准的两台雷达分别保留各自的观测。

沿用[只读工位部署](sensor-time-calibration.md)中的环境变量，可通过统一编排与预检入口 `python3 scripts/calibration_station.py preflight --mode hardware --profile /abs/path/installed-sensors.json --check-live` 一次性核对配置单位、环境变量、Ollama 与在线 ROS 2 发布者，再用 `python3 scripts/calibration_station.py up --mode hardware --profile /abs/path/installed-sensors.json` 启动只读标定工位（时间标定观察器默认按 `time-calibration` profile 隔离，空间标定无需手写 6 个服务名）。若现场只有 Livox UDP 数据流，追加 `--passive-livox`，指定接收网卡、本机地址、雷达源 IP、角色和原生坐标帧。该接收器验证数据包并输出原生米制点云，打开过滤的接收套接字后释放 Linux capabilities。其时间戳是约 50 ms 一段的主机收包时间中点，只适用于静止装配及静止板姿的空间标定。

## 扩展方法

识别器清单声明可识别的目标。方法清单声明目标、必要测量、训练与留出姿态数、算法入口和实现版本。Agent 使用同一组观测、录制、拟合、验证工具，由当前工位选择适用方法；随意更换标定板或停用匹配算法会使新的求解失去有效依据。已实现棋盘和 FAST-Calib 靶型兼容的独立四孔算法。Kalibr、FAST-Calib 上游程序及其他方法可分别增加适配器，并声明自己的输入及验证条件。[方法清单](../../calibration/station/methods) · [四孔法独立重放](../../artifacts/verification/four-hole-dds-20261125/README.md)

## LingAo 单次小行程台架验收

Jetson 实车已验收有人监护的 50 cm 直行和原地左转。标定页面可以显示这两次运动的实测回执。标定任务继续通过已注册的目标检测、同步采集、求解和留出验证能力推进。自主换位需要完成下述物理运行时验收。[底盘编程与实测](lingao-chassis-control.md)

四孔站的标定任务可以请求人工换板姿。底盘是否允许自行换位，由物理运行时单独决定。仓库中的 `calibration.lingao_trial_ros` 是部署前候选，当前没有注册为现场动作工具。

候选复用现场 `move_relative.py` 已核实 SHA-256 的直线控制方法。请求固定为前进或后退 10 mm，速度上限 0.02 m/s。控制进程只面向普通仲裁入口，保持遥控与急停优先。成功需要底盘 MCU 回读的对应方向速度、起始车体朝向上的位移及连续 1.5 s 的停稳证据。厂商进程退出、目标零速指令、图像和 GPS 均不能单独证明这一结果。

现场适配预期 ROS Domain 61、localhost 可见性、唯一 `/lingao_base_driver` 来源及 `odom → base_foot`。操作 UUID 的 SQLite 记录跨进程保留。未证实停稳的操作保留为 `RECOVERY_REQUIRED`，阻止再次下发。物理运行时需要先完成独立、方向匹配的碰撞制停验收，发行绑定主机启动 ID、机器人、操作 UUID、方向和不可续期截止时间的短租约；控制进程卡住时，独立的车端看门狗仍需执行截止。候选定义了本机租约读接口，现场尚无已投产的发行器。现有后雷达诊断回执仅用于观测。

**复验**时，将已核实的车载 `move_relative.py` 提供给隔离的 ROS 容器，同时设置 `--network none` 和独立 Domain。运行 `python3 tests/system/lingao_short_trial_shadow.py <厂商脚本路径> --out <结果文件>`。夹具将每次运行的输入输出限定在唯一的 `/edge/commissioning/shadow_*` 前缀。台架回执使用 `edge.lingao.relative-trial.shadow.v1`，现场契约使用 `edge.lingao.relative-trial.v1`。台架模拟 MCU、互锁与独立看门狗，覆盖方向、人工接管、急停、过期里程计、未运动、未停稳、控制进程冻结和操作重放。
