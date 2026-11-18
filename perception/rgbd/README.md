# RGB-D 像素测距

在[像素测距页面](http://127.0.0.1:8772/)选择桌角，彩色图和深度图会标出同一像素，并给出 `(u, v) · 1.246 m · RGB …` 这样的读数。距离是该表面沿相机光轴的 Z 分量。斜看物体时，它与从相机中心到物体的直线距离有差别。读数直接来自原始深度像素，伪彩只用于显示，色标在 6 m 饱和。黑色表示该像素没有可用于测量的回波。

## 运行 Gazebo 示范

从仓库根目录运行。第一行提供现有仿真世界与 ROS 时钟，第二行插入独立 RGB-D 台站与观测插件。

```sh
docker compose up -d sim bridge
docker compose -f compose.yaml -f compose.rgbd.yaml -f compose.rgbd-sensor.yaml --profile rgbd-sensor up -d rgbd rgbd-sensor
```

台站固定在场地的 (-1.4, -1.0, 1.1) m，朝向世界 +X，以 3 Hz 生成 320 × 240 的彩色、深度和内参。机器人自身移动时台站仍保持原位。彩色、深度、内参在彩色光学坐标系 `camera_color_optical_frame`，右、下、前分别为 +X、+Y、+Z。Gazebo 原生注册点云 `/edge/rgbd/points` 在机体坐标系 `edge_rgbd_link`，其前三轴是前、左、上。点云经采样时刻的 TF 进入三维地图。要添加这个图层，从仓库根目录把 `perception/rgbd/profiles/gazebo-rgbd-points.json` 复制到 `map-inputs/` 并启用 map3d。仿真插件通过单传感器共投影生成已对齐的数据，可校验系统契约和像素几何；D4xx 的 USB 带宽、主动红外性能和测距精度需上机测量。

```sh
docker compose -f compose.yaml -f compose.rgbd.yaml -f compose.rgbd-sensor.yaml --profile rgbd-sensor stop rgbd-sensor
docker compose -f compose.yaml -f compose.rgbd.yaml -f compose.rgbd-sensor.yaml --profile rgbd-sensor start rgbd-sensor
```

停止现场 ROS 桥后，超过 2 s 没有新帧会关闭测量。停止 `rgbd` 仅撤下页面与读侧订阅。Gazebo 保留台站实体，恢复桥时复用，避免热删除渲染传感器导致世界退出。重启 rgbd 服务读取新的接入配置。仅需复用现有传感器时可以单独部署 `rgbd`；对外提供远程调试通过 SSH 转发本机 8772。发布到其他端口可设置 `EDGE_RGBD_PORT`。

## 接入 RealSense D435i / D455

1. 将相机接到机器人运行驱动的主机。在该机按所用 Ubuntu / Jetson 的上游安装说明安装 librealsense、USB 权限规则及与 ROS 发行版匹配的 realsense2_camera。用 `rs-enumerate-devices` 核对序列号、USB 协议、固件，选定可同时工作的彩深分辨率及帧率。在原生 Linux 常用 USB 3.x 连接以提供带宽。本插件只需读取 ROS，不需获得 USB 设备权限。[厂商驱动安装与参数](https://github.com/realsenseai/realsense-ros) · [Linux USB 权限规则](https://github.com/realsenseai/librealsense/blob/master/doc/installation.md)
2. 在已加载 ROS 环境的终端中启动驱动，并与应用保持相同 ROS Domain 和可通信的 DDS 网络。默认应用使用域 72。对齐由驱动的处理模块实施，内参由驱动随图发布。多台相机时，通过厂商的 camera_name / camera_namespace / serial_no 参数分别绑定，避免覆盖相同 TF 和话题。
3. 在同一台 Linux 主机启动硬件 Compose。硬件镜像使用板卡架构对应的版本。硬件覆盖文件选用系统时间并读取 `config/realsense.json`，仿真模式使用 ROS /clock。不要将两个时钟域的消息拼帧。

```sh
export ROS_DOMAIN_ID=72
ros2 launch realsense2_camera rs_launch.py enable_color:=true enable_depth:=true enable_sync:=true align_depth.enable:=true pointcloud.enable:=true
```

```sh
docker compose -f compose.hardware.yaml -f compose.rgbd.yaml -f compose.rgbd-hardware.yaml up -d rgbd
```

在另一终端核对实际 topic、frame 和内参。D4xx 模板按当前驱动的默认 camera/camera 命名空间；采用自定义 namespace 时复制 `config/realsense.json`，修改三个绝对 topic，设置 `EDGE_RGBD_CONFIG_DIR` 为该目录、`EDGE_RGBD_PROFILE` 为文件名（去掉 .json），再更新单个 rgbd 容器。

|流|仿真 topic|D4xx 厂商驱动常见 topic|
|---|---|---|
|彩色 Image|/edge/rgbd/color/image_raw|/camera/camera/color/image_raw|
|已配准深度 Image|/edge/rgbd/aligned_depth_to_color/image_raw|/camera/camera/aligned_depth_to_color/image_raw|
|彩色 CameraInfo|/edge/rgbd/color/camera_info|/camera/camera/color/camera_info|
|可选 PointCloud2|/edge/rgbd/points|/camera/camera/depth/color/points|

```sh
ros2 topic list -t
ros2 topic echo /camera/camera/color/camera_info --once
ros2 topic echo /camera/camera/aligned_depth_to_color/image_raw --field header --once
```

在 [camera 插件](../camera/README.md) 中展示同一彩色流时，把 [相机声明](profiles/realsense-camera.json) 中 sources 数组里的条目合并进该插件的 sources.json，既有相机可同时保留。将 [D4xx 点云声明](profiles/realsense-points.json) 复制到 map-inputs/ 即可动态追加 [map3d 图层](../map3d/profiles/README.md)。让定位/标定系统发布实际的 `地图 → 机体 → 相机` TF，点云 header 中的源 frame 必须在其采样时间可变换到地图固定 frame。地图仅存 XYZ 表面并按高度着色。仿真台站外参与实物支架须分别标定。 每帧容许 32 MiB；32 字节布局下 848 × 480 约 12.4 MiB，1280 × 720 约 28.1 MiB。多路点云请测量板载 DDS、网络、内存和 CPU 余量。

真机验收时靠近与远离一块正对相机的平板，在不同深度、视野中心和边缘对照标尺，同时观测 RGB 轮廓与深度边界。记下序列号、固件、驱动版本、分辨率、工作距离、重复测量的偏差和无回波比例。遮挡边缘、镜面与强日照会影响实际可用区域。拔掉后检查读数清空与事件记录，再插回确认新帧身份；随车移动场景额外核对采样时刻的 TF。

## 测量契约与诊断

服务独立对接一台相机，复用相机插件的原始图像解码和有界 HTTP 工作线程。校验三路时间戳、非空相同 frame、分辨率一致、有效焦距与主点，返回配置中的配准依据。当前协议面向驱动逐帧发布的 CameraInfo。每路最多暂存三帧，支持最多 1920 × 1080 像素总量，输出预览最多 640 × 480，帧对发布上限 5 Hz。历史最多保留 4 对、最长 2 s，任一时钟检测到超时即失效。HTTP API 只读。状态事件与启动、心跳写入标准 `journal:rgbd`；视频字节和测距点击均不写入事件日志。

|请求|结果|
|---|---|
|GET /health|插件 id、version、started_event，供同实例发现|
|GET /api/rgbd|状态、原因、来源话题、配准声明、frame、分辨率、内参、编码、三路纳秒时间戳、最大与实际时间差、pair_id|
|GET /frame/color.png?pair=…，GET /frame/depth.png?pair=…|指定帧的预览；响应头 X-Pair-Id 确认身份|
|GET /api/sample?u=160&v=120&pair=…|指定帧、原图像素、彩色 RGB 和深度 depth_m，原点左上，列 u、行 v|

省略 pair 时读取最新有效帧。交互或可复现测量请明确传入 pair，防止两次请求落到不同帧。HTTP 400 表示坐标越界或输入错误，409 表示给定帧已淘汰，422 表示该像素无有效回波，503 表示当前没有满足约束的帧。16UC1 的正整数以毫米转米，32FC1 正浮点以米读取。无效值保留为不可测；行填充和字节序按 ROS 消息解码。[ROS 深度图 REP-118](https://github.com/ros-infrastructure/rep/blob/master/rep-0118.rst)

|页面原因|先检查什么|
|---|---|
|waiting_for_streams / clock_unavailable|三个订阅、DDS 域、QoS、仿真 /clock|
|frame_mismatch / size_mismatch|接入的是否为 aligned_depth_to_color，三路是否来自同台设备和同一分辨率|
|timestamp_skew / stamp_outside_clock|三者最大相差须 ≤20 ms，源和接收机使用同一时钟域|
|invalid_intrinsics|CameraInfo.K 的焦距、主点、分辨率及驱动标定|
|stream_timeout / clock_reset|传感器断流、仿真重置或时间回退|
|no_depth_return|该像素有颜色，但深度传感器在该次曝光无有效回波|

[SDD-024 与验收条件](../../docs/specs/SDD-024-realsense-rgbd.md) · [数值与配准回归测试](../../tests/test_rgbd.py)
