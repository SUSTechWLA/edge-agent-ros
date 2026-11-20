# 已发布的传感器观测与位置融合

两个可分别装卸的只读 Service 共享[空间观测契约](../../edge_contracts/spatial_facts.py)。调用方通过现有 DSH 风格的 `ctx.require` 绑定服务。下面的消费者可以是巡检工具、定位查询或路线规划。

```python
observations = ctx.require("sensing.observations")
localize = ctx.require("localization.position")

async def position_for(actor_ref):
    batch = await observations({"actor_ref": actor_ref})
    return await localize(batch)
```

| Service | 当前 Provider | 责任 |
| --- | --- | --- |
| `sensing.observations` | `sensing.replay@1.0.0` | 从已发布的站点配置读取主体的录制快照。用 `basis="replay"` 和录制时钟标记数据。订阅者可以单独读取媒体引用及其采样时间、坐标系和标定版本。 |
| `sensing.observations` | `sensing.published@1.0.0` | 通过站点私网网关读取实时发布的事实。网关按请求的主体和本机 Unix 时钟检查源时间与允许帧龄。 |
| `localization.position` | `localization.weighted@1.0.0` | 接受符合契约的观测批次，按主体策略选择位置来源，输出位置、各轴方差、原始来源、最老输入时间和有效截止时间。 |

快照由 `actors[actor_ref]` 索引，每个主体有 `clock_id`、`as_of_ns`、`samples`。批次最多 32 条，每条包含来源 ID、标定引用、源时间和坐标系。位置输入表达**同一机体参考点**在指定地图、坐标系和定位代次内的二维或三维笛卡尔坐标与各轴方差。工件输入包含内容摘要和媒体类型，可用来共享点云或图像引用。工件访问权限由发布者管理。首个回放 Provider 不打开文件或设备。

录制配置可以用十进制字符串保存 `as_of_ns` 和 `stamp_ns`。例如 `"1770911200000000001"` 可无损存入站点 JSON。融合在内部用整数做时间运算，Agent 工具在回执中输出十进制字符串，浏览器、重放程序与其他消费者因而能保留纳秒精度。

融合策略由 `actors[actor_ref]` 选择，包含 `map_ref`、`frame_id`、`localization_epoch`、`clock_id`、`max_age_ns`、`max_skew_ns` 和 `sources`。其中 `sources` 将每个必需位置源 ID 绑定到标定版本。部署者需确认所选估计误差相互独立，且时间跨度内运动引入的偏差可忽略。相同里程计经两个转换得到的估计具有相关性，须选择其中一个。原始里程计、点云、RTK 经纬度的时间对齐与坐标转换由相应估计 Provider 完成。

算法先逐项核对标定、采样时钟、地图、坐标系、定位代次、时效与维数。任一必需来源缺失都返回明确错误。任意两份估计若在某轴相差超过五倍合成标准差，同样返回错误。通过后各轴按逆方差加权。录制时钟的有效期仅在该时钟域内解释。输出只描述位置，姿态、速度和更一般的协方差需要其他估计 Provider。

新增观测生产者可独立发布同一批次契约。读取实时 ROS、相机设备或原始传感器的适配器归物理 runtime 所有，持有其资源租约，并负责登记采集证据。此处的消费者读取已获准发布的事实。消费者选用其他融合方法时，通过站点绑定换 Provider，保留已有任务的冻结版本。

可用 `python tests/test_spatial_facts.py -v` 复测两源二维及三维融合，独立图像引用读取，以及地图、时钟、标定、定位代次或输入相互矛盾时的拒绝行为。
