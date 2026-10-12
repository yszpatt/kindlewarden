"""管理页**运行时注入的 HTML** 的完整性测试。

为什么需要这一层
----------------
管理页的列表行、详情按钮都是 ``innerHTML`` 注入的，内联处理器像这样拼出来：

    '<button onclick="removeTodo(' + JSON.stringify(id) + ')">删除</button>'

``JSON.stringify`` 产出的是**双引号**字符串，而属性本身也用双引号定界，于是：

    onclick="removeTodo("todo_abc")"
                        ↑ HTML 解析器在这里就结束了属性

属性值只剩 ``removeTodo(`` —— 语法错误。表现是**点击毫无反应**，
而且加载期不报错、只在事件触发时才炸。

``test_admin.py`` 里那条「内联处理器调用的函数是否已定义」的检查**抓不到它**：
函数确实定义了，坏掉的是属性**边界**。所以这里换一种测法：

1. 用 Node 跑**真实的管理页脚本**（只桩掉 DOM 和 fetch），把运行时
   注入的 HTML 原样抓出来 —— 不复制、不重写模板逻辑；
2. 用 Python 标准库里真正的 HTML 解析器 ``html.parser`` 解析它
   —— 自己写正则去切属性，等于把同一个 bug 再犯一遍；
3. 断言解析出来的属性值是一段**完整**的 JS 调用。

Node 不在时整个文件跳过（和项目里其它依赖外部工具的测试一致）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import pytest

from kindlewarden.admin_ui import ADMIN_HTML
from kindlewarden.config import Config
from kindlewarden.server import build_server

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("_admin_dom_harness.js")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 才能跑真实前端脚本")

#: 这些内联处理器必须出现在注入的 HTML 里，且属性要完整。
EXPECTED_TODO_HANDLERS = {"toggleTodo", "editTodo", "removeTodo"}
EXPECTED_DETAIL_HANDLERS = {"togglePin", "retract", "deleteBrief"}


class _HandlerCollector(HTMLParser):
    """收集所有内联事件处理器的**已解码**属性值。

    ``html.parser`` 会按 HTML 规范处理实体：``&quot;`` 解码回 ``"``。
    这正是浏览器的行为，也是本测试的意义所在 ——
    转义后的引号不会撑破属性，而裸引号会。
    """

    EVENTS = ("onclick", "onchange", "onblur", "oninput", "onkeydown")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.handlers: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in self.EVENTS and value is not None:
                self.handlers.append((key, value))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def _parse_handlers(html: str) -> list[tuple[str, str]]:
    p = _HandlerCollector()
    p.feed(html)
    p.close()
    return p.handlers


@pytest.fixture(scope="module")
def dom(tmp_path_factory):
    """起真服务取页面 → 交给 Node 跑真实脚本 → 拿回注入的 HTML。"""
    tmp = tmp_path_factory.mktemp("dom")
    cfg = Config()
    cfg.data_dir = tmp
    cfg.source_path = tmp / "panels.toml"
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            page = resp.read().decode("utf-8")

        page_path = tmp / "admin.html"
        page_path.write_text(page, encoding="utf-8")
        out_path = tmp / "dom.json"

        proc = subprocess.run(
            [NODE, str(HARNESS), str(page_path), str(out_path)],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, (
            f"Node harness 失败（rc={proc.returncode}）\n"
            f"stdout: {proc.stdout[:2000]}\nstderr: {proc.stderr[:2000]}")
        assert out_path.exists(), "harness 没有产出结果文件"
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert not data.get("errors"), f"前端脚本抛异常：{data['errors']}"
        return data
    finally:
        httpd.shutdown()
        httpd.server_close()


def _split_call(value: str) -> tuple[str, str]:
    """把 ``fn(args)`` 拆成 ``("fn", "args")``；不是函数调用就返回空名字。"""
    m = re.match(r"^\s*([A-Za-z_$][\w$]*)\s*\((.*)\)\s*$", value, re.S)
    return (m.group(1), m.group(2)) if m else ("", "")


def _assert_handler_intact(event: str, value: str, fn: str, arg_id: str) -> None:
    """属性值必须是一段完整、可解析、且带着正确 id 的函数调用。"""
    name, args = _split_call(value)
    assert name == fn, (
        f"{event} 属性被截断了：期望调用 {fn}(…)，实际解析出 {value!r}\n"
        "（属性里的双引号没转义，HTML 解析器提前结束了属性）")
    assert value.count("(") == value.count(")"), (
        f"{event}={value!r} 括号不配平 —— 多半是属性边界被撑破")
    assert arg_id in args, f"{event}={value!r} 里没有参数 {arg_id!r}"
    assert args.lstrip().startswith(f'"{arg_id}"'), (
        f"{event}={value!r} 的第一个参数不是带引号的 id —— "
        "JSON 字符串字面量被破坏后 JS 会把它当标识符")


# ---------------------------------------------------------------- 注入的 HTML


def test_todo_rows_have_intact_handlers(dom):
    """待办的勾选 / 编辑 / 删除三个处理器都要完好。

    这三处曾经**全部**失效：删除点了没反应、勾选框不动、编辑后不保存 ——
    根因都是同一个属性截断。
    """
    handlers = _parse_handlers(dom["todos_html"])
    by_fn = {}
    for event, value in handlers:
        name, _ = _split_call(value)
        if name:
            by_fn.setdefault(name, []).append((event, value))

    assert EXPECTED_TODO_HANDLERS <= set(by_fn), (
        f"待办行缺少处理器：{sorted(EXPECTED_TODO_HANDLERS - set(by_fn))}"
        f"（解析到的：{sorted(by_fn)}）")

    for fn in EXPECTED_TODO_HANDLERS:
        for event, value in by_fn[fn]:
            _assert_handler_intact(event, value, fn, dom["ids"]["todo"])


def test_brief_detail_buttons_have_intact_handlers(dom):
    """详情抽屉的钉住 / 撤回 / 彻底删除也必须是完整调用。"""
    handlers = _parse_handlers(dom["detail_html"])
    by_fn = {}
    for event, value in handlers:
        name, _ = _split_call(value)
        if name:
            by_fn.setdefault(name, []).append((event, value))

    assert EXPECTED_DETAIL_HANDLERS <= set(by_fn), (
        f"详情按钮缺少处理器：{sorted(EXPECTED_DETAIL_HANDLERS - set(by_fn))}"
        f"（解析到的：{sorted(by_fn)}）")

    for fn in EXPECTED_DETAIL_HANDLERS:
        for event, value in by_fn[fn]:
            _assert_handler_intact(event, value, fn, dom["ids"]["brief"])


def test_no_handler_attribute_is_left_unparsable(dom):
    """所有注入的处理器都必须能被解析成一个完整的调用 —— 一条都不许漏。

    这条是兜底：将来新增按钮时忘了走 ``act()``，这里会直接炸。
    """
    broken: list[str] = []
    for key in ("todos_html", "briefs_html", "detail_html"):
        for event, value in _parse_handlers(dom.get(key) or ""):
            if event == "onclick" and "return false" in value:
                continue                      # 内联表达式，不是函数调用
            name, _ = _split_call(value)
            if not name or value.count("(") != value.count(")"):
                broken.append(f"{key} {event}={value!r}")
    assert not broken, "存在无法解析的内联处理器：\n  " + "\n  ".join(broken)


# ---------------------------------------------------------------- 真的"点"一遍
#
# 上面那些断言证明属性**没被撑破**；下面这些进一步证明
# **点下去真的会发出那个请求** —— 这才是用户说的"删除无效"的完整闭环。


def _click_result(dom: dict, fn: str) -> dict:
    """取出某个函数对应的那次"点击"记录。"""
    for click in dom.get("clicks", []):
        name, _ = _split_call(click["handler"])
        if name == fn:
            return click
    raise AssertionError(
        f"没找到 {fn} 的点击记录（有："
        f"{[c['handler'] for c in dom.get('clicks', [])]}）")


def _assert_posts(click: dict, path: str, payload: dict) -> None:
    assert "error" not in click["result"], (
        f"执行 {click['handler']} 抛异常：{click['result'].get('error')}")
    posts = [c for c in click["calls"] if c["method"] == "POST"]
    assert any(c["path"] == path and c["body"] == payload for c in posts), (
        f"点击 {click['handler']} 后没有发出预期的请求\n"
        f"期望: POST {path} {payload}\n实际: {posts}")


def test_clicking_delete_todo_issues_the_remove_request(dom):
    """点「删除」必须真的发出 ``POST /todo/remove``。

    这是用户报的「待办删除无效」的直接回归测试：以前 onclick 属性被
    截断成 ``removeTodo(``，点击时既不报错也不发请求，看起来就是"没反应"。
    """
    click = _click_result(dom, "removeTodo")
    _assert_posts(click, "/todo/remove", {"id": dom["ids"]["todo"]})


def test_clicking_brief_actions_issue_their_requests(dom):
    """简报的三个动作各自发出正确的请求 —— 撤回与彻底删除是**不同**的端点。"""
    _assert_posts(_click_result(dom, "retract"), "/retract",
                  {"id": dom["ids"]["brief"]})
    _assert_posts(_click_result(dom, "deleteBrief"), "/brief/delete",
                  {"id": dom["ids"]["brief"]})
    _assert_posts(_click_result(dom, "togglePin"), "/pin",
                  {"id": dom["ids"]["brief"], "pinned": True})


def test_every_clickable_handler_runs_without_error(dom):
    """所有能被"点"的处理器都不能抛异常。

    属性被撑破时，浏览器是在**事件触发那一刻**才报 SyntaxError ——
    加载期一片安静。这里把那一刻提前到测试里。
    """
    bad = [c for c in dom.get("clicks", []) if "error" in c["result"]]
    assert not bad, "这些内联处理器一执行就炸：\n  " + "\n  ".join(
        f"{c['handler']} → {c['result']['error']}" for c in bad)
    assert dom.get("clicks"), "一条点击记录都没有，harness 可能没在工作"


# ---------------------------------------------------------------- 端到端


def _post(base: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_real_delete_button_actually_deletes(tmp_path):
    """端到端：**真实页面**的按钮 → **真实服务**的数据真的没了。

    前面几条测的是"属性完整"和"发出了正确的请求"。这条把最后一环也接上：
    用真实服务端返回的 /board 生成页面，取出真实按钮的请求，打回真服务，
    再确认待办和简报确实被删掉 —— 也就是用户报的那个现象本身。

    任何一环断了（属性被撑破、端点写错、投影没处理事件）这里都会红。
    """
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        todo_id = _post(base, "/todo", {"text": "这条要被删掉"})["todo_id"]
        brief_id = _post(base, "/brief", {"title": "这条要被删掉"})["brief_id"]
        assert _get(base, "/board")["total_todos"] == 1
        assert _get(base, "/board")["total_briefs"] == 1

        # ---- 用真实 board 数据跑真实前端脚本
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            page = resp.read().decode("utf-8")
        page_path = tmp_path / "admin.html"
        page_path.write_text(page, encoding="utf-8")
        board_path = tmp_path / "board.json"
        board_path.write_text(json.dumps(_get(base, "/board"), ensure_ascii=False),
                              encoding="utf-8")
        out_path = tmp_path / "dom.json"

        proc = subprocess.run(
            [NODE, str(HARNESS), str(page_path), str(out_path), str(board_path)],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        dom = json.loads(out_path.read_text(encoding="utf-8"))
        assert not dom.get("errors"), dom["errors"]
        # harness 应当抓到了真实 id，而不是内置样例
        assert dom["ids"]["todo"] == todo_id
        assert dom["ids"]["brief"] == brief_id

        # ---- 把页面按钮发出的请求**原样**打回真服务
        for fn, expect_path in (("removeTodo", "/todo/remove"),
                                ("deleteBrief", "/brief/delete")):
            click = _click_result(dom, fn)
            posts = [c for c in click["calls"] if c["method"] == "POST"]
            assert posts, f"{fn} 点击后没有发出任何请求"
            for call in posts:
                _post(base, call["path"], call["body"])
            assert any(c["path"] == expect_path for c in posts), posts

        after = _get(base, "/board")
        assert after["total_todos"] == 0, "待办没有被删掉"
        assert after["total_briefs"] == 0, "简报没有被删掉"
        assert _get(base, "/board?include_hidden=1")["briefs"] == [], \
            "彻底删除后归档里不该还有它"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- 源码级防护


def _strip_js_comments(src: str) -> str:
    """去掉 JS 的块注释与行注释，**保留行号**。

    和 ``tools/check_lua.py`` 的 ``strip_comments`` 是同一个教训
    （见 docs/dev-notes.md「剥注释」那一节）：本文件里正好有一段注释在
    **演示错误写法**，不剥掉的话它会把自己判成违规 —— 这次实测就发生了。

    没有直接复用 Lua 那份，是因为注释语法不同（Lua 是 ``--``，
    JS 是 ``//`` 与 ``/* */``）。块注释替换成等量的换行，
    这样报出来的行号仍然对得上。
    """
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                 src, flags=re.S)
    # 行注释：`//` 前面不是 `:`，免得把 https:// 这类也切掉
    return re.sub(r"(?<!:)//[^\n]*", "", src)


def test_admin_source_never_embeds_json_inside_a_quoted_attribute():
    """源码级防护：不许再写出 ``onclick="…" + JSON.stringify(…)``。

    这是那个 bug 的**确切形状** —— 属性用双引号定界，插进来的却是
    带双引号的 JSON 字符串。不需要 Node，任何环境都会跑这条。

    判据故意取"同一行同时出现内联属性和 JSON.stringify"：
    不去猜属性有没有闭合（那需要把 JS 字符串字面量解析一遍，
    反而容易写出抓不住的正则）。本文件里合法的地方都不会两者同时出现。
    """
    attr = re.compile(r'on(?:click|change|blur|input|keydown)\s*=')
    code = _strip_js_comments(ADMIN_HTML)
    offenders = [
        f"L{lineno}: {line.strip()}"
        for lineno, line in enumerate(code.splitlines(), 1)
        if "JSON.stringify" in line and attr.search(line)
    ]
    assert not offenders, (
        "内联处理器里直接嵌了 JSON.stringify —— 属性边界会被撑破，"
        "点击静默失效。请改用 act()/q()：\n  " + "\n  ".join(offenders))


def test_strip_js_comments_keeps_line_numbers_and_urls():
    """剥注释这一步本身也要对：行号不漂、URL 不被当成注释切掉。"""
    src = 'line1\n/* c\nc */\nlet u = "https://x";  // trailing\nlast'
    out = _strip_js_comments(src)
    assert out.splitlines()[0] == "line1"
    assert out.splitlines()[3].strip().startswith('let u = "https://x"')
    assert "tralling" not in out and "trailing" not in out
    assert out.count("\n") == src.count("\n")


def test_admin_source_uses_the_act_helper_for_dynamic_handlers(dom):
    """动态注入的处理器必须走 ``act()``（而不是手拼引号）。"""
    assert "function act(" in ADMIN_HTML
    assert "const q = s => JSON.stringify" in ADMIN_HTML
    # 三处以上动态处理器都在用
    assert ADMIN_HTML.count("act(") >= 5, "act() 用得比预期少，可能有地方漏改"
