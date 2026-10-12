"""服务健壮性：**单个客户端的任何行为都不该让服务停下**。

这次的真实故障
--------------
服务的日志以这段结尾，然后进程 exit 1：

    ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。

起因只是**浏览器请求 ``/favicon.ico`` 之后关掉了连接** ——
``socketserver`` 在请求处理里抛了未捕获异常，把整个服务带走了。

对一个要 7×24 常驻、给 Kindle 提供画面的服务来说这是致命的：
用户随时在开关管理页，每次都可能触发。服务一停，副屏就永远停在旧画面上，
而且**没有任何提示** —— 用户只会觉得"副屏坏了"。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from kindlewarden.config import Config
from kindlewarden.server import build_server


@pytest.fixture
def live(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_server_survives_client_abrupt_disconnect(live):
    """客户端**连上就断开**（不发完整请求）之后，服务必须还活着。

    这正是搞死过服务的场景：浏览器拿到 favicon 的 404 就关连接。
    """
    host, port = live.replace("http://", "").split(":")
    port = int(port)

    # 连上、只发半个请求、立刻 RST 掉连接 —— 模拟最粗暴的客户端
    for _ in range(5):
        s = socket.create_connection((host, port), timeout=3)
        s.sendall(b"GET /favicon.ico HTTP/1.1\r\nHost: x\r\n")
        # 强制 RST 而不是正常 FIN，这最容易触发 ConnectionResetError
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                     b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()

    # 服务必须still 正常响应
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(live + "/health", timeout=3) as r:
                body = json.loads(r.read().decode())
            assert body.get("status") == "ok"
            return
        except Exception:                          # noqa: BLE001
            time.sleep(0.2)
    pytest.fail("服务在被客户端粗暴断连后不再响应 —— 又会被一个浏览器搞死")


def test_server_survives_many_disconnects(live):
    """连续多次断连之后，服务仍然要能正常服务。

    防的是"能扛一次、扛不住多次"这类半吊子修复。
    """
    host, port = live.replace("http://", "").split(":")
    port = int(port)

    for i in range(30):
        try:
            s = socket.create_connection((host, port), timeout=2)
            if i % 2 == 0:
                # 一个字节都不发就断
                pass
            else:
                # 发一半请求头就断
                s.sendall(b"GET /scr")
            s.close()
        except OSError:
            pass

    with urllib.request.urlopen(live + "/health", timeout=5) as r:
        assert json.loads(r.read().decode())["status"] == "ok"


def test_server_ignores_connection_reset_during_response(live):
    """请求**已经发出**、客户端在收响应前断连 —— 也不能让服务挂。

    对端断在写响应途中是最典型的 ``BrokenPipeError`` / ``ConnectionResetError``
    来源，而且它发生在**服务已经决定要响应之后**。
    """
    host, port = live.replace("http://", "").split(":")
    port = int(port)

    for _ in range(5):
        s = socket.create_connection((host, port), timeout=3)
        s.sendall(b"GET /screen.png HTTP/1.1\r\nHost: x\r\n"
                  b"Connection: keep-alive\r\n\r\n")
        # 不等响应就 RST
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                     b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(live + "/health", timeout=3) as r:
                assert json.loads(r.read().decode())["status"] == "ok"
                return
        except Exception:                          # noqa: BLE001
            time.sleep(0.2)
    pytest.fail("写响应途中被断连之后，服务不再响应")


def test_server_is_still_functional_after_disconnects(live):
    """断连之后不只是"活着"，**功能也要正常**（能发简报、能取画面）。"""
    host, port = live.replace("http://", "").split(":")
    port = int(port)

    for _ in range(10):
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.sendall(b"GET /")
            s.close()
        except OSError:
            pass

    # 发一条简报
    req = urllib.request.Request(
        live + "/brief", method="POST",
        data=json.dumps({"title": "断连之后仍然能发"},
                        ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read().decode())["brief_id"]

    # 取画面
    with urllib.request.urlopen(live + "/screen.json", timeout=5) as r:
        info = json.loads(r.read().decode())
    assert info["briefs"][0]["title"] == "断连之后仍然能发"


def test_second_instance_refuses_the_occupied_port(tmp_path):
    """**端口已被占用时，第二个实例必须明确失败。**

    ⚠️ 这里踩过一个很隐蔽的坑：我在服务器类上设了
    ``allow_reuse_address = True``（以为能"避免重启时报 address in use"）。

    但 Windows 上 ``SO_REUSEADDR`` 的语义是 **允许两个 socket 绑同一端口**
    （Linux 只允许 TIME_WAIT 状态的）。结果第二个实例**照样启动成功**，
    两个服务同时"监听" 8577，连接被随机分给其中一个。

    现象：有时响应正常、有时数据是旧的、有时状态不一致 ——
    极难排查。而且它把"端口占用"这个本该立刻暴露的错误变成了**静默**。

    实测就是这样：我以为在验证"端口占用会报错"，结果第二个实例兴高采烈地
    打印了启动横幅，才发现自己引入了这个 bug。
    """
    import socket as _socket

    from kindlewarden.server import RobustHTTPServer, build_server

    # 明确要求：不能开端口复用
    assert RobustHTTPServer.allow_reuse_address is False, (
        "allow_reuse_address 必须是 False —— 在 Windows 上开启它会让"
        "两个实例绑同一端口，导致请求随机分发、状态不一致"
    )

    # 真起一个服务占住端口
    cfg1 = Config()
    cfg1.data_dir = tmp_path
    cfg1.source_path = tmp_path / "panels.toml"
    cfg1.server.host = "127.0.0.1"
    cfg1.server.port = 0
    srv1 = build_server(cfg1)
    port = srv1.server_address[1]
    threading.Thread(target=srv1.serve_forever, daemon=True).start()

    try:
        # 同一个端口再起一个：必须失败
        cfg2 = Config()
        cfg2.data_dir = tmp_path
        cfg2.source_path = tmp_path / "panels.toml"
        cfg2.server.host = "127.0.0.1"
        cfg2.server.port = port

        with pytest.raises(OSError):
            build_server(cfg2)
    finally:
        srv1.shutdown()
        srv1.server_close()


def test_serve_explains_port_in_use_clearly(tmp_path, capsys, monkeypatch):
    """``serve`` 撞上端口占用时，要给出**可操作**的说明。

    不能只抛一个 ``OSError: [Errno 10048]`` —— 那个报错对本机场景
    毫无指引（该看什么、该不该再起一个、怎么换端口）。
    """
    import threading as _threading

    from kindlewarden import server as srv_mod
    from kindlewarden.config import Config as _Config

    # 先占住一个端口
    cfg1 = _Config()
    cfg1.data_dir = tmp_path
    cfg1.source_path = tmp_path / "panels.toml"
    cfg1.server.host = "127.0.0.1"
    cfg1.server.port = 0
    srv1 = srv_mod.build_server(cfg1)
    port = srv1.server_address[1]
    _threading.Thread(target=srv1.serve_forever, daemon=True).start()

    try:
        cfg2 = _Config()
        cfg2.data_dir = tmp_path
        cfg2.source_path = tmp_path / "panels.toml"
        cfg2.server.host = "127.0.0.1"
        cfg2.server.port = port

        with pytest.raises(SystemExit):
            srv_mod.serve(cfg2)

        out = capsys.readouterr().out
        assert "已经被占用" in out, "没有说明端口被占用"
        assert "netstat" in out, "没告诉用户怎么查是谁占着"
        assert "health" in out, "没提示先确认已有实例是否正常"
        assert "--port" in out, "没给换端口的退路"
    finally:
        srv1.shutdown()
        srv1.server_close()


def test_handle_error_swallows_disconnect_but_reports_real_bugs(capsys):
    """``handle_error`` 要区分"客户端断连"和"真的 bug"。

    断连是正常现象，不该刷日志（浏览器每次关页面都会有）；
    但真的 bug 必须留下痕迹 —— 否则排查时什么都看不到。
    """
    from kindlewarden.server import RobustHTTPServer

    # 构造一个不真正 bind 的实例，只调用 handle_error
    srv = RobustHTTPServer.__new__(RobustHTTPServer)

    try:
        raise ConnectionResetError("客户端跑了")
    except ConnectionResetError:
        srv.handle_error(None, ("1.2.3.4", 1234))
    captured = capsys.readouterr()
    assert "客户端跑了" not in captured.err, \
        "客户端断连不该刷日志（浏览器每次关页面都会有）"

    try:
        raise ValueError("真的出 bug 了")
    except ValueError:
        srv.handle_error(None, ("1.2.3.4", 1234))
    captured = capsys.readouterr()
    assert "真的出 bug 了" in captured.err, \
        "真的 bug 必须留下痕迹，否则排查时什么都看不到"
