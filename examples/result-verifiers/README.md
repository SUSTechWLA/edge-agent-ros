# 给同一个相机工具安装两个独立验收插件

读完这个例子，你可以在相机保持在线的情况下，加装图像解码或摘要验收，定位一条回执为何通过、被拒绝或需要核查。两个插件各自提供一个 HTTP 服务，对 LLM 暴露的 Tool 数量都是 0。[SDD-006](../../docs/specs/SDD-006-runtime-plugins.md)

| 插件、服务 | 实际读取和计算 | 结论示例 |
|---|---|---|
| `image-check` / `verify.image` | 根据 `outcomes.image` 定位候选字段，打开该操作的产物，按 P5 的宽、高、最大灰度解析像素并计算整文件 SHA-256。 | 像素数量和取值范围符合头部，返回宽高、像素取值范围和摘要；声明为 PGM 的文件内容损坏则 `fail`。 |
| `digest-check` / `verify.digest` | 独立打开同一操作的产物，对实际字节重算 SHA-256，与候选 `evidence.sha256` 比较。 | 一致返回摘要和字节数；不一致返回 `digest.mismatch`、声明值与实际值；缺少可比较的摘要时为 `unknown`。 |

## 一次拍摄的文件约定

采集方收到 `operation_id` 后，用其 UTF-8 字节的 SHA-256 十六进制摘要作为产物目录名，例如 `<scope>/frame.pgm`。先在目录中写临时文件，完成后原子改名，再返回 `evidence.image_uri = "artifact://<scope>/frame.pgm"`。相机清单的 `outcomes.image` 值要指向相应证据字段，例如 `image_uri`。部署摘要插件时，采集方还要在 `evidence.sha256` 给出完整文件的 64 位小写十六进制摘要。一份回执可以交给两个插件分开核对。

这里的交付约定是每个操作发布一份不可变产物。相机重投同一操作时复用原件。两个验收服务均以只读方式打开同一个目录，限定文件名、操作作用域、单链接普通文件及 4 MiB 上限。一个服务已通过后，如果采集方仍能覆写文件，两个服务可能看到不同的字节；上线应使用写一次的存储或同一不可变对象版本。这个例子验证文件归属、可解码性和内容摘要。图像是否拍到了指定对象、相机采集时间是否与机体时钟同步，需要有相应观测的独立验收服务。

`image-check` 的样例格式为小型二进制 PGM（P5，单通道，每像素 1 字节，扩展名 `.pgm`）。例如 `b"P5\n2 2\n255\n\x00\x01\x02\xff"` 表示宽高为 2 的图像，四个像素亮度分别是 0、1、2、255。超过大小限制、16 位 PGM 或其他扩展名时返回 `unknown`。在已经正确挂载的存储中找不到已交付的文件，或引用属于另一操作时返回 `fail`；挂载目录不可读取时返回 `unknown`。

## 接入本地仿真工作台

以下命令在已部署 SDD-006 镜像的项目根目录运行，使用 PowerShell 7、仿真 Compose、默认目录和项目网络。该覆盖文件使用 `verification-artifacts` 命名卷；相机服务应把同一卷挂载为可写，两个验收容器以只读方式使用它。验收进程默认使用 UID/GID 65534，采集方需给该身份读取产物的权限；可设置 `EDGE_VERIFIER_USER` 选择已有采集组。测试夹具自带模拟相机，日常工作台需自行提供能够产出图像的工具。Compose 服务和注册清单均按插件分别启停。

```powershell
$base = 'examples/result-verifiers'
$composeArgs = @('compose', '-f', 'compose.yaml', '-f', "$base/compose.yaml")
docker @composeArgs up -d --no-deps image-check

function Publish-LocalFile([string]$source, [string]$destination) {
    $path = [IO.Path]::GetFullPath($destination)
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path)) | Out-Null
    $tmp = "$path.$([guid]::NewGuid()).tmp"
    [IO.File]::Copy([IO.Path]::GetFullPath($source), $tmp)
    [IO.File]::Move($tmp, $path, $true)
}
Publish-LocalFile "$base/manifests/image-check.json" 'plugins/runtime/image-check.json'
Invoke-RestMethod -NoProxy http://127.0.0.1:8765/agent/plugins | ConvertTo-Json -Depth 10
```

新图像任务由内置规则要求 `verify.image`。第二个插件新增摘要要求时，先启动服务并注册，再发布现场策略。已有 `contracts/verification-policy.json` 时，先把示例的 `outcomes.image` 数组与现有规则合并，将合并结果作为发布源，保留其他工具和产物的验收要求。

```powershell
docker @composeArgs up -d --no-deps digest-check
Publish-LocalFile "$base/manifests/digest-check.json" 'plugins/runtime/digest-check.json'
if (!(Test-Path contracts/verification-policy.json)) {
    Publish-LocalFile "$base/verification-policy.json" 'contracts/verification-policy.json'
}
```

调试时用 `docker @composeArgs stop digest-check` 模拟进程离线，此时清单仍在，实际调用会报告不可用。需要永久卸载时，先把新任务的策略调整到目标版本，待已绑定该服务的任务完成或进入明确待核查状态，再撤下对应清单、停止对应服务。只删除清单并不会自动放宽必需验收。现场端如果使用宿主网络或另一种存储布局，将服务端点、进程端口和数据挂载同时调整为该部署的值。

## 复测

有任务在途时，可先把已安装清单的 `draining` 设为 `true` 再原子发布。随后不带摘要的新查询会得到 `plugin.draining`，已绑定任务仍可凭原摘要取回端点、验收旧候选。等待这些操作完成后再撤清单、停容器；只变更 `draining` 不会改变服务身份。移除服务前若需要同时覆盖外部非 Agent 消费者，也要向各消费者确认它们的在途调用。[共享服务协议](../../docs/specs/SDD-006-runtime-plugins.md)

下面的命令在 Linux 容器运行图像格式、双服务独立判断、重投、操作绑定、读写范围和异常文件共 10 项白盒测试。部署样例的两个服务都没有设备或镜像管理权限。

```powershell
$src = (Resolve-Path examples/result-verifiers).Path
docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --user 65534:65534 --mount "type=bind,source=$src,target=/sample,readonly" --entrypoint python3 edge-agent-ros:v0.1 -B /sample/test_verifiers.py -v
```
