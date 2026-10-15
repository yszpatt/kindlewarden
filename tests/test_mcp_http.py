"""MCP over HTTP 的测试。

这是**新增的传输形态**，和原来的 stdio 瘦客户端并存：

    stdio   客户端 ─▶ 本地发件箱 ─▶ 异步投递    （host 挂了无感）
    HTTP    客户端 ─▶ POST /mcp ─▶ 直接写        （同步，一个 URL）

本文件盯住 HTTP 形态最容易坏的地方：
  * 端点挂在**同一个端口**上（不额外开端口）
  * 端到端能跑通（initialize → tools/list → tools/call）
  * 不启用时给的是**可读的** 503，而不是 500 或静默 404
  * 出错不连累看板（MCP 挂了，Kindle 还得能拉到图）
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request

import pytest

from kindlewarden.config import Config
from kindlewarden.server import build_server


def _rpc(base: str, method: str, params: dict | None = None, rid: int = 1,
         session: str | None = None):
    """打一次 /mcp，返回 (json, session_id)。"""
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                       "params": params or {}}).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    req = urllib.request.Request(base + "/mcp", data=body, method="POST",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        sid = resp.headers.get("mcp-session-id") or session
    payload = None
    for line in raw.splitlines():
        if line.strip().startswith("data:"):
            payload = json.loads(line.strip()[5:].strip())
    if payload is None and raw.strip():
        payload = json.loads(raw)
    return payload, sid


@pytest.fixture
def live(tmp_path):
    """起一个开了 MCP 的服务，返回 base url。"""
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg, enable_mcp=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, httpd
    finally:
        if getattr(httpd, "asgi_host", None) is not None:
            httpd.asgi_host.stop()
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def live_without_mcp(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg, enable_mcp=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_mcp_shares_the_same_port_as_the_board(live):
    """MCP 和看板在**同一个端口**上 —— 客户端只需要记一个地址。"""
    base, _httpd = live
    with urllib.request.urlopen(base + "/health", timeout=5) as r:
        assert r.status == 200
    payload, sid = _rpc(base, "initialize",
                        {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "1"}})
    assert "result" in payload, payload
    assert sid, "没拿到 session id —— streamable-http 靠它维持会话"


def test_mcp_reports_itself_as_kindlewarden(live):
    base, _ = live
    payload, _sid = _rpc(base, "initialize",
                         {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "t", "version": "1"}})
    info = payload["result"]["serverInfo"]
    assert info["name"] == "kindlewarden"


def test_mcp_exposes_all_the_tools(live):
    """HTTP 形态要和 stdio 形态**提供同一套工具**。

    少一个的话，配置成 HTTP 的终端就凭空少一个能力 ——
    而用户不会去对比两种 transport，只会觉得"这个工具时好时坏"。
    """
    base, _ = live
    _init, sid = _rpc(base, "initialize",
                      {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}})
    payload, _ = _rpc(base, "tools/list", {}, rid=2, session=sid)
    names = {t["name"] for t in payload["result"]["tools"]}
    for expect in ("warden_publish_brief", "warden_list_recent", "warden_pin",
                   "warden_retract", "warden_add_todo", "warden_update_todo",
                   "warden_complete_todo", "warden_remove_todo",
                   "warden_alert", "warden_clear_alert", "warden_status"):
        assert expect in names, f"HTTP 形态少了工具 {expect}"


def test_mcp_publish_is_synchronous(live):
    """HTTP 形态是**同步**的：调用返回时，板上就该有了。

    这正是它和 stdio 形态的核心区别（那边是排队 + 后台投递）。
    """
    base, _ = live
    _init, sid = _rpc(base, "initialize",
                      {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}})

    payload, _ = _rpc(base, "tools/call",
                      {"name": "warden_publish_brief",
                       "arguments": {"title": "同步写入验证",
                                     "status": "ok", "summary": "写完立刻可读"}},
                      rid=2, session=sid)
    text = payload["result"]["content"][0]["text"]
    assert '"ok": true' in text or '"ok":true' in text, text

    # 不等待、直接读 —— 同步就意味着这里必须已经有
    payload, _ = _rpc(base, "tools/call",
                      {"name": "warden_list_recent", "arguments": {"limit": 5}},
                      rid=3, session=sid)
    data = json.loads(payload["result"]["content"][0]["text"])
    assert "同步写入验证" in [b["title"] for b in data["briefs"]]


def test_mcp_todo_roundtrip(live):
    """待办三件套在 HTTP 形态下也要能用（加 / 改 / 删）。"""
    base, _ = live
    _init, sid = _rpc(base, "initialize",
                      {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}})

    def call(name, args, rid):
        payload, _ = _rpc(base, "tools/call",
                          {"name": name, "arguments": args}, rid=rid,
                          session=sid)
        return json.loads(payload["result"]["content"][0]["text"])

    r = call("warden_add_todo", {"text": "HTTP 形态的待办"}, 10)
    assert r.get("ok"), r
    tid = r.get("todo_id")

    r = call("warden_update_todo",
             {"todo_id": tid, "text": "HTTP 形态的待办（已改）"}, 11)
    assert r.get("ok") and r.get("was") == "HTTP 形态的待办", r

    r = call("warden_remove_todo", {"todo_id": tid}, 12)
    assert r.get("ok"), r


def test_mcp_ambiguity_is_refused_over_http(live):
    """歧义拒绝这条规则，HTTP 形态也必须有。

    两种 transport 各有一套定位器（一个读 HTTP，一个直读 BoardService）——
    最容易出现的就是"一边修了、另一边没修"。
    """
    base, _ = live
    _init, sid = _rpc(base, "initialize",
                      {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}})

    def call(name, args, rid):
        payload, _ = _rpc(base, "tools/call",
                          {"name": name, "arguments": args}, rid=rid,
                          session=sid)
        return json.loads(payload["result"]["content"][0]["text"])

    for t in ("买牛奶", "买牛奶回来"):
        assert call("warden_add_todo", {"text": t}, 20).get("ok")

    r = call("warden_update_todo", {"match": "买牛奶", "text": "不该被改"}, 21)
    assert not r.get("ok"), f"歧义时不该动手：{r}"
    assert "匹配到 2 条" in r["error"], r
    assert len(r.get("candidates", [])) == 2


def test_mcp_endpoint_says_how_to_enable_it_when_off(live_without_mcp):
    """没开 MCP 时，访问 /mcp 要给**可读的**提示。

    给 404 的话，用户只会以为是路径写错了，然后去翻文档 ——
    而真正的原因是"启动时没加 --mcp"。
    """
    base = live_without_mcp
    req = urllib.request.Request(
        base + "/mcp", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 503, exc.value.code
    body = exc.value.read().decode("utf-8", errors="replace")
    assert "--mcp" in body, f"提示里没说怎么打开：{body}"


def test_board_still_works_when_mcp_is_off(live_without_mcp):
    """关掉 MCP 不该影响看板 —— 两条路径互不依赖。"""
    base = live_without_mcp
    for path in ("/health", "/board", "/screen.json?c=kindle"):
        with urllib.request.urlopen(base + path, timeout=8) as r:
            assert r.status == 200, path


# ---------------------------------------------------------------- Host 校验
#
# ⚠️ 这一组盯住一个**会让远程客户端完全连不上**的默认值。
#
# MCP 的 streamable-http 默认开 DNS-rebinding 防护，而
# ``allowed_hosts`` 默认是**空列表** —— 于是任何 Host 都不匹配：
#
#     421 Misdirected Request
#     Invalid Host header
#
# 实测：不配的话，连 127.0.0.1 都是 421。
#
# 而且它**不支持** ``"*"`` 这种通配（我第一版就写了 `["*"]`，
# 结果连本机都被拒）。看实现要么精确匹配完整 Host（**含端口**），
# 要么用 ``host:*``。这两种形式都要有测试盯着。


def test_default_allowed_hosts_uses_the_wildcard_port_form():
    """白名单条目必须是 ``host:*`` 形式。

    校验是拿**完整的 Host 头**（含端口，如 ``192.168.9.198:8577``）
    去比对的；只写裸 IP 匹配不上，等于没放行。
    """
    from kindlewarden.mcp_server import _default_allowed_hosts

    hosts = _default_allowed_hosts()
    assert hosts, "白名单不能为空 —— 空列表会让所有请求 421"
    for h in hosts:
        assert h.endswith(":*"), (
            f"{h!r} 没有端口通配 —— 匹配不上带端口的 Host 头，"
            f"请求会被 421 拒掉"
        )
    # 本机地址必须在里面，否则本机客户端也连不上
    assert any(h.startswith("127.0.0.1") for h in hosts), hosts
    assert any(h.startswith("localhost") for h in hosts), hosts


def test_explicit_allowed_hosts_is_respected(tmp_path):
    """显式传 allowed_hosts 时按它来，不要偷偷加别的。"""
    from kindlewarden.mcp_server import build_http_server
    from kindlewarden.server import BoardService

    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    svc = BoardService(cfg)
    mcp = build_http_server(svc, allowed_hosts=["example.test:*"])
    settings = mcp.settings.transport_security
    assert settings.allowed_hosts == ["example.test:*"], settings.allowed_hosts
    assert settings.enable_dns_rebinding_protection is True


# ---------------------------------------------------------------- 容器部署
#
# 容器是跑 MCP 最省事的位置（起一个，所有终端指过来），所以镜像**默认开 MCP**。
# 但容器里有个隐蔽的坑：Host 白名单是按"本机地址"算的，
# 而容器算出来的是**自己的** 172.x —— 客户端连的是宿主机的 192.168.x，
# 对不上 → 每个请求 421，而报错只说 "Invalid Host header"。


def test_public_host_env_feeds_the_mcp_allowlist(monkeypatch):
    """``KINDLEWARDEN_PUBLIC_HOST`` 必须进 MCP 的 Host 白名单。

    这是容器能用的**关键**：容器自动探测到的地址是错的，
    只能靠这个环境变量告诉它"客户端会用什么地址连我"。
    """
    from kindlewarden.mcp_server import _default_allowed_hosts

    monkeypatch.setenv("KINDLEWARDEN_PUBLIC_HOST", "192.168.1.50")
    hosts = _default_allowed_hosts()
    assert "192.168.1.50:*" in hosts, (
        f"设了 PUBLIC_HOST 却没进白名单：{hosts} —— 容器里 /mcp 会 421"
    )


def test_container_without_public_host_warns(monkeypatch):
    """容器里没设对外地址时要**主动警告**。

    不然现象只是「421 Invalid Host header」，
    从这句话完全推不到"容器不知道自己的对外地址"。
    """
    from kindlewarden.mcp_server import warn_if_container_without_public_host

    monkeypatch.setattr("kindlewarden.mcp_server.in_container", lambda: True)
    monkeypatch.delenv("KINDLEWARDEN_PUBLIC_HOST", raising=False)

    warn = warn_if_container_without_public_host()
    assert warn, "容器里没设 PUBLIC_HOST 却没警告"
    assert "KINDLEWARDEN_PUBLIC_HOST" in warn, warn
    assert "421" in warn, f"警告里要点出症状是 421：{warn}"


def test_container_with_public_host_is_quiet(monkeypatch):
    """设了就不要再嚷嚷。"""
    from kindlewarden.mcp_server import warn_if_container_without_public_host

    monkeypatch.setattr("kindlewarden.mcp_server.in_container", lambda: True)
    monkeypatch.setenv("KINDLEWARDEN_PUBLIC_HOST", "192.168.1.50")
    assert warn_if_container_without_public_host() is None


def test_not_in_container_never_warns(monkeypatch):
    """普通部署（非容器）不该看到这条警告 —— 那条建议对它没意义。"""
    from kindlewarden.mcp_server import warn_if_container_without_public_host

    monkeypatch.setattr("kindlewarden.mcp_server.in_container", lambda: False)
    monkeypatch.delenv("KINDLEWARDEN_PUBLIC_HOST", raising=False)
    assert warn_if_container_without_public_host() is None


def test_dockerfile_enables_mcp_by_default():
    """镜像的默认 CMD 必须带 ``--mcp``。

    这是个**产品决定**（容器默认开 MCP），值得用测试钉住 ——
    不然哪天有人重写 CMD 时很容易漏掉，而症状是
    "容器里 /mcp 是 503"，看着像没实现，实际只是没开。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    # 取最后那条 CMD（启动命令）
    cmd_lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip().startswith("CMD [")]
    assert cmd_lines, "Dockerfile 里没有 CMD"
    start = cmd_lines[-1]
    assert '"--mcp"' in start or "'--mcp'" in start, (
        f"镜像的启动命令没带 --mcp：{start}"
    )
    assert "0.0.0.0" in start, f"没绑 0.0.0.0，容器外连不上：{start}"


# ---------------------------------------------------------------- extra 依赖
#
# ⚠️ 这一组盯住一个**会导致容器无限重启**的坑。
#
# MCP SDK 在 pyproject 的 optional-dependencies 里（`mcp = [...]`），
# 而 `pip install -e .` **只装基础依赖**。我第一版给 Dockerfile 加了
# `--mcp` 却没装 extra，于是容器一起来就 ModuleNotFoundError →
# 进程退出 → compose 的 restart: unless-stopped 让它**无限重启**。
#
# 现象是"容器一直 restarting"、日志刷满 traceback，
# 而原因是构建时少写了一个 extra —— 两者看着毫无关系。


def test_dockerfile_installs_the_mcp_extra():
    """Dockerfile 必须装 ``[mcp]`` extra。

    这是上面那个重启循环的**直接原因**，也是唯一能在构建阶段
    拦住它的地方。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert '".[mcp]"' in text or "'.[mcp]'" in text, (
        "Dockerfile 没有装 [mcp] extra —— 容器起来会因为 "
        "ModuleNotFoundError 无限重启"
    )


def test_dockerfile_verifies_mcp_at_build_time():
    """构建时要**真的把 MCP 起一遍**，而不是只 import。

    `import mcp` 成功不等于 MCP 能用（ASGI host、lifespan、
    Host 白名单都可能在这之后才炸），而那些失败发生在运行时，
    会被 restart 策略变成重启循环。构建时验，失败就是**构建失败**。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "build_server" in text and "enable_mcp=True" in text, (
        "Dockerfile 没有在构建时起一遍 MCP —— "
        "只 import 挡不住运行时才炸的问题"
    )


def test_missing_mcp_sdk_does_not_kill_the_service(tmp_path, monkeypatch):
    """**缺 MCP SDK 时服务必须照常起**（这是重启循环的根治）。

    看板比 MCP 重要 —— 它连着 Kindle，不该因为一个
    **可选**依赖缺失就整个服务起不来。
    """
    import builtins

    from kindlewarden import server as srv

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real_import(name, *args, **kwargs)

    # 清掉已缓存的，再挡住后续导入
    saved = {k: v for k, v in sys.modules.items()
             if k == "mcp" or k.startswith("mcp.")}
    for k in saved:
        del sys.modules[k]
    monkeypatch.setattr(builtins, "__import__", fake_import)

    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0

    try:
        httpd = srv.build_server(cfg, enable_mcp=True)
    finally:
        monkeypatch.undo()
        sys.modules.update(saved)

    # 关键：没抛异常
    assert httpd is not None
    # MCP 标记为不可用，并且**记下了原因**
    assert getattr(httpd, "asgi_host", None) is None
    reason = getattr(httpd, "mcp_error", None)
    assert reason, "没记下失败原因 —— /mcp 的 503 就说不清是哪种情况"
    assert "mcp" in reason.lower(), reason

    # 看板照常
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        for path in ("/health", "/board"):
            with urllib.request.urlopen(base + path, timeout=8) as r:
                assert r.status == 200, path
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_mcp_503_distinguishes_missing_sdk_from_missing_flag(live_without_mcp):
    """没开 MCP 时的 503 要说清是**哪种**原因。

    两种情况修法完全不同：
      * 忘了加 --mcp           → 加参数
      * 加参数了但 SDK 没装     → 装 extra

    统一说"加 --mcp"的话，第二种情况的用户会白折腾。
    """
    base = live_without_mcp
    req = urllib.request.Request(
        base + "/mcp", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    body = exc.value.read().decode("utf-8", errors="replace")
    # 这个 fixture 是"没加 --mcp"，所以提示应该指向参数
    assert "--mcp" in body, body



