"""字体发现的**兜底逻辑**测试。

为什么单独测这个：兜底那条路**只在 Linux/容器里才会走到**
（Windows 上有 msyh，永远命中快速路径）。也就是说，
它在开发机上永远不会被执行 —— 而这正是容器部署时唯一的字体来源。

「只有生产环境才跑的分支」是最容易长期带着 bug 的那类代码，
所以这里把字体目录指向**临时目录**，人为造出各种布局来验证。
"""
from __future__ import annotations

import pytest

from kindlewarden.render import FontBook


def _touch(path, content=b"fake-font"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def fake_fonts(tmp_path, monkeypatch):
    """把 FontBook 的搜索范围限制到一个空临时目录，并清掉快速路径。

    ``CANDIDATES`` 也一并清空 —— 否则会命中开发机上的真实字体，
    测的就不是兜底逻辑了。
    """
    root = tmp_path / "fonts"
    root.mkdir()
    monkeypatch.setattr(FontBook, "FONT_DIRS", [str(root)])
    monkeypatch.setattr(FontBook, "CANDIDATES", [])
    return root


def test_fallback_finds_noto_cjk_in_a_debian_layout(fake_fonts):
    """Debian 的 fonts-noto-cjk 布局能认出来。

    这是容器里的**正常情况**：字体装在
    /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
    """
    _touch(fake_fonts / "opentype/noto/NotoSansCJK-Regular.ttc")
    _touch(fake_fonts / "opentype/noto/NotoSansCJK-Bold.ttc")

    reg, bold = FontBook._discover()
    assert reg.endswith("NotoSansCJK-Regular.ttc")
    assert bold.endswith("NotoSansCJK-Bold.ttc")


def test_fallback_finds_noto_cjk_in_an_alternate_layout(fake_fonts):
    """换个发行版把字体放在别的目录，也要能找到。

    这正是"不写死路径"的意义：写死一条路径的话，
    换个基础镜像就找不到字体，而且要到**运行时渲染**才报错。
    """
    _touch(fake_fonts / "noto-cjk/NotoSansCJK-Regular.ttc")
    _touch(fake_fonts / "noto-cjk/NotoSansCJK-Bold.ttc")

    reg, bold = FontBook._discover()
    assert "NotoSansCJK-Regular" in reg
    assert "NotoSansCJK-Bold" in bold


def test_fallback_handles_language_subset_names(fake_fonts):
    """语言子集的命名（NotoSansCJKsc-*）也要认。

    Noto CJK 有一堆变体名：NotoSansCJKsc / -jp / -kr / -tc，
    而我们的正则是 ``NotoSansCJK.*-Regular`` —— 这里确认它真的匹配得上。
    """
    _touch(fake_fonts / "opentype/noto/NotoSansCJKsc-Regular.otf")
    _touch(fake_fonts / "opentype/noto/NotoSansCJKsc-Bold.otf")

    reg, bold = FontBook._discover()
    assert reg.endswith(".otf") and "Regular" in reg
    assert bold.endswith(".otf") and "Bold" in bold


def test_fallback_prefers_noto_over_wqy(fake_fonts):
    """两款都在时，选 Noto（优先级由 FONT_PATTERNS 的顺序决定）。"""
    _touch(fake_fonts / "truetype/wqy/wqy-zenhei.ttc")
    _touch(fake_fonts / "opentype/noto/NotoSansCJK-Regular.ttc")
    _touch(fake_fonts / "opentype/noto/NotoSansCJK-Bold.ttc")

    reg, bold = FontBook._discover()
    assert "NotoSansCJK" in reg, f"应该优先选 Noto，实际选了 {reg}"


def test_only_regular_font_falls_back_for_bold(fake_fonts):
    """只有常规体时，粗体**退回常规体**而不是报错。

    wqy-zenhei 这类字体只有一个字重。渲染器会在标题上请求 bold，
    这时宁可字重不对，也不能让整个服务渲染失败。
    """
    _touch(fake_fonts / "truetype/wqy/wqy-zenhei.ttc")

    reg, bold = FontBook._discover()
    assert reg.endswith("wqy-zenhei.ttc")
    assert bold == reg, "粗体应该退回常规体"


def test_no_font_at_all_raises_with_actionable_message(fake_fonts):
    """一个字体都没有时，报错要说清楚**怎么办**。

    容器里没交互终端，这条报错就是用户唯一的线索，
    所以它必须包含"装哪个包"。
    """
    with pytest.raises(RuntimeError) as exc:
        FontBook._discover()

    msg = str(exc.value)
    assert "fonts-noto-cjk" in msg, f"报错里没给出安装命令：{msg}"
    assert str(fake_fonts) in msg, f"报错里没列出搜过的目录：{msg}"


def test_discover_skips_directories(fake_fonts):
    """同名**目录**不该被当成字体文件。

    rglob 会把目录也列出来，不过滤的话会拿到一个目录路径，
    然后 truetype() 报一个很难懂的错误。
    """
    (fake_fonts / "opentype/noto/NotoSansCJK-Regular.ttc").mkdir(parents=True)
    _touch(fake_fonts / "opentype/noto/NotoSansCJK-Bold.ttc")
    # 常规体只以目录形式存在 → 应该认不出可用常规体
    with pytest.raises(RuntimeError):
        FontBook._discover()


def test_real_fontbook_still_loads_on_this_machine():
    """本机（开发环境）上真实字体仍然可用 —— 别把正常路径改坏了。"""
    fb = FontBook()
    face = fb.get(32, True)
    assert face is not None
    assert fb.regular and fb.bold
