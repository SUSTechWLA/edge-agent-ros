# 接入额外的 ROS 2 传感器

控制台每秒扫描此目录的 `*.json`。默认只有本说明。要观测已经发布的前向测距话题，可保存以下内容为 `front-range.json`。删除该文件即可取消订阅，之前的事件仍能回放。

```json
{
  "id": "front-range",
  "name": "前向测距",
  "topic": "/front/range",
  "type": "sensor_msgs/msg/Range",
  "ttl_s": 1.5
}
```

`id` 在本目录内唯一，`name` 可选。驱动应已在相同 ROS 域发布该话题，消息类型应安装在控制台镜像内。控制台采用 ROS 2 best-effort、深度 1 的订阅。可以先写临时文件，再改名为 `.json`，一次完成清单更新。Compose 通过只读挂载读取此目录，主机端可用 `EDGE_OBSERVATIONS_DIR` 指定其他目录。

打开控制台传感器页，或查看 `GET /api/state` 的 `extra_sources.front-range`。第一帧到达前显示 `waiting`，超过 `ttl_s` 未收到第一帧或时间戳停止推进时显示 `stale`，继续收到新采样后返回 `online`。没有消息头的传感器使用本机接收时间，`frame` 和 `stamp_s` 为空。清单或 ROS 类型错误通过 `status=error` 和 `error` 字段说明。日志中的 `observation.added`、`online`、`stale`、`error` 及 `removed` 事件均带传感器 ID。

消息按 ROS 字段名转成 JSON。普通标量按原值展示，非有限浮点用字符串标明。为控制预览开销，每个数组或二进制字段最多展示前 32 项，字符串最多 256 个字符，整条消息最多访问 192 个值、8 层嵌套。任一限制触发时顶层 `truncated=true`。例如摄像头预览能看到消息头、宽高、编码及开头的像素字节。需要完整原始帧时可直接订阅原话题或录制 rosbag。该接入口只订阅和展示消息；要将新传感器的量测用于姿态融合或地图更新，还需在对应感知模块中实现算法。
