/* Field Console consumes a read-only, same-origin projection of archived Agent events and observations. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const put = (id, value) => {
    $(id).textContent = value == null ? "—" : String(value);
  };
  const fmt = (value) =>
    Number.isFinite(value) ? String(Number(value.toFixed(3))) : "未知";
  const percent = (value) =>
    Number.isFinite(value) ? (100 * value).toFixed(1) + "%" : "未知";
  const text = (value) =>
    typeof value === "string"
      ? value
      : value == null
        ? ""
        : JSON.stringify(value, null, 2);
  const phaseNames = {
    "task.accepted": "接收自然语言任务",
    "request.received": "接收自然语言任务",
    "planner.context": "读取工具目录",
    "model.request": "调用规划模型",
    "model.response": "收到模型输出",
    gap_observed: "发现能力缺口",
    candidate_generated: "模型生成候选插件",
    candidate_verified: "独立用例验收通过",
    release_applied: "热加载插件",
    continuation_accepted: "恢复原任务",
    "tool.request": "派发只读调用",
    "tool.response": "收到工具回执",
    "plugin.reused": "复用已安装插件",
    plugin_reused: "复用已安装插件",
    candidate_rejected: "候选被拒绝",
    blocked: "任务受阻",
    submitted: "提交自然语言任务",
    "planner.plugin_gap": "规划器发现能力缺口",
    author_requested: "调用车端插件作者",
    author_replied: "收到模型候选程序",
    plugin_loaded: "在线宿主载入插件",
    continuation_submitted: "提交原任务的续办",
    continuation_terminal: "收到续办终态",
    measurement: "收到分区测量回执",
    "planner.family_selected": "模型选择能力族",
    "operator.maintenance": "部署人员修复发布服务",
    "tool.error": "只读调用报错",
    failed: "插件发布未完成",
    retrying: "重新核验发布状态",
  };
  const classifications = {
    near_surface: "近距",
    above_threshold: "阈值外",
    unknown: "未知",
  };
  const requestQuery = new URLSearchParams(location.search);
  const embedded = $("field-source");
  const archiveId = requestQuery.get("archive");
  const invalidArchive =
    requestQuery.getAll("archive").length > 1 ||
    (archiveId !== null && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(archiveId));
  const archived = !!embedded || archiveId !== null;
  const archivedImages = $("field-images");
  const localImages = archivedImages
    ? JSON.parse(archivedImages.textContent)
    : {};
  const manifestURL = new URL(
    archiveId && !invalidArchive
      ? "records/archives/" + archiveId + "/manifest.json"
      : "records/manifest.json",
    location.href,
  );
  const state = {
    doc: null,
    pages: [],
    cursor: 0,
    view: "agent",
    follow: !archived && requestQuery.get("follow") !== "0",
    timer: 0,
    pending: false,
    cached: "",
    imageTicket: 0,
    imageStore: new Map(),
    activeObjectURL: null,
    frame: null,
  };
  function el(tag, className, content) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (content != null) n.textContent = String(content);
    return n;
  }
  function note(message) {
    put("notification", message);
    $("notification").hidden = !message;
  }
  function connection(label, tone) {
    put("feed-status", label);
    $("feed-dot").className = tone;
  }
  function stopPlay() {
    clearTimeout(state.timer);
    state.timer = 0;
    put("play", "播放");
  }
  function setFollow(v) {
    state.follow = !!v && !archived;
    $("follow").setAttribute("aria-pressed", String(state.follow));
  }
  function taskPages(doc) {
    if (!doc || !Array.isArray(doc.events) || !Array.isArray(doc.runs))
      throw Error("缺少任务或事件数组");
    const pages = [];
    const tasks = new Map();
    for (const r of doc.runs) {
      if (!r?.task_id || tasks.has(r.task_id))
        throw Error("任务 ID 缺失或重复");
      tasks.set(r.task_id, r);
    }
    function sourceEvent(e, run, i) {
      if (!e || typeof e.phase !== "string") throw Error("事件缺少来源标识");
      const name =
        e.title ||
        phaseNames[e.phase] ||
        (/[^\x00-\x7f]/.test(e.phase) ? e.phase : "源事件记录");
      const last = pages.at(-1);
      if (
        e.phase === "retrying" &&
        last?.event?.phase === e.phase &&
        last.run === run &&
        last.event.detail === e.detail
      ) {
        last.count = (last.count || 1) + 1;
        last.lastUtc = e.utc;
        last.lastReason = e.source?.reason;
        last.name = name + " · " + last.count + " 次";
        return;
      }
      pages.push({
        key: "event-" + i,
        run,
        type: "event",
        name,
        detail: e.detail,
        event: e,
        screen: "agent",
      });
    }
    const common = doc.events
      .map((e, i) => [e, i])
      .filter(([e]) => !tasks.has(e.task_id));
    if (!doc.runs.length) {
      for (const [e, i] of common) sourceEvent(e, null, i);
      if (!pages.length && doc.summary)
        pages.push({
          key: "status",
          run: null,
          type: "status",
          name: "现场状态",
          detail: doc.summary,
          screen: "agent",
        });
    }
    doc.runs.forEach((r, n) => {
      const prompt = r.text || [doc.first_text, doc.second_text][n] || "";
      const submitted = doc.events.some(
        (e) => e.task_id === r.task_id && e.phase === "submitted",
      );
      if (prompt && !submitted)
        pages.push({
          key: r.task_id + "-request",
          run: r,
          type: "request",
          name: "提交自然语言任务",
          detail: prompt,
          screen: "agent",
        });
      if (n === 0) for (const [e, i] of common) sourceEvent(e, r, i);
      doc.events.forEach((e, i) => {
        if (e.task_id === r.task_id) sourceEvent(e, r, i);
      });
      if (r.analysis || r.image)
        pages.push({
          key: r.task_id + "-frame",
          run: r,
          type: "observation",
          name: "读取本次观测",
          detail: "根据本次传感器回执展示分区与有效像素。",
          screen: "sensor",
        });
      if (r.review != null)
        pages.push({
          key: r.task_id + "-review",
          run: r,
          type: "review",
          name: "查看复核建议",
          detail: "根据插件回执显示复核顺序和未知区域。",
          screen: "sensor",
        });
    });
    return pages;
  }
  function sourceTime(e) {
    if (e?.utc) return String(e.utc).replace("T", " ");
    if (e?.stamp_ns) return String(e.stamp_ns) + " ns";
    return "未附时间戳";
  }
  function evidenceDetail(p) {
    const e = p.event,
      r = p.run || {};
    const s = e?.source || {};
    let description = text(p.detail),
      code = e?.code || p.proof || "";
    if (!e) return [description, code];
    if (e.phase === "operator.maintenance") {
      description =
        e.detail || "部署人员修正发布侧文件所有权并重新启动作者服务。";
      code = s.image_digest || s.image || s.container_id || code;
    }
    if (e.phase === "retrying") {
      description = p.count
        ? "连续记录 " + p.count + " 次发布重试。最近记录于 " + p.lastUtc
        : description;
      code = p.lastReason || s.reason || code;
    }
    if (e.phase === "planner.family_selected" && s.selected) {
      description = s.reason || description;
      code =
        "能力族  " +
        text(s.selected) +
        (s.provider ? "\n模型  " + s.provider : "");
    }
    if (
      e.phase === "plugin.reused" &&
      r.reuse?.same_binding &&
      s.to_task_id === r.task_id
    ) {
      description =
        "两次独立任务使用了同一插件绑定。" +
        (Number.isInteger(s.author_calls)
          ? "本次生成调用 " + s.author_calls + " 次。"
          : "");
      code =
        (s.tool_id || r.tool) + " · " + (s.version || "") + "\n" + s.binding;
    }
    if (e.phase === "author_requested" && s.model)
      description =
        "由 " +
        s.model +
        " 在边缘端生成候选程序。第 " +
        s.attempt +
        " 次尝试。";
    if (e.phase === "candidate_rejected" && (s.reason || s.code))
      code = text(s.reason || s.code);
    if (e.phase === "candidate_verified") {
      if (Number.isInteger(s.case_count))
        description =
          "候选 " + s.name + " 已通过 " + s.case_count + " 组独立验收。";
      if (
        r.candidate?.name === s.name &&
        r.release?.program_digest === s.program_sha256
      )
        code = Object.entries(r.candidate.program || {})
          .map(
            ([k, v]) =>
              k + "  " + (typeof v === "string" ? v : JSON.stringify(v)),
          )
          .join("\n");
    }
    if (e.phase === "plugin_loaded" && s.plugin_id) {
      description = s.plugin_id + " 已进入在线工具目录。";
      if (s.binding) code = "binding  " + s.binding;
    }
    if (e.phase === "continuation_submitted" && s.followup_task_id)
      code = s.followup_task_id;
    if (e.phase === "continuation_terminal" && s.state) code = s.state;
    if (e.phase === "measurement" && r.operation_id)
      code = r.tool + "\n" + r.operation_id;
    return [description, code];
  }
  function setView(view) {
    state.view = view;
    $("agent-screen").hidden = view !== "agent";
    $("sensor-screen").hidden = view !== "sensor";
    $("tab-agent").setAttribute("aria-pressed", String(view === "agent"));
    $("tab-sensor").setAttribute("aria-pressed", String(view === "sensor"));
    put("page-title", view === "agent" ? "Agent 决策" : "感知与规划");
    if (view === "sensor") showSensor();
  }
  function drawJournal() {
    const items = document.createDocumentFragment();
    state.pages.forEach((p, i) => {
      const b = el("button", "journal-button");
      b.type = "button";
      b.dataset.index = i;
      b.setAttribute("aria-current", String(i === state.cursor));
      const number = state.doc.runs.indexOf(p.run) + 1;
      b.append(
        el("span", "seq", String(i + 1).padStart(2, "0")),
        el("strong", "", p.name),
        el("small", "", number ? "任务 " + number : "现场"),
      );
      b.addEventListener("click", () => manualJump(i));
      items.append(b);
    });
    $("journal").replaceChildren(items);
    const row = $("journal").querySelector('[aria-current="true"]');
    if (row) {
      const top = row.offsetTop - row.parentElement.offsetTop;
      if (
        top < $("journal").scrollTop ||
        top + row.offsetHeight >
          $("journal").scrollTop + $("journal").clientHeight
      )
        $("journal").scrollTop = Math.max(
          0,
          top - $("journal").clientHeight * 0.42,
        );
    }
    put("journal-count", state.pages.length + " 条记录");
  }
  function jump(index, view) {
    if (!state.pages.length) return;
    state.cursor = Math.max(0, Math.min(index, state.pages.length - 1));
    const p = state.pages[state.cursor],
      e = p.event,
      run = p.run || {};
    const number = state.doc.runs.indexOf(run) + 1;
    const prompt =
      run.text ||
      [state.doc.first_text, state.doc.second_text][number - 1] ||
      state.doc.summary ||
      "现场记录";
    put(
      "task-number",
      number ? "任务 " + String(number).padStart(2, "0") : "现场",
    );
    const mode =
      run.mode_label ||
      state.doc.mode_label ||
      { live: "Jetson 实机运行", replay: "归档输入回放" }[state.doc.mode] ||
      state.doc.mode ||
      "任务记录";
    put("source-mode", mode);
    put("mission", prompt);
    put("task-id", run.task_id || "");
    const step = state.cursor + 1 + " / " + state.pages.length;
    put("event-position", step);
    put("scrub-index", step);
    $("scrub").max = state.pages.length - 1;
    $("scrub").value = state.cursor;
    $("back").disabled = state.cursor === 0;
    $("next").disabled = state.cursor === state.pages.length - 1;
    put(
      "event-class",
      p.type === "event"
        ? "Agent / 插件事件"
        : p.type === "request"
          ? "任务入口"
          : "观测回执",
    );
    put("event-title", p.name);
    const [detail, proof] = evidenceDetail(p);
    put(
      "event-detail",
      typeof detail === "object" && detail
        ? "来源事件返回了以下字段。"
        : detail,
    );
    const code =
      proof || (detail && typeof detail === "object" ? detail : null);
    $("event-code").hidden = !code;
    put("event-code", code == null ? "" : text(code));
    put(
      "event-key",
      e?.phase ||
        (p.type === "request"
          ? "任务原文"
          : p.type === "review"
            ? "review"
            : p.type === "observation"
              ? "analysis"
              : p.type === "comparison"
                ? "两项操作的回执对照"
                : "现场记录"),
    );
    put(
      "event-time",
      sourceTime(
        e ||
          (p.type === "observation"
            ? { stamp_ns: run.analysis?.rgb_stamp_ns }
            : null),
      ),
    );
    drawJournal();
    setView(view || p.screen);
  }
  function manualJump(i) {
    stopPlay();
    setFollow(false);
    jump(i);
  }
  function clockPlay() {
    if (state.cursor >= state.pages.length - 1) {
      stopPlay();
      return;
    }
    state.timer = setTimeout(() => {
      jump(state.cursor + 1);
      clockPlay();
    }, 4200);
  }
  // A LAN HTTP origin may not expose SubtleCrypto. Keep integrity checks available there, too.
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
  async function imageBytes(name, wanted) {
    let bytes;
    if (embedded) {
      const encoded = localImages[name];
      if (typeof encoded !== "string") throw Error("归档缺少原始帧");
      bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
    } else {
      const base = new URL(".", manifestURL),
        src = new URL(name, base);
      if (
        src.origin !== base.origin ||
        !src.pathname.startsWith(base.pathname) ||
        src.username ||
        src.password
      )
        throw Error("图像不在同站点的证据目录");
      const response = await fetch(src, {
        cache: "no-store",
        credentials: "same-origin",
        signal: AbortSignal.timeout(10000),
      });
      if (!response.ok || Number(response.headers.get("Content-Length")) > 16e6)
        throw Error("原始图像无法读取");
      bytes = new Uint8Array(await response.arrayBuffer());
    }
    if (bytes.length > 16e6) throw Error("原始帧超过预算");
    const isPNG =
      bytes[0] === 137 && bytes[1] === 80 && bytes[2] === 78 && bytes[3] === 71;
    const isJPEG = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
    if (!isPNG && !isJPEG) throw Error("不支持的归档图像格式");
    let verified = false;
    if (/^sha256:[0-9a-f]{64}$/i.test(wanted)) {
      const actual = globalThis.crypto?.subtle
        ? Array.from(
            new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
            (v) => v.toString(16).padStart(2, "0"),
          ).join("")
        : portableSha256(bytes);
      if (wanted.slice(7).toLowerCase() !== actual)
        throw Error("原始帧摘要不匹配");
      verified = true;
    }
    return { bytes, isPNG, verified };
  }
  async function loadImage(run) {
    if (typeof run.image !== "string" || !run.image) return null;
    const wanted = String(run.analysis?.rgb_ref || run.image_ref || "");
    const key = run.image + "|" + wanted;
    if (state.imageStore.has(key)) return state.imageStore.get(key);
    const { bytes, isPNG, verified } = await imageBytes(run.image, wanted);
    const url = URL.createObjectURL(
        new Blob([bytes], { type: isPNG ? "image/png" : "image/jpeg" }),
      ),
      img = new Image();
    img.src = url;
    try {
      await img.decode();
    } catch (err) {
      URL.revokeObjectURL(url);
      throw err;
    }
    if (state.imageStore.size >= 6) {
      const [old, entry] = state.imageStore.entries().next().value;
      state.imageStore.delete(old);
      URL.revokeObjectURL(entry.url);
    }
    const entry = {
      url,
      width: img.naturalWidth,
      height: img.naturalHeight,
      verified,
    };
    state.imageStore.set(key, entry);
    return entry;
  }
  function validBounds(b, w, h) {
    return (
      Array.isArray(b) &&
      b.length === 4 &&
      b.every(Number.isFinite) &&
      0 <= b[0] &&
      b[0] < b[2] &&
      b[2] <= w &&
      0 <= b[1] &&
      b[1] < b[3] &&
      b[3] <= h
    );
  }
  function resizeCamera() {
    if (!state.frame) return;
    const { w, h } = state.frame,
      fit = Math.min(
        ($("viewport").clientWidth - ($("depth-frame").hidden ? 0 : 8)) /
          ($("depth-frame").hidden ? w : 2 * w),
        $("viewport").clientHeight / h,
      );
    $("camera-frame").style.width = Math.floor(w * fit) + "px";
    $("camera-frame").style.height = Math.floor(h * fit) + "px";
    $("depth-frame").style.width = Math.floor(w * fit) + "px";
    $("depth-frame").style.height = Math.floor(h * fit) + "px";
  }
  // The deployed depth producer writes one 16-bit grayscale IDAT with unfiltered rows.
  // Decode the signed-receipt bytes directly: browser img/canvas would reduce them to 8-bit color.
  async function paintDepth(run, ticket) {
    const a = run.analysis || {},
      threshold = run.review?.distance_m;
    if (
      !run.depth_image ||
      !/^sha256:[0-9a-f]{64}$/.test(a.depth_ref) ||
      !Number.isFinite(threshold) ||
      !(a.depth_scale_m > 0) ||
      typeof DecompressionStream !== "function"
    )
      return;
    const { bytes, isPNG, verified } = await imageBytes(
      run.depth_image,
      a.depth_ref,
    );
    if (!isPNG || !verified || bytes.length < 56)
      throw Error("深度帧格式不匹配");
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength),
      w = view.getUint32(16),
      h = view.getUint32(20);
    if (
      view.getUint32(8) !== 13 ||
      view.getUint32(12) !== 0x49484452 ||
      w !== a.width ||
      h !== a.height ||
      w * h > 4194304 ||
      bytes[24] !== 16 ||
      bytes[25] !== 0 ||
      bytes[26] !== 0 ||
      bytes[27] !== 0 ||
      bytes[28] !== 0 ||
      view.getUint32(37) !== 0x49444154
    )
      throw Error("深度帧超出支持的采集格式");
    const length = view.getUint32(33),
      end = 41 + length;
    if (
      end + 16 !== bytes.length ||
      view.getUint32(end + 4) !== 0 ||
      view.getUint32(end + 8) !== 0x49454e44
    )
      throw Error("深度帧长度不匹配");
    const stream = new Blob([bytes.subarray(41, end)])
      .stream()
      .pipeThrough(new DecompressionStream("deflate"));
    const row = w * 2 + 1,
      raw = new Uint8Array(row * h),
      reader = stream.getReader();
    let offset = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (offset + value.length > raw.length) {
        await reader.cancel();
        throw Error("解码超出深度帧尺寸");
      }
      raw.set(value, offset);
      offset += value.length;
    }
    if (offset !== raw.length) throw Error("深度帧物理尺寸不匹配");
    if (ticket !== state.imageTicket) return;
    const canvas = $("depth-map");
    canvas.width = w;
    canvas.height = h;
    const context = canvas.getContext("2d"),
      rgba = context.createImageData(w, h);
    const palette = [
      [11, 18, 21],
      [239, 161, 86],
      [65, 124, 146],
    ];
    for (let y = 0; y < h; y++) {
      if (raw[y * row] !== 0) throw Error("不支持的深度行编码");
      for (let x = 0; x < w; x++) {
        const pos = y * row + 1 + 2 * x,
          n = raw[pos] * 256 + raw[pos + 1],
          p = 4 * (w * y + x);
        const c =
          palette[
            n === 0 || n === 65535
              ? 0
              : n * a.depth_scale_m <= threshold
                ? 1
                : 2
          ];
        rgba.data[p] = c[0];
        rgba.data[p + 1] = c[1];
        rgba.data[p + 2] = c[2];
        rgba.data[p + 3] = 255;
      }
    }
    context.putImageData(rgba, 0, 0);
    $("depth-frame").hidden = false;
    resizeCamera();
  }
  new ResizeObserver(resizeCamera).observe($("viewport"));
  async function camera(run) {
    const ticket = ++state.imageTicket;
    const a = run?.analysis || {},
      rv = run?.review || {};
    $("camera").hidden = true;
    $("depth-frame").hidden = true;
    $("region-overlay").replaceChildren();
    state.frame = null;
    put("image-check", "尚无可核对原始帧");
    put("frame-size", "");
    put("camera-note", run?.image ? "正在核对原始帧…" : "暂无本次彩色帧");
    put("camera-source", a.rgb_source_id || "");
    let im = null;
    if (run?.image) {
      try {
        im = await loadImage(run);
      } catch (err) {
        if (ticket === state.imageTicket)
          put("camera-note", "图像校验失败 · " + err.message);
        return;
      }
    }
    if (ticket !== state.imageTicket) return;
    const w = im?.width || a.width,
      h = im?.height || a.height;
    if (!(Number.isInteger(w) && Number.isInteger(h) && w > 0 && h > 0)) return;
    state.frame = { w, h };
    resizeCamera();
    put("frame-size", w + " × " + h + " px");
    if (im) {
      $("camera").src = im.url;
      $("camera").hidden = false;
      put("camera-note", "");
      put(
        "image-check",
        im.verified ? "原始 RGB · 内容摘要已匹配" : "原始图像 · 无内容摘要",
      );
    }
    if (!im) {
      put("camera-note", "仅按回执坐标显示分区");
      put("image-check", "未附原始彩色帧");
    }
    const aligned = !im || (im.verified && a.width === w && a.height === h);
    if (im && !aligned) {
      put("image-check", "图像与测量的对应关系未核验");
      return;
    }
    const cls = new Map(
      (Array.isArray(rv.regions) ? rv.regions : []).map((r) => [
        r.column,
        r.state,
      ]),
    );
    (Array.isArray(a.regions) ? a.regions : []).forEach((r, i) => {
      const b = r.bounds_xyxy;
      if (!validBounds(b, w, h)) return;
      const status = cls.get(i + 1);
      const z = el("div", "zone " + (classifications[status] ? status : ""));
      Object.assign(z.style, {
        left: (100 * b[0]) / w + "%",
        top: (100 * b[1]) / h + "%",
        width: (100 * (b[2] - b[0])) / w + "%",
        height: (100 * (b[3] - b[1])) / h + "%",
      });
      z.append(el("span", "", String(i + 1).padStart(2, "0")));
      if (Number.isFinite(r.value) && b[2] - b[0] >= w / 5) {
        z.append(el("b", "zone-value", fmt(r.value) + " " + (a.unit || "")));
      }
      $("region-overlay").append(z);
    });
    if (im?.verified && aligned) {
      try {
        await paintDepth(run, ticket);
      } catch (err) {
        if (ticket === state.imageTicket)
          put("image-check", "RGB 已核对 · 深度阈值图不可用");
      }
    }
  }
  function showSensor() {
    const p = state.pages[state.cursor],
      run = p?.run;
    const ready =
      p?.screen === "sensor" || p?.event?.observation_available === true;
    const source = ready ? run : null;
    $("sensor-screen").dataset.focus =
      p?.type === "review" ? "review" : "observation";
    const a = source?.analysis || {},
      review = source?.review || {};
    const regions = Array.isArray(review.regions) ? review.regions : [];
    const measured = Array.isArray(a.regions) ? a.regions : [];
    const index = new Map(regions.map((r) => [r.column, r]));
    void camera(source);
    put(
      "threshold",
      Number.isFinite(review.distance_m) ? fmt(review.distance_m) + " m" : "—",
    );
    const order = $("order-list");
    order.replaceChildren();
    if (Array.isArray(review.review_order) && review.review_order.length) {
      review.review_order.forEach((c, n) => {
        const row = el("div", "order"),
          v = index.get(c)?.depth_m;
        row.append(
          el("span", "number", String(n + 1).padStart(2, "0")),
          el("span", "", "第 " + c + " 区"),
          el("span", "value", Number.isFinite(v) ? fmt(v) + " m" : "未知"),
        );
        order.append(row);
      });
    } else
      order.append(
        el(
          "p",
          "muted",
          Array.isArray(review.review_order)
            ? "回执未列出近距项"
            : "当前阶段尚无复核回执",
        ),
      );
    put(
      "unknown",
      Array.isArray(review.unknown_columns)
        ? review.unknown_columns.map((c) => "第 " + c + " 区").join("、") ||
            "无"
        : "未报告",
    );
    put(
      "scope",
      Array.isArray(review.review_order)
        ? "按相机光轴距离复核，通行性仍待现场确认。"
        : "只读观测与人工复核。",
    );
    const statistic = review.statistic || a.statistic;
    const statName = {
      p10: "有效深度第 10 百分位",
      p25: "有效深度第 25 百分位",
      "quantile(0.1)": "有效深度第 10 百分位",
      "quantile(0.25)": "有效深度第 25 百分位",
      median: "中位数",
    };
    put("measure-title", statName[statistic] || statistic || "");
    const container = $("measurements");
    container.replaceChildren();
    measured.forEach((r, i) => {
      const match = index.get(i + 1) || {},
        status = match.state || "",
        frac = Number.isFinite(match.valid_fraction)
          ? match.valid_fraction
          : r.pixels > 0
            ? r.valid_pixels / r.pixels
            : null;
      const m = el("div", "measure " + (classifications[status] ? status : "")),
        val = Number.isFinite(r.value)
          ? fmt(r.value) + " " + (a.unit || "")
          : "未知";
      m.append(
        el("span", "", "第 " + (i + 1) + " 区"),
        el("strong", "", val),
        el(
          "small",
          "",
          (classifications[status] || "区域读数") +
            " · 有效率 " +
            percent(frac),
        ),
      );
      container.append(m);
    });
    if (!measured.length)
      container.append(
        el(
          "p",
          "muted",
          ready ? "本次回执未给出分区数值。" : "等待该任务的观测事件。",
        ),
      );
  }
  function accept(doc) {
    const pages = taskPages(doc),
      old = state.pages[state.cursor]?.key,
      prior = state.view;
    state.doc = doc;
    state.pages = pages;
    $("workspace").hidden = !pages.length;
    $("empty-state").hidden = !!pages.length;
    note("");
    connection(
      archived ? "已封存回放" : state.follow ? "跟随更新" : "任务回放",
      "good",
    );
    if (pages.length) {
      let i = state.follow
        ? pages.length - 1
        : pages.findIndex((p) => p.key === old);
      if (i < 0) i = 0;
      jump(i, !state.follow && old ? prior : null);
    }
  }
  async function refresh() {
    if (invalidArchive) {
      connection("归档标识无效", "warn");
      put(
        "empty-message",
        "归档标识只允许 1 至 64 个字母、数字、下划线或连字符，开头须为字母或数字。",
      );
      $("empty-state").hidden = false;
      $("retry").disabled = true;
      return;
    }
    if (state.pending) return;
    state.pending = true;
    try {
      let source;
      if (embedded) source = embedded.textContent;
      else {
        const res = await fetch(manifestURL, {
          cache: "no-store",
          credentials: "same-origin",
          signal: AbortSignal.timeout(5000),
        });
        if (!res.ok) throw Error("HTTP " + res.status);
        source = await res.text();
        if (source.length > 8e6) throw Error("记录过大");
      }
      if (source !== state.cached) {
        const doc = JSON.parse(source);
        accept(doc);
        state.cached = source;
      } else {
        note("");
        connection(
          archived ? "已封存回放" : state.follow ? "跟随更新" : "任务回放",
          "good",
        );
      }
    } catch (err) {
      const warning = "无法读取现场记录 · " + err.message;
      connection("数据暂不可用", "warn");
      if (state.pages.length)
        note(warning + "。当前显示最近一次成功读取的记录。");
      else {
        put("empty-message", warning + "。等待本次任务的记录发布。");
        $("empty-state").hidden = false;
      }
    } finally {
      state.pending = false;
    }
  }
  $("tab-agent").onclick = () => setView("agent");
  $("tab-sensor").onclick = () => setView("sensor");
  $("scrub").oninput = (e) => manualJump(Number(e.target.value));
  $("back").onclick = () => manualJump(state.cursor - 1);
  $("next").onclick = () => manualJump(state.cursor + 1);
  $("play").onclick = () => {
    if (state.timer) {
      stopPlay();
      return;
    }
    setFollow(false);
    if (state.cursor === state.pages.length - 1) jump(0);
    put("play", "暂停");
    clockPlay();
  };
  $("follow").onclick = () => {
    stopPlay();
    setFollow(!state.follow);
    if (state.follow && state.pages.length) jump(state.pages.length - 1);
    refresh();
  };
  $("follow").disabled = archived;
  $("retry").onclick = refresh;
  setFollow(state.follow);
  refresh();
  if (!archived)
    setInterval(() => {
      if (state.follow) refresh();
    }, 2000);
})();
