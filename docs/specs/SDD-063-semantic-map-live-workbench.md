# SDD-063 实时状态感知与多层语义代价地图前端工作台同步升级

## 背景与价值

在基线 `179f07c45d9f6b1467e1682dc15f4c6f39137df8` 上，`SDD-060` 与 `SDD-061` 已在后端交付受限语义场景子图（`semantic_scene` 含 `attached_to` / `guards` 三维归属边）、多通道语义代价场（`semantic_costmap`：`risk`、`traversable`、`preferred` 与 `valid_until_ns` TTL）、单调几何安全否决与条件虚拟偏好路由（`navigation/grid_corridor.py`、`exploration/survey_route.py`），以及主动标定取景走廊评分（`calibration/view_planner.py` `v6`：`corridor_risk`、`preferred_zone`、`semantic_cost`）。但前端观测面与只读状态投影仍停留在升级前的纯几何阶段，导致开发者与现场操作员存在三处感知断层：

1. **外参自主标定缺少实时空间走廊与闭环阶段视图**：在 `calibration/board_session.py` 中，`plan_capture_views`（`v6`）仅在相机内参任务（`collection is not None`）下写入 `capture_feedback`，空间外参任务（`verify_sensor_extrinsics`）的 `navigation_context`（机体位姿、8 扇区激光最近净距、靶板三维坐标）、候选走廊评分（`corridor_risk` / `preferred_zone` / `semantic_cost`）及 `["target", "guards", "capture_zone"]` 场景边仅留在 Agent 步骤回执中，未进入 `/status` → `/api/calibration-status`；标定面板也只有文字列表与相机画面，无法直观看到机器人、靶板、周界净空包络、候选视点风险对比、RGB 九宫格/横向基线进度及闭环阶段。
2. **探索与校园导航工作台仅能渲染三色占据栅格**：`exploration/survey_session.py` 的 `show()` 未暴露与栅格对齐的 `semantic_costmap` 及 `semantic_scene` 关系投影；`console/web/exploration-mission.html`（含全局二维与局部三维斜视图）只能绘制「未知 / 自由 / 占据」三色瓦片，开发者无法切换查看连续语义风险层、通行偏好层、单调安全准入层（区分几何障碍一票否决与语义高危禁入），也无法实时感知语义先验 TTL 是否生效或已过期退回纯几何保底，更无法悬停/点选栅格读取精确代价值。
3. **主观测台环境地图缺少净空代价热力、游标拾取与多源新鲜度总览**：`console/web/index.html` 的 `#monitor` 地图（`map.js`）仅绘制原始占据栅格、激光点与轨迹，无法直观显示障碍膨胀净空热力，也没有鼠标游标处的 `(x, y)` 坐标、占据值、最近障碍净距与所在语义分区实时读数及多传感器新鲜度横条。

本次交付在不改变任何运动控制权、工具授权和既有浏览器只读安全边界的前提下，打通标定与探索会话的只读语义状态投影，并同步升级**标定采集工位**、**自主探索与校园导航观测台**、**主观测台实时监控地图**三大前端界面。本次不引入浏览器端直接控制底盘或传感器参数的新写接口，也不在前端运行点云分割模型。

## 设计与契约

### 扩展点与所有者

| 项目 | 本次设计 |
|---|---|
| 扩展入口 | 复用既有只读状态接口 `GET /status`（经 Console `GET /api/calibration-status` 代理）、`GET /mission`（经 Console `GET /api/exploration-mission` 代理）与 `GET /api/state`，以及前端既有静态模块加载路径，不新增后台控制端点或平行插件框架 |
| Definition | （1）`calibration/board_session.py` 在 `capture_feedback` 中附加有界 `view_plan` 摘要（含 `planner_version`、`outcome`、`stamp_ns`、`navigation_context`、`view_options` 前 6 项、`rejected`、`captured_lateral_left_m/right_m`、`captured_rgb_cells`）与 `scene_slice`（`compact_scene` v1），单次 `/status` 响应严守 `< 64 KiB` 上限；（2）`exploration/survey_session.py` 在 `show().mission` 中输出栅格对齐的有界 `semantic_costmap`（经 `validate_semantic_layers` 校验的 `{risk, traversable, preferred, stale, archived, source_ref, stamp_ns, valid_until_ns, risk_block, min_traversable, summary}`，网格上限 `≤ 65536` 格）与 `semantic_scene`（经 `compact_scene` 校验，`≤ 12` 节点、`≤ 16` 边，含 `guards` / `attached_to` / `inside` / `approach_to`），响应严守 `< 2 MiB` 上限；（3）前端新增纯客户端只读图层与拾取器 |
| Provider | `calibration/board_session.py`（标定会话状态提供者）；`exploration/survey_session.py`（探索与校园导航会话状态提供者，`scene/campus_large/navigation/tools/navigate-survey-place.json` 与 `survey-area.json` 行为兼容）；`console/web/` 静态前端资源 |
| Consumer | `console/web/calibration-map.js`、`console/web/calibration-motion.js`、`console/web/calibration.js`（标定实时走廊地图、阶段步进条、RGB 九宫格与基线仪表、场景关系条）；`console/web/exploration-local.js`、`console/web/exploration-mission.js`（二维/三维多层语义地图切换、TTL 状态徽章、场景关系卡片、栅格拾取器）；`console/web/map.js`、`console/web/app.js`（主监控净空代价热力层、游标拾取栏、多源新鲜度条） |
| 状态所有者 | 物理运动执行与停稳见证仍归 `edge_core` / `edge_gateway` 与 `view-controller` / `view-verifier`；标定样本与外参修订归 `calibration-session`；探索地图与路线归 `exploration` 会话；Console 服务端与浏览器前端保持纯只读观察者（除既有自然语言任务提交、几何尺寸登记与人工复核按钮外不发起任何写请求） |
| 上下文与证据 | 前端所有地图图层、候选视点与场景边均绑定源 `task_id`、`operation_id`、`stamp_ns`、`source_ref`（64 位十六进制摘要）及 `map_ref.revision`；历史任务回看时优先融合 `/api/task-context` 已归档步骤回执中的 `view_options`、`scene_slice` 与到位见证 |
| 宿主变更 | `agent/`、`src/edge_core`、`src/edge_gateway` 与 `edge_contracts/` 零改动；`console/server/` 代理契约保持不变，完全复用 `edge_contracts` 纯标准库校验函数，严守 `tests/test_package_boundaries.py` 包边界 |

新概念说明：前端 **单调安全准入视图（`admitted` 图层模式）** 最接近现有占据栅格 `occupancy` 视图，区别在于它将几何占据（`cells > 49`）与语义通道（`risk`、`traversable`、`preferred`）的合成判定显式分为四类可观察状态：「几何障碍否决」「语义高危禁入」「先验过期降级放行」与「可通行（优选/普通）」，使开发者一眼核实视觉语义绝未抹除激光几何障碍。

### 生命周期与旧行为

| 场景 | 新请求 | 已受理/在途工作 | 证据、资源与责任方 |
|---|---|---|---|
| 安装 / 首次发现 | 静态前端与会话投影随镜像或 `deploy/robot` 同步加载；无语义层字段时默认渲染原占据栅格 | 在途标定或导航任务无感衔接，自动点亮对应阶段与走廊图层 | 无新增端口、守护线程或持久化账本格式破坏 |
| 依赖缺失 / 语义过期（TTL 超时） | 若 `semantic_costmap` 缺失则图层按钮自动降级或提示仅几何观测；若 `now_ns > valid_until_ns`（`stale: true`），界面醒目显示「先验已过期 · 纯几何保底」，`admitted` 图层解除语义软禁区、仅保留几何障碍否决 | 在途运动仍由底层激光净空独立守护，前端忠实反映降级状态 | `source_ref`、`stamp_ns` 与 `valid_until_ns` 原样展示供排障核对 |
| draining / 卸载 | 标定工位或探索服务离线时，`#cal-station` 隐藏、`#connection` 显示中断，不残留过期实时帧 | 浏览器轮询保持单飞与超时取消，不堆积挂起连接 | 页面无未捕获异常，不发起写请求 |
| 换版 / 旧 binding | 旧归档任务（无 `last_view_plan` 或无显式 `semantic_layers`）通过步骤回执回填或从冻结地图几何确定性派生只读预览，标注归档来源 | 切换 `task_id` 或 `map_ref.revision` 时立即重置相机视角、拾取读数与缓存瓦片，防止跨任务串图 | 缓存键绑定 `(grid, semantic_costmap.source_ref, stale, layerMode)` |
| 超时 / 崩溃 / 重启 | 会话重启后 `last_view_plan` 随 `sessions.json` 恢复或在下一次 `plan_capture_views` 更新 | 历史回放（`replay` 模式）在主监控地图中准确显示快照时刻状态与过期标识 | 保持桌面 `1440px` 与移动端 `390px` 零横向溢出 |

## 验收与证据

| AC | 输入 / 故障 | 可独立观察的预期 | 测试入口 | 实测结果 / 证据 |
|---|---|---|---|---|
| AC-1 | 空间外参及内参主动标定任务执行 `inspect_calibration_view`、`plan_capture_views`（`v6`）、`move_to_pose` 与 `record_board_sample` | `Station.status()` 在 `< 64 KiB` 预算内返回最新 `view_plan` 与 `scene_slice`；前端 `#cal-live-workbench` 实时渲染 6 阶段闭环步进条、8 扇区周界净空与靶板法向走廊图（按 `corridor_risk` / `preferred_zone` 着色候选视点并支持悬停/点击检视）、RGB 九宫格与左右横向基线进度条，以及 `guards` / `observed_by` 场景关系条 | `tests/test_semantic_map_workbench.py`、`tests/map_semantic_workbench.test.mjs`、`console/server/test_calibration_station.py` | 通过：容器内 `test_semantic_map_workbench.py` 3/3、`test_calibration_station.py` 5/5；无头浏览器实测渲染 [标定与主监控截图](../../artifacts/verification/sdd063-semantic-map-ui-20261127/console-calibration-and-monitor.png) |
| AC-2 | 探索与校园导航会话含区域几何（室内/过渡区/室外）、门牌地标、待复核避让区及新鲜/过期 `semantic_layers` | `Survey.show()` 返回经 `validate_semantic_layers` 与 `compact_scene` 校验的 `semantic_costmap` 和 `semantic_scene`；前端支持在全局二维与局部三维视图间切换 `占据几何 / 语义风险 / 通行偏好 / 单调准入` 四种地图模式，默认 `占据几何` 像素与旧版完全一致；几何障碍在所有模式下均被单调否决；TTL 过期时显示降级状态并解除语义软禁区；悬停/点击栅格实时显示 `(x, y)`、三通道值与条件代价倍率 | `tests/test_semantic_map_workbench.py`、`tests/map_semantic_workbench.test.mjs`、`tests/test_survey_providers.py` | 通过：4 种图层哈希在 3D/2D 下两两独立，TTL 超时自动解除语义软禁区并切换徽章 [探索单调准入截图](../../artifacts/verification/sdd063-semantic-map-ui-20261127/exploration-semantic-admitted.png) · [浏览器核验记录](../../artifacts/verification/sdd063-semantic-map-ui-20261127/browser-verification.json) |
| AC-3 | 主观测台 `#monitor` 接收实时或回放 `/api/state`（含占据栅格、位姿、激光、里程计、IMU 与语义分区） | 勾选「安全净空热力层」可在环境地图叠加障碍距离场热力；移动指针或点击地图在 `#map-inspect` 实时读出世界坐标、栅格坐标、占据值、最近障碍净距与所在语义区；多源新鲜度横条实时展示各传感器在线/延迟/回放状态 | `tests/map_semantic_workbench.test.mjs` | 通过：Node 契约与无头浏览器实测 `clearance` 热力层像素生效、游标拾取与 6 源新鲜度药丸正常 [浏览器核验记录](../../artifacts/verification/sdd063-semantic-map-ui-20261127/browser-verification.json) |
| AC-4 | 既有标定工作台契约、只读浏览模式、历史任务回放、`390px` 窄屏布局、`deploy/robot` 投影及包依赖检查 | `#cal-motion` 零交互控件、`#cal-station` 与主页面在 `390px` / `1440px` 无横向溢出、只读浏览零 `POST` 请求、`test_package_boundaries.py` 与 `test_selection.py` 全绿、`release_robot.py` 完整收录新增前端脚本 | `tests/test_selection.py`、`tests/test_package_boundaries.py`、`tests/test_robot_release.py`、`tests/system/calibration_motion_guidance.cjs` | 通过：宿主机套件运行号 `20261127T034052Z-6eafd802`（`repository` + `planner` 全绿），`390px` 窄屏零溢出且零写请求 |

## 迁移与回退

1. **部署与静态资源同步**：`calibration/board_session.py`、`exploration/survey_session.py` 与 `console/web/` 下新增/更新的 `.html`、`.js`、`.css` 均位于 `scripts/release_robot.py` 既有运行时投影目录白名单内。合入 `main` 后执行 `python scripts/release_robot.py HEAD` 即可原子更新 `deploy/robot`。
2. **旧数据兼容与回退**：旧版 `sessions.json` 或探索任务存档不含 `last_view_plan` / `semantic_layers` 时，后端与前端自动走空值安全分支并回退至原有几何栅格与文字读数展示。如需完整回退，执行 `git revert <合并提交>` 并重新运行 `python scripts/release_robot.py HEAD`，无需迁移磁盘状态文件。

## 变更记录

2026-11-27 完成 `SDD-063`（AC-1 至 AC-4）全部前后端实现与验收：（1）`calibration/board_session.py` 为空间外参与内参标定统一持久化有界 `view_plan` 与 `scene_slice` 只读投影（`< 64 KiB`）；`exploration/survey_session.py` 输出经 `validate_semantic_layers` 与 `compact_scene` 校验的 `semantic_costmap` 和 `semantic_scene`（含 `guards` / `attached_to` / `inside` / `approach_to` 关系与实时 TTL 状态）；（2）升级 `console/web/calibration-map.js`、`calibration-motion.js`、`calibration.js`、`exploration-local.js`、`exploration-mission.js`、`map.js`、`app.js` 与配套 HTML/CSS，交付标定闭环阶段步进条与空间走廊图、探索二维/三维四模式语义地图与栅格拾取器、主监控安全净空热力层与多源新鲜度横条；（3）通过宿主机 `tests/system_suite.py --stages repository planner`（运行号 `20261127T034052Z-6eafd802`）、`node --test tests/map_planar.test.mjs tests/map_spatial.test.mjs tests/map_semantic_workbench.test.mjs`（11/11）、标定与观测台容器内单测（`test_semantic_map_workbench.py` 3/3、`test_capture_views.py` 10/10、`test_calibration_station.py` 5/5）及无头 Chromium/Edge 浏览器桌面（`1440px`）与窄屏（`390px`）实测。物理实车室外开放词汇分割叠加仍待后续实机阶段联调。[浏览器核验记录](../../artifacts/verification/sdd063-semantic-map-ui-20261127/browser-verification.json) · [标定与主监控实测画面](../../artifacts/verification/sdd063-semantic-map-ui-20261127/console-calibration-and-monitor.png) · [探索语义准入实测画面](../../artifacts/verification/sdd063-semantic-map-ui-20261127/exploration-semantic-admitted.png)

2026-11-27 基于 `179f07c45d9f6b1467e1682dc15f4c6f39137df8` 立项登记 `SDD-063`（AC-1 至 AC-4），同步打通 `SDD-060`/`SDD-061` 后端语义场景子图、多通道语义代价地图、单调安全走廊与主动标定视点评分的只读状态投影及三大前端工作台实时可视化。
