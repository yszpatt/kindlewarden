"""MCP 瘦客户端。

架构决策（docs/architecture.md §2.1）：**这个进程不做网络同步。**

    agent ──stdio──▶ warden-mcp
                        │ ① 立即写本地持久发件箱，返回
                        │ ② 后台线程异步投递到副屏 host
                        │ ③ 失败则退避重试，队列不丢
                        ▼
                     副屏 Host

为什么不让 MCP server 直接以 HTTP 形式暴露在 host 上？
因为那会让 agent 的每次 tool call 都依赖网络 —— host 重启、网络抖动，
agent 就要处理错误、重试、甚至中断任务。**副屏永远不该影响主屏的工作。**

于是：agent 侧永远是亚毫秒级的本地写；host 挂了、Kindle 关机、网线拔了，
agent 完全无感。

工具 description 是**唯一跨 harness 的提示注入点**
--------------------------------------------------
不同 harness 的 skill 机制完全不同（Claude Code 有 skills、Codex 有 AGENTS.md、
Cursor 有 rules、有些什么都没有），但所有 harness 都会读 MCP 的 tool description。
所以简报风格的核心约束必须写在这里，skill 只是它的增强版。
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal

from .config import default_data_dir
from .models import normalize_artifacts

DEFAULT_URL = "http://127.0.0.1:8577"
HTTP_TIMEOUT = 5.0
RELAY_INTERVAL = 2.0
# 退避封顶。副屏本身是分钟级刷新的，所以不需要更激进；
# 但也不能太长 —— 否则一次断网之后要等好几分钟简报才补上。
MAX_BACKOFF = 60.0

Status = Literal["ok", "partial", "failed", "info"]


# ---------------------------------------------------------------- 配置


def client_dir() -> Path:
    env = os.environ.get("KINDLEWARDEN_CLIENT_DATA")
    if env:
        return Path(env)
    return default_data_dir() / "client"


def host_url() -> str:
    return (os.environ.get("KINDLEWARDEN_URL") or DEFAULT_URL).rstrip("/")


# ---------------------------------------------------------------- 发件箱


class Outbox:
    """本地持久发件箱：一条一个文件，写完用 ``os.replace`` 原子落盘。

    待投递条目放在 ``pending/`` 子目录，同步状态放在 ``state.json`` ——
    两者混在同一层的话，「扫描待投递」的 glob 会把状态文件也当成一个条目。

    用「文件即条目」而不是单个 JSONL：写入天然原子（不需要偏移量记账），
    投递成功后直接删文件，崩溃时最多重发一条（副屏是幂等展示，重发无害）。
    """

    def __init__(self, path: Path):
        self.dir = Path(path)
        self.pending_dir = self.dir / "pending"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"

    def put(self, path: str, payload: dict) -> str:
        item_id = uuid.uuid4().hex
        item = {
            "id": item_id,
            "path": path,
            "payload": payload,
            "created_at": time.time(),
            "attempts": 0,
            "next_at": 0.0,
        }
        tmp = self.pending_dir / f".{item_id}.tmp"
        final = self.pending_dir / f"{item_id}.json"
        tmp.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)
        return item_id

    def pending(self) -> list[Path]:
        return sorted(self.pending_dir.glob("*.json"))

    def load(self, p: Path) -> dict | None:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, p: Path, item: dict) -> None:
        try:
            p.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def drop(self, p: Path) -> None:
        try:
            p.unlink()
        except OSError:
            pass

    def depth(self) -> int:
        return len(self.pending())

    # ---- 同步状态（供 warden_status 报告）

    def read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def write_state(self, **kw: Any) -> None:
        state = self.read_state()
        state.update(kw)
        try:
            self.state_path.write_text(json.dumps(state, ensure_ascii=False),
                                       encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------- 投递线程


def post_json(url: str, path: str, payload: dict, timeout: float = HTTP_TIMEOUT) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url + path, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str, path: str, timeout: float = HTTP_TIMEOUT) -> dict:
    with urllib.request.urlopen(url + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Relay:
    """后台投递线程。指数退避，永不放弃。"""

    def __init__(self, outbox: Outbox, url: str | None = None):
        self.outbox = outbox
        self.url = url or host_url()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="warden-relay",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:                      # noqa: BLE001
                pass                               # 投递线程绝不能把 MCP server 带崩
            self._stop.wait(RELAY_INTERVAL)

    def drain_once(self) -> int:
        """投递一轮。返回成功条数。"""
        sent = 0
        now = time.time()
        for item_path in self.outbox.pending():
            item = self.outbox.load(item_path)
            if item is None:
                self.outbox.drop(item_path)
                continue
            if now < float(item.get("next_at") or 0.0):
                continue
            try:
                post_json(self.url, str(item["path"]), dict(item["payload"]))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
                attempts = int(item.get("attempts") or 0) + 1
                item["attempts"] = attempts
                # 指数退避 + 抖动，避免多个终端同时重试把 host 打垮
                backoff = min(2.0 ** min(attempts, 9), MAX_BACKOFF)
                item["next_at"] = time.time() + backoff * (0.5 + random.random())
                self.outbox.save(item_path, item)
                self.outbox.write_state(
                    last_error=f"{type(exc).__name__}: {exc}",
                    last_error_at=time.time(),
                    pending=self.outbox.depth(),
                )
            else:
                self.outbox.drop(item_path)
                sent += 1
                self.outbox.write_state(last_ok_at=time.time(), last_error="",
                                        pending=self.outbox.depth())
        return sent


# ---------------------------------------------------------------- MCP server


def build_server(outbox: Outbox | None = None):
    from mcp.server.fastmcp import FastMCP

    box = outbox or Outbox(client_dir() / "outbox")
    relay = Relay(box)
    relay.start()

    mcp = FastMCP("kindlewarden")

    # ------------------------------------------------------------ 工具

    @mcp.tool(
        title="发布任务完成简报到副屏",
        description=(
            "把一条任务完成简报推送到 Kindle 副屏。立即返回，不等网络。\n"
            "\n"
            "副屏是常驻桌面、只读、一屏且**不能滚动**的电子墨水屏，"
            "只承载**结论**，不承载**过程**。\n"
            "\n"
            "应该发布：完成一个多步骤任务 / 修复并验证了 bug / 完成部署迁移 / "
            "得出用户需要知道的结论 / 任务失败或需要用户介入。\n"
            "不要发布：单个文件编辑、单次命令执行、**任何中间步骤**、"
            "纯信息查询（读文件、搜索）、你只是「开始了」某件事。\n"
            "判定方法：这条信息，用户一小时后回头看还有价值吗？"
            "如果只是「此刻正在发生」，不要发 —— 主屏已经显示了。\n"
            "\n"
            "title 是**结论**不是**过程**，≤20 个中文字（超出会被截断）。\n"
            "  ✗ \"执行了 12 次工具调用并修改了 3 个文件\"\n"
            "  ✓ \"修复了 auth 模块 token 刷新的竞态条件\"\n"
            "\n"
            "summary 有两个显示位置，长度要求不同：\n"
            "  * 看板：**单行截断**，所以第一句要 ≤28 个中文字、独立成句\n"
            "  * 详情页（点卡片进去）：**全文折行 + 分页**，没有硬上限\n"
            "\n"
            "所以 summary 可以写长，但必须**分段**：\n"
            "  * 先给结论（这句就是看板上显示的那 28 字）\n"
            "  * 用换行分段，一个意思一段\n"
            "  * 以 `-` / `*` / `1.` 开头的行会渲染成列表项\n"
            "  * 用 `\\n` 换行，不要写成一大坨\n"
            "\n"
            "写摘要的四个要求：\n"
            "  条理清晰 —— 一个意思一段，别把三件事挤在一句里\n"
            "  表述精确 —— 说\"输入抑制状态没恢复\"，不说\"有点问题\"\n"
            "  不繁琐   —— 能一句说清就别写三句，删掉过程叙述\n"
            "  容易理解 —— 用具体名词和动词，不用抽象黑话\n"
            "\n"
            "  做了 + 结果如何 + **需要用户注意什么**（最重要，也最常被漏掉）。\n"
            "\n"
            "绝对不要包含密钥、token、密码、客户数据或大段代码 —— "
            "副屏是常亮摆在桌面上的（服务端会兜底打码，但被打码的简报等于没汇报）。\n"
            "\n"
            "同一任务只发一次。要更新就用 supersedes 替换先前那条，而不是再发一条。"
        ),
    )
    def warden_publish_brief(
        title: str,
        status: Status = "ok",
        summary: str = "",
        project: str = "",
        artifacts: list[str | dict] | None = None,
        duration_s: int = 0,
        supersedes: str = "",
    ) -> dict:
        """发布一条简报。

        Args:
            title: 一句话结论（不是过程），≤20 个中文字。
            status: ok=完全达成 / partial=部分达成或有遗留 / failed=未达成需介入 / info=无需行动的结论。
            summary: **第一句 ≤28 字**（看板单行截断，这句是门面）。
                可以接着用换行写更多 —— 详情页会全文折行并分页显示。
                请分段：结论 / 原因或步骤 / 影响或后续。
                以 `-` 或 `1.` 开头的行会渲染成列表项。
            project: 项目名，作为标签显示。
            artifacts: 产出物，最多 5 条。**接受两种写法**：直接给字符串（如
                "PR #482"），或给对象 {"label": "...", "kind": "...", "ref": "..."}。
                kind 可选 pr / commit / file / url / doc / code；ref 是可点开的链接或路径。
                服务端两种都收 —— 因为管理页里产出物显示为对象，agent 自然会那样传。
            duration_s: 本次任务耗时（秒）。
            supersedes: 要取代的简报 id（用于更新同一条，避免刷屏）。
        """
        payload = {
            "title": title,
            "status": status,
            "summary": summary,
            "project": project,
            "duration_s": int(duration_s or 0),
        }
        if artifacts:
            # 复用服务端的规范化实现，**不要在这里另写一份**：
            # 服务端本来两种形式都收（字符串 / 对象），
            # 而这一版之前是 `[{"label": a} for a in artifacts]` ——
            # 传对象进来会变成 {"label": {...}}，直接 pydantic 校验失败。
            #
            # 这是个真实的可用性缺陷：管理和文档里产出物都显示为对象
            # （{label, kind, ref}），agent 自然会照那样传，然后吃一个校验错误。
            # 工具签名必须匹配实际数据模型，否则"通用性"就是空话。
            payload["artifacts"] = normalize_artifacts(artifacts)
        if supersedes:
            payload["supersedes"] = supersedes
        item_id = box.put("/brief", payload)
        return {
            "queued": True,
            "queue_id": item_id,
            "pending": box.depth(),
            "note": "已入本地发件箱，后台自动投递；host 不可达也不会影响你。",
        }

    @mcp.tool(
        title="查看副屏当前内容",
        description=(
            "读取副屏上现在显示的简报、待办与告警。\n"
            "**发布简报前先调这个**，检查同一任务是否已经汇报过 —— "
            "如果已经发过，应该用 warden_publish_brief 的 supersedes 参数更新那一条，"
            "而不是再发一条新的。\n"
            "这是同步调用（会访问副屏服务），但超时很短，失败会返回错误而不是卡住。"
        ),
    )
    def warden_list_recent(limit: int = 10) -> dict:
        """查看副屏当前内容，用于去重与避免重复汇报。"""
        try:
            board = get_json(host_url(), "/board", timeout=3.0)
        except Exception as exc:                   # noqa: BLE001
            return {
                "ok": False,
                "error": f"副屏服务不可达：{type(exc).__name__}: {exc}",
                "pending_local": box.depth(),
                "hint": "你可能在另一台机器上，或副屏服务没启动。"
                        "发布简报不需要它在线 —— 内容会排队等它回来。",
            }
        n = max(1, min(int(limit or 10), 50))
        return {
            "ok": True,
            "rev": board.get("rev"),
            "alert": board.get("alert"),
            "briefs": [
                {"id": b["id"], "title": b["title"], "status": b["status"],
                 "summary": b.get("summary", ""), "project": b.get("project", ""),
                 "pinned": b.get("pinned", False)}
                for b in (board.get("briefs") or [])[:n]
            ],
            "todos": [{"id": t["id"], "text": t["text"], "done": t["done"]}
                      for t in (board.get("todos") or [])],
            "pending_local": box.depth(),
        }

    @mcp.tool(
        title="钉住/取消钉住一条简报",
        description=(
            "把某条简报钉在副屏最前面。钉住的简报**永不过期**，"
            "适合需要长期挂在眼前的信息（进行中的事故、长期目标）。\n"
            "用 id 指定，从 warden_list_recent 拿到。"
        ),
    )
    def warden_pin(brief_id: str, pinned: bool = True) -> dict:
        """钉住一条简报，使其永不过期并排在最前。"""
        return {"queued": True, "queue_id": box.put(
            "/pin", {"id": brief_id, "pinned": bool(pinned)})}

    @mcp.tool(
        title="撤回一条简报",
        description=(
            "从副屏撤下一条简报（例如发现汇报有误）。\n"
            "如果只是想**更正**内容，优先用 warden_publish_brief 的 supersedes —— "
            "那会保留下「这条被更新过」的信息，而撤回是直接抹掉。"
        ),
    )
    def warden_retract(brief_id: str) -> dict:
        """撤回一条简报。"""
        return {"queued": True, "queue_id": box.put("/retract", {"id": brief_id})}

    @mcp.tool(
        title="在副屏待办板添加一条",
        description=(
            "往副屏的**待办板**加一条。这是给**人**看的待办，"
            "不是 agent 的内部执行计划 —— 不要把你的步骤清单塞进来。\n"
            "适合：需要用户后续处理的事、跨会话要记住的下一步。\n"
            "文本 ≤24 个中文字。"
        ),
    )
    def warden_add_todo(text: str) -> dict:
        """往副屏待办板添加一条（给你的用户看的）。"""
        return {"queued": True, "queue_id": box.put("/todo", {"text": text})}

    @mcp.tool(
        title="挂起/清除「需要你介入」告警",
        description=(
            "在副屏顶部显示一条高优先级告警。这是副屏上**最强的注意力信号**，"
            "只在真的需要人做决定时使用（等待授权、无法自行判断、需要人工确认）。\n"
            "不要用它来播报进度或成功消息 —— 那会让真正重要的告警被忽视。\n"
            "问题解决后调 warden_clear_alert 清掉，否则会一直挂在屏幕顶端。"
        ),
    )
    def warden_alert(title: str, detail: str = "") -> dict:
        """挂起一条需要人介入的告警。title ≤15 字，detail ≤30 字。"""
        return {"queued": True, "queue_id": box.put(
            "/alert", {"title": title, "detail": detail})}

    @mcp.tool(
        title="清除副屏告警",
        description="清除当前挂在副屏顶部的「需要你介入」告警。",
    )
    def warden_clear_alert() -> dict:
        """清除副屏告警。"""
        return {"queued": True, "queue_id": box.put("/alert/clear", {})}

    @mcp.tool(
        title="副屏连接状态",
        description=(
            "查看本地发件箱积压了多少条、上次成功投递是什么时候、"
            "有没有投递错误。当你怀疑简报没送到副屏时用它自查。"
        ),
    )
    def warden_status() -> dict:
        """查看发件箱状态与最近一次投递结果。"""
        state = box.read_state()
        pending = box.depth()
        return {
            "host": relay.url,
            "pending": pending,
            "last_ok_at": state.get("last_ok_at"),
            "last_error": state.get("last_error") or "",
            "last_error_at": state.get("last_error_at"),
            "healthy": pending == 0 and not state.get("last_error"),
        }

    # ------------------------------------------------------------ 资源

    @mcp.resource("warden://board/current",
                  description="副屏当前显示的完整内容（文本形式）")
    def board_current() -> str:
        try:
            board = get_json(host_url(), "/board", timeout=3.0)
        except Exception as exc:                   # noqa: BLE001
            return f"副屏服务不可达：{type(exc).__name__}: {exc}"
        lines = [f"rev {board.get('rev')} · 健康度 {board.get('source_health')}"]
        if board.get("alert"):
            lines.append(f"⚠ 告警：{board['alert']['title']}")
        lines.append(f"简报 {board.get('total_briefs')} 条：")
        for b in board.get("briefs") or []:
            pin = "[钉]" if b.get("pinned") else ""
            lines.append(f"  - {pin}[{b['status']}] {b['title']} —— {b.get('summary','')}")
        lines.append(f"待办 {board.get('total_todos')} 项：")
        for t in board.get("todos") or []:
            lines.append(f"  - {'[x]' if t['done'] else '[ ]'} {t['text']}")
        if box.depth():
            lines.append(f"（本地还有 {box.depth()} 条待投递）")
        return "\n".join(lines)

    @mcp.resource("warden://board/health",
                  description="副屏服务与本地发件箱的健康状况")
    def board_health() -> str:
        state = box.read_state()
        return json.dumps({
            "host": relay.url,
            "pending": box.depth(),
            "last_ok_at": state.get("last_ok_at"),
            "last_error": state.get("last_error") or "",
        }, ensure_ascii=False, indent=2)

    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
