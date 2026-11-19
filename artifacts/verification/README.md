# 独立验收运行记录

每次运行 `scripts/verify.ps1` 都在这里建立一个运行号目录，保存本轮任务、轨迹、容器日志和验收结果。复盘时先打开该次的 `result.json`（若运行中断可能缺失），再按任务或操作 ID 查同目录的现场文件。

| 要查什么 | 路径 |
|---|---|
| 一条巡检任务从中文下发、超时重试、在途 Agent 重启直至归位 | [带章节的录像](rover-long-mission/continuous-02/video/index.html) · [七站世界位姿审计](rover-long-mission/continuous-02/waypoint-audit.json) |
| 工作台上实际怎样提交中文；机器人如何依据地图自主选点 | [工作台只读任务操作](rover-console-recording/20261119T020956Z/console-position-task.mp4) · [探索与取消的两轮补充实验](commission-exploration/filmed-01/video/rover-exploration.mp4) |
| 车体回弹时的取消失败 | [第一次实测](20261117-031103-3a9cc1/cancel.json) |
| 修复后的四次取消及该轮综合结果 | [取消实测](20261117-031532-ae63c8/cancel.json) · [综合结果](20261117-031532-ae63c8/result.json) |
| 这两次运行的环境、判断标准和解释 | [联合验收报告](../../docs/v1/console-validation.md) |
| 校园模型、往返穿门、机体动作和三处激光测站 | [原始验收数据](unified-campus/final.json) · [读数方法与复测步骤](../../docs/testing/campus-robot-view.md) |
| 四种机体的传感器、桥中断、自然语言诊断和场景重载 | [最终运行时的原始闭环记录](edge-field/campus-qualified.json) · [升级与现场验收](../../docs/testing/edge-field-readiness.md) |

完整运行留在生成它的机器上。要让同事通过 Git 复现一个判断，先检查所选文件是否含凭据、私人任务或设备信息，然后在本目录的 `.gitignore` 末尾逐文件加入 `!/<运行号>/<文件名>`，在报告里直接引用这个文件。精选原件和同一轮的其他日志共用运行号和路径。运行整套 `tests/system_suite.py` 的汇总、各阶段日志及夹具仍按该套件自身的运行号保存在 `.ci-results/system/runs/`。
