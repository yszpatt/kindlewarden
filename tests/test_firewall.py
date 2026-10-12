"""防火墙规则检测的测试。

这块踩过一个**潜伏很久**的 bug：``winreg.OpenKey`` 的第一个参数已经是
``HKEY_LOCAL_MACHINE``，路径里**不能再写 ``HKLM\\``** —— 写了就变成找
"HKLM 键下面的 SYSTEM"，而 HKLM 下面并没有叫 HKLM 的子键，直接抛 OSError。

代码 catch 了 OSError 就返回「没有规则」，所以现象是
**``warden firewall`` 永远认为规则不存在**、每次重建一遍。
一直没暴露，是因为"重复建一条同名规则"本身不报错。

这类"异常被吞掉变成正常返回值"的 bug 只能靠**直接验行为**来挡。
"""
from __future__ import annotations

import pytest

from kindlewarden.cli import (_firewall_rule_exists,
                              _firewall_rules_from_registry)


def test_registry_path_does_not_include_the_hive_name():
    """注册表路径里不许出现 ``HKLM\\`` 前缀。

    这是那个 bug 的**直接判据**：``OpenKey`` 已经拿到 hive handle 了，
    路径再带 hive 名就会去开一个不存在的子键，抛 OSError。

    用源码检查而不是运行检查 —— 因为运行时的失败模式是
    "静默返回没有规则"，在没有规则的真机上分不出来。
    """
    import inspect

    src = inspect.getsource(_firewall_rules_from_registry)
    # 取路径字面量那两行
    assert "SYSTEM\\CurrentControlSet" in src, \
        "应该以 SYSTEM\\CurrentControlSet 开头"
    assert "HKLM\\SYSTEM" not in src, (
        "注册表路径带了 HKLM\\ 前缀 —— OpenKey 会去找一个不存在的子键，"
        "然后 OSError 被吞掉、表现为「没有规则」"
    )


@pytest.mark.skipif(__import__("os").name != "nt", reason="只在 Windows 上有意义")
def test_registry_reads_real_rules():
    """在真机上要能读到规则（读不到就说明路径又写错了）。

    判据：如果注册表能打开，就必然有几百条内置规则 ——
    返回空列表基本只可能是"路径错了/被吞异常"。
    """
    rules = _firewall_rules_from_registry()
    assert rules is not None, "注册表读不到（权限或路径问题）"
    assert len(rules) > 10, (
        f"只读到 {len(rules)} 条规则 —— Windows 自带规则就有几百条，"
        f"这个数字说明读取路径有问题（异常被吞了）"
    )
    # 内容看起来得像防火墙规则
    assert any("LPort=" in r or "Dir=" in r for r in rules), \
        "读到的内容不像防火墙规则"


@pytest.mark.skipif(__import__("os").name != "nt", reason="只在 Windows 上有意义")
def test_rule_exists_distinguishes_ports():
    """按端口查要能区分开 —— 换端口之后旧规则还在，不能当作"已放行"。

    实测就是换到 8577 时，8080 的规则还在，而新端口其实没放行。
    只按名字查会得到「有规则」这个**错误结论**。
    """
    # 一个几乎不可能被放行的端口
    assert _firewall_rule_exists(name="kindlewarden", port=59999) is False


@pytest.mark.skipif(__import__("os").name != "nt", reason="只在 Windows 上有意义")
def test_rule_exists_by_name_only():
    """不带端口时只看名字（保持向后兼容）。"""
    got = _firewall_rule_exists()
    assert got in (True, False)


def test_rule_exists_returns_none_off_windows(monkeypatch):
    """非 Windows 返回 None（表示"查不了"），而不是 False。

    这个区分很重要：False 会让 `doctor` 报「没有规则」——
    在 Linux/macOS 上那是误导。
    """
    monkeypatch.setattr("kindlewarden.cli.os.name", "posix")
    assert _firewall_rules_from_registry() is None
    assert _firewall_rule_exists() is None
