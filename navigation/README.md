# Edge Agent 导航插件

导航插件回答「这个主体在哪里」「这个地点在当前图上指向哪里」「它按当前约束可以怎么走」。`nav_plan` 返回一份可校验的路线和起点定位证据。导航查询默认只读。机器人执行另有机体本地的 physical 插件、独占租约和独立见证。人端的路线类型为 `guidance`，本版交付路径几何，指引送达和本人到达确认需通过终端提供者接入。 [SDD-051](../docs/specs/SDD-051-unified-navigation.md)

## 公开入口

| Tool | 标量参数 | 成功证据 |
| --- | --- | --- |
| `read_spatial_observations` | `actor_ref` | 主体的观测批次，附原始来源、时钟、标定及数据类型 |
| `nav_locate` | `actor_ref` | 融合位置、方差、来源、地图、时效、定位代次 |
| `nav_resolve` | `actor_ref`、`destination_ref` | 当前地图和机体 profile 允许的唯一地点入口及其坐标 |
| `nav_plan` | `actor_ref`、`destination_ref` | `evidence.route` 和 `evidence.localization`。路线包含 `route_ref`、有序路点和长度 |

只读工具通过 `ctx.require` 调用目录登记的 Service。地图目录、规划、观测与融合独立发布。巡检、建图或录制工具可以直接消费 `sensing.observations`，需要定位的业务可以叠加 `localization.position`。原始 ROS 消息经现场适配器发布为带来源的观测；读取器、融合器和规划器都不持有机体执行权。 [观测与融合契约](../perception/spatial_facts/README.md) · [Tool 适配器](tools.py)

## 接入地图、机体与算法

1. **登记地图**。在 `navigation.maps` 的 `catalog.maps` 中声明场地、地图 ID、修订、坐标系、地图类型和几何数据。`map_reference(record)` 对这些字段整体计算 SHA-256。同名坐标系可以属于不同场地或修订。二维栅格几何直接接受项目地图制品中的 `occupancy.json` 数据，保留 ROS 行优先布局、原点和偏航角。有向图用 `geometry.nodes` 声明米制坐标，`geometry.edges` 声明起点、终点和通行标签。
2. **登记机体与入口**。在 `profiles` 中声明路线类型、维数、算法及通行约束，在 `actors` 中绑定主体的 profile 和活动地图。在 `destinations` 按地点登记该地图上各 profile 适用的入口。未命中返回 `navigation.destination_unavailable`，同一主体和地图有多个匹配项返回 `navigation.destination_ambiguous`。定位事实必须绑定同一主体、地图和坐标系。地面路线为二维、飞行路线为三维，地形与步行 profile 可声明二维或三维。
3. **切换规划算法**。内置 `algorithm_ref` 可选 `astar` 或 `dijkstra`。其他算法可以独立提供 `navigation.route_planner/call@1`。该服务接收 `actor_ref`、`destination_ref` 和已绑定的 `localization`，返回通过 v1 校验的路线。关联版本、来源、镜像摘要并更新站点 pack，按软插件协议冻结、排空和替换旧 Provider。每次同名服务只有一个发布所有者。算法内部需要更多地图层或模型时，声明自己的服务依赖和配置。 [规划服务入口](route_service.py) · [公开路线契约](../edge_contracts/navigation.py)
4. **接入控制器**。把要执行的路线交给获得该机体授权的物理适配器。它独立核对路线摘要、主体、地图、坐标系、定位代次、profile 摘要及资源占用情况，按原操作号提交、查询和取消。当前仓库的 `nav_execute` 验收使用单独的地面回放执行器。新机体需要提供可独立判定的到达和交接条件。 [physical 回放适配器](../agent/physical_navigation.py)

栅格规划仅通过占据值为零的空闲单元。足迹半径按分辨率向上取整，用方形窗口保守膨胀障碍、未知与边界，再沿四邻接或八邻接走廊（`grid_corridor.py`）搜索。在此基础上，走廊搜索器（供校园路线 `exploration/survey_route.py` 与标定视点规划 `calibration/view_planner.py` v6 共用）支持叠加可选的有界语义空间代价层 `semantic_layers`（`risk`、`traversable`、`preferred`，契约见 [`edge_contracts/semantic_costmap.py`](../edge_contracts/semantic_costmap.py) 与 [SDD-061](../docs/specs/SDD-061-semantic-costmap-corridor.md)）：物理占据/膨胀障碍与 `risk >= 0.85` 始终保持单调硬否决，语义偏好仅在安全空闲单元上产生有界虚拟边权奖励（有效步代价下限 `0.35 * step_len`，保证 A\* 启发式可采纳），一旦语义层超过 TTL 自动退回纯几何规划并标记 `semantic_prior_stale`。有向图只通过已登记且标签获准的边，起点和终点需落在已登记节点。图上某条三维边能否在飞行器动力学、空域或实时避障条件下执行，由具体提供者和站点验收决定。两种表示都限制单条路线最多 256 个路点，超限返回 `navigation.route_too_long`。

路线摘要绑定地图、主体、定位代次、profile 内容、算法与几何。线上的 `localization_stamp_ns` 为十进制字符串，保留纳秒精度。接收端传入独立获得的当前机体绑定，由 `validate_route` 核对上下文、摘要、维数、边界和总长。执行方还需比较当前 profile 摘要并检查独立授权。基准中的四种机体共享同一个地点 ID，各自走本 profile 批准的路线。 [配置示例](bench.py)

## 本地复测

在项目根目录运行以下命令。它们读取合成场景及隔离夹具，可以在没有 ROS 或硬件的电脑上执行。

```sh
python tests/test_navigation_planner.py -v
python tests/test_semantic_costmap.py -v
python tests/test_spatial_facts.py -v
python tests/test_navigation_pack.py -v
python tests/test_navigation_physical.py -v
```

`navigation-bench` 选用双源录制样本，适合评估装卸、冻结和任务回放。`navigation-live` 选用已发布观测并要求显式提供站点地图和融合策略。两种软件配置只申请 `read` 权限。构建站点时以实际镜像和源提交运行 `python scripts/prepare_navigation.py --help` 所列命令。旧任务使用已冻结绑定，停止新任务准入后才能排空并撤回本版。 [站点准备脚本](../scripts/prepare_navigation.py) · [回放 profile](../profiles/navigation-bench.json) · [发布观测 profile](../profiles/navigation-live.json)
