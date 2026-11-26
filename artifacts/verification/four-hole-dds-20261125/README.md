# 四孔板工位的合成 DDS 验收

输入是隔离 DDS 域 171 内生成的 ArUco 图像、相机内参和双雷达 PointCloud2。用户在 8912 控制台提交“请帮我标定当前传感器”，Qwen3 4B 选择 DSH 工具。模拟持板人在收到缺角码、右雷达缺孔和重复姿态提示后换位。五段录制共 23 个同步时刻，前三个姿态拟合，后两个独立验证。

- `agent-task.json` 保存原任务及工具调用回执。
- `operator-feedback.jsonl` 记录合成持板人实际观察到的反馈。
- `revision.json` 是工位发布的标定版本，`independent_truth.json` 给出夹具真值误差。
- `observations.zip` 保存原始 RGB、点云、内参和五段录制的清单。二进制对象以 SHA-256 命名。
- `restarted.json` 和 `plugin-lifecycle.json` 记录重启、卸载和恢复的实测结果。
- `offline-replay.json` 是独立容器逐帧复跑所得，完整报告与原结果一致。

在仓库根目录运行以下命令可脱网重放，需要本地已有计算镜像。

```powershell
docker run --rm --network none -e PYTHONPATH=/src -e PYTHONDONTWRITEBYTECODE=1 -e OPENBLAS_NUM_THREADS=1 -e OMP_NUM_THREADS=1 -v "${PWD}:/src:ro" -w /src edge-agent-ros:board-calibration-1 python3 tests/system/four_hole_replay.py artifacts/verification/four-hole-dds-20261125
```
