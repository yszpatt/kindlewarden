"""共享的 pytest fixture。

主要是「找到 Kindle 和它上面的 KOReader」—— 插件那几组测试都要用，
但**不应该各写一份"找盘符"的逻辑**（那种重复迟早会漂移，
而且正是这个项目里反复踩到的坑：同一个判断散在多处，改一处漏一处）。

Kindle 不在时相关测试会 skip，而不是失败 —— 它们是"有设备时多加一道保险"，
不是常驻门禁。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_LUA = ROOT / "koplugin" / "kindlewarden.koplugin" / "main.lua"


def strip_lua_comments(src: str) -> str:
    """去掉 Lua 注释，只留代码。

    **凡是要检查"源码里有没有某种写法"的地方，都必须先过这一层。**

    踩过三次同一个坑：检查「不许出现 ``scale_factor = 0``」、
    「不许调用 ``getCacheDir``」这类规则时，注释里往往正好在解释
    **为什么不那么写**，于是注释把规则本身判成了违规。

    唯一实现在 ``tools/check_lua.py``，这里转发一下 ——
    测试、命令行工具、验证脚本共用同一份，不各自实现。
    """
    sys.path.insert(0, str(ROOT / "tools"))
    from check_lua import strip_comments
    return strip_comments(src)


@pytest.fixture(scope="session")
def plugin_source() -> str:
    """插件源码原文（含注释）。"""
    return PLUGIN_LUA.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def lua(plugin_source: str) -> str:
    """插件源码，**已剥注释** —— 检查"有没有某种写法"时用这个。"""
    return strip_lua_comments(plugin_source)


def find_kindle_root() -> Path | None:
    """找出 Kindle 用户分区，找不到返回 None。

    按**特征目录**判断（``documents`` + ``system``），不猜盘符 ——
    用户机器上常有网络驱动器、U 盘、读卡器。
    """
    roots: list[str] = []
    try:
        roots = list(os.listdrives())          # Python 3.12+
    except AttributeError:
        roots = [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"]
    for root in roots:
        try:
            entries = set(os.listdir(root))
        except OSError:
            continue
        if {"documents", "system"} <= entries:
            return Path(root)
    return None


@pytest.fixture(scope="session")
def kindle_root() -> Path:
    """Kindle 用户分区根目录。没插就 skip。"""
    root = find_kindle_root()
    if root is None:
        pytest.skip("Kindle 没插着 —— 这条检查需要设备")
    return root


@pytest.fixture(scope="session")
def koreader(kindle_root: Path) -> Path:
    """设备上的 KOReader 目录。没装就 skip。"""
    ko = kindle_root / "koreader"
    if not ko.is_dir():
        pytest.skip("设备上没装 KOReader")
    return ko
