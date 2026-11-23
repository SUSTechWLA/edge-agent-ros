# Jetson 只读感知与插件创作站点

本入口复用已配置的 DSH 站点、带校准的相机读者、独立实况注册表、站点只读策略以及经鉴权的 Console。仓库提供应用与启动定义；图像、相机标定、账号、模型权重、站点策略和凭据留在站点。Jetson 的 Docker 项目为 `edge-nav-spatial`。使用新的项目名会创建一套新的卷与网络，因此恢复时须沿用原名和注册表。

## 在已有站点核对版本和配置

在 Linux 上从代码发布目录运行以下示例。基础 `site.env` 包含镜像、站点路径、ROS 镜像域、校准配置及只读接口。首次创建站点时，应先用 DSH 正常部署及激活基础软件包，填入与已审核站点匹配的锁；现场已经完成了这一步并保存了后来增量发布的注册表。

```sh
field="$HOME/edge-integration/field-20261123"
autonomy="$HOME/edge-integration/autonomy-20261120"
console="$HOME/edge-integration/jetson-workstation-console/source/console"
export EDGE_FIELD_SITE_ENV="$autonomy/site.env"
export EDGE_FIELD_RELEASE_ENV="$field/release.env"
```

以下命令可在新部署工作目录生成 release.env。版本文件由站点发布者保存。若在恢复现有站点，应先比对已记录的镜像与挂载，保留已经确认的配置。旧 ARM 镜像中的 gateway、odom-reader 和 review-feed 以对应发布源文件的只读挂载补齐。启动前应核对三份文件的 SHA-256，或在更新发行版时重做校验。

```sh
cat >"$EDGE_FIELD_RELEASE_ENV" <<EOF
EDGE_NAV_LOCK=7d7f9dfd66914511e314142ba1724a3d5629aa0a93e874f46cf398cae44036ae
EDGE_READ_AGENT_IMAGE=$(cat "$field/image-v2.txt")
EDGE_READ_PLANNER_DIR=$autonomy/planner-profile
EDGE_FIELD_AUTHOR_IMAGE=$(cat "$field/author-image.txt")
EDGE_FIELD_FEED_IMAGE=$(cat "$field/field-image.txt")
EDGE_FIELD_SPATIAL_IMAGE=sha256:a8b7c711a90d3db5807ef92cf2ab3491556cf3424ef016cd3a393e51ec10242f
EDGE_FIELD_LIVOX_IMAGE=sha256:a8b7c711a90d3db5807ef92cf2ab3491556cf3424ef016cd3a393e51ec10242f
EDGE_FIELD_ADAPTER_COMMIT=af062b577c9565c95ce9b49170126a894a363797
EDGE_FIELD_FAMILIES=$field/families.json
EDGE_FIELD_AUTHOR_STATE=$field/author-state
EDGE_FIELD_SELECTION_DIR=$field/selection
EDGE_FIELD_REVIEW_DIR=$console/web/records
EDGE_FIELD_FACTS_MODULE=$field/gateway.py
EDGE_FIELD_ODOM_MODULE=$field/odom_capture.py
EDGE_FIELD_REVIEW_MODULE=$field/field_review_feed.py
EDGE_FIELD_SITE_GID=1000
EDGE_FIELD_MODEL=qwen3:4b-instruct
EDGE_OLLAMA_URL=http://edge-read-llm:11434
EDGE_SPATIAL_PRIMARY_SOURCE_ID=wheel.odometry
EOF

sha256sum "$field/gateway.py" "$field/odom_capture.py" "$field/field_review_feed.py"
sh scripts/field-perception-compose.sh config --quiet
sh scripts/field-perception-compose.sh config --services
```

示例中的 `EDGE_NAV_LOCK` 是 11 月 23 日增量发布回执 `published-plugin.json` 中的 `lock_digest`，已与该站点当前 `pack.lock.json` 的规范化摘要交叉核对。旧 `site.env` 保留了此前的锁。以后有新发布时，从当次受信回执取新值并与实际安装核对；复原时将整套站点、策略、注册表和账本作为同一版本恢复。只启动常驻容器或独立发布者无需重新执行 `activate`。

本次现场的三个文件按上面顺序分别是 `78e0e232931d03351779ee7b4d79b383d01f0e3c108cb2968c1c4cf5e77a8493`、`93e9073111842f3054854691618d2c9b92b8c9ad8a5ede183162abd5c6e14600` 和 `3ecf55f7d3d93d1b854305f10cbd967e1489bf26199c68154278b56c6521d9c5`。现场基础模型服务 `edge-read-llm` 和 Agent 同属 `edge-nav-spatial_agent-api`，规划器从 `planner-profile/planner-profile.json` 读取实际模型参数。发布者专用镜像内已固化 watcher、发布器以及受信适配器。

## 启动和验收

已运行的 Jetson 通过 Docker 的 `unless-stopped` 恢复常驻服务。新接管一个已有读者、Agent 和 Ollama 的站点时，先创建并确认所列目录权限，把仓库中的 `profiles/field-read-families.json` 作为站点审核文件安装，确认 Agent 加载本次可选 family 配置。作者与数据服务逐个使用固定容器名，以确保一个站点只有一个活跃作者。若部署前已有同名独立容器，沿用原服务或安排受控的停机交接。

```sh
# 仅在接管已准备好且这两个容器名可用的站点时执行
sh scripts/field-perception-compose.sh up -d --no-deps field-author field-review-feed

docker inspect -f '{{.Name}} {{.State.Status}} {{.HostConfig.RestartPolicy.Name}}' \
  edge-nav-spatial-agent-1 edge-nav-spatial-soft-host-1 edge-nav-spatial-spatial-facts-1 \
  edge-read-llm edge-field-author edge-field-review-feed jetson-console jetson-console-session jetson-console-gateway
```

两个附加服务均在私有应用网桥，无硬件设备、宿主网络、Docker socket 或公开端口。发布者只能使用站点、注册表、只读插件授权目录和自己的状态目录完成事务；它有站点目录所需的组与 `CHOWN` 权限。审阅服务只有 Console 的 `web/records` 可写。把 `{"task_ids":["第一个任务ID","第二个任务ID"]}` 或 `{"since":"2026-11-23T03:45:00+00:00"}` 写入 `selection/session.json`，它会将选定任务的账本、创作回执、带摘要校验的帧投影到 `web/records/manifest.json`。创建新的录制时原子替换该文件即可，挂载的是父目录。

`EDGE_SPATIAL_PRIMARY_SOURCE_ID=wheel.odometry` 在这个站点启用独立源检查。相机等任一源须通过时效和内容校验方可返回，里程计缺失时不会补造姿态或时间戳。仍要求里程计的插件独立检验其依赖。原来未采用此配置的部署保留原始主源要求。

## 已鉴权 Console 的开机恢复

将 `console/web/` 中已审查的界面更新部署到现有 Console 挂载目录，保留数据目录、鉴权网关和持久卷。同一 LAN 经 `http://192.168.0.144:8767/login` 登录后打开 `/field-review.html`。Agent API 只发布在 Jetson 本机的 8794 端口。网关、Console 及 session 进程均设置 `unless-stopped`。额外的 user systemd 服务在 LAN 地址晚于 Docker 出现时重新连接既有网关。

现场已经安装该恢复服务。在相同架构的新站点，可安装仓库中的 `scripts/edge-agent-console-online.service` 到 `~/.config/systemd/user/`，复制 `scripts/ensure-console-gateway.sh` 到自己的发布目录，并在 `~/.config/edge-agent/console-online.env` 填入该站点的四项参数。示例沿用 Jetson 的实际值。

```ini
EDGE_CONSOLE_HELPER=/home/cas/edge-integration/field-20261123/ensure-console-gateway.sh
EDGE_CONSOLE_GATEWAY=jetson-console-gateway
EDGE_CONSOLE_NETWORK=edge-nav-spatial_agent-api
EDGE_CONSOLE_LOGIN_URL=http://192.168.0.144:8767/login
```

新站点应由具备权限的管理员启用 Docker 和对应用户的 linger，再执行 `systemctl --user daemon-reload` 和 `systemctl --user enable --now edge-agent-console-online.service`。验收时查询 `systemctl is-enabled docker`、`loginctl show-user "$USER" -p Linger`、`systemctl --user show edge-agent-console-online.service -p UnitFileState -p ActiveState -p Result`，并从 LAN 核查登录页。这些检查可在机器运行时完成，断电恢复属于独立演练。
