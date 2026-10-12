"""MCP 瘦客户端的测试。

最关键的不变量：**host 不可达时 agent 完全无感** ——
发布立刻返回，内容进本地发件箱排队，后台自动重试。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kindlewarden.mcp_server import MAX_BACKOFF, Outbox, Relay
from kindlewarden.store import EventStore, project


# ---------------------------------------------------------------- 测试用 host


class Recorder(BaseHTTPRequestHandler):
    received: list[tuple[str, dict]] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        Recorder.received.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):  # noqa: N802
        payload = json.dumps({"rev": "test", "briefs": [], "todos": [],
                              "alert": None, "source_health": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # 静音
        pass


@pytest.fixture
def host():
    Recorder.received = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------- 发件箱


def test_outbox_put_and_depth(tmp_path):
    box = Outbox(tmp_path)
    assert box.depth() == 0
    box.put("/brief", {"title": "一"})
    box.put("/brief", {"title": "二"})
    assert box.depth() == 2
    # 写入是原子的：不该留下临时文件
    assert not list(box.pending_dir.glob(".*.tmp"))


def test_outbox_state_file_is_not_treated_as_an_item(tmp_path):
    """回归测试：状态文件和待投递条目混在一层时，glob 会把它也算成一个条目。"""
    box = Outbox(tmp_path)
    box.write_state(last_ok_at=1.0, pending=0)
    assert box.depth() == 0
    box.put("/brief", {"title": "一"})
    assert box.depth() == 1


def test_outbox_survives_reopen(tmp_path):
    """持久化：进程重启后队列不能丢。"""
    Outbox(tmp_path).put("/brief", {"title": "重启前"})
    assert Outbox(tmp_path).depth() == 1


# ---------------------------------------------------------------- 投递


def test_relay_delivers_and_clears(tmp_path, host):
    box = Outbox(tmp_path)
    box.put("/brief", {"title": "投递测试"})
    relay = Relay(box, host)

    assert relay.drain_once() == 1
    assert box.depth() == 0
    assert Recorder.received == [("/brief", {"title": "投递测试"})]


def test_relay_keeps_item_when_host_is_down(tmp_path):
    """host 不可达时，条目必须留在队列里等重试 —— 这是 agent 无感的前提。"""
    box = Outbox(tmp_path)
    box.put("/brief", {"title": "离线排队"})
    relay = Relay(box, "http://127.0.0.1:1")   # 必定连不上

    assert relay.drain_once() == 0
    assert box.depth() == 1

    item = box.load(box.pending()[0])
    assert item["attempts"] == 1
    assert item["next_at"] > time.time()       # 已排入退避
    assert "last_error" in box.read_state()


def test_relay_backs_off_before_retrying(tmp_path):
    box = Outbox(tmp_path)
    box.put("/brief", {"title": "退避"})
    relay = Relay(box, "http://127.0.0.1:1")
    relay.drain_once()
    # 退避窗口内不应再次尝试
    assert relay.drain_once() == 0
    assert box.load(box.pending()[0])["attempts"] == 1


def test_relay_recovers_when_host_returns(tmp_path, host):
    """host 恢复后队列自动清空，不需要人工干预。"""
    box = Outbox(tmp_path)
    box.put("/brief", {"title": "先离线"})

    down = Relay(box, "http://127.0.0.1:1")
    down.drain_once()
    assert box.depth() == 1

    # 退避窗口过去（真实运行时由时间流逝完成）
    path = box.pending()[0]
    item = box.load(path)
    item["next_at"] = 0.0
    box.save(path, item)

    up = Relay(box, host)
    assert up.drain_once() == 1
    assert box.depth() == 0
    assert Recorder.received[0][1]["title"] == "先离线"


def test_backoff_is_capped(tmp_path):
    """退避必须封顶。

    否则长时间断网后 attempts 会很大，恢复投递要等很久 ——
    对一个「我离开电脑时也能看到进展」的副屏来说，那是致命的。
    """
    box = Outbox(tmp_path)
    box.put("/brief", {"title": "封顶"})
    path = box.pending()[0]
    item = box.load(path)
    item["attempts"] = 40                  # 模拟长期失败
    box.save(path, item)

    Relay(box, "http://127.0.0.1:1").drain_once()
    delay = box.load(path)["next_at"] - time.time()
    assert delay <= MAX_BACKOFF * 1.5, f"退避 {delay:.0f}s 未封顶"


def test_relay_drops_corrupt_item(tmp_path):
    """损坏的队列文件不能把队列卡死。"""
    box = Outbox(tmp_path)
    (tmp_path / "broken.json").write_text("{ 这不是 json", encoding="utf-8")
    relay = Relay(box, "http://127.0.0.1:1")
    relay.drain_once()
    assert box.depth() == 0


# ---------------------------------------------------------------- 端到端（stdio）


def _rpc_lines(proc, messages, timeout=20.0):
    """通过 stdio 发 JSON-RPC，收集响应。"""
    out: list[dict] = []
    stdin, stdout = proc.stdin, proc.stdout
    assert stdin and stdout
    for msg in messages:
        stdin.write(json.dumps(msg) + "\n")
        stdin.flush()
    # 读到收到足够多的带 id 响应为止
    wanted = sum(1 for m in messages if "id" in m)
    deadline = time.time() + timeout
    while len(out) < wanted and time.time() < deadline:
        line = stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in parsed:
            out.append(parsed)
    return out


@pytest.mark.slow
def test_mcp_stdio_end_to_end(tmp_path, host):
    """真正起一个 MCP server 子进程，走 stdio 协议发一条简报。

    验证的是 harness 实际会做的事情：initialize → tools/list → tools/call。
    """
    env = dict(os.environ)
    env["KINDLEWARDEN_URL"] = host
    env["KINDLEWARDEN_CLIENT_DATA"] = str(tmp_path / "client")

    proc = subprocess.Popen(
        [sys.executable, "-m", "kindlewarden.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    try:
        responses = _rpc_lines(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "warden_publish_brief",
                        "arguments": {"title": "端到端测试简报", "status": "ok",
                                      "summary": "经由 MCP stdio 通路"}}},
        ])

        by_id = {r["id"]: r for r in responses}
        assert 1 in by_id, f"initialize 无响应：{responses}"
        assert by_id[1]["result"]["serverInfo"]["name"] == "kindlewarden"

        tools = {t["name"] for t in by_id[2]["result"]["tools"]}
        assert {"warden_publish_brief", "warden_list_recent",
                "warden_status"} <= tools

        # 工具调用必须是「已入队」而不是「已送达」—— 非阻塞的证据
        text = by_id[3]["result"]["content"][0]["text"]
        assert "queue_id" in text

        # 后台线程会异步把它投出去
        deadline = time.time() + 15
        while time.time() < deadline and not Recorder.received:
            time.sleep(0.2)
        assert Recorder.received, "后台投递线程没有送达"
        assert Recorder.received[0][1]["title"] == "端到端测试简报"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_publish_brief_accepts_artifacts_as_strings_or_objects(tmp_path, host):
    """``artifacts`` 必须**同时接受字符串和对象**。

    这是一个真实的可用性缺陷（实测踩到）：MCP 工具原先签名是
    ``artifacts: list[str]``，实现里写 ``[{"label": a} for a in artifacts]``。
    agent 从管理页/文档看到产出物是对象 ``{label, kind, ref}``，
    自然照那样传 —— 于是 ``{"label": {...}}`` 撞上 pydantic 校验错误：

        artifacts.0  Input should be a valid string [type=string_type]

    服务端本来两种都收，是 MCP 这一层在到达服务端前就把它拍扁了。

    **工具签名必须匹配实际数据模型** —— 否则"让 agent 按要求汇报"
    这件事就卡在最基础的一步上。
    """
    env = dict(os.environ)
    env["KINDLEWARDEN_URL"] = host
    env["KINDLEWARDEN_CLIENT_DATA"] = str(tmp_path / "client")

    proc = subprocess.Popen(
        [sys.executable, "-m", "kindlewarden.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    try:
        responses = _rpc_lines(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            # 两种形式混着传，更接近真实用法
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "warden_publish_brief",
                        "arguments": {
                            "title": "产出物两种形式",
                            "status": "ok",
                            "artifacts": [
                                "PR #482",                                   # 字符串
                                {"label": "screen.py", "kind": "code",       # 对象
                                 "ref": "kindlewarden/screen.py"},
                            ],
                        }}},
        ])

        by_id = {r["id"]: r for r in responses}
        text = by_id[2]["result"]["content"][0]["text"]
        assert "queue_id" in text, f"对象形式被拒了：{text}"
        assert "validation error" not in text.lower(), (
            f"还是校验失败 —— 工具签名没匹配数据模型：{text}"
        )

        # 后台投递，检查落到服务端的产出物是规范化后的对象
        deadline = time.time() + 15
        while time.time() < deadline and not Recorder.received:
            time.sleep(0.2)
        assert Recorder.received, "后台投递线程没有送达"

        sent = Recorder.received[0][1]
        arts = sent.get("artifacts") or []
        assert len(arts) == 2, f"产出物条数不对：{arts}"

        # 字符串形式：label 和 ref 都用原字符串
        assert arts[0]["label"] == "PR #482"
        assert arts[0]["ref"] == "PR #482"
        # 对象形式：三个字段都保留
        assert arts[1]["label"] == "screen.py"
        assert arts[1]["kind"] == "code"
        assert arts[1]["ref"] == "kindlewarden/screen.py"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.slow
def test_mcp_call_is_non_blocking_when_host_down(tmp_path):
    """host 完全不可达时，工具调用仍必须立刻成功返回。"""
    env = dict(os.environ)
    env["KINDLEWARDEN_URL"] = "http://127.0.0.1:1"
    env["KINDLEWARDEN_CLIENT_DATA"] = str(tmp_path / "client")

    proc = subprocess.Popen(
        [sys.executable, "-m", "kindlewarden.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    try:
        started = time.time()
        responses = _rpc_lines(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "warden_publish_brief",
                        "arguments": {"title": "host 不在线也要能发"}}},
        ])
        elapsed = time.time() - started

        by_id = {r["id"]: r for r in responses}
        assert 2 in by_id, f"host 不可达时调用失败了：{responses}"
        assert "queue_id" in by_id[2]["result"]["content"][0]["text"]
        # 不该有网络重试带来的长时间阻塞
        assert elapsed < 10, f"调用耗时 {elapsed:.1f}s，疑似阻塞在网络重试上"

        # 队列里确实留着这条
        from pathlib import Path
        pending = list((Path(env["KINDLEWARDEN_CLIENT_DATA"])
                        / "outbox" / "pending").glob("*.json"))
        assert len(pending) == 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
