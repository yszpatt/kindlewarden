"""管理页面的前端（单文件 HTML，零外部依赖）。

**刻意不引任何 CDN。** 这台机器和 Kindle 在同一个局域网里，很可能没有外网；
一个「打开是白屏」的管理页比没有管理页更糟。

页面能力：
- 实时预览副屏画面（跟着 rev 走，画面没变就不重新拉图）
- 简报列表 + **点击打开任务详情**（完整文本、产出物、元数据、操作）
- 待办增删改、告警挂起/清除
- 手动发布简报
- 显示设置（板块开关、保留时长、时钟、轮询间隔）—— 保存即生效，不用重启
"""

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kindlewarden 控制台</title>
<style>
  :root{
    --bg:#14161a; --panel:#1c1f26; --panel2:#22262f; --line:#2a2f3a;
    --fg:#e6e8ec; --muted:#8b93a3; --accent:#4c8dff;
    --ok:#3fb950; --partial:#d29922; --failed:#f85149; --info:#8b93a3;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif}
  a{color:var(--accent)}
  button,input,select,textarea{font:inherit;color:inherit}
  button{background:var(--panel2);border:1px solid var(--line);color:var(--fg);
         padding:7px 13px;border-radius:7px;cursor:pointer}
  button:hover{border-color:#3d4655}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.danger{color:var(--failed);border-color:#4a2a2c}
  button.ghost{background:transparent}
  button:disabled{opacity:.5;cursor:not-allowed}
  input,select,textarea{background:#0f1115;border:1px solid var(--line);border-radius:7px;
                         padding:7px 10px;width:100%}
  textarea{resize:vertical;min-height:60px;font-family:ui-monospace,Consolas,monospace}
  label{display:block;color:var(--muted);font-size:12px;margin:10px 0 4px}

  header{display:flex;align-items:center;gap:14px;padding:12px 18px;
         border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
  header h1{font-size:15px;margin:0;font-weight:700;letter-spacing:.02em}
  .spacer{flex:1}
  .pill{font-size:12px;color:var(--muted);border:1px solid var(--line);
        border-radius:999px;padding:3px 10px;white-space:nowrap}
  .pill b{color:var(--fg);font-weight:600}
  .pill.warn{color:var(--partial);border-color:#4a3f1e}

  main{display:grid;grid-template-columns:352px minmax(0,1fr);gap:16px;padding:16px;
       align-items:start;max-width:1500px;margin:0 auto}
  @media(max-width:900px){main{grid-template-columns:1fr}}

  .card{background:var(--panel);border:1px solid var(--line);border-radius:11px;overflow:hidden}
  .card .hd{padding:10px 14px;border-bottom:1px solid var(--line);
            font-size:12px;color:var(--muted);display:flex;align-items:center;gap:8px}
  .card .bd{padding:14px}

  #shot{display:block;width:100%;background:#fff;image-rendering:auto}
  .preview-wrap{position:relative;background:#fff}
  .preview-wrap.busy::after{content:"";position:absolute;inset:0;background:rgba(20,22,26,.25)}

  nav{display:flex;gap:4px;padding:8px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  nav button{border-color:transparent;background:transparent;color:var(--muted);padding:6px 12px}
  nav button.on{background:var(--panel2);color:var(--fg);border-color:var(--line)}
  nav .count{font-size:11px;color:var(--muted);margin-left:5px}

  .list{max-height:60vh;overflow:auto}
  .row{display:flex;gap:11px;padding:12px 14px;border-bottom:1px solid var(--line);
       cursor:pointer;align-items:flex-start}
  .row:hover{background:var(--panel2)}
  .row.sel{background:#243043}
  .row.dead{opacity:.45}
  .glyph{width:20px;flex:0 0 20px;text-align:center;font-size:16px;line-height:1.35}
  .glyph.ok{color:var(--ok)} .glyph.partial{color:var(--partial)}
  .glyph.failed{color:var(--failed)} .glyph.info{color:var(--info)}
  .row .t{font-weight:600;word-break:break-word}
  .row .m{color:var(--muted);font-size:12px;margin-top:2px;word-break:break-word}
  .row .rt{margin-left:auto;color:var(--muted);font-size:12px;white-space:nowrap;padding-left:8px}
  .star{color:var(--partial)}
  .empty{padding:34px 16px;text-align:center;color:var(--muted)}

  .badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;
         border:1px solid var(--line);color:var(--muted)}
  .badge.ok{color:var(--ok);border-color:#204227}
  .badge.partial{color:var(--partial);border-color:#4a3f1e}
  .badge.failed{color:var(--failed);border-color:#4a2a2c}

  .warn{background:#3a2f12;border:1px solid #5c4a1c;color:#f0d68a;
        padding:8px 11px;border-radius:7px;font-size:12.5px;margin:10px 0}

  /* 详情抽屉 */
  #scrim{position:fixed;inset:0;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;
         transition:opacity .16s;z-index:40}
  #scrim.on{opacity:1;pointer-events:auto}
  #drawer{position:fixed;top:0;right:0;height:100%;width:min(560px,94vw);
          background:var(--panel);border-left:1px solid var(--line);z-index:41;
          transform:translateX(102%);transition:transform .2s ease;display:flex;
          flex-direction:column}
  #drawer.on{transform:none}
  #drawer .dh{display:flex;align-items:center;gap:10px;padding:13px 16px;
              border-bottom:1px solid var(--line)}
  #drawer .dh .spacer{flex:1}
  #drawer .db{padding:16px;overflow:auto;flex:1}
  #drawer .df{padding:12px 16px;border-top:1px solid var(--line);display:flex;gap:8px;
              flex-wrap:wrap;background:var(--panel2)}
  #drawer h2{margin:.1em 0 .5em;font-size:20px;line-height:1.4;word-break:break-word}
  .kv{display:grid;grid-template-columns:86px 1fr;gap:5px 12px;font-size:12.5px;margin-top:14px}
  .kv dt{color:var(--muted)} .kv dd{margin:0;word-break:break-all;font-family:ui-monospace,Consolas,monospace}
  .summary{white-space:pre-wrap;background:var(--panel2);border:1px solid var(--line);
           border-radius:8px;padding:11px;margin-top:6px;line-height:1.65}
  .arts{margin-top:14px}
  .arts a,.arts span{display:block;padding:7px 10px;border:1px solid var(--line);
                     border-radius:7px;margin-bottom:6px;text-decoration:none;
                     word-break:break-all;font-size:12.5px}

  .form{max-width:640px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
  .ops{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap;align-items:center}
  .counter{font-size:11.5px;color:var(--muted);margin-top:3px}
  .counter.over{color:var(--partial)}
  .hint{color:var(--muted);font-size:12px;margin-top:12px;line-height:1.7}
  .chk{display:flex;align-items:center;gap:8px;margin:8px 0}
  .chk input{width:auto}
  .chk label{margin:0;color:var(--fg);font-size:13px}
  .toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(80px);
         background:var(--panel2);border:1px solid var(--line);border-radius:9px;
         padding:10px 16px;z-index:60;transition:transform .2s;max-width:80vw}
  .toast.on{transform:translateX(-50%)}
  .toast.bad{border-color:#5c2a2c;color:#ffb4b0}
</style>
</head>
<body>

<header>
  <h1>kindlewarden</h1>
  <span class="pill">画面 <b id="h-rev">—</b></span>
  <span class="pill" id="h-dev">设备 —</span>
  <span class="pill" id="h-health">源 —</span>
  <span class="spacer"></span>
  <button class="ghost" onclick="forceRefresh()">强制重渲染</button>
  <button class="danger ghost" onclick="clearBoard()">清空看板</button>
</header>

<main>
  <section class="card">
    <div class="hd">副屏画面 <span class="spacer"></span><span id="shot-age"></span></div>
    <div class="preview-wrap"><img id="shot" alt="副屏画面"></div>
    <div class="bd" style="padding:11px 14px;color:var(--muted);font-size:12px">
      1072 × 1448 · 16 灰阶 · Kindle PW3
    </div>
    <div class="hd" style="border-top:1px solid var(--line)">Kindle 上打开</div>
    <div class="bd" id="kindle-url" style="font-size:12.5px"></div>
  </section>

  <section class="card">
    <nav>
      <button data-tab="briefs" class="on">简报<span class="count" id="c-briefs"></span></button>
      <button data-tab="todos">待办<span class="count" id="c-todos"></span></button>
      <button data-tab="alert">告警</button>
      <button data-tab="publish">发布</button>
      <button data-tab="settings">设置</button>
    </nav>

    <div id="tab-briefs" class="pane">
      <div class="hd" style="border-bottom:1px solid var(--line)">
        <label class="chk" style="margin:0">
          <input type="checkbox" id="show-archived" onchange="loadBoard()">
          <span>显示已撤回 / 已被取代</span>
        </label>
      </div>
      <div class="list" id="briefs"></div>
    </div>

    <div id="tab-todos" class="pane" hidden>
      <div class="bd" style="border-bottom:1px solid var(--line);display:flex;gap:8px">
        <input id="todo-new" placeholder="添加一条待办（给人看的，不是 agent 的执行计划）"
               onkeydown="if(event.key==='Enter')addTodo()">
        <button class="primary" onclick="addTodo()">添加</button>
      </div>
      <div class="list" id="todos"></div>
    </div>

    <div id="tab-alert" class="pane" hidden>
      <div class="bd">
        <div id="alert-now"></div>
        <div class="form" style="margin-top:18px">
          <div style="color:var(--muted);font-size:12px">挂起一条告警</div>
          <label>标题（≤15 字，板上会被截断）</label>
          <input id="alert-title" placeholder="允许执行 rm -rf build">
          <label>详情（≤30 字）</label>
          <input id="alert-detail" placeholder="session: refactor-auth · 已等待 3 分钟">
          <div class="ops">
            <button class="primary" onclick="raiseAlert()">挂起告警</button>
            <button onclick="clearAlert()">清除告警</button>
          </div>
          <div class="hint">
            告警是副屏上<b>最强的注意力信号</b>，只在真的需要人做决定时使用。<br>
            不要用它播报进度或成功消息 —— 那会让真正重要的告警被忽视。<br>
            挂起后会一直留在屏幕顶端，问题解决后记得清除。
          </div>
        </div>
      </div>
    </div>

    <div id="tab-publish" class="pane" hidden>
      <div class="bd">
        <div class="form" id="pub-form">
          <div id="pub-supersede"></div>
          <label>标题 —— 一句话<b>结论</b>，不是过程</label>
          <input id="pub-title" oninput="counts()" placeholder="修复了 auth 模块 token 刷新的竞态条件">
          <div class="counter" id="c-title"></div>

          <div class="grid2">
            <div>
              <label>状态</label>
              <select id="pub-status">
                <option value="ok">ok —— 完全达成</option>
                <option value="partial">partial —— 部分达成或有遗留</option>
                <option value="failed">failed —— 未达成，需介入</option>
                <option value="info">info —— 无需行动的结论</option>
              </select>
            </div>
            <div>
              <label>项目</label>
              <input id="pub-project" placeholder="backend">
            </div>
          </div>

          <label>摘要 —— 做了什么 + 结果如何 + 需要人注意什么</label>
          <input id="pub-summary" oninput="counts()" placeholder="定时器刷新改为惰性刷新，补 4 个并发测试">
          <div class="counter" id="c-summary"></div>

          <label>产出物（每行一条，最多 5 条）</label>
          <textarea id="pub-artifacts" placeholder="https://github.com/org/repo/pull/482"></textarea>

          <label>耗时（秒，可空）</label>
          <input id="pub-duration" type="number" min="0" placeholder="1860">

          <div class="ops">
            <button class="primary" onclick="publish()">发布到副屏</button>
            <button class="ghost" onclick="resetPublish()">清空表单</button>
          </div>
          <div class="hint">
            发布是<b>异步</b>的：内容先落到本地发件箱，后台自动投递，副屏可能几十秒后才刷新。<br>
            同一任务只发一次；要更新就点开那条简报用「以此为准发布更新」，而不是再发一条。
          </div>
        </div>
      </div>
    </div>

    <div id="tab-settings" class="pane" hidden>
      <div class="bd"><div class="form" id="settings"></div></div>
    </div>
  </section>
</main>

<div id="scrim" onclick="closeDetail()"></div>
<aside id="drawer">
  <div class="dh">
    <span id="d-glyph" class="glyph"></span>
    <b>任务详情</b>
    <span class="spacer"></span>
    <button class="ghost" onclick="closeDetail()">关闭</button>
  </div>
  <div class="db" id="d-body"></div>
  <div class="df" id="d-actions"></div>
</aside>

<div class="toast" id="toast"></div>

<script>
"use strict";
const $ = id => document.getElementById(id);
let BOARD = null, CONFIG = null, DEVICE = null;
let LAST_REV = null, SELECTED = null, TAB = "briefs";

/* ---------------------------------------------------------------- 基础 */

function toast(msg, bad){
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast on" + (bad ? " bad" : "");
  clearTimeout(t._t);
  t._t = setTimeout(() => t.className = "toast", 2600);
}

async function api(path, body){
  const opts = {cache:"no-store"};
  if (body !== undefined){
    opts.method = "POST";
    opts.headers = {"Content-Type":"application/json"};
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch(e){ data = {raw:text}; }
  if (!r.ok) throw new Error((data && data.error) || (r.status + " " + r.statusText));
  return data;
}

/* 与后端同一套「显示宽度」规则：东亚全角算 1，其余算 0.5。
   这样管理页上的字数提示和板上实际的截断是一致的。 */
function dispWidth(s){
  let w = 0;
  for (const ch of (s || "")){
    const c = ch.codePointAt(0);
    const wide = (c >= 0x1100 && c <= 0x115F) || (c >= 0x2E80 && c <= 0xA4CF) ||
                 (c >= 0xAC00 && c <= 0xD7A3) || (c >= 0xF900 && c <= 0xFAFF) ||
                 (c >= 0xFE30 && c <= 0xFE6F) || (c >= 0xFF00 && c <= 0xFF60) ||
                 (c >= 0xFFE0 && c <= 0xFFE6) || (c >= 0x20000 && c <= 0x3FFFD);
    w += wide ? 1 : 0.5;
  }
  return w;
}

function fmtTime(ts){
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return p(d.getMonth()+1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}
function fmtAge(s){
  if (s === null || s === undefined) return "—";
  if (s < 60) return s + " 秒前";
  if (s < 3600) return Math.floor(s/60) + " 分钟前";
  if (s < 86400) return Math.floor(s/3600) + " 小时前";
  return Math.floor(s/86400) + " 天前";
}
const GLYPH = {ok:"✓", partial:"◐", failed:"✗", info:"·"};
const esc = s => String(s == null ? "" : s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;").replace(/'/g,"&#39;");

/* ---------------------------------------------------------------- 内联处理器
 *
 * ⚠️ 这里踩过一个**全线静默失效**的坑，务必读完再改。
 *
 * 反面教材（原来的写法，5 处全是这样）：
 *
 *     '<button onclick="removeTodo(' + JSON.stringify(t.id) + ')">删除</button>'
 *
 * 拼出来是：
 *
 *     onclick="removeTodo("todo_abc")"
 *
 * HTML 解析器读到第二个 `"` 就认为属性结束了，于是 onclick 的值只剩
 * `removeTodo(` —— 一个语法错误。**点击毫无反应**，而且在事件真正触发前
 * 不会有任何报错。所以「函数定义了吗」这类静态检查完全抓不到它。
 *
 * 修法：参数一律 JSON 化后再做 HTML 实体转义（`"` → `&quot;`）。
 * 实体解码发生在属性边界确定**之后**，所以属性不再被撑破，
 * 浏览器交到 JS 手上的仍是 `removeTodo("todo_abc")`。
 *
 * 用 `q()` 包字符串字面量，其余参数直接给 JS 表达式。
 */
const q = s => JSON.stringify(s == null ? "" : s);
function act(fn, ...exprs){
  return esc(fn + "(" + exprs.join(",") + ")");
}

/* ---------------------------------------------------------------- 加载 */

async function loadBoard(){
  const arch = $("show-archived").checked ? "?include_hidden=1" : "";
  const [board, device] = await Promise.all([api("/board" + arch), api("/device")]);
  BOARD = board; DEVICE = device;
  if (board.rev !== LAST_REV){
    LAST_REV = board.rev;
    const img = new Image();
    img.onload = () => { $("shot").src = img.src; };
    img.src = "/screen.png?rev=" + board.rev;
  }
  renderHeader();
  if (TAB === "briefs") renderBriefs();
  if (TAB === "todos") renderTodos();
  if (TAB === "alert") renderAlert();
}

async function loadConfig(){
  CONFIG = await api("/config");
  renderSettings();
}

function renderHeader(){
  $("h-rev").textContent = BOARD ? BOARD.rev : "—";
  $("shot-age").textContent = BOARD ? ("更新于 " + fmtTime(BOARD.generated_at)) : "";
  const h = BOARD ? BOARD.source_health : "—";
  const hEl = $("h-health");
  hEl.innerHTML = "源 <b>" + esc(h) + "</b>";
  hEl.className = "pill" + (h === "ok" ? "" : " warn");
  $("c-briefs").textContent = BOARD ? " " + BOARD.total_briefs : "";
  $("c-todos").textContent = BOARD ? " " + BOARD.total_todos : "";

  const d = DEVICE && DEVICE.device;
  const el = $("h-dev");
  if (d && d.last_seen){
    const stale = d.age_s > (DEVICE.poll_idle_s * 3);
    el.innerHTML = "Kindle <b>" + fmtAge(d.age_s) + "</b>";
    el.className = "pill" + (stale ? " warn" : "");
  } else {
    el.innerHTML = "Kindle <b>尚未连接</b>";
    el.className = "pill warn";
  }
  renderKindleUrl();
}

/* 最常被问的问题：Kindle 上到底打开哪个地址。
   直接算好摆在预览图下面，不让人去命令行里查。 */
function renderKindleUrl(){
  const box = $("kindle-url");
  if (!box || !DEVICE) return;
  const addrs = DEVICE.lan || [];
  const port = DEVICE.port || location.port || 80;
  if (!addrs.length){
    box.innerHTML = '<span style="color:var(--muted)">没找到局域网地址 —— ' +
      '这台机器可能没连上网络</span>';
    return;
  }
  const lines = addrs.map((a, i) => {
    const url = "http://" + a + ":" + port + "/kindle";
    const tag = i === 0 ? "" : ' <span style="color:var(--muted)">（其它网卡）</span>';
    return '<div style="margin-bottom:6px"><code style="user-select:all">' + esc(url) +
           '</code>' + tag + '</div>';
  }).join("");
  box.innerHTML = lines +
    '<div style="color:var(--muted);margin-top:8px;line-height:1.7">' +
    '在 Kindle 自带浏览器（体验版网页浏览器）里输入上面的地址。<br>' +
    '那个页面是<b>零 JavaScript</b> 的，且会自动刷新 —— 老 WebKit 也跑得动。<br>' +
    '本页（控制台）用了现代 CSS 和 JS，<b>Kindle 上打不开</b>。</div>';
}

/* ---------------------------------------------------------------- 简报 */

function renderBriefs(){
  const box = $("briefs");
  const list = (BOARD && BOARD.briefs) || [];
  if (!list.length){
    box.innerHTML = '<div class="empty">板上还没有简报<br><br>' +
      '让 agent 通过 MCP 发布，或者在「发布」标签页手动发一条</div>';
    return;
  }
  box.innerHTML = list.map(b => {
    const dead = b.retracted || b.superseded_by;
    const why = b.retracted ? "已撤回" : (b.superseded_by ? "已被取代" : "");
    return '<div class="row' + (dead ? " dead" : "") + '" data-id="' + esc(b.id) + '">' +
      '<span class="glyph ' + esc(b.status) + '">' + (dead ? "○" : GLYPH[b.status] || "·") + '</span>' +
      '<div style="min-width:0">' +
        '<div class="t">' + (b.pinned ? '<span class="star">★ </span>' : "") + esc(b.title) + '</div>' +
        '<div class="m">' + esc([b.project, b.summary].filter(Boolean).join(" · ") || "（无摘要）") +
          (why ? ' <span class="badge">' + why + '</span>' : '') + '</div>' +
      '</div>' +
      '<div class="rt">' + fmtTime(b.ts) + '</div>' +
    '</div>';
  }).join("");
  box.querySelectorAll(".row").forEach(el =>
    el.onclick = () => openDetail(el.dataset.id));
}

function findBrief(id){
  return ((BOARD && BOARD.briefs) || []).find(b => b.id === id) || null;
}

/* 任务详情：点击列表项打开 */
function openDetail(id){
  const b = findBrief(id);
  if (!b) return;
  SELECTED = b;
  $("d-glyph").textContent = GLYPH[b.status] || "·";
  $("d-glyph").className = "glyph " + b.status;

  const art = (b.artifacts || []).map(a => {
    const isUrl = /^https?:\/\//i.test(a.ref || "");
    const label = esc(a.label || a.ref || "");
    return isUrl
      ? '<a href="' + esc(a.ref) + '" target="_blank" rel="noopener">' + label + '</a>'
      : '<span>' + label + '</span>';
  }).join("") || '<div style="color:var(--muted);font-size:12.5px">（无）</div>';

  const warns = (b.warnings || []).map(w => '<div class="warn">' + esc(w) + '</div>').join("");

  $("d-body").innerHTML =
    '<span class="badge ' + esc(b.status) + '">' + esc(b.status) + '</span>' +
    (b.pinned ? ' <span class="badge">已钉住</span>' : '') +
    (b.retracted ? ' <span class="badge">已撤回</span>' : '') +
    (b.superseded_by ? ' <span class="badge">已被取代</span>' : '') +
    '<h2>' + esc(b.title) + '</h2>' +
    warns +
    '<div style="color:var(--muted);font-size:12px;margin-top:10px">摘要</div>' +
    '<div class="summary">' + (esc(b.summary) || '<span style="color:var(--muted)">（无）</span>') + '</div>' +
    '<div class="arts"><div style="color:var(--muted);font-size:12px">产出物</div>' + art + '</div>' +
    '<dl class="kv">' +
      '<dt>项目</dt><dd>' + (esc(b.project) || "—") + '</dd>' +
      '<dt>耗时</dt><dd>' + (b.duration_s ? Math.round(b.duration_s/60) + " 分钟" : "—") + '</dd>' +
      '<dt>创建</dt><dd>' + fmtTime(b.ts) + '</dd>' +
      '<dt>简报 ID</dt><dd>' + esc(b.id) + '</dd>' +
      '<dt>来源</dt><dd>' + (esc(JSON.stringify(b.source)) || "—") + '</dd>' +
      (b.superseded_by ? '<dt>被谁取代</dt><dd>' + esc(b.superseded_by) + '</dd>' : '') +
    '</dl>';

  const acts = [];
  if (!b.retracted && !b.superseded_by){
    acts.push('<button onclick="' + act("togglePin", q(b.id),
              b.pinned ? "false" : "true") + '">' +
              (b.pinned ? "取消钉住" : "钉住") + '</button>');
    acts.push('<button class="primary" onclick="fromBrief()">以此为准发布更新</button>');
    acts.push('<button class="danger" onclick="' + act("retract", q(b.id)) +
              '">撤回</button>');
    acts.push('<button class="danger" onclick="' + act("deleteBrief", q(b.id)) +
              '">彻底删除</button>');
  } else {
    /* 已撤回/已被取代的简报不在板上，撤回没有意义，但仍可彻底删除 */
    acts.push('<span style="color:var(--muted);font-size:12.5px">这条已不在板上</span>');
    acts.push('<button class="danger" onclick="' + act("deleteBrief", q(b.id)) +
              '">彻底删除</button>');
  }
  $("d-actions").innerHTML = acts.join("");
  $("scrim").className = "on";
  $("drawer").className = "on";
  /* 深链接：详情页可以直接分享 / 收藏。用 replaceState 免得刷爆历史记录。 */
  history.replaceState(null, "", "#b=" + encodeURIComponent(id));
}

function closeDetail(){
  $("scrim").className = "";
  $("drawer").className = "";
  SELECTED = null;
  history.replaceState(null, "", location.pathname);
}

async function togglePin(id, pinned){
  try { await api("/pin", {id, pinned}); toast(pinned ? "已钉住" : "已取消钉住"); await loadBoard(); openDetail(id); }
  catch(e){ toast("失败：" + e.message, true); }
}

async function retract(id){
  if (!confirm("从副屏撤回这条简报？")) return;
  try { await api("/retract", {id}); toast("已撤回"); closeDetail(); await loadBoard(); }
  catch(e){ toast("失败：" + e.message, true); }
}

/* 彻底删除：与「撤回」不同 —— 归档里也不再保留。
   所以要二次确认，并且说清差别，否则用户会以为两者是一回事。 */
async function deleteBrief(id){
  if (!confirm("彻底删除这条简报？\n\n删掉后连「显示已撤回」的归档里也不会再有，" +
               "无法恢复。\n只想让它从副屏消失、归档保留，请用「撤回」。")) return;
  try { await api("/brief/delete", {id}); toast("已彻底删除"); closeDetail(); await loadBoard(); }
  catch(e){ toast("失败：" + e.message, true); }
}

/* 用某条简报的内容预填发布表单，并带上 supersedes */
function fromBrief(){
  if (!SELECTED) return;
  const b = SELECTED;
  goto_("publish");
  $("pub-title").value = b.title;
  $("pub-status").value = b.status;
  $("pub-summary").value = b.summary || "";
  $("pub-project").value = b.project || "";
  $("pub-artifacts").value = (b.artifacts || []).map(a => a.ref || a.label).join("\n");
  $("pub-duration").value = b.duration_s || "";
  $("pub-supersede").innerHTML =
    '<div class="warn">这次发布会<b>取代</b>：' + esc(b.title) +
    '<br><span style="font-family:ui-monospace">' + esc(b.id) + '</span>' +
    ' <a href="#" onclick="resetPublish();return false;">取消取代</a></div>';
  $("pub-form").dataset.supersedes = b.id;
  counts();
  toast("已载入该简报内容");
}

function resetPublish(){
  ["pub-title","pub-summary","pub-project","pub-artifacts","pub-duration"].forEach(i => $(i).value = "");
  $("pub-supersede").innerHTML = "";
  delete $("pub-form").dataset.supersedes;
  $("pub-status").value = "ok";
  counts();
}

function counts(){
  const tw = dispWidth($("pub-title").value);
  const sw = dispWidth($("pub-summary").value);
  const t = $("c-title"), s = $("c-summary");
  t.textContent = "显示宽度 " + tw.toFixed(1) + " / 20 字" +
    (tw > 20 ? " —— 板上会被截断，建议精简" : "");
  t.className = "counter" + (tw > 20 ? " over" : "");
  s.textContent = "显示宽度 " + sw.toFixed(1) + " / 28 字" +
    (sw > 28 ? " —— 板上单行放不下，会被截断" : "");
  s.className = "counter" + (sw > 28 ? " over" : "");
}

async function publish(){
  const title = $("pub-title").value.trim();
  if (!title){ toast("标题不能为空", true); return; }
  const payload = {
    title,
    status: $("pub-status").value,
    summary: $("pub-summary").value.trim(),
    project: $("pub-project").value.trim(),
    duration_s: parseInt($("pub-duration").value || "0", 10) || 0,
  };
  const arts = $("pub-artifacts").value.split("\n").map(s => s.trim()).filter(Boolean).slice(0,5);
  if (arts.length) payload.artifacts = arts.map(a => ({label: a, ref: a}));
  if ($("pub-form").dataset.supersedes) payload.supersedes = $("pub-form").dataset.supersedes;

  try {
    const r = await api("/brief", payload);
    let msg = "已发布 " + r.brief_id;
    if (r.redactions && r.redactions.length) msg += "（命中敏感模式并已打码：" + r.redactions.join(", ") + "）";
    toast(msg, !!(r.redactions && r.redactions.length));
    resetPublish();
    await loadBoard();
    goto_("briefs");
  } catch(e){ toast("发布失败：" + e.message, true); }
}

/* ---------------------------------------------------------------- 待办 */

function renderTodos(){
  const box = $("todos");
  const list = (BOARD && BOARD.todos) || [];
  if (!list.length){
    box.innerHTML = '<div class="empty">没有待办<br><br>这是给<b>人</b>看的待办板，' +
      '不是 agent 的内部执行计划</div>';
    return;
  }
  box.innerHTML = list.map(t =>
    '<div class="row" style="cursor:default" data-id="' + esc(t.id) + '">' +
      '<input type="checkbox" style="width:auto;margin-top:5px" ' + (t.done ? "checked" : "") +
        ' onchange="' + act("toggleTodo", q(t.id), "this.checked") + '">' +
      '<div style="min-width:0;flex:1">' +
        '<div class="t" style="' + (t.done ? "text-decoration:line-through;color:var(--muted)" : "") +
          '" contenteditable="true" onblur="' + act("editTodo", q(t.id), "this") + '">' +
          esc(t.text) + '</div>' +
        '<div class="m">' + esc(t.id) + (t.done_ts ? " · 完成于 " + fmtTime(t.done_ts) : "") + '</div>' +
      '</div>' +
      '<button class="ghost danger" onclick="' + act("removeTodo", q(t.id)) +
        '">删除</button>' +
    '</div>').join("");
}

async function addTodo(){
  const inp = $("todo-new");
  const text = inp.value.trim();
  if (!text) return;
  try { await api("/todo", {text}); inp.value = ""; toast("已添加"); await loadBoard(); }
  catch(e){ toast("失败：" + e.message, true); }
}
async function toggleTodo(id, done){
  try { await api("/todo/toggle", {id, done}); await loadBoard(); }
  catch(e){ toast("失败：" + e.message, true); }
}
async function editTodo(id, el){
  const text = el.textContent.trim();
  const cur = ((BOARD && BOARD.todos) || []).find(t => t.id === id);
  if (!text || (cur && cur.text === text)) return;
  try { await api("/todo/update", {id, text}); toast("已保存"); await loadBoard(); }
  catch(e){ toast("失败：" + e.message, true); }
}
async function removeTodo(id){
  try { await api("/todo/remove", {id}); toast("已删除"); await loadBoard(); }
  catch(e){ toast("失败：" + e.message, true); }
}

/* ---------------------------------------------------------------- 告警 */

function renderAlert(){
  const a = BOARD && BOARD.alert;
  $("alert-now").innerHTML = a
    ? '<div class="warn" style="font-size:13.5px">⚠ <b>' + esc(a.title) + '</b><br>' +
      esc(a.detail || "") + '<br><span style="opacity:.7;font-size:12px">' + esc(a.id) + '</span></div>'
    : '<div class="empty" style="padding:18px">当前没有告警</div>';
}
async function raiseAlert(){
  const title = $("alert-title").value.trim();
  if (!title){ toast("标题不能为空", true); return; }
  try {
    await api("/alert", {title, detail: $("alert-detail").value.trim()});
    $("alert-title").value = ""; $("alert-detail").value = "";
    toast("告警已挂起"); await loadBoard();
  } catch(e){ toast("失败：" + e.message, true); }
}
async function clearAlert(){
  try { await api("/alert/clear", {}); toast("告警已清除"); await loadBoard(); }
  catch(e){ toast("失败：" + e.message, true); }
}

/* ---------------------------------------------------------------- 设置 */

const LABELS = {
  "panels.alerts":"告警板块", "panels.briefs":"简报板块", "panels.todos":"待办板块",
  "panels.ambient":"环境板块（日期/天气）", "panels.statusbar":"底部状态栏",
  "board.max_briefs":"简报条数上限", "board.max_todos":"待办条数上限",
  "board.brief_retention_h":"普通简报保留（小时）",
  "board.failed_retention_h":"failed 简报保留（小时）",
  "board.show_info":"显示 info 状态的简报",
  "ambient.weather":"天气文本（留空则不显示）",
  "ambient.show_clock":"在环境区显示时间",
  "browser.viewport_w":"Kindle 浏览器视口宽（CSS 像素）",
  "browser.viewport_h":"Kindle 浏览器视口高（已扣掉工具栏）",
  "display.night":"夜间模式（反色，暗环境不刺眼）",
  "display.landscape":"横屏模式（要物理转动设备看）",
  "display.rotate":'旋转方向：cw=往右转设备 / ccw=往左转',
  "server.heartbeat_min":"心跳重渲染间隔（分钟）",
  "server.poll_idle_s":"空闲时 Kindle 轮询间隔（秒）",
  "server.poll_active_s":"有新内容时轮询间隔（秒）",
};
// 这些字段渲染成开关。**新增布尔配置必须加进来** ——
// 不加的话它会被当成文本框，输入 true/false 才能改，很难用。
const BOOLS = new Set(["panels.alerts","panels.briefs","panels.todos","panels.ambient",
  "panels.statusbar","board.show_info","ambient.show_clock",
  "display.night","display.landscape"]);

function renderSettings(){
  if (!CONFIG){ $("settings").innerHTML = '<div class="empty">正在加载…</div>'; return; }
  const ed = CONFIG.editable;
  let html = "";
  for (const section of Object.keys(ed)){
    html += '<div style="color:var(--accent);font-size:12px;font-weight:600;' +
            'margin:18px 0 2px;text-transform:uppercase;letter-spacing:.06em">' + esc(section) + '</div>';
    for (const key of ed[section]){
      const path = section + "." + key;
      const val = CONFIG[section][key];
      const label = LABELS[path] || key;
      if (BOOLS.has(path)){
        html += '<div class="chk"><input type="checkbox" id="cfg-' + path + '" ' +
                (val ? "checked" : "") + '><label for="cfg-' + path + '">' + esc(label) + '</label></div>';
      } else {
        const type = (typeof val === "number") ? "number" : "text";
        const step = (typeof val === "number" && !Number.isInteger(val)) ? ' step="0.5"' : '';
        html += '<label>' + esc(label) + '</label><input type="' + type + '"' + step +
                ' id="cfg-' + path + '" value="' + esc(val) + '">';
      }
    }
  }
  const ro = CONFIG.restart_required || [];
  html += '<div style="color:var(--muted);font-size:12px;margin:18px 0 2px">只读（改动需要重启服务）</div>' +
          '<dl class="kv"><dt>host</dt><dd>' + esc(CONFIG.server.host) + '</dd>' +
          '<dt>port</dt><dd>' + esc(CONFIG.server.port) + '</dd>' +
          '<dt>配置文件</dt><dd>' + esc(CONFIG.path) + '</dd></dl>' +
          '<div class="ops"><button class="primary" onclick="saveSettings()">保存并立即生效</button></div>' +
          '<div class="hint">保存会写回 panels.toml，并清掉渲染缓存 —— <b>不需要重启服务</b>。<br>' +
          '这些设置同时影响副屏版面和管理页的提示。</div>' +
          '<div class="hint" style="border-top:1px solid var(--line);padding-top:12px">' +
          '<b>调 Kindle 浏览器视口：</b><br>' +
          '图片尺寸是按 viewport_w × viewport_h 算好后写死在 &lt;img width height&gt; 上的 ' +
          '（老 WebKit 对 CSS 的 max-height:100% 解析不可靠，会切掉底部的时间戳）。<br>' +
          '在设备上不方便改配置，可以直接改地址栏试：' +
          '<code>…/kindle?w=1000&amp;h=1300</code>。<br>' +
          '四周留白太多就把数调大，出现滚动条或切边就调小；定下来后填进上面的两个框。</div>';
  $("settings").innerHTML = html;
}

async function saveSettings(){
  const patch = {};
  for (const section of Object.keys(CONFIG.editable)){
    patch[section] = {};
    for (const key of CONFIG.editable[section]){
      const el = $("cfg-" + section + "." + key);
      if (!el) continue;
      patch[section][key] = (el.type === "checkbox") ? el.checked : el.value;
    }
  }
  try {
    const r = await api("/config", patch);
    toast(r.changed.length ? ("已更新 " + r.changed.length + " 项，立即生效")
                           : "没有改动");
    await loadConfig(); await loadBoard();
  } catch(e){ toast("保存失败：" + e.message, true); }
}

/* ---------------------------------------------------------------- 顶部操作 */

async function forceRefresh(){
  try { await api("/refresh", {}); LAST_REV = null; await loadBoard(); toast("已强制重渲染"); }
  catch(e){ toast("失败：" + e.message, true); }
}
async function clearBoard(){
  if (!confirm("清空看板上的全部简报、待办和告警？\n\n（会以事件的形式记录，日志仍可回放）")) return;
  try {
    const r = await api("/clear", {});
    toast("已清空：撤回 " + r.retracted + " 条简报、" + r.todos_removed + " 条待办");
    closeDetail(); LAST_REV = null; await loadBoard();
  } catch(e){ toast("失败：" + e.message, true); }
}

/* ---------------------------------------------------------------- 标签页 */

function goto_(tab){
  TAB = tab;
  document.querySelectorAll("nav button").forEach(b =>
    b.classList.toggle("on", b.dataset.tab === tab));
  ["briefs","todos","alert","publish","settings"].forEach(t =>
    $("tab-" + t).hidden = (t !== tab));
  if (tab === "briefs") renderBriefs();
  if (tab === "todos") renderTodos();
  if (tab === "alert") renderAlert();
  if (tab === "settings") renderSettings();
}

document.querySelectorAll("nav button").forEach(b =>
  b.onclick = () => goto_(b.dataset.tab));

/* ---------------------------------------------------------------- 轮询 */

async function tick(){
  try { await loadBoard(); }
  catch(e){ $("h-health").innerHTML = "源 <b>不可达</b>"; $("h-health").className = "pill warn"; }
}

(async function start(){
  counts();
  try { await loadConfig(); } catch(e){}
  await tick();
  /* 支持 #b=<id> 直接打开某条详情的深链接 */
  const m = /^#b=(.+)$/.exec(location.hash || "");
  if (m) openDetail(decodeURIComponent(m[1]));
  setInterval(tick, 3000);
})();
</script>
</body>
</html>
"""


VIEWER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>kindlewarden 副屏</title>
<style>
  body{margin:0;background:#222;color:#ddd;font:14px/1.6 system-ui,sans-serif;
       display:flex;flex-direction:column;align-items:center;gap:12px;padding:16px}
  img{max-width:min(92vw,560px);border:1px solid #444;background:#fff}
  code{background:#111;padding:2px 6px;border-radius:4px}
  a{color:#6cf}
</style></head><body>
<h2>kindlewarden 副屏</h2>
<img id="s" src="/screen.png" alt="副屏">
<p><code id="r">rev —</code> · <span id="a">—</span></p>
<p><a href="/">打开控制台</a></p>
<script>
let last=null;
async function tick(){
  try{
    const r=await fetch('/rev',{cache:'no-store'}).then(x=>x.json());
    if(r.global_rev!==last){last=r.global_rev;
      document.getElementById('s').src='/screen.png?rev='+r.global_rev;}
    document.getElementById('r').textContent='rev '+r.global_rev;
    document.getElementById('a').textContent=
      '下次轮询 '+r.next_poll_after+'s · 数据源 '+r.source_health;
  }catch(e){document.getElementById('a').textContent='连接失败';}
  setTimeout(tick,5000);
}
tick();
</script></body></html>
"""


# ---------------------------------------------------------------- Kindle 浏览器版
#
# ⚠️ 这个页面**刻意做到零 JavaScript、零现代 CSS**，因为 Kindle 自带浏览器
#    （PW3 / 固件 5.16.2.1.1 的「体验版网页浏览器」）是老 WebKit：
#    - 不支持 let / const —— 一旦脚本里出现，**整段脚本都不执行**
#    - 没有 fetch、没有箭头函数、没有模板字符串
#    - CSS 不支持自定义属性（variables）和 display:grid
#    所以管理控制台（/）在它上面必然白屏。设备要看的只有一张全屏图片。
#
#    自动刷新用 <meta http-equiv="refresh">，间隔由服务端按当前活跃度注入
#    （有新内容就快一点，空闲就慢一点），老浏览器不需要为此执行任何脚本。
#
#    图片尺寸由服务端算好、直接写在 <img width= height=> 上，
#    **完全不依赖 CSS 缩放**。之前用 max-width/max-height:100% 想让它自动适配，
#    但替换元素 + 百分比高度父容器这个组合在老 WebKit 上解析不可靠 ——
#    实测结果就是底部那行「更新于 HH:MM」被切掉，而那一行恰恰是判断
#    「画面是不是过期的」的唯一依据。width/height 属性则是 HTML 3.2 就有的东西。

KINDLE_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="__EVERY__">
<title>副屏</title>
<style>
  html,body{margin:0;padding:0;background:#fff;}
  img{display:block;margin:0 auto;}
</style>
</head><body>
<img src="/screen.png?rev=__REV__" width="__W__" height="__H__" alt="">
</body></html>
"""

