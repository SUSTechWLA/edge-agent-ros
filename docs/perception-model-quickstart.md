# 从工作台安装感知与模型插件

本例在一台 Docker 工作站上同时运行 2D 导航、三维点云地图、RGB 相机、RGB-D 像素测距和可切换的模型适配器。任务运行时、地图观测和模型调用各有独立入口。第一次读本页，可先回答三个问题：机器人看到了什么、来源的坐标与采样时间是否可信、模型究竟通过哪一家服务选择了工具。

## 1　先建立可核查的场景

观察同一片地面时，地图插件可以在二维占据栅格与三维透视之间切换。两边共用 ROS 栅格与语义区域，三维可进一步检查回波高度和遮挡。[二维与三维操作手册](../perception/map3d/README.md#在同一个位置比较二维与三维)

从仓库根目录执行。先完成仓库[快速运行](../README.md#快速运行)，用本次代码构建 Edge 镜像并启动已有工作台。以下 PowerShell 数组让所有命令使用同一工程和同一组 Compose 定义；在已有现场保留原工程名与 `EDGE_IMAGE`。如需运行本地 Qwen，先检查 Compose 的 model 是否已缓存权重。

```powershell
$edgeStack = @('-f','compose.yaml','-f','compose/camera.yaml','-f','compose/model-provider.yaml',
               '-f','compose/rgbd.yaml','-f','compose/rgbd-sensor.yaml')
docker compose @edgeStack --profile model up -d model
docker compose @edgeStack exec -T model ollama list
# 此镜像首次使用 Qwen 时执行一次
docker compose @edgeStack exec -T model ollama pull qwen2.5:1.5b
docker compose @edgeStack up -d sim bridge gateway core perception scene agent console
docker compose @edgeStack up -d map3d map3d-sensor camera camera-sensor rgbd rgbd-sensor model-provider
docker compose @edgeStack ps -a
```

显式列出服务可启动相应的可选 profile 和其初始化器。清单中 `*-init` 正常退出属于一次性初始化；常驻提供者应保持运行。工作台从 Console 的插件目录和对应启动事件发现同机可用视图。浏览器直接打开子服务也能检查它们各自的状态。

| 想验证什么 | 打开的位置 | 观察与交叉核对 |
|---|---|---|
| 依旧能够完成原来的任务 | [工作台](http://127.0.0.1:8765/) | 提交一个已知只读任务。用任务 ID 读取实际工具选择与结果。 |
| 看真实空间点 | [3D 地图](http://127.0.0.1:8770/) | 选择传感来源与视角，区分设计场景、二维扫描和测得的三维点。检查点云时间、固定 frame 和 TF。 |
| 看传感器原始 RGB | [相机预览](http://127.0.0.1:8771/) | 选择来源，看画面下方原始采样时间与坐标。停 camera-sensor 后等到 TTL 过期，旧图应撤下。 |
| 量取已对齐 RGB-D 深度 | [RGB-D](http://127.0.0.1:8772/) | 选择像素后比对帧对、颜色与光轴深度（米）；检查 CameraInfo、采样差和无效值。仅有 RGB 图无法推算经过标定的距离。 |
| 核对模型路由 | [已配置别名](http://127.0.0.1:8134/api/tags) | 路由存在不保证权重、网络或账户就绪。按下一节通过真实任务核验。 |

## 2　用同一模型入口切换本地与云端

本地 Qwen 可独立运行。在 Windows 工作站上若现有密钥保存在个人应用数据目录，只向云 overlay 指定文件路径。凭据文件由操作者保管，只读挂载至 model-provider；无需把值写入 .env、PowerShell 历史、业务请求、Git 或公共事件。云端角色会向 DeepSeek 发送它当次的私有目标、可用能力及输出 schema。只选择获准联网的角色。

```powershell
$env:EDGE_DEEPSEEK_KEY_FILE = Join-Path $env:LOCALAPPDATA 'edge-agent-ros/secrets/deepseek-api-key'
$cloudStack = $edgeStack + @('-f','compose/model-provider-cloud.yaml')
docker compose @cloudStack up -d model-provider

# 在默认 contracts 目录运行；若部署指定了 EDGE_CONTRACTS_DIR，显式传 --contracts-dir 该目录。
$localReceipt = python model_provider/select_planner.py --model qwen2.5:1.5b | ConvertFrom-Json
# 此时在工作台发起只读自然语言任务，例如“请读取本机当前的位置和速度”。
$cloudReceipt = python model_provider/select_planner.py --model deepseek-flash --expect $localReceipt.active | ConvertFrom-Json
# 再用一个新的任务 ID 提交等价的只读请求。
python model_provider/select_planner.py --restore $cloudReceipt.previous --expect $cloudReceipt.active
```

工作台测试选中了同一条 `status` 工具，并从 `/odom` 获得实测值。一次本地 Qwen 调用和一次云端调用在 Agent 侧分别记录约 4.18 秒与 1.17 秒。它们是两次实验记录；比较性能需要一致的输入集、冷/热启动、网络条件及多轮重复。Agent 侧 `ollama/deepseek-flash` 表示客户端所用的兼容协议。公共源 `journal:model.provider` 中的 provider=deepseek、模型 ID 及请求摘要才标明这次调用抵达哪个提供者。两次摘要均可与原生 Agent 事件对账。[实际调用摘要](evidence/workbench-model-routing.json) · [模型架构、权限与错误码](../model_provider/README.md)

## 3　从固定台站迁移到真实机器人

RealSense D435i / D455 可直接使用随仓库提供的 D4xx 配置。先在持有 USB 设备的主机启动厂商驱动，再启动板端的 `rgbd` 容器，即可在相同的测距页面读数。板端 Agent 采用 host 网络时，本机模型适配器的规划地址设为 `--url http://127.0.0.1:8134`。[接线、驱动参数、话题、点云和标尺验收](../perception/rgbd/README.md)

| 插件 / 规格 | 需要准备的真机输入 | 可单独改动、启停的部分 |
|---|---|---|
| [3D 地图 · SDD-020](specs/SDD-020-3d-map-plugin.md) | 已有标准 PointCloud2、有效的固定坐标系与采样时刻 TF | 只替换地图的视角、采样或可视化时改 map3d。只有二维激光时，真实高度观测还需新增数据源。 |
| [模型 · SDD-021](specs/SDD-021-model-provider.md) | 本机 Ollama 权重或已授权的 DeepSeek API。真实 Linux 板端另加 compose/model-provider-hardware.yaml | 规划器每次读取 profile。已有作者第一次切换接入地址时按其配置重建该作者进程。 |
| [相机 · SDD-022](specs/SDD-022-camera-plugin.md) | ROS 2 Image/CompressedImage、可选 CameraInfo 与同步时间 | 同目录原子替换 sources.json 可增删多路相机，每路独立 TTL 与状态。原始预览和语义识别属于不同任务。[相机操作](../perception/camera/README.md) |
| [多雷达 · SDD-023](specs/SDD-023-lidar-input-plugins.md) | 如 Livox MID-360，通过厂商驱动发布标准 PointCloud2、真实 frame 和外参 | 按机型把清单加入 map-inputs，移除该清单即退订。点云的厂商 CustomMsg 或逐点运动去畸变需另接转换与验收。[MID-360 接线与对账](../perception/map3d/profiles/README.md) |
| [RGB-D · SDD-024](specs/SDD-024-realsense-rgbd.md) | RealSense D435i/D455 的彩色、对齐到彩色的深度、CameraInfo、有效内参和一致的光学 frame | 在 rgbd 的配置中选择真实话题、时钟和配准来源；Gazebo RGB-D 服务可单独撤下。RGB、深度伪彩与数值测距可来自同一对已确认帧。 |

这一轮的物理边界为桌面 Docker、Gazebo 实际生成的传感数据及 ROS 协议夹具。MID-360、D435i/D455 的真机 USB/以太网、外参、反光或弱纹理场景、量程误差与台架长稳须用实际设备复核。若要将这些图像投入训练，还需另建带采样时间、原始深度、内外参、授权与数据集版本的录制及独立验证流程。当前 RGB-D 页面和模型适配器不产生训练后的模型。

## 4　独立撤下与排障

1. 先记下 `docker compose @edgeStack ps` 中当前服务身份，找一条实际成功且可重放的只读任务作对照。
2. `docker compose @edgeStack stop camera-sensor` 停止 RGB 相机的 ROS 桥。camera 的健康端点仍可读，来源过期后图像端点返回 503。三维雷达及 RGB-D 分别使用 `stop map3d-sensor` 和 `stop rgbd-sensor` 停止各自的桥及专用 TF 发布者。三个固定台站实体随 Gazebo 世界保留，场景可能仍占用渲染资源。用 `start <服务名>` 复用相应实体并恢复发布，核对新的 ROS 采样时间可确认恢复。世界重建后，仍运行的桥接服务会自动补建各自的台站。
3. `docker compose @edgeStack stop camera map3d rgbd` 撤下选择的预览服务。已装驱动拥有设备，数据库仍保留原有事件。查看独立模块源、话题发布者与采样时刻，能分开识别驱动断流和显示插件退出。
4. 撤下 model-provider 前先恢复此前的规划器配置并确认在途请求已经结束。若模型请求意外中断，恢复日志记录结果未知，不能据此推断一定未计费。然后执行 `docker compose @cloudStack stop model-provider`；仅使用本地 overlay 时改用 `@edgeStack`。
5. 加回同名服务使用 `docker compose ... start <服务>`；更改了挂载或部署配置则使用 `up -d <服务>`。上述操作均保留工程卷。核心任务、视觉插件、设备驱动与模型提供者各自的职责，通过[独立装卸实验](testing/map-and-model-plugins.md)和[事件总线](module-eventbus.md)定位。
