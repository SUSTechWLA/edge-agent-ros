# 在 Gazebo 复测 RGB-D 与双 3D 激光融合

本实验把两个不同位置、不同朝向的三维扫描激光与一台 RGB-D 相机固定在同一支架上，分别在两个静止机位观测同一场景。Gazebo Harmonic 实际渲染七种材质、近处遮挡物和背景，ROS 2 Jazzy 桥只转发传感器消息。通过两次仿真读回的支架位姿，将两个站点的点表达在同一世界坐标系。该实验验证米制融合数据链与标定误差的后果。位姿精度、真实 MID360 扫描线型与硬件同步需另行测量。[场景 SDF](../../sim/rgb_lidar_bench.sdf) · [采集器](../../tests/system/capture_rgb_lidar_sim.py) · [独立评分](../../tests/system/score_rgb_lidar_sim.py)

## 本轮结果

| 观测与独立评分 | A 站 | B 站 |
|---|---:|---:|
| 世界位姿 x、y、偏航 | 0 m，0 m，0 rad | 0.32 m，−0.48 m，0.1 rad |
| 有限激光点 / 着色点 | 12,680 / 8,184 | 13,306 / 8,702 |
| 留出的清晰可见面内，着色点材质命中 | 7,536 / 7,537 | 7,933 / 7,935 |
| 留出的清晰可见面内，着色覆盖率 | 96.5% | 98.1% |
| 明确被挡的光线中，错误着色 | 0 / 430 | 0 / 532 |
| 全部有限点到最近世界真值表面的距离 P95 | 7.47 mm | 6.98 mm |
| 错误批准相机外参，横向偏 3 cm 且偏航偏 2°，明确被挡点错误着色 | 93 / 430 | 130 / 532 |
| 错误批准相机外参，横向偏 45 cm，明确被挡点错误着色 | 250 / 430 | 246 / 532 |

两个站点的彩色、对齐深度、相机内参和两台激光共五个 ROS 消息头均对齐在各自仿真时钟的第 1 秒。支架的位姿由 Gazebo 的 `/world/fusion_bench/pose/info` 回读，与 SDF 初始位姿交叉核对。归档分别保存原始 RGB24、32FC1 深度、每个激光的点、标定和两个版本的场景摘要，其中 `static_world_ref` 排除支架，`world_file_sha256` 包括该站的支架位姿。这两个独立静止实验之间没有行驶轨迹。[A 站原始归档](../../artifacts/verification/rgb-lidar-fusion-20261124/station-a.tar.gz) · [B 站原始归档](../../artifacts/verification/rgb-lidar-fusion-20261124/station-b.tar.gz)

评分程序从世界 SDF 读取盒体几何与漫反射颜色，独立计算相机视场、光线的首个相交面和材质类别。面内点距物体外表面小于 2.5 cm，距棱边超过 3.5 cm。判定明确遮挡时，将遮挡物各面向内缩 1.5 cm，且交点到目标还剩 5 cm 以上。由此剔除两个站点各 124 和 195 条轮廓歧义光线。材质命中率只统计已经着色且满足上述可见性规则的点。同时列出覆盖率和遮挡误涂，能识别只给少量容易的点上色所产生的虚高命中率。将其中一台雷达的时间偏移 200 ms，服务拒绝融合；单改输入外参而保持固定的标定版本，服务也拒绝融合。故意把错误数值同时写进输入与标定后，独立真值仍能暴露误差。[A 站评分](../../artifacts/verification/rgb-lidar-fusion-20261124/station-a-score.json) · [B 站评分](../../artifacts/verification/rgb-lidar-fusion-20261124/station-b-score.json)

## 从传感器重跑

以下命令在项目根目录的 Bash 中执行，使用空输出目录和本地 Gazebo 镜像。容器仅挂载仓库与本次输出，不接入设备。每台激光使用 161 × 51 条仿真射线，彩色与深度图各为 320 × 200。采集会等待五个时间戳一致、经过渲染预热且能看到彩色目标的窗口。

```sh
export OUT="$(mktemp -d)"
export IMAGE=edge-rover-local:maneuver-verified
for station in a b
do
  pose="0 0 0"
  if [ "$station" = b ]; then pose="0.32 -0.48 0.1"; fi
  docker run --rm --network none --cap-drop ALL --read-only \
    --tmpfs /tmp:exec,size=300m --tmpfs /root:size=200m --shm-size=256m \
    -e ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST -e ROS_DOMAIN_ID=168 \
    -e GZ_PARTITION=fusion_exp -e GZ_IP=127.0.0.1 -e LIBGL_ALWAYS_SOFTWARE=1 \
    -v "$PWD:/repo:ro" -v "$OUT:/out" --entrypoint bash "$IMAGE" -c \
    "source /opt/ros/jazzy/setup.bash && python3 -B /repo/tests/system/capture_rgb_lidar_sim.py /out/station-$station --pose $pose"
  ref="sha256:$(sha256sum "$OUT/station-$station/capture.json" | cut -d' ' -f1)"
  docker run --rm --network none --cap-drop ALL --read-only --tmpfs /tmp \
    -v "$PWD:/repo:ro" -v "$OUT:/out" --entrypoint python3 "$IMAGE" -B \
    /repo/tests/system/score_rgb_lidar_sim.py "/out/station-$station" "$ref" "/out/station-$station-score.json"
done
```

每站评分会从两份原始激光点数组枚举全部有限回波，逐个核对输出的传感器 ID 和源索引。两项负对照分别删去最后一条回波、将最后一条替换为第一条。评分必须拒绝两种结果，即使替换后总行数与着色比例保持一致。报告中的 `finite_returns_by_source` 给出各源数量，`rejected_provenance_negative_controls` 给出拒绝原因。

继续使用本次的 `OUT` 重算两个站点的地图清单与二进制 PLY。第二个容器按清单摘要打开地图，PLY 中的 `station`、`source`、`source_index` 对应原始测量。

```sh
docker run --rm --network none --cap-drop ALL --read-only --tmpfs /tmp \
  -v "$PWD:/repo:ro" -v "$OUT:/out" -w /repo --entrypoint python3 "$IMAGE" -B \
  -m perception.scene_layers.fusion_map /out station-a station-b
docker run --rm -i --network none --cap-drop ALL --read-only --tmpfs /tmp \
  -v "$PWD:/repo:ro" -v "$OUT:/out" -w /repo --entrypoint python3 "$IMAGE" -B - <<'PY'
from hashlib import sha256
from pathlib import Path
from perception.scene_layers.fusion_map import RegisteredMap
from perception.scene_layers.lidar_rgb import encode_ply

root = Path("/out")
ref = "sha256:" + sha256((root / "map.json").read_bytes()).hexdigest()
survey = RegisteredMap(root, ref)
layer = survey.describe("fusion")
data = survey.read("fusion", layer["layer_ref"])["data"]
(root / "rebuilt-map.ply").write_bytes(encode_ply(data))
print(ref, layer["layer_ref"], data["stats"])
PY
```

本轮镜像 ID 为 `sha256:f69ecbd0c97e19c142f170007e3d2d22351da4ebb98d8a002fcd252309df58ba`。要复查既有归档，可将两个压缩包解到同一输出目录，使用各自评分中的 `capture_ref` 执行评分命令。不同运行的头一组完整时间戳和文件摘要可能因调度而变化，评分通过条件与世界几何保持可复核。
