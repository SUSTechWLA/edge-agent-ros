# SDD-061 语义-空间解耦先验、单调安全代价与标定取景走廊升级

## 背景与价值

在基线 `24d83f70f7e927fc937494a3b3e26b92859ece64` 上，`edge-agent-ros` 已具备受限语义场景子图（`SDD-060`）、室内外几何栅格路由（`SDD-058`）与固定地面靶标自主换位标定（`SDD-059`）。但对照开放世界自主移动架构（ROAM），当前系统仍存在四处能力断层：

1. **占据栅格仅支持二值几何准入**：`navigation/grid_corridor.py` 与 `exploration/survey_route.py` 仅区分自由栅格（`cells == 0`）与障碍/未知栅格，无法表达连续的语义风险（`risk`）、本体可通行性（`traversable`）与通行偏好（`preferred`），也缺乏“处于优选区时约束偏离、借道非优选区绕障时自动解除虚拟路肩以防死锁”的条件偏好代价。
2. **缺少非方向性动态语义词表与跨帧平滑契约**：若视觉语言模型输出含相对方位词（如 `left_grass`、`path_ahead`）的标签，下游分割与局部地图会产生视角相关性错配；单帧无记忆推理在路牌移出视野后会立即遗忘风险禁令。
3. **端侧小模型缺少三维几何附着关系**：在 `edge_contracts/semantic_scene.py` 中，现有关系集仅含 `observed_by / approach_to / inside / adjacent`，路牌、靶板或警戒物无法显式表达对邻近地面区域或作业走廊的 `attached_to / guards` 归属关系。
4. **主动标定取景规划缺少统一的连续走廊风险与条件偏好评分**：`calibration/view_planner.py` 通过硬阈值剔除越界候选后，按离散启发式排序，尚未向规划器提供统一的连续走廊风险（`corridor_risk`）与条件偏好代价（`semantic_cost`）可解释事实。

本次交付聚焦高收益、零重型推理依赖的核心契约与闭环升级：在 `edge_contracts` 建立纯标准库的语义先验词表、滑动记忆滤波、三维路牌/靶标附着关系与单调安全合成法则，将其同时接入 `exploration/survey_route.py` 地图路由与 `calibration/view_planner.py`（`v6`）主动标定取景走廊，并在真实 Gazebo + Ollama 仿真工位跑通自然语言自动化标定任务。本次不引入重型 6 Hz GPU 开放词汇分割容器（留作后续独立 GPU sidecar 规格）。

## 设计与契约

### 扩展点与所有者

| 项目 | 本次设计 |
|---|---|
| 扩展入口 | 复用 `edge_contracts`（共享纯标准库契约）、`exploration/survey_route.py`（只读路线计算 Service `/route`）、`calibration/view_planner.py`（`board.view-plan` Service `v1.5.0` / `plan_capture_views` Tool `v6`）及 `agent/planning.py` 有界观测字段白名单，不新增平行插件框架 |
| Definition | `edge_contracts/semantic_costmap.py` 定义：（1）`validate_semantic_prior`：5–16 个非方向性 `snake_case` 标签，每个含 `[0, 1]` 标量 `{risk, traversable, preferred}`，强制注入兜底类 `other: {risk: 1.0, traversable: 0.0, preferred: 0.0}`；（2）`SemanticPriorMemory`：默认窗口 3 帧的跨帧均值滤波与 `valid_until_ns` TTL；（3）`synthesize_semantic_field`：温度缩放 Softmax 期望场合成；（4）`monotonic_cell_admitted` 与 `conditional_preference_cost`：几何一票否决 + 条件偏好代价公式。`edge_contracts/semantic_scene.py` 扩展 `attached_to`、`guards` 关系与三维距离归属函数 `attach_sign_entities` |
| Provider | `edge_contracts`（共享契约库）；`exploration/survey_providers.py`（地图路由只读提供者）；`calibration/board_provider.py` + `calibration/board_session.py`（标定取景规划 `v1.5.0` 与会话提供者） |
| Consumer | `exploration/survey_route.py`（消费可选 `semantic_layers` 进行路线搜索）；`calibration/view_planner.py`（消费周界净空、轨迹边距与当前采集区覆盖状态计算 `corridor_risk`、`preferred_zone`、`semantic_cost`）；`agent/planning.py` `feedback_plan`（向 LLM 暴露有界标量摘要，模型仍仅选择不透明 `goal_ref`） |
| 状态所有者 | 物理运动租约、实时避障刹停与停稳见证仍归 `view-controller` / `view-guard` / `view-verifier` 与 `edge_core` 独占；标定样本与修订归 `calibration-session`；语义先验滑动记忆归各自感知会话内存所有，不跨任务污染 |
| 上下文与证据 | 保留 `task_id`、`operation_id`、`stamp_ns`、`origin_epoch`、`source_ref`；路由回执新增 `semantic_prior_stale` / `semantic_source_ref` 状态，标定取景候选新增 `corridor_risk`、`preferred_zone`、`semantic_cost`，标定场景子图在观测到净空走廊时附带 `["target", "guards", "capture_zone"]` 边 |
| 宿主变更 | `agent/planning.py` 仅在 `feedback_plan` 的 `view_options` 标量投影白名单中追加 `"corridor_risk"` 与 `"semantic_cost"` 两个只读数值字段；未启用语义层的旧调用方输入输出保持完全兼容 |

新概念说明：`semantic_costmap`（多层语义代价层）最接近现有 `occupancy.Grid`，区别在于 `occupancy.Grid` 由激光/深度几何观测独占且表达二值障碍，而 `semantic_costmap` 叠加带 TTL 的 `[0, 1]` 连续 `{risk, traversable, preferred}` 通道，且受**单调安全铁律**约束——当某栅格几何占据 `cells > 0` 时，即便语义层给出 `risk = 0.0, traversable = 1.0`，准入结果仍恒为拒绝。

### 生命周期与旧行为

| 场景 | 新请求 | 已受理/在途工作 | 证据、资源与责任方 |
|---|---|---|---|
| 安装 / 首次发现 | `plan_capture_views` 升级至版本 `6`，`board.view-plan` 升级至 `1.5.0`；`board_session.py` 同时兼容 `("1".."6")` | 已冻结步骤按原 `operation_id` 与版本对账 | 描述符与源码摘要写入取景 `source.planner_version` 与 `planner_sha256` |
| 依赖缺失 / 语义过期（TTL 超时） | 含方位词（如 `left_road`）或非法数值的先验被 `validate_semantic_prior` 拒绝；`stamp_ns > valid_until_ns` 或缺省 `semantic_layers` 时自动降级为纯几何准入 | 在途物理换位仍由激光周界 `view-guard` 独立保护，不因语义过期中断或放行实体障碍 | 路由结果显式标记 `semantic_prior_stale: true`，不静默伪造新鲜语义 |
| draining / 卸载 | 撤下语义层输入后，`survey_route` 与 `view_planner` 退回原有几何距离场逻辑 | 软宿主排空期间拒绝新读取绑定 | 无新增后台常驻线程或设备句柄 |
| 换版 / 旧 binding | `TOOL_VERSIONS["plan-views"]` 保留 `1..6` 兼容旧清单，新工位清单声明版本 `6` | 旧归档与历史回放中的 `scene_slice` / `view_options` 原样可读 | `compact_scene` 保持 `version == 1` 向后兼容 |
| 超时 / 崩溃 / 重启 | 重启后 `SemanticPriorMemory` 为空，首帧前按纯几何保守运行 | `calibration-session` 与 `physical_ops` 按原 `operation_id` 恢复，不重复触发底盘运动 | 独立 Gazebo 世界位姿见证核对物理运动次数与停稳窗口 |

## 验收与证据

| AC | 输入 / 故障 | 可独立观察的预期 | 测试入口 | 实测结果 / 证据 |
|---|---|---|---|---|
| AC-1 | 动态语义词表正反例（含 `left/right/ahead` 方位词、超界值、缺 `other` 类）、3 帧滑动窗口更新及温度缩放 Softmax 合成 | 方位词与非法标量抛出 `ValueError`；`other` 类恒为 `{risk:1, traversable:0, preferred:0}`；连续 3 帧同名标签输出算术均值，路牌移出单帧后风险记忆不跳零；TTL 超期标记 `stale=True` | `tests/test_semantic_costmap.py` | 通过（宿主机与容器均 4/4 通过） |
| AC-2 | 3D 路牌/靶标锚点与多个候选地面/走廊实体（含距离内与超距实体）输入 `attach_sign_entities` 与 `target_scene` | 距离阈值内的实体生成合法 `attached_to` / `guards` 边并通过 `compact_scene` 预算校验；超距实体不误绑；原始点云字节不泄露进模型上下文 | `tests/test_semantic_costmap.py`、`tests/test_semantic_scene.py` | 通过（4/4 + 3/3 通过） |
| AC-3 | 双层栅格路由叠加 `semantic_layers`：（a）语义标 `risk=0` 但激光标障碍；（b）主步道无几何障碍但施工牌标 `risk=0.95`；（c）机器人在步道内 vs 被迫借道草坪绕行；（d）语义层超过 `valid_until_ns` | （a）几何障碍一票否决，绝不穿墙；（b）自动绕开高风险施工区借道草坪；（c）在步道上抑制抄草坪，进入草坪后自动解除虚拟路肩顺利绕回主路；（d）过期自动退回纯几何路径并报告 `semantic_prior_stale: true` | `tests/test_semantic_costmap.py`、`tests/test_survey_providers.py` | 通过（宿主机与容器 13/13 通过） |
| AC-4 | 主动标定 `plan_capture_views`（`v6`）在已采样与缺侧视角下规划候选走廊，并经 `agent/planning.py` 构造模型输入 | 候选包含有界 `corridor_risk`、`preferred_zone`、`semantic_cost`；初始仅中心采样时解除非优选过渡惩罚，优先推荐低风险补侧视角；`feedback_plan` 准确透传 `corridor_risk` 与 `semantic_cost` | `tests/test_capture_views.py`、`tests/test_semantic_costmap.py`、`tests/test_feedback_planner.py` | 通过（容器 `test_capture_views.py` 10/10、`test_feedback_planner.py` 14/14） |
| AC-5 | 启动隔离 Gazebo 3D 仿真 + LingAo 差速车 + 本地 Ollama（`qwen3:4b-instruct`），提交自然语言任务「请帮我标定当前传感器」 | 见证首次摆板提示与确认、真实模型逐步选择工具、多次自主换位并经独立世界真值确认停稳、`plan_capture_views` v6 产出语义走廊评分与 `guards` 场景边、独立留出孔心误差 ≤ 25 mm、相对 Gazebo SDF 安装真值误差 ≤ 35 mm / 1.5° | `tests/system/fixed_rover_calibration_witness.py` | 通过：任务 `fixed-cal-da3d0edbddd24305` `SUCCEEDED`（8 次换位，留出孔心 RMS `5.80 mm` / 最大 `13.17 mm`，安装真值误差 `5.25 mm` / `0.132°`）。[验收摘要](../../artifacts/verification/sdd061-roam-autocal-20261127/summary.json) · [外参发布](../../artifacts/verification/sdd061-roam-autocal-20261127/spatial-publication.json) |

## 迁移与回退

1. **升级与部署同步**：`edge_contracts/semantic_costmap.py`、`edge_contracts/semantic_scene.py`、`navigation/grid_corridor.py`、`exploration/survey_route.py` 与 `calibration/` 均已纳入 `scripts/release_robot.py` 的运行时投影范围，合并主分支后执行 `python scripts/release_robot.py HEAD` 即可同步至 `deploy/robot`。
2. **在线与配置回退**：不传 `semantic_layers` 的既有地图路由调用方自动走原几何走廊逻辑；若需回退标定取景工具清单版本，可将 `calibration/station/tools/plan_capture_views.json` 恢复为 `"version": "5"`（`board_session.py` 兼容 `1..6`）；完整代码回退执行 `git revert <合并提交>` 并重新投影 `deploy/robot`，已持久化的任务账本与标定修订文件保持可读。

## 变更记录

2026-11-27 完成 `SDD-061`（AC-1 至 AC-5）全部实现、契约单测与 Gazebo 自然语言自动化标定闭环验收：（1）新增 `edge_contracts/semantic_costmap.py`（纯标准库）并扩展 `edge_contracts/semantic_scene.py`（`attached_to`、`guards`、`attach_sign_entities`），接入 `navigation/grid_corridor.py`、`exploration/survey_route.py`、`calibration/view_planner.py`（`v6` / `board.view-plan` `v1.5.0`）、`calibration/board_session.py` 与 `agent/planning.py`；（2）宿主机套件 `tests/system_suite.py --stages repository planner`（74 组测试，运行号 `20261126T170057Z-390ea8a1`）及标定容器内 `tests/test_capture_views.py`（10/10）、`tests/test_survey_providers.py`（13/13）全部通过；（3）在 16 容器隔离 Gazebo 3D 仿真工位提交自然语言指令「请帮我标定当前传感器」，真实本地模型 `ollama/qwen3:4b-instruct` 完成任务 `fixed-cal-da3d0edbddd24305`（`SUCCEEDED`，29 步、8 次自主换位与独立世界位姿停稳见证，`plan_capture_views` `v6` 实时输出 `corridor_risk` / `semantic_cost` 与 `["target", "guards", "capture_zone"]` 场景边；4 组独立留出姿态孔心 RMS `5.80 mm`、最大 `13.17 mm`，相对 Gazebo SDF 安装真值平移误差 `5.25 mm`、旋转误差 `0.132°`）。实车野外语义先验与 6 Hz GPU 开放词汇分割 sidecar 仍待后续实机阶段验收。[验收摘要](../../artifacts/verification/sdd061-roam-autocal-20261127/summary.json) · [外参发布](../../artifacts/verification/sdd061-roam-autocal-20261127/spatial-publication.json)

2026-11-27 基于 `24d83f70f7e927fc937494a3b3e26b92859ece64` 立项登记 `SDD-061`（AC-1 至 AC-5），按 ROAM 语义-空间解耦设计落地纯标准库语义代价与三维附着契约、单调安全条件偏好路由及主动标定走廊评分，并准备执行 Gazebo 自然语言自动化标定闭环验收。
