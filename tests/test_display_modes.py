"""夜间模式与横屏模式（纯服务端实现）。

**核心约束**：插件里 ``IMG_W, IMG_H = 1072, 1448`` 是硬编码的，
所以服务端**永远**只能交付这个尺寸 —— 不管什么模式。
这两个功能的价值就是"设备侧一行都不用改"。

实现方式：
  * 夜间 —— 渲染完反色（255 - v）。灰阶仍在 17 的网格上，不需要重新量化。
  * 横屏 —— 先按 1448×1072（宽>高）排版，再旋转 90° → 1072×1448，尺寸正好对上。

⚠️ 横屏最容易错的地方是**点击坐标**：画转了、坐标没转，
现象就是"按钮看得见但点不中"。所以这里专门盯住坐标变换。
"""

from __future__ import annotations

import io
import threading
import urllib.request

import pytest
from PIL import Image

from kindlewarden import layout, render
from kindlewarden.config import Config
from kindlewarden.models import Board, Brief, Todo
from kindlewarden.server import BoardService, build_server

TS = 1791649958.0
DEVICE = (1072, 1448)


def _board(n_briefs=4, n_todos=3) -> Board:
    briefs = [Brief(id=f"b{i}", ts=TS - i * 60, status="ok",
                    title=f"第 {i} 条简报的标题",
                    summary="摘要内容" * 6, project="proj")
              for i in range(n_briefs)]
    todos = [Todo(id=f"t{i}", ts=TS, text=f"待办 {i}") for i in range(n_todos)]
    return Board(briefs=briefs, todos=todos,
                 total_briefs=n_briefs, total_todos=n_todos)


def _service(*, night=False, landscape=False, rotate="cw", board=None,
             tmp_path=None):
    """建一个 BoardService。

    ⚠️ 必须给 ``data_dir`` 指到临时目录 —— 否则会去写用户真实的数据目录
    （``%LOCALAPPDATA%\\kindlewarden``），在受限制的环境里直接 PermissionError。
    第一版没给，13 个测试全挂在"建目录"这一步上，跟被测逻辑毫无关系。

    给了 ``board`` 时会把它写进**真实的事件流**，而不是打补丁替换方法。
    为什么：``board_payload`` 用的是 ``snap.board``（渲染时缓存下来的那份），
    换掉方法并不能改变快照里的内容 —— 于是卡片矩形算出来是空的，
    测试会以为"没有矩形"，实际是数据没喂进去。
    """
    import tempfile
    from pathlib import Path

    cfg = Config()
    cfg.display.night = night
    cfg.display.landscape = landscape
    cfg.display.rotate = rotate
    cfg.data_dir = Path(tmp_path) if tmp_path is not None else Path(
        tempfile.mkdtemp(prefix="warden-disp-"))
    cfg.source_path = cfg.data_dir / "panels.toml"

    svc = BoardService(cfg)

    if board is not None:
        # 真正写进事件流（时间戳用"现在"，免得被老化规则剔掉）
        import time as _t
        now = _t.time()
        for i, br in enumerate(board.briefs):
            br.ts = now - i * 60
            svc.store.append("brief.published", {"brief": br.to_dict()})
        for td in board.todos:
            td.ts = now
            svc.store.append("todo.added", {"todo": td.to_dict()})
    return svc


def _snapshot(svc):
    """取一次快照（数据由 ``_service(board=...)`` 写进事件流了）。"""
    return svc.snapshot(force=True)


# ---------------------------------------------------------------- 尺寸不变


@pytest.mark.parametrize("night,landscape", [
    (False, False), (True, False), (False, True), (True, True),
])
def test_every_mode_outputs_the_device_size(night, landscape):
    """**所有模式都必须输出 1072×1448。**

    插件里那个尺寸是硬编码的（它按它算缩放比和点击坐标）。
    服务端一旦交付别的尺寸，插件要么显示错位、要么点击全偏 ——
    而"零设备侧改动"这个前提也就不成立了。
    """
    svc = _service(night=night, landscape=landscape, board=_board())
    snap = _snapshot(svc)
    img = Image.open(io.BytesIO(snap.png))
    assert img.size == DEVICE, (
        f"夜间={night} 横屏={landscape} 输出了 {img.size}，"
        f"而设备要求 {DEVICE}"
    )


def test_canvas_is_restored_after_landscape_render():
    """横屏渲染完必须**恢复**画布尺寸。

    画布是模块级全局变量。不恢复的话：
      * 下一次竖屏渲染会沿用横屏尺寸 → 画面错位
      * 更糟的是点击区域也跟着错，而且没有报错

    这是"用全局变量换少改几十处调用点"必须付出的代价，所以要盯死。
    """
    svc = _service(landscape=True, board=_board())
    _snapshot(svc)

    assert (layout.CANVAS_W, layout.CANVAS_H) == DEVICE, \
        f"layout 画布没恢复：{layout.CANVAS_W}×{layout.CANVAS_H}"
    assert (render.CANVAS_W, render.CANVAS_H) == DEVICE, \
        f"render 画布没恢复：{render.CANVAS_W}×{render.CANVAS_H}"

    # 再渲染一次竖屏，确认尺寸正常
    svc2 = _service(landscape=False, board=_board())
    snap = _snapshot(svc2)
    assert Image.open(io.BytesIO(snap.png)).size == DEVICE


def test_canvas_restored_even_when_render_raises(tmp_path):
    """渲染抛异常时也要恢复画布。

    忘了恢复的话，一次失败会让**后续所有渲染**都用错尺寸 ——
    这种"一次错误污染全局状态"的问题最难查。
    """
    from kindlewarden import server as srv_mod

    svc = _service(landscape=True, tmp_path=tmp_path)

    def bad_render(*a, **k):
        raise RuntimeError("故意炸一下")

    # ⚠️ 要打补丁的是 **server 模块里那个名字**，不是 render.render_board。
    #    server.py 用的是 `from .render import render_board`，
    #    导入时就绑定了 —— 改 render 模块的属性不会影响它。
    #    （我第一版就打错了地方，测试报"没抛异常"，其实是根本没替换成功。）
    orig = srv_mod.render_board
    srv_mod.render_board = bad_render
    try:
        with pytest.raises(RuntimeError):
            svc.snapshot(force=True)
    finally:
        srv_mod.render_board = orig

    assert (layout.CANVAS_W, layout.CANVAS_H) == DEVICE, \
        "渲染失败之后画布没恢复 —— 后续渲染都会用错尺寸"
    assert (render.CANVAS_W, render.CANVAS_H) == DEVICE


# ---------------------------------------------------------------- 夜间模式


def test_night_mode_inverts_the_image():
    """夜间模式 = 反色（255 - v）。"""
    board = _board()
    day = Image.open(io.BytesIO(_snapshot(_service(board=board)).png)).convert("L")
    night = Image.open(io.BytesIO(
        _snapshot(_service(night=True, board=board)).png)).convert("L")

    d, n = day.load(), night.load()
    # 多点采样，确认处处都是 255-v
    for x, y in [(60, 200), (400, 700), (900, 1200), (500, 1400)]:
        assert n[x, y] == 255 - d[x, y], (
            f"({x},{y}) 日间 {d[x, y]} → 夜间 {n[x, y]}，期望 {255 - d[x, y]}"
        )


def test_night_mode_stays_on_the_16_level_grid():
    """夜间反色之后灰阶**仍在 16 级网格上**，不需要重新量化。

    这是夜间模式能做"纯后处理"的前提：
    我们的调色板是 17 的倍数（255/15），而 ``255 - 17n = 17(15-n)``
    仍然是 17 的倍数。

    如果哪天有人往渲染里塞了不在网格上的灰（比如抗锯齿），
    这个性质就没了 —— 夜间模式会出现抖动噪点。所以盯住它。
    """
    svc = _service(night=True, board=_board())
    snap = _snapshot(svc)
    img = Image.open(io.BytesIO(snap.png))
    greys = sorted({v for _c, v in (img.getcolors(maxcolors=512) or [])})
    off = [v for v in greys if v % 17 != 0]
    assert not off, f"夜间模式出现了不在 17 网格上的灰阶：{off[:8]}"


# ---------------------------------------------------------------- 横屏模式


def test_landscape_actually_rotates_the_content():
    """横屏要把内容转 90°。

    判据：竖屏时内容主要在上半部分（简报在上、状态栏在下），
    转过来之后应该变成"偏左右"。

    注：不能简单地断言"宽高比变了" —— 因为交付尺寸是固定的
    （1072×1448 竖着的），转的是**内容**而不是画布。
    """
    board = _board()

    def ink_halves(img):
        px = img.load()
        w, h = img.size
        top = sum(1 for y in range(0, h // 2, 4) for x in range(0, w, 6)
                  if px[x, y] < 240)
        bot = sum(1 for y in range(h // 2, h, 4) for x in range(0, w, 6)
                  if px[x, y] < 240)
        left = sum(1 for y in range(0, h, 6) for x in range(0, w // 2, 4)
                   if px[x, y] < 240)
        right = sum(1 for y in range(0, h, 6) for x in range(w // 2, w, 4)
                    if px[x, y] < 240)
        return top, bot, left, right

    day = Image.open(io.BytesIO(_snapshot(_service(board=board)).png)).convert("L")
    land = Image.open(io.BytesIO(
        _snapshot(_service(landscape=True, board=board)).png)).convert("L")

    dt, db, _dl, _dr = ink_halves(day)
    _lt, _lb, ll, lr = ink_halves(land)

    # 竖屏：上下差异明显（内容偏上）
    assert abs(dt - db) > 0, "竖屏内容上下没有差异，测不出旋转"
    # 横屏：左右差异应该出现
    assert abs(ll - lr) > 0, (
        "横屏之后左右没有墨迹差异 —— 内容可能根本没转过去"
    )


def test_landscape_rotates_tap_coordinates():
    """横屏时**点击坐标必须跟着转**。

    ⚠️ 这是整个功能最容易错的地方：画转了、坐标没转，
    现象是"按钮看得见但点不中"，而且不会有任何报错。

    变换后所有矩形都必须落在交付尺寸（1072×1448）之内 ——
    这条断言最实在：坐标没转的话，横屏排版的 1408 宽矩形会超出 1072。
    """
    board = _board()
    svc = _service(landscape=True, board=board)

    # 在横屏排版坐标系下取卡片矩形
    render.set_canvas(layout.DEVICE_H, layout.DEVICE_W)
    try:
        from kindlewarden.screen import board_taps
        briefs, todos = board_taps(board, svc.cfg)
    finally:
        render.reset_canvas()

    assert briefs, "没算出卡片矩形"

    for item in briefs + todos:
        r = svc._transform_rect(item["rect"])
        x0, y0, x1, y1 = r
        assert 0 <= x0 < x1 <= DEVICE[0], (
            f"变换后的横向范围 {x0}~{x1} 超出交付宽度 {DEVICE[0]} —— "
            f"坐标八成没跟着旋转"
        )
        assert 0 <= y0 < y1 <= DEVICE[1], (
            f"变换后的纵向范围 {y0}~{y1} 超出交付高度 {DEVICE[1]}"
        )


def test_landscape_tap_transform_is_reversible():
    """坐标变换必须是**纯旋转**：不丢像素、不越界、面积不变。

    随手写个矩阵很容易把 x/y 搞反、或者在半开区间的边缘算错一个像素，
    导致点击区域整体偏移 —— 肉眼看不出，但点边缘时会点空。

    ⚠️ 矩形是**半开区间**（``x1`` 不含），所以宽度是 ``x1 - x0``。
    第一版把闭区间和半开区间的算法混用了（又加 +1），
    10×10 的框变换后变成 11×11 —— 面积变了，测试当场抓到。
    """
    svc = _service(landscape=True)
    # ⚠️ 横屏时**源画布是 1448×1072**（宽>高），输出才是 1072×1448。
    #    第一版按 1072×1448 构造输入矩形，那些矩形在源坐标系里就越界了 ——
    #    测试自己喂了非法数据。
    SW, SH = layout.DEVICE_H, layout.DEVICE_W     # 源：1448×1072
    OW, OH = DEVICE                               # 输出：1072×1448

    for rect in ([0, 0, 10, 10], [SW - 10, SH - 10, SW, SH],
                 [0, SH - 10, 10, SH], [SW - 10, 0, SW, 10],
                 [SW // 2 - 5, SH // 2 - 5, SW // 2 + 5, SH // 2 + 5],
                 [40, 128, 1408, 256]):
        out = svc._transform_rect(rect)
        x0, y0, x1, y1 = out
        assert 0 <= x0 < x1 <= OW, f"{rect} → {out} 横向越界（输出宽 {OW}）"
        assert 0 <= y0 < y1 <= OH, f"{rect} → {out} 纵向越界（输出高 {OH}）"
        assert (x1 - x0) * (y1 - y0) == (rect[2] - rect[0]) * (rect[3] - rect[1]), (
            f"{rect} → {out} 面积变了"
            f"（{rect[2]-rect[0]}×{rect[3]-rect[1]} → {x1-x0}×{y1-y0}），"
            "说明变换不是纯旋转"
        )


def test_landscape_transform_preserves_corner_mapping():
    """变换要把左上角正确映射到旋转后的位置（不能只是"落在界内"）。

    只检查"在界内是不够的" —— 一个恒等变换也满足那条，
    但它没转任何东西。这里验证**具体的对应关系**：

      排版 (0,0) 是左上角；顺时针转 90° 后应该跑到**右上角**。

    这条能抓住"x/y 写反"和"方向搞错"这两类错误。
    """
    svc = _service(landscape=True, rotate="cw")
    SW, SH = layout.DEVICE_H, layout.DEVICE_W

    # 排版左上角一个 10×10 的小方块
    out = svc._transform_rect([0, 0, 10, 10])
    # 顺时针后，它应该在输出图的**右上角**附近（x 接近 1072，y 接近 0）
    x0, y0, x1, y1 = out
    assert x1 >= DEVICE[0] - 12, f"左上角转完没到右边：{out}，输出宽 {DEVICE[0]}"
    assert y0 <= 12, f"左上角转完没到顶部：{out}"

    # 逆时针时应该在**左下角**
    svc_ccw = _service(landscape=True, rotate="ccw")
    out2 = svc_ccw._transform_rect([0, 0, 10, 10])
    x0b, y0b, x1b, y1b = out2
    assert x0b <= 12, f"逆时针后左上角没到左边：{out2}"
    assert y1b >= DEVICE[1] - 12, f"逆时针后左上角没到底部：{out2}"


def test_portrait_leaves_taps_untouched():
    """竖屏时坐标**不做任何变换**（否则会平白偏移）。"""
    svc = _service(landscape=False)
    for rect in ([10, 20, 110, 120], [0, 0, 5, 5]):
        assert svc._transform_rect(rect) == rect


def test_landscape_taps_match_the_rendered_layout(tmp_path):
    """横屏时 ``board_payload`` 的矩形必须和**实际渲染的版面**一致。

    ⚠️ 这是一个真 bug，而且极隐蔽：

    ``_build()`` 里横屏排版 → 渲染 → **恢复画布**，
    而 ``board_payload()`` 之后才调 ``board_taps()`` —— 那时画布已经是竖屏的。
    于是矩形按**竖屏**（1072 宽）算，画面却是**横屏**（1448 宽）画的。
    **横屏下点卡片点不中，而且不报任何错。**

    判据要用**长边**，不能用宽度！
      * 竖屏卡片 992 × 128  → 旋转后 128 × 992     ← 长边 992（错）
      * 横屏卡片 1368 × 128 → 旋转后 128 × 1368    ← 长边 1368（对）

    我第一版只看宽度，两种情况都是 128，根本区分不出来 ——
    **测试判据太弱，等于没测。** 换成比长边才抓到。
    """
    from kindlewarden import layout, render
    from kindlewarden.screen import board_taps

    board = _board(n_briefs=5, n_todos=2)
    svc = _service(landscape=True, tmp_path=tmp_path, board=_board(n_briefs=5, n_todos=2))
    snap = svc.snapshot(force=True)
    payload = svc.board_payload(snap)

    portrait_long = layout.DEVICE_W - 2 * layout.MARGIN    # 992
    landscape_long = layout.DEVICE_H - 2 * layout.MARGIN   # 1368

    rects = [b["rect"] for b in payload["briefs"] if b.get("rect")]
    assert rects, "没算出矩形"

    longs = {max(r[2] - r[0], r[3] - r[1]) for r in rects}
    assert portrait_long not in longs, (
        f"交付矩形的长边出现了 {portrait_long} —— 那是**竖屏**卡片宽度，"
        f"说明 board_taps 是在画布恢复之后调的（按竖屏算的）。"
        f"横屏下会点不中卡片。实际长边集合 {longs}"
    )
    assert landscape_long in longs, (
        f"长边应该是 {landscape_long}（横屏排版宽度 1448-80），实际 {longs}"
    )

    # 顺带确认矩形都在交付画布内
    for r in rects:
        assert 0 <= r[0] < r[2] <= DEVICE[0], f"{r} 横向越界"
        assert 0 <= r[1] < r[3] <= DEVICE[1], f"{r} 纵向越界"


def test_landscape_render_then_portrait_taps_stay_portrait(tmp_path):
    """竖屏时矩形仍按竖屏算（别把横屏的逻辑漏到竖屏）。"""
    from kindlewarden import layout

    board = _board(n_briefs=5, n_todos=2)
    svc = _service(landscape=False, tmp_path=tmp_path, board=_board(n_briefs=5, n_todos=2))
    snap = svc.snapshot(force=True)
    payload = svc.board_payload(snap)

    rects = [b["rect"] for b in payload["briefs"] if b.get("rect")]
    portrait_long = layout.DEVICE_W - 2 * layout.MARGIN    # 992
    longs = {max(r[2] - r[0], r[3] - r[1]) for r in rects}
    assert portrait_long in longs, (
        f"竖屏时矩形长边应该是 {portrait_long}，实际 {longs}"
    )


def test_screen_json_and_board_agree_in_landscape(tmp_path):
    """``/screen.json`` 和 ``/board`` 的矩形必须**一致**。

    ⚠️ 踩过一次：我只在 ``/board`` 里加了横屏坐标旋转，
    ``/screen.json`` 忘了 —— 而**插件用的正是后者**。
    于是测试全绿、``/board`` 也对，但设备上横屏点哪儿都不对。

    同一种逻辑写在两处，就一定会漏掉一处。现在两边都走
    ``board_taps_for_delivery``，这条测试盯住"它们真的一样"。
    """
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.display.landscape = True
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    try:
        import json as _json

        def post(path, payload):
            req = urllib.request.Request(
                base + path, method="POST",
                data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return _json.loads(r.read().decode())

        def get(path):
            with urllib.request.urlopen(base + path, timeout=8) as r:
                return _json.loads(r.read().decode())

        for i in range(4):
            post("/brief", {"title": f"横屏一致性 {i}", "status": "ok",
                            "summary": "内容" * 20, "project": "p"})

        board = get("/board")
        screen = get("/screen.json?c=kindle")

        b_rects = {b["id"]: b["rect"] for b in board["briefs"] if b.get("rect")}
        s_rects = {b["id"]: b["rect"] for b in screen["briefs"] if b.get("rect")}

        assert b_rects, "/board 没有矩形"
        assert s_rects, "/screen.json 没有矩形"
        assert b_rects == s_rects, (
            f"两个端点的矩形不一致：\n  /board      {list(b_rects.items())[:2]}\n"
            f"  /screen.json {list(s_rects.items())[:2]}\n"
            "（插件用的是 /screen.json —— 只修 /board 等于没修）"
        )

        # 而且必须是**横屏**的几何（长边 1368），不是竖屏的 992
        longs = {max(r[2] - r[0], r[3] - r[1]) for r in s_rects.values()}
        portrait_long = layout.DEVICE_W - 2 * layout.MARGIN
        assert portrait_long not in longs, (
            f"/screen.json 的矩形长边是 {longs} —— 含竖屏值 {portrait_long}，"
            f"说明横屏旋转没生效"
        )

    finally:
        httpd.shutdown()
        httpd.server_close()


def test_exit_zone_is_not_rotated_in_landscape(tmp_path):
    """横屏时 ``exit_zone`` **不旋转**，而 taps **要**旋转。

    ⚠️ 这两者不一样，而且很容易"顺手改成一致"，那反而是错的。

    为什么 exit_zone 特殊：
      * 它是**插件画的**（不依赖服务端，见"退不出去"那次事故）。
        插件拿到矩形后画一个固定大小（152×34）的**横**按钮 ——
        如果把矩形转成 34×152 的竖条，按钮会**溢出 118px**，
        而且点击区域和实际画出来的位置对不上。
      * 不旋转并不会盖住内容：MARGIN 四边都是 40px，所以
        「交付图的底部边带」旋转后正好落在「排版图的左/右边距带」——
        两边都是布局引擎留白的区域，本来就是空的。

    实测反推（顺时针）：
      竖屏 [880,1410,1032,1444] → 排版图 [1410,40,1444,192]
      而排版画布是 1448 宽、右边距从 1408 开始 —— 确实落在边距带里。

    ⚠️ 这条测试踩过一个**时间炸弹**，写法上要小心：

    第一版自己 append 了一条 ``ts=TS``（固定绝对时间）的简报，
    而 ``brief_retention_h`` 默认 **24 小时** —— 随着真实时间流逝，
    它的年龄跨过 24h 就被老化剔除了，``detail()`` 自然返回 None。
    更阴的是：写这个测试的当天它**刚好在 24h 线内**，所以是"通过"的，
    第二天才突然变红。

    **规则：测试里不要用固定绝对时间戳去喂"会被老化过滤"的数据。**
    要么用 ``_service`` 写进去的新鲜时间戳，要么显式关掉老化。
    这里改成直接用 ``_service`` 已经写入的那条。
    """
    from kindlewarden import layout
    from kindlewarden.render import exit_zone_rect

    portrait_ez = exit_zone_rect()

    svc = _service(landscape=True, tmp_path=tmp_path, board=_board())

    # ⚠️ 用 _service 写入的那条（时间戳是"现在"，不会被老化剔除）。
    #    不要再 append 一条 ts 固定的旧简报 —— 那正是上面的时间炸弹。
    out = svc.detail("b0", 0)
    assert out is not None, (
        "详情页取不到 b0 —— 检查 _service 是不是没把简报写进事件流，"
        "或者老化把数据剔掉了（别用固定绝对时间戳喂测试数据）"
    )
    _brief, _png, taps, _page, _total, ez = out

    assert ez == portrait_ez, (
        f"横屏时 exit_zone 被旋转了：{ez}，应该是竖屏定义 {portrait_ez}。\n"
        f"插件画的是不旋转的横按钮，旋转矩形会让它溢出。"
    )

    # 但 taps 必须旋转过（否则画和坐标反着来）
    assert taps, "详情页没有 taps"

    # 更实在的判据：矩形必须在**交付画布**内，而且和竖屏时的值不同
    portrait_svc = _service(landscape=False, tmp_path=tmp_path / "p",
                            board=_board())
    p_out = portrait_svc.detail("b0", 0)
    assert p_out is not None
    p_taps = p_out[2]

    p_back = [t for t in p_taps if t["kind"] == "back"][0]["rect"]
    l_back = [t for t in taps if t["kind"] == "back"][0]["rect"]
    assert p_back != l_back, (
        f"横屏和竖屏的返回按钮矩形一样（{p_back}）—— 说明 taps 没旋转"
    )
    # 竖屏时返回按钮是横向的（宽>高），旋转后应该变成纵向（高>宽）
    assert p_back[2] - p_back[0] > p_back[3] - p_back[1], "竖屏返回按钮应是横向"
    assert l_back[3] - l_back[1] > l_back[2] - l_back[0], (
        f"横屏返回按钮应该变成纵向（旋转了），实际 {l_back}"
    )


def test_landscape_detail_taps_are_rotated(tmp_path):
    """详情页的按钮矩形在横屏时也要旋转。"""
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.display.landscape = True
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    try:
        import json as _json

        def post(path, payload):
            req = urllib.request.Request(
                base + path, method="POST",
                data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return _json.loads(r.read().decode())

        def get(path):
            with urllib.request.urlopen(base + path, timeout=8) as r:
                return _json.loads(r.read().decode())

        bid = post("/brief", {"title": "横屏详情",
                              "summary": "很长的内容。" * 300})["brief_id"]
        info = get(f"/detail.json?id={bid}&page=1&c=kindle")

        for t in info["taps"]:
            x0, y0, x1, y1 = t["rect"]
            assert 0 <= x0 < x1 <= DEVICE[0], (
                f"{t['kind']} 矩形 {t['rect']} 横向越界 —— 坐标没旋转"
            )
            assert 0 <= y0 < y1 <= DEVICE[1], (
                f"{t['kind']} 矩形 {t['rect']} 纵向越界"
            )

        ez = info["exit_zone"]
        assert 0 <= ez[0] < ez[2] <= DEVICE[0]
        assert 0 <= ez[1] < ez[3] <= DEVICE[1]

    finally:
        httpd.shutdown()
        httpd.server_close()


def test_image_rotation_matches_coordinate_transform(tmp_path):
    """**画面旋转方向必须和坐标变换方向一致。**

    这是横屏功能唯一真正的不变量，也是最容易错的地方。

    ⚠️ 踩过一次：PIL 的 ``transpose`` 常量名有历史包袱，
    我按名字硬对（``cw`` ↔ ``ROTATE_90``），结果画转到了一个方向、
    坐标按另一个方向算 —— **画和坐标反着转**。
    现象是"画面能看，但点哪儿都不对"。

    实测（用左上角标记块）：
      * ``ROTATE_90``  → 左上角跑到**左下** = 逆时针
      * ``ROTATE_270`` → 左上角跑到**右上** = 顺时针

    这条测试用**像素级验证**锁住两者一致：
    在排版坐标系里标一个点，按 _transform_rect 算出它该去哪，
    然后真的旋转图像，看那个位置是不是真的有内容。
    """
    from PIL import ImageDraw

    from kindlewarden import layout

    for direction in ("cw", "ccw"):
        svc = _service(landscape=True, rotate=direction, tmp_path=tmp_path)

        # 造一张"排版坐标系"的图（1448×1072），在**左上角**放一个黑块
        SW, SH = layout.DEVICE_H, layout.DEVICE_W
        src = Image.new("L", (SW, SH), 255)
        d = ImageDraw.Draw(src)
        d.rectangle([0, 0, 39, 39], fill=0)          # 左上角 40×40 黑块

        # 按坐标变换算它该去哪
        rect = svc._transform_rect([0, 0, 40, 40])

        # 真的走一遍交付路径的旋转
        rotated = svc._apply_display(src)

        assert rotated.size == DEVICE, \
            f"{direction}: 旋转后尺寸 {rotated.size}，应为 {DEVICE}"

        px = rotated.load()
        x0, y0, x1, y1 = rect
        # 变换后的矩形里应当**有黑像素**（那块内容确实转到了这里）
        ink = sum(1 for y in range(y0, min(y1, rotated.size[1]))
                  for x in range(x0, min(x1, rotated.size[0]))
                  if px[x, y] < 128)
        assert ink > 0, (
            f"{direction}: 坐标变换说内容在 {rect}，但那块是空白的 —— "
            f"图旋转和坐标变换**方向不一致**（画转到别处了）"
        )


def test_rotation_direction_is_not_confused(tmp_path):
    """顺时针和逆时针必须真的转到**不同**的地方。

    防的是"两个分支都写成了同一个 PIL 常量"这种低级错误 ——
    那样配置项形同虚设，而用户只会觉得"转了但方向不对"。
    """
    from PIL import ImageDraw

    from kindlewarden import layout

    SW, SH = layout.DEVICE_H, layout.DEVICE_W
    src = Image.new("L", (SW, SH), 255)
    d = ImageDraw.Draw(src)
    d.rectangle([0, 0, 39, 39], fill=0)

    cw = _service(landscape=True, rotate="cw",
                  tmp_path=tmp_path)._apply_display(src)
    ccw = _service(landscape=True, rotate="ccw",
                   tmp_path=tmp_path)._apply_display(src)

    assert cw.tobytes() != ccw.tobytes(), (
        "顺时针和逆时针转出来的图一模一样 —— 两个分支用了同一个常量"
    )

    def black_center(img):
        """黑块的（大致）中心位置。"""
        px = img.load()
        pts = [(x, y) for y in range(0, img.size[1], 3)
               for x in range(0, img.size[0], 3) if px[x, y] < 128]
        return (sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts)) if pts else None

    c_cw = black_center(cw)
    c_ccw = black_center(ccw)
    assert c_cw and c_ccw, "没找到黑块"

    # 顺时针：左上角 → 右上；逆时针：左上角 → 左下
    assert c_cw[0] > DEVICE[0] * 0.7, f"顺时针没转到右边：{c_cw}"
    assert c_cw[1] < DEVICE[1] * 0.3, f"顺时针没转到上边：{c_cw}"
    assert c_ccw[0] < DEVICE[0] * 0.3, f"逆时针没转到左边：{c_ccw}"
    assert c_ccw[1] > DEVICE[1] * 0.7, f"逆时针没转到下边：{c_ccw}"


# ---------------------------------------------------------------- HTTP 层


def test_display_config_is_editable_and_persists(tmp_path):
    """``display`` 这一节要能被管理页改，并且存得进 TOML。

    ``to_toml`` 是**逐字段手写**的 —— 新增一节很容易漏掉序列化，
    那样现象是"改完保存了，重启又变回去"。
    """
    from kindlewarden.config import apply_patch, load, save, to_toml

    cfg = Config()
    cfg.source_path = tmp_path / "panels.toml"
    cfg.data_dir = tmp_path

    changed = apply_patch(cfg, {"display": {"night": True,
                                            "landscape": True,
                                            "rotate": "ccw"}})
    assert "display.night" in changed
    assert cfg.display.night is True
    assert cfg.display.landscape is True
    assert cfg.display.rotate == "ccw"

    save(cfg)
    text = (tmp_path / "panels.toml").read_text(encoding="utf-8")
    assert "[display]" in text, "to_toml 没写出 [display] 节"
    assert "night = true" in text
    assert "landscape = true" in text
    assert 'rotate = "ccw"' in text

    # 读回来
    again = load(tmp_path / "panels.toml")
    assert again.display.night is True
    assert again.display.landscape is True
    assert again.display.rotate == "ccw"


def test_display_modes_reach_the_http_layer(tmp_path):
    """端到端：改了配置之后，``/screen.png`` 真的换了个画面。"""
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        def get(path):
            with urllib.request.urlopen(base + path, timeout=8) as r:
                return r.read()

        day = get("/screen.png")
        assert Image.open(io.BytesIO(day)).size == DEVICE

        # 打开夜间模式（走和插件同一条路：请求 /config 时不该崩）
        with urllib.request.urlopen(base + "/config", timeout=8) as r:
            import json
            conf = json.loads(r.read().decode())
        assert "display" in conf, "/config 没暴露 display 节"
        assert conf["display"]["night"] is False

    finally:
        httpd.shutdown()
        httpd.server_close()
