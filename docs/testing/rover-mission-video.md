# 从一次无人车任务的原始观测生成实验录像

录像回答三个问题：用户提交了什么，车在执行哪一步，故障出现后依据什么记录继续执行。摄像机观察真实运行中的 Gazebo，地图与融合位姿从 ROS 话题取得。开发机负责编码与字幕，机器人运行时无需安装视频依赖。

## 重放一条完整任务

准备已经通过验收的运行目录，其中包含 `acceptance.json`、`media/mission/timeline.jsonl` 和时间轴引用的 PNG、栅格 JSON。开发机安装 Python 3.11+、Pillow、含 libx264 的 FFmpeg、ffprobe 与可显示中文的字体。以下命令在仓库根目录执行，将路径和任务 ID 换成本次实际运行的值。

```sh
python tests/system/render_rover_video.py --mode mission \
  --run artifacts/verification/rover-long-mission/continuous-02 \
  --task-id patrol-0764413ed7 \
  --out artifacts/verification/rover-long-mission/continuous-02/video \
  --font /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
  --bold-font /usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc
```

Windows 可传入 `--font C:/Windows/Fonts/msyh.ttc --bold-font C:/Windows/Fonts/msyhbd.ttc`。如果 FFmpeg 没有加入 PATH，可用 `--ffmpeg`、`--ffprobe` 指定两个可执行文件的绝对路径。

| 生成文件 | 阅读方式 |
|---|---|
| `rover-long-mission.mp4` | H.264、1440 × 810、12 fps、仿真原速。右上为 Gazebo 同一实验的时钟。 |
| `rover-long-mission-cover.png` | 取同一任务第三站回执时的实际画面作为封面。 |
| `README.md` | 按视频位置查到自然语言请求、故障、恢复、目标、到点和终态的中文导读。 |
| `video.json` | 按 `task_id` 绑定录像。播放器按 `markers[].video_seconds` 定位章节，沿 `stamp_ns` 查当时的原始观测；其中包含录像和时间轴的 SHA-256。 |
| `ffprobe.json` | 对实际 H.264 输出检查编码、像素尺寸、帧数、时长后得到的媒体信息。 |

## 如何解释画面

- **左侧相机**覆盖室内工坊、门廊和庭院。针对观察相机隐去屋顶和靠近相机的两面墙，雷达与物理碰撞仍使用实际场景几何。
- **右侧地图**截取坐标原点附近 10 m × 10 m 的视窗，保留栅格边界。已知格数统计整张图；车端融合位姿与地图在同一坐标系时才叠加。绿色箭头给出当时的朝向，历史点来自实际测量，黄圈来自网关目标记录。
- **任务字幕与故障栏**分别显示当前工作与已观测的故障历史。即使容器很快重新启动，之前的超时与进程退出也会保留。仿真原速覆盖全部等待过程。
- 源传感器按消息时间取样。宿主机上从接口观察到的任务记录，以采集器接收该记录时的 `/clock` 登记。多个事件可能落在同一帧，可对照逐条导读分析。
- 每个播放时刻使用不晚于该时刻的最近真实观测。完整主片对应单个 `task_id`、一个仿真时钟周期和一次成功验收；独立的探索和撤销实验用 `--mode exploration` 另行生成。

[实验录制规格](../specs/SDD-042-continuous-mission-recording.md) · [录制驱动](../../tests/system/record_long_mission.py) · [渲染工具](../../tests/system/render_rover_video.py)
