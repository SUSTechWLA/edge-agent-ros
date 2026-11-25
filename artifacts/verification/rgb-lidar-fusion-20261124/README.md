# Gazebo 双站 RGB-D 与双雷达三维地图

两台三维扫描雷达和一台 RGB-D 相机在两个静止机位观测同一世界。两个进程各自读回支架的仿真真值位姿，各站的同步采集独立配对。软件服务 `map.lidar_rgb.fused` 投影激光点、核对相机深度和遮挡，然后将带来源的观测放到世界坐标中。这里的双雷达是 Gazebo 三维扫描模型。实验没有估计移动轨迹，也未连接实车。[Gazebo 采集、真值评分与重跑命令](../../../docs/testing/gazebo-rgb-lidar-fusion.md) · [实车标定与验收](../../../docs/testing/rover-lidar-rgb-calibration.md)

| 观察 | 复核结果 |
|---|---:|
| 两个机位的有效激光回波 | 25,986 |
| 取得同步相机颜色 / 保留几何、颜色未知 | 16,886 / 9,100 |
| 可独立评分且已着色的可见面内点，材质正确 | 15,469 / 15,472 |
| 两站可见面内的着色覆盖率 | 96.5% / 98.1% |
| 明确遮挡的点中，正确标定时误着色 | 0 / 962 |
| 错误批准相机横向偏移 3 cm、偏航偏差 2° 后误着色 | 223 / 962 |
| 两站到场景真值表面的距离 P95 | 7.47 mm / 6.98 mm |
| 在 10 cm 网格去重后，B 站补充的已着色格数 | 746 |

导出保留所有有效回波。PLY 用 `color_valid` 区分实测 RGB 与未知，用 `station`、`source` 和 `source_index` 追溯机位、雷达与原始点。浏览器可以旋转、俯视、切换来源和未知颜色、点击查看世界坐标，或下载当前绑定的 PLY。[三维视图](fusion-map.png) · [俯视图](fusion-top.png) · [米制点云 PLY](fusion-map.ply) · [验收明细](acceptance.json) · [浏览器回归](browser.json)

## 从归档重放

在仓库根目录用 Bash 执行下列命令。原始包只包含本次仿真数据；无须启动 ROS 或 Gazebo 便能重算地图。两个归档各含独立的采集清单、RGB、深度、标定、位姿见证、SDF 和日志。[A 站原始包](station-a.tar.gz) · [B 站原始包](station-b.tar.gz) · [A 站真值评分](station-a-score.json) · [B 站真值评分](station-b-score.json) · [双站清单](map.json)

```sh
evidence=artifacts/verification/rgb-lidar-fusion-20261124
survey_dir=$(mktemp -d)
tar -xzf "$evidence/station-a.tar.gz" -C "$survey_dir"
tar -xzf "$evidence/station-b.tar.gz" -C "$survey_dir"
cp "$evidence/map.json" "$survey_dir/map.json"
python3 -B -m perception.scene_layers.workbench --port 8760 \
  --fusion-directory "$survey_dir" \
  --fusion-manifest-ref sha256:750a90554b595bb5e4d204f0778881e791fe8222ba8a796256cd3dbeddb2fab0
```

打开 [本机融合地图](http://127.0.0.1:8760/fusion)。若端口已用于另一回放，可替换 `--port`。原始输入可在固定版本下反复计算；每次服务安装具有独立的运行时绑定。[插件接口](../../../perception/scene_layers/README.md) · [多站位姿和热停用测试](../../../tests/test_registered_fusion_map.py)
