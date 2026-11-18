# TU Delft X 校园试验场的资产、坐标与测试

本场地把教学楼、机器人实验广场、运动场与林带放在同一张代尔夫特校园航片上。沿步道可以从门厅走到体育园并接入 Mekelweg 西侧人行道。打开“校园试验场”可从同一个斜视镜头定位这些设施；“全校”提供北向俯视。[场景构建器](build.py) · [可视化及导航入口](README.md)

## 场地如何复现

| 内容 | 来源与处理 | 可用于检验 |
|---|---|---|
| 545 × 1,170 米的地面与 187 个完整 BAG 建筑对象 | 荷兰 3DBAG LoD2.2、PDOK 2025 正射影像与 Kadaster BGT 人行道。建筑保留原朝向、轮廓和屋顶。场地以米为单位，东向为 X，北向为 Y；`odom` 原点对应 RD New (85610, 445601)。 | 校园布局、建筑遮挡、外形与街网配准。立面窗格采用示意材质；航片中的车、树和行人只有颜色。 [源数据、授权及离线复建](real_site_data/README.md) |
| 四层教学楼、三层侧翼、实验广场 | 项目在体育园的训练用地构建，层高 3.68 m。砖铺广场位于教学楼东侧，连接有行道树、长椅和灯杆的步道。 | 门厅到门外的路线、室内近障、开门后的观测变化与多机器人动作反馈。[机器人资产、控制方式及限制](robots/README.md) |
| 体育场 | 以 (−234, 61) 为中心，对齐原有球场，长轴由东方向逆时针转 20°。八道跑道由 84.39 m 直道和内沿半径 36.50 m 的弯道组成，道宽 1.22 m。第一道沿内沿外侧 0.30 m 计长约 400.001 m。内场为 105 × 68 m，球门净宽 7.32 m、净高 2.44 m。 | 开阔区域激光观测、平面定位与细线展示；球门、看台及灯塔有实体几何。尺寸参照 [World Athletics 设施手册](https://worldathletics.org/about-iaaf/documents/technical-information) 和 [IFAB 场地规则](https://www.theifab.com/laws/latest/the-field-of-play/)。 |
| 林带与道路 | 林道沿可见林冠从西南伸向东北。程序生成阔叶树、植树行列和步道；部分松树采用 Gazebo Fuel OpenRobotics / Pine Tree v6，原模型高约 5.08 m、冠幅约 3.17 m。Mekelweg 街口落在 BGT 的实际人行道面内。 | 林下遮挡、树干回波、校园步道与测绘区域的衔接。[布局与路线](landscape.py) · [原始松树](https://app.gazebosim.org/OpenRobotics/fuel/models/Pine%20Tree) |

旧版 NCS / SIT 校园的 OBJ 最高顶点为 3.49 m，包含道路、家具与低墙。移除这块单层场景后，教学与体育设施在同一片体育园内共享航片、坐标与街口。历史版本仍可从 Git 复现。[原 NCS 模型](https://app.gazebosim.org/NCS_robotmanager/fuel/models/Campus)

## 从一次穿门到长期测试

| 任务 | 关键证据 |
|---|---|
| **连续往返**。从 (0, 0) 发“前往教学楼门外”，停车后发“回到教学楼门厅”，再发“前往教学楼门外”。三次单程名义长度合计 9.9 m，第二次穿门需要转向。 | `navigate_place` 的三个 trace、同一仿真会话/场景摘要、独立 `/ground_truth` 和 `/odom` 的门内、门洞、门外与停稳样本。入口位于 (1.6, 0)，净宽 2.20 m、净高 2.70 m。[连续往返核验](../../tests/system/campus_round_trip.py) |
| **整条步道核查**。从教学楼东侧出发，经过看台入口、草坪、林带，最后进入 Mekelweg 人行道。 | `overview/manifest.json` 的 `routes` 逐段列出坐标、宽度、SDF 路面名称。独立核查对 25 段共 776.82 m 的中心线投影车体高度范围内的障碍，并拿原始 BGT 面复核街口 (65.5, 14)。[物理几何核查](../../tests/system/campus_route_geometry.py) |
| **分站感知**。在球场中圈、林间步道与教学楼外对比同一台轮式机器人的激光。 | 使用 Gazebo 测试工装单独摆位，等待带新仿真时间戳且位置匹配的扫描，再比较有效回波。该实验测试各环境的可感知性，站间行驶需要另行验收。[观测工装](../../tests/system/campus_station_probe.py) |

将整条步道开放为自然语言导航还需要与场景绑定的许可区域、逐段限时、位置丢失时的停车与恢复、跨段任务日志。目前导航网关逐轴校验目标在 ±4 m 内，单段直行的命令速度上限为 0.32 m/s。按该速度完成 776.82 m 单算平移也需约 40.5 分钟仿真时间，转弯、停车和核验会进一步增加耗时。可先在真实执行的门厅往返中反复验证会话连续性，再逐段开放到广场和林道。[动作网关](../../src/edge_gateway/src/edge_gateway_node.cpp)

## 缓存与离线测试

校园测绘数据和机器人模型已随代码固定版本。外部 Fuel 松树模型缓存于 `artifacts/sim-assets/`，容器中对应 `/assets/models/Pine Tree/`。Pine Tree v6 由 Louise Poubel / OpenRobotics 发布，采用 CC0-1.0。ZIP 大小为 877,470 字节；下载器核对 SHA-256、文件清单和入口，可从完整缓存离线恢复缺失文件。[版本锁](asset-lock.json) · [松树授权](https://creativecommons.org/publicdomain/zero/1.0/)

从项目根目录运行。

```bash
python scene/campus_large/fetch_assets.py
python scene/campus_large/fetch_assets.py --offline
python -m unittest discover -s tests -p test_campus_facilities.py -v
```
