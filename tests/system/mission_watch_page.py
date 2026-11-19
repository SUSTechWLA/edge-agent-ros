"""Build an offline viewing guide alongside the verified SDD-042 continuous video."""
import argparse
import html
import json
from pathlib import Path


def build(root):
    def read(path):
        return json.loads((root/path).read_bytes())
    run=read("acceptance.json")
    audit=read("waypoint-audit.json")
    video=read("video/video.json")
    if not (run["passed"] and audit["passed"] and run["task_id"]==audit["task_id"]):
        raise ValueError("an accepted mission and independent audit are required")
    chapters=[
        ("submitted","下发巡检","一条中文请求，拆成巡检和回报状态两个工具。"),
        ("retry","查询回执超时","测试暂停 Core，车在工位等待。Agent 记录失败并按原操作号重试。"),
        ("fault_agent","运动中 Agent 退出","车正在运动。留意 Core 和网关怎样处理已受理的目标。"),
        ("agent_restored","继续原任务","Docker 拉起 Agent。它读回账本，继续跟踪同一个巡检操作。"),
        ("waypoint","驶出工坊","第二个路点在门外。车穿过门廊，地图随新观测更新。"),
        ("verified","返回并验收","车回到工位，读取里程计；用 Gazebo 独立位置核对七站。")]
    waypoint=0
    times={}
    for event in video["markers"]:
        phase=event["phase"]
        if phase=="waypoint":
            waypoint+=1
            if waypoint!=2: continue
        times.setdefault(phase,event["video_seconds"])
    buttons=[]
    for phase,title,caption in chapters:
        t=times[phase]
        buttons.append(f'<button data-seek="{t:.3f}"><time>{int(t)//60:02}:{int(t)%60:02}</time>'
                       f'<span><b>{title}</b><small>{caption}</small></span></button>')
    rows=[]
    for goal in audit["goals"]:
        x,y=goal["world_xy"]
        rows.append(f'<tr><td>{goal["index"]}</td><td>{goal["goal_xy"][0]:.2f}, {goal["goal_xy"][1]:.2f}</td>'
                    f'<td>{x:.3f}, {y:.3f}</td><td>{100*goal["world_goal_error_m"]:.1f} cm</td></tr>')
    vals={"@BUTTONS@":"\n".join(buttons),"@ROWS@":"\n".join(rows),"@TASK@":html.escape(run["task_id"]),
          "@REQUEST@":html.escape(run["request"]),"@DIST@":f'{run["media"]["measured_world_path_m"]:.2f}',
          "@ERR@":f'{100*audit["stop"]["goal_error_m"]:.1f}',"@N@":str(len(audit["goals"])),
          "@RESTART@":str(run["restore"]["after"]["restarts"]-run["restore"]["before"]["restarts"])}
    page='''<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>七站巡检 · 实验录像</title><style>
*{box-sizing:border-box}body{margin:0;background:#edf1ef;color:#172c2b;font:17px/1.6 "Microsoft YaHei","PingFang SC",sans-serif}main{max-width:1520px;margin:auto;padding:35px clamp(16px,3.5vw,56px)}
h1{margin:0;font-size:clamp(29px,3.5vw,53px);font-weight:700;letter-spacing:-.04em}p{margin:10px 0}header p{color:#435b59}h2{font-size:21px;margin:0 0 10px}
.layout{display:grid;grid-template-columns:minmax(0,2.5fr) minmax(265px,1fr);gap:22px;margin:24px 0;align-items:start}figure{margin:0;background:#142529;border-radius:12px;overflow:hidden;color:#ebf4f1}video{width:100%;display:block;aspect-ratio:16/9;background:#142529}
figcaption{padding:13px 19px;font-size:15px}aside,details{border:1px solid #c4d0cb;border-radius:12px;padding:16px;background:#fafcfb}button{appearance:none;display:flex;width:100%;text-align:left;align-items:baseline;gap:13px;padding:9px 8px;border:0;border-top:1px solid #d5dfdb;background:transparent;color:inherit;cursor:pointer;font:inherit}
button:hover,button:focus-visible,button[aria-current=true]{background:#dcece5}button small{display:block;color:#48615c;font-size:13px;font-weight:400}button time,code{font-family:Consolas,monospace;font-size:14px;color:#176854}button time{font-variant-numeric:tabular-nums}
.result{padding:14px 18px;border-left:4px solid #26755d;background:#deece4;margin:18px 0}.request{font-size:15px;overflow-wrap:anywhere}summary{cursor:pointer;font-weight:bold}table{width:100%;border-collapse:collapse;font-size:15px;margin-top:12px}th,td{padding:6px;text-align:left;border-bottom:1px solid #cad5d0}
a{color:#10594a;text-underline-offset:3px}footer{font-size:14px;margin:16px 0} @media(max-width:820px){.layout{grid-template-columns:1fr}main{padding-top:20px}}
</style><main><header><h1>七站巡检，故障后怎样继续？</h1><p>工位出发，穿过门廊到庭院，再返回工位。跟随同一个任务 <code>@TASK@</code>。</p></header>
<div class="layout"><figure><video id="film" controls playsinline preload="metadata" poster="rover-long-mission-cover.png" src="rover-long-mission.mp4"></video>
<figcaption>左边是 Gazebo 观察相机，右边是车载感知生成的占据图。屋顶和近侧墙为观察相机作剖视，现场仍保留碰撞。全片沿用同一仿真时钟。</figcaption></figure>
<aside><h2>跳到现场</h2>@BUTTONS@</aside></div>
<p class="request"><b>现场提交的原文　</b>@REQUEST@</p>
<p class="result">一次下发，Agent 自动重启 @RESTART@ 次，网关完成 @N@ 个路点。世界真值轨迹长 @DIST@ 米，最终距工位目标 @ERR@ 厘米。</p>
<details><summary>查看每一站的独立位置证据</summary><p>每行用到点回执的里程计时间，匹配同一时刻的 Gazebo 世界位姿。坐标单位为米。</p><table><thead><tr><th>站</th><th>目标 x, y</th><th>世界实测 x, y</th><th>位置误差</th></tr></thead><tbody>@ROWS@</tbody></table></details>
<footer><a href="../task.json">任务与工具回执</a>　<a href="../waypoint-audit.json">逐站真值审计</a>　<a href="../acceptance.json">完整验收</a>　<a href="rover-long-mission.mp4">直接播放 MP4</a></footer></main>
<script>const film=document.querySelector('video'),buttons=[...document.querySelectorAll('button')];
buttons.forEach(button=>button.onclick=()=>{film.currentTime=Number(button.dataset.seek);film.play().catch(()=>{});});
film.addEventListener('timeupdate',()=>{const active=buttons.filter(b=>Number(b.dataset.seek)<=film.currentTime+.01).at(-1);for(const b of buttons)b.setAttribute('aria-current',String(b===active));});</script></html>'''
    for token,value in vals.items():
        page=page.replace(token,value)
    out=root/"video/index.html"
    out.write_text(page,"utf8")
    return out


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("run",type=Path)
    print(build(cli.parse_args().run))
