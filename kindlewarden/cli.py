"""命令行入口。

约定：**改状态走 HTTP，看状态可以本地做**。

- ``publish`` / ``todo`` / ``alert`` 等写操作 POST 到常驻服务
  （保证事件日志只有一个写入者，不需要跨进程文件锁）
- ``preview`` 是纯本地的：直接从事件日志渲染一张 PNG，
  用来在碰设备之前反复调版面
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import Config, load as load_config

DEFAULT_BASE = "http://127.0.0.1:8577"


# ---------------------------------------------------------------- HTTP 客户端


def _post(cfg: Config, path: str, payload: dict) -> dict:
    base = f"http://127.0.0.1:{cfg.server.port}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"服务返回 {exc.code}：{detail}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"连不上副屏服务（{base}）：{exc.reason}\n"
            f"先运行： warden serve"
        ) from None


def _get(cfg: Config, path: str) -> dict:
    base = f"http://127.0.0.1:{cfg.server.port}"
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"连不上副屏服务（{base}）：{exc.reason}") from None


# ---------------------------------------------------------------- 默认配置

DEFAULT_CONFIG_TOML = """\
# kindlewarden 配置文件
#
# 改完需要重启 `warden serve`。

[panels]
# 板块开关。关掉的板块**不占版面空间**，空间自动回流给弹性区。
alerts    = true
briefs    = true
todos     = true
ambient   = true
statusbar = true

[board]
max_briefs         = 6      # 简报列表的硬上限
max_todos          = 8
brief_retention_h  = 24     # 普通简报在板上停留的小时数
failed_retention_h = 72     # failed 简报保留更久 —— 因为需要人处理
show_info          = true   # 是否显示 info 状态的简报

[ambient]
weather = ""       # 例如 "24°C 晴"。留空则不显示天气槽位。

# show_clock 默认关闭，这是**有意的设计决策**。
#
# ambient 里的「当前时间」与「不做动态元素」直接冲突：一个分钟级准确的时钟
# 要求每分钟重新渲染并刷新屏幕 —— 每天 1440 次全屏闪刷，既费电又伤眼，
# 也与「不变就不刷」的省电模型相悖。
#
# 打开后，显示的时间是**渲染时刻**（最多滞后 heartbeat_min 分钟）。
# 看板底部始终有「更新于 HH:MM」，那是一个永远为真的快照事实，
# 不需要靠频繁刷新来维持诚实。
show_clock = false

[server]
host          = "0.0.0.0"
port          = 8577
heartbeat_min = 10    # 状态不变时也强制重渲染一次，证明"还活着"
poll_idle_s   = 300   # 空闲时告诉 Kindle 多久后再来
poll_active_s = 30    # 有新内容时告诉 Kindle 多久后再来
"""


# ---------------------------------------------------------------- 子命令


def cmd_init(args: argparse.Namespace, cfg: Config) -> int:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if cfg.config_path.exists() and not args.force:
        print(f"配置已存在：{cfg.config_path}（用 --force 覆盖）")
        return 0
    cfg.config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    print(f"已写入 {cfg.config_path}")
    print(f"事件日志将存放在 {cfg.state_path}")
    return 0


def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    from .server import serve
    serve(cfg, enable_mcp=bool(getattr(args, "mcp", False)))
    return 0


def cmd_publish(args: argparse.Namespace, cfg: Config) -> int:
    payload = {
        "title": args.title,
        "status": args.status,
        "summary": args.summary or "",
        "project": args.project or "",
        "duration_s": args.duration or 0,
    }
    if args.supersedes:
        payload["supersedes"] = args.supersedes
    if args.artifact:
        payload["artifacts"] = [{"label": a} for a in args.artifact]
    out = _post(cfg, "/brief", payload)
    print(f"已发布 {out.get('brief_id')}：{out.get('title')}")
    if out.get("redactions"):
        print(f"  ⚠ 命中敏感模式并已打码：{', '.join(out['redactions'])}", file=sys.stderr)
    if out.get("truncated"):
        print("  ⚠ 内容超长，已按版面截断", file=sys.stderr)
    return 0


def cmd_todo(args: argparse.Namespace, cfg: Config) -> int:
    if args.done:
        out = _post(cfg, "/todo/toggle", {"id": args.done, "done": True})
        print(f"已完成 {out.get('todo_id')}")
        return 0
    if args.undone:
        out = _post(cfg, "/todo/toggle", {"id": args.undone, "done": False})
        print(f"已重开 {out.get('todo_id')}")
        return 0
    if args.remove:
        out = _post(cfg, "/todo/remove", {"id": args.remove})
        print(f"已删除 {out.get('todo_id')}")
        return 0

    if args.edit:
        # warden todo --edit <id> 新文字...
        new_text = " ".join(args.text or [])
        if not new_text:
            raise SystemExit("要改待办请给出新文字：warden todo --edit <id> 新文字")
        board = _get(cfg, "/board")
        old = next((t for t in board["todos"] if t["id"] == args.edit), None)
        if old is None:
            # 报错时把现有的列出来，省得用户再跑一次 list
            print(f"没有 id={args.edit} 的待办。现有：")
            for t in board["todos"]:
                mark = "☑" if t["done"] else "☐"
                print(f"  {mark} {t['id']}  {t['text']}")
            return 1
        out = _post(cfg, "/todo/update", {"id": args.edit, "text": new_text})
        print(f"已修改 {out.get('todo_id')}")
        print(f"  原：{old['text']}")
        print(f"  新：{out.get('text')}")
        return 0

    if not args.text:
        board = _get(cfg, "/board")
        if not board["todos"]:
            print("待办为空")
            return 0
        for t in board["todos"]:
            mark = "☑" if t["done"] else "☐"
            print(f"  {mark} {t['id']}  {t['text']}")
        return 0
    out = _post(cfg, "/todo", {"text": " ".join(args.text)})
    print(f"已添加 {out.get('todo_id')}")
    return 0


def cmd_alert(args: argparse.Namespace, cfg: Config) -> int:
    if args.clear:
        _post(cfg, "/alert/clear", {"id": args.clear if args.clear is not True else ""})
        print("已清除告警")
        return 0
    if not args.title:
        raise SystemExit("需要提供告警标题，或用 --clear 清除")
    out = _post(cfg, "/alert", {"title": args.title, "detail": args.detail or ""})
    print(f"已上警报 {out.get('alert_id')}")
    return 0


def cmd_brief(args: argparse.Namespace, cfg: Config) -> int:
    if args.pin:
        _post(cfg, "/pin", {"id": args.pin, "pinned": True})
        print(f"已钉住 {args.pin}")
        return 0
    if args.unpin:
        _post(cfg, "/pin", {"id": args.unpin, "pinned": False})
        print(f"已取消钉住 {args.unpin}")
        return 0
    if args.retract:
        _post(cfg, "/retract", {"id": args.retract})
        print(f"已撤回 {args.retract}")
        return 0
    board = _get(cfg, "/board")
    if not board["briefs"]:
        print("板上还没有简报")
        return 0
    for b in board["briefs"]:
        pin = "📌" if b["pinned"] else "  "
        print(f"  {pin} [{b['status']:7}] {b['id']}  {b['title']}")
    return 0


def cmd_board(args: argparse.Namespace, cfg: Config) -> int:
    if args.json:
        print(json.dumps(_get(cfg, "/board"), ensure_ascii=False, indent=2))
        return 0
    board = _get(cfg, "/board")
    print(f"rev {board['rev']}  健康度 {board['source_health']}")
    if board["alert"]:
        print(f"  ⚠ 告警：{board['alert']['title']}")
    print(f"  简报 {board['total_briefs']} 条 / 待办 {board['total_todos']} 项")
    return 0


def cmd_package(args: argparse.Namespace, cfg: Config) -> int:
    """把 Kindle 侧客户端打包成可直接拷进 Kindle 的两级目录。

    产出结构**和设备上的实际落点一一对应**，避免「哪个文件放哪儿」搞错：

        dist/kindlewarden/
            extensions/kindlewarden/    →  <Kindle>\\extensions\\kindlewarden\\
            documents/                  →  <Kindle>\\documents\\

    做三件手工操作最容易出错的事：
      1. 把服务端地址写进 config.sh（省掉最容易填错的一步）
      2. **统一换行符为 LF** —— Windows 上编辑出来的 CRLF 会让 Kindle 上的
         shell 脚本直接报 ``bad interpreter``，而且报错很难看出原因
      3. 以 UTF-8 无 BOM 写出 —— BOM 会被 shell 当成命令的一部分

    为什么有 ``documents/``：启动靠的是 **Scriptlet**（WinterBreak 自带的
    SH_Integration）。官方已明确 **KUAL 废弃**（"KUAL ... is now obsolete and
    does not work"），现在把 ``.sh`` 放进 ``documents/`` 就会作为「书」出现在
    书库，点一下即运行。``extensions/`` 只用来放客户端本体，免得每个脚本
    都被索引成一本书。
    """
    server = args.server.rstrip("/")
    if not server.startswith(("http://", "https://")):
        server = "http://" + server

    root = Path(__file__).resolve().parent.parent
    src = root / "kindle"
    if not src.is_dir():
        raise SystemExit(
            f"找不到 Kindle 客户端源目录：{src}\n"
            "（这个命令需要从源码检出里运行）"
        )

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    ext_out = out / "extensions" / "kindlewarden"
    doc_out = out / "documents"
    ext_out.mkdir(parents=True, exist_ok=True)
    doc_out.mkdir(parents=True, exist_ok=True)

    def write_lf(text: str, target: Path) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        target.write_text(text, encoding="utf-8", newline="\n")

    # 客户端本体
    for name in ("warden.sh", "config.sh"):
        write_lf((src / name).read_text(encoding="utf-8"), ext_out / name)

    ext_src = src / "extensions" / "kindlewarden"
    for item in sorted(ext_src.iterdir()):
        if item.is_file():
            write_lf(item.read_text(encoding="utf-8"), ext_out / item.name)

    # Scriptlet 启动器（放进 documents/，会变成书库里的「书」）
    for name in ("scriptlet-start.sh", "scriptlet-stop.sh"):
        s = src / name
        if not s.exists():
            continue
        # 设备上的文件名 = 书库里的条目名，用 ASCII 免得索引出乱码
        target = doc_out / (name.replace("scriptlet-", "kindlewarden-"))
        write_lf(s.read_text(encoding="utf-8"), target)

    # ---- KOReader 插件（首选路径）
    #
    # 和 shell 客户端并列打包，因为两者落点不同。插件是现在的主线：
    # shell 那套要靠不断重绘去和 framework 抢屏幕，而且触摸会穿透底下的书；
    # KOReader 独占屏幕，两件事都天然成立。
    plugin_out = out / "koreader" / "plugins" / "kindlewarden.koplugin"
    plugin_src = root / "koplugin" / "kindlewarden.koplugin" / "main.lua"
    plugin_written = False
    if plugin_src.exists():
        plugin_out.mkdir(parents=True, exist_ok=True)
        lua_text = plugin_src.read_text(encoding="utf-8")
        # 把服务端地址烘进去 —— 和 config.sh 是同一个目的：省掉最容易填错的一步
        lua_text, n_sub = re.subn(
            r'(?m)^(local DEFAULT_SERVER = )"[^"]*"',
            lambda m: m.group(1) + f'"{server}"', lua_text)
        if n_sub != 1:
            raise SystemExit(
                "打包插件时没能在 main.lua 里定位 DEFAULT_SERVER —— "
                "插件结构变了，请同步更新 cmd_package")
        write_lf(lua_text, plugin_out / "main.lua")
        plugin_written = True

    # 覆盖 config.sh 里的默认地址
    cfg_path = ext_out / "config.sh"
    cfg_text = cfg_path.read_text(encoding="utf-8")
    cfg_text = re.sub(r'(?m)^WARDEN_SERVER=.*$',
                      f'WARDEN_SERVER="{server}"', cfg_text)
    write_lf(cfg_text, cfg_path)

    # 自检：CRLF 会让 shell 脚本在设备上直接报 bad interpreter；
    # BOM 会被当成命令的一部分。两者都只在设备上暴露，所以打包时就要查。
    problems = []
    checked = list(ext_out.iterdir()) + list(doc_out.iterdir())
    if plugin_written:
        checked += list(plugin_out.iterdir())
    for p in sorted(checked):
        if not p.is_file():
            continue
        raw = p.read_bytes()
        crlf = sum(1 for i in range(1, len(raw)) if raw[i] == 10 and raw[i - 1] == 13)
        bom = raw[:3] == b"\xef\xbb\xbf"
        if crlf or bom:
            problems.append(f"{p.name} (CRLF={crlf} BOM={bom})")

    # ⚠️ 自检：产物里的插件必须和**源文件**内容一致（除服务端地址那行）。
    #
    # 踩过一次：改了插件源码却忘了重新 package，直接跑 deploy ——
    # deploy 从 dist/ 拷贝，于是**设备上装的是旧版本**，
    # 而所有输出都显示"✓ 部署成功"。我据此以为改动生效了，
    # 实际白忙一场（排查了一轮"为什么新功能没出现"）。
    #
    # 这类问题的共同点：**成功消息不区分"拷过去了"和"拷的是新的"**。
    # 所以在这里比一次内容，不一致就明确报出来。
    if plugin_written:
        src_plugin = root / "koplugin" / "kindlewarden.koplugin" / "main.lua"
        if src_plugin.exists():
            src_text = src_plugin.read_text(encoding="utf-8")
            # 打包时会替换 DEFAULT_SERVER 那一行，所以比之前先做同样的替换
            import re as _re
            src_normalized = _re.sub(
                r'(?m)^(local DEFAULT_SERVER = )"[^"]*"',
                lambda m: m.group(1) + f'"{server}"', src_text)
            dist_text = (plugin_out / "main.lua").read_text(encoding="utf-8")
            if src_normalized != dist_text:
                problems.append(
                    "插件产物与源文件不一致（dist 里是旧版本 —— "
                    "需要重新 package）")

    print(f"已打包到 {out}")
    print(f"  服务端地址：{server}")
    if problems:
        print(f"  ⚠ 换行符/BOM 自检不通过：{', '.join(problems)}")
    else:
        print(f"  自检：{len([p for p in checked if p.is_file()])} 个文件，"
              "全部 LF 换行、无 BOM  ✓")
    print()
    if plugin_written:
        print("部署（推荐：KOReader 插件）")
        print(f"  1. {out}\\koreader\\plugins\\kindlewarden.koplugin\\")
        print("     →  <Kindle盘符>\\koreader\\plugins\\kindlewarden.koplugin\\")
        print("  2. 安全弹出，拔线，重启 KOReader")
        print("  3. KOReader 菜单 → 工具 → kindlewarden 副屏")
        print()
        print("  （KOReader 独占屏幕，不跟 framework 抢，触摸也不会穿透。）")
    print()
    print("部署（备选：shell + fbink，需要 framework 还活着）")
    print(f"  1. {out}\\extensions\\kindlewarden\\  →  <Kindle盘符>\\extensions\\kindlewarden\\")
    print(f"  2. {out}\\documents\\ 里的 .sh        →  <Kindle盘符>\\documents\\")
    print("  3. 书库里会出现「kindlewarden 副屏」，点一下即运行")
    print()
    print("  （不需要 KUAL —— 官方已废弃它，现在用 Scriptlet。）")
    print()
    print("详见 docs/kindle-deploy.md")
    return 1 if problems else 0


def _try_connect(host: str, port: int, timeout: float = 1.5) -> tuple[bool, str]:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _firewall_rules_from_registry() -> list[str] | None:
    """从注册表读出所有防火墙规则（原始字符串）。None 表示读不到。

    ⚠️ **这是唯一可靠的判据** —— 不要改用 ``Get-NetFirewallRule``。

    踩过一次：沙箱/受限环境里 ``Get-NetFirewallRule`` 会返回**空集**
    而不报错（实测 ``@(Get-NetFirewallRule).Count`` 是 0，
    而注册表里有 776 条规则）。基于它做的任何判断都是假的 ——
    「没有规则」和「读不到规则」分不清，于是 `doctor` 会谎报
    「没有防火墙放行规则」，把一个配置正确的人引去瞎折腾。

    ``netsh`` 在同样环境下也可能什么都不返回。

    注册表读取不需要提权、也不经过 WMI。

    ⚠️ 路径**不能带 hive 前缀**（不要写成 ``HKLM`` 加反斜杠再加 ``SYSTEM``）。

    ``winreg.OpenKey`` 的第一个参数已经是 ``HKEY_LOCAL_MACHINE`` 了，
    再在路径里写一遍 hive 名，就变成找"HKLM 键下面的 SYSTEM"——
    而 HKLM 下面并没有一个叫 HKLM 的子键，于是直接抛 OSError。

    这个 bug **原本就潜伏在 `cmd_firewall` 里**（同一段代码是从那儿提出来的）：
    它 catch 了 OSError 就返回「没有规则」，所以现象是
    **`warden firewall` 永远认为规则不存在**、每次都重建一遍。
    隐藏了很久是因为"重复建一条同名规则"本身不报错。
    """
    if os.name != "nt":
        return None
    key = (r"SYSTEM\CurrentControlSet\Services\SharedAccess"
           r"\Parameters\FirewallPolicy\FirewallRules")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            _, n, _ = winreg.QueryInfoKey(k)
            out: list[str] = []
            for i in range(n):
                try:
                    _name, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    continue
                out.append(str(val))
            return out
    except OSError:
        return None


def _firewall_rule_exists(name: str = "kindlewarden",
                          port: int | None = None) -> bool | None:
    """查防火墙规则。返回 None 表示查不了（非 Windows 或没有权限）。

    判据顺序：**注册表优先**，netsh 只作兜底。

    为什么注册表优先：它是这三个途径里唯一在受限环境下还靠谱的
    （见 ``_firewall_rules_from_registry`` 的说明）。
    以前这里只查 netsh —— 于是在沙箱里 `doctor` 会谎报「没有放行规则」，
    而实际上规则好端端地在注册表里。
    """
    rules = _firewall_rules_from_registry()
    if rules:
        for val in rules:
            if name in val:
                if port is None or f"LPort={port}" in val:
                    return True
        return False

    # 注册表读不到（权限/非 Windows）→ 退回 netsh
    import subprocess
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
            capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return False
    # netsh 找不到规则时返回 0 但输出「没有与指定条件相符的规则」
    return not any(k in out.stdout for k in ("没有与指定条件相符", "No rules match"))


def cmd_install_service(args: argparse.Namespace, cfg: Config) -> int:
    """把服务注册成 Windows 计划任务：**开机自启，独立于任何终端**。

    为什么需要这个
    --------------
    ``warden serve`` 是**前台阻塞**的 —— 谁启动它，它就挂在谁身上：

      * 在终端里跑 → 关掉窗口它就死
      * 在某个会话里跑 → 会话结束它就死
      * 被调试器/包装器启动 → 包装器退出它可能被连带带走

    而副屏必须 7×24 可用：服务一停，Kindle 就永远停在旧画面上，
    而且**没有任何提示** —— 用户只会觉得"副屏坏了"。

    实测就吃过这个亏：服务在会话里跑着，中途静默退出（exit 1，无输出），
    Kindle 端随即失联。所以做成**计划任务**，让 Windows 自己管它。
    """
    import shutil as _shutil
    import subprocess as _sp
    import sys as _sys

    python = _sys.executable
    repo = Path(__file__).resolve().parent.parent
    task = getattr(args, "name", None) or "kindlewarden"

    # 计划任务要跑的命令：直接调 python，并带上工作目录
    cmd = f'"{python}" -m kindlewarden.cli serve'

    print("kindlewarden 服务安装")
    print()
    print(f"  任务名    {task}")
    print(f"  命令      {cmd}")
    print(f"  工作目录  {repo}")
    print()

    if os.name != "nt":
        print("  这个命令只针对 Windows。")
        print("  其它平台建议用 systemd / launchd：")
        print(f"    ExecStart={cmd}")
        return 1

    if _shutil.which("schtasks") is None:
        print("  找不到 schtasks —— 无法注册计划任务。")
        print("  请手动把下面这条命令加到自启里：")
        print(f"    cd /d {repo} && {cmd}")
        return 1

    # ⚠️ 用 PowerShell 的 Register-ScheduledTask，**不要用 schtasks**。
    #
    # schtasks 的 /TR 参数对引号极其挑剔：命令里既要引 Python 路径、
    # 又要引工作目录，实测直接报 "The system cannot find the path specified"，
    # 而且它不会告诉你是哪个引号的问题。
    #
    # PowerShell 的 cmdlet 接受"可执行文件 + 参数"分离的写法，
    # 完全没有引号嵌套问题，而且还能顺便设工作目录。
    ps = _shutil.which("powershell") or _shutil.which("pwsh")
    if ps is None:
        print("  找不到 PowerShell，无法注册计划任务。")
        return 1

    # 用 New-ScheduledTask* 系列构造，动作里 exe 和参数分开写。
    #
    # ⚠️ **必须用 try/catch + 显式标记成功。**
    #
    # 踩过一次：脚本末尾写 `Write-Output 'OK'`，而 PowerShell 的 cmdlet
    # 失败默认**不终止**执行 —— 于是 Register-ScheduledTask 明明失败了，
    # 后面那行照样打印 OK，我据此报告"✓ 已注册"，
    # 而实际上 `Get-ScheduledTask` 返回 NOT FOUND。
    #
    # 这跟之前 schtasks 那次是同一类错误：**没真正检查退出状态**。
    # 现在改成：成功才输出 OK，失败走 catch 输出 FAIL + 原因。
    script = (
        f"$ErrorActionPreference = 'Stop';"
        f"try {{"
        f"  $exe = '{python}';"
        f"  $arg = '-m kindlewarden.cli serve';"
        f"  $wd  = '{repo}';"
        f"  $act = New-ScheduledTaskAction -Execute $exe -Argument $arg "
        f"-WorkingDirectory $wd;"
        f"  $trg = New-ScheduledTaskTrigger -AtLogOn;"
        # 允许在电池上跑、不因空闲而停 —— 这是个常驻服务
        f"  $set = New-ScheduledTaskSettingsSet "
        f"-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        f"-ExecutionTimeLimit ([TimeSpan]::Zero) "
        f"-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1);"
        f"  Register-ScheduledTask -TaskName '{task}' "
        f"  -Action $act -Trigger $trg -Settings $set "
        f"  -Description 'kindlewarden 副屏服务' -Force | Out-Null;"
        f"  Write-Output 'OK'"
        f"}} catch {{"
        f"  Write-Output ('FAIL: ' + $_.Exception.Message)"
        f"}}"
    )

    print("  正在注册（用 PowerShell 的计划任务 cmdlet）…")
    proc = _sp.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    if "OK" not in out:
        print("  ✗ 注册失败：")
        for line in out.splitlines():
            if line.strip():
                print("    " + line.strip()[:110])
        print()
        print("  最常见的原因是权限不够。用**管理员**身份重跑：")
        print(f"    warden install-service")
        print()
        print("  或者手动把下面这条命令加到系统的启动项里：")
        print(f"    cd /d {repo} && {cmd}")
        return 1

    print("  ✓ 已注册")

    # 立刻启动一次，省得等到下次登录。
    # 同样要检查真实结果 —— 端口被占时任务会"启动成功"但服务起不来。
    started = _sp.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command",
         f"Start-ScheduledTask -TaskName '{task}' -ErrorAction Stop;"
         f"Write-Output 'OK'"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if "OK" in ((started.stdout or "") + (started.stderr or "")):
        print("  ✓ 已触发启动")
    else:
        print("  （触发启动失败，下次登录时会自己起来）")

    print()
    print("验证：")
    print(f"  1. 打开 http://127.0.0.1:{cfg.server.port}/  看控制台是否响应")
    print("  2. warden doctor          确认设备可达")
    print()
    print("管理（PowerShell）：")
    print(f"  Start-ScheduledTask  -TaskName {task}    立即启动")
    print(f"  Stop-ScheduledTask   -TaskName {task}    停止")
    print(f"  Get-ScheduledTask    -TaskName {task}    查看状态")
    print(f"  warden uninstall-service                 卸载")
    return 0


def cmd_uninstall_service(args: argparse.Namespace, cfg: Config) -> int:
    """卸载上面注册的计划任务。"""
    import shutil as _shutil
    import subprocess as _sp

    task = getattr(args, "name", None) or "kindlewarden"
    print(f"卸载计划任务 {task}")

    ps = _shutil.which("powershell") or _shutil.which("pwsh")
    if ps is None:
        print("  找不到 PowerShell")
        return 1

    # 先停掉再删，避免留下在跑的实例
    script = (
        f"Stop-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue;"
        f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false "
        f"-ErrorAction Stop;"
        f"Write-Output 'OK'"
    )
    proc = _sp.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace")
    if "OK" in (proc.stdout or ""):
        print("  ✓ 已卸载")
        return 0

    print("  （没有找到这个任务，或者删除失败）")
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip():
            print("    " + line[:110])
    return 1


def cmd_firewall(args: argparse.Namespace, cfg: Config) -> int:
    """给服务加一条 Windows 防火墙入站放行规则。

    为什么需要这个命令
    ------------------
    实测踩过：**服务在本机一切正常，Kindle 却死活连不上。**

        netstat:  0.0.0.0:8577 LISTENING      ← 绑定正确
        netsh:    没有任何针对 8577 的入站规则  ← 走默认动作 = 阻止

    Windows 防火墙默认**阻止入站**。没有放行规则时：
      * 本机访问 ``127.0.0.1`` 正常（走回环，不经防火墙）
      * **局域网里其它设备全部连不上**

    这个陷阱特别阴：``warden doctor`` 会说"服务在跑"，
    浏览器打开 ``127.0.0.1`` 也一切正常，只有真正的外部设备连不上 ——
    而日志里只会看到设备侧的"服务不可达"。

    而且它**不是一次配好就永久有效**：Windows 会在网络配置文件
    重新分类（专用↔公用）后改变行为，所以"昨天还能用今天就断了"
    是这个问题的典型表现。

    为什么用 UAC 提权而不是直接失败
    ------------------------------
    加防火墙规则必须管理员权限。直接报"权限不够"要用户自己去翻
    PowerShell、抄一条长命令 —— 太容易出错。
    这里用 ``Start-Process -Verb RunAs`` 弹一个 UAC 确认框，
    用户点一下"是"就行。
    """
    import subprocess as _sp

    port = args.port or cfg.server.port
    name = f"kindlewarden ({port})"
    profile = "Any" if args.any_profile else "Private"

    print("kindlewarden 防火墙放行")
    print()
    print(f"  规则名  {name}")
    print(f"  端口    TCP {port} 入站")
    # ⚠️ 这里要跟着参数走。踩过一次：加了 --any-profile 但提示还写着
    #    「专用网络」，让人以为参数没生效。
    if profile == "Any":
        print("  范围    **所有网络**（专用 + 公用）")
        print("          —— 不确定自己的网络属于哪类时用这个")
    else:
        print("  范围    专用网络 (Private)")
        print("          —— 如果当前网络被归为「公用」，这条不生效，")
        print("             那就加 --any-profile 重建")
    print()

    if os.name != "nt":
        print("  这个命令只针对 Windows。")
        print("  其它平台请自行放行入站端口。")
        return 1

    # 判断"是否已有规则"统一走 `_firewall_rule_exists`（注册表优先）。
    #
    # 这里以前是**另一份实现**（嵌套函数），和 `doctor` 各查各的 ——
    # 而两份用的判据还不一样（这份查注册表、`doctor` 查 netsh），
    # 于是同一条规则两个命令结论相反。
    # 现在只有一处实现，不会再出现"一个说有、一个说没有"。
    def _rule_exists() -> bool:
        return _firewall_rule_exists(name="kindlewarden", port=port) is True
        return False

    if _rule_exists() and not args.force:
        print(f"  ✓ 已经有放行 {port} 的规则了。")
        print("    （想重建加 --force）")
        return 0

    if args.remove:
        print("  正在删除（会弹 UAC）…")
    else:
        print("  正在添加（会弹 UAC，点「是」即可）…")

    # ⚠️ **必须检查内层脚本的真实结果，不能只看外层进程的退出码。**
    #
    # 踩过一次：`Start-Process -Verb RunAs -Wait` 成功启动了提权的
    # PowerShell（所以外层退出码是 0），但**内层脚本失败了** ——
    # 我却报告"✓ 完成"，而实际上规则根本没建上。
    #
    # 这和处理 schtasks / install-service 时犯的是同一类错误：
    # **把"命令跑起来了"当成"事情做成了"。**
    #
    # 修法：让提权的进程把结果**写到一个临时文件**，外层读文件判断。
    # 这样才是真的检查了结果，而不是检查了"有没有启动"。
    import base64
    import tempfile

    result_file = Path(tempfile.gettempdir()) / f"kindlewarden-fw-{os.getpid()}.txt"
    try:
        result_file.unlink()
    except OSError:
        pass

    result_literal = str(result_file).replace("'", "''")
    inner = (
        f"$ErrorActionPreference='Stop';"
        f"try {{"
        f"  New-NetFirewallRule -DisplayName '{name}' "
        f"  -Direction Inbound -Protocol TCP -LocalPort {port} "
        f"  -Action Allow -Profile {profile} | Out-Null;"
        f"  Set-Content -Path '{result_literal}' -Value 'OK' -Encoding UTF8"
        f"}} catch {{"
        f"  Set-Content -Path '{result_literal}' "
        f"  -Value ('FAIL: ' + $_.Exception.Message) -Encoding UTF8"
        f"}}"
    ) if not args.remove else (
        f"$ErrorActionPreference='Stop';"
        f"try {{"
        f"  Remove-NetFirewallRule -DisplayName '{name}' -ErrorAction Stop;"
        f"  Set-Content -Path '{result_literal}' -Value 'OK' -Encoding UTF8"
        f"}} catch {{"
        f"  Set-Content -Path '{result_literal}' "
        f"  -Value ('FAIL: ' + $_.Exception.Message) -Encoding UTF8"
        f"}}"
    )

    encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    runner = (
        f"$p = Start-Process -FilePath 'powershell' "
        f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{encoded}' "
        f"-Verb RunAs -Wait -PassThru;"
        f"Write-Output ('EXIT=' + $p.ExitCode)"
    )
    r = _sp.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", runner],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = ((r.stdout or "") + (r.stderr or "")).strip()

    # **读内层进程写的结果**，而不是外层退出码
    inner_result = ""
    if result_file.exists():
        try:
            # ⚠️ 用 utf-8-sig 读：PowerShell 的 Set-Content -Encoding UTF8
            #    会**带 BOM**，直接 utf-8 读出来开头会多一个 \ufeff，
            #    于是 `== "OK"` 判断失败 —— 明明成功却报失败。
            inner_result = result_file.read_text(encoding="utf-8-sig").strip()
        except OSError:
            pass
        try:
            result_file.unlink()
        except OSError:
            pass

    # 最终判据用**注册表**（不依赖 WMI，不受沙箱影响）
    made_it = _rule_exists()

    if made_it:
        print("  ✓ 完成（已在注册表里确认规则存在）")
    elif inner_result.startswith("OK"):
        # 脚本说成功但注册表里看不到 —— 可能是延迟，也可能判断有问题
        print("  ? 脚本报告成功，但注册表里暂时没看到规则。")
        print("    稍等几秒再看，或者用手机/Kindle 实测一下。")
    else:
        print("  ✗ 失败：")
        if inner_result:
            print("    " + inner_result[:140])
        else:
            for line in out.splitlines():
                if line.strip():
                    print("    " + line.strip()[:110])
            print("    （提权进程没有留下结果 —— UAC 可能被取消了）")
        print()
        print("  手动执行：以**管理员**身份打开 PowerShell，跑：")
        print(f"    New-NetFirewallRule -DisplayName '{name}' "
              f"-Direction Inbound -Protocol TCP -LocalPort {port} "
              f"-Action Allow -Profile Private")
        return 1

    print()
    print("验证：")
    print(f"  1. 在**另一台设备**（手机/Kindle）上打开 http://<本机IP>:{port}/check")
    print("     —— 本机浏览器打开是不算数的，它走回环，不经过防火墙")
    print("  2. warden doctor")
    print()
    print("注意：Windows 在网络配置文件重新分类后可能改变行为。")
    print("      如果哪天又连不上，先重跑一次这个命令看看。")
    return 0


def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    """诊断「Kindle 连不上副屏服务」这类问题。

    这类故障的现象只是「页面无法显示」，很难联想到真正的原因。
    最常见的两种：服务只绑了 127.0.0.1、防火墙拦了入站。
    """
    from .server import lan_addresses

    port = args.port or cfg.server.port
    problems: list[str] = []

    print("kindlewarden 自检")
    print()

    # ---- 1. 服务在不在
    local_ok, local_err = _try_connect("127.0.0.1", port)
    if local_ok:
        print(f"  [ OK ] 服务在跑（127.0.0.1:{port}）")
    else:
        print(f"  [FAIL] 本机连不上 {port} 端口 —— 服务没启动？")
        print(f"         {local_err}")
        print(f"         启动： warden serve")
        return 1

    # ---- 1.5 时区（部署在容器里时最容易错的一项）
    #
    # 为什么放在这么靠前：它**不影响连通性**，所以不会被别的检查发现，
    # 而现象是"看板上的时刻差 8 小时"—— 很难往时区上想。
    # 容器默认 UTC，配了 display.timezone 才是对的。
    from .render import current_timezone, set_timezone
    from . import render as _render

    _render.set_timezone(cfg.display.timezone)
    tz_now = current_timezone()
    sample = _render.format_clock(time.time())
    if cfg.display.timezone:
        if "取不到" in tz_now:
            print(f"  [WARN] 时区 {cfg.display.timezone!r} 解析不了，"
                  f"现在按本机时区显示（{sample}）")
            print(f"         装 tzdata，或改用固定偏移（如 \"+08:00\"）")
        else:
            print(f"  [ OK ] 时区 {tz_now}（现在 {sample}）")
    else:
        print(f"  [ -- ] 时区未配置，跟随进程本地（{tz_now}，现在 {sample}）")
        print(f"         容器部署**必须**设 display.timezone，"
              f"否则会比本地时间早 8 小时")

    # ---- 2. 局域网地址能不能连（这一步能抓住「只绑回环」这个坑）
    from .server import is_likely_virtual, lan_addresses

    lan = lan_addresses()
    if not lan:
        print("  [FAIL] 没找到局域网 IPv4 —— 这台机器没连上网络")
        problems.append("网络未连接")
    else:
        for addr in lan:
            ok, err = _try_connect(addr, port)
            tag = "  ← 虚拟网卡，Kindle 连不上" if is_likely_virtual(addr) else ""
            if ok:
                print(f"  [ OK ] 已绑定 {addr}:{port}{tag}")
            else:
                print(f"  [FAIL] {addr}:{port} 连不上 —— 服务可能只监听了 127.0.0.1{tag}")
                print(f"         {err}")
                problems.append(f"{addr} 未绑定")

    # ---- 3. 防火墙
    #
    # ⚠️ 必须带上**端口**：换过端口之后，旧端口上的放行规则还在，
    #    只按名字查会得到「有规则」这个**错误结论**。
    #    实测就是换到 8577 时，8080 的规则还在，而新端口其实没放行。
    fw = _firewall_rule_exists(port=port)
    if fw is True:
        print(f"  [ OK ] 防火墙规则 kindlewarden 放行了 {port}")
    elif fw is False:
        print(f"  [WARN] 没有放行 {port} 的防火墙规则")
        print(f"         以**管理员**身份运行：")
        print(f"         warden firewall --any-profile")
        print(f'         （等价于 New-NetFirewallRule -DisplayName "kindlewarden" '
              f"-Direction Inbound -Protocol TCP -LocalPort {port} "
              f"-Action Allow -Profile Private）")
    else:
        print("  [ -- ] 防火墙状态无法查询（跳过）")

    # ---- 4. 给出 Kindle 上该输入的地址（跳过虚拟网卡）
    print()
    real = [a for a in lan if not is_likely_virtual(a)] or lan
    if real:
        print(f"  Kindle 自带浏览器里打开： http://{real[0]}:{port}/")
        print(f"  越狱后客户端配置：      WARDEN_SERVER=\"http://{real[0]}:{port}\"")
    else:
        print("  还没有可用的地址 —— 先让这台机器连上局域网。")

    print()
    print("  注意：从本机访问自己的局域网 IP 走的是回环，**不能证明防火墙放行了**。")
    print("        真正的外部可达性只能用另一台设备（比如 Kindle）来验证。")

    if problems:
        print()
        print("  发现的问题：" + "、".join(problems))
        return 1
    return 0


def _amazon_usb_devices() -> list[str]:
    """列出注册表里已枚举过的 Amazon USB 设备（厂商 ID 0x1949）。

    Windows 只有在**电气层面枚举成功**时才会往这个位置写记录 ——
    哪怕驱动没装好、盘符没分配，也会留下痕迹。
    所以「这里有没有」能一刀切开两类完全不同的故障：

    - 有记录  → 电脑看见了设备，问题在驱动/盘符/文件系统
    - 没记录  → 电脑根本没看见，问题在线材、接口针脚、焊点

    这就是为什么它比「在文件资源管理器里找找看」有用得多。
    """
    import winreg
    found: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Enum\USB") as key:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                if name.upper().startswith("VID_1949"):
                    found.append(name)
                index += 1
    except OSError:
        return []
    return found


def _find_kindle_volume() -> Path | None:
    """找出 Kindle 用户分区挂在哪个盘符，找不到返回 None。

    按**特征目录**判断，不猜盘符 —— 用户机器上可能有网络驱动器、U 盘、读卡器
    （这台机器上就有 X/Y/Z 三个网络盘）。Kindle 用户分区必定有
    ``documents`` 和 ``system`` 两个目录。

    这是全项目唯一的实现：`usb` / `deploy` / 打包自检都走它，
    免得几份「找盘符」的逻辑各自漂移。
    """
    roots: list[str] = []
    try:
        roots = list(os.listdrives())          # Python 3.12+
    except AttributeError:
        roots = [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"]
    for root in roots:
        try:
            entries = set(os.listdir(root))
        except OSError:
            continue
        if {"documents", "system"} <= entries:
            return Path(root)
    return None


def _usb_device_count() -> int:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Enum\USB") as key:
            index = 0
            while True:
                try:
                    winreg.EnumKey(key, index)
                except OSError:
                    return index
                index += 1
    except OSError:
        return 0


def cmd_usb(args: argparse.Namespace, cfg: Config) -> int:
    """盯着 USB 总线，看 Kindle 有没有被枚举出来。

    这是排查「插上没反应」最有用的一步：它把问题切成两半 ——
    是电脑根本没看见（线/口/硬件），还是看见了但没挂载（驱动/盘符）。
    """
    if os.name != "nt":
        print("这个命令只在 Windows 上有意义（读的是注册表里的 USB 枚举记录）。")
        return 1

    print("kindlewarden USB 检查")
    print()
    amazon = _amazon_usb_devices()
    volume = _find_kindle_volume()
    print(f"  Amazon 设备的枚举记录：{amazon or '无'}")
    print(f"  当前 USB 设备总数：{_usb_device_count()}")
    print(f"  Kindle 用户分区：{volume or '未挂载'}")
    print()

    if amazon and volume:
        print(f"  ✓ 一切正常。Kindle 已挂载在 {volume}")
        print(f"    书在 {volume / 'documents'} 下；更新包要放在 {volume} 根目录。")
        return 0

    if amazon and not volume:
        print("  ⚠ 电脑认出了 Kindle，但**没有挂载成磁盘**。")
        print("    问题在驱动 / 盘符 / 文件系统，不在线材或接口：")
        print("      - 磁盘管理（diskmgmt.msc）看卷是不是没分到盘符")
        print("      - 设备管理器看有没有带感叹号的设备")
        return 1

    if args.watch <= 0:
        print("  没找到 Kindle 的枚举记录。")
        print("  想让它在插拔时实时确认，跑： warden usb --watch 60")
        print("  然后在 Kindle 那头轻轻动插头、换个插入深度。")
        return 1

    print(f"  没找到记录。现在开始监控 {args.watch} 秒 —— 请在这段时间里")
    print("  轻轻动插头：往上/下/左/右施力、往里推紧、拔出来一点再插回。")
    print()
    deadline = time.time() + args.watch
    base = _usb_device_count()
    seen = False
    try:
        while time.time() < deadline:
            hit = _amazon_usb_devices()
            now_count = _usb_device_count()
            stamp = time.strftime("%H:%M:%S")
            if hit:
                seen = True
                print(f"  {stamp}  ★★★ 出现了：{hit}")
            elif now_count != base:
                print(f"  {stamp}  USB 设备数 {base} → {now_count}（有变化，但不是我认识的 Amazon 设备）")
            else:
                print(f"  {stamp}  无变化", end="\r", flush=True)
            time.sleep(1.5)
    except KeyboardInterrupt:
        pass

    print()
    print()
    if seen:
        print("  ★ 抓到了！接口是**接触不良**，不是死路。")
        print("    记住那个能用的角度/深度，先把文件传进去；根治要补焊或更换 micro-USB 口。")
        return 0
    print("  全程没有枚举。结合「能充电但电脑毫无反应」，基本可以判定：")
    print("    电源针脚（1、5）通了，数据针脚（2、3）没通。")
    print("  按这个顺序处理：")
    print("    1. 关机，用强光看 micro-USB 口里有没有绒毛，用木质牙签挑出来（别用金属）")
    print("    2. 换一根**确认能传数据**的线（拿它连手机传一次文件验证）")
    print("    3. 换一台电脑 —— 这一步能一刀切开是线还是设备")
    print("    4. 都无效 → 硬件问题，需要补焊/更换接口")
    return 1


def _remove_stale_shell_client(kindle: Path) -> int:
    """删掉设备上残留的旧 shell 客户端，返回删掉的项数。

    为什么必须清掉：**两套客户端会抢同一块屏幕**。
    shell 那套用 fbink 直接写 framebuffer，每 60 秒把服务端 PNG 刷上去，
    覆盖掉 KOReader 画的一切 —— 包括插件自画的「退出」按钮。

    实测现象：看板还在更新，但退出按钮消失、触摸/滑动/电源键全无反应，
    看起来就是死机；而且 preventScreenSaver=1 让设备永不休眠。

    顺带删掉书库里的两个 scriptlet —— 它们是"再点一次就复发"的入口。
    """
    import shutil as _shutil

    targets = [
        kindle / "extensions" / "kindlewarden",
        kindle / "documents" / "kindlewarden-start.sh",
        kindle / "documents" / "kindlewarden-stop.sh",
    ]
    # scriptlet 在书库里可能还留了索引/封面缓存，一并清掉
    for extra in ("kindlewarden-start.sdr", "kindlewarden-stop.sdr"):
        targets.append(kindle / "documents" / extra)

    removed = 0
    for t in targets:
        try:
            if t.is_dir():
                _shutil.rmtree(t)
                removed += 1
            elif t.exists():
                t.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def cmd_deploy(args: argparse.Namespace, cfg: Config) -> int:
    """把打包产物部署到 Kindle（自动找盘符）。

    产出结构本身就和设备落点一一对应，所以这里只是按映射拷过去 ——
    不需要人去判断"哪个文件放哪个目录"，那是最容易出错的一步。
    """
    out = Path(args.out)
    if not out.is_dir():
        raise SystemExit(f"找不到打包产物：{out}\n先跑： warden package --server ...")

    # ⚠️ **部署前先确认产物不是旧的。**
    #
    # 踩过一次：改了插件源码却忘了重新 package，直接跑 deploy ——
    # deploy 从 dist/ 拷贝，于是设备上装的是**旧版本**，
    # 而输出全是"✓ 部署成功"。据此以为改动生效了，白排查一轮。
    #
    # 这里比对 dist 里的插件和源文件，不一致就**停下来**，
    # 而不是让用户去猜"为什么新功能没出现"。
    src_plugin = (Path(__file__).resolve().parent.parent
                  / "koplugin" / "kindlewarden.koplugin" / "main.lua")
    dist_plugin = (out / "koreader" / "plugins" / "kindlewarden.koplugin"
                   / "main.lua")
    if src_plugin.exists() and dist_plugin.exists():
        import re as _re
        src_text = src_plugin.read_text(encoding="utf-8")
        # 打包会把 DEFAULT_SERVER 换成实际地址，比对前先做同样的替换
        cfg_path = out / "extensions" / "kindlewarden" / "config.sh"
        server = ""
        if cfg_path.exists():
            m = _re.search(r'(?m)^WARDEN_SERVER="([^"]*)"',
                           cfg_path.read_text(encoding="utf-8"))
            if m:
                server = m.group(1)
        if server:
            src_text = _re.sub(
                r'(?m)^(local DEFAULT_SERVER = )"[^"]*"',
                lambda mm: mm.group(1) + f'"{server}"', src_text)

        if src_text != dist_plugin.read_text(encoding="utf-8"):
            raise SystemExit(
                "打包产物里的插件和源文件**不一致** —— dist 里是旧版本。\n"
                "先重新打包再部署：\n"
                "  warden package --server http://<本机IP>:8577\n"
                "  warden deploy\n"
                "\n"
                "（这条检查是为了防止「改了代码却部署了旧版本」——\n"
                "  那种情况下输出全是 ✓，但设备上跑的是老逻辑。）"
            )

    kindle = Path(args.kindle) if args.kindle else _find_kindle_volume()
    if kindle is None:
        raise SystemExit(
            "没找到 Kindle。请确认：\n"
            "  1. 用**能传数据**的线连上电脑（充电线不行）\n"
            "  2. Kindle 已解锁并停在主页\n"
            "  3. 跑 `warden usb` 确认电脑认得它\n"
            "也可以手动指定盘符： warden deploy --kindle F:\\"
        )

    print(f"Kindle 盘符：{kindle}")
    print()

    # ---- 默认只装插件，并清掉设备上残留的旧 shell 客户端。
    #
    # ⚠️ 为什么不一起装 shell 客户端（实测踩过的坑）：
    #
    # 两套客户端会**抢同一块屏幕**。shell 那套用 fbink 直接写 framebuffer，
    # 每 60 秒把服务端 PNG 刷上去，**覆盖掉 KOReader 画的一切** ——
    # 包括插件自画的「退出」按钮。
    #
    # 用户遇到的现象：看板还在更新，但退出按钮消失、
    # 触摸/滑动/电源键全都没反应，看起来就是死机。
    # 而且 shell 客户端设的 preventScreenSaver=1 会让设备永远不休眠。
    #
    # 所以：**插件是唯一主线**。旧的留着只会在误点时把设备搞成假死。
    # 需要它时才用 --with-shell 显式要求。
    jobs = [
        (r"koreader\plugins\kindlewarden.koplugin",
         r"koreader\plugins\kindlewarden.koplugin",
         "KOReader 插件", True),
    ]
    if getattr(args, "with_shell", False):
        jobs += [
            (r"extensions\kindlewarden", r"extensions\kindlewarden",
             "shell 客户端（备选路径）", False),
            (r"documents", r"documents", "Scriptlet 启动器", False),
        ]

    deployed = 0
    for rel_src, rel_dst, label, required in jobs:
        src = out / rel_src
        if not src.is_dir():
            if required:
                raise SystemExit(f"打包产物缺少必需目录：{src}")
            print(f"  - 跳过 {label}（产物里没有）")
            continue

        dst = kindle / rel_dst
        dst.mkdir(parents=True, exist_ok=True)
        for item in sorted(src.iterdir()):
            if not item.is_file():
                continue
            target = dst / item.name
            shutil.copyfile(item, target)

            # 拷完立刻校验换行符和 BOM —— 这两个问题只在设备上才暴露，
            # 而且报错信息完全不指向原因（bad interpreter / 命令找不到）
            raw = target.read_bytes()
            crlf = sum(1 for i in range(1, len(raw))
                       if raw[i] == 10 and raw[i - 1] == 13)
            bom = raw[:3] == b"\xef\xbb\xbf"
            flag = "✓" if not crlf and not bom else f"⚠ CRLF={crlf} BOM={bom}"
            print(f"  {flag} {rel_dst}\\{item.name}  {len(raw)} 字节")
            deployed += 1
        print()

    # ---- 清掉设备上可能残留的旧 shell 客户端
    if not getattr(args, "with_shell", False):
        removed = _remove_stale_shell_client(kindle)
        if removed:
            print(f"  已清理旧的 shell 客户端（{removed} 项）")
            print("    它和插件会抢屏幕 —— 留着只会让设备看起来像死机。")
            print()

    # KOReader 本体在不在？插件拷进去但没装 KOReader 是没用的
    if (out / "koreader" / "plugins" / "kindlewarden.koplugin").is_dir():
        if not (kindle / "koreader").is_dir():
            print("  ⚠ 设备上没有 koreader\\ 目录 —— 插件虽然拷进去了，")
            print("    但没有 KOReader 就加载不了。先装 KOReader，或改用 shell 路径。")
            print()

    print(f"共部署 {deployed} 个文件到 {kindle}")
    print()
    print("下一步：")
    print("  1. 在系统托盘里**安全弹出** Kindle，再拔线")
    if (kindle / "koreader").is_dir():
        print("  2. 重启 KOReader（新插件要重启才加载）")
        print("  3. 菜单 → 工具 → kindlewarden 副屏")
    else:
        print("  2. 书库里会出现「kindlewarden 副屏」，点一下即运行")
    return 0


def cmd_preview(args: argparse.Namespace, cfg: Config) -> int:
    """纯本地渲染 —— 不需要服务在跑，用来反复调版面。"""
    from .render import encode_png
    from .server import BoardService

    service = BoardService(cfg)
    snap = service.snapshot(force=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(snap.png)

    board = snap.board
    print(f"已渲染 {out}  ({len(snap.png) / 1024:.0f} KB, rev {snap.rev})")
    print(f"  简报 {board.total_briefs} 条 · 待办 {board.total_todos} 项"
          f"{' · 有告警' if board.alert else ''}")
    if snap.layout is not None:
        for z in snap.layout.zones():
            extra = f"，溢出 {z.overflow} 条" if z.overflow else ""
            print(f"  {z.key}: 显示 {z.shown}/{z.capacity} 条{extra}")
    return 0


DEMO_BRIEFS = [
    {"title": "调研了 e-ink 局部刷新方案", "status": "info",
     "summary": "结论：当前刷新频率下收益不足", "project": "research"},
    {"title": "部署 staging 失败", "status": "failed",
     "summary": "镜像构建超时，需人工确认", "project": "infra"},
    {"title": "重构订单服务", "status": "partial",
     "summary": "主流程已通，退款路径未覆盖", "project": "backend"},
    {"title": "迁移用户表到 PostgreSQL 16", "status": "ok",
     "summary": "停机 40 秒，回滚脚本已就绪", "project": "infra"},
    {"title": "修复了 auth 模块 token 刷新的竞态条件", "status": "ok",
     "summary": "定时器刷新改为惰性刷新，补 4 个并发测试", "project": "backend"},
]

DEMO_TODOS = ["补 auth 模块的删除用户测试用例", "确认 staging 构建超时的根因", "归档 Q3 的性能报告"]


def cmd_seed_demo(args: argparse.Namespace, cfg: Config) -> int:
    """往板上灌一组样例内容，用来审版面。"""
    for b in DEMO_BRIEFS:
        _post(cfg, "/brief", b)
    for t in DEMO_TODOS:
        _post(cfg, "/todo", {"text": t})
    _post(cfg, "/alert", {"title": "允许执行 rm -rf build",
                          "detail": "session: refactor-auth · 已等待 3 分钟"})
    print("已灌入样例内容（5 条简报 + 3 条待办 + 1 条告警）")
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="warden", description="Kindle 副屏控制器")
    p.add_argument("--config", help="panels.toml 路径（默认在数据目录下）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="写出默认配置")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("serve", help="启动常驻服务")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--mcp", action="store_true",
                   help="同时在这个端口上提供 MCP（POST /mcp）")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("publish", help="发布一条任务完成简报")
    s.add_argument("title")
    s.add_argument("--status", default="ok",
                   choices=("ok", "partial", "failed", "info"))
    s.add_argument("--summary", default="")
    s.add_argument("--project", default="")
    s.add_argument("--duration", type=int, default=0, help="耗时（秒）")
    s.add_argument("--artifact", action="append", help="产出物，可重复")
    s.add_argument("--supersedes", help="取代此前的某条简报 id")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("todo", help="待办板")
    s.add_argument("text", nargs="*",
                   help="不带选项时是新增；配合 --edit 时是新文字")
    s.add_argument("--done", metavar="ID", help="标记完成")
    s.add_argument("--undone", metavar="ID", help="取消完成")
    s.add_argument("--remove", metavar="ID", help="删除")
    s.add_argument("--edit", metavar="ID",
                   help="修改已有待办的文字（新文字写在位置参数里）")
    s.set_defaults(func=cmd_todo)

    s = sub.add_parser("alert", help="需要人介入的告警")
    s.add_argument("title", nargs="?")
    s.add_argument("--detail", default="")
    s.add_argument("--clear", nargs="?", const=True)
    s.set_defaults(func=cmd_alert)

    s = sub.add_parser("brief", help="列出 / 操作简报")
    s.add_argument("--pin")
    s.add_argument("--unpin")
    s.add_argument("--retract")
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("board", help="查看当前看板")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("doctor", help="诊断「Kindle 连不上」这类问题")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("usb", help="诊断「插上电脑没反应」（Windows）")
    s.add_argument("--watch", type=int, default=0, metavar="秒",
                   help="实时监控 N 秒，期间请动一动插头")
    s.set_defaults(func=cmd_usb)

    s = sub.add_parser("preview", help="本地渲染一张 PNG（不需要服务在跑）")
    s.add_argument("--out", default="mockups/out/preview.png")
    s.set_defaults(func=cmd_preview)

    s = sub.add_parser("package", help="打包 Kindle 侧（KOReader 插件 + shell 客户端）")
    s.add_argument("--server", required=True,
                   help="副屏服务地址，例如 http://192.168.1.50:8577")
    s.add_argument("--out", default="dist/kindlewarden")
    s.set_defaults(func=cmd_package)

    s = sub.add_parser("deploy", help="把打包产物部署到 Kindle（自动找盘符）")
    s.add_argument("--out", default="dist/kindlewarden")
    s.add_argument("--kindle", help="手动指定 Kindle 盘符，例如 F:\\")
    s.add_argument(
        "--with-shell", action="store_true",
        help="同时部署旧的 shell 客户端。"
             "⚠️ 它会和插件抢屏幕（用 fbink 直接刷 framebuffer），"
             "导致退出按钮消失、触摸失效、设备不休眠 —— 除非明确需要，否则别开")
    s.set_defaults(func=cmd_deploy)

    s = sub.add_parser("seed-demo", help="灌入样例内容用于审版面")
    s.set_defaults(func=cmd_seed_demo)

    s = sub.add_parser(
        "install-service",
        help="注册成开机自启的常驻服务（Windows 计划任务）")
    s.add_argument("--name", default="kindlewarden", help="计划任务名")
    s.set_defaults(func=cmd_install_service)

    s = sub.add_parser("uninstall-service", help="卸载上面注册的常驻服务")
    s.add_argument("--name", default="kindlewarden", help="计划任务名")
    s.set_defaults(func=cmd_uninstall_service)

    s = sub.add_parser(
        "firewall",
        help="加 Windows 防火墙入站放行规则（会弹 UAC）—— "
             "「本机能访问但局域网连不上」就是缺这条")
    s.add_argument("--port", type=int, help="端口，默认取配置")
    s.add_argument("--force", action="store_true", help="已有规则也重建")
    s.add_argument("--remove", action="store_true", help="删除规则")
    s.add_argument("--any-profile", action="store_true",
                   help="对所有网络配置文件生效（默认只对专用网络）")
    s.set_defaults(func=cmd_firewall)

    return p


def main(argv: list[str] | None = None) -> int:
    # Windows 上重定向时 Python 默认按 ANSI 码页编码，中文会变成乱码。
    # 这个工具的输出全是中文，所以显式锁成 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
