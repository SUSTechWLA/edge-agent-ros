# 三维地图插件

在浏览器打开 [三维地图](http://127.0.0.1:8770/)。ROS 读侧插件在内存维护短期实测表面。页面中彩色点是带真实 Z 值的传感器命中，物体边框是场地提供者发布的先验，金色圆点是定位模块最新的机器人位姿。界面中拖动旋转、Shift 拖动平移、滚轮或加减键缩放，选择“俯视”检查平面分布。“适应视图”回到当前覆盖范围。

## 仿真

从仓库根目录使用含本插件的项目镜像。已有运行中的 Gazebo 时直接追加最后一条命令；两项 profile 均可单独启用。

```sh
docker compose build sim
docker compose up -d sim bridge perception scene console
docker compose --profile map3d --profile map3d-sensor up -d map3d map3d-sensor
```

原有车载 `/scan` 仍是单层二维扫描。选装的固定台站在 Gazebo `edge_lab` 中注册 `edge_map3d_survey`，位于 odom 的 (0,0,1.2) m，以 3 Hz 扫描 160 × 16 个方向。观测来自台站视野，机体移动不会带动台站。插件向 Gazebo 发送模型 XML，使用独立的点云桥及静态 TF，原有控制桥和任务进程可以继续运行。关闭传感器服务回收 ROS 桥和 TF。Gazebo 保留这一台站模型，重启桥时复用，世界重建后重新插入。在运行的 Harmonic/Ogre2 世界删除活动渲染传感器曾使整个仿真进程退出，桥接热插拔由此与实体生命周期分开。

```sh
docker compose stop map3d-sensor
docker compose start map3d-sensor
docker compose stop map3d
```

新版二维观测台的地图卡片在同一主机、同一归档中验证到活动地图插件后，最多约十秒显示“打开实时三维地图”。发布自定义端口时设置 `EDGE_MAP3D_PORT`，例如端口 8870 在控制台 URL 上追加 `?map3dPort=8870#monitor`。旧控制台可直接打开地图端口。默认监听宿主回环，远程开发可转发 8770 与控制台端口到工作站。

## ROS 机器人和非导航消费者

安装可热插拔的附加雷达可从 [MID-360 与其他标准 PointCloud2 输入](profiles/README.md) 开始。主输入和各附加输入分别拥有订阅、时间戳、缓存和来源状态，可在同一地图上比较实测覆盖。

硬件 Compose 中的 `map3d` 可独立启停。先启动已安装的点云驱动及 TF 发布者，再执行：

```sh
EDGE_MAP3D_TOPIC=/lidar/points EDGE_MAP3D_FRAME=map \
docker compose -f compose.hardware.yaml --profile map3d up -d map3d
```

地图只读订阅 PointCloud2、位姿（`EDGE_MAP3D_POSE`）、语义区域与物体（`/edge/semantic_map`），以及占据栅格（`EDGE_MAP3D_GRID_TOPIC`，默认 `/edge/perception/map`）。驱动提供 XYZ 浮点字段和有效的采样时间，TF 应覆盖该时间从传感器到固定 frame 的变换。位姿、栅格与语义先验使用同一固定 frame。每层可独立缺失。

## 在同一个位置比较二维与三维

仿真实例可挂载与当前 ROS 场景同版本、同坐标系的静态外观包。二维显示北向正射图。三维用 WebGL 2 绘制从 Gazebo 导出的 GLB，可以观察同一屋檐的顶部和下方，也可从地面看窗带与球门。只带高度图的旧包和无法使用 WebGL 的浏览器可回退到高度表面。建筑外观、ROS 占据与实测点云各有独立来源。[校园地图与书签](../../scene/campus_large/README.md) · [数据契约](../../docs/specs/SDD-028-campus-geometry.md)

在“可见图层”勾选“已核查测试步道”，再点“查看整条步道”。青色中心线从教学楼门外经运动场、林地与广场连接 Mekelweg 步道，地图当前版本共 25 段、776.823 米。二维与三维共享同一组 `odom` 路线节点。三维视角用场景深度处理树木、看台和建筑的遮挡。线路表示已核查的场景几何；机器人已经行驶的距离和实际轨迹须从具体任务回执与位姿观测读取。

外观包可以在 `manifest.json` 中提供 `route`，字段为 `id`、`frame`、`scene_revision`、`points`（顺序排列的米制 XY）和 `length_m`。校园发布脚本逐段核对原始清单与 Gazebo 路面网格端点。地图端再次核对坐标系、场景版本、地图范围和总长，接受 2 至 256 个节点、总长最多 10 公里。其他外观包可以省略此字段。可用 `node tests/system/console_browser.mjs http://127.0.0.1:8773 /existing/directory/route.json --campus-route` 在桌面和手机视口复核图层开关、视角变化及截图。

打开地图地址（默认 `http://127.0.0.1:8770/`），选择“2D 平面地图”或“3D 空间地图”。也可直接以 `/?view=2d` 打开二维。图层开关共享，观察位置独立。切换回来可以继续检查刚才的位置。

| 想知道什么 | 操作和读数 |
|---|---|
| 机器人附近哪格被占据，是哪片区域？ | 在二维拖动平移，以滚轮在指针位置缩放，点击读取 XY、列行、原始格值和已命名的语义区域。+X 向右、+Y 向上，原点带偏航的栅格仍与物体对齐。 |
| 回波在地面、门框还是屋顶？ | 三维左键拖动环绕，Shift 或右键拖动平移，滚轮或双指缩放。点击可见回波或物体表面读取 XYZ；点击裸露地面可读同一占据单元。方向键、加减号、Home、Enter 可用于键盘检查。轨道相机手势参照 [Three.js OrbitControls](https://threejs.org/docs/pages/OrbitControls.html)。 |
| 灰暗或空白的区域代表什么？ | 看图例和“地图查询”的来源状态。本项目编码以 −1 表示未知，0 表示空闲，100 表示占据；中间值按发布者的占据估计展示。点云只覆盖收到回波的位置，未观测区保持未知。 |

当地图摆在训练场，地面可以同时属于“草坪”且当前格值为“占据”（例如有人站在上面）。占据回答空间观测，语义提供区域或物体含义。以后为草坪增加正射纹理，只需给外观图层提供图像、坐标基准、范围和采集版本。墙面纹理还需要立面几何与映射关系。同一纹理可用于两种观察角度，纹理内容不参与通行判定。[设计及后续纹理接口](../../docs/specs/SDD-026-map-viewers.md)

占据订阅使用 transient local、best effort、深度 1，适用于本项目持续发布的融合地图。单轴最多 4096 格，总计最多 262144 格；平面可带绕 Z 的旋转和固定高度。新样本要求 ROS 时间在 5 秒窗口内，已接受的缓存变旧后显示上次观测。实际机器接入其他发布者时先核对话题、QoS、坐标系、时间来源及格值编码。快照 `grid.cells[row*width+col]` 使用 ROS 行主序，`origin=[x,y,yaw]`，高度另见 `origin_z`。[ROS 消息定义](https://github.com/ros2/common_interfaces/blob/jazzy/nav_msgs/msg/OccupancyGrid.msg)

GET `/api/map` 是同源、只读的 JSON，字段 version、generation、frame 和 environment 描述地图世代与坐标。cloud 包含 topic、source_frame、stamp_s、status、age_s、sample_points、voxel_m、evictions 及 points；pose 的 xyz 单位米，scene 的 features 带底面中心 xyz 和 size。单次消息限 32 MiB，人为下采样最多 4,096 点，保留至多 8,192 个 10 cm 体素。一个体素保存最近接受的真实测量。静止环境的旧回波可能持续保留；动态障碍清除、自由空间、未知体积和碰撞安全性不能由此推断。此数据适合地图展示和只读分析，运动仍按原授权路径执行。

点云几何结构错误、坐标变换缺失和不合时效的时间会返回 error 及原因，超过三秒未更新为 stale，保留的点变灰。仿真时钟回退会清空先前世代的体素、位姿和先验。地图重启也从新观测重建，当前版本没有持久三维 SLAM 地图。源状态变化写入 `journal:map3d`，可在 Console 的事件查询按 source 检索。/health 返回插件 ID、版本和启动事件，用于确认实例归属。[规格](../../docs/specs/SDD-020-3d-map-plugin.md)

每个点云订阅采用 best effort、队列深度 1、单帧上限 32 MiB，并至多抽样 4,096 个点。848 × 480 × 32 字节的 D4xx 兼容组织点云已在 ROS 消息测试中运行；1280 × 720 × 32 字节也在预算内。JSON 的 packet_stamp_s / packet_age_s 描述最近接受的数据包；stamp_s / age_s 描述最近有有效回波的采样及年龄，取 ROS 采样年龄和实际到达年龄的较大值。全 NaN 或无回波的新包保留历史体素并显示灰色，直至再次收到有效点。
