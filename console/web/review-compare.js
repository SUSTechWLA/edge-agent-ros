(() => {
  "use strict";
  const $=id=>document.getElementById(id);
  const set=(id,value)=>{$(id).textContent=value??"";};
  const make=(name,cls,value)=>{const el=document.createElement(name);if(cls)el.className=cls;if(value!=null)el.textContent=value;return el;};
  const ids=new URLSearchParams(location.search).getAll("archive");
  const offline=$("comparison-data");
  const sealed=Boolean(offline||ids.length);
  const validArchive=!ids.length || (ids.length===1 && /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(ids[0]));
  const path="records/review-compare/"+(ids.length && validArchive ? "archives/"+ids[0]+"/":"")+"manifest.json";
  const manifestURL=new URL(path,location.href);
  const mediaBase=new URL(".",manifestURL);
  const imageBundle=offline ? JSON.parse($("comparison-images").textContent):null;
  const originNames={builtin:"系统自带插件",site_workflow:"站点工作流",agent_composed:"Agent 组合工具",workflow:"站点工作流",composed:"Agent 组合工具",agent:"Agent 组合工具",system:"系统自带插件",user:"站点工作流"};
  const stages={
    "submitted":"用户提交自然语言任务","task.submitted":"用户提交自然语言任务",
    "planner.context":"读取授权能力目录","planner.family_selected":"识别目标契约",
    "provider.selected":"选择感知实现","planner.selected":"选择感知实现","planner.provider_selected":"选择感知实现",
    "graph.authored":"模型生成工具组合","graph.proposed":"模型提出组合图","graph.validated":"校验组合图",
    "model.request":"调用车端模型","model.response":"收到车端模型结果","tool.request":"派发冻结绑定",
    "tool.response":"工具返回观测结果","verified":"独立核验任务结果","measurement":"返回障碍复核结果",
    "provider.disabled":"站点停用首选插件","provider.retired":"站点停用首选插件","operator.disable":"站点停用首选插件",
    "operator.preference":"站点调整验证偏好","provider.fallback":"选择可用的备用实现","plugin.fallback":"选择可用的备用实现",
    "comparison.verified":"同帧测量对照已核验","task.failed":"任务未完成","blocked":"任务阻塞",
    "planner.implementation_selected":"Agent 选定可用实现","planner.graph_composed":"车端模型生成工具组合",
    "site.preference":"部署方调整验证偏好","site.drain":"部署方停用首选插件"
  };
  const operationTitles={measure_p10:"测量分区第 10 百分位",measure_median:"测量分区中位数",classify_near:"判定近距、远距与未知",rank_nearest:"按距离排列复核顺序",rank_image:"按画面位置排列"};
  let data=null,events=[],cursor=-1,follow=!sealed,playing=0,view="decision",cached="",pending=null,acking=false;
  let mediaKey="",mediaTicket=0,mediaResult=null,imageURL="",mediaError="";
  const hash=ref=>typeof ref==="string" && /^sha256:[a-f0-9]{64}$/.test(ref);
  const short=ref=>hash(ref)?"sha256:"+ref.slice(7,19)+"…":ref||"";
  const val=n=>typeof n==="number" && Number.isFinite(n)?n.toFixed(3)+" m":"未知";
  const fmt=utc=>{const dt=new Date(utc);return Number.isFinite(dt.getTime())?new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(dt):"";};
  const time=utc=>Date.parse(utc);
  const origin=r=>originNames[r?.origin]||r?.origin||"来源待核对";
  const current=()=>events[cursor];
  const spanTitle=(el,value)=>{el.title=value??"";return el;};
  function status(text,cls=""){set("status",text);$("signal").className=cls;}
  function displayError(message){set("error",message);$("error").hidden=!message;}
  function runs(){return Array.isArray(data?.runs)?data.runs:[];}
  function selected(){const e=current();return e && runs().find(r=>r.task_id===e.task_id);}
  function visibleRuns(){
    const e=current();if(!e)return [];
    return runs().filter(r=>r.review && r.receipt_id && r.state==="SUCCEEDED" && Number.isFinite(time(r.verified_utc)) && time(r.verified_utc)<=time(e.utc));
  }
  const sourceLine=r=>r.detail || r.reason || "";
  const label=e=>e?.title||stages[e?.kind]||e?.kind||"等待运行记录";
  function renderStages(list,active){
    const out=$("steps");out.replaceChildren();
    const recent=list.slice(-5);
    for(const item of recent){
      const card=make("div","chip"+(active===item?" active":""));
      card.append(make("small","",item.top),make("strong","",item.body));out.append(card);
    }
    if(!recent.length)out.append(make("p","", "等待已记录步骤"));
  }
  function nodes(graph) {
    const list=Array.isArray(graph) ? graph : graph?.nodes??graph?.steps;
    return Array.isArray(list)?list.filter(n=>n && typeof n==="object").slice(0,32):[];
  }
  function renderDecision(run,e) {
    set("event-title",label(e));set("event-time",fmt(e?.utc));set("event-kind",e?.kind||"");
    set("event-detail",e?sourceLine(e):"提交任务后，此处会随真实运行记录更新。");
    const verified=visibleRuns().includes(run), metrics=$("metrics");metrics.replaceChildren();
    function metric(key,value){const box=make("div","metric");box.append(make("span","",key),make("b","",value));metrics.append(box);}
    if(e?.provider || (verified && run?.provider))metric("调用实现",originNames[e?.origin]|| (verified ? origin(run):e?.provider));
    if(verified && Array.isArray(run.review.review_order))metric("复核顺序",run.review.review_order.join(" → ")||"无候选");
    if(verified && Array.isArray(run.review.unknown_columns))metric("未知区",run.review.unknown_columns.join("、")||"无");
    const pin=e?.binding||(verified?run?.binding:null)||e?.source_ref;
    set("pin-label",pin?(hash(pin)?"证据摘要 / 绑定":"来源回执"):"");set("pin",short(pin));spanTitle($("pin"),pin);
    const history=events.slice(0,cursor+1), earlierGraph=[...history].reverse().find(row=>row.task_id===run?.task_id && nodes(row.graph).length);
    const graph=nodes(e?.graph).length?e.graph:earlierGraph?.graph||(verified?run?.graph:null),ns=nodes(graph);
    if(ns.length){
      set("lower-title","本次工具组合");set("lower-note",ns.length+" 个节点");
      const out=$("steps");out.replaceChildren();
      ns.forEach(node=>{
        const card=make("div","chip active");const deps=node.input??node.depends_on??node.inputs;
        const port=typeof deps==="object" && deps!==null ? JSON.stringify(deps):deps;
        const op=node.tool||node.op||node.operator||node.service||node.capability||node.kind||node.id;
        const edge=[port,node.id||node.name].filter(Boolean).join(" → ");
        card.append(make("small","",edge||"节点"),spanTitle(make("strong","",node.label||operationTitles[op]||op),op));out.append(card);
      });
    } else if (!history.length && Array.isArray(data?.providers)) {
      set("lower-title","站点已登记的实现");set("lower-note","当前目录");
      renderStages(data.providers.map(p=>({top:p.available?(p.preferred?"站点首选":"可用"):"不可用",body:originNames[p.origin]||p.label||p.id})));
    } else {
      set("lower-title","已记录的步骤");set("lower-note",history.length+" 条事件");
      const steps=history.map(row=>({top:fmt(row.utc),body:label(row)}));renderStages(steps,steps.at(-1));
    }
  }
  function renderComparison() {
    const list=visibleRuns(); const out=$("compare-rows");out.replaceChildren();
    const frame=data?.frame||{};
    const verified=list.filter(r=>hash(r.binding)&&r.operation_id && hash(frame.rgb_ref) && hash(frame.depth_ref) && r.frame?.rgb_ref===frame.rgb_ref && r.frame?.depth_ref===frame.depth_ref);
    set("completeness",verified.length+" 份同帧回执");
    for(const run of list) {
      const row=make("div","compare-row");
      const provider=make("div"),params=make("div"),measure=make("div"),order=make("div");
      provider.append(make("strong","",origin(run)),spanTitle(make("code","",run.provider||""),run.binding));
      params.append(make("strong","",(run.review.distance_m??run.args?.distance_m??"—")+" m"),make("small","",verified.includes(run)?"RGB 与深度帧摘要相同":"输入待核验"));
      measure.append(make("strong","",(run.review.regions||[]).map(r=>r.state==="unknown"?"未知":val(r.depth_m)).join("  /  ")),make("small","",run.review.statistic==="p10"?"分区有效深度 · 第 10 百分位":run.review.statistic||"分区测量"));
      order.append(make("strong","order",(run.review.review_order||[]).join(" → ")||"无候选"),make("small","", "未知区 · "+((run.review.unknown_columns||[]).join("、")||"无")));
      row.append(provider,params,measure,order);out.append(row);
    }
    if(!list.length)out.append(make("p","agreement","等待任务完成并取得验证回执。"));
    const distinct=new Set(verified.map(r=>origin(r)));
    function equivalentTo(reference,run) {
      const a=reference.review,b=run.review;
      const matchingDepth=(x,y)=>x===y || (Number.isFinite(x) && Number.isFinite(y) && Math.abs(x-y)<=1e-9);
      return reference.text===run.text && a.distance_m===b.distance_m &&
        a.statistic===b.statistic && JSON.stringify(a.review_order)===JSON.stringify(b.review_order) &&
        JSON.stringify(a.unknown_columns)===JSON.stringify(b.unknown_columns) &&
        Array.isArray(a.regions) && Array.isArray(b.regions) && a.regions.length>0 && a.regions.length===b.regions.length &&
        a.regions.every((v,i)=>{const w=b.regions[i];return v.column===w.column && v.valid_pixels===w.valid_pixels &&
          v.pixels===w.pixels && v.state===w.state && matchingDepth(v.depth_m,w.depth_m);});
    }
    const equivalent=verified.length>0 && verified.every(r=>equivalentTo(verified[0],r));
    const complete=["系统自带插件","站点工作流","Agent 组合工具"].every(label=>distinct.has(label));
    set("agreement",list.length?(complete && equivalent?"三种来源在同一 RGB-D 帧上返回一致的测量、未知区和复核顺序。":complete?"三种来源的回执存在差异，请对照任务与测量结果。":"当前已取得 "+distinct.size+" 种来源的同帧回执。"):"");
    $("agreement").classList.toggle("warn",complete&&!equivalent);
  }
  function frameNotes(rgb,depth){
    $("rgb-missing").hidden=Boolean(rgb===null);set("rgb-missing",rgb);
    $("depth-missing").hidden=Boolean(depth===null);set("depth-missing",depth);
  }
  function drawFrame(run){
    const frame=data?.frame;if(!frame)return;
    const verified=visibleRuns().includes(run), review=verified?run?.review:null;
    const overlay=$("overlay");overlay.replaceChildren();
    const bound=Boolean(run && run.frame?.depth_ref===frame.depth_ref && run.frame?.rgb_ref===frame.rgb_ref);
    if(review && bound && mediaResult){
      for(const region of review.regions||[]){const box=make("div","column "+region.state);box.append(make("span","",region.column),make("b","",region.state==="unknown"?"未知":val(region.depth_m)));overlay.append(box);}
    }
    set("resolution",Number.isInteger(frame.width)&&Number.isInteger(frame.height)?frame.width+" × "+frame.height:"");
    set("depth-hash",short(frame.depth_ref));spanTitle($("depth-hash"),frame.depth_ref);
    const threshold=review&&bound ? review.distance_m : null;
    const depth=$("depth");
    if(mediaResult) {
      $("rgb").hidden=false;
      if(Number.isFinite(threshold)){
        const was=depth.dataset.key,key=mediaKey+"|"+threshold;
        if(was!==key){compareMedia.draw(depth,frame,mediaResult,threshold);depth.dataset.key=key;}
        depth.hidden=false;frameNotes(null,null);set("depth-caption","深度阈值图 · 橙色在 "+threshold+" m 内");
      } else {depth.hidden=true;frameNotes(null,"取得复核阈值后显示");set("depth-caption","对齐深度 · 原帧已校验");}
      set("frame-check","RGB 与深度原帧摘要已校验");
    } else {set("frame-check",mediaError||"等待原始 RGB-D 帧校验");}
  }
  async function loadFrames(){
    const f=data?.frame, key=f?JSON.stringify([f.rgb_ref,f.depth_ref,f.width,f.height,f.depth_scale_m]):"";
    if(key===mediaKey)return;
    mediaKey=key;const ticket=++mediaTicket;mediaResult=null;mediaError="";
    $("rgb").hidden=true;$("depth").hidden=true;frameNotes("读取彩色帧","读取深度帧");
    if(imageURL){URL.revokeObjectURL(imageURL);imageURL="";}
    if(!f || !hash(f.rgb_ref) || !hash(f.depth_ref)){frameNotes("等待彩色帧","等待深度帧");return;}
    try {
      const [rgb,d]=await Promise.all([compareMedia.readPNG(f.rgb_ref,mediaBase,imageBundle),compareMedia.readPNG(f.depth_ref,mediaBase,imageBundle)]);
      const pixels=await compareMedia.decodeDepth(d,f);if(ticket!==mediaTicket)return;
      imageURL=URL.createObjectURL(new Blob([rgb],{type:"image/png"}));$("rgb").src=imageURL;await $("rgb").decode();
      if(ticket!==mediaTicket)return;
      if($("rgb").naturalWidth!==f.width || $("rgb").naturalHeight!==f.height)throw Error("RGB 尺寸与观测契约不符");
      mediaResult=pixels;drawFrame(selected());
    } catch(err) {if(ticket===mediaTicket){mediaError=err.message;frameNotes(err.message,err.message);set("frame-check",err.message);}}
  }
  function render(){
    const e=current(),r=selected();
    const lastUser=[...events.slice(0,cursor+1)].reverse().find(row=>runs().some(run=>run.task_id===row.task_id))?.task_id;
    const request=r||runs().find(run=>run.task_id===lastUser);
    if($("composer").hidden){set("request-text",request?.text||"等待任务提交");set("request-id",request?.task_id||"");}
    $("tab-decision").setAttribute("aria-pressed",String(view==="decision"));$("tab-compare").setAttribute("aria-pressed",String(view==="compare"));
    $("decision").hidden=view!=="decision";$("comparison").hidden=view!=="compare";
    set("position",events.length?(cursor+1)+" / "+events.length:"0 / 0");
    $("cursor").max=Math.max(0,events.length-1);$("cursor").value=Math.max(0,cursor);$("cursor").disabled=!events.length;
    $("previous").disabled=cursor<=0;$("next").disabled=cursor>=events.length-1;$("play").disabled=events.length<2;
    $("follow").setAttribute("aria-pressed",String(follow));$("follow").hidden=sealed;
    renderDecision(r,e);renderComparison();drawFrame(r);
  }
  const stop=()=>{clearInterval(playing);playing=0;set("play","播放");};
  function manual(i){stop();follow=false;cursor=Math.max(0,Math.min(i,events.length-1));render();}
  $("previous").onclick=()=>manual(cursor-1);$("next").onclick=()=>manual(cursor+1);
  $("cursor").oninput=()=>manual(Number($("cursor").value));
  $("follow").onclick=()=>{stop();follow=!follow;if(follow)cursor=events.length-1;render();};
  $("play").onclick=()=>{
    if(playing){stop();return;}
    if(cursor>=events.length-1)cursor=0;
    follow=false;set("play","暂停");render();
    playing=setInterval(()=>{if(cursor>=events.length-1)stop();else{cursor++;render();}},2800);
  };
  $("tab-decision").onclick=()=>{view="decision";render();};$("tab-compare").onclick=()=>{view="compare";render();};
  const submitAllowed=()=>!sealed && data?.submit_path==="/review-api/tasks";
  $("open-compose").onclick=()=>{
    if(!submitAllowed())return;
    $("composer").hidden=false;$("request-text").hidden=true;$("open-compose").hidden=true;set("request-id","");
    $("task-text").focus();
  };
  $("cancel-compose").onclick=()=>{$("composer").hidden=true;$("request-text").hidden=false;$("open-compose").hidden=!submitAllowed();render();};
  $("composer").onsubmit=async event=>{
    event.preventDefault();if(!submitAllowed() || acking)return;
    const text=$("task-text").value.trim();if(!text || text.length>2048)return;
    if(!pending || pending.text!==text)pending={id:crypto.randomUUID?.() || "review-"+Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,"0")).join(""),text};
    acking=true;$("submit-task").disabled=true;set("submit-status","等待站点受理…");
    try {
      const res=await fetch("/review-api/tasks",{method:"POST",credentials:"same-origin",redirect:"error",headers:{"Content-Type":"application/json"},body:JSON.stringify(pending),signal:AbortSignal.timeout(20000)});
      if(res.status!==202)throw Error("HTTP "+res.status);
      const ack=await res.json();if(ack.id!==pending.id)throw Error("任务编号与申请不一致");
      set("submit-status","站点已受理 · "+pending.id);set("request-id",pending.id);
      pending=null;follow=true;stop();await poll();
      setTimeout(()=>{if(!acking && !$("composer").hidden){$("task-text").value="";$("cancel-compose").click();}},1000);
    } catch(err){set("submit-status","提交未确认 · "+err.message+"。可用原任务编号重试。");}
    finally{acking=false;$("submit-task").disabled=false;}
  };
  let loading=false;
  async function poll(){
    if(loading)return;loading=true;
    try{
      if(!validArchive)throw Error("归档编号无效");
      let raw;
      if(offline)raw=offline.textContent;
      else{
        const res=await fetch(manifestURL,{cache:"no-store",credentials:"same-origin",signal:AbortSignal.timeout(10000)});
        if(!res.ok)throw Error("记录端返回 HTTP "+res.status);
        raw=await res.text();
      }
      if(raw.length>4e6)throw Error("记录超过读取预算");
      if(raw!==cached) {
        const doc=JSON.parse(raw);
        if(doc.schema_version!==1 || !Array.isArray(doc.events) || !Array.isArray(doc.runs))throw Error("不支持的对照记录");
        const previous=current(),fresh=data===null;
        data=doc;cached=raw;
        events=doc.events.filter(e=>e&&typeof e.kind==="string"&&Number.isFinite(time(e.utc))).sort((a,b)=>time(a.utc)-time(b.utc));
        if(follow || fresh)cursor=events.length-1;
        else if(previous){const i=events.findIndex(e=>e.kind===previous.kind && e.task_id===previous.task_id && e.utc===previous.utc);cursor=i<0?Math.min(cursor,events.length-1):i;}
        $("open-compose").hidden=!submitAllowed() || !$("composer").hidden;render();void loadFrames();
      }
      displayError("");status(sealed?"封存回放":"站点记录在线","ok");
    } catch(err){displayError(err.message);status("读取失败","fail");}
    finally{loading=false;}
  }
  if(validArchive && !sealed)setInterval(poll,1200);
  void poll();
})();
