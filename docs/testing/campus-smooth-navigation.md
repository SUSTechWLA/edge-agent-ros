# 校园教学楼的局部规划与轨迹跟踪

在完整的 `campus_large` 世界中，下发「前往教学楼门外北侧等候点」。起点为门厅，门洞位于世界坐标 x = 1.60 米。等候点为教学楼门外区的仿真场地标注 (3.30, 0.85) 米。三种工况使用相同的校园建筑、机器人碰撞体和场地地图。场地导入工具记录建筑布局、世界文件摘要和等候点来源。

校园地点导航启用 `continuous_path`。只读路线服务在冻结地图与当前占用栅格上求路，每个参考弦段均受站点步长和通行区域限制。会话在派发前独立检验整条路径、起点、终点和坐标系，通过 Core 的 `/edge/submit_path` 提交一个可对账操作。Gateway 根据实测里程更新沿程前视点，将实际输出弧线和制动扫掠交给本机安全检查。中途经过参考点继续行驶，终点由 Core 核验停稳。探索中的独立观察点保留逐次到点核验。遇到行人时使用动态让行与通道复查，保留原任务与地图身份。

扫掠净距阈值为 0.55 米，TTC 制动阈值为 0.8 秒。移动目标预测覆盖未来 2.5 秒。Agent 在原动作停稳后以同一阈值检查预测轨迹到车体扫掠矩形的欧氏距离。已有动态保持的路段以激光过期等原因终止时，任务会话保留原复查截止时间，取得新鲜净空证据后才能派发下一段。独立世界真值另外记录车体到行人的最小实测距离和对应车速，包括行人在小车停车后继续接近的时段。

## 日用校园站

日常只需打开 8910 任务台。「全局地图」显示完整校园正射影像，地标与北向标识随视图显示。「校园三维」使用保存于同一世界卷的原三维模型。「聚焦任务」把几米长的教学楼路线放大到可读尺寸。「局部三维」显示车体附近的深度、激光与轨迹。已建场景、当前任务栅格和车体观测各有独立来源。全校范围内的运动许可仍取决于导航地图及站点配置。[进入校园任务台](http://127.0.0.1:8910/exploration-mission.html)

| Docker 服务 | 常驻职责 |
|---|---|
| `sim` | 加载原校园世界并运行 Gazebo 物理仿真 |
| `bridge` | 在 Gazebo 和 ROS 之间传递传感器、时钟及仿真底盘指令 |
| `perception` | 运行感知节点、RGB-D 桥及独立于控制桥的世界真值桥 |
| `gateway` | 核验局部净距、制动条件，接管仿真底盘输出 |
| `core` | 管理运动操作与停稳回执 |
| `agent` | 接收自然语言任务、调用注册工具并记录结果 |
| `console` | 在 8910 提供地图、实况和任务回看 |
| `survey-session` | 绑定导航任务的地图、定位、规划与执行进度 |
| `survey-readonly` | 为地点解析、路线、前沿和通道复核提供只读计算，保留各自的插件端点 |
| `survey-maps` | 读取和发布有版本的导航地图，单独持有地图写入卷 |

运行 `./scripts/campus-core.ps1 ps` 查看以上十项，`./scripts/campus-core.ps1 up` 启动已建站点。默认复用 `campus-smooth-phase1-r08` 的原任务卷、地图卷与 `edge-campus-teaching-1124_campus-world` 场景卷。要操作另一个已建站点，可设置 `EDGE_CAMPUS_RUN`。[日用入口](../../scripts/campus-core.ps1) · [精简部署配置](../../compose.campus-core.yaml)

日用站扩展至校园前广场南侧露台。场地导入器从同一份校园世界、步道路网和地点标注生成版本化地图 `campus-forecourt-walk`。路线插件在已发布地图上作全局规划，用当前激光占用数据逐段核验；运行时保存首份全程规划供回看。Core 与 Gateway 在各自的入口检查校园场地范围。其他部署默认仍用原点四周 4 米的范围。[场地与地点标注](../../scene/campus_large/navigation/forecourt.json) · [校园运动范围](../../scene/campus_large/navigation/forecourt-site.yaml)

首次在本机配置这张地图时，从仓库根目录构建带场地范围的镜像，使用日用站的 Compose 配置导入地图，再启动十项服务。地图导入应在站点首次启动或空闲停机维护时执行。

```powershell
./scripts/campus-native-image.ps1 site
$env:EDGE_CAMPUS_RUN = 'r08'
./scripts/campus-navigation.ps1 phase1 -f compose.campus-core.yaml run --rm --no-deps campus-seed
./scripts/campus-core.ps1 up
```

## 启动与观测

在 Windows PowerShell 7 中运行 [校园启动脚本](../../scripts/campus-navigation.ps1)。脚本为 `phase1`、`doorway`、`crossing` 和 `blocked` 选择独立的 Compose 项目、DDS 域、Gazebo 分区和本机端口。通过 `EDGE_CAMPUS_BASE_VOLUME` 指定由 [大型校园构建器](../../scene/campus_large/README.md) 生成的世界卷，默认使用 `edge-campus-teaching-1124_campus-world`。Core 与 Gateway 共同使用 `EDGE_CAMPUS_GATEWAY_IMAGE` 指定的镜像，默认标签为 `edge-rover-local:sdd058-campus-continuous-r3`。

原生镜像使用本机 Git 分支 `archive/rover-local-before-consolidation-20261125` 保存的 SDD-056 源码。构建脚本把该分支解析为提交 SHA，用 `git archive` 将三个 ROS 包和当前配方直接送给 Docker，并把 SHA 写入镜像标签。构建脚本需要 PowerShell 7.4 或更高版本来传输二进制 tar。基础镜像为 `edge-rover-local:sdd057-container`。在另一台机器上复现，需要先取得该本地源码分支及基础镜像。构建期间会运行 Gateway 和 Core 的 CTest。独有的历史运行材料保存在本机仓库的 `.local-archives/rover-local`。[原生构建入口](../../scripts/campus-native-image.ps1) · [构建配方](../../docker/Dockerfile.campus-continuous)

```powershell
./scripts/campus-native-image.ps1 continuous
docker build -f docker/Dockerfile.campus-lidar -t edge-rover-local:sdd058-campus-lidar-r1 docker
./scripts/campus-navigation.ps1 phase1 config --quiet
./scripts/campus-navigation.ps1 phase1 run --rm --no-deps campus-seed
./scripts/campus-navigation.ps1 phase1 up --detach sim bridge campus-camera-bridge witness core gateway perception agent console survey-route survey-maps survey-semantics survey-corridor survey-session
./scripts/campus-navigation.ps1 phase1 ps
```

校园仿真器使用 `EDGE_CAMPUS_SIM_IMAGE` 选择 GPU 激光渲染镜像，默认值为 `edge-rover-local:sdd058-campus-lidar-r1`。该镜像在原校园 Gazebo 镜像上应用 Ogre2 第二遍采样的边界修复。独立标靶校验另在校园世界的屋顶上方架设一份同型激光与五个静止标靶，直接从 Gazebo Transport 核对原始测距。导航验收运行原校园世界。[构建配方](../../docker/Dockerfile.campus-lidar) · [标靶校验程序](../../tests/system/campus_lidar_calibration.py) · [上游修复](https://github.com/gazebosim/gz-rendering/pull/1319)

单独复测激光时，可在仓库根目录运行以下命令。独立 Gazebo 分区隔离标靶传感器与导航任务。原镜像在 −45° 把 3.20 米靶面读为 1.10 米，在 +45° 把 1.10 米读为 4.30 米。修复后五帧的 75 项检查全部通过，包括 0.90 米处仅占一束的细障碍。

```powershell
$results = (New-Item -Force -ItemType Directory .ci-results/campus-lidar).FullName
./scripts/campus-navigation.ps1 phase1 run --rm --no-deps -e GZ_PARTITION=campus_lidar_calbench -v "${results}:/results" sim python3 -B /fixture/campus_lidar_calibration.py --out /results/measurements.json
```

专项复测按需选择 `doorway`、`crossing` 或 `blocked`。原三工况已归档，对应实例启动后分别使用 8909、8907、8908。Compose 服务 `campus-seed` 导入校园地图。验收时先启动 Gazebo 独立见证，待成对真值样本就绪后从浏览器提交任务。见证只驱动测试替身，小车运动经任务运行时派发。[Gazebo 独立见证](../../tests/system/campus_dynamic_check.py) · [三工况历史实录](../../artifacts/verification/campus-continuous-20261124/campus-navigation.mp4)

在已有任务存档上复测地图交互时，直接运行浏览器用例。将 `CHROME_PATH` 设为本地 Chrome 可执行文件路径，可通过 `--puppeteer` 指定已有的 `puppeteer-core` 模块绝对路径。用例只读取任务页和地图，按画布内小车标记的实际像素位移验证操作。

```powershell
node tests/system/campus_map_camera.cjs --url "http://127.0.0.1:8909/exploration-mission.html?task_id=campus-dynamic-doorway-08" --browser "$env:CHROME_PATH"
```

## 导航架构与验证

### 第一阶段的室内外语义导航

上面的命令在 `phase1` 独立项目中导入地图并启动导航。端口为 8910，ROS 域为 191。通过 `EDGE_CAMPUS_RUN` 可给独立仿真世代指定 Compose 项目后缀，旧校园站点的地图和任务卷保留原修订。同一端口和域同时只启动一站。导入工具在写入前会检查已有地图摘要。[校园任务台](http://127.0.0.1:8910/exploration-mission.html)

| 在任务台输入 | 地图如何解释 |
|---|---|
| 前往门外北侧等候点 | 别名对应室外的「教学楼门外北侧等候点」，导航目标是场地登记的可停靠位置。 |
| 前往一楼门厅 | 别名对应室内的「教学楼门厅」，停靠位置来自教学楼布局中的门厅节点。 |
| 前往等候点 | 室内、室外各有一处匹配。展示两处候选，请用户补充区域或完整名称。 |
| 前往不存在的气象站 | 本版地图没有匹配地点，任务以可查的原因结束。 |

正式轮次 `campus-phase1-08` 的四条前端任务均通过独立验收。去程和返程各派发一个连续运动操作，均经过教学楼门洞，最终线速度和角速度为零。整轮沿用地图修订 `23d8fa62c686`、0.55 米车体扫掠净距和 0.8 秒 TTC 阈值。两条未确定目的地的指令均为零运动操作、零非零速度指令、零位移。

| Gazebo 世界测量 | 出门至北侧等候点 | 返回一楼门厅 |
|---|---:|---:|
| 实际行驶距离 | 3.611 m | 3.533 m |
| 到点误差 | 4.95 cm | 5.32 cm |
| 最近门框净距 | 0.767 m | 0.802 m |
| 最近行人净距 | 0.901 m | 0.914 m |
| 物理接触 | 0 | 0 |

该轮原速验收录像保留了四个可跳转的任务章节。[95 秒原速录像](../../artifacts/verification/campus-phase1-20261124/campus-navigation.mp4) · [双向实测轨迹](../../artifacts/verification/campus-phase1-20261124/round-trip.png) · [任务与前端交互记录](../../artifacts/verification/campus-phase1-20261124/verification.json) · [仿真位姿、ROS 包和激光标靶对照](../../artifacts/verification/campus-phase1-20261124/gazebo-evidence.zip)

Agent 从已注册的地点工具取得语言参数。只读 DSH 服务 `map.semantic_resolve` 按当前地图的名称、别名和可选环境限定选出实体。会话核对返回的地图摘要、已确认实体、停靠位置、机体净距及现场坐标系，再向规划服务请求路径。观察台把用户的目的地和小车当前位置分别显示。车体经过门廊时，位置标签来自同一版校园地图的区域边界和实际里程计。[站点标注](../../scene/campus_large/teaching-navigation.json) · [解析服务](../../exploration/survey_semantic.py) · [任务会话](../../exploration/survey_session.py)

前端验收逐字输入每条指令，记录提交回执、视图切换和任务终态。录像使用浏览器实拍帧与墙钟时间；时间索引可以定位到实际提交时刻。运动验收另用 Gazebo 世界位姿、接触传感器和速度指令核对。歧义和未知地点都要同时满足零运动操作、零非零速度指令和零位移。对两个有效地点，车体必须实际穿过门洞、满足净距并在目标处停稳。[前端验收](../../tests/system/campus_semantic_browser.cjs) · [独立世界见证](../../tests/system/campus_semantic_witness.py)

上实机时，可以沿用地点 ID、别名、入口和来源。应先把实测语义锚点登记到现场地图，核对双 MID-360、IMU 与 D435 的时间和外参，再用录包验证出入门时地图定位的连续性。如果接入室外 GNSS/RTK，应验证其到地图坐标的变换、定位质量下降和重新定位后的代次。当前执行适配器限定在隔离 Gazebo，使用同一 `odom` 原点和已发布的校园底图。精确朝向停靠、真实门牌识别和实机制动均需独立验收。

复测完整往返时，小车应停在门厅，独立见证与浏览器共享一个新的证据目录。下面的脚本只在本机 8910 的校园 Gazebo 下发任务。`CHROME_PATH` 和 `FFMPEG_PATH` 分别指向已安装的可执行文件，浏览器脚本可用 `--puppeteer` 指定已安装的 `puppeteer-core` 模块路径。截图在任务期间采集，视频在任务结束后编码。

```powershell
$trial = 'test-' + (Get-Date -Format 'MMddHHmmss')
$capture = New-Item -ItemType Directory -Path ".ci-results/campus-semantic-$trial"
$env:EDGE_SDD058_WITNESS_DIR = $capture.FullName
./scripts/campus-navigation.ps1 phase1 run --detach --rm --no-deps --entrypoint /usr/local/bin/edge-entrypoint campus-check python3 -B /fixture/campus_semantic_witness.py --out /evidence
node tests/system/campus_semantic_browser.cjs --trial $trial --out $capture.FullName --browser $env:CHROME_PATH --ffmpeg $env:FFMPEG_PATH
```

### 一句话如何变成实际运动

| 责任 | 本轮输入和输出 | 源码 |
|---|---|---|
| Agent 与导航会话 | “前往教学楼门外北侧等候点”匹配站点登记的地点导航工具。会话解析已确认的语义入口，冻结地图引用和软件服务绑定，跟踪一个外层任务号。 | [站点工具](../../scene/campus_large/navigation/tools) · [任务会话](../../exploration/survey_session.py) |
| 地图与定位 | 此轮以校园场景几何生成底图，等候点由场地标注提供。实时占用和里程计参与导航。RGB-D 测量、平面扫描与底图结构分别显示，Gazebo 世界位姿单独用于验收。 | [底图与标注来源](../../tests/system/campus_dynamic_map.py) · [观测适配器](../../exploration/survey_ros.py) |
| 全局规划 | 只读服务在发布与实时栅格同时可通行的区域内搜索 A* 路线。返回含起终点的途经点序列，派发前会话再次检查完整路径。 | [路线服务](../../exploration/survey_route.py) · [穿越单元格检查](../../navigation/grid_corridor.py) |
| Core 与 Gateway | Core 记录路径和操作身份，Gateway 根据里程计逐周期前视跟踪，沿可执行弧线检查车身扫掠、制动和动态冲突，遇到风险可以就地停车。终点需要位姿和停稳回执。 | [连续路径构建入口](../../docker/Dockerfile.campus-continuous) |
| 恢复与证明 | 原生让行期间核对冻结任务和地图。原动作若已结束，会话先核对停稳凭证，再要求连续新鲜观测，重新规划后才可派发。录制从真实任务页取帧，独立验收从 Gazebo 采世界位姿、车速、行人位置与接触。 | [通道复查](../../exploration/corridor_review.py) · [独立真值验收](../../tests/system/campus_dynamic_acceptance.py) |

**SLAM、VLN 和端到端覆盖不同问题。** SLAM 估计“我在哪里、周围是什么样”，典型产物是坐标一致的地图和位姿，可作为上表第二行的输入。VLN 以当前视觉观察解释语言，如“穿过玻璃门后在蓝色标牌右侧等候”，可将地标与子目标供给规划器。端到端描述训练方式，策略可直接从图像、历史观察和目标预测未来几个动作或路点。一个 VLN 系统也可以使用 SLAM、拓扑地图或端到端策略。[ORB-SLAM3](https://github.com/UZ-SLAMLab/ORB_SLAM3) · [VLN 原始论文](https://arxiv.org/abs/1711.07280) · [ViNT 的策略及拓扑搜索](https://general-navigation-models.github.io/vint/)

扩展时优先稳定三个接口。定位与建图提供带坐标系、时戳、标定版本和质量的地图/位姿；地点理解提供有观测来源、置信度和可行入口的语义目标；局部策略提供带有效期和机体约束的候选轨迹。轮式、飞行和机械臂分别实现本机运动准入与停止契约。下一阶段可先对双雷达、IMU 与 D435 的录包验证配准、闭环和重定位，再用同一批录包影子运行 VLN 或学习型局部策略，比较到点率、最小净空、误停和最差延迟。这样能分别定位“地点理解错”“地图漂移”和“本机轨迹不可行”。

### 从独立世界位姿计分

校园任务台默认展开当前 Gazebo 世界卷中的全校纹理底图，范围为 565 × 1190 m。横向窗口中北向朝右，纵向窗口中北向朝上。点「校园三维」可以旋转同版校园原始建筑模型，包括体育场、教学楼与北侧楼群。「聚焦任务」将全局视图移至本次任务区，显示发布的任务栅格、规划参考线和实测轨迹。任务覆盖率只统计该区栅格。任务地图与底图的位姿坐标系、冻结版本及来源校验一致时才叠加。

「局部三维」跟随机器人显示现场传感器观测。可在任务链接附加 `&view=local` 直接进入。局部与全局平面地图均支持左键或单指拖动平移，滚轮或双指缩放。观察墙角或障碍物时，可用右键或 Shift 加左键拖动旋转局部视角，触屏可先点「旋转模式」。调整后的视角会在任务刷新和视图切换时保留，点「复位视角」恢复局部跟车或全局总览。键盘方向键平移，Shift 加方向键旋转，加减键缩放，Home 复位。

墙体取自当前地图版本绑定的校园布局。RGB-D 深度像素与同时间戳的相机内参配对，按仿真安装位姿转换到 `odom`。视图对 7 米内深度均匀采样，按测量高度着色。橙色点来自单层激光的扫描截面。两种观测各自使用 60 毫秒内的里程计配准，并只纳入采集年龄在 650 毫秒内的样本。面板显示当前视野里的测点数与深度点高度范围。任务存档保留其自身的末帧观测。

绿色参考线与青色实测轨迹可同时在局部和全局地图查看。复查和终态使用带“历史参考”标记的上次派发路线。图下方的速度曲线来自当前任务最近 15 秒的里程计，横轴使用仿真时间，采样中断处保留间隙。独立见证程序另行读取 Gazebo 世界位姿与速度，用于验收。

[独立计分器](../../tests/system/campus_trajectory_truth.py)读取带 Gazebo 时间戳的世界位姿，按 6 厘米等距离重采样相邻 XY 点。分别分析连续移动区间，记录行驶距离、航迹方向变化和低速车身转角。一段可验证的移动转弯至少有三个同向角度增量，总转角至少 12°，最大单次增量小于总角度的 65%。停车、采样中断及原地转向不能拼接成移动转弯。独立计分与动态场景的接触、停车和恢复检查分别运行。

## 三种工况的连续跟踪验收

轮次 `08` 共通过 58 项验收。三站使用同一校园地图、机体轮廓和镜像 `sha256:1151679f6081c204575696e7cba60cff9f72153805c7d57a2edab6a49bd5b8d5`，均为零接触。初版会将可见墙面的边缘变化当成横向移动。另一类误停来自门框两侧轮流成为最近的激光表面。修复后本机区分可重复观测的静止表面及新露出的目标边缘；最近排名改变时，只有连续两帧中多束实测表面的方位覆盖和量距相容才继续估计 TTC。

| 场景 | 实测路线、速度与结果 |
|---|---|
| 门边站人 | 1 个动作行驶 3.582 m。行驶段 135 个采样跨 13.668 仿真秒，实测线速度 0.055–0.280 m/s，中途停车 0 次。连续 1.98 m 弯道累计转向 56.1°。最近行人净距 0.840 m，最近门框净距 0.755 m，到达误差 5.01 cm。 |
| 连续横穿 | 两人同时侵入通道的 31 个采样中车体全部静止。最近时行人距已停稳车体 0.418 m。Agent 核验净空，保持原外层任务、地图和 Gateway 实例重新派发并到达，误差 5.29 cm。 |
| 持续堵塞 | 阻挡物就位后持续采集 243 帧，跨 24.684 仿真秒、36.563 墙钟秒。最大实测线速度、角速度、指令和位移均为零，返回 `BLOCKED`。 |

[完整前端录像，含三个场景章节](../../artifacts/verification/campus-continuous-20261124/campus-navigation.mp4) · [实测路径、速度与净距](../../artifacts/verification/campus-continuous-20261124/world-trajectory-speed.png) · [行驶中的局部三维视图](../../artifacts/verification/campus-continuous-20261124/local-3d.png) · [验收、位姿、原始轨迹及传感器文件索引](../../artifacts/verification/campus-continuous-20261124/verification.json)

## 历史对照，逐段停靠的版本

正式轮次 `05` 使用 Gateway 镜像 `sha256:03d8ae30ef6457b472150dbe2faa04a64638027e398e13ef28bb1d17d4d073d6`，两站点地图摘要均为 `7213be4161685bde93928908c130da3624f539db55546e96d7d92701367295f5`。横穿任务共行驶 3.622 米，最大连续实测转弯在 0.36 米内改变航迹方向 24.56°，终点误差 4.56 厘米。两名行人同时占用通道的 31 次观测中车体均停稳。持续堵塞任务在阻挡物就位后持续观测 24.276 秒仿真时间，墙钟时间 37.738 秒，最大速度指令、实测速度及漂移均为零。两场均无接触。

横穿场景中车体实际轮廓与最近行人的最小距离为 0.417 米，发生时车速和指令均为零。车速超过 0.04 米/秒时记录的最小距离为 0.818 米。在额外的诊断轮次 `04` 中，见证程序在地图绑定前退出，两个替身停在初始位置，Gateway 在门口保守停车。该轮使用旧版 v4 镜像。

[世界坐标与速度图](../../artifacts/verification/campus-smooth-20261124/world-trajectory.png) · [轨迹计分](../../artifacts/verification/campus-smooth-20261124/trajectory-verification.json) · [逐次停车与放行验收](../../artifacts/verification/campus-smooth-20261124/acceptance.json) · [原生执行器证据](../../artifacts/verification/campus-smooth-20261124/native-verification.json) · [横穿实录](../../artifacts/verification/campus-smooth-20261124/dynamic-crossing.mp4) · [堵塞实录](../../artifacts/verification/campus-smooth-20261124/dynamic-blocked.mp4)
