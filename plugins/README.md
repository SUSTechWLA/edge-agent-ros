# 热加载的能力清单

将 JSON 文件放入本目录，下一次规划时会重新读取。插件以独立服务运行，端点由部署者配置。下面的示例约定提供者实现 HTTP POST，接收 `operation_id`、`version` 和 `arguments`。相同操作 ID 在提供者重启后仍须复用同一执行结果。有副作用的能力仅在提供者具备持久化幂等和按 ID 对账能力时登记为 idempotent。

```json
{"id":"read_temperature","version":"1","description":"读取室温","effect":"read","parameters":{},"outcomes":{"temperature":"celsius"},"endpoint":"http://temperature:8092/invoke"}
```

成功回执形如 `{"operation_id":"example/step/1","state":"SUCCEEDED","evidence":{"celsius":23.4}}`。进行中的操作返回 RUNNING。输入契约支持 string、number、boolean。新文件用临时文件写好后原子改名，删除清单即可撤销后续调度。纯软件服务、传感查询和硬件网关均可独立封装。端点地址只从本机配置读取。

若需替换内置的状态读取或巡检能力，清单可使用对应 ID，保持原参数和 effect 契约。`status` 是 read、无参数；`inspect` 是 idempotent，参数为 `{"points":"xy_list"}`。服务可以用不同语言、不同导航栈在本地或可达设备上实现。

每个新规划保存能力 ID、版本、参数、effect 和提供者端点的绑定摘要。更新清单后新任务可以采用新实现，在途任务仍要求原绑定；缺失时先退避。如果要切换正在执行的能力，应先等它返回可核查的结果。更换同一 URL 后面的服务仍需升级版本，并让新旧部署共用原操作账本或提供旧版本端点。稳定操作 ID 的去重和结果查询需要提供者履约。

可用 `GET /agent/capabilities` 检查当前目录。独立测试会动态安装提供者，首次调用注入 503，随后核对重试的操作 ID、证据，最后卸载能力。[运行测试](../tests/console_resilience.py)

### 让产物参与任务验收

`outcomes` 把任务要求的产物映射到成功回执里的证据字段。上面的测温插件要求 `evidence.celsius`。相机可声明 `"outcomes":{"image":"image_uri"}`，提供者应在实际保存后返回可读取的图像引用，必要时附上采集时间和文件摘要。运行时检查约定字段；图像内容和引用的可访问性由相机提供者及其验收测试保证。

新领域还可声明 `"outcome_terms":{"soil_moisture":["土壤含水量","土壤湿度"]}` 与 `"outcomes":{"soil_moisture":"volumetric_percent"}`。原请求命中这些词时，计划须含能提供该产物的步骤。内置规则能识别常见的照片、视频、抓取、温度和电量需求，即使相应模块尚未安装，也能指出缺少哪类能力。[拍照误报的修复与实测](../docs/model.md)
