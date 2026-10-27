/* 视频工作台前端（原生 JS，无构建链） */
"use strict";

const STATUS_TEXT = {
  making: "制作中", ready: "待发布", publishing: "发布中",
  published: "已发布", failed: "失败", interrupted: "已中断",
};
const STEP_NAMES = {
  storyboard: "分镜生成", image: "生图", voice: "配音",
  subtitle: "字幕", video: "视频合成", publish: "发布",
};
const STEP_ORDER = ["storyboard", "image", "voice", "subtitle", "video"];

function $(sel) { return document.querySelector(sel); }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtDur(sec) {
  if (sec == null) return "";
  sec = Math.round(sec);
  return sec >= 60 ? `${Math.floor(sec / 60)}m${sec % 60}s` : `${sec}s`;
}
async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}
function badge(status) {
  return `<span class="badge ${esc(status)}">${STATUS_TEXT[status] || esc(status)}</span>`;
}

/* ---------------- 创作页 ---------------- */
async function initCreate() {
  const topicEl = $("#topic"), noteEl = $("#note"), countEl = $("#topic-count");
  const btn = $("#submit-btn"), errEl = $("#create-error");

  try {
    const h = await api("/api/health");
    const banner = $("#health-banner");
    if (!h.dashscope_key) {
      banner.textContent = "未检测到 DASHSCOPE_API_KEY，生图与分镜将无法运行。请在 imagegen/.env 配置后重启服务。";
      banner.className = "banner err";
    } else if (!h.publish_ready) {
      banner.textContent = "发布脚本缺失（studio/publish/douyin_publish.py），制作可用但无法发布。";
      banner.className = "banner warn";
    } else {
      banner.className = "banner hidden";
    }
    if (h.ref_image) {
      banner.className = "banner warn";
      banner.style.background = "#e7f8ee"; banner.style.color = "#14532d"; banner.style.borderColor = "#86efac";
      banner.textContent = "人设基准图已启用（I2I 锁定画风）：" + decodeURIComponent(h.ref_image.split("/").pop());
    } else if (h.dashscope_key) {
      banner.className = "banner warn";
      banner.textContent = "未启用人设基准图，生图为纯文生图，画风可能与青瓜瓜人设不一致（可配置 studio/.env 的 REF_IMAGE）。";
    }
    if (h.making) {
      btn.disabled = true;
      btn.textContent = "已有制作任务进行中";
    }
  } catch (e) { /* 健康检查失败不阻塞页面 */ }

  topicEl.addEventListener("input", () => {
    countEl.textContent = `${topicEl.value.length}/200`;
  });

  $("#create-form").addEventListener("submit", async ev => {
    ev.preventDefault();
    errEl.textContent = "";
    const topic = topicEl.value.trim();
    if (!topic) { errEl.textContent = "主题不能为空"; return; }
    if (topic.length > 200) { errEl.textContent = "主题超过 200 字"; return; }
    btn.disabled = true;
    try {
      const res = await api("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic, note: noteEl.value.trim() }),
      });
      location.href = `/tasks/${res.id}`;
    } catch (e) {
      errEl.textContent = e.message;
      btn.disabled = false;
    }
  });

  $("#upload-file").addEventListener("change", async ev => {
    const errUp = $("#upload-error");
    const file = ev.target.files[0];
    errUp.textContent = "";
    if (!file) return;
    const ext = file.name.split(".").pop().toLowerCase();
    if (!["mp4", "mov"].includes(ext)) { errUp.textContent = "仅支持 mp4 / mov 格式"; ev.target.value = ""; return; }
    if (file.size > 2 * 1024 * 1024 * 1024) { errUp.textContent = "文件超过 2GB 上限"; ev.target.value = ""; return; }
    const prog = $("#upload-progress"), bar = $("#upload-bar"), text = $("#upload-text");
    prog.classList.remove("hidden");
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.setRequestHeader("x-file-name", encodeURIComponent(file.name));
      xhr.upload.onprogress = e => {
        if (e.lengthComputable) {
          const pct = Math.round(e.loaded / e.total * 100);
          bar.style.width = pct + "%";
          text.textContent = `上传中 ${pct}%`;
        }
      };
      xhr.onload = () => {
        if (xhr.status === 200) {
          text.textContent = "上传完成，跳转中…";
          resolve(JSON.parse(xhr.responseText));
        } else {
          let msg = `上传失败（HTTP ${xhr.status}）`;
          try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (e2) { /* ignore */ }
          errUp.textContent = msg;
          prog.classList.add("hidden");
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => { errUp.textContent = "网络错误"; reject(new Error("network")); };
      xhr.send(file);
    }).then(res => { location.href = `/tasks/${res.id}`; }).catch(() => {});
    ev.target.value = "";
  });
}

/* ---------------- 任务列表页 ---------------- */
async function initTasks() {
  const tbody = $("#task-table tbody");
  try {
    const tasks = await api("/api/tasks");
    $("#empty-hint").classList.toggle("hidden", tasks.length > 0);
    tbody.innerHTML = tasks.map(t => `
      <tr>
        <td>${esc(t.topic || t.title || "(未命名)")}</td>
        <td>${t.type === "upload" ? "上传" : "生成"}</td>
        <td>${badge(t.status)}</td>
        <td class="muted">${fmtTime(t.created_at)}</td>
        <td><a href="/tasks/${esc(t.id)}">查看</a></td>
      </tr>`).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="error">${esc(e.message)}</td></tr>`;
  }
}

/* ---------------- 任务详情页 ---------------- */
let es = null;
let pubTitle = null;      // 发布编辑态（跨重渲染保留）
let pubTags = null;
let smsOpen = false;

async function initTask(tid) {
  await loadAndRender(tid);
}

function subscribe(tid) {
  if (es) es.close();
  es = new EventSource(`/api/tasks/${tid}/events`);
  es.addEventListener("snapshot", ev => {
    const data = JSON.parse(ev.data);
    renderTask(data);
    if (!["making", "publishing"].includes(data.task.status)) es.close();
  });
}

async function loadAndRender(tid) {
  const data = await api(`/api/tasks/${tid}`);
  renderTask(data);
  if (["making", "publishing"].includes(data.task.status)) subscribe(tid);
}

function renderTask(data) {
  window.__last = data;
  const { task, steps, records } = data;
  const root = $("#detail");
  let html = `
    <section class="card">
      <h1>${task.type === "upload" ? "上传视频" : "制作任务"}：${esc(task.topic || task.title)}</h1>
      <p class="hint">创建于 ${fmtTime(task.created_at)} · 状态 ${badge(task.status)}
        ${task.error ? ` · <span class="error">${esc(task.error)}</span>` : ""}</p>`;

  /* 制作进度（gen 任务） */
  if (task.type === "gen") {
    const stepMap = Object.fromEntries(steps.map(s => [s.step, s]));
    html += `<ul class="steps">` + STEP_ORDER.map(name => {
      const s = stepMap[name] || {};
      const cost = s.started_at && s.finished_at ? fmtDur(s.finished_at - s.started_at) : "";
      let right = "";
      if (s.status === "running") right = `<span class="step-detail">运行中…</span>`;
      else if (s.status === "done") right = `<span class="step-detail">${esc(s.detail || "完成")}</span>`;
      else if (s.status === "failed") right = `<span class="step-err">${esc(s.error || "失败")}</span>`;
      else right = `<span class="step-detail muted">等待</span>`;
      return `<li><span class="dot ${s.status || "pending"}"></span>
        <span class="step-name">${STEP_NAMES[name]}</span>${right}
        <span class="step-time">${cost}</span></li>`;
    }).join("") + `</ul>`;

    if (["failed", "interrupted"].includes(task.status)) {
      const failedStep = steps.find(s => s.status === "failed");
      html += `<p class="hint">失败于「${STEP_NAMES[failedStep?.step] || "未知"}」步骤，重试将从该步骤继续（已完成产物不重跑）。</p>
        <button class="ghost" onclick="retryTask('${esc(task.id)}')">从失败步骤重试</button>`;
    }
  }

  /* 成片预览 + 发布编辑 */
  if (task.video_path && ["ready", "publishing", "published", "failed"].includes(task.status)) {
    html += `<h2 style="margin-top:22px">成片预览</h2>
      <video class="preview" controls preload="metadata" src="/api/tasks/${esc(task.id)}/video"></video>`;
    if (records && records.length) {
      html += `<ul class="records">` + records.map(r => `
        <li>${r.result === "success" ? "✅" : "❌"} ${fmtTime(r.published_at)} · 「${esc(r.title)}」
          ${r.result === "failed" ? `（${esc((r.detail || "").slice(0, 120))}）` : ""}</li>`).join("") + `</ul>`;
    }
  }
  if (task.status === "publishing") {
    html += `<p class="hint">正在发布到抖音（上传→发布→读回核验），请勿关闭服务与浏览器登录窗口…</p>`;
  }
  if (task.status === "ready" || (task.status === "failed" && task.video_path)) {
    if (pubTitle === null) pubTitle = task.title || "";
    if (pubTags === null) pubTags = safeTags(task.tags);
    html += `
      <h2 style="margin-top:22px">发布信息</h2>
      <label>标题 <span class="muted"><span id="title-count">${pubTitle.length}</span>/30（抖音上限）</span></label>
      <input type="text" id="pub-title" maxlength="30" value="${esc(pubTitle)}">
      <label>话题（至少 1 个）</label>
      <div class="tags" id="pub-tags">
        ${pubTags.map((t, i) => `<span class="tag">#${esc(t)}<button onclick="delTag(${i})">×</button></span>`).join("")}
        <input type="text" id="tag-input" placeholder="回车添加">
      </div>
      <div class="pub-row">
        <button id="pub-btn" onclick="doPublish('${esc(task.id)}')">确认发布到抖音</button>
        <button class="ghost" onclick="checkAccount()">检查登录状态</button>
        <span id="pub-msg" class="muted"></span>
      </div>
      <div class="login-box" id="login-box">
        <strong style="font-size:14px">抖音登录</strong>
        <div class="state muted">登录态保存在本机浏览器档案（DouyinProfile），登录一次后续免扫码。</div>
        <div class="state" id="login-state">未发起</div>
        <button class="ghost" onclick="startLogin()">扫码登录</button>
        <span id="sms-area" class="${smsOpen ? "" : "hidden"}">
          <input type="text" id="sms-code" placeholder="短信验证码" style="width:130px" maxlength="6">
          <button class="ghost" onclick="submitSms()">提交验证码</button>
        </span>
      </div>`;
  }
  html += `</section>`;

  /* 分镜预览（gen 任务且有 episode） */
  if (task.type === "gen" && task.status !== "making" && task.episode_json) {
    let ep = null;
    try { ep = JSON.parse(task.episode_json); } catch (e) { /* ignore */ }
    if (ep) {
      html += `<section class="card"><h2>分镜（${ep.scenes.length} 镜）</h2>
        <div class="storyboard-grid">` + ep.scenes.map(s => `
          <figure><img loading="lazy" src="/api/tasks/${esc(task.id)}/images/${esc(s.id)}.png" alt="${esc(s.id)}">
          <figcaption>${esc(s.id)}：${esc(s.narration)}</figcaption></figure>`).join("") + `
        </div></section>`;
    }
  }

  root.innerHTML = html;

  /* 绑定发布编辑交互 */
  const titleInput = $("#pub-title");
  if (titleInput) {
    titleInput.addEventListener("input", () => {
      pubTitle = titleInput.value;
      $("#title-count").textContent = pubTitle.length;
    });
    const tagInput = $("#tag-input");
    tagInput.addEventListener("keydown", ev => {
      if (ev.key === "Enter") { ev.preventDefault(); addTag(); }
    });
  }
}

function safeTags(v) {
  try {
    const arr = typeof v === "string" ? JSON.parse(v) : (v || []);
    return arr.map(t => String(t)).filter(Boolean);
  } catch (e) { return []; }
}
function addTag() {
  const input = $("#tag-input");
  const t = (input.value || "").trim().replace(/^#/, "");
  if (t && !pubTags.includes(t)) { pubTags.push(t); pubTitle = $("#pub-title").value; renderTask(window.__last); }
  input.focus();
}
function delTag(i) {
  pubTags.splice(i, 1);
  pubTitle = $("#pub-title").value;
  renderTask(window.__last);
}
async function doPublish(tid) {
  const msg = $("#pub-msg"), btn = $("#pub-btn");
  msg.textContent = "";
  try {
    await api(`/api/tasks/${tid}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: pubTitle.trim(), tags: pubTags }),
    });
    btn.disabled = true;
    msg.textContent = "发布任务已启动";
    subscribe(tid);
  } catch (e) { msg.textContent = e.message; }
}
async function retryTask(tid) {
  try {
    await api(`/api/tasks/${tid}/retry`, { method: "POST" });
    subscribe(tid);
  } catch (e) { alert(e.message); }
}

/* ---------------- 登录 ---------------- */
let loginTimer = null;
async function startLogin() {
  const stateEl = $("#login-state");
  try { await api("/api/login", { method: "POST" }); }
  catch (e) { stateEl.textContent = `发起登录失败：${e.message}`; return; }
  if (loginTimer) clearInterval(loginTimer);
  loginTimer = setInterval(pollLogin, 2000);
  pollLogin();
}
async function pollLogin() {
  const stateEl = $("#login-state"), smsArea = $("#sms-area");
  if (!stateEl) { if (loginTimer) clearInterval(loginTimer); return; }
  try {
    const s = await api("/api/login/status");
    stateEl.textContent = LOGIN_STATE_TEXT[s.state] || s.state + (s.message ? `：${s.message}` : "");
    if (s.state === "sms_required") smsOpen = true; else if (s.state !== "verifying") smsOpen = false;
    smsArea.classList.toggle("hidden", !smsOpen);
    if (s.state === "success") {
      if (loginTimer) clearInterval(loginTimer);
      stateEl.textContent = "已登录，可以发布了";
    }
    if (s.state === "error" || s.state === "expired") {
      if (loginTimer) clearInterval(loginTimer);
    }
  } catch (e) { /* 下次再试 */ }
}
const LOGIN_STATE_TEXT = {
  starting: "启动浏览器…", qr_ready: "二维码已生成，请用抖音 App 扫码（窗口里）",
  window_login: "请在弹出的浏览器窗口里扫码登录，不要关闭窗口",
  scanned: "已扫码，等待确认…", sms_required: "需要短信验证码，请查收后填入",
  verifying: "验证码校验中…", success: "登录成功", expired: "二维码已过期，请重新发起", error: "登录失败",
};
async function submitSms() {
  const code = $("#sms-code").value.trim();
  if (!code) return;
  try {
    await api("/api/login/sms", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    $("#login-state").textContent = "验证码已提交，校验中…";
    $("#sms-code").value = "";
  } catch (e) { $("#login-state").textContent = `提交失败：${e.message}`; }
}
async function checkAccount() {
  const msg = $("#pub-msg");
  if (!msg) return;
  msg.textContent = "正在校验登录态（约 10 秒）…";
  try {
    const r = await api("/api/login/account");
    if (r.loggedIn) msg.textContent = `已登录：${r.name || "(未知昵称)"}`;
    else msg.textContent = `未登录${r.error ? `（${r.error}）` : ""}，请先扫码登录`;
  } catch (e) { msg.textContent = `校验失败：${e.message}`; }
}

/* ---------------- 页面分发 ---------------- */
document.addEventListener("DOMContentLoaded", () => {
  const path = location.pathname;
  if (path === "/") initCreate();
  else if (path === "/tasks") initTasks();
  else {
    const m = path.match(/^\/tasks\/([^/]+)$/);
    if (m) initTask(decodeURIComponent(m[1]));
  }
});
