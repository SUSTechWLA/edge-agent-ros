# 大学校园与树模型的来源和维护

校园插件在首次准备时下载 Gazebo Fuel 的两个固定版本。默认缓存为仓库内 `artifacts/sim-assets/`，容器将此目录挂为 `/assets`。运行中的 Gazebo 从 `/assets/models` 解析模型；下载器与模型源文件不参与机器人的在线决策。

| 模型 | 作者与版本 | 原始 ZIP | 解包 | 入口 |
|---|---|---:|---:|---|
| SIT 校园 | 模型署名 Wan Yi，由 NCS_robotmanager 发布，Fuel v2 | 71,816,347 B | 126,117,050 B，51 个文件 | `models/Campus/model.sdf` |
| Pine Tree | Louise Poubel，OpenRobotics，Fuel v6 | 877,470 B | 955,728 B，13 个文件 | `models/Pine Tree/model.sdf` |

两者在 Gazebo Fuel 元数据中均标注 CC0-1.0。仓库记录作者、版本、原始下载 URL、字节数、整包 SHA-256 和引用的模型入口。首次传输遇到大文件超时后，校园改为用 HTTP Range 请求，再按顺序还原上游 ZIP。固定版本的整包散列为 `ef37e069a89e24c3908c3c86918ecbcca8d07314612fc11215081864c84194fb`，树的整包散列为 `d436a44229321314e79e4c2ba709dc926e833a5b5d90d16c5d085c2a74576e25`。[校园原始项](https://app.gazebosim.org/NCS_robotmanager/fuel/models/Campus) · [树原始项](https://app.gazebosim.org/OpenRobotics/fuel/models/Pine%20Tree) · [CC0](https://creativecommons.org/publicdomain/zero/1.0/) · [版本锁](asset-lock.json)

## 哪些内容来自校园模型

原始 SDF 把视觉与碰撞都绑定到 `meshes/Campus_world.obj`，局部 Z 坐标偏移 −0.25 m。OBJ 在该偏移之前的边界为 X [−171.791458, 181.617828]、Y [−171.876160, 112.102425]、Z [−1.000000, 3.485012] m。其水平跨度约 353.4 × 284.0 m。模型共有 477,187 个顶点、405,658 个多边形面，按面顶点数展开约 724,102 个三角形。合并后的校园网格使用 50 种材质，包含道路、路缘、人行道、草坪、灌木、地毯、桌椅、投影幕与白板。上游预览可以辨认有分隔的室内区域。穿门、进教室、碰撞和任务可达性应在本项目具体运动区域验证。[原始 SDF](https://fuel.gazebosim.org/1.0/NCS_robotmanager/models/Campus/2/files/model.sdf) · [上游总览图](https://fuel.gazebosim.org/1.0/NCS_robotmanager/models/Campus/2/files/thumbnails/campus_sim.png)

原包内的 `Campus_world.mtl` 引用的全部漫反射贴图均存在。材质同时引用三张未随包分发的可选法线贴图，分别是 `CoffeeTable_Normal.png`、`PottedPlant1_Normal.png` 与 `PottedPlant2_Normal.png`。实务上需区分法线细节缺失与基础几何或漫反射贴图加载失败。仅一种盆栽材质就包含 115,710 个面，完整网格参与碰撞，在 Jetson 或 RK3588 部署前，应单独测量实时率、内存和局部导航性能。

原始树模型的 DAE 使用 Z 朝上，节点附带 0.0254 的缩放。应用节点变换后的树高约 5.08 m、冠幅约 3.17 m。教学验证区、成片树林的布置、车道和运动场由本项目构建器组织，具体位置以生成后的场地说明为准。[原始树模型 SDF](https://fuel.gazebosim.org/1.0/OpenRobotics/models/Pine%20Tree/6/files/model.sdf) · [校园生成器](build.py)

## 准备、恢复与测试

从项目根目录执行。两个原包共约 72.7 MB，加上模型展开后的约 127.1 MB。首次分段下载与安装暂存还需额外空间。终端中的 `restored_files` 表示从经校验的原包安装或恢复了多少个源文件。

```bash
python scene/campus_large/fetch_assets.py
python scene/campus_large/fetch_assets.py --offline
python tests/test_campus_assets.py
# 已安装 pytest 的开发环境也可执行
python -m pytest -q tests/test_campus_assets.py
```

断网状态下可以使用完整的原包修复展开后丢失或被改过的文件。已经完整的缓存重复执行，两个模型均返回 `restored_files: 0`。若 ZIP 本身损坏，离线模式报告待恢复的路径，联网运行会重新获取候选包，校验通过才替换。首次安装先验证所有成员并写入临时目录，最后发布模型目录。脚本使用 `--root PATH` 可选择其他专用缓存。测试覆盖断网、重复执行、同长度损坏、原包损坏、分段重试、拒绝异常文件路径与重复成员。[获取脚本](fetch_assets.py) · [独立测试](../../tests/test_campus_assets.py)
