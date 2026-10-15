"use strict";

const TOKEN = document.querySelector('meta[name="studio-token"]').content;
const $ = (sel) => document.querySelector(sel);

const state = {
  config: null,          // {providers, default_provider, skill}
  convs: [],             // sidebar list
  conv: null,            // the open conversation, or null
  draft: { provider_id: null, model: "", use_skill: true, thinking: true, effort: "high" },  // top bar values with no conversation open
  pending: [],           // images attached to the next message: {name, url, uploading}
  streaming: null,       // AbortController of the reply being streamed
  autoRounds: 0,         // automatic fix requests sent since the user last wrote
};

// ---------------------------------------------------------------- api

async function api(path, { method = "GET", body, raw, headers = {} } = {}) {
  const opts = { method, headers: { "X-Studio-Token": TOKEN, ...headers } };
  if (raw !== undefined) {
    opts.body = raw;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(text, isError = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = "show" + (isError ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.className = ""), isError ? 6000 : 2500);
}

// Browser storage may be unavailable (private mode, blocked site data): fall back to defaults
function storageGet(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : v;
  } catch {
    return fallback;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* not persisted; the switch still works for this page */
  }
}

// ---------------------------------------------------------------- markdown

const hasMarked = typeof marked !== "undefined";
const hasPurify = typeof DOMPurify !== "undefined";
const hasCodeMirror = typeof CodeMirror !== "undefined";

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Model output as safe HTML: without both libraries it is shown as plain text */
function mdToHtml(tokensOrText) {
  if (!hasMarked || !hasPurify) {
    const text = typeof tokensOrText === "string" ? tokensOrText : tokensOrText.map((t) => t.raw).join("");
    return `<div style="white-space:pre-wrap">${escapeHtml(text)}</div>`;
  }
  const html = typeof tokensOrText === "string" ? marked.parse(tokensOrText) : marked.parser(tokensOrText);
  return DOMPurify.sanitize(html);
}

function isRunnable(token) {
  const lang = (token.lang || "").trim().toLowerCase();
  if (lang === "python" || lang === "py" || lang === "python3") return true;
  return lang === "" && /from\s+manimlib\s+import|class\s+\w+\s*\(.*Scene/.test(token.text);
}

function findScenes(code) {
  const scenes = [];
  const others = [];
  for (const m of code.matchAll(/^class\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*:/gm)) {
    (/Scene/.test(m[2]) ? scenes : others).push(m[1]);
  }
  return scenes.length ? scenes : others;
}

// ---------------------------------------------------------------- sidebar and top bar

function providerById(id) {
  return state.config.providers.find((p) => p.id === id) || state.config.providers[0];
}

function current() {
  return state.conv || state.draft;
}

function renderConvList() {
  const list = $("#conv-list");
  list.innerHTML = "";
  for (const c of state.convs) {
    const li = document.createElement("li");
    if (state.conv && state.conv.id === c.id) li.classList.add("active");
    const when = new Date((c.updated || c.created) * 1000);
    const provider = state.config.providers.find((p) => p.id === c.provider_id);
    li.innerHTML = `
      <div class="conv-text">
        <div class="conv-title"></div>
        <div class="conv-meta"></div>
      </div>
      <button class="conv-del" title="删除对话">✕</button>`;
    li.querySelector(".conv-title").textContent = c.title || "新对话";
    li.querySelector(".conv-meta").textContent =
      `${provider ? provider.name : c.provider_id} · ${c.model || ""} · ${when.toLocaleString()}`;
    li.addEventListener("click", () => openConv(c.id));
    li.querySelector(".conv-del").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteConv(c.id, c.title);
    });
    list.appendChild(li);
  }
}

function renderTopbar() {
  const cur = current();
  const select = $("#provider");
  select.innerHTML = "";
  for (const p of state.config.providers) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name + (p.has_key ? "" : "（未设置 Key）");
    select.appendChild(opt);
  }
  select.value = cur.provider_id;
  const provider = providerById(cur.provider_id);
  $("#model").value = cur.model || provider.model;
  const options = $("#model-options");
  options.innerHTML = "";
  for (const m of new Set([provider.model, ...(provider.models || [])])) {
    const opt = document.createElement("option");
    opt.value = m;
    options.appendChild(opt);
  }
  $("#use-skill").checked = cur.use_skill !== false;
  // One select for thinking: "off" switches DeepSeek's thinking off; Claude always thinks, so
  // it has no "off"; plain OpenAI-style providers have neither
  const reasoning = provider.reasoning || "none";
  $("#effort-wrap").classList.toggle("hidden", reasoning === "none");
  $("#effort").querySelector('option[value="off"]').hidden = reasoning !== "deepseek";
  const off = cur.thinking === false && reasoning === "deepseek";
  $("#effort").value = off ? "off" : cur.effort || "high";
  $("#conv-title").textContent = state.conv ? state.conv.title : "新对话";
  renderConvStats();
  $("#close-chat").disabled = !state.conv;
  $("#delete-chat").disabled = !state.conv;
  const skill = state.config.skill;
  $("#skill-status").textContent = skill.found
    ? `Skill 已就绪：${skill.name}（约 ${Math.round(skill.approx_tokens / 1000)}k tokens）`
    : `未找到 Skill：${skill.path}`;
  updateHint();
}

async function updateConvSettings(patch) {
  if (!state.conv) {
    Object.assign(state.draft, patch);
    return;
  }
  Object.assign(state.conv, patch);
  try {
    await api(`/api/conversations/${state.conv.id}`, { method: "PUT", body: patch });
    await refreshConvList();
  } catch (e) {
    toast(e.message, true);
  }
}

function updateHint() {
  const hint = $("#composer-hint");
  const provider = providerById(current().provider_id);
  const msgs = [];
  if (!provider.has_key) msgs.push(`⚠ ${provider.name} 还没有设置 API Key，点左下角“设置”填写。`);
  if (state.pending.length && !provider.vision) msgs.push(`⚠ ${provider.name} 当前配置为不支持图片，发送图片前请换用 OpenAI / Claude 等支持图片的模型。`);
  if (!state.config.skill.found && $("#use-skill").checked) msgs.push("⚠ 没找到 Skill 文件，开关打开也不会附带技能规则。");
  hint.textContent = msgs.join("  ");
  hint.classList.toggle("warn", msgs.length > 0);
}

async function refreshConvList() {
  state.convs = await api("/api/conversations");
  renderConvList();
}

// ---------------------------------------------------------------- conversations

async function openConv(id) {
  if (state.streaming) return toast("正在生成回复，请先等待或停止。");
  try {
    state.conv = await api(`/api/conversations/${id}`);
  } catch (e) {
    toast(e.message, true);
    return;
  }
  renderTopbar();
  renderConvList();
  renderMessages();
}

function closeConv() {
  if (state.streaming) return toast("正在生成回复，请先等待或停止。");
  if (state.conv) {
    state.draft = { provider_id: state.conv.provider_id, model: state.conv.model, use_skill: state.conv.use_skill,
      thinking: state.conv.thinking, effort: state.conv.effort };
  }
  state.conv = null;
  renderTopbar();
  renderConvList();
  renderMessages();
}

async function deleteConv(id, title) {
  if (state.streaming && state.conv && state.conv.id === id) return toast("正在生成回复，请先停止。");
  if (!confirm(`确定删除对话“${title || "新对话"}”吗？对话记录和其中的图片都会被删除。`)) return;
  try {
    await api(`/api/conversations/${id}`, { method: "DELETE" });
  } catch (e) {
    return toast(e.message, true);
  }
  if (state.conv && state.conv.id === id) closeConv();
  await refreshConvList();
  toast("已删除");
}

async function renameConv() {
  if (!state.conv) return;
  const title = prompt("对话标题", state.conv.title);
  if (title && title.trim()) {
    await updateConvSettings({ title: title.trim() });
    $("#conv-title").textContent = state.conv.title;
  }
}

// ---------------------------------------------------------------- messages

function renderMessages() {
  const box = $("#messages");
  box.innerHTML = "";
  if (!state.conv || !state.conv.messages.length) {
    box.appendChild(welcomeEl);
    return;
  }
  state.conv.messages.forEach((msg, index) => box.appendChild(renderMessage(msg, index)));
  box.scrollTop = box.scrollHeight;
}

function renderMessage(msg, index) {
  const el = document.createElement("div");
  el.className = `msg ${msg.role}`;
  if (msg.role === "user") {
    if (msg.images && msg.images.length) {
      const imgs = document.createElement("div");
      imgs.className = "msg-images";
      for (const name of msg.images) {
        const img = document.createElement("img");
        img.src = `/uploads/${name}`;
        img.addEventListener("click", () => viewImage(img.src));
        imgs.appendChild(img);
      }
      el.appendChild(imgs);
    }
    if (msg.content) {
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = msg.content;
      el.appendChild(bubble);
    }
    return el;
  }
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = `${msg.provider || "AI"} · ${msg.model || ""}`;
  el.appendChild(who);
  if (msg.thinking) el.appendChild(thinkingEl(msg.thinking, false));
  const content = document.createElement("div");
  content.className = "content";
  el.appendChild(content);
  renderAssistantContent(content, msg, index);
  for (const n of msg.notices || []) {
    const note = document.createElement("div");
    note.className = "notice";
    note.textContent = n;
    el.appendChild(note);
  }
  if (msg.usage) {
    const first = state.conv && state.conv.messages.findIndex((m) => m.usage) === index;
    el.appendChild(usageEl(msg.usage, first));
  }
  return el;
}

const fmt = (n) => (n || 0).toLocaleString("en-US");
const money = (usd) => (usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(3)}`);

/** One line of token counts under a reply: input with its cache hits, output, estimated cost */
function usageEl(u, firstReply) {
  const el = document.createElement("div");
  el.className = "usage-line";
  const rate = u.input ? (u.cache_hit / u.input) * 100 : 0;
  const parts = [
    `输入 ${fmt(u.input)}`,
    `缓存命中 ${fmt(u.cache_hit)}（${rate.toFixed(1)}%）`,
    `输出 ${fmt(u.output)}` + (u.reasoning ? `（含思考 ${fmt(u.reasoning)}）` : ""),
  ];
  if (u.cost_usd !== undefined) {
    parts.push(`约 ${money(u.cost_usd)}` + (u.peak ? "（高峰价）" : ""));
    if (u.saved_usd > 0) parts.push(`缓存省下 ${money(u.saved_usd)}`);
  }
  el.textContent = parts.join(" · ");
  if (rate >= 50) el.classList.add("good");
  if (firstReply && rate < 50) {
    el.title = "第一次请求要先把提示词写进缓存，从下一条消息起，相同的前缀就会命中缓存";
    el.textContent += " · 首轮需建立缓存";
  }
  return el;
}

/** Totals for the open conversation, shown under its title */
function renderConvStats() {
  const el = $("#conv-stats");
  const usages = state.conv ? state.conv.messages.filter((m) => m.usage).map((m) => m.usage) : [];
  if (!usages.length) {
    el.textContent = "";
    return;
  }
  const sum = (k) => usages.reduce((a, u) => a + (u[k] || 0), 0);
  const input = sum("input");
  const hit = sum("cache_hit");
  let text = `本对话 ${usages.length} 次请求 · 输入 ${fmt(input)}，缓存命中 ${input ? ((hit / input) * 100).toFixed(1) : 0}% · 输出 ${fmt(sum("output"))}`;
  if (usages.some((u) => u.cost_usd !== undefined)) {
    text += ` · 约 ${money(sum("cost_usd"))}，缓存省下 ${money(sum("saved_usd"))}`;
  }
  el.textContent = text;
}

/** Changes that alter the prompt prefix make the next request miss the cache: say so */
function warnCacheChange(what) {
  if (state.conv && state.conv.messages.length) {
    toast(`${what}会改变请求开头的内容，下一条消息将无法命中缓存（之后会重新建立）。`);
  }
}

function thinkingEl(text, open) {
  const d = document.createElement("details");
  d.className = "thinking";
  d.open = open;
  d.innerHTML = `<summary>思考过程</summary><div class="thinking-text"></div>`;
  d.querySelector(".thinking-text").textContent = text;
  return d;
}

/** Markdown with each runnable Python block turned into an editable, runnable code card */
function renderAssistantContent(container, msg, index) {
  if (!hasMarked) {
    container.innerHTML = mdToHtml(msg.content);
    return;
  }
  const tokens = marked.lexer(msg.content);
  let group = [];
  const flush = () => {
    if (!group.length) return;
    group.links = tokens.links;
    const div = document.createElement("div");
    div.innerHTML = mdToHtml(group);
    container.appendChild(div);
    group = [];
  };
  for (const token of tokens) {
    if (token.type === "code" && isRunnable(token)) {
      flush();
      container.appendChild(codeCard(token, msg, index));
    } else {
      group.push(token);
    }
  }
  flush();
}

// ---------------------------------------------------------------- code cards

function codeCard(token, msg, msgIndex) {
  const card = document.createElement("div");
  card.className = "code-card";
  card.innerHTML = `
    <div class="cc-head">
      <span class="cc-title">python</span>
      <span class="cc-dirty hidden">● 未保存的修改</span>
      <select class="cc-scene" title="要运行的场景类"></select>
      <select class="cc-quality" title="渲染质量（只影响“渲染视频”）">
        <option value="l">480p</option>
        <option value="m">720p</option>
        <option value="hd">1080p</option>
      </select>
      <button class="btn small cc-check" title="跳过动画、只渲染最后一帧，几秒钟内检查所有场景能不能跑通">✔ 检查</button>
      <button class="btn small cc-preview" title="弹出 ManimGL 窗口实时预览，按 Esc 关闭">▶ 预览窗口</button>
      <button class="btn small primary cc-render" title="渲染成 mp4 并在下方播放">🎬 渲染视频</button>
      <button class="btn small cc-save" disabled title="把修改保存到对话记录里">保存修改</button>
      <button class="btn small cc-copy">复制</button>
    </div>
    <div class="cc-editor"></div>
    <div class="cc-run hidden">
      <div class="cc-status">
        <span class="dot"></span><span class="cc-status-text grow"></span>
        <button class="btn small cc-fix hidden" title="把报错发给 AI 修复">🛠 发给 AI 修复</button>
        <button class="btn small cc-folder" title="打开脚本和视频所在的文件夹">📂</button>
        <button class="btn small danger cc-stop hidden">■ 停止</button>
        <button class="btn small cc-log-toggle">日志</button>
      </div>
      <pre class="cc-log hidden"></pre>
      <video class="cc-video hidden" controls></video>
      <img class="cc-image hidden" alt="最后一帧">
    </div>`;

  const q = (s) => card.querySelector(s);
  let savedCode = token.text;
  let raw = token.raw;

  // editor
  let getCode;
  const editorBox = q(".cc-editor");
  if (hasCodeMirror) {
    const cm = CodeMirror(editorBox, {
      value: token.text,
      mode: "python",
      theme: "material-darker",
      lineNumbers: true,
      indentUnit: 4,
      tabSize: 4,
      viewportMargin: Infinity,
      extraKeys: { Tab: (c) => c.replaceSelection("    ") },
    });
    getCode = () => cm.getValue();
    cm.on("change", onEdit);
    setTimeout(() => cm.refresh(), 0);
  } else {
    const ta = document.createElement("textarea");
    ta.className = "fallback-editor";
    ta.spellcheck = false;
    ta.value = token.text;
    editorBox.appendChild(ta);
    getCode = () => ta.value;
    ta.addEventListener("input", onEdit);
  }

  function refreshScenes() {
    const select = q(".cc-scene");
    const before = select.value;
    const scenes = findScenes(getCode());
    select.innerHTML = "";
    for (const s of scenes) {
      const opt = document.createElement("option");
      opt.value = opt.textContent = s;
      select.appendChild(opt);
    }
    if (scenes.includes(before)) select.value = before;
    const none = !scenes.length;
    q(".cc-preview").disabled = none;
    q(".cc-render").disabled = none;
    q(".cc-check").disabled = none;
    q(".cc-title").textContent = none ? "python（没找到 Scene 类）" : `python · ${scenes.length} 个场景`;
  }

  function onEdit() {
    const dirty = getCode() !== savedCode;
    q(".cc-dirty").classList.toggle("hidden", !dirty);
    q(".cc-save").disabled = !dirty;
    refreshScenes();
  }

  async function save() {
    const code = getCode();
    if (code === savedCode) return true;
    const fence = raw.match(/^\s*(`{3,}|~{3,})/);
    const mark = fence ? fence[1] : "```";
    const trail = (raw.match(/\n*$/) || [""])[0];
    const newRaw = `${mark}${token.lang || "python"}\n${code.replace(/\n+$/, "")}\n${mark}${trail}`;
    const content = msg.content.includes(raw) ? msg.content.replace(raw, () => newRaw) : msg.content;
    try {
      await api(`/api/conversations/${state.conv.id}/messages/${msgIndex}`, { method: "PUT", body: { content } });
    } catch (e) {
      toast("保存失败：" + e.message, true);
      return false;
    }
    msg.content = content;
    raw = newRaw;
    savedCode = code;
    onEdit();
    return true;
  }

  q(".cc-save").addEventListener("click", async () => {
    const earlier = state.conv && msgIndex < state.conv.messages.length - 1;
    if (await save()) {
      toast(earlier
        ? "已保存。注意：修改的是较早的消息，下一条消息从这里往后无法命中缓存。"
        : "已保存到对话记录");
    }
  });
  q(".cc-copy").addEventListener("click", async () => {
    await navigator.clipboard.writeText(getCode());
    toast("代码已复制");
  });
  q(".cc-log-toggle").addEventListener("click", () => q(".cc-log").classList.toggle("hidden"));
  q(".cc-folder").addEventListener("click", () =>
    api("/api/open-folder", { method: "POST", body: { conv_id: state.conv.id } }).catch((e) => toast(e.message, true)));

  let job = null;
  let failedScene = null;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const LABELS = { preview: "预览", render: "渲染", check: "检查" };

  /** Starts a run and resolves with the finished job (or null if it could not start) */
  async function run(mode, scene = q(".cc-scene").value) {
    if (job && job.status === "running") {
      toast("上一次运行还没结束，可以先停止。");
      return null;
    }
    if (!scene) return null;
    await save(); // run what is on screen, and keep the conversation in step with it
    q(".cc-run").classList.remove("hidden");
    for (const s of [".cc-video", ".cc-image"]) {
      q(s).classList.add("hidden");
      q(s).removeAttribute("src");
    }
    q(".cc-fix").classList.add("hidden");
    q(".cc-log").textContent = "";
    q(".cc-log").classList.toggle("hidden", mode !== "render");
    setStatus("running", mode === "preview" ? `正在打开预览窗口：${scene} …` : `正在${LABELS[mode]} ${scene} …`);
    try {
      job = await api("/api/run", {
        method: "POST",
        body: { conv_id: state.conv.id, code: getCode(), scene, mode, quality: q(".cc-quality").value },
      });
    } catch (e) {
      setStatus("failed", "启动失败：" + e.message);
      return null;
    }
    q(".cc-stop").classList.remove("hidden");
    return await follow();
  }

  async function follow() {
    const log = q(".cc-log");
    for (;;) {
      await sleep(600);
      try {
        job = await api(`/api/run/${job.id}`);
      } catch (e) {
        setStatus("failed", e.message);
        return null;
      }
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
      log.textContent = job.log;
      if (atBottom) log.scrollTop = log.scrollHeight;
      const seconds = Math.round(Date.now() / 1000 - job.started);
      if (job.status === "running") {
        setStatus("running", job.mode === "preview"
          ? `预览运行中（${seconds}s）· 窗口可能在其他窗口后面，看看任务栏；在窗口里按 Esc 关闭`
          : `${LABELS[job.mode]}中 ${job.scene}（${seconds}s）…`);
        continue;
      }
      q(".cc-stop").classList.add("hidden");
      if (job.status === "done") {
        failedScene = null;
        log.classList.add("hidden");
        if (job.video_url) {
          const video = q(".cc-video");
          video.src = job.video_url;
          video.classList.remove("hidden");
          setStatus("done", `渲染完成（${seconds}s）：${job.video_url.split("/").pop()}`);
        } else if (job.image_url) {
          const img = q(".cc-image");
          img.src = job.image_url;
          img.classList.remove("hidden");
          setStatus("done", `✔ ${job.scene} 检查通过（${seconds}s），下面是最后一帧`);
        } else {
          setStatus("done", job.mode === "preview" ? `预览已结束（${seconds}s）` : "运行结束，但没有找到输出文件");
        }
      } else if (job.status === "stopped") {
        setStatus("stopped", "已停止");
      } else {
        failedScene = job.scene;
        setStatus("failed", `${job.scene} 运行失败（退出码 ${job.returncode}），看下方日志，或把报错发给 AI 修复`);
        log.classList.remove("hidden");
        q(".cc-fix").classList.remove("hidden");
        log.scrollTop = log.scrollHeight;
      }
      return job;
    }
  }

  function setStatus(kind, text) {
    q(".cc-status").className = `cc-status ${kind}`;
    q(".cc-status-text").textContent = text;
  }

  /** The request that asks the model to fix the last failed run, with the real signatures */
  function fixMessage() {
    const tail = (job && job.log ? job.log : "").split("\n").slice(-40).join("\n");
    let text = `我运行上面（最新版本）代码里的场景 ${failedScene || q(".cc-scene").value} 时出错了，报错日志末尾如下：\n` +
      "```\n" + tail + "\n```\n";
    if (job && job.api_hint) {
      text += "\n报错涉及的类在本机 manimlib 中的真实签名如下（以此为准）：\n```\n" + job.api_hint + "\n```\n";
    }
    return text + "请对照真实签名分析原因，并给出修正后的完整代码。";
  }

  /** Checks every scene in turn, stopping at the first failure */
  card.checkAll = async () => {
    for (const scene of findScenes(getCode())) {
      q(".cc-scene").value = scene;
      const done = await run("check", scene);
      if (!done) return { ok: false, aborted: true };
      if (done.status !== "done") return { ok: false, aborted: done.status === "stopped" };
    }
    return { ok: true };
  };
  card.fixMessage = fixMessage;

  q(".cc-check").addEventListener("click", () => card.checkAll());
  q(".cc-preview").addEventListener("click", () => run("preview"));
  q(".cc-render").addEventListener("click", () => run("render"));
  q(".cc-stop").addEventListener("click", async () => {
    if (!job) return;
    await api(`/api/run/${job.id}/stop`, { method: "POST" }).catch((e) => toast(e.message, true));
  });
  q(".cc-fix").addEventListener("click", () => setInput(fixMessage()));

  refreshScenes();
  return card;
}

// ---------------------------------------------------------------- composer

const welcomeEl = $("#welcome");

function setInput(text) {
  const input = $("#input");
  input.value = text;
  autoGrow();
  input.focus();
  input.setSelectionRange(text.length, text.length);
}

function autoGrow() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 220) + "px";
}

function renderAttachments() {
  const box = $("#attachments");
  box.innerHTML = "";
  state.pending.forEach((item, i) => {
    const div = document.createElement("div");
    div.className = "thumb" + (item.uploading ? " uploading" : "");
    div.innerHTML = `<img alt=""><button title="移除">✕</button>`;
    div.querySelector("img").src = item.preview;
    div.querySelector("button").addEventListener("click", () => {
      state.pending.splice(i, 1);
      renderAttachments();
    });
    box.appendChild(div);
  });
  updateHint();
}

async function addImages(files) {
  for (const file of files) {
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) {
      toast(`不支持的文件类型：${file.name || file.type}`, true);
      continue;
    }
    if (file.size > 10 * 1024 * 1024) {
      toast("图片不能超过 10MB", true);
      continue;
    }
    const item = { preview: URL.createObjectURL(file), uploading: true };
    state.pending.push(item);
    renderAttachments();
    try {
      const res = await api("/api/upload", { method: "POST", raw: file, headers: { "Content-Type": file.type } });
      item.name = res.name;
      item.uploading = false;
    } catch (e) {
      state.pending.splice(state.pending.indexOf(item), 1);
      toast("图片上传失败：" + e.message, true);
    }
    renderAttachments();
  }
}

function setSending(on) {
  const btn = $("#send");
  btn.textContent = on ? "■ 停止" : "发送";
  btn.classList.toggle("primary", !on);
  btn.classList.toggle("danger", on);
}

const MAX_AUTO_FIX = 2;

/**
 * After a reply: check the last code block in it, and if a scene fails, send the error with the
 * real signatures back to the model, at most MAX_AUTO_FIX times in a row
 */
async function autoCheck() {
  const convId = state.conv && state.conv.id;
  const replies = document.querySelectorAll("#messages .msg.assistant");
  const last = replies[replies.length - 1];
  const cards = last ? last.querySelectorAll(".code-card") : [];
  if (!cards.length) return;
  const card = cards[cards.length - 1];
  card.scrollIntoView({ block: "nearest" });
  const result = await card.checkAll();
  if (result.ok || result.aborted || !state.conv || state.conv.id !== convId || state.streaming) return;
  if (state.autoRounds >= MAX_AUTO_FIX) {
    toast(`已自动修复 ${MAX_AUTO_FIX} 轮仍未通过，请看日志后手动处理或换个说法。`, true);
    return;
  }
  state.autoRounds += 1;
  toast(`检查未通过，正在自动把报错发给模型修复（第 ${state.autoRounds}/${MAX_AUTO_FIX} 轮）`);
  setInput(card.fixMessage());
  await send(true);
}

async function send(auto = false) {
  if (state.streaming) {
    state.streaming.abort();
    return;
  }
  if (!auto) state.autoRounds = 0;
  const text = $("#input").value.trim();
  if (state.pending.some((p) => p.uploading)) return toast("图片还在上传，请稍等");
  const images = state.pending.map((p) => p.name);
  if (!text && !images.length) return;

  const cur = current();
  const provider = providerById(cur.provider_id);
  if (images.length && !provider.vision) {
    return toast(`${provider.name} 当前配置为不支持图片输入，请换用支持图片的模型。`, true);
  }
  const model = $("#model").value.trim() || provider.model;
  const useSkill = $("#use-skill").checked;
  const effortChoice = $("#effort").value;
  const thinkingOn = effortChoice !== "off";
  const effort = thinkingOn ? effortChoice : cur.effort || "high";

  if (!state.conv) {
    try {
      state.conv = await api("/api/conversations", {
        method: "POST",
        body: { provider_id: cur.provider_id, model, use_skill: useSkill, thinking: thinkingOn, effort },
      });
    } catch (e) {
      return toast(e.message, true);
    }
    renderTopbar();
  }
  const conv = state.conv;

  // show the question and an empty reply right away
  const box = $("#messages");
  if (welcomeEl.parentNode) box.innerHTML = "";
  const userMsg = { role: "user", content: text, images };
  box.appendChild(renderMessage(userMsg, -1));
  const replyEl = document.createElement("div");
  replyEl.className = "msg assistant";
  replyEl.innerHTML = `<div class="who"></div><div class="content cursor"></div>`;
  const reasoning = provider.reasoning || "none";
  const thinkLabel = { low: "低", medium: "中", high: "高", max: "最高" }[effort];
  const thinkTag = reasoning === "claude" || (reasoning === "deepseek" && thinkingOn) ? ` · 思考(${thinkLabel})` : "";
  replyEl.querySelector(".who").textContent = `${provider.name} · ${model}${thinkTag}${useSkill ? " · Skill" : ""}`;
  box.appendChild(replyEl);
  box.scrollTop = box.scrollHeight;

  $("#input").value = "";
  autoGrow();
  const sentPending = state.pending;
  state.pending = [];
  renderAttachments();

  const controller = new AbortController();
  state.streaming = controller;
  setSending(true);

  let answer = "";
  let thinking = "";
  let thinkingBox = null;
  let renderQueued = false;
  const contentEl = replyEl.querySelector(".content");
  const scheduleRender = () => {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 60;
      contentEl.innerHTML = mdToHtml(answer);
      if (thinking) {
        if (!thinkingBox) {
          thinkingBox = thinkingEl("", true);
          replyEl.insertBefore(thinkingBox, contentEl);
        }
        const textEl = thinkingBox.querySelector(".thinking-text");
        textEl.textContent = thinking;
        if (thinkingBox.open && !answer) textEl.scrollTop = textEl.scrollHeight;
        // fold the reasoning away once, when the answer starts; reopening it afterwards sticks
        if (answer && !thinkingBox.dataset.folded) {
          thinkingBox.open = false;
          thinkingBox.dataset.folded = "1";
        }
        thinkingBox.querySelector("summary").textContent =
          answer ? `思考过程（${thinking.length} 字）` : `思考中…（${thinking.length} 字）`;
      }
      if (stick) box.scrollTop = box.scrollHeight;
    });
  };

  let errorText = null;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "X-Studio-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ conv_id: conv.id, text, images, provider_id: cur.provider_id, model, use_skill: useSkill, thinking: thinkingOn, effort }),
      signal: controller.signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (!block.startsWith("data: ")) continue;
        const ev = JSON.parse(block.slice(6));
        if (ev.type === "text") answer += ev.text;
        else if (ev.type === "thinking") thinking += ev.text;
        else if (ev.type === "notice") toast(ev.text);
        else if (ev.type === "error") errorText = ev.text;
        else if (ev.type === "usage") replyEl.appendChild(usageEl(ev.usage, false));
        scheduleRender();
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") errorText = e.message;
  } finally {
    state.streaming = null;
    setSending(false);
  }

  if (!answer && !thinking) {
    // nothing came back, so the server dropped the question: put text and images back to resend
    if (!$("#input").value) setInput(text);
    if (!state.pending.length) {
      state.pending = sentPending;
      renderAttachments();
    }
  }
  // reload from the server, which holds the saved reply with its message indices
  try {
    if (state.conv && state.conv.id === conv.id) {
      // a stopped stream is saved once the server notices; give it a moment
      if (!errorText && controller.signal.aborted) await new Promise((r) => setTimeout(r, 1200));
      state.conv = await api(`/api/conversations/${conv.id}`);
      renderMessages();
      renderTopbar();
    }
    await refreshConvList();
  } catch (e) {
    toast(e.message, true);
  }
  if (errorText) {
    const err = document.createElement("div");
    err.className = "msg-error";
    err.textContent = "出错了：" + errorText;
    $("#messages").appendChild(err);
    $("#messages").scrollTop = $("#messages").scrollHeight;
  } else if (answer && $("#auto-fix").checked && state.conv && state.conv.id === conv.id) {
    await autoCheck();
  }
}

function viewImage(src) {
  const v = document.createElement("div");
  v.className = "viewer";
  v.innerHTML = `<img alt="">`;
  v.querySelector("img").src = src;
  v.addEventListener("click", () => v.remove());
  document.body.appendChild(v);
}

// ---------------------------------------------------------------- settings

function providerCard(p) {
  const card = document.createElement("div");
  card.className = "provider-card";
  // A new provider gets its id now, so the default-provider list can point at it before saving
  card.dataset.id = p.id || "custom-" + Math.random().toString(36).slice(2, 8);
  card.dataset.env = p.env || "";
  card.innerHTML = `
    <div class="card-head">
      <strong></strong>
      <span class="key-state"></span>
      <button type="button" class="btn small danger remove">删除</button>
    </div>
    <label class="field"><span>名称</span><input class="f-name"></label>
    <label class="field"><span>接口类型</span>
      <select class="f-type">
        <option value="openai">OpenAI 兼容（DeepSeek / OpenAI / 其他）</option>
        <option value="anthropic">Anthropic（Claude）</option>
      </select>
    </label>
    <label class="field full"><span>Base URL（Claude 留空即可）</span><input class="f-base" spellcheck="false"></label>
    <label class="field full"><span>API Key（留空 = 不修改）</span><input class="f-key" type="password" autocomplete="off" spellcheck="false"></label>
    <label class="field"><span>默认模型</span><input class="f-model" spellcheck="false"></label>
    <label class="field"><span>备选模型（逗号分隔）</span><input class="f-models" spellcheck="false"></label>
    <label class="field"><span>max_tokens（0 = 不限制，思考内容也计入）</span><input class="f-max" type="number" min="0" step="1024"></label>
    <label class="field f-reasoning-wrap"><span>思考模式参数</span>
      <select class="f-reasoning">
        <option value="none">不发送（普通模型）</option>
        <option value="deepseek">DeepSeek 风格（thinking + reasoning_effort）</option>
      </select>
    </label>
    <label class="field check"><input type="checkbox" class="f-vision"> 支持图片输入</label>`;
  const q = (s) => card.querySelector(s);
  q("strong").textContent = p.name || "新提供商";
  q(".f-name").value = p.name || "";
  q(".f-type").value = p.type || "openai";
  q(".f-base").value = p.base_url || "";
  q(".f-key").placeholder = p.key_hint || "未设置";
  q(".f-model").value = p.model || "";
  q(".f-models").value = (p.models || []).join(", ");
  q(".f-max").value = p.max_tokens || 0;
  q(".f-vision").checked = !!p.vision;
  q(".f-reasoning").value = p.reasoning === "deepseek" ? "deepseek" : "none";
  // Claude always thinks adaptively, so the switch only applies to OpenAI-compatible providers
  const syncReasoning = () => q(".f-reasoning-wrap").classList.toggle("hidden", q(".f-type").value === "anthropic");
  q(".f-type").addEventListener("change", syncReasoning);
  syncReasoning();
  const ks = q(".key-state");
  ks.textContent = p.has_key ? "Key 已设置" : "未设置 Key";
  ks.className = "key-state " + (p.has_key ? "ok" : "missing");
  q(".f-name").addEventListener("input", () => (q("strong").textContent = q(".f-name").value || "新提供商"));
  q(".remove").addEventListener("click", () => {
    card.remove();
    fillDefaultSelect();
  });
  return card;
}

function fillDefaultSelect(selected) {
  const select = $("#default-provider");
  const keep = selected || select.value;
  select.innerHTML = "";
  for (const card of document.querySelectorAll(".provider-card")) {
    const opt = document.createElement("option");
    opt.value = card.dataset.id;
    opt.textContent = card.querySelector(".f-name").value || "新提供商";
    select.appendChild(opt);
  }
  if ([...select.options].some((o) => o.value === keep)) select.value = keep;
}

function openSettings() {
  const cards = $("#provider-cards");
  cards.innerHTML = "";
  for (const p of state.config.providers) cards.appendChild(providerCard(p));
  fillDefaultSelect(state.config.default_provider);
  const s = state.config.skill;
  $("#skill-info").textContent = s.found
    ? `Skill：${s.name} 已找到（${s.path}），开启后会把 SKILL.md 和 ${s.files - 1} 个规则文件（约 ${Math.round(s.approx_tokens / 1000)}k tokens）作为参考资料发给模型。`
    : `没有找到 Skill：${s.path}`;
  $("#settings-error").textContent = "";
  $("#settings").showModal();
}

async function saveSettings(e) {
  e.preventDefault();
  const providers = [...document.querySelectorAll(".provider-card")].map((card) => {
    const q = (s) => card.querySelector(s);
    const name = q(".f-name").value.trim();
    return {
      id: card.dataset.id,
      name,
      type: q(".f-type").value,
      base_url: q(".f-base").value.trim(),
      api_key: q(".f-key").value.trim(),
      model: q(".f-model").value.trim(),
      models: q(".f-models").value.split(",").map((s) => s.trim()).filter(Boolean),
      max_tokens: parseInt(q(".f-max").value, 10) || 0,
      vision: q(".f-vision").checked,
      reasoning: q(".f-reasoning").value,
      env: card.dataset.env,
    };
  });
  if (providers.some((p) => !p.name || !p.model)) {
    $("#settings-error").textContent = "每个提供商都需要填写名称和默认模型";
    return;
  }
  try {
    state.config = await api("/api/config", {
      method: "PUT",
      body: { providers, default_provider: $("#default-provider").value },
    });
  } catch (err) {
    $("#settings-error").textContent = err.message;
    return;
  }
  $("#settings").close();
  const ids = state.config.providers.map((p) => p.id);
  if (!ids.includes(state.draft.provider_id)) state.draft.provider_id = state.config.default_provider;
  if (state.conv && !ids.includes(state.conv.provider_id)) state.conv.provider_id = state.config.default_provider;
  renderTopbar();
  renderConvList();
  toast("设置已保存");
}

// ---------------------------------------------------------------- wiring

function bind() {
  $("#new-chat").addEventListener("click", () => {
    closeConv();
    $("#input").focus();
  });
  $("#close-chat").addEventListener("click", closeConv);
  $("#delete-chat").addEventListener("click", () => state.conv && deleteConv(state.conv.id, state.conv.title));
  $("#conv-title").addEventListener("click", renameConv);
  $("#provider").addEventListener("change", (e) => {
    const provider = providerById(e.target.value);
    warnCacheChange("换模型提供商");
    updateConvSettings({ provider_id: provider.id, model: provider.model });
    renderTopbar();
  });
  $("#model").addEventListener("change", (e) => {
    const model = e.target.value.trim();
    if (!model) return;
    // each model has its own cache
    warnCacheChange("换模型");
    updateConvSettings({ model });
  });
  $("#effort").addEventListener("change", (e) => {
    const v = e.target.value;
    updateConvSettings(v === "off" ? { thinking: false } : { thinking: true, effort: v });
  });
  const autoFix = $("#auto-fix");
  autoFix.checked = storageGet("autoFix", "1") === "1";
  autoFix.addEventListener("change", () => storageSet("autoFix", autoFix.checked ? "1" : "0"));
  $("#use-skill").addEventListener("change", (e) => {
    // the skill sits in the system prompt, at the very start of every request
    warnCacheChange("切换 Skill ");
    updateConvSettings({ use_skill: e.target.checked });
    updateHint();
  });
  $("#send").addEventListener("click", () => send());
  const input = $("#input");
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!state.streaming) send();
    }
  });
  input.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
    if (files.length) {
      e.preventDefault();
      addImages(files);
    }
  });
  $("#attach").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", (e) => {
    addImages([...e.target.files]);
    e.target.value = "";
  });
  const composer = $("#composer");
  composer.addEventListener("dragover", (e) => {
    e.preventDefault();
    composer.classList.add("dragover");
  });
  composer.addEventListener("dragleave", () => composer.classList.remove("dragover"));
  composer.addEventListener("drop", (e) => {
    e.preventDefault();
    composer.classList.remove("dragover");
    addImages([...e.dataTransfer.files]);
  });
  for (const ex of document.querySelectorAll(".example")) {
    ex.addEventListener("click", () => setInput(ex.textContent));
  }
  $("#open-settings").addEventListener("click", openSettings);
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#settings-cancel").addEventListener("click", () => $("#settings").close());
  $("#add-provider").addEventListener("click", () => {
    $("#provider-cards").appendChild(providerCard({
      name: "", type: "openai", base_url: "https://", model: "", models: [], vision: false, max_tokens: 0,
    }));
    fillDefaultSelect();
  });
}

async function init() {
  if (!hasMarked || !hasPurify || !hasCodeMirror) {
    toast("部分前端库（CDN）加载失败：页面仍可用，但代码高亮或 Markdown 排版会降级。", true);
  }
  bind();
  try {
    state.config = await api("/api/config");
  } catch (e) {
    toast("连接本地服务失败：" + e.message, true);
    return;
  }
  state.draft = {
    provider_id: state.config.default_provider,
    model: providerById(state.config.default_provider).model,
    use_skill: true,
    thinking: true,
    effort: "high",
  };
  renderTopbar();
  await refreshConvList();
  renderMessages();
  $("#input").focus();
}

init();
