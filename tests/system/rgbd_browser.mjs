// Verify that a real pointer on both RGB-D layers samples the rendered frame at its native pixel.
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export async function checkRgbd({width, output, evaluate, waitFor, send}) {
  await waitFor("document.querySelector('#status')?.dataset.state==='online' && ['color-image','depth-image'].every(id=>document.getElementById(id)?.naturalWidth===320)", 'paired sensor previews rendered');
  const documentWidth=await evaluate('document.documentElement.scrollWidth');
  if(documentWidth>width+1) throw Error('RGB-D horizontal overflow at '+width);
  const readings={};
  for(const id of ['depth-image','color-image']){
    const rect=await evaluate(`(() => {
      const img=document.getElementById('${id}'); img.scrollIntoView({block:'center'});
      const r=img.getBoundingClientRect();
      return {x:r.left+r.width*160.6/320, y:r.top+r.height*120.6/240};
    })()`);
    for (const type of ['mousePressed','mouseReleased']){
      await send('Input.dispatchMouseEvent',{type,...rect,button:'left',buttons:type==='mousePressed'?1:0,clickCount:1});
    }
    await waitFor("document.querySelector('#pixel-value')?.textContent.includes('(160, 120) · 3.110 m · RGB')", 'center pixel at the known south wall, 3.110 m');
    readings[id]=await evaluate("document.querySelector('#pixel-value').textContent");
  }
  if(width===1440){
    await evaluate('window.scrollTo(0,0)');
    const {data}=await send('Page.captureScreenshot',{format:'png'});
    writeFileSync(join(dirname(output),'gazebo-rgbd-view.png'),Buffer.from(data,'base64'));
  }
  return {width,document_width:documentWidth,readings};
}
