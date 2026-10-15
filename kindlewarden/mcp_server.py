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


def resolve_todo_id(todo_id: str, match: str) -> tuple[str | None, dict | None]:
    """把「要操作哪条待办」解析成一个具体的 id。

    返回 ``(todo_id, error)``：成功时 error 为 None，失败时 todo_id 为 None
    且 error 是一个可直接返回给 agent 的字典。

    ⚠️ 三个待办工具（改 / 删 / 完成）共用一个解析器。

    各写一份的话，行为迟早不一致 —— 比如"匹配到多条"时一个报错、
    另一个随便挑一条。**随便挑一条然后改错**比拒绝更糟：
    agent 以为改对了，而板上是另一条被改了。

    为什么支持按文字匹配：agent 常常只记得内容、不记得 id。
    但匹配到多条时**绝不猜**，直接把候选列出来让它自己指定。
    """
    todo_id = (todo_id or "").strip()
    match = (match or "").strip()

    if not todo_id and not match:
        return None, {
            "ok": False,
            "error": "要指定操作哪一条：给 todo_id，或者给 match",
            "hint": "先用 warden_list_recent 看有哪些待办",
        }

    try:
        board = get_json(host_url(), "/board", timeout=3.0)
    except Exception as exc:                       # noqa: BLE001
        return None, {
            "ok": False,
            "error": f"副屏服务不可达：{type(exc).__name__}: {exc}",
            "hint": "按文字定位需要先读到当前列表；"
                    "服务恢复后再试。",
        }

    todos = board.get("todos") or []

    if not todo_id:
        needle = match.lower()
        hits = [t for t in todos if needle in (t.get("text") or "").lower()]
        if not hits:
            return None, {
                "ok": False,
                "error": f"没有待办匹配 {match!r}",
                "todos": [{"id": t["id"], "text": t["text"]} for t in todos],
            }
        if len(hits) > 1:
            return None, {
                "ok": False,
                "error": f"{match!r} 匹配到 {len(hits)} 条，请用 todo_id 指定",
                "candidates": [{"id": t["id"], "text": t["text"]}
                               for t in hits],
            }
        todo_id = hits[0]["id"]

    if not any(t.get("id") == todo_id for t in todos):
        return None, {
            "ok": False,
            "error": f"看板上没有 id={todo_id} 的待办",
            "todos": [{"id": t["id"], "text": t["text"]} for t in todos],
        }

    return todo_id, None


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
        title="修改一条已有待办",
        description=(
            "改掉副屏待办板上已有的一条（写错了、措辞要调整、范围变了）。\n"
            "\n"
            "**改之前先想一下：是不是该直接删掉？** "
            "如果那条已经没意义了，用 warden_remove_todo 比改它更干净。\n"
            "\n"
            "定位方式二选一：\n"
            "  * ``todo_id`` —— 从 warden_list_recent 拿到的 id，最可靠\n"
            "  * ``match`` —— 用一段文字去匹配现有待办（子串匹配，忽略大小写）\n"
            "\n"
            "⚠️ 用 ``match`` 时如果**匹配到多条**会报错并列出候选，"
            "这时改用 ``todo_id`` 指定，别猜。\n"
            "这是同步调用（要读当前看板才能定位），失败会返回错误而不是卡住。"
        ),
    )
    def warden_update_todo(text: str, todo_id: str = "",
                           match: str = "") -> dict:
        """把一条已有待办改成新的文字。

        ``todo_id`` 和 ``match`` 二选一；都给时以 ``todo_id`` 为准。
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "新文字不能为空"}

        resolved, err = resolve_todo_id(todo_id, match)
        if err:
            return err

        # 记下改前的样子 —— agent 看不到界面，
        # 回一句"原来是什么"能让它自己确认改对了没有。
        try:
            board = get_json(host_url(), "/board", timeout=3.0)
            was = next((t.get("text", "") for t in (board.get("todos") or [])
                        if t.get("id") == resolved), "")
        except Exception:                              # noqa: BLE001
            was = ""

        return {
            "ok": True,
            "queued": True,
            "todo_id": resolved,
            "was": was,
            "now": text,
            "queue_id": box.put("/todo/update", {"id": resolved, "text": text}),
        }

    @mcp.tool(
        title="删除一条待办",
        description=(
            "把一条待办从副屏上彻底删掉。\n"
            "和「完成」不同：完成是勾上、留痕（``warden_complete_todo``）；"
            "删除是它**本来就不该存在**（记错了、重复了、作废了）。\n"
            "用 ``todo_id``（从 warden_list_recent 拿）或 ``match`` 定位。"
        ),
    )
    def warden_remove_todo(todo_id: str = "", match: str = "") -> dict:
        """删掉一条待办。"""
        resolved, err = resolve_todo_id(todo_id, match)
        if err:
            return err
        return {"ok": True, "queued": True, "todo_id": resolved,
                "queue_id": box.put("/todo/remove", {"id": resolved})}

    @mcp.tool(
        title="把一条待办标记为完成",
        description=(
            "勾上一条待办（做完了）。会记录完成时间，板上显示成划掉的样子。\n"
            "用 ``todo_id`` 或 ``match`` 定位；``done=False`` 可以取消勾选。"
        ),
    )
    def warden_complete_todo(todo_id: str = "", match: str = "",
                             done: bool = True) -> dict:
        """标记待办完成/未完成。"""
        resolved, err = resolve_todo_id(todo_id, match)
        if err:
            return err
        return {"ok": True, "queued": True, "todo_id": resolved,
                "done": bool(done),
                "queue_id": box.put("/todo/toggle",
                                    {"id": resolved, "done": bool(done)})}

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


# ---------------------------------------------------------------- HTTP 形态
#
# 和上面的 stdio 形态**并存**，不是替换。
#
# 两者的区别只有一个：**投递要不要经过本地发件箱**。
#
#   stdio（上面的 build_server）
#     agent ─stdio─▶ 瘦客户端 ─▶ 本地队列 ─▶ 后台异步 POST ─▶ host
#     agent 永远亚毫秒返回；host 挂了也完全无感。
#     代价：每个终端要装一次（还要处理 exe 路径这类事）。
#
#   HTTP（下面的 build_http_server）
#     agent ─HTTP─▶ host 的 /mcp ─▶ 直接写 BoardService
#     没有 exe、没有本地队列、配置里就一个 URL。
#     代价：**host 不可达时 tool call 直接报错** ——
#           "副屏不影响主屏"这条保证在 HTTP 形态下**不再成立**。
#
# 为什么 HTTP 形态可以直接调 BoardService 而不走 HTTP：
# 它就挂在同一个进程里（见 asgi_host），所以"直接写"是最短路径，
# 中间再绕一次自己的 HTTP 反而多一个失败点。


def in_container() -> bool:
    """是不是跑在容器里（Docker / Podman）。

    判据用 ``/.dockerenv`` 或 ``/run/.containerenv`` ——
    这是最稳的：读 cgroup 在 cgroup v2 上已经不可靠了。
    """
    return (Path("/.dockerenv").exists()
            or Path("/run/.containerenv").exists())


def _default_allowed_hosts() -> list[str]:
    """按本机实际地址生成 MCP 的 Host 白名单（含端口通配）。

    形如 ``["127.0.0.1:*", "localhost:*", "192.168.9.198:*", ...]``。

    ⚠️ 必须带 ``:*`` —— 校验是拿**完整的 Host 头**（含端口）去比对的，
    只写 IP 匹配不上 ``192.168.9.198:8577``。

    ⚠️⚠️ **容器里这个函数算出来的地址是错的**，要说清楚。

    ``lan_addresses()`` 给的是"本机的地址"，而在容器里那是
    **容器自己的**地址（``172.17.0.x``）—— 客户端连的是**宿主机**的
    局域网地址（``192.168.x.x:8577``），两者对不上，于是每个请求都 421。

    补救靠 ``KINDLEWARDEN_PUBLIC_HOST``：``lan_addresses()`` 已经支持
    用它覆盖，所以只要用户按 docker-compose 里的注释设了它，
    这里自动就是对的。

    没设的时候**要吵**（见 ``warn_if_container_without_public_host``）——
    否则现象只是"421 Invalid Host header"，而真正的原因是
    "容器不知道自己的对外地址"。
    """
    hosts = ["127.0.0.1", "localhost", "[::1]"]
    try:
        from .server import lan_addresses
        hosts.extend(lan_addresses())
    except Exception:                                  # noqa: BLE001
        pass
    # 去重且保序
    seen: list[str] = []
    for h in hosts:
        if h and h not in seen:
            seen.append(h)
    return [f"{h}:*" for h in seen]


def warn_if_container_without_public_host() -> str | None:
    """容器里没设 ``KINDLEWARDEN_PUBLIC_HOST`` 时给一句警告，否则 None。

    返回的是**要打印的文案**（不直接打印），方便测试断言。

    为什么值得专门警告：这种情况下 MCP 会**每个请求都 421**，
    而报错只说 ``Invalid Host header`` ——
    从这句话推不到"容器不知道自己的对外地址"。
    """
    if not in_container():
        return None
    if (os.environ.get("KINDLEWARDEN_PUBLIC_HOST") or "").strip():
        return None
    return (
        "⚠ 容器里没设 KINDLEWARDEN_PUBLIC_HOST —— MCP 的 Host 校验会拒绝"
        "所有外部请求（返回 421 Invalid Host header）。\n"
        "  容器探测到的是自己的地址（172.x），而客户端连的是宿主机的地址。\n"
        "  修法：给容器加环境变量 KINDLEWARDEN_PUBLIC_HOST=<宿主机的局域网IP>"
    )


def build_http_server(svc: Any, *, allowed_hosts: list[str] | None = None):
    """构造**挂在 host 进程内**的 MCP server（纯 HTTP 形态）。

    ``svc`` 是 ``BoardService``。工具直接调它的方法 ——
    没有网络、没有发件箱，调用返回即为最终结果。

    ⚠️ ``allowed_hosts`` 必须配好，否则**局域网访问会被拒**。

    MCP 的 streamable-http 默认开了 DNS-rebinding 防护，
    ``allowed_hosts`` 默认是空列表 —— 于是**任何** Host 都不匹配：

        421 Misdirected Request
        Invalid Host header

    实测：不配这个，连 ``127.0.0.1:8577/mcp`` 都是 421。

    ⚠️ 它**不支持** ``"*"`` 这种通配。看实现（``_validate_host``）：
    要么**精确匹配**完整的 Host 值（**含端口**，如 ``192.168.9.198:8577``），
    要么用 ``host:*`` 这种"端口任意"的写法。
    我第一版写了 ``["*"]``，结果连本机都被拒 —— 通配符的形式要对。

    这个防护对"只给本机 agent 用"是合理的，但本项目的副屏
    **本来就是局域网服务**（Kindle 按 IP 拉图、别的机器按 IP 连 MCP）。
    所以默认按**本机所有地址 + 端口**生成 allowlist：
    既让局域网能用，也不至于把 Host 校验完全关掉。
    """
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    if allowed_hosts is None:
        # 没显式指定 → 按实际地址生成
        allowed_hosts = _default_allowed_hosts()

    mcp = FastMCP(
        "kindlewarden",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
        ),
    )

    @mcp.tool(
        title="发布任务完成简报到副屏",
        description=(
            "把一条任务完成简报推送到 Kindle 副屏。**同步生效**。\n"
            "\n"
            "副屏是常驻桌面、只读、一屏且**不能滚动**的电子墨水屏，"
            "只承载**结论**，不承载**过程**。\n"
            "\n"
            "应该发布：完成一个多步骤任务 / 修复并验证了 bug / 完成部署迁移 / "
            "得出用户需要知道的结论 / 任务失败或需要用户介入。\n"
            "不要发布：单个文件编辑、单次命令执行、**任何中间步骤**、"
            "纯信息查询（读文件、搜索）、你只是「开始了」某件事。\n"
            "\n"
            "summary 写成**给这个人的简报**（中文、一句一句说清结论），"
            "不是写给他看的日志。第一句控制在 28 个中文字以内 —— "
            "看板每行只显示这么多，超了会被截断。\n"
            "多段用 \\n 分隔；想分条就用 - 或 1. 开头，渲染时会变成项目符号。\n"
            "\n"
            "如果同一件事之前发过，用 supersedes 传旧简报的 id 来更新那一条，"
            "不要发新的（会刷屏）。"
        ),
    )
    def warden_publish_brief(
        title: str,
        status: Status = "ok",
        summary: str = "",
        project: str = "",
        artifacts: list | None = None,
        duration_s: int = 0,
        supersedes: str = "",
    ) -> dict:
        """发布一条简报（同步写入副屏）。"""
        payload: dict = {
            "title": title, "status": status, "summary": summary,
            "project": project, "duration_s": int(duration_s or 0),
        }
        if artifacts:
            payload["artifacts"] = normalize_artifacts(artifacts)
        if supersedes:
            payload["supersedes"] = supersedes
        try:
            out = svc.publish_brief(payload)
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, **out}

    @mcp.tool(
        title="查看副屏当前内容",
        description=(
            "读取副屏上现在显示的简报、待办与告警。\n"
            "**发布简报前先调这个**，检查同一任务是否已经汇报过 —— "
            "如果已经发过，应该用 warden_publish_brief 的 supersedes 参数"
            "更新那一条，而不是再发一条新的。"
        ),
    )
    def warden_list_recent(limit: int = 10) -> dict:
        """查看副屏当前内容。"""
        board = svc.board(include_hidden=False)
        n = max(1, min(int(limit or 10), 50))
        return {
            "ok": True,
            "alert": board.alert.to_dict() if board.alert else None,
            "briefs": [
                {"id": b.id, "title": b.title, "status": b.status,
                 "summary": b.summary, "project": b.project,
                 "pinned": b.pinned}
                for b in board.briefs[:n]
            ],
            "todos": [{"id": t.id, "text": t.text, "done": t.done}
                      for t in board.todos],
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
        try:
            out = svc.pin_brief(brief_id, bool(pinned))
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, **out}

    @mcp.tool(
        title="撤回一条简报",
        description=(
            "从副屏撤下一条简报（例如发现汇报有误）。\n"
            "用 id 指定，从 warden_list_recent 拿到。"
        ),
    )
    def warden_retract(brief_id: str) -> dict:
        """撤回一条简报。"""
        try:
            out = svc.retract_brief(brief_id)
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, **out}

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
        """往副屏待办板添加一条。"""
        try:
            out = svc.add_todo({"text": text})
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, **out}

    @mcp.tool(
        title="修改一条已有待办",
        description=(
            "改掉副屏待办板上已有的一条（写错了、措辞要调整、范围变了）。\n"
            "**改之前先想一下：是不是该直接删掉？** "
            "如果那条已经没意义了，用 warden_remove_todo 比改它更干净。\n"
            "定位用 todo_id（从 warden_list_recent 拿），"
            "或者用 match 传一段文字做子串匹配；"
            "match 匹配到多条会报错并列出候选，这时改用 todo_id。"
        ),
    )
    def warden_update_todo(text: str, todo_id: str = "",
                           match: str = "") -> dict:
        """把一条已有待办改成新的文字。"""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "新文字不能为空"}
        tid, err = _resolve_todo(svc, todo_id, match)
        if err:
            return err
        was = _todo_text(svc, tid)
        try:
            out = svc.update_todo(tid, text)
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "was": was, "now": text, **out}

    @mcp.tool(
        title="把一条待办标记为完成",
        description=(
            "勾上一条待办（做完了）。会记录完成时间，板上显示成划掉的样子。\n"
            "和 warden_remove_todo 的区别：完成**留痕**，删除当作没发生过。"
        ),
    )
    def warden_complete_todo(todo_id: str = "", match: str = "",
                             done: bool = True) -> dict:
        """标记待办完成/未完成。"""
        tid, err = _resolve_todo(svc, todo_id, match)
        if err:
            return err
        try:
            out = svc.toggle_todo(tid, bool(done))
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "done": bool(done), **out}

    @mcp.tool(
        title="删除一条待办",
        description=(
            "把一条待办从副屏上彻底删掉。\n"
            "和「完成」不同：完成是勾上、留痕；删除是它**本来就不该存在**"
            "（记错了、重复了、作废了）。"
        ),
    )
    def warden_remove_todo(todo_id: str = "", match: str = "") -> dict:
        """删掉一条待办。"""
        tid, err = _resolve_todo(svc, todo_id, match)
        if err:
            return err
        try:
            out = svc.remove_todo(tid)
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, **out}

    @mcp.tool(
        title="挂起「需要你介入」告警",
        description=(
            "在副屏顶部显示一条高优先级告警。这是副屏上**最强的注意力信号**，"
            "只在真的需要人做决定时使用（等待授权、无法自行判断、需要人工确认）。\n"
            "不要用它来播报进度或成功消息 —— 那会让真正重要的告警被忽视。\n"
            "问题解决后调 warden_clear_alert 清掉，否则会一直挂在屏幕顶端。"
        ),
    )
    def warden_alert(title: str, detail: str = "") -> dict:
        """挂起一条需要人介入的告警。title ≤15 字，detail ≤30 字。"""
        try:
            out = svc.raise_alert({"title": title, "detail": detail})
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, **out}

    @mcp.tool(
        title="清除副屏告警",
        description="清除当前挂在副屏顶部的「需要你介入」告警。",
    )
    def warden_clear_alert() -> dict:
        """清除副屏告警。"""
        try:
            out = svc.clear_alert("")
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, **out}

    @mcp.tool(
        title="副屏状态",
        description="查看副屏服务当前的简报/待办/告警统计与设备最后拉取时间。",
    )
    def warden_status() -> dict:
        """查看副屏状态。"""
        board = svc.board(include_hidden=False)
        return {
            "ok": True,
            "briefs": board.total_briefs,
            "todos": board.total_todos,
            "alert": board.alert.to_dict() if board.alert else None,
            "device": svc.device_last_seen(),
            "transport": "http（同步，直连 host）",
        }

    # ------------------------------------------------------------ 资源
    #
    # ⚠️ 两种 transport 要提供**同一套**资源和工具。
    #    少一个的话，配成 HTTP 的终端就凭空少一个能力，
    #    而用户不会去对比两种形态，只会觉得"这东西时好时坏"。

    @mcp.resource("warden://board/current",
                  description="副屏当前显示的完整内容（文本形式）")
    def board_current() -> str:
        board = svc.board(include_hidden=False)
        lines = [f"简报 {board.total_briefs} 条 · 待办 {board.total_todos} 项"]
        if board.alert:
            lines.append(f"⚠ 告警：{board.alert.title}")
        for b in board.briefs:
            pin = "[钉]" if b.pinned else ""
            lines.append(f"  - {pin}[{b.status}] {b.title} —— {b.summary}")
        for t in board.todos:
            lines.append(f"  - {'[x]' if t.done else '[ ]'} {t.text}")
        return "\n".join(lines)

    @mcp.resource("warden://board/health",
                  description="副屏服务与设备最近一次拉取情况")
    def board_health() -> str:
        return json.dumps({
            "transport": "http",
            "device": svc.device_last_seen(),
        }, ensure_ascii=False, indent=2)

    return mcp


# ---------------------------------------------------------------- 待办定位（HTTP 形态用）


def _todo_text(svc: Any, todo_id: str) -> str:
    board = svc.board(include_hidden=False)
    return next((t.text for t in board.todos if t.id == todo_id), "")


def _resolve_todo(svc: Any, todo_id: str, match: str) -> tuple[str | None, dict | None]:
    """HTTP 形态的待办定位器。

    和 stdio 形态的 ``resolve_todo_id`` **同一套规则**：
    匹配到多条就报错并列候选，绝不替调用方猜。
    区别只是数据来源（这里直接读 BoardService，不走 HTTP）。
    """
    todo_id = (todo_id or "").strip()
    match = (match or "").strip()

    if not todo_id and not match:
        return None, {
            "ok": False,
            "error": "要指定操作哪一条：给 todo_id，或者给 match",
            "hint": "先用 warden_list_recent 看有哪些待办",
        }

    board = svc.board(include_hidden=False)
    todos = [{"id": t.id, "text": t.text} for t in board.todos]

    if not todo_id:
        needle = match.lower()
        hits = [t for t in todos if needle in t["text"].lower()]
        if not hits:
            return None, {"ok": False, "error": f"没有待办匹配 {match!r}",
                          "todos": todos}
        if len(hits) > 1:
            return None, {
                "ok": False,
                "error": f"{match!r} 匹配到 {len(hits)} 条，请用 todo_id 指定",
                "candidates": hits,
            }
        todo_id = hits[0]["id"]

    if not any(t["id"] == todo_id for t in todos):
        return None, {"ok": False, "error": f"看板上没有 id={todo_id} 的待办",
                      "todos": todos}

    return todo_id, None


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()