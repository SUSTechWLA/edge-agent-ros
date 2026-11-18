# 代尔夫特理工大学实景建筑区

在校园书签中可以看到 TU Delft 的 Mekelweg、EWI 高层、大学图书馆周边建筑及绿地。从高处看地面航片，再将视角降低，能看到同一位置上的墙面、屋顶高差和建筑之间的通道。选区为 **650 × 760 米（49.4 公顷）**。合计 **152 个 BAG 建筑登记对象**，包含学术楼、邻近住宅和附属建筑。学校地图上的 21 号楼位于 Prometheusplein 1，是大学图书馆；36 号楼位于 Mekelweg 4，是 EWI 塔楼。[TU Delft 校园地图](https://homepage.tudelft.nl/z63s8/Plattegrond_TUDelft.pdf)

| 仿真中看到的内容 | 数据及处理 | 适用范围 |
|---|---|---|
| 建筑外形与屋顶 | 3DBAG 用荷兰 BAG 建筑轮廓和 AHN 高程数据重建 LoD2.2 表面。按原始环和孔进行平面三角剖分，保留轮廓、朝向、屋面起伏和登记 ID。 | 13,445 个屋顶三角面、18,499 个墙面三角面、3,179 个底面三角面；最高顶点距其楼体基底 95.66 米。可用于观察建筑遮挡、立面和室外空间。 |
| 道路、绿地、树冠与屋顶颜色 | PDOK 发布的 2025 年 RGB 正射航片。原始图层地面采样间距为 25 厘米，本场景以 1,560 × 1,824 像素发布，每像素约 41.7 厘米。南西角作为贴图原点。 | 照片中的车辆、行人、树冠、水面与临时设施没有各自的三维碰撞体。树冠或高楼边缘可能存在正射位移，拍摄年份和建筑重建数据也可能不同。 |
| 墙面上的窗格、材质 | 本仓库生成示意性玻璃和砖墙贴图，按墙面方向及楼层尺度重复采样。 | 窗格、门和装修样式没有逐栋测绘。建筑区提供城市尺度的外壳；室内导航在配套搭建的可进入教学楼中验证。 |
| 物理接触 | Gazebo 的静态建筑碰撞面与可见建筑使用相同 OBJ，包含完整显式法线。地面为全局仿真支撑平面。 | 外壳在地图矩形的四条边上裁切，可观察区域位于边界内。立面材质和地面照片的色度变化不会产生额外碰撞。 |

## 坐标与摆放

源数据为 EPSG:7415，水平轴是荷兰 RD New 东、北坐标，Z 轴是 NAP 高程，单位米。取样边界在 RD 平面为西 85,030、南 445,850、东 85,680、北 446,610。模型顶点的 `x = RD_east − 85030`、`y = RD_north − 445850`。每栋建筑的 Z 减去其地面表面的最低高程，匹配仿真平地，保留楼体自身的垂向形状。SDF 将整个模型平移至 `odom` 的 `(395,12,0)`。地图内保留米制比例与朝北方向。原始页中保留 NAP 高程，后续如需起伏地形可用同一高程基准重新摆放。

## 授权与署名

建筑数据 © 3DBAG by tudelft3d and 3DGI。这里执行了裁切、模型坐标转换、三角化和材质映射。数字地图应在右下角展示上述署名并链接版权页。[3DBAG 版权与署名要求](https://docs.3dbag.nl/en/copyright/) · [3DBAG API](https://api.3dbag.nl/api.html) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

地面及屋顶影像来自荷兰各级政府的 Samenwerkingsverband Beeldmateriaal，由 Kadaster / PDOK 提供。使用 2025 年 `2025_ortho25` 图层，按选区重采样并转为 PNG。[PDOK 的 2025 年影像公告](https://www.pdok.nl/-/luchtfoto-2025-nu-beschikbaar-bij-pdok) · [政府数据目录与 CC BY 4.0 许可](https://data.overheid.nl/en/dataset/80585-luchtfoto-beeldmateriaal-2025-25cm-rgb-open-data)

## 离线运行与可复现构建

| 文件 | 用途 |
|---|---|
| `manifest.json` | 固定 CityJSON 页、原始影像请求、输入 SHA-256、运行文件 SHA-256、建筑 ID 和统计数据。压缩 OBJ 的运行校验针对解压后的内容。 |
| `sources.zip` | 十个不重叠取样窗口的原始 CityJSON 完整响应，以及原始 WMS 影像响应。ZIP 保留原始字节。接口在请求 PNG 时实际返回了 JPEG，本项目按返回内容解码，生成真正的 PNG。 |
| `buildings.obj.gz`、`terrain.obj`、`buildings.mtl`、`*.png` | 可离线使用的完整运行资源。构建场景时 `real_site.py` 按清单校验、解压、写入共享场景目录 `/campus/real_site/`，供 Gazebo、正射渲染器和浏览器模型导出器使用。 |

在仓库根目录使用以下命令从固定源重建至临时输出目录，并运行无网络回归检查。依赖仅用于重作地图，启动模拟器时不需要安装。

```sh
uv run --python 3.11 --with numpy==2.3.3 --with mapbox-earcut==2.0.0 --with pillow==11.3.0 python scene/campus_large/real_site_import.py --output artifacts/sim-assets/delft-rebuilt
python -m unittest discover -s tests -p test_campus_real_site.py
```

将临时产物中的 `manifest.json` 与本目录版本比较，可校验全部重建结果。开发者也可加上 `--fetch --cache artifacts/sim-assets/delft-sources` 重新请求官方服务。导入器会检查输入哈希，上游若已重新发布数据会明确报错。更新数据版本须复核许可、疆域、原始页、建筑清单和三维视图，随后同一变更发布新归档及派生文件。无需联网即可恢复本版地图。
