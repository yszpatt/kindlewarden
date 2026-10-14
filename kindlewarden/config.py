"""配置加载。

用 TOML 而不是 YAML：Python 3.11+ 的 ``tomllib`` 是标准库，少一个依赖；
而配置是给人编辑的，需要注释 —— JSON 不合适。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class PanelsConfig:
    """板块开关。

    关掉的板块**不占版面空间**，空间自动回流给弹性区 —— 这是板块系统
    区别于「画布上摆几张卡片」的地方（架构文档 §4.2 否决了自由坐标布局）。
    """

    alerts: bool = True
    briefs: bool = True
    todos: bool = True
    ambient: bool = True
    statusbar: bool = True


@dataclass
class BoardConfig:
    max_briefs: int = 6
    max_todos: int = 8
    brief_retention_h: float = 24.0
    failed_retention_h: float = 72.0
    show_info: bool = True


@dataclass
class AmbientConfig:
    weather: str = ""
    # 见 panels.toml 中的详细说明。默认关闭是**有意的设计决策**：
    # 分钟级准确的时钟要求每分钟重渲染，与「不变就不刷」的省电模型直接冲突。
    show_clock: bool = False


@dataclass
class BrowserConfig:
    """Kindle 自带浏览器的可用视口（CSS 像素，已扣掉工具栏）。

    页面按这个尺寸算出图片的**确切像素大小**，靠 ``<img width= height=>`` 缩放，
    不依赖 CSS —— 老 WebKit 对替换元素上的 ``max-height:100%`` 解析不可靠，
    实测结果就是底部那行「更新于 HH:MM」被切掉。

    默认值按 PW3 推：屏幕 1072×1448，减去约 60px 的浏览器工具栏。
    如果设备上看到四周留白过多或被切边，在管理控制台的「设置」里调这两个数。
    """

    viewport_w: int = 1072
    viewport_h: int = 1388


@dataclass
class DisplayConfig:
    """显示模式：夜间 / 横屏。

    这两个功能**完全在服务端实现**，Kindle 侧不需要任何更新 ——
    这是刻意的设计约束，理由见下。

    为什么必须"服务端出图"而不是"设备侧变换"
    ----------------------------------------
    插件里 ``IMG_W, IMG_H = 1072, 1448`` 是**硬编码**的（它按这个尺寸算
    缩放比和点击坐标）。所以服务端**必须始终交付 1072×1448 的图**。

    这恰好决定了两种模式的实现方式：

    * **夜间模式** —— 渲染完把灰度反过来。尺寸不变，插件毫无察觉。
    * **横屏模式** —— 先按 **1448×1072**（宽>高）排版，再**旋转 90°**，
      得到 1072×1448。尺寸正好对上。

    代价是横屏时**用户要物理转动设备**（PW3 没有重力感应，
    KOReader 也不会替我们转）。但换来的是"零设备侧改动" ——
    不用重新打包部署插件，改配置就生效。

    为什么横屏不做成"设备侧旋转"
    ----------------------------
    KOReader 能转屏，但那要改插件、要重新部署，而且触摸坐标的换算会跟着
    变复杂（屏幕和图像不再同尺寸）。现在这条路上**只有服务端在变**，
    插件那份坐标换算代码一行都不用动。
    """

    #: 夜间模式：把画面反色（黑底白字），暗环境下不刺眼。
    night: bool = False

    #: 横屏：按宽>高排版后旋转 90° 输出。
    landscape: bool = False

    #: 横屏时旋转的方向。
    #:   "cw"  = 顺时针（画面顶部朝设备**右侧**，往右转设备看）
    #:   "ccw" = 逆时针（画面顶部朝设备**左侧**，往左转设备看）
    rotate: str = "cw"

    #: 横屏时「最近完成」的条数上限（``0`` = 不限，沿用 ``board.max_briefs``）。
    #:
    #: 为什么是**配置项**而不是代码里的常量
    #: ------------------------------------
    #: 这里最早写死过 4 条，结果是「管理页改了 max_briefs 设备上毫无反应」——
    #: 等于在配置之外偷偷加了第二个人管不着的上限。用户的原话：
    #: 「浪费空间且与后台配置不符合」。
    #:
    #: 所以横屏这个上限必须**看得见、改得动**：写在管理页的设置里、
    #: 存进 panels.toml。默认 5 —— 横屏单行条目 84px，弹性区最多放得下 6 条，
    #: 但 6 条会让版面发挤、没了呼吸，5 条是平衡点。
    landscape_max_briefs: int = 5


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8577
    heartbeat_min: float = 10.0
    poll_idle_s: int = 300
    poll_active_s: int = 30


@dataclass
class Config:
    panels: PanelsConfig = field(default_factory=PanelsConfig)
    board: BoardConfig = field(default_factory=BoardConfig)
    ambient: AmbientConfig = field(default_factory=AmbientConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    data_dir: Path = field(default_factory=lambda: default_data_dir())
    source_path: Path | None = None

    @property
    def state_path(self) -> Path:
        return self.data_dir / "board.jsonl"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "panels.toml"


def default_data_dir() -> Path:
    env = os.environ.get("KINDLEWARDEN_DATA")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "kindlewarden"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "kindlewarden"
    return Path.home() / ".local" / "state" / "kindlewarden"


def _fill(dc_type, raw: object):
    """用 dict 填充 dataclass，忽略未知键（配置写错不该炸掉服务）。"""
    if not isinstance(raw, dict):
        return dc_type()
    known = {f.name for f in fields(dc_type)}
    return dc_type(**{k: v for k, v in raw.items() if k in known})


def load(path: str | os.PathLike[str] | None = None) -> Config:
    """加载配置。文件不存在时返回默认值（首次运行不该失败）。"""
    cfg = Config()
    cfg.source_path = Path(path) if path else cfg.config_path

    if cfg.source_path.exists():
        with cfg.source_path.open("rb") as fh:
            raw = tomllib.load(fh)
        cfg.panels = _fill(PanelsConfig, raw.get("panels"))
        cfg.board = _fill(BoardConfig, raw.get("board"))
        cfg.ambient = _fill(AmbientConfig, raw.get("ambient"))
        cfg.browser = _fill(BrowserConfig, raw.get("browser"))
        cfg.display = _fill(DisplayConfig, raw.get("display"))
        cfg.server = _fill(ServerConfig, raw.get("server"))

    if os.environ.get("KINDLEWARDEN_DATA"):
        cfg.data_dir = Path(os.environ["KINDLEWARDEN_DATA"])

    return cfg


# ---------------------------------------------------------------- 管理页读写

# 管理页可以改哪些字段。
#
# 注意这里**刻意不含** server.host / server.port：socket 已经绑好了，
# 改了也不可能即时生效，放进可编辑列表只会让人以为改完就好了。
# 它们在页面上以只读形式展示，并标明「改完要重启」。
EDITABLE: dict[str, tuple[str, ...]] = {
    "panels": ("alerts", "briefs", "todos", "ambient", "statusbar"),
    "board": ("max_briefs", "max_todos", "brief_retention_h",
              "failed_retention_h", "show_info"),
    "ambient": ("weather", "show_clock"),
    "browser": ("viewport_w", "viewport_h"),
    "display": ("night", "landscape", "rotate", "landscape_max_briefs"),
    "server": ("heartbeat_min", "poll_idle_s", "poll_active_s"),
}

RESTART_REQUIRED = ("host", "port")


def _coerce(current, incoming):
    """按当前值的类型转换表单输入（表单来的都是字符串）。"""
    if isinstance(current, bool):
        if isinstance(incoming, str):
            return incoming.strip().lower() in ("1", "true", "yes", "on")
        return bool(incoming)
    if isinstance(current, int):
        return int(incoming)
    if isinstance(current, float):
        return float(incoming)
    return str(incoming)


def apply_patch(cfg: Config, patch: dict) -> list[str]:
    """把管理页提交的局部配置合并进 ``cfg``（就地修改），返回被改动的字段。

    就地修改是刻意的：``BoardService`` 持有同一个 ``cfg`` 对象，
    改完清一下渲染缓存就即时生效，不需要重启服务。
    """
    changed: list[str] = []
    for section, keys in EDITABLE.items():
        target = getattr(cfg, section, None)
        incoming = patch.get(section)
        if target is None or not isinstance(incoming, dict):
            continue
        for key in keys:
            if key not in incoming:
                continue
            try:
                value = _coerce(getattr(target, key), incoming[key])
            except (TypeError, ValueError):
                continue
            if getattr(target, key) != value:
                setattr(target, key, value)
                changed.append(f"{section}.{key}")
    return changed


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def to_toml(cfg: Config) -> str:
    """把配置序列化成 panels.toml。

    只写这个项目自己的扁平结构，所以没必要引入 TOML 写入库。
    """
    lines = [
        "# kindlewarden 配置文件",
        "#",
        "# 这个文件由管理页（http://<host>:<port>/）维护。",
        "# 手工编辑也可以，改完需要重启 `warden serve`。",
        "",
        "[panels]",
        "# 板块开关。关掉的板块不占版面空间，空间自动回流给弹性区。",
    ]
    for key in EDITABLE["panels"]:
        lines.append(f"{key} = {_toml_value(getattr(cfg.panels, key))}")

    lines += ["", "[board]",
              "# 简报列表的硬上限；超出部分在板上显示为「还有 N 条」",
              f"max_briefs = {_toml_value(cfg.board.max_briefs)}",
              "# 待办的硬上限",
              f"max_todos = {_toml_value(cfg.board.max_todos)}",
              "# 普通简报在板上停留的小时数",
              f"brief_retention_h = {_toml_value(cfg.board.brief_retention_h)}",
              "# failed 简报保留更久 —— 因为需要人处理",
              f"failed_retention_h = {_toml_value(cfg.board.failed_retention_h)}",
              "# 是否显示 info 状态的简报",
              f"show_info = {_toml_value(cfg.board.show_info)}"]

    lines += ["", "[ambient]",
              "# 例如 \"24°C 晴\"。留空则不显示天气槽位。",
              f"weather = {_toml_value(cfg.ambient.weather)}",
              "#",
              "# show_clock 默认关闭，这是有意的设计决策：",
              "# 分钟级准确的时钟要求每分钟重渲染，每天 1440 次全屏闪刷，",
              "# 与「不变就不刷」的省电模型直接冲突。",
              "# 打开后显示的是**渲染时刻**，最多滞后 heartbeat_min 分钟。",
              f"show_clock = {_toml_value(cfg.ambient.show_clock)}"]

    lines += ["", "[browser]",
              "# Kindle 自带浏览器的可用视口（CSS 像素，已扣掉工具栏）。",
              "# 页面按这个尺寸算出图片的确切像素大小，靠 <img width height> 缩放，",
              "# 不依赖 CSS —— 老 WebKit 对替换元素上的 max-height:100% 解析不可靠。",
              "# 设备上若四周留白过多就把数调大，若被切边就调小。",
              f"viewport_w = {_toml_value(cfg.browser.viewport_w)}",
              f"viewport_h = {_toml_value(cfg.browser.viewport_h)}"]

    lines += ["", "[display]",
              "# 夜间模式：画面反色（黑底白字），暗环境下不刺眼。",
              "# **纯服务端实现** —— 渲染完把灰度反过来，尺寸不变，",
              "# 所以 Kindle 侧一行都不用改。",
              f"night = {_toml_value(cfg.display.night)}",
              "# 横屏：先按 1448x1072（宽>高）排版，再旋转 90° 输出成 1072x1448。",
              "# 尺寸正好对上插件硬编码的画布，所以同样是纯服务端改动。",
              "# 代价：PW3 没有重力感应，**要物理转动设备**来看。",
              f"landscape = {_toml_value(cfg.display.landscape)}",
              '# 旋转方向："cw" 顺时针（往右转设备看）/"ccw" 逆时针。',
              f"rotate = {_toml_value(cfg.display.rotate)}",
              "# 横屏时「最近完成」最多显示几条（0 = 不限，用 board.max_briefs）。",
              "# 单行条目 84px，弹性区最多放得下 6 条；6 条会发挤，5 条是平衡点。",
              f"landscape_max_briefs = "
              f"{_toml_value(cfg.display.landscape_max_briefs)}",
              ""]

    lines += ["", "[server]",
              "# 监听地址与端口。**改动需要重启服务**，所以管理页里是只读的。",
              f"host = {_toml_value(cfg.server.host)}",
              f"port = {_toml_value(cfg.server.port)}",
              "# 状态不变时也强制重渲染一次，证明「还活着」",
              f"heartbeat_min = {_toml_value(cfg.server.heartbeat_min)}",
              "# 空闲时告诉 Kindle 多久后再来（秒）",
              f"poll_idle_s = {_toml_value(cfg.server.poll_idle_s)}",
              "# 有新内容时告诉 Kindle 多久后再来（秒）",
              f"poll_active_s = {_toml_value(cfg.server.poll_active_s)}",
              ""]
    return "\n".join(lines)


def save(cfg: Config) -> Path:
    """把当前配置写回 panels.toml。"""
    target = cfg.source_path or cfg.config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_toml(cfg), encoding="utf-8")
    return target
