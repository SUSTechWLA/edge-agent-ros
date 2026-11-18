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

地图只读订阅 PointCloud2、`EDGE_MAP3D_POSE`（默认 /edge/perception/pose）和 /edge/semantic_map。驱动提供 XYZ 浮点字段和有效的采样时间，TF 应覆盖该时间从传感器到固定 frame 的变换；已经在固定 frame 注册的点云无需 TF。机器人位姿与设计先验须使用同一固定 frame。硬件没有发布语义先验时，实测点云仍可用。安装新设备驱动、标定或解决位姿漂移需由相应所有者承担。

GET `/api/map` 是同源、只读的 JSON，字段 version、generation、frame 和 environment 描述地图世代与坐标。cloud 包含 topic、source_frame、stamp_s、status、age_s、sample_points、voxel_m、evictions 及 points；pose 的 xyz 单位米，scene 的 features 带底面中心 xyz 和 size。单次消息限 32 MiB，人为下采样最多 4,096 点，保留至多 8,192 个 10 cm 体素。一个体素保存最近接受的真实测量。静止环境的旧回波可能持续保留；动态障碍清除、自由空间、未知体积和碰撞安全性不能由此推断。此数据适合地图展示和只读分析，运动仍按原授权路径执行。

点云几何结构错误、坐标变换缺失和不合时效的时间会返回 error 及原因，超过三秒未更新为 stale，保留的点变灰。仿真时钟回退会清空先前世代的体素、位姿和先验。地图重启也从新观测重建，当前版本没有持久三维 SLAM 地图。源状态变化写入 `journal:map3d`，可在 Console 的事件查询按 source 检索。/health 返回插件 ID、版本和启动事件，用于确认实例归属。[规格](../../docs/specs/SDD-020-3d-map-plugin.md)

每个点云订阅采用 best effort、队列深度 1、单帧上限 32 MiB，并至多抽样 4,096 个点。848 × 480 × 32 字节的 D4xx 兼容组织点云已在 ROS 消息测试中运行；1280 × 720 × 32 字节也在预算内。JSON 的 packet_stamp_s / packet_age_s 描述最近接受的数据包；stamp_s / age_s 描述最近有有效回波的采样及年龄，取 ROS 采样年龄和实际到达年龄的较大值。全 NaN 或无回波的新包保留历史体素并显示灰色，直至再次收到有效点。
