# 独立验收运行记录

每次运行 `scripts/verify.ps1` 都在这里建立一个运行号目录，保存本轮任务、轨迹、容器日志和验收结果。复盘时先打开该次的 `result.json`（若运行中断可能缺失），再按任务或操作 ID 查同目录的现场文件。

| 要查什么 | 路径 |
|---|---|
| 两路三维雷达的点能否取得正确的相机颜色，外参误差会造成什么影响 | [Gazebo 双站点地图、实物导出与真值对照](rgb-lidar-fusion-20261124/README.md) |
| 未读取地图时，工作台能否直接规划并画出车体局部路线 | [Gazebo 空场的浏览器请求与渲染结果](map-optional-planning-20261124.json) |
| 双雷达与 RGB-D 缺少配准时怎样处理、停用纹理后局部规划是否可用 | [归档工作台、插件卸载与规划复测](scene-layers-20261123/README.md) |
| 常驻局部规划响应多快、平滑曲线离障碍多近 | [配对复算、响应时间与前端回放](rover-smooth-planning-20261123/README.md) |
| 按灵遨车头、车侧和车尾规划后，绕箱少偏了多远 | [同车模、同定位、同障碍的真值对照](lingao-footprint-20261123/local/README.md) |
| 对 Agent 说「躲避前方障碍物」实际发生了什么 | [同一指令的静态、横穿、停车与恢复回执](rover-maneuver-20261123/local/README.md) |
| 轮式车在空场的直行、中央箱体的绕行净距与到达误差 | [校园 Gazebo 首轮真值验收](rover-local-plan-20261123/local/README.md) |
| 一条巡检任务从中文下发、超时重试、在途 Agent 重启直至归位 | [带章节的录像](rover-long-mission/continuous-02/video/index.html) · [七站世界位姿审计](rover-long-mission/continuous-02/waypoint-audit.json) |
| 工作台上实际怎样提交中文；机器人如何依据地图自主选点 | [工作台只读任务操作](rover-console-recording/20261119T020956Z/console-position-task.mp4) · [探索与取消的两轮补充实验](commission-exploration/filmed-01/video/rover-exploration.mp4) |
| 车体回弹时的取消失败 | [第一次实测](20261117-031103-3a9cc1/cancel.json) |
| 修复后的四次取消及该轮综合结果 | [取消实测](20261117-031532-ae63c8/cancel.json) · [综合结果](20261117-031532-ae63c8/result.json) |
| 这两次运行的环境、判断标准和解释 | [联合验收报告](../../docs/v1/console-validation.md) |
| 校园模型、往返穿门、机体动作和三处激光测站 | [原始验收数据](unified-campus/final.json) · [读数方法与复测步骤](../../docs/testing/campus-robot-view.md) |
| 四种机体的传感器、桥中断、自然语言诊断和场景重载 | [最终运行时的原始闭环记录](edge-field/campus-qualified.json) · [升级与现场验收](../../docs/testing/edge-field-readiness.md) |

完整运行留在生成它的机器上。要让同事通过 Git 复现一个判断，先检查所选文件是否含凭据、私人任务或设备信息，然后在本目录的 `.gitignore` 末尾逐文件加入 `!/<运行号>/<文件名>`，在报告里直接引用这个文件。精选原件和同一轮的其他日志共用运行号和路径。运行整套 `tests/system_suite.py` 的汇总、各阶段日志及夹具仍按该套件自身的运行号保存在 `.ci-results/system/runs/`。
