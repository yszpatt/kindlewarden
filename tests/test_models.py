"""输入规范化与脱敏的测试。"""

from __future__ import annotations

from kindlewarden import sanitize
from kindlewarden.models import (ALERT_TITLE_DISPLAY_W, STATUSES, SUMMARY_DISPLAY_W,
                                 SUMMARY_STORE_MAX, TITLE_DISPLAY_W, TITLE_STORE_MAX,
                                 Alert, Brief, Todo)
from kindlewarden.textutil import cap_chars, display_width, fit_display, one_line


# ---------------------------------------------------------------- textutil

def test_display_width_counts_cjk_as_one():
    assert display_width("中文") == 2.0
    assert display_width("ab") == 1.0
    assert display_width("中a") == 1.5


def test_fit_display_truncates_with_ellipsis():
    out, cut = fit_display("这是一段很长的中文标题需要被截断", 10.0)
    assert cut is True
    assert out.endswith("…")
    assert display_width(out) <= 10.0


def test_fit_display_leaves_short_text_alone():
    out, cut = fit_display("短", 10.0)
    assert (out, cut) == ("短", False)


def test_one_line_flattens_newlines():
    assert one_line("第一行\n第二行\t第三行") == "第一行 第二行 第三行"
    assert one_line("  多   空格  ") == "多 空格"


# ---------------------------------------------------------------- sanitize

def test_redacts_openai_key():
    out, hits = sanitize.redact("用 sk-abcdefghijklmnopqrstuvwxyz 调接口")
    assert "openai_key" in hits
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in out
    assert sanitize.REDACTED in out


def test_redacts_github_and_aws_tokens():
    out, hits = sanitize.redact("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    assert "github_token" in hits
    out, hits = sanitize.redact("AKIAIOSFODNN7EXAMPLE")
    assert "aws_key" in hits


def test_redacts_url_credentials():
    out, hits = sanitize.redact("postgres://admin:hunter2@db.internal/prod")
    assert "url_credentials" in hits
    assert "hunter2" not in out


def test_key_value_keeps_the_key_readable():
    """只打码值、保留键名，句子仍然可读。"""
    out, hits = sanitize.redact("token: abcdefgh12345")
    assert "key_value" in hits
    assert out.startswith("token")
    assert "abcdefgh12345" not in out


def test_clean_text_is_untouched():
    text = "修复了 auth 模块 token 刷新的竞态条件"
    out, hits = sanitize.redact(text)
    assert out == text
    assert hits == []


def test_redact_deep_walks_nested_structures():
    payload = {"title": "ok", "artifacts": [{"ref": "sk-abcdefghijklmnopqrst"}]}
    hits: list[str] = []
    clean = sanitize.redact_deep(payload, hits)
    assert "openai_key" in hits
    assert "sk-abcdefghijklmnopqrst" not in clean["artifacts"][0]["ref"]


# ---------------------------------------------------------------- 模型规范化

def test_brief_defaults_are_forgiving():
    """必填只有标题 —— 缺字段绝不能让 agent 的调用失败。"""
    brief, hits, cut = Brief.from_request({"title": "完成了迁移"})
    assert brief.title == "完成了迁移"
    assert brief.status == "info"          # 非法/缺失状态降级为 info
    assert brief.summary == ""
    assert hits == [] and cut is False


def test_brief_rejects_unknown_status_gracefully():
    brief, _, _ = Brief.from_request({"title": "x", "status": "炸了"})
    assert brief.status in STATUSES


def test_overlong_text_is_kept_in_full_not_display_truncated():
    """超过显示宽度**不再**在入库时截断。

    回归测试：之前这里会把标题截到 20 字、摘要截到 28 字，
    结果是完整文本在入库时就丢了，管理页的任务详情无从展示。
    现在存储保留全文，只提示「板上会被截断」。
    """
    long_title = "很长的标题" * 10        # 50 字，远超板上放得下的 20 字
    long_summary = "很长的摘要" * 20

    brief, _, cut = Brief.from_request({"title": long_title, "summary": long_summary})
    assert cut is False                   # 没触到存储上限
    assert brief.title == long_title      # 全文保留
    assert brief.summary == long_summary

    warnings = brief.display_warnings()
    assert any("标题" in w for w in warnings)
    assert any("摘要" in w for w in warnings)


def test_short_text_has_no_display_warnings():
    brief, _, _ = Brief.from_request({"title": "短标题", "summary": "短摘要"})
    assert brief.display_warnings() == []
    assert display_width(brief.title) <= TITLE_DISPLAY_W
    assert display_width(brief.summary) <= SUMMARY_DISPLAY_W


def test_storage_cap_prevents_abuse():
    """存储上限仍然存在，只是它守的是「别往日志里灌长文」，不是显示。"""
    brief, _, cut = Brief.from_request({"title": "字" * (TITLE_STORE_MAX + 500)})
    assert cut is True
    assert len(brief.title) <= TITLE_STORE_MAX + 1        # 含省略号

    brief2, _, cut2 = Brief.from_request({"summary": "字" * (SUMMARY_STORE_MAX + 500)})
    assert cut2 is True
    assert len(brief2.summary) <= SUMMARY_STORE_MAX + 1


def test_cap_chars_is_about_length_not_display_width():
    """cap_chars 按字符数封顶，跟显示宽度是两件事。"""
    text, cut = cap_chars("abcdef", 10)
    assert (text, cut) == ("abcdef", False)
    text, cut = cap_chars("abcdefghij", 5)
    assert cut is True and text.endswith("…") and len(text) == 6


def test_brief_flattens_single_line_fields_but_not_summary():
    """单行字段压平换行；**summary 保留段落结构**。

    这里有个层次容易被忽略，而且踩过一次：

    * ``title`` / ``project`` / ``tags`` 是**单行字段** ——
      换行是噪音，必须压掉，否则"看不见的换行会把版面撑坏"
    * ``summary`` 是**正文** —— 换行是**结构**，是作者主动分的段，
      压掉就等于把段落结构在**入库那一刻**毁掉

    当时的 bug 就是 summary 也走 ``one_line()``，于是：

    * 只看看板**发现不了**（看板本来就是单行截断，压不压平一个样）
    * 只有点进详情页才会发现"怎么不分段"，而那时数据已经存坏了

    所以这条测试要同时盯住两侧的行为。
    """
    brief, _, _ = Brief.from_request({
        "title": "第一行\n第二行",
        "project": "proj\nname",
        "summary": "第一句结论。\n\n- 原因一\n- 原因二",
    })

    # 单行字段：换行必须被压掉
    assert "\n" not in brief.title, "标题是单行字段，换行要压平"
    assert "\n" not in brief.project, "项目名是单行字段，换行要压平"

    # 正文：换行必须保留
    assert "\n" in brief.summary, (
        "summary 的换行被压掉了 —— 简报的分段结构会在入库时就丢失，"
        "详情页再怎么渲染也显示不出层次"
    )
    assert "\n\n" in brief.summary, "空行（段落分隔）也要保留"
    assert brief.summary.startswith("第一句结论。")
    assert "- 原因一" in brief.summary


def test_summary_multiline_is_cleaned_not_raw():
    """保留段落 ≠ 原样照收。脏东西还是要清掉。

    否则 agent 随手多敲几个空行，详情页上就会出现大片莫名其妙的空白；
    或者混进 ``\\r`` / 控制字符把版面弄乱。
    """
    brief, _, _ = Brief.from_request({
        "title": "x",
        "summary": "A\r\n\r\n\r\n\r\nB\x00C    D\n\n\n\n",
    })
    s = brief.summary
    assert "\r" not in s, "\\r 要归一化掉"
    assert "\x00" not in s, "控制字符要清掉"
    assert "\n\n\n" not in s, "连续空行要压成一个"
    assert s == s.strip(), "首尾不该有空白"
    assert "A" in s and "B" in s and "C" in s and "D" in s


def test_brief_redacts_secrets_in_any_field():
    brief, hits, _ = Brief.from_request({
        "title": "部署完成",
        "summary": "使用 token: supersecret123 连接",
        "artifacts": [{"label": "PR", "ref": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}],
    })
    assert "key_value" in hits
    assert "github_token" in hits
    assert "supersecret123" not in brief.summary


def test_brief_artifacts_capped_and_accepts_bare_strings():
    brief, _, _ = Brief.from_request({
        "title": "x",
        "artifacts": ["plain-string"] + [{"label": f"n{i}"} for i in range(20)],
    })
    assert len(brief.artifacts) == 5
    assert brief.artifacts[0]["label"] == "plain-string"


def test_todo_and_alert_normalize():
    todo, _, _ = Todo.from_request({"text": "  写   文档  "})
    assert todo.text == "写 文档"

    alert, _, _ = Alert.from_request({"title": "允许执行 rm -rf", "detail": ""})
    assert alert.title
    # 告警标题在板上只有约 15 字宽，超了要给管理页提示
    long_alert, _, _ = Alert.from_request({"title": "允许执行 rm -rf build " * 3})
    assert any("标题" in w for w in long_alert.display_warnings())
    assert display_width("允许执行 rm -rf") <= ALERT_TITLE_DISPLAY_W
