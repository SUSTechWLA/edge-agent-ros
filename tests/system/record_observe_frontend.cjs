/* Record the live planning page, including the real submission and terminal receipt. */
'use strict';
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {spawn,spawnSync}=require('node:child_process');
const {once}=require('node:events');
const {performance}=require('node:perf_hooks');

const flags=Object.fromEntries(process.argv.slice(2).map(arg=>{
  const eq=arg.indexOf('=');
  if(!arg.startsWith('--')||eq<3)throw Error('Expected --name=value');
  return [arg.slice(2,eq),arg.slice(eq+1)];
}));
const digest=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const terminal=new Set(['SUCCEEDED','BLOCKED','FAILED','REFUSED','CANCELED','CANCELLED','RECOVERY_REQUIRED']);
const save=(p,v)=>fs.writeFileSync(p,JSON.stringify(v,null,2)+'\n');

async function record(){
  if(!flags.url||!flags.text||!flags.out||!flags.browser)throw Error('Supply --url, --text, --out and --browser');
  const ui=new URL(flags.url);
  if(!['http:','https:'].includes(ui.protocol)||ui.username||ui.password||ui.search||ui.hash)throw Error('Expected a clean planning page URL');
  const out=path.resolve(flags.out);
  fs.mkdirSync(out,{recursive:false});
  const puppeteer=require(flags.puppeteer||'puppeteer-core');
  const ffmpeg=flags.ffmpeg||'ffmpeg', ffprobe=flags.ffprobe||'ffprobe';
  for(const program of [ffmpeg,ffprobe])if(spawnSync(program,['-version'],{windowsHide:true,stdio:'ignore'}).status!==0)throw Error('Media tool unavailable: '+program);
  const width=1600,height=900,fps=8;
  let browser,writer,capturing,stopped=false,encodedExit,frames=0,receipt,id,started=0;
  const stages=[], errors=[], captured=[];
  const video=path.join(out,'frontend.mp4');
  const metadata={url:ui.href,source:'Chromium Page.captureScreenshot',capture_fps:fps,width,height,
    start_utc:null,task_text:flags.text,task_id:null,timebase:'monotonic wall time',stages,errors};
  function mark(kind,more={}) {stages.push({kind,sec:+((performance.now()-started)/1000).toFixed(3),...more});}
  try{
    browser=await puppeteer.launch({headless:true,executablePath:flags.browser,defaultViewport:{width,height,deviceScaleFactor:1},
      args:['--disable-background-networking','--disable-component-update','--no-first-run','--hide-scrollbars','--disable-gpu']});
    const page=await browser.newPage();
    page.setDefaultTimeout(20000);
    if(flags.credentials){
      if(flags.origin!==ui.origin)throw Error('Login requires --origin equal to the intended console origin');
      const {username,password}=JSON.parse(fs.readFileSync(flags.credentials,'utf8').replace(/^\uFEFF/,''));
      if(typeof username!=='string'||typeof password!=='string')throw Error('Expected username and password fields in the credential file');
      await page.goto(new URL('/login',ui).href,{waitUntil:'domcontentloaded'});
      if(new URL(page.url()).origin!==ui.origin)throw Error('Unexpected login origin');
      const ok=await page.evaluate(async credentials=>{
        const response=await fetch('/login',{method:'POST',body:new URLSearchParams(credentials),credentials:'same-origin'});
        return response.ok;
      },{username,password});
      if(!ok)throw Error('Console login failed');
    }
    await page.goto(ui.href,{waitUntil:'domcontentloaded'});
    await page.waitForSelector('#task-text');
    await page.evaluate(()=>document.fonts.ready);
    await page.waitForSelector('#connection.online',{timeout:25000});
    const cdp=await page.createCDPSession();
    const log=fs.openSync(path.join(out,'encode.log'),'w');
    try{
      writer=spawn(ffmpeg,['-hide_banner','-loglevel','warning','-y','-f','image2pipe','-framerate',String(fps),'-vcodec','mjpeg','-i','pipe:0',
        '-an','-c:v','libx264','-preset','veryfast','-crf','17','-pix_fmt','yuv420p','-r','24','-movflags','+faststart',video],
        {stdio:['pipe','ignore',log],windowsHide:true});
    }finally{fs.closeSync(log);}
    encodedExit=once(writer,'close');
    started=performance.now();metadata.start_utc=new Date().toISOString();
    function writeFrame(buf){
      if(writer.exitCode!==null)throw Error('Video encoder stopped');
      return writer.stdin.write(buf)?Promise.resolve():once(writer.stdin,'drain');
    }
    capturing=(async()=>{
      let previous;
      while(!stopped){
        const {data}=await cdp.send('Page.captureScreenshot',{format:'jpeg',quality:90,fromSurface:true});
        const shot=Buffer.from(data,'base64'),now=performance.now(),index=Math.floor((now-started)*fps/1000);
        while(frames<index){await writeFrame(previous||shot);frames++;}
        await writeFrame(shot);frames++;previous=shot;
        await sleep(Math.max(0,started+frames*1000/fps-performance.now()));
      }
      return previous;
    })();
    const captureFailure=new Promise((_,reject)=>capturing.catch(reject));
    async function flow(){
      let receivedUtc='',poseReceipt;
      page.on('response',async r=>{
        try{
          const u=new URL(r.url());
          if(u.origin!==ui.origin||!r.ok())return;
          if(u.pathname==='/field/pose-window'&&id&&u.searchParams.get('id')===id){
            const result=await r.json();if(result?.status){poseReceipt=result;save(path.join(out,'pose-window.json'),result);}
            return;
          }
          if(u.pathname!=='/agent/tasks')return;
          const t=await r.json();
          if(t.text!==flags.text||!t.id||id&&t.id!==id)return;
          receivedUtc=new Date().toISOString();
          if(!id){id=t.id;metadata.task_id=id;mark('agent.accepted',{id});}
          const previous=captured.at(-1);
          const summary={state:t.state,events:(t.events||[]).map(e=>e.seq),tools:(t.steps||[]).map(s=>s.tool)};
          if(!previous||JSON.stringify(summary)!==JSON.stringify(previous)){
            captured.push(summary);
            fs.appendFileSync(path.join(out,'http-observations.jsonl'),JSON.stringify({received_utc:receivedUtc,
              sec:(performance.now()-started)/1000,task_id:id,...summary})+'\n');
          }
          if(terminal.has(t.state)&&Array.isArray(t.events))receipt=t;
        }catch(error){errors.push('Task receipt: '+error.message);}
      });
      await sleep(900);mark('input.start');
      await page.focus('#task-text');
      await page.keyboard.type(flags.text,{delay:25});
      await sleep(1000);
      fs.writeFileSync(path.join(out,'submission.jpg'),await page.screenshot({type:'jpeg',quality:91}));
      mark('form.submit');
      await page.click('#submit');
      await page.waitForFunction(states=>states.includes(document.querySelector('#request')?.dataset.taskState),{timeout:Number(flags.timeout||300)*1000},Array.from(terminal));
      for(let i=0;i<60&&!receipt;i++)await sleep(150);
      if(!receipt||receipt.id!==await page.$eval('#request',node=>node.dataset.taskId))throw Error('Terminal page and HTTP receipt differ');
      mark('agent.terminal',{state:receipt.state});
      await page.waitForFunction(()=>['complete','closed'].includes(document.querySelector('#request')?.dataset.poseWindow),{timeout:8500});
      if(!poseReceipt){
        const read=await page.evaluate(async id=>{const r=await fetch('/field/pose-window?'+new URLSearchParams({id}),{credentials:'same-origin',cache:'no-store'});return r.ok?await r.json():null;},id);
        if(read?.status){poseReceipt=read;save(path.join(out,'pose-window.json'),read);}
      }
      if(poseReceipt)metadata.pose_window_sha256=digest(fs.readFileSync(path.join(out,'pose-window.json')));
      metadata.pose_window_status=poseReceipt?.status||'unavailable';
      await sleep(4200);
      const final=await page.screenshot({type:'jpeg',quality:94});
      fs.writeFileSync(path.join(out,'terminal.jpg'),final);
      metadata.final_ui=await page.evaluate(()=>Object.fromEntries(['state','message','start-value','end-value','goal-value','motion','source','rgb-source'].map(id=>[id,document.getElementById(id).textContent])));
      save(path.join(out,'task.json'),receipt);
      metadata.receipt_sha256=digest(fs.readFileSync(path.join(out,'task.json')));
      metadata.terminal_frame_sha256=digest(final);metadata.last_http_utc=receivedUtc;
    }
    await Promise.race([flow(),captureFailure]);
  }catch(e){metadata.error=e.message;throw e;}
  finally{
    try{
      stopped=true;
      if(capturing)await capturing;
    }finally{
      if(writer){writer.stdin.end();const [code]=await encodedExit;metadata.encoder_exit=code;}
      if(browser)await browser.close();
      metadata.frames_written=frames;metadata.elapsed_video_s=frames/fps;
      if(fs.existsSync(video)&&fs.statSync(video).size){
        const info=spawnSync(ffprobe,['-v','error','-show_entries','stream=codec_name,width,height,duration,nb_frames','-of','json',video],{encoding:'utf8',windowsHide:true});
        metadata.media=info.status===0?JSON.parse(info.stdout):null;
        metadata.video_sha256=digest(fs.readFileSync(video));
      }
      save(path.join(out,'recording.json'),metadata);
    }
  }
  if(metadata.encoder_exit!==0||metadata.media?.streams?.[0]?.codec_name!=='h264')throw Error('MP4 verification failed');
  console.log(JSON.stringify({task_id:id,state:receipt.state,video,seconds:metadata.elapsed_video_s,frames}));
}
record().catch(error=>{console.error(error.message);process.exitCode=1;});
