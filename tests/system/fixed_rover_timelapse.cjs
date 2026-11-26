// Speed up the original browser frames in order. Hold the real operator request and verification frames for reading.
'use strict';
const fs=require('node:fs/promises');
const path=require('node:path');
const {spawnSync}=require('node:child_process');
const assert=require('node:assert/strict');
const [frameDir,movie,mode]=process.argv.slice(2);
async function main(){
  const dir=path.resolve(frameDir), output=path.resolve(movie);
  const source=await fs.readFile(path.join(dir,'receipts.jsonl'),'utf8');
  const frames=source.trim().split('\n').map(line=>JSON.parse(line));
  const last=frames.at(-1);
  assert(last.state==='SUCCEEDED' && last.task && frames[0].state==='DRAFT');
  const duration=(last.at_ms-frames[0].at_ms)/1000, factor=Math.max(1,duration/90);
  const pauses=new Map(), milestones=[];
  const hold=(title,test,seconds,required=true)=>{
    const i=frames.findIndex(test);
    if(i<0){assert(!required,'missing browser milestone: '+title);return i;}
    pauses.set(i,Math.max(pauses.get(i)||0,seconds));milestones.push({title,index:i});
    return i;
  };
  const camera = row => row.panel==='camera' && row.camera;
  hold('输入自然语言',r=>r.panel==='form' && r.goal===last.goal,3);
  hold('首次摆板',r=>camera(r) && r.instruction.includes('车头正前方'),3);
  hold('确认摆板',r=>camera(r) && r.instruction.includes('已确认正面板位'),2);
  const moving = hold('底盘自主换位',r=>camera(r) && r.instruction.includes('底盘正在自主换位'),3);
  if(mode==='joint'){
    hold('识别 RGB 覆盖缺口',r=>camera(r) && r.step_count>=2 && /待覆盖区域\s.*/.test(r.readings||''),3);
    hold('换位后停稳采样',r=>camera(r) && r.index>moving && r.readings?.includes('到位见证') &&
         (r.instruction==='正在录制稳态样本' || /(?:[2-9]|\d{2,}) 组训练/.test(r.readings)),2);
    hold('采集独立留出组',r=>camera(r) && r.index>moving && /[123] 组留出/.test(r.readings||''),3);
    hold('确认可达视点无法补角',r=>camera(r) && r.readings?.includes('可补当前缺项 0 个') &&
         r.instruction.includes('倾斜标定板'),3);
    hold('按提示补俯仰',r=>camera(r) && r.instruction.includes('倾斜标定板') && r.guidance?.includes('上缘'),4);
    hold('按提示补滚转',r=>camera(r) && r.instruction.includes('倾斜标定板') && /[左右]上角/.test(r.guidance||''),4);
    hold('用训练组求解内外参',r=>camera(r) && r.index>moving && r.instruction==='正在求解标定参数',3,false);
  }
  hold('独立验证结果',r=>r.panel==='camera' && r.instruction.includes('独立验证通过') &&
       r.state==='SUCCEEDED' && (mode!=='joint'||r.readings?.includes('内参留出角点 RMS')),6);
  const lines=['ffconcat version 1.0'];
  const picture = index => "file '"+path.join(dir,String(index).padStart(5,'0')+'.jpg')
    .split(path.sep).join('/').replaceAll("'","'\\''")+"'";
  let maxGap=0, time=0;
  for(const [i,row] of frames.entries()){
    assert(row.index===i && (!row.task || row.task===last.task));
    const gap=(frames[i+1]?.at_ms || row.at_ms+1200)-row.at_ms;
    assert(gap>=0);
    maxGap=Math.max(maxGap,gap);
    const seconds=Math.max(.06,gap/1000/factor,pauses.get(i)||0);
    for(const milestone of milestones.filter(entry=>entry.index===i))milestone.second=+time.toFixed(2);
    time+=seconds;
    lines.push(picture(i),'duration '+seconds.toFixed(4));
  }
  lines.push(picture(frames.length-1));
  const list=path.join(dir,'workbench.ffconcat');
  await fs.writeFile(list,lines.join('\n')+'\n');
  const run=(bin,args)=>{const p=spawnSync(bin,args,{encoding:'utf8',windowsHide:true});if(p.error||p.status!==0)throw Error(p.error||p.stderr);return p.stdout;};
  run(process.env.FFMPEG_PATH||'ffmpeg',['-n','-v','error','-protocol_whitelist','file','-f','concat','-safe','0','-i',list,'-vf','fps=24,format=yuv420p','-c:v','libx264','-crf','22','-preset','fast','-movflags','+faststart',output]);
  const probe=JSON.parse(run(process.env.FFPROBE_PATH||'ffprobe',['-v','error','-count_frames','-show_entries','stream=width,height,codec_name,nb_read_frames,r_frame_rate:format=duration,size','-of','json',output]));
  assert(probe.streams[0].codec_name==='h264' && Number(probe.streams[0].nb_read_frames)>frames.length);
  const summary={task:last.task,mode:'chronological workbench time-lapse with held original frames',
    source_frames:frames.length,source_duration_seconds:duration,max_capture_interval_ms:maxGap,
    held_original_frame_seconds:Object.fromEntries(pauses),milestones:milestones.sort((a,b)=>a.index-b.index),...probe};
  await fs.writeFile(output.replace(/\.mp4$/i,'.json'),JSON.stringify(summary,null,2)+'\n');
  console.log(JSON.stringify(summary));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
