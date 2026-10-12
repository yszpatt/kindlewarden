"""管理页与配套接口的端到端测试。

真的起一个 HTTP 服务（端口 0），用 urllib 打进去 ——
路由分派、JSON 编解码、配置热生效这些东西只有走一遍真实请求才算验证过。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kindlewarden.config import Config
from kindlewarden.server import build_server

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def live(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", cfg
    httpd.shutdown()
    httpd.server_close()


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        return json.loads(raw.decode("utf-8")) if "json" in ctype else raw.decode("utf-8")


def _post(base: str, path: str, payload: dict):
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_text(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------- 客户端脚本的解析约定


def test_rev_response_is_parseable_by_the_kindle_script_recipe(live):
    """Kindle 客户端用「压平空白 + sed」解析 ``/rev``，规则必须和服务端真实输出对得上。

    回归测试：服务端是 Python ``json.dumps`` 输出的，默认格式为
    ``"global_rev": "abc123"`` —— **冒号后面有空格**。
    而脚本原先的 sed 模式假定无空格，结果是：拉取成功、日志里一条错都没有，
    但永远解析不出 rev，屏幕一直不动。这种「看起来一切正常」的失败最难查。
    """
    base, _ = live
    _post(base, "/brief", {"title": "解析回归测试"})
    raw = _get_text(base, "/rev")

    # 先确认这个回归测试有意义：原始响应确实带空格
    assert '"global_rev": "' in raw, "服务端格式变了，这条回归测试需要重写"

    # 脚本的做法：tr -d '[:space:]' 压平，再用无空格模式匹配
    flat = re.sub(r"\s+", "", raw)
    found = re.search(r'"global_rev":"([^"]*)"', flat)
    assert found, f"压平后仍解析不出 rev：{raw[:160]}"
    assert len(found.group(1)) == 12, found.group(1)

    poll = re.search(r'"next_poll_after":(\d+)', flat)
    assert poll, "压平后解析不出 next_poll_after"

    # 旧写法（无空格、不压平）**必须**是失败的 —— 否则说明服务端格式变了，
    # 那就要重新审视脚本里的做法，而不是让它碰巧能用。
    assert not re.search(r'"global_rev":"([^"]*)"', raw), \
        "服务端不再输出带空格的 JSON 了，脚本文档和测试都该更新"


def test_kindle_script_flattens_whitespace_before_parsing(live):
    """脚本必须保留压平空白这一步，并且对解析失败显式报错。"""
    script = (ROOT / "kindle" / "warden.sh").read_text(encoding="utf-8")

    assert "tr -d '[:space:]'" in script, "解析前必须先压平空白"
    assert 'FLAT=$(echo "$RES"' in script
    # 解析不出 rev 时必须记日志并跳过，而不是安静地什么都不做
    assert "解析 rev 失败" in script
    # 不能出现旧的、假定无空格的写法
    assert 's/.*"global_rev":"' not in script.replace(
        's/.*"global_rev":"\\([^"]*\\)".*/\\1/p', "")


def test_kindle_script_discovers_fbink_outside_path():
    """越狱包把 fbink 放在 ``/mnt/us/libkh/bin/fbink``，**不在 PATH 里**。

    回归测试：脚本原先裸调 ``fbink``，上机后是 command not found，
    而日志里只会显示「绘制失败」—— 完全看不出是路径问题。
    （WinterBreak 带的 libkh 就是放在这个位置的，已实测确认。）
    """
    script = (ROOT / "kindle" / "warden.sh").read_text(encoding="utf-8")
    config = (ROOT / "kindle" / "config.sh").read_text(encoding="utf-8")

    assert "find_fbink" in script, "必须主动查找 fbink"
    assert "/mnt/us/libkh/bin/fbink" in script, "WinterBreak 带的 fbink 位置要覆盖到"
    assert "WARDEN_FBINK" in config, "要能在 config.sh 里手工指定"
    assert '"$FBINK" -g "file=$1,dither"' in script, "blit 必须用找到的路径调用"


def test_kindle_script_redraws_periodically_to_fight_the_framework():
    """必须定期重绘，否则画面会被 Kindle 的 framework 盖掉。

    fbink 直接往 framebuffer 画，而 **Kindle 的 Java framework 也在画同一个**。
    scriptlet 退出后 framework 重绘书库界面，我们画的东西就没了 ——
    实测现象正是「画面闪一下就消失」。

    客户端原本只在 rev 变化时才画，所以被盖掉之后要等好几分钟才恢复。
    """
    script = (ROOT / "kindle" / "warden.sh").read_text(encoding="utf-8")
    config = (ROOT / "kindle" / "config.sh").read_text(encoding="utf-8")

    assert "WARDEN_REDRAW_EVERY" in config, "重绘周期要可配"
    assert "REDRAW_EVERY" in script
    assert "LAST_DRAW" in script, "要记录上次绘制时间才能算周期"
    # 内容变化 → 闪刷（清残影）；周期重绘 → 不闪（免得每 60 秒黑闪一次）
    assert 'blit "$PNG" 1' in script
    assert 'blit "$PNG" 0' in script
    # 画面文件丢了（重启过 / /tmp 被清）要能强制重新下载
    assert 'rm -f "$REVFILE"' in script


def test_kindle_script_uses_chunked_sleep_so_traps_fire():
    """shell 在 ``sleep`` 期间**不执行 trap**。

    主循环可能睡 300 秒，于是收到停止信号后要等 5 分钟才跑 cleanup ——
    表现是「点了停止没反应」，而且日志里永远看不到「已停止」，
    排查时就分不清「被杀了」还是「还活着」。分片睡眠解决这个。
    """
    script = (ROOT / "kindle" / "warden.sh").read_text(encoding="utf-8")
    # 等待被拆成小片
    assert 'WAIT="$NEXT"' in script, "主循环等待要拆片"
    assert 'sleep "$STEP"' in script, "每片要单独 sleep"
    assert "nap() {" in script, "失败路径仍需要分片睡眠"
    # 主循环里不该再有整段的长睡眠
    loop = script.split("while :; do", 1)[1]
    assert 'sleep "$NEXT"' not in loop, "不能整段睡 next_poll_after"


def test_redraw_is_decoupled_from_poll_interval():
    """重绘检查必须落在**分片等待里**，不能只在主循环开头查一次。

    空闲时服务端给的 ``next_poll_after`` 是 **300 秒**。如果整段 sleep 掉，
    ``REDRAW_EVERY=60`` 就形同虚设 —— 画面被 framework 盖掉后要等 5 分钟
    才可能回来，看起来就是彻底失效。

    实测踩过这个坑：日志里启动后只出现一次「周期重绘」，之后再无任何动静。
    """
    script = (ROOT / "kindle" / "warden.sh").read_text(encoding="utf-8")

    assert "maybe_redraw() {" in script, "重绘检查要抽成函数"
    wait_block = script.split('WAIT="$NEXT"', 1)
    assert len(wait_block) == 2, "找不到分片等待块"
    assert "maybe_redraw" in wait_block[1], "分片等待期间必须持续维护屏幕"
    # 旧的写法（整段 nap）必须消失
    assert 'nap "$NEXT"' not in script


def test_kindle_script_logs_periodic_redraw():
    """周期重绘必须打日志。

    否则「重绘没生效」和「进程已经死了」在日志里长得一模一样 ——
    这个盲点已经踩过一次，白跑了一轮排查。
    """
    script = (ROOT / "kindle" / "warden.sh").read_text(encoding="utf-8")
    assert "周期重绘" in script, "周期重绘要留日志"
    assert "启动 pid=$$" in script, "启动时要记 PID"


def test_kindle_script_reinforces_screensaver_block_every_cycle():
    """``preventScreenSaver`` 会被 powerd 重置，设一次不管用。

    参考实现 alexishida/kindle-dashboard 的注释同样写着
    「powerd 有时会重置，所以每个周期都要重新设置一次」。
    """
    script = (ROOT / "kindle" / "warden.sh").read_text(encoding="utf-8")
    loop = script.split("while :; do", 1)[1]
    assert "preventScreenSaver 1" in loop, "循环里每轮都要重新阻止屏保"


def test_kindle_script_uses_portable_wget_timeout():
    """``-T`` 而不是 ``--timeout`` —— Kindle 上的 wget 可能是 busybox 版。

    而且这个参数在 config.sh 里，万一设备上连 ``-T`` 都不认，用户能自己改，
    不用去动脚本。
    """
    script = (ROOT / "kindle" / "warden.sh").read_text(encoding="utf-8")
    config = (ROOT / "kindle" / "config.sh").read_text(encoding="utf-8")

    assert "--timeout" not in script, "不要用 GNU 专属的 --timeout"
    assert "WARDEN_WGET_OPTS" in config, "wget 参数要可以在 config.sh 里调"
    assert "WARDEN_WGET_OPTS" in script


# ---------------------------------------------------------------- 页面本身


def test_admin_page_is_served_at_root(live):
    base, _ = live
    html = _get(base, "/")
    assert "<title>kindlewarden 控制台</title>" in html
    assert "/board" in html and "/config" in html


def test_admin_page_has_no_external_resources(live):
    """**不能引任何 CDN。**

    这台机器和 Kindle 在同一个局域网里，很可能没有外网。
    一个「打开是白屏」的管理页比没有管理页更糟。
    """
    base, _ = live
    html = _get(base, "/")
    assert "<script src" not in html
    assert "<link " not in html
    assert "cdn." not in html.lower()
    assert "googleapis" not in html.lower()
    assert "unpkg" not in html.lower()


def test_admin_page_element_ids_all_exist(live):
    """JS 里 ``$("id")`` 引用的元素必须都真实存在。

    这类错误**不会抛异常**，只会在运行时静默失效 —— 某个按钮点了没反应、
    某个面板永远空白。语法检查抓不到，肉眼看页面也未必发现。
    目前没法在沙箱里跑浏览器，所以静态查一遍。
    """
    base, _ = live
    html = _get(base, "/")

    ids = set(re.findall(r'\bid="([^"]+)"', html))
    # 设置表单的 id 是按配置拼出来的，不在静态 HTML 里
    editable = {
        "panels": ("alerts", "briefs", "todos", "ambient", "statusbar"),
        "board": ("max_briefs", "max_todos", "brief_retention_h",
                  "failed_retention_h", "show_info"),
        "ambient": ("weather", "show_clock"),
        "browser": ("viewport_w", "viewport_h"),
        "server": ("heartbeat_min", "poll_idle_s", "poll_active_s"),
    }
    ids |= {f"cfg-{sec}.{key}" for sec, keys in editable.items() for key in keys}

    refs = set(re.findall(r'\$\("([A-Za-z0-9_.\-]+)"\)', html))
    assert refs, "没有找到任何 $(\"id\") 引用，正则可能失效了"
    missing = sorted(refs - ids)
    assert not missing, f"JS 引用了不存在的元素 id：{missing}"


def test_admin_page_inline_handlers_are_defined(live):
    """内联 ``onclick="fn()"`` 调的必须是自己定义过的函数。

    这些同样不会在加载时报错，只会点了没反应。
    """
    base, _ = live
    html = _get(base, "/")

    handlers = set(re.findall(r'on(?:click|change|input|keydown)="([A-Za-z_]\w*)\(', html))
    # 内联处理器里可以写表达式而不仅是函数调用，比如 onkeydown="if(...)..."。
    # 这些不是「未定义的函数」，别误报。
    keywords = {"if", "for", "while", "switch", "return", "typeof", "event"}
    handlers -= keywords

    defined = set(re.findall(r'function\s+([A-Za-z_]\w*)\s*\(', html))
    defined |= set(re.findall(r'(?:const|let|var)\s+([A-Za-z_]\w*)\s*=', html))

    assert handlers, "没有找到任何内联事件处理器，正则可能失效了"
    missing = sorted(handlers - defined)
    assert not missing, f"内联事件调用了未定义的函数：{missing}"


def test_admin_page_has_all_tab_panes(live):
    base, _ = live
    html = _get(base, "/")
    panes = set(re.findall(r'id="tab-([a-z]+)"', html))
    assert panes == {"briefs", "todos", "alert", "publish", "settings"}


def test_admin_page_supports_detail_deeplink(live):
    """``#b=<id>`` 深链接 —— 详情页可以直接分享 / 收藏。"""
    base, _ = live
    html = _get(base, "/")
    assert r"/^#b=(.+)$/" in html
    assert "history.replaceState" in html


def test_viewer_page_still_available(live):
    base, _ = live
    html = _get(base, "/view")
    assert "副屏" in html


# ---------------------------------------------------------------- Kindle 浏览器版


def _fetch(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.headers, resp.read()


def test_kindle_page_is_javascript_free(live):
    """Kindle 自带浏览器是老 WebKit：不支持 let / const。

    一旦脚本里出现这些，**整段脚本都不执行** —— 所以这个页面必须完全没有 JS。
    管理控制台（/）用的 CSS 变量、display:grid、fetch、箭头函数它同样不支持，
    在设备上必然白屏。设备要看的只有一张全屏图片。
    """
    base, _ = live
    for route in ("/k", "/kindle"):
        headers, raw = _fetch(base, route)
        html = raw.decode("utf-8")
        assert "text/html" in headers.get("Content-Type", "")
        assert "<script" not in html.lower(), f"{route} 里不该有任何脚本"
        assert "fetch(" not in html
        assert "=>" not in html
        # 没有 JS 却出现 let/const 只可能是在 <style> 里，这里不该有
        assert "const " not in html and "let " not in html


def test_kindle_page_uses_meta_refresh_with_server_chosen_interval(live):
    """自动刷新靠 <meta http-equiv="refresh">，间隔由服务端按活跃度注入。

    间隔**必须封顶 60s**：越狱客户端每次轮询都会重新问 /rev 所以能自适应，
    但 <meta refresh> 是页面加载时定死的 —— 空闲时按 300s 渲染的话，
    之后即使立刻来了新简报，也要等满 5 分钟才出现。
    """
    base, _ = live
    _, raw = _fetch(base, "/kindle")
    html = raw.decode("utf-8")

    match = re.search(r'<meta http-equiv="refresh" content="(\d+)">', html)
    assert match, "缺少 meta refresh"
    every = int(match.group(1))
    assert 15 <= every <= 60, f"刷新间隔 {every}s 超出合理范围"

    rev = _get(base, "/rev")
    assert every == max(15, min(rev["next_poll_after"], 60))


def test_kindle_page_image_url_is_content_addressed(live):
    """图片 URL 带 rev，配合长缓存 —— 画面没变就不重新下载。"""
    base, _ = live
    _post(base, "/brief", {"title": "给 Kindle 看的"})
    rev = _get(base, "/rev")["global_rev"]
    _, raw = _fetch(base, "/kindle")
    assert f'src="/screen.png?rev={rev}"' in raw.decode("utf-8")


def test_kindle_page_sizes_image_with_explicit_attributes(live):
    """图片尺寸必须写在 width/height 属性上，**不能交给 CSS 缩放**。

    回归测试：先前用 max-width/max-height:100% 让浏览器自适应，
    但「替换元素 + 百分比高度父容器」这个组合在老 WebKit 上解析不可靠 ——
    实测就是底部那行「更新于 HH:MM」被切掉，而那一行是判断画面是否过期的唯一依据。
    """
    base, cfg = live
    _, raw = _fetch(base, "/kindle")
    html = raw.decode("utf-8")

    match = re.search(r'<img src="/screen\.png\?rev=[^"]+" width="(\d+)" height="(\d+)"', html)
    assert match, f"图片缺少显式的 width/height：{html}"
    w, h = int(match.group(1)), int(match.group(2))

    # 必须完整放得进配置的视口
    assert w <= cfg.browser.viewport_w, (w, cfg.browser.viewport_w)
    assert h <= cfg.browser.viewport_h, (h, cfg.browser.viewport_h)
    # 且保持画面比例
    assert abs(h / w - 1448 / 1072) < 0.01

    # 样式里不能再出现依赖百分比高度的缩放
    style = html.split("<style>")[1].split("</style>")[0]
    assert "max-height" not in style
    assert "max-width" not in style
    assert "height:100%" not in style


def test_kindle_page_accepts_viewport_overrides(live):
    """支持 ?w= / ?h= 临时试 —— 在设备上不方便改配置。"""
    base, _ = live
    _, raw = _fetch(base, "/kindle?w=800&h=2000")
    html = raw.decode("utf-8")
    match = re.search(r'width="(\d+)" height="(\d+)"', html)
    assert match
    assert int(match.group(1)) == 800          # 宽度受 800 限制

    # 非法值要回落，不能把页面搞崩
    _, raw = _fetch(base, "/kindle?w=abc&h=-5")
    html = raw.decode("utf-8")
    assert re.search(r'width="\d+" height="\d+"', html)


def test_viewport_is_editable_from_the_console(live):
    """视口尺寸要在管理页里可调 —— 用户没法直接看设备报告。"""
    base, cfg = live
    assert "viewport_w" in _get(base, "/config")["editable"]["browser"]

    _post(base, "/config", {"browser": {"viewport_h": 1200}})
    assert cfg.browser.viewport_h == 1200

    _, raw = _fetch(base, "/kindle")
    html = raw.decode("utf-8")
    match = re.search(r'width="(\d+)" height="(\d+)"', html)
    assert int(match.group(2)) <= 1200


def test_kindle_page_uses_no_modern_css(live):
    """CSS 自定义属性和 grid 在老 WebKit 上不支持。"""
    base, _ = live
    _, raw = _fetch(base, "/kindle")
    html = raw.decode("utf-8")
    style = html.split("<style>")[1].split("</style>")[0]
    assert "display:grid" not in style
    assert "var(" not in style        # 自定义属性
    assert "flex" not in style
    assert "--" not in style          # 变量声明


def test_rev_query_param_enables_cache_busting(live):
    """带 rev 的图片可以长缓存；不带 rev 的必须 no-store。

    带 rev 的 URL 是**内容寻址**的：内容一变 rev 就变、URL 就变，
    所以长缓存是安全的，而且能让 Kindle 只在画面真的变化时才下载。
    """
    base, _ = live
    _post(base, "/brief", {"title": "缓存测试"})
    rev = _get(base, "/rev")["global_rev"]

    headers, _body = _fetch(base, f"/screen.png?rev={rev}")
    assert "immutable" in headers.get("Cache-Control", "")
    assert "max-age" in headers.get("Cache-Control", "")

    headers, _body = _fetch(base, "/screen.png")
    assert headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------- 看板数据


def test_board_exposes_full_text_and_display_warnings(live):
    """管理页的详情功能依赖这个：入库不截断，全文要拿得到。"""
    base, _ = live
    long_title = "非常长的标题" * 6
    _post(base, "/brief", {"title": long_title, "summary": "短摘要", "status": "ok"})

    board = _get(base, "/board")
    brief = board["briefs"][0]
    assert brief["title"] == long_title          # 全文
    assert brief["warnings"], "应当提示这条在板上会被截断"


def test_include_hidden_reveals_retracted(live):
    base, _ = live
    brief_id = _post(base, "/brief", {"title": "会被撤回"})["brief_id"]

    assert len(_get(base, "/board")["briefs"]) == 1
    _post(base, "/retract", {"id": brief_id})
    assert len(_get(base, "/board")["briefs"]) == 0

    shown = _get(base, "/board?include_hidden=1")["briefs"]
    assert len(shown) == 1
    assert shown[0]["retracted"] is True
    # 归档条目不算进「可见数量」
    assert _get(base, "/board?include_hidden=1")["total_briefs"] == 0


def test_supersede_keeps_only_the_new_one_visible(live):
    base, _ = live
    old = _post(base, "/brief", {"title": "初版结论"})["brief_id"]
    _post(base, "/brief", {"title": "修订结论", "supersedes": old})

    board = _get(base, "/board")
    assert [b["title"] for b in board["briefs"]] == ["修订结论"]

    archived = _get(base, "/board?include_hidden=1")["briefs"]
    assert any(b["title"] == "初版结论" and b["superseded_by"] for b in archived)


def test_delete_brief_removes_it_from_the_archive_too(live):
    """彻底删除与撤回的分界：归档里也不再有。

    这条也顺带锁住了"简报可被真正删掉"这个能力 ——
    管理页上「彻底删除」按钮打的就是 ``/brief/delete``。
    """
    base, _ = live
    keep = _post(base, "/brief", {"title": "留下的"})["brief_id"]
    gone = _post(base, "/brief", {"title": "要彻底删掉的"})["brief_id"]

    assert len(_get(base, "/board")["briefs"]) == 2

    result = _post(base, "/brief/delete", {"id": gone})
    assert result["accepted"] is True and result["deleted"] is True

    visible = _get(base, "/board")["briefs"]
    assert [b["id"] for b in visible] == [keep]

    # 关键差别：撤回能让它出现在归档里，删除不行
    archived = _get(base, "/board?include_hidden=1")["briefs"]
    assert [b["id"] for b in archived] == [keep], "彻底删除后归档里不该还有它"


def test_delete_unknown_brief_does_not_break_the_board(live):
    base, _ = live
    keep = _post(base, "/brief", {"title": "正常"})["brief_id"]
    _post(base, "/brief/delete", {"id": "brief_不存在"})
    assert [b["id"] for b in _get(base, "/board")["briefs"]] == [keep]


def test_retract_and_delete_are_different(live):
    """两个动作必须语义可分 —— 页面上的按钮文案不同，后端行为也必须不同。"""
    base, _ = live
    a = _post(base, "/brief", {"title": "A"})["brief_id"]
    b = _post(base, "/brief", {"title": "B"})["brief_id"]

    _post(base, "/retract", {"id": a})
    _post(base, "/brief/delete", {"id": b})

    visible = [x["id"] for x in _get(base, "/board")["briefs"]]
    archived = [x["id"] for x in _get(base, "/board?include_hidden=1")["briefs"]]
    assert visible == []
    assert archived == [a], "撤回的留在归档，删除的不在"


# ---------------------------------------------------------------- 待办


def test_todo_lifecycle_over_http(live):
    base, _ = live
    todo_id = _post(base, "/todo", {"text": "写文档"})["todo_id"]

    _post(base, "/todo/update", {"id": todo_id, "text": "写完文档"})
    assert _get(base, "/board")["todos"][0]["text"] == "写完文档"

    _post(base, "/todo/toggle", {"id": todo_id, "done": True})
    assert _get(base, "/board")["todos"][0]["done"] is True

    _post(base, "/todo/remove", {"id": todo_id})
    assert _get(base, "/board")["todos"] == []


# ---------------------------------------------------------------- 配置热生效


def test_config_endpoint_lists_editable_fields(live):
    base, _ = live
    cfg = _get(base, "/config")
    assert "panels" in cfg["editable"] and "board" in cfg["editable"]
    assert "todos" in cfg["editable"]["panels"]
    # host/port 改不了（socket 已经绑好了），必须如实标出来
    assert "host" in cfg["restart_required"]
    assert "port" in cfg["restart_required"]


def test_config_change_applies_without_restart(live):
    """关掉待办板块应当**立即**从画面上消失 —— 不用重启服务。"""
    base, cfg = live
    _post(base, "/todo", {"text": "这条应该随后消失"})
    before = _get(base, "/board")
    assert before["total_todos"] == 1

    result = _post(base, "/config", {"panels": {"todos": False}})
    assert "panels.todos" in result["changed"]
    assert cfg.panels.todos is False

    # 确实写回了文件
    assert cfg.source_path.exists()
    assert "todos = false" in cfg.source_path.read_text(encoding="utf-8")

    # 而且渲染出来的画面变了（rev 变化即证明重渲染过）
    after = _get(base, "/board")
    assert after["rev"] != before["rev"]


def test_config_rejects_unknown_and_uneditable_keys(live):
    base, cfg = live
    _post(base, "/config", {
        "panels": {"todos": False, "不存在的板块": True},
        "server": {"port": 9999},          # 不可编辑，必须被忽略
        "乱写的段": {"x": 1},
    })
    assert cfg.server.port != 9999
    assert cfg.panels.todos is False


# ---------------------------------------------------------------- 清空


def test_clear_board_empties_everything(live):
    base, _ = live
    _post(base, "/brief", {"title": "一条简报"})
    _post(base, "/todo", {"text": "一条待办"})
    _post(base, "/alert", {"title": "一条告警"})

    result = _post(base, "/clear", {})
    assert result["retracted"] == 1
    assert result["todos_removed"] == 1
    assert result["alert_cleared"] is True

    board = _get(base, "/board")
    assert board["briefs"] == [] and board["todos"] == [] and board["alert"] is None

    # 清空是可追溯的事件，不是删文件：归档里还看得到
    assert len(_get(base, "/board?include_hidden=1")["briefs"]) == 1


# ---------------------------------------------------------------- 设备在线状态


def test_device_status_only_counts_kindle_requests(live):
    """管理页自己也轮询 /rev。

    如果不区分，「Kindle 最后在线」会永远显示「刚刚」，这个指标就废了。
    """
    base, _ = live
    assert _get(base, "/device")["device"] is None

    _get(base, "/rev")                      # 管理页式的轮询
    assert _get(base, "/device")["device"] is None

    _get(base, "/rev?c=kindle")             # 设备式轮询
    dev = _get(base, "/device")["device"]
    assert dev is not None and dev["ip"] == "127.0.0.1"
    assert dev["age_s"] >= 0


def test_device_status_persists_across_restart(live, tmp_path):
    """设备最后在线时间要落盘 —— 服务重启后管理页仍应看得到。"""
    base, cfg = live
    _get(base, "/rev?c=kindle")
    saved = json.loads((cfg.data_dir / "device.json").read_text(encoding="utf-8"))
    assert saved["ip"] == "127.0.0.1"


# ---------------------------------------------------------------- 强制重渲染


def test_refresh_forces_a_new_render(live):
    base, _ = live
    _post(base, "/brief", {"title": "触发一次渲染"})
    first = _get(base, "/rev")["global_rev"]
    second = _post(base, "/refresh", {})["rev"]
    # 内容没变，但强制重渲染后 rev 应当依旧稳定（同样的输入 → 同样的像素）
    assert second == first


# ---------------------------------------------------------------- 错误处理


def test_unknown_route_returns_json_error(live):
    base, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, "/nope")
    assert exc.value.code == 404
    assert "error" in json.loads(exc.value.read().decode("utf-8"))


def test_bad_json_body_is_rejected(live):
    base, _ = live
    req = urllib.request.Request(
        base + "/brief", method="POST", data=b"{ not json",
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
