# 大型校园 Gazebo 实验场

从教学楼门厅把车开到门外，再切到校园总览，沿步道找到运动场入口、林带、广场和 Mekelweg 实测人行道。地图提供差速车、四旋翼、四足与人形机器人的近景，可以对照任务和物理测量辨认每个动作。[四种机体的操作与验收](../../docs/testing/campus-robot-view.md)

## 1　首次准备与启动

在仓库根目录运行。需要 Docker Compose、宿主 Python 3.11 或更新版本，以及本项目已有的 Gazebo Harmonic 仿真镜像。模型推理服务可以保持关闭。

```sh
python scene/campus_large/fetch_assets.py
python scene/campus_large/run.py start --image edge-agent-ros:field-ready
python scene/campus_large/run.py map --image edge-agent-ros:field-ready --with-lidar
python scene/campus_large/run.py sensors --image edge-agent-ros:field-ready
python scene/campus_large/run.py sensor-health --image edge-agent-ros:field-ready
python scene/campus_large/run.py review --image edge-agent-ros:field-ready
python scene/campus_large/run.py status
```

校园网页默认使用 [8780 总览](http://127.0.0.1:8780/)，机器人工作台使用 [8766 工作台](http://127.0.0.1:8766/)。两者绑定在本机回环地址。首次启动后图像会陆续出现。`campus-build` 在生成成功后退出，`campus-capture` 在所有已登记视角取得图像后退出，这两个服务显示 `Exited (0)` 是预期结果。ROS 传感器持续更新可以在工作台确认。

`edge-agent-ros:field-ready` 是本次在开发机上保留的校园镜像标签。新机器准备好项目基础镜像后，在仓库根目录运行 `docker build -f docker/Dockerfile -t edge-agent-ros:field-ready .` 即可得到同名镜像。后续 `start`、`map` 或 `reload` 均可传 `--image` 指定标签，或在 PowerShell 先设置 `$env:EDGE_CAMPUS_IMAGE='edge-agent-ros:field-ready'`。端口冲突时可加 `--console-port 8866 --overview-port 8880`。[项目部署指南](../../docs/deployment.md)

### 输入资产与离线缓存

| 输入 | 标识与作者 | 在世界中的用途 |
|---|---|---|
| 代尔夫特理工大学体育园与周边街区 | 3DBAG LoD2.2、PDOK 2025 正射影像及 Kadaster BGT 道路多边形，随项目离线分发 | 米制建筑轮廓和屋顶与地面航片配准。训练设施在同一场地内布局，垂直立面窗格采用示意材质。[场地来源与重建方法](real_site_data/README.md) |
| 差速车与机器人实验广场 | 原项目的 Edge Rover 及收录的 Crazyflie、Go1、G1 模型 | 对照原生 Gazebo 机体、位姿和动作。各资产的授权、来源提交和离线校验见[机器人资产清单](robots/assets.json) |
| Gazebo Fuel Pine Tree v6 | OpenRobotics / Louise Poubel，CC0-1.0，下载 877,470 字节 | 林区中的松树模型。[原始模型](https://app.gazebosim.org/OpenRobotics/fuel/models/Pine%20Tree) |
| 项目训练区 | 本仓库的米制场景生成器 | 门厅、教室、门廊与门口、田径场、林道、车道、过街线和草坪。[生成器](build.py) |

下载器按锁文件校验 ZIP 和资源文件，缓存默认在 `artifacts/sim-assets/`。离线迁移时复制整个缓存，并在目标机上验证。将缓存放在其他磁盘时，两条命令使用同一个目录。[锁定版本、来源与 SHA-256](asset-lock.json)

```sh
python scene/campus_large/fetch_assets.py --offline --root /path/to/sim-assets
python scene/campus_large/run.py start --assets-dir /path/to/sim-assets
```

Windows 也可直接传入带引号的绝对路径，例如 `"D:\robot-data\sim-assets"`。世界生成容器禁用网络，只读挂载离线资产。校园站点、图片与字体均从本地提供。

## 2　分清校园范围、导航许可和观测范围

已启动校园时，在仓库根目录执行 `python scene/campus_large/run.py map --image <与当前提交配套的本地镜像>`，可独立安装或刷新地图。Docker 在本地生成原生三维网格和宽 2048 像素的二维正射图。重新发布世界后，再运行一次 map 使外观与新世界匹配。使用 `--map-port` 可以改用其他回环端口。

先从[校园总览](http://127.0.0.1:8773/?view=3d&place=campus)辨认运动场、树林和教学楼，选择[实测街面](http://127.0.0.1:8773/?view=3d&place=campus_street)观察高层楼群，再切到[教学楼主入口](http://127.0.0.1:8773/?view=3d&place=teaching)、[一层教室](http://127.0.0.1:8773/?view=3d&place=teaching_interior)和[田径场](http://127.0.0.1:8773/?view=3d&place=track)检查局部细节。二维图用于核对道路和场区间距。“机器人附近”定位到当前 ROS 位姿。也可以将三维透视与 Gazebo 的对应固定相机图像并排比较。选择地点只改变相机。[校园二维地图](http://127.0.0.1:8773/?view=2d) · [Gazebo 相机总览](http://127.0.0.1:8780/)

地图可与已有固定激光台站同时运行。给 `run.py map` 加 `--with-lidar` 即可在同一校园网络及 ROS 域启动 `map3d-sensor`。纹理来自仿真资产，局部占据、点云和位姿来自 ROS。两者的来源与范围可以通过图层开关、坐标和拾取读数核查。[设计与验收](../../docs/specs/SDD-027-campus-texture.md)

| 在哪里看 | 读到的内容 | 适用的任务 |
|---|---|---|
| 8780 总览 | 米制场地边界与 Gazebo 固定视角，当前测绘数据覆盖 545 × 1,170 米。 | 选择实验区域、对照教学楼入口和实验广场，比较道路及林区的视线。 |
| 工作台语义场景 | `odom` 原点在教学楼门厅。已登记的导航点是门厅 (0, 0)、门口 (1.7, 0)、门外 (3.3, 0)。 | 在任务框发“前往教学楼门外”。查看路线、运动终态，再对比 `/odom` 与 `/ground_truth`。[具名导航](../plugins/navigate-place.json) |
| 工作台实时感知 | `/scan`、`/imu`、`/odom` 随机器人更新，原有局部栅格在机器人起点附近建图。 | 核对穿门前后位置、激光量测与动作结果。 |

当前任务内核和运动网关仍将每个平面目标限制为 `|x| ≤ 4 m、|y| ≤ 4 m`。一条任务允许 1 至 8 个航点。远处的车道和田径场是可渲染的场地；把这些地点开放为可执行目的地还需验证定位、沿路避障、航点时限和地理围栏。默认工作台以离线规则处理已支持的任务表达，模型的云端凭据与个人规划配置留在原项目。

### 校园任务观测台

在有地图的校园 Console 打开 `/campus-mission.html`，或从环境地图选择「校园任务」。普通任务视图显示自然语言原文、步骤状态、场景地点与候选路线、当前车体状态和局部观测。开发者视图显示实际任务、工具绑定、操作和观测帧的引用。二维图、三维图和 Gazebo 静态取景分别使用各自的查看器；缺失或过期的机器人观测在任务页按来源标明。

用「在任务台提交」打开现有任务输入，从门厅发「前往教学楼门外」，再发「回到教学楼门厅」。路线展示使用地点注册与任务回执，实际终点可以与 Gazebo 真值对照。预填文字由操作员确认后提交。真实 TU Delft 的咖啡设施需要在独立训练区登记模型、标识和可达停靠位后才能用于具名取餐任务。[任务观测规格](../../docs/specs/SDD-031-campus-workbench-replay.md)

任务台里的地图适配了嵌入窗口，保留取景、缩放与三维俯视。「展开」可放大当前任务地图。独立校园地图提供图层开关、机器人选择和坐标拾取。完成任意一条具名地点任务后，可对独立校园运行浏览器验收，检查桌面与窄屏、可见画布、故障恢复和草稿确认。

绿色是 Agent 已公布的全局航点路线，任务视图隐藏校园测试步道。长路线沿当前航段最多显示 80 米、120 个点。可选的本地预览服务用当前航点、新鲜的 `odom` 位姿和局部栅格生成橙色候选。候选最长 1.25 米，仅供观测台显示。到达、切换操作、观测过期或插件不可用时撤下候选，已取得的全局路线仍可查看。局部视图可切换成三维点云，显示本地 XYZ 回波及有同帧证据的路线。当前轮式规划建议来自二维栅格。

在校园 Compose 项目中用由当前源码构建的 `EDGE_IMAGE` 启用只读候选。用插件管理器按 CAS 收据管理注册。若已安装其他提供者，通过站点配置选择相应版本。

```sh
docker compose -p edge-agent-campus -f compose.yaml -f compose/campus.yaml up -d --no-deps campus-local-preview
docker compose -p edge-agent-campus -f compose.yaml -f compose/campus.yaml run --rm -T --no-deps \
  -v "$PWD/scene/campus_large/campus.local_preview.json:/release/campus.local_preview.json:ro" \
  plugin-admin publish --kind runtime --root /plugins --expect absent /release/campus.local_preview.json
```

```sh
node tests/system/campus_mission_browser.cjs --url http://127.0.0.1:8766 \
  --browser /path/to/chrome --puppeteer /path/to/puppeteer-core \
  --map-port 8773 --overview-port 8780 --out .ci-results/campus-mission/browser
```

## 3　照片怎样取得，何时会更新

要看随机器一起运动的相机，在运行中的校园执行 `python scene/campus_large/run.py sensors --image <当前校园镜像>`。工作台的实时感知面板会出现两个入口。彩色相机页用右上角来源切换到轮式车、Crazyflie 或 G1，画面下方可核对话题、光学坐标系、标定和时间。Go1 此版搭载 3D 激光雷达，点云话题是 `/model/go1/lidar/points`。RGB-D 页显示轮式车的一对同步图，点击彩色图或深度图可读同一像素的距离和 RGB，超量程时显示“无有效回波”。端口冲突可用 `--camera-port`、`--rgbd-port` 自定义，工作台会同步更新。此命令只启动观测订阅和视图注册。[三台机载相机](http://127.0.0.1:8782/) · [车载 RGB-D](http://127.0.0.1:8783/)

首次进入总览时，采集服务依次订阅 `/campus/view/<视角>/image`。取景包括全校、实测街面、教学楼、机器人广场、运动场、树林和草坪。每处收到首个带有效仿真时间戳的 RGB8 帧后，将原像素编码为 PNG，随即退订。查询机器人的当前位置可在地图里选择机体，读取实时传感器可打开任务工作台。

图像可追溯到源话题、像素尺寸、采样时间戳和 PNG 的 SHA-256。复用前还会比对世界 SDF 与语义布局摘要。同一世界的完整照片可用于下次打开；照片损坏或缺失时仅重新采集相应视角。世界或语义布局换版会撤下旧图，待新世界提供有效采样后再展示。[当前照片采集清单](http://127.0.0.1:8780/capture.json)

## 4　独立运维与故障定位

从地图页头直接选择“任务会话”“运行复盘”或“原始事件”。读一次任务优先打开会话，里面的证据链接可以定位到同一归档中的原始事件。如果工作台显示 `session.service_missing`，为当前校园执行 `python scene/campus_large/run.py review --image <当前校园镜像>` 即可补装。已经有绑定但提供者离线时，查看 Session 容器日志，期间仍可使用其他两种记录页。[任务会话](http://127.0.0.1:8766/#sessions) · [验收记录](../../docs/evidence/campus-workbench-replay.json)

```sh
python scene/campus_large/run.py logs
python scene/campus_large/run.py stop
python scene/campus_large/run.py start
python scene/campus_large/run.py down
```

- `stop` 停止校园服务，`start` 从已安装的本地资产启动。`down` 撤下校园容器与网络，保留任务卷、已采集照片以及宿主下载缓存。日常命令固定使用 Compose 工程 `edge-agent-campus` 和 ROS_DOMAIN_ID 73；工作台、任务账本、插件注册目录均与已有机器人项目分开。
- 修改场景定义或资产后，运行 `python scene/campus_large/run.py reload`。命令先通过校园 Console 的公开接口查看运行中、规划中或等待的任务、占用底盘的任务、当前动作与新鲜里程计。仍有工作或车体在动时，命令会给出原因；可在工作台用返回的任务 ID 查看进度。
- 通过首次检查后，命令暂停接收新任务，再次核对任务和停车证据。若这时收到新工作，会恢复校园 Agent，原世界继续运行。确认空闲后，校园仿真、网关、任务内核、感知、工作台等进程及该工程正在运行的附加插件一起重启，建立新的仿真时钟基线。历史卷、已完成任务和照片缓存保留。ROS 就绪探针会核查 `/odom` 和 `/ground_truth` 的时间戳在推进、导航 Action 和底盘任务服务在线，之后可再次提交“前往教学楼门外”。业务入口无法读取或时钟已停时，先检查 `status` 和 `logs`；部分服务意外退出时用 `start` 恢复校园栈再执行检查。
- 页面可访问但暂时缺图时，运行 `logs` 查看 `campus-capture` 是否收到对应 Gazebo 话题。构建失败时检查 `campus-build` 的退出信息，并用下载器的 `--offline` 核对缓存。位姿或扫描未更新时在工作台核查 `/clock`、`/odom`、`/scan` 三路。图像来自渲染，导航终态应由 ROS 回执验证。

Compose 覆盖文件、截图回归和部署隔离检查均可独立阅读。[部署定义](../../compose/campus.yaml) · [截图时序与缓存回归](../../tests/test_campus_capture.py) · [部署隔离回归](../../tests/test_campus_deployment.py) · [SDD-025](../../docs/specs/SDD-025-large-campus-world.md)

要单独检验重载与再次派发，可在仓库根目录运行以下命令。测试创建名称随机的校园工程，在同一工程两次提交独立 ID 的穿门任务，两次之间调用日常 `reload` 的实现。报告记录新旧会话、旧回执能否继续读取、第二次任务的验收点数及独立真值和里程计。运行结束回收临时工程。[隔离 Gazebo 验收](../../tests/system/campus_acceptance.py)

```sh
python tests/system/campus_acceptance.py --verify-reload --image edge-agent-ros:v0.1 --out .ci-results/system/campus-reload.json
```

本次独立运行两次均完成 3 个航点。重载后可查到此前相同的逐点回执，第二次停车的 Gazebo 真值距目标 6.46 cm。两轮均在仿真执行。[两次任务与独立量测](../../docs/evidence/campus-reload.json)
