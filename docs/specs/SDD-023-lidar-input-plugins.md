# SDD-023 · 可热装的三维雷达输入与 MID-360 契约

## 背景与价值

SDD-020 建立只读三维地图，但一份实例只绑定一个默认点云话题。室内外环境同时有固定站、车载雷达时，开发者要能区分谁看到了表面、何时观察以及该设备有没有定位。MID-360 由 Livox 驱动发布标准 ROS 2 PointCloud2 或 Livox CustomMsg。采用前者可复用既有几何实现和 ROS TF。[厂商 ROS 2 启动配置](https://github.com/Livox-SDK/livox_ros_driver2/blob/master/launch_ROS2/rviz_MID360_launch.py) · [打包字段及话题](https://github.com/Livox-SDK/livox_ros_driver2/blob/master/src/lddc.cpp)

## 设计与契约

三维地图服务从只读目录 `/etc/edge/map3d-inputs` 周期读取每机一份输入清单，沿用 Console observation 清单的 id、name、topic、type 和 ttl_s。可选 expected_frame 用于识别误接话题。每个来源拥有独立订阅、体素缓存、时间戳、错误和状态事件。已有主输入和 HTTP 字段 cloud 保持兼容。加装来源在 /api/map 的 cloud_sources 字典中暴露其名称、来源话题、坐标、状态和测量点。图层颜色按高度，过期的点变灰。用户可按来源切换显示。每源有界采样与缓存，附加来源总数也有限制。

PointCloud2 读取 XYZ 的数据类型与实际偏移，强度、tag、line、逐点时间可随驱动存在。地图按包头采样时刻请求 TF；移动车体需要定位系统提供固定 frame 到机体的时间序列和独立标定的机体到雷达外参。本版不做逐点运动去畸变。缺变换、帧不符、数据过期或乱序要隔离于该来源。一个站点关闭、清单移除、同 ID 换话题时，旧缓存不得继续挂到新来源。每次注册变化发出持久来源事件。

## 验收与证据

- AC-1 与官方 xfer_format=0 对齐的七字段合成点云通过真实 ROS 消息和时间插值变换后，输出坐标与独立期望一致。缺 TF 或不符合 expected_frame 时拒绝接纳该包。
- AC-2 运行中增加、损坏、恢复、替换和撤下清单，通过源事件/HTTP 查到变化。一个来源故障时主来源继续可用；在途旧回调无法写入替换来源。
- AC-3 MID-360 与其他标准 PointCloud2 传感器均可接入，不修改 Agent、Console 接收器或控制内核。清单可用于既有原始观测摘要。独立 ROS 发布器、Gazebo 和真实硬件的证据分别留档。

## 迁移与回退

先按厂商说明安装 SDK2 与相应 ROS 2 驱动，实车的 IP、供电、时钟、话题命名和外参由操作者核验。将示例清单原子移入输入目录，移出即可退订并释放该来源缓存。默认目录不启用任何新雷达。原 primary 仍可通过环境选项选择，日常默认值不变。真实板卡、真雷达延迟与在线标定另作验收。

## 变更记录

2026-11-18，先登记多来源边界、Livox 字段契约、故障隔离和 AC-1 至 AC-3，再实现适配与测试。
