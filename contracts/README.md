# 本地运行时契约

仿真和硬件部署把本目录只读挂载到 Agent 的 `/etc/edge/contracts`。可用 `EDGE_CONTRACTS_DIR` 指向设备专用目录。在文件系统里写好临时文件并原子改名，新规划会读取更新。模型访问密钥应由受信提供者管理。具体的模型选择、提示版本和诊断覆盖格式见 [Agent 插件契约](../agent/README.md)。
