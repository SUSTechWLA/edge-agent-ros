// Browser acceptance on the shipped map plugin. Freeze one real ROS snapshot in
// this tab so that live sensor updates cannot masquerade as camera movement.
import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

const selectors = {spatial:'#viewport', planar:'#planar-viewport'};

function pixelSignature(selector) {
  const map = document.querySelector(selector);
  const sample = document.createElement('canvas');
  sample.width = 160;
  sample.height = 120;
  const ctx = sample.getContext('2d');
  ctx.drawImage(map, 0, 0, sample.width, sample.height);
  let hash = 2166136261;
  for (const byte of ctx.getImageData(0, 0, sample.width, sample.height).data) hash = Math.imul(hash ^ byte, 16777619);
  return hash >>> 0;
}

function cellFixture(snapshot) {
  // Rotate the source grid 90°. Its center cell remains at world (0, 0).
  const cells = Array.from({length:25}, (_, i) => i);
  cells[12] = 83;
  return {
    ...snapshot, generation:snapshot.generation + 10,
    grid:{width:5, height:5, resolution:1, origin:[2.5,-2.5,Math.PI / 2], frame:snapshot.frame,
      cells, status:'online', stamp_s:10, topic:'/fixture/grid'},
    cloud:{...snapshot.cloud, points:[[0,0,1.25]], source_frame:'viewer-test-lidar'},
    cloud_sources:{}, pose:{status:'waiting'}, scene:{status:'waiting', features:[], zones:[]}
  };
}

export async function checkMap3D({url, width, output, evaluate, waitFor, send}) {
  const snapshot = await (await fetch(new URL('/api/map',url))).json();
  assert.ok(snapshot.cloud?.status === 'online' && snapshot.cloud.points.length, 'Map acceptance requires a live ROS survey cloud');
  await waitFor("document.querySelector('#viewport')?.width > 0 && Number(document.querySelector('#point-count')?.textContent.replace(/[^0-9]/g,'')) > 0");
  const click = selector => evaluate('document.querySelector('+JSON.stringify(selector)+').click()');
  const value = selector => evaluate('document.querySelector('+JSON.stringify(selector)+').textContent');
  function pixels(mode) {
    return '('+pixelSignature.toString()+')('+JSON.stringify(selectors[mode])+')';
  }
  async function nextPaint() {
    await evaluate('window.__mapTestPaint = false; requestAnimationFrame(() => requestAnimationFrame(() => {window.__mapTestPaint = true}))');
    await waitFor('window.__mapTestPaint === true', 'two animation frames');
  }
  async function select(mode) {
    await click('#mode-'+mode);
    await waitFor('document.querySelector("#mode-'+mode+'").getAttribute("aria-pressed") === "true"');
    await nextPaint();
    const other = mode === 'spatial' ? 'planar' : 'spatial';
    assert.ok(!await evaluate('document.querySelector("'+selectors[other]+'").getClientRects().length'), 'Inactive canvas must be hidden');
    const geometry = await evaluate(`(() => {
      const canvas = document.querySelector('${selectors[mode]}');
      canvas.scrollIntoView({block:'center'});
      const r=canvas.getBoundingClientRect();
      return {document_width:document.documentElement.scrollWidth, canvas:[r.x,r.y,r.width,r.height]};
    })()`);
    assert.ok(geometry.document_width <= width+1 && geometry.canvas[2]>=160 && geometry.canvas[3]>=150,
      mode+' layout at '+width+': '+JSON.stringify(geometry));
    assert.ok(await evaluate(`(() => {
      const picker = document.querySelector('#inspect-source');
      if(!picker.getClientRects().length) return true;
      return picker.getBoundingClientRect().right <= picker.closest('.panel').getBoundingClientRect().right;
    })()`), 'Lidar source selector must fit in its panel at '+width);
    return geometry;
  }
  async function checkChange(mode, label, act) {
    const before = await evaluate(pixels(mode));
    await act();
    await waitFor(pixels(mode)+' !== '+before, mode+': '+label);
    return evaluate(pixels(mode));
  }
  async function receipt(label, predicate) {
    await waitFor('('+predicate.toString()+')(document.querySelector("#pick-result").textContent)', label);
    return value('#pick-result');
  }
  function checkGridValue(text, grid) {
    const cell = /栅格\s*[（(]\s*(\d+)\s*[,，]\s*(\d+)\s*[）)]/.exec(text)
      || /列\s*(\d+)\s*行\s*(\d+)/.exec(text);
    const reading = /值\s*(-?\d+)/.exec(text);
    assert.ok(cell && reading, 'Cell indices and raw occupancy must be visible: '+text);
    const [col,row] = cell.slice(1).map(Number);
    assert.equal(Number(reading[1]),grid.cells[row*grid.width+col],'readout agrees with original ROS row-major data');
    return [col,row];
  }
  async function layer(id, checked) {
    if (await evaluate('document.querySelector("#layer-'+id+'").checked') !== checked) await click('#layer-'+id);
  }
  async function sceneLayers(checked) {
    for (const id of ['grid','scene','pose','cloud']) await layer(id, checked);
    await nextPaint();
  }
  function pointer(type, x, y, buttons, modifiers=0) {
    return send('Input.dispatchMouseEvent',{type,x,y,buttons,button:'left',modifiers,clickCount:1});
  }
  async function drag(x,y,dx,dy,modifiers=0) {
    await pointer('mousePressed',x,y,1,modifiers);
    await pointer('mouseMoved',x+dx,y+dy,1,modifiers);
    await pointer('mouseReleased',x+dx,y+dy,0,modifiers);
  }
  const center = ([x,y,w,h]) => [x+w/2,y+h/2];
  const tap = async (x,y) => {await pointer('mousePressed',x,y,1); await pointer('mouseReleased',x,y,0);};
  async function planarRuler() {
    const {canvas} = await select('planar');
    const [x,y] = center(canvas), positions=[];
    for (const dx of [0,30]) {
      await tap(x+dx,y);
      const selection = await receipt('metric planar coordinates',text => /X\s+[-\d.]+\s*m/.test(text));
      positions.push(selection.match(/X\s+[-\d.]+\s*m\s*·\s*Y\s+[-\d.]+\s*m/)[0]);
    }
    assert.notEqual(positions[0],positions[1],'distinct world points under two distinct pixels');
    return positions;
  }
  async function pinch(mode,x,y) {
    if (width!==390) return;
    await send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:2});
    try {
      const pair = distance => [{x:x-distance,y,id:1},{x:x+distance,y,id:2}];
      await checkChange(mode,'two-finger pinch',async () => {
        await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:pair(20)});
        await send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:pair(38)});
        await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
      });
    } finally {await send('Emulation.setTouchEmulationEnabled',{enabled:false});}
  }
  async function key(mode, key) {
    await evaluate('document.querySelector("'+selectors[mode]+'").focus({preventScroll:true})');
    for (const type of ['rawKeyDown','keyUp']) await send('Input.dispatchKeyEvent',{type,key,code:key});
  }
  async function screenshot(name) {
    const {cssContentSize:size} = await send('Page.getLayoutMetrics');
    const {data} = await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,
      clip:{x:0,y:0,width:size.width,height:size.height,scale:1}});
    const suffix = width===1440 ? '' : '-'+width;
    writeFileSync(join(dirname(output),name+suffix+'.png'),Buffer.from(data,'base64'));
  }
  // Only replace the GET snapshot in this tab; collecting the live snapshot
  // above and restoring fetch below both use the actual provider.
  await evaluate(`window.__viewerReplay={fetch:window.fetch,served:0,snapshot:null};
    window.fetch=(input,init) => {
      const replay=window.__viewerReplay;
      const path=new URL(input.url || input,location.href).pathname;
      if(path==='/api/map' && replay.snapshot) {
        replay.served++;
        return Promise.resolve(Response.json(replay.snapshot));
      }
      return replay.fetch.call(window,input,init);
    };`);
  async function replay(data) {
    await evaluate('window.__viewerReplay.served=0; window.__viewerReplay.snapshot='+JSON.stringify(data)+'; true');
    await waitFor('window.__viewerReplay.served>0','viewer consumed replay');
    await nextPaint();
  }
  try {
    await replay(snapshot);
    await sceneLayers(true);
    const spatialLayout = await select('spatial');
    const [x,y] = center(spatialLayout.canvas);
    await click('#orbit-reset');
    await nextPaint();
    await screenshot('map3d-simulation');
    await checkChange('spatial','top projection',() => click('#view-top'));
    await click('#orbit-reset');
    await nextPaint();
    await checkChange('spatial','hide measured points',() => layer('cloud',false));
    await layer('cloud',true);
    await nextPaint();
    await checkChange('spatial','left-drag orbit',() => drag(x,y,40,20));
    await checkChange('spatial','shift-drag pan',() => drag(x,y,20,-15,8));
    await checkChange('spatial','wheel dolly',() =>
      send('Input.dispatchMouseEvent',{type:'mouseWheel',x,y,deltaX:0,deltaY:-140}));
    await pinch('spatial',x,y);
    const spatialSaved = await evaluate(pixels('spatial'));

    const planarLayout = await select('planar');
    const [u,v] = center(planarLayout.canvas);
    await click('#orbit-reset');
    await nextPaint();
    await screenshot('map2d-simulation');
    await checkChange('planar','drag pan',() => drag(u,v,37,19));
    await checkChange('planar','wheel zoom',() =>
      send('Input.dispatchMouseEvent',{type:'mouseWheel',x:u,y:v,deltaX:0,deltaY:-100}));
    await checkChange('planar','keyboard navigation',() => key('planar','ArrowLeft'));
    await pinch('planar',u,v);
    const planarSaved = await planarRuler();
    await select('spatial');
    assert.equal(await evaluate(pixels('spatial')),spatialSaved,'restored spatial camera');
    await select('planar');
    assert.deepEqual(await planarRuler(),planarSaved,'both translation and metric scale survive a mode switch');

    let liveGridPick;
    if (snapshot.grid?.status==='online') {
      await sceneLayers(false);
      await layer('grid',true);
      await click('#orbit-reset');
      await nextPaint();
      await tap(...center(planarLayout.canvas));
      liveGridPick = await receipt('real ROS OccupancyGrid selection',text => /值\s*-?\d+/.test(text));
      checkGridValue(liveGridPick,snapshot.grid);
    }
    const fixture = cellFixture(snapshot);
    await replay(fixture);
    await sceneLayers(false);
    const picks = {};
    for (const mode of ['planar','spatial']) {
      const layout = await select(mode);
      await layer('grid',false);
      await waitFor('!document.querySelector("#empty").hidden','all layers off');
      await layer('grid',true);
      await click('#orbit-reset');
      if(mode==='spatial') await click('#view-top');
      await nextPaint();
      await waitFor('document.querySelector("#empty").hidden','only occupancy grid is visible');
      await tap(...center(layout.canvas));
      picks[mode] = await receipt(mode+' picked center cell value 83', text => /值\s*83/.test(text));
      assert.deepEqual(checkGridValue(picks[mode],fixture.grid),[2,2]);
    }
    fixture.generation++;
    fixture.grid.cells[12] = -1;
    await replay(fixture);
    assert.ok(!(await value('#pick-result')).includes('83'), 'A new generation clears obsolete selected cells');
    await select('planar');
    await click('#orbit-reset');
    await nextPaint();
    await tap(...center((await select('planar')).canvas));
    picks.unknown = await receipt('distinguish unknown from free',text => text.includes('未知'));
    checkGridValue(picks.unknown,fixture.grid);
    fixture.scene = {status:'online', zones:[], features:[{
      id:'test-lobby', label:'教学楼门厅', kind:'landmark', xyz:[0,0,0], size:[2,2,1.5]}]};
    fixture.generation++;
    await replay(fixture);
    await layer('scene',true);
    for(const mode of ['planar','spatial']) {
      const layout = await select(mode);
      await click('#orbit-reset');
      await nextPaint();
      await tap(...center(layout.canvas));
      picks[mode+'_semantic'] = await receipt(mode+' semantic feature',text => text.includes('教学楼门厅'));
    }

    // A legacy producer can omit the grid entirely. The measured XYZ sample
    // must remain inspectable, with the source and height reported by the view.
    delete fixture.grid;
    fixture.generation++;
    await replay(fixture);
    const probe = await select('spatial');
    await layer('scene',false);
    await layer('cloud',true);
    await click('#orbit-reset');
    await nextPaint();
    await tap(...center(probe.canvas));
    picks.point = await receipt('raw lidar point in XYZ frame',text => text.includes('viewer-test-lidar') && text.includes('1.25'));
    return {width,...spatialLayout,planar_layout:planarLayout,cloud_frame:snapshot.cloud.source_frame,point_count:snapshot.cloud.points.length,
      top_projection:true,cloud_toggle:true,drag_orbit:true,wheel_zoom:true,shift_pan:true,
      planar_drag:true,planar_keyboard:true,planar_wheel:true,touch_pinch:width===390,
      independent_cameras:true,live_grid_status:snapshot.grid?.status ?? 'absent',live_grid_pick:liveGridPick,fixture_picks:picks};
  } finally {
    await evaluate('window.fetch=window.__viewerReplay.fetch; delete window.__viewerReplay');
    await sceneLayers(true);
    await select('spatial');
    await click('#orbit-reset');
  }
}
