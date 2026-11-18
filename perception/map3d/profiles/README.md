# Livox MID-360 与其他三维雷达

## 1. 在车上确认来源

MID-360 由单独部署的 `livox_ros_driver2` 连接，驱动侧需要与网卡匹配的设备网络配置和 Livox SDK2。厂商 ROS 2 的 MID360 点云启动示例采用 `xfer_format=0`（PointCloud2）、`multi_topic=0`（共用话题）和 `livox_frame`。共用模式的 ROS 名称为 `livox/lidar`，默认根命名空间下为 `/livox/lidar`。格式 1 的消息是厂商 CustomMsg；若还在使用该模式，切换驱动配置后重新核验 ROS 类型，或由单独的转换提供者发布 PointCloud2。[官方配置、SDK2 与构建说明](https://github.com/Livox-SDK/livox_ros_driver2) · [MID360 ROS 2 启动文件](https://github.com/Livox-SDK/livox_ros_driver2/blob/master/launch_ROS2/rviz_MID360_launch.py) · [官方打包和话题实现](https://github.com/Livox-SDK/livox_ros_driver2/blob/master/src/lddc.cpp)

在可访问同一 ROS 域的终端，先核实下列现场值：

```sh
ros2 topic type /livox/lidar
ros2 topic echo /livox/lidar --field header --once --qos-reliability best_effort
ros2 run tf2_ros tf2_echo odom livox_frame
```

第一项应显示 `sensor_msgs/msg/PointCloud2`。第二项取得真实 frame_id、包头时间戳。第三项检查坐标树连通性。行驶中还需有采样时刻的 TF 历史：通常由定位系统发布 odom → base_link，由标定或机器人描述发布 base_link → livox_frame。配置中没有预置实体安装高度；实施人员按本机标定提供外参。驱动已做坐标变换时按其真实输出 frame 配置，避免重复应用外参。机器人、驱动、定位及 ROS 时钟必须使用可对齐的时间基准。采用该插件不要求在 Agent 容器中安装厂商 SDK 或链接设备驱动。

## 2. 热装或撤下地图来源

以仓库根目录为当前目录。先启动硬件版地图，再把示例清单原子移入该服务已挂载的目录：

```sh
docker compose -f compose.hardware.yaml --profile map3d up -d map3d
cp perception/map3d/profiles/livox-mid360.json map-inputs/.livox-mid360.tmp
mv map-inputs/.livox-mid360.tmp map-inputs/livox-mid360.json
```

可在 [三维地图](http://127.0.0.1:8770/) 选择此来源；程序通过 [只读地图接口](http://127.0.0.1:8770/api/map) 的 `cloud_sources["livox-mid360"]` 读取。刚安装为 waiting，定位、时效和数据布局均有效后转 online；错误时附有原因码。单看坐标树有路径不能证明过去特定时间可插值。举例，在检测到某包的时间前后机器人分别位于 x=10 和 x=14，正中采样应按 x=12 配准。白盒用这种工况检查节点确实读取指定时刻的变换。[ROS 时间插值验收](../../../tests/test_lidar_inputs_ros.py)

停用时将 `map-inputs/livox-mid360.json` 改名为 `livox-mid360.disabled`。每秒检查一次目录，撤下或替换时退订并删除该来源的旧点。重新安装从新数据建立。若需要观察台“原始传感器”同时展示包头、点数和字段预览，以相同的临时文件加改名方式将这一份清单复制到 `observations/livox-mid360.json`。地图使用完整二进制数据并做有界采样，Console 展示有界摘要。两处可以独立启停。

## 3. 其他雷达和室内外工作区

清单必须使用文件名 `<id>.json`。将示例复制成新 ID，设置实际话题、expected_frame、显示名和 ttl_s。具备 XYZ float32/64 字段的标准 PointCloud2 可沿用同一实现。地图最多热装四路附加来源，每路有独立的最近 2,048 个体素。超过目录预算将停止该目录中的附加订阅并报告错误；原 primary 与机器人任务可继续运行。每一新源要验证实际帧、驱动、标定与时间同步，尤其是同品牌多个设备的独立标识。

例如室内车载 MID-360 用 `/rover/livox/lidar`、`rover/livox_frame`；室外固定测站可用 `/yard/points`、`yard/scanner`。这些是示例名称。每路各用一份文件，同时通过独立且有依据的 TF 连接到配置的固定 frame。启用多设备时检查厂商 `multi_topic` 设置、ROS 实际话题和真实 frame 的唯一性，避免将同一话题和模糊坐标重复登记。固定 frame 不同时，先建立经过验证的空间关系，或使用隔离实例查看各自地图。

当前地图按整包 header.stamp 配准，不使用 MID-360 每点的 timestamp 做运动补偿。高速运动、设备时钟偏差、定位漂移和长程建图需要上游去畸变与定位系统，并在真实机体上评估。Gazebo 固定多线台站、合成 MID-360 七字段 ROS 消息与实际 Livox 设备是三种不同试验。此次没有连接真雷达。
