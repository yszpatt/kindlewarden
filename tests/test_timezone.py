"""时区相关的测试。

**这是一个真实故障**：服务部署在容器里（Docker 默认 UTC），
看板上印的时刻比北京时间早 8 小时。

难查的地方在于证据链每一环看着都没问题：
  * 容器时钟**准**（HTTP Date 头与本机只差 1 秒）
  * 时间戳**对**（标准 UTC 秒，`generated_at` 与实际一致）
  * 但"渲染成文字"那一步用了**错的时区**

所以这一组盯住：时区必须**显式可配**、而且配错时要**吵**。
"""
from __future__ import annotations

import time

import pytest

from kindlewarden import render
from kindlewarden.config import Config
from kindlewarden.models import Board, Brief
from kindlewarden.render import format_clock, format_date, render_board

#: 一个固定时刻：2026-10-16 10:10:01 +0800
TS = 1792116601.0


@pytest.fixture(autouse=True)
def _restore_tz():
    """每条测试后恢复成"跟进程本地时区"，避免互相污染。"""
    yield
    render.set_timezone("")


def test_utc_and_offset_render_differently():
    """配了时区就必须**真的**按它渲染。

    UTC 与 +08:00 应当相差整整 8 小时 —— 这正是那个故障的差值。
    """
    render.set_timezone("UTC")
    utc = format_clock(TS)
    render.set_timezone("+08:00")
    plus8 = format_clock(TS)

    assert utc == "02:10", f"UTC 应该是 02:10，实际 {utc}"
    assert plus8 == "10:10", f"+08:00 应该是 10:10，实际 {plus8}"


def test_iana_name_works_when_tzdata_is_available():
    """IANA 名字（``Asia/Shanghai``）要能解析。

    这依赖 `tzdata` 包 —— Windows 上没有系统 tzdata，
    精简的 Linux 镜像也常被裁掉，所以它是**运行时依赖**。
    """
    pytest.importorskip("tzdata", reason="没装 tzdata，IANA 名字测不了")
    got = render.set_timezone("Asia/Shanghai")
    assert got == "Asia/Shanghai", got
    assert format_clock(TS) == "10:10"


def test_bad_timezone_falls_back_but_warns(capsys):
    """名字写错时**不能静默退回本地时区**。

    ⚠️ 这是这类 bug 最坏的形态：配置看着生效了、实际没生效。
    第一版就是把异常吞掉、静默退回 —— 于是用户改了什么都不知道。
    """
    got = render.set_timezone("Not/AZone")
    assert "取不到" in got, got
    err = capsys.readouterr().err
    assert "Not/AZone" in err, f"没打警告：{err!r}"
    assert "tzdata" in err, f"警告里要给出修法：{err!r}"


def test_empty_timezone_keeps_process_local_behaviour():
    """不配时区时退回老行为（进程本地时区）—— 向后兼容。"""
    render.set_timezone("")
    assert render.current_timezone().startswith("进程本地")
    # 本机是 +0800，所以应当渲染成 10:10
    assert format_clock(TS) == "10:10"


def test_render_board_applies_the_configured_timezone():
    """**端到端**：``render_board`` 必须应用 ``cfg.display.timezone``。

    单测 `set_timezone` 只能证明那个函数对；
    真正会出故障的是"渲染时忘了调它" —— 这条盯住那一环。
    """
    board = Board(briefs=[Brief(id="b1", ts=TS, status="ok", title="t")],
                  todos=[], total_briefs=1, total_todos=0)

    cfg = Config()
    cfg.display.timezone = "UTC"
    # 渲染一次，之后读渲染期间生效的时钟
    import io

    from PIL import Image

    img, _lay = render_board(board, cfg, now=TS)
    assert img is not None
    # render_board 里调过 set_timezone("UTC")，所以现在是 UTC
    assert format_clock(TS) == "02:10", (
        "render_board 没有应用配置里的时区 —— 容器里会差 8 小时"
    )

    cfg.display.timezone = "+08:00"
    render_board(board, cfg, now=TS)
    assert format_clock(TS) == "10:10"


def test_format_date_follows_the_timezone_too():
    """日期也要跟着时区走，不能只有时钟。

    跨日的时候（比如 UTC 的 16:00 = 北京次日 00:00）日期会差一天 ——
    这是真实会出现的情况。
    """
    # ⚠️ TS 是 2026-10-16 **10:10 北京** = 02:10 UTC。
    #    要跨日，得推到 UTC 的 16:00 之后 → 北京次日。
    #    第一版加了 6h20m（→ UTC 08:30），两边都还是 16 日，
    #    测试自己算错了日子。
    ts = TS + 14 * 3600          # 02:10 UTC + 14h = 16:10 UTC = 次日 00:10 北京
    render.set_timezone("UTC")
    utc_date = format_date(ts)
    render.set_timezone("+08:00")
    cn_date = format_date(ts)
    assert utc_date != cn_date, (
        f"跨日时日期应该不同：UTC {utc_date} vs 北京 {cn_date}"
    )
    assert "16日" in utc_date, f"UTC 应该还是 16 日：{utc_date}"
    assert "17日" in cn_date, f"北京应该已经是 17 日：{cn_date}"


def test_tzdata_is_a_declared_dependency():
    """``tzdata`` 必须是**声明了的**运行时依赖。

    它不在的话 `Asia/Shanghai` 解析不出来（Windows 没有系统 tzdata，
    slim 镜像也常裁掉），而配置项看着是生效的。
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent
            / "pyproject.toml").read_text(encoding="utf-8")
    assert "tzdata" in text, (
        "pyproject 里没声明 tzdata —— 配 IANA 时区会在某些平台上失效"
    )


def test_docker_sets_a_timezone():
    """镜像/ compose 必须设 TZ。

    容器默认 UTC —— 不设的话看板上的时刻**全是错的**，
    而这是"部署对了、显示错了"的典型，很难往时区上想。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    df = (root / "Dockerfile").read_text(encoding="utf-8")
    dc = (root / "docker-compose.yml").read_text(encoding="utf-8")

    assert "TZ=" in df, "Dockerfile 没设 TZ —— 容器里时间是 UTC"
    assert "TZ" in dc, "docker-compose.yml 没设 TZ"
