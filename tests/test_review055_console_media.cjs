/* Browser-side replay must preserve 16-bit depths, invalid sentinels and source integrity on LAN HTTP. */
'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm');
const {readFileSync}=require('node:fs'),{join}=require('node:path'),{createHash}=require('node:crypto'),{deflateSync}=require('node:zlib');
function source(depths){
  const bytesPerRow=depths.length*2+1,raw=Buffer.alloc(bytesPerRow);
  depths.forEach((z,i)=>raw.writeUInt16BE(z,1+i*2));
  const header=Buffer.alloc(13);header.writeUInt32BE(depths.length,0);header.writeUInt32BE(1,4);header[8]=16;
  function chunk(name,data){const block=Buffer.alloc(data.length+12);block.writeUInt32BE(data.length);block.write(name,4);data.copy(block,8);
    let crc=0xffffffff;for(const b of block.subarray(4,block.length-4)){crc^=b;for(let bit=0;bit<8;bit++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}
    block.writeUInt32BE((crc^0xffffffff)>>>0,block.length-4);return block;}
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),chunk('IDAT',deflateSync(raw)),chunk('IEND',Buffer.alloc(0))]);
}
function media(){const scope={Blob,DecompressionStream,Uint8Array,Uint16Array,DataView,atob,crypto:{},URL};
  vm.runInNewContext(readFileSync(join(__dirname,'../console/web/review-compare-media.js'),'utf8'),scope);return scope.compareMedia;}
const frame={width:5,height:1,depth_scale_m:.001},values=[0,799,800,801,65535],image=source(values);
const digest=createHash('sha256').update(image).digest('hex');
test('offline and LAN replay use the pinned archived bytes',async()=>{
  const impl=media(),bundle={[digest+'.png']:image.toString('base64')};
  const checked=await impl.readPNG('sha256:'+digest,null,bundle);
  assert.deepEqual(Array.from(await impl.decodeDepth(checked,frame)),values);
  const other='0'.repeat(64);await assert.rejects(impl.readPNG('sha256:'+other,null,{[other+'.png']:image.toString('base64')}),/摘要不符/);
  await assert.rejects(impl.decodeDepth(checked,{...frame,width:6}),/尺寸不符/);
});
test('inclusive optical threshold does not mark invalid pixels as near or distant',()=>{
  const impl=media();let output;
  const canvas={getContext:()=>({createImageData:()=>({data:new Uint8Array(20)}),putImageData(img){output=img.data;}})};
  impl.draw(canvas,frame,values,.8);
  assert.deepEqual([...output].filter((_,i)=>i%4===0),[10,238,238,66,10]);
});
