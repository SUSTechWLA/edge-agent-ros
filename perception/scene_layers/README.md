# 地图表达与场景查询

本地工作台使用已有的 Jetson 传感器归档，按文件摘要核对证据。可以分别查看双 MID‑360 和 D435，查询二者能否组成共同场景，停用纹理插件，再向原有的轮式车服务发起新规划。进程仅监听环回地址，使用回放时钟，无需 ROS、Jetson 连接或底盘权限。

在本仓库根目录启动后打开 [本机工作台](http://127.0.0.1:8757/)。

```sh
python -B -m perception.scene_layers.workbench --port 8757
```

Windows 可用项目的 PSF 签名 Python 启动器运行同一模块。本机交付的启动命令如下。

```powershell
& 'D:/project/edge-agent-ros/scripts/project-python.ps1' -B -m perception.scene_layers.workbench --port 8757
```

默认输入为 `artifacts/verification/jetson-field-scene-20261123/sensor-evidence`。若现场数据单独保管，用 `--archive` 指定同格式的只读目录。工作台每次启动创建独立站点目录，路径打印到标准输出；可指定 `--state-parent` 保存本次 DSH 管理收据及冻结清单。停止服务后可从原始归档重开。可选的 `--grid-directory` 与 `--grid-manifest-ref sha256:…` 将独立的已归档 Nav2 栅格加入站点，查询名为 `grid2d`。

## 软件插件与读取流程

| DSH 服务 | 拥有的结果 |
|---|---|
| `map.mid360.occupancy` | 归档车体坐标中的三维占用体素、已观测和占用地面格。依据双雷达数据和绑定标定重建、与原制品比较。 |
| `map.rgbd.surface` | 1.1.0 版提供相机光学帧中的单曝光表面、每个有效顶点的 UV、原始 RGB PNG 和标定来源。深度跳变处保留空洞。 |
| `map.scene.query` | 按图层、坐标、参与采样的时间范围和质量要求固定场景快照。每次按站点目录选择实际图层提供者。 |
| `map.nav2.grid` | 可选，读取已有二维地图及其清单，保留原地图身份。 |
| `map.lidar_rgb.fused` | 可选，在有标定的同窗内给激光回波赋 RGB，保留未知颜色与每个点的来源。支持在独立仿真站点使用已知位姿登记多窗到同一静态世界。 |
| `planning.local_path` | 沿用版本 1.1.0 原有的静态路径评估接口。机器端接收原始车体射线与回波，独立于纹理。 |

三个默认地图服务在各自的只读业务进程计算。站点管理者以现有 DSH v1 的 `plugin_manager.change` 写入受管清单，使用期望修订比较交换并落下管理事件。消费者通过 `ServiceSnapshot.pin` 固定服务及依赖。停用纹理时，先排空，等待受理中的读取结束和进程退出，再移除导出。旧快照元数据可按摘要查阅；再次读取纹理须重新核验原提供者绑定。变化过程可用登记事件、进程退出状态及新的消费者请求独立检查。

工作台 `POST /api/compose` 可以传入 `archive`、`layers`、可选的 `limits`。质量限制包括 `frame_id`、`max_skew_ms`、`max_resolution_m`、`max_error_m` 和以米计的三轴 `roi_m`。回放的稀疏表面和体素没有完整三维覆盖证明，要求整个体积已观测时会返回原因。场景查询要求共享已验证的空间身份并核对时间。跨传感器着色由独立的配准图层生产者核验，现有实车 RGB-D 与雷达归档仍然分别显示。

在跨传感器工作台打开 `/fusion`，页面通过 `GET /api/fusion/map` 读取固定版本。PLY 下载引用同一个场景和图层；颜色未知的点带 `color_valid=0`，每个观察均可定位到雷达、原始点索引和采集站。累计地图的时间戳是检查点创建时间，各站采样时间属于各自仿真运行。软件注册、查询与排空复用现有 DSH 生命周期。[地图下载与启动指令](../../artifacts/verification/rgb-lidar-fusion-20261124/README.md)

### 查看或下载 RGB 纹理地图

工作台的 `/?view=texture#texture-card` 直接打开归档。在「RGB 纹理」和「深度几何」之间切换会保留观察角度，可以拖动旋转、放大并对照同一时刻的彩色照片。深度无效或跨越遮挡边缘的区域保持空白。

「下载纹理地图」导出单个 GLB 文件，内含米制三角面、按原始像素中心生成的 UV 和原始 PNG。HTTP 入口为 `GET /api/maps/<scene_ref 的 64 位摘要>/<layer_ref 的 64 位摘要>.glb`。每次下载都校验冻结场景、提供者绑定和图像摘要。模型保留相机光学帧及采集时间，顶点从光学轴转换为 glTF 轴 `(x, -y, -z)`，可供其他三维工具读取。[glTF 2.0 规范](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html)

```json
{"archive":"jetson-20261123","layers":["occupancy","texture"],"limits":{"frame_id":"base_foot","max_skew_ms":250}}
```

成功的查询返回内容寻址的 `scene_ref`。可用 `GET /api/scenes/<64位十六进制摘要>` 读取冻结清单，或向 `POST /api/layers` 提交同一 `archive`、`layer`、`scene_ref`、`layer_ref` 读取绑定内容。不兼容查询返回具体原因和独立观测的描述，`scene_ref` 为空。站点可通过 `scene-layer-services.json` 把新的图层名指向独立安装、符合该协议的 DSH 导出。现有例子覆盖新增第三方图层、停用后不能再获取数据及历史清单仍可读。[运行时回归](../../tests/test_scene_layer_runtime.py)

例如，在工作台打印的站点目录中放置以下 `scene-layer-services.json`，将用户图层映射到已由站点管理者发布的唯一导出。索引只描述选用关系；安装、撤销和版本追溯仍由 DSH 目录负责。

```json
{"schema_version":1,"layers":{"usersurface":"vendor.tabletop.mesh"}}
```

该导出在环回地址提供 `POST /invoke`。请求包含 `binding`、`operation`（`describe` 或 `read`）及 `arguments`。成功回复附 `X-DSH-Binding`。描述符提供场景查询所需的 `kind`、坐标、采集时间、空间身份、质量与内容摘要，另附实际生产者 binding 和 `implementation_ref`。按 `layer_ref` 读取时返回同一描述符和 `data`。每个提供者负责核对原始数据与返回内容。内置提供者记录本轮所用几何和服务源码的摘要。外部提供者的构建身份由站点发布者验收。

纹理文件缺失或摘要有误时，该提供者显示 `offline`，占用与场景查询继续受理。修复归档后重新启用即可恢复。监督进程关闭会排空子进程；显式停用另存为站点操作意图，下一次以相同 `ReplaySite` 状态目录启动时继续生效。

## 复现验证

从仓库根目录可构建仿真镜像并运行封装验收。回放测试只挂载公开的 Gazebo 归档，检查镜像内工作台的启动、HTTP 地图与冻结 PLY 导出。

```sh
docker build -f docker/Dockerfile -t edge-rover-local:sdd057-container .
docker run --rm --network none --read-only --cap-drop ALL --tmpfs /tmp \
  --mount "type=bind,source=$PWD/artifacts/verification/rgb-lidar-fusion-20261124,target=/survey,readonly" \
  edge-rover-local:sdd057-container python3 -B /opt/edge/tests/system/scene_workbench_image.py /survey
```

```sh
python -B -m unittest discover -s tests -p 'test_scene*057.py'
python -B -m unittest discover -s tests -p 'test_scene_layer_runtime.py'
```

有现场归档时，可对独立启动的回放站点运行浏览器验收。测试会在该站点停用、恢复纹理，并对照三角面内部的真实像素和导出文件；截图和回执保存在指定输出目录。

```sh
node tests/system/scene_texture_browser.cjs --url http://127.0.0.1:8758 --out .ci-results/rgb-browser --browser /path/to/chrome --puppeteer /path/to/puppeteer-core
```

现场规划保留原传感器时戳，隔离的 HTTP 提供者使用冻结的归档时钟。Gazebo 的独立对照站点把仿真时钟平移到声明的回放原点，回执同时列出原时间和准入时间。每次请求的路径由原 `/evaluate` 重新计算。回执包含传感器快照摘要、提供者绑定、几何摘要及下游实际评估序号。两种时钟均用于回放，实机实时性须另测。[规格与验收范围](../../docs/specs/SDD-057-map-layers-and-planning-scenes.md)
