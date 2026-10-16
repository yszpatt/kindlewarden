# kindlewarden 副屏 Host
#
# 只跑**服务端**：渲染画面、存事件、提供 HTTP。
# Kindle 那边照旧只拉图，跟这个容器无关。
#
# 为什么用 `pip install -e .` 而不是 `pip install .`
# ------------------------------------------------
# `cli.py` 用 `Path(__file__).parent.parent` 定位仓库根目录，再去找
# `koplugin/` 和 `kindle/` —— `warden package` / `warden deploy` 要靠它们
# 生成设备侧的安装包。装成普通 wheel 的话包会进 site-packages，
# 那个相对路径就指向 site-packages，这两个命令会直接失败。
# 所以保留源码布局 + 可编辑安装：既拿到入口脚本和依赖，又保住路径。

FROM python:3.12-slim

# 中文字体 —— **这是镜像里唯一不能省的东西**。
#
# 渲染器要画中文。少了它服务能起来，但**一渲染就抛
# 「找不到可用的中文字体」**，而 Kindle 那边只是拉不到图。
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KINDLEWARDEN_DATA=/data \
    TZ=Asia/Shanghai

# ⚠️ ``TZ`` 是**必须**的，不是可选项。
#
# 容器默认跑在 UTC，而画面上所有时刻都由服务端渲染 ——
# 不设 TZ 的话，看板上的「更新于 10:10」会印成「02:10」，
# 比北京时间早 8 小时。实测踩过这个坑，而且**很难查**：
#   * 容器时钟是准的（HTTP Date 头与本机只差 1 秒）
#   * 时间戳也是标准 UTC 秒
#   * 只有"渲染成文字"那一步用了错的时区
#
# 除了 TZ，服务自己也有 ``display.timezone`` 配置项（优先级更高），
# 两条路都留着：TZ 管整个进程，配置项则可以在管理页里改、不用重建容器。
#
# 想要别的时区就改这里，或者设 ``display.timezone``。

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY kindlewarden ./kindlewarden
COPY koplugin ./koplugin
COPY kindle ./kindle
COPY skills ./skills
COPY tools ./tools

# 装依赖 + 入口脚本，**顺手把字体也验了**。
#
# ⚠️ 字体检查放在源码就位**之后**，而且调**真正的发现逻辑**，
#    不是 `test -f` 某个具体路径：各家发行版放 Noto CJK 的位置不一样
#    （opentype/noto、noto-cjk、语言子集 NotoSansCJKsc-*…），
#    写死路径换个基础镜像就失效。
#
#    让代码自己去认，并把选中的字体打出来 ——
#    缺字体在**构建时**炸掉，比等第一次渲染才发现好排查得多
#    （容器里没有交互终端，事后翻日志很痛苦）。
#
# ⚠️ **必须带 `[mcp]` extra。**
#
# 踩过一次，而且是容器里最难查的那种：CMD 带了 `--mcp`，但镜像是
# `pip install -e .` 装的 —— 那**只装基础依赖**，而 `mcp` 在
# pyproject 的 optional-dependencies 里。于是容器一起来就
# `ModuleNotFoundError: No module named 'mcp'`，进程退出，
# 而 compose 的 `restart: unless-stopped` 让它**无限重启**。
#
# 现象是"容器一直 restarting"、日志刷满 traceback，
# 而原因是构建时少写了一个 extra —— 两者看着毫无关系。
#
# 所以这里**同时验两件事**：
#   1. 装了 `[mcp]` extra 之后 mcp 能 import
#   2. 服务能真的带着 --mcp 构造出 MCP server（见下面 build_server 那行）
RUN pip install --no-cache-dir -e ".[mcp]" \
 && python -c "from kindlewarden.render import FontBook; \
fb = FontBook(); print('中文字体 OK:', fb.regular); print('粗体 OK:', fb.bold)" \
 && python -c "import mcp; print('MCP SDK OK')"

# 构建时**真的把 MCP 起一遍**，确认镜像能在 `--mcp` 下工作。
#
# ⚠️ 这条存在的理由：`import mcp` 成功**不等于** MCP 能用 ——
#    ASGI host、FastMCP 的 lifespan、Host 白名单都可能在这之后才炸。
#    而它们的失败会发生在**容器运行时**，被 restart 策略变成重启循环。
#    在这里起一次，失败就是**构建失败**：一眼能看到，也不会循环。
#
# 用临时目录 + 0 端口，跑完立刻停，不留下进程和文件。
RUN python -c "\
import tempfile; from pathlib import Path; \
from kindlewarden.config import Config; \
from kindlewarden.server import build_server; \
d = Path(tempfile.mkdtemp()); \
c = Config(); c.data_dir = d; c.source_path = d / 'panels.toml'; \
c.server.host = '127.0.0.1'; c.server.port = 0; \
s = build_server(c, enable_mcp=True); \
assert s.asgi_host is not None, 'MCP 没起来'; \
s.asgi_host.stop(); print('MCP 启动自检 OK')"

# 非 root 运行。数据目录必须**先建好并授权**，
# 否则挂载卷之后容器用户写不进去，现象是「服务起来但一条都存不下」。
RUN groupadd --system --gid 10001 warden \
 && useradd --system --uid 10001 --gid warden --create-home warden \
 && mkdir -p /data \
 && chown -R warden:warden /data /app

USER warden
VOLUME ["/data"]

EXPOSE 8577

# 用 python 做健康检查，省掉装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8577/health', timeout=3).status == 200 else 1)"

# 显式绑 0.0.0.0：只绑回环的话容器外永远连不上，
# 而现象只是「页面无法显示」，很难联想到是监听地址。
#
# `--mcp`：容器部署默认**同时提供 MCP**（POST /mcp，同一个端口）。
#
# 容器是跑 MCP 的理想位置 —— 它没有 exe、不用在每台终端装东西，
# 客户端配置里只写一个 URL。而这正是容器最省事的地方：
# 起一个容器，所有终端指过来就行。
#
# ⚠️ 但**必须同时设 KINDLEWARDEN_PUBLIC_HOST**，否则 /mcp 会返回 421。
#    原因见 mcp_server._default_allowed_hosts 的说明：
#    MCP 会校验 Host 头，而容器里自动探测到的是**容器自己的**地址
#    （172.x），不是客户端实际连的那个 —— 于是任何请求都对不上。
CMD ["warden", "serve", "--host", "0.0.0.0", "--port", "8577", "--mcp"]
