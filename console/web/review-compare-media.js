/* Shared capture convention and LAN-compatible SHA-256, also used by the field review Console. */
(() => {
  'use strict';
  function portableSha256(bytes) {
    const H = [],
      K = [];
    for (let n = 2; K.length < 64; n++) {
      let prime = true;
      for (let m = 2; m * m <= n; m++)
        if (n % m === 0) {
          prime = false;
          break;
        }
      if (prime) {
        if (H.length < 8) H.push((Math.sqrt(n) * 2 ** 32) >>> 0);
        K.push((Math.cbrt(n) * 2 ** 32) >>> 0);
      }
    }
    const buffer = new Uint8Array(Math.ceil((bytes.length + 9) / 64) * 64);
    buffer.set(bytes);
    buffer[bytes.length] = 128;
    const dv = new DataView(buffer.buffer),
      end = buffer.length;
    dv.setUint32(end - 8, Math.floor(bytes.length / 0x20000000));
    dv.setUint32(end - 4, (bytes.length * 8) >>> 0);
    const rr = (x, n) => (x >>> n) | (x << (32 - n)),
      w = new Uint32Array(64);
    for (let i = 0; i < end; i += 64) {
      for (let j = 0; j < 16; j++) w[j] = dv.getUint32(i + j * 4);
      for (let j = 16; j < 64; j++) {
        const a = w[j - 15],
          b = w[j - 2];
        w[j] =
          (w[j - 16] +
            (rr(a, 7) ^ rr(a, 18) ^ (a >>> 3)) +
            w[j - 7] +
            (rr(b, 17) ^ rr(b, 19) ^ (b >>> 10))) >>>
          0;
      }
      let [a, b, c, d, e, f, g, h] = H;
      for (let j = 0; j < 64; j++) {
        const u =
          (h +
            (rr(e, 6) ^ rr(e, 11) ^ rr(e, 25)) +
            ((e & f) ^ (~e & g)) +
            K[j] +
            w[j]) >>>
          0;
        const v =
          ((rr(a, 2) ^ rr(a, 13) ^ rr(a, 22)) +
            ((a & b) ^ (a & c) ^ (b & c))) >>>
          0;
        [a, b, c, d, e, f, g, h] = [
          (u + v) >>> 0,
          a,
          b,
          c,
          (d + u) >>> 0,
          e,
          f,
          g,
        ];
      }
      [a, b, c, d, e, f, g, h].forEach((v, j) => {
        H[j] = (H[j] + v) >>> 0;
      });
    }
    return H.map((x) => x.toString(16).padStart(8, "0")).join("");
  }
  const REF = /^sha256:([0-9a-f]{64})$/;
  async function readPNG(ref, directory, embedded) {
    const match = REF.exec(ref);
    if (!match) throw Error("缺少有效帧摘要");
    const name = match[1] + ".png";
    let bytes;
    if (embedded) {
      const data = embedded[name];
      if (typeof data !== "string" || data.length > 12e6) throw Error("离线包缺少原图");
      bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
    } else {
      const response = await fetch(new URL(name, directory), {credentials:"same-origin",signal:AbortSignal.timeout(10000)});
      if (!response.ok || Number(response.headers.get("Content-Length")) > 8e6) throw Error("读取原图失败");
      bytes = new Uint8Array(await response.arrayBuffer());
    }
    if (bytes.length > 8e6 || bytes.length < 33 || new DataView(bytes.buffer,bytes.byteOffset).getUint32(0) !== 0x89504e47) throw Error("帧格式或大小不符");
    const hash = crypto.subtle ? [...new Uint8Array(await crypto.subtle.digest("SHA-256",bytes))].map(b=>b.toString(16).padStart(2,"0")).join("") : portableSha256(bytes);
    if (hash !== match[1]) throw Error("帧文件与原始摘要不符");
    return bytes;
  }
  async function decodeDepth(bytes, frame) {
    const view = new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
    const width = view.getUint32(16), height = view.getUint32(20);
    if (view.getUint32(8)!==13 || view.getUint32(12)!==0x49484452 || width!==frame.width || height!==frame.height ||
        !width || !height || width*height>4194304 || bytes[24]!==16 || bytes[25]!==0 || bytes[26] || bytes[27] || bytes[28]) throw Error("深度数据与标定尺寸不符");
    const compressed = [];
    let ended = false;
    for (let at=33; at+12<=bytes.length;) {
      const size=view.getUint32(at),type=view.getUint32(at+4),end=at+size+12;
      if (end>bytes.length) throw Error("深度 PNG 被截断");
      if (type===0x49444154) compressed.push(bytes.subarray(at+8,end-4));
      if (type===0x49454e44) { ended=end===bytes.length && !size; break; }
      at=end;
    }
    if (!compressed.length || !ended) throw Error("缺少完整深度帧");
    const stride=2*width+1, raw=new Uint8Array(stride*height);
    const reader=new Blob(compressed).stream().pipeThrough(new DecompressionStream("deflate")).getReader();
    let pos=0;
    for (;;) {
      const {value,done}=await reader.read();
      if (done) break;
      if (value.length>raw.length-pos) { await reader.cancel(); throw Error("深度解码超出尺寸"); }
      raw.set(value,pos);pos+=value.length;
    }
    if (pos!==raw.length) throw Error("深度图实际长度不符");
    const pixels=new Uint16Array(width*height);
    for (let y=0;y<height;y++) {
      if (raw[y*stride]!==0) throw Error("站点深度帧行编码不受支持");
      for (let x=0;x<width;x++) {
        const off=y*stride+1+x*2;
        pixels[y*width+x]=raw[off]*256+raw[off+1];
      }
    }
    return pixels;
  }
  function draw(canvas, frame, pixels, threshold) {
    if (!(frame.depth_scale_m>0 && Number.isFinite(threshold) && threshold>0)) throw Error("无阈值或深度量纲");
    canvas.width=frame.width;canvas.height=frame.height;
    const context=canvas.getContext("2d"),img=context.createImageData(frame.width,frame.height);
    const colors=[[10,17,19],[238,166,95],[66,124,146]];
    for(let i=0;i<pixels.length;i++) {
      const z=pixels[i];
      const color=colors[z===0 || z===65535 ? 0 : z*frame.depth_scale_m<=threshold+1e-12 ? 1 : 2];
      img.data[i*4]=color[0];img.data[i*4+1]=color[1];img.data[i*4+2]=color[2];img.data[i*4+3]=255;
    }
    context.putImageData(img,0,0);
  }
  globalThis.compareMedia={readPNG,decodeDepth,draw};
})();
