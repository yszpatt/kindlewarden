"""服务启动提示的测试。

这段文案里藏着一类真实故障：服务只绑了 127.0.0.1 时，**本机测试一切正常**，
但局域网上的设备（包括 Kindle）全部连不上，而现象只是「页面无法显示」，
很难联想到是监听地址的问题。所以必须显式警告。
"""

from __future__ import annotations

from kindlewarden.config import Config
from kindlewarden.server import is_likely_virtual, lan_addresses, startup_banner


def test_banner_warns_when_bound_to_loopback():
    cfg = Config()
    lines = startup_banner(host="127.0.0.1", port=8080, lan=["192.168.1.50"], cfg=cfg)
    text = "\n".join(lines)
    assert "只监听 127.0.0.1" in text
    assert "--host 0.0.0.0" in text
    # 关键是别把可用的局域网地址漏出去让人误以为能连
    assert "192.168.1.50" not in text


def test_banner_shows_lan_address_when_bound_wide():
    cfg = Config()
    lines = startup_banner(host="0.0.0.0", port=8080, lan=["192.168.1.50"], cfg=cfg)
    text = "\n".join(lines)
    assert "http://192.168.1.50:8080/rev" in text
    assert "只监听" not in text


def test_banner_lists_extra_adapters_separately():
    """多网卡时要区分开，否则用户可能挑到 WSL 那种连不通的虚拟网卡。"""
    cfg = Config()
    lines = startup_banner(host="0.0.0.0", port=8080,
                           lan=["192.168.1.50", "172.31.224.1"], cfg=cfg)
    text = "\n".join(lines)
    # 真实网卡（调用方已排好序）出现在第一行推荐位
    assert text.index("192.168.1.50") < text.index("172.31.224.1")
    # 虚拟网卡必须被明确标注，否则用户会照抄
    assert "虚拟网卡，Kindle 连不上" in text


def test_lan_addresses_puts_real_nic_before_virtual():
    """真实出站网卡必须排在虚拟网卡前面。

    回归测试：只按字典序排的话，WSL 的 172.31.x.x 会排在 192.168.x.x 前面，
    启动提示就会把一个从 Kindle 根本连不上的地址推荐给用户 ——
    而现象依然是「页面无法显示」。
    """
    addrs = lan_addresses()
    virtual_seen = False
    for addr in addrs:
        if is_likely_virtual(addr):
            virtual_seen = True
        else:
            assert not virtual_seen, f"真实网卡 {addr} 排在了虚拟网卡后面：{addrs}"


def test_is_likely_virtual_classification():
    for addr in ("172.31.224.1", "172.17.0.1", "192.168.56.1", "169.254.1.1"):
        assert is_likely_virtual(addr), addr
    for addr in ("192.168.1.50", "10.0.0.5", "172.15.0.1", "172.32.0.1"):
        assert not is_likely_virtual(addr), addr


def test_banner_warns_when_no_lan_address():
    cfg = Config()
    lines = startup_banner(host="0.0.0.0", port=8080, lan=[], cfg=cfg)
    assert any("没找到局域网 IPv4" in line for line in lines)


def test_banner_mentions_firewall_and_preroll():
    cfg = Config()
    text = "\n".join(startup_banner(host="0.0.0.0", port=8080,
                                    lan=["192.168.1.50"], cfg=cfg))
    assert "New-NetFirewallRule" in text
    assert "预检" in text


def test_lan_addresses_excludes_loopback():
    for addr in lan_addresses():
        assert not addr.startswith("127.")
        assert addr.count(".") == 3
