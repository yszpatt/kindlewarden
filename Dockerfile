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
    KINDLEWARDEN_DATA=/data

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
RUN pip install --no-cache-dir -e . \
 && python -c "from kindlewarden.render import FontBook; \
fb = FontBook(); print('中文字体 OK:', fb.regular); print('粗体 OK:', fb.bold)"

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
