"""把 ASGI app（这里是 FastMCP 的 streamable-http）托管在**当前进程**里。

为什么要这么绕
--------------
FastMCP 的 streamable-http app 是 ASGI 的，而本项目的服务是
``ThreadingHTTPServer``（不是 ASGI）。两者要在一个端口上共存，
最省事的做法不是"再开一个端口 + 反向代理"（多一个进程/失败点），
而是**在进程内把 ASGI 当函数调用**。

难点在于：streamable-http 依赖一个**常驻的 anyio task group**
（session manager）。直接一次性 ``await app(...)`` 会报：

    RuntimeError: Task group is not initialized. Make sure you use run().

所以这里起一个**专用后台线程**跑事件循环，线程启动时进入一次
``lifespan_context``，把 task group 建起来并常驻；之后每个请求用
``run_coroutine_threadsafe`` 投进去执行。

效果：lifespan 只跑一次、session 在多个请求之间共享（实测
``initialize`` 拿到的 session id 能在后续 ``tools/list`` 里复用），
而调用方看到的只是一个普通的同步函数。

⚠️ 只支持**一次性响应**（``http.response.body`` 不带 ``more_body``）。
   本用途（JSON-RPC over HTTP）符合；真要长连接 SSE 得换 uvicorn。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

#: 单次请求的超时（秒）。MCP 调用都是本地的，几秒足够；
#: 卡住时要能返回错误而不是把服务线程占死。
CALL_TIMEOUT = 30.0


class AsgiHost:
    """在后台线程里常驻一个 ASGI app，并提供同步的 ``call()``。"""

    def __init__(self, app: Any, *, name: str = "asgi"):
        self.app = app
        self.name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    # ---------------------------------------------------------------- 生命周期

    def start(self, timeout: float = 15.0) -> None:
        """启动后台循环并进入 lifespan。抛异常表示起不来。"""
        if self._thread is not None:
            return

        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            stop = asyncio.Event()
            self._stop_event = stop

            async def boot() -> None:
                async with self.app.router.lifespan_context(self.app):
                    self._ready.set()
                    # 常驻：task group 活着，session 才能跨请求复用。
                    # ⚠️ 用**事件**而不是 loop.stop() —— 后者会把循环直接掐掉，
                    #    lifespan 的退出路径不执行，里面的后台任务
                    #    （sse_starlette 的 shutdown watcher 等）就变成
                    #    "Task was destroyed but it is pending!"。
                    #    停的时候要让它**正常退出 async with**。
                    await stop.wait()

            try:
                loop.run_until_complete(boot())
            except BaseException as exc:                  # noqa: BLE001
                # 起不来时把原因留住，让 start() 能报出来而不是静默超时
                self._error = exc
                self._ready.set()
            finally:
                try:
                    # 清掉残留任务，否则关解释器时会刷一堆警告
                    pending = [t for t in asyncio.all_tasks(loop)
                               if not t.done()]
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:                         # noqa: BLE001
                    pass
                finally:
                    try:
                        loop.close()
                    except Exception:                     # noqa: BLE001
                        pass

        self._thread = threading.Thread(target=run, name=self.name, daemon=True)
        self._thread.start()

        if not self._ready.wait(timeout=timeout):
            raise RuntimeError(f"{self.name}: lifespan 启动超时")
        if self._error is not None:
            raise RuntimeError(f"{self.name}: lifespan 启动失败：{self._error!r}")

    def stop(self, timeout: float = 5.0) -> None:
        """让 lifespan 正常退出，然后收线程。

        顺序很重要：**先请它退出**（唤醒 boot 里的 stop 事件，
        让 ``async with lifespan_context`` 走到 ``__aexit__``），
        再 join。直接 loop.stop() 会跳过清理，留下孤儿子任务。
        """
        loop, stop = self._loop, getattr(self, "_stop_event", None)
        if loop is None:
            return
        if stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ---------------------------------------------------------------- 请求

    async def _drive(self, scope: dict, body: bytes) -> list[dict]:
        sent: list[dict] = []
        done = asyncio.Event()
        state = {"sent": False}

        async def receive() -> dict:
            if not state["sent"]:
                state["sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            # ASGI 语义：给完 body 之后要挂住，直到响应结束。
            # 直接返回 disconnect 会让 app 以为客户端跑了。
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)
            if (message["type"] == "http.response.body"
                    and not message.get("more_body")):
                done.set()

        await self.app(scope, receive, send)
        return sent

    def call(self, scope: dict, body: bytes,
             timeout: float = CALL_TIMEOUT) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        """同步驱动一次 ASGI 请求，返回 ``(status, headers, body)``。"""
        if self._loop is None:
            raise RuntimeError(f"{self.name}: 还没 start()")
        fut = asyncio.run_coroutine_threadsafe(self._drive(scope, body), self._loop)
        sent = fut.result(timeout=timeout)

        status = 500
        headers: list[tuple[bytes, bytes]] = []
        chunks: list[bytes] = []
        for msg in sent:
            if msg["type"] == "http.response.start":
                status = int(msg.get("status", 500))
                headers = list(msg.get("headers") or [])
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b"") or b"")
        return status, headers, b"".join(chunks)
