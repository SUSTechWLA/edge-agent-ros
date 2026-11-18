/* Each immutable edition is decoded once. The map remains usable during loading. */
window.createMapTexture = function createMapTexture(notify) {
  let key = null, request = null, loaded = null, state = "absent", retryAt = 0;
  async function raster(path, info, signal, keepImage) {
    const response = await fetch(path,{signal,cache:"default"});
    if (!response.ok) throw Error("texture_download_failed");
    const bitmap = await createImageBitmap(await response.blob(), {colorSpaceConversion:"none"});
    try {
      if (bitmap.width !== info.width || bitmap.height !== info.height) throw Error("texture_size_invalid");
      const canvas = document.createElement("canvas");
      canvas.width=bitmap.width; canvas.height=bitmap.height;
      const ctx=canvas.getContext("2d",{willReadFrequently:true});
      ctx.drawImage(bitmap,0,0);
      return {image:keepImage ? canvas : null, pixels:ctx.getImageData(0,0,canvas.width,canvas.height).data};
    } finally { bitmap.close(); }
  }
  async function load(info, token) {
    const timer = setTimeout(() => token.abort(), 20000);
    try {
      const [color, elevation] = await Promise.all([
        raster(info.url,info,token.signal,true),
        info.height_url ? raster(info.height_url,info,token.signal,false) : null
      ]);
      if (request !== token) return;
      loaded = {...info,...color,heights:elevation?.pixels || null};
      state = "ready";
    } catch {
      if (request !== token) return;
      token.abort();
      state="download_error";
      retryAt=Date.now()+10000;
    } finally {
      clearTimeout(timer);
      if (request === token) {request=null;notify();}
    }
  }
  function update(info) {
    const next = info?.status==="ready" ? info.key : null;
    if (next!==key) {
      request?.abort(); request=null; loaded=null; key=next; retryAt=0; state="absent";
    }
    if (!next) {state=info?.error || "absent";return;}
    if (!loaded && !request && Date.now()>=retryAt) {
      state="loading";request=new AbortController();load(info,request);
    }
  }
  return {update, get current(){return loaded;}, get state(){return state;}};
};
