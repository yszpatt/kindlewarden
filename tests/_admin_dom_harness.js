'use strict';
/* 在 Node 里跑管理页**真实**的脚本，把运行时注入的 HTML 抓出来，
   供 pytest 用真正的 HTML 解析器检查属性完整性。

   为什么必须这么测：内联处理器是 innerHTML 注入的，属性一旦被引号撑破，
   浏览器只会"点了没反应"，加载期不报错 —— 静态正则完全抓不到。
*/
const fs = require('fs');
const vm = require('vm');

const pagePath = process.argv[2];
const outPath = process.argv[3];
/* 可选：用真实服务端返回的 /board 数据喂进来，
   这样生成的内联处理器里带的是**真实 id**，可以拿回去打真服务。 */
const boardPath = process.argv[4];

const html = fs.readFileSync(pagePath, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('页面里没有 <script>'); process.exit(2); }
const src = m[1];

const elements = new Map();
function makeEl(id) {
  return {
    id, _html: '', textContent: '', value: '', checked: false,
    className: '', hidden: false, dataset: {}, style: {},
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    querySelectorAll() { return []; },
    addEventListener() {},
    classList: { add() {}, remove() {}, toggle() {} },
  };
}
function el(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

const BRIEF_ID = 'brief_0123456789';
const TODO_ID = 'todo_abcdef0123';

const CANNED_BOARD = {
  rev: 'deadbeefcafe',
  generated_at: 1791609120,
  source_health: 'ok',
  screen: 'board',
  size: [1072, 1448],
  alert: null,
  briefs: [{
    id: BRIEF_ID, ts: 1791609120, title: '测试简报', status: 'ok',
    summary: '摘要', artifacts: [], project: 'proj', tags: [], duration_s: 60,
    source: {}, pinned: false, superseded_by: '', retracted: false,
    warnings: [], rect: [40, 100, 1032, 228],
  }],
  todos: [{
    id: TODO_ID, ts: 1791609120, text: '测试待办', done: false,
    done_ts: 0, rect: [40, 300, 1032, 364],
  }],
  total_briefs: 1, total_todos: 1,
};

/* 真实 board（若提供）优先；否则用内置样例。 */
const BOARD = boardPath
  ? JSON.parse(fs.readFileSync(boardPath, 'utf8'))
  : CANNED_BOARD;
const REAL_BRIEF = BOARD.briefs && BOARD.briefs[0] ? BOARD.briefs[0].id : BRIEF_ID;
const REAL_TODO = BOARD.todos && BOARD.todos[0] ? BOARD.todos[0].id : TODO_ID;
const DEVICE = { device: null, poll_idle_s: 300, lan: ['192.168.1.5'], port: 8577 };
const CONFIG = {
  panels: { alerts: true, briefs: true, todos: true, ambient: true, statusbar: true },
  board: { max_briefs: 6, max_todos: 8, brief_retention_h: 24, failed_retention_h: 72, show_info: true },
  ambient: { weather: '', show_clock: false },
  browser: { viewport_w: 1072, viewport_h: 1388 },
  server: { host: '0.0.0.0', port: 8577, heartbeat_min: 10, poll_idle_s: 300, poll_active_s: 30 },
  source_path: 'x',
};

const jsonResponse = obj => Promise.resolve({
  ok: true, status: 200, statusText: 'OK',
  text: () => Promise.resolve(JSON.stringify(obj)),
  json: () => Promise.resolve(obj),
});

/* 记录所有发出去的请求 —— 用来验证"点一下到底有没有真的发请求"。 */
const CALLS = [];

const sandbox = {
  console,
  document: { getElementById: el, querySelectorAll: () => [] },
  location: { hash: '', port: '8577', pathname: '/' },
  history: { replaceState() {} },
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  Image: function () { this.onload = null; },
  fetch: (path, opts) => {
    CALLS.push({
      path: String(path),
      method: (opts && opts.method) || 'GET',
      body: opts && opts.body ? JSON.parse(opts.body) : null,
    });
    if (path.startsWith('/board')) return jsonResponse(BOARD);
    if (path.startsWith('/device')) return jsonResponse(DEVICE);
    if (path.startsWith('/config')) return jsonResponse(CONFIG);
    return jsonResponse({ accepted: true });
  },
  confirm: () => true,
  alert: () => {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx, { filename: 'admin.js' });

/* 浏览器的属性值会先做实体解码，再交给 JS —— 这里必须照做，
   否则拿到的是 removeTodo(&quot;x&quot;) 而不是 removeTodo("x")。
   &amp; 必须**最后**解码，避免把 &amp;quot; 二次解码成引号。 */
function decodeEntities(s) {
  return String(s)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}

/* 像浏览器那样"点"一个内联处理器：解码实体后当脚本执行。
   含 `this` 的（onchange/onblur）需要一个真实元素作用域，
   这里跳过 —— 它们由另外的断言覆盖属性完整性。 */
function clickHandler(rawValue) {
  const value = decodeEntities(rawValue);
  if (/\bthis\b/.test(value)) return { skipped: 'uses this' };
  try {
    vm.runInContext(value, ctx);
    return { ok: true, value };
  } catch (e) {
    return { error: String((e && e.message) || e), value };
  }
}

function handlersIn(html) {
  const out = [];
  const re = /on(click|change|blur|input|keydown)\s*=\s*"([^"]*)"/g;
  let m;
  while ((m = re.exec(html)) !== null) out.push({ event: m[1], value: m[2] });
  return out;
}

const errors = [];
setImmediate(() => setImmediate(() => {
  const out = { errors, ids: { brief: REAL_BRIEF, todo: REAL_TODO } };
  try {
    // 强制渲染待办页（默认 TAB 是 briefs）
    vm.runInContext('goto_("todos")', ctx);
    out.todos_html = el('todos')._html;
    // 简报列表
    out.briefs_html = el('briefs')._html;
    // 详情抽屉（按钮在 d-actions 里，同样是 innerHTML 注入）
    vm.runInContext('openDetail(' + JSON.stringify(REAL_BRIEF) + ')', ctx);
    out.detail_html = el('d-actions')._html;

    // ---- 真的"点"一遍：从渲染出来的 HTML 里取出处理器并执行 ----
    CALLS.length = 0;
    out.clicks = [];
    for (const src of [out.todos_html, out.detail_html]) {
      for (const h of handlersIn(src)) {
        const before = CALLS.length;
        const res = clickHandler(h.value);
        out.clicks.push({
          handler: decodeEntities(h.value),
          raw_handler: h.value,
          result: res,
          calls: CALLS.slice(before),
        });
      }
    }
    out.all_calls = CALLS.slice();
  } catch (e) {
    errors.push(String((e && e.stack) || e));
  }
  fs.writeFileSync(outPath, JSON.stringify(out), 'utf8');
}));
