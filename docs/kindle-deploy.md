# Kindle 部署手册（P4）

> ⚠️ **本文档中的越狱步骤我无法代你验证。**
> 越狱会改动设备系统分区，操作失误可能变砖。
> 具体命令与文件请**以官方指南为准**，本文只说明「顺序、依赖、以及哪里最容易出错」。
>
> 主要依据：
> - [Kindle Modding Guide — LanguageBreak 5.14.3–5.16.2.1.1](https://kindlemodding.gitbook.io/kindlemodding/jailbreak-software/languagebreak-5.14.3-5.16.2.1.1)
> - [Kindle Modding Guide — 安装 KUAL/MRPI](https://kindlemodding.gitbook.io/kindlemodding/post-jailbreak/installing-kual-mrpi)
> - [LanguageBreak 仓库](https://github.com/notmarek/LanguageBreak)
> - [FBInk](https://github.com/NiLuJe/FBInk)

---

## 目标机型确认

| 项 | 值 |
|---|---|
| 机型 | Kindle Paperwhite 3 = 第 7 代（2015） |
| 型号号 | DP75SDI |
| 屏幕 | 6", 300 PPI, **1072 × 1448**, 16 级灰度 |
| 固件 | 5.16.2.1.1 |

**一个有利条件**：7~9 代老机型**停在 5.16.2.1.1**，这是它们的末版固件。
也就是说越狱之后**不存在被 OTA 升级冲掉越狱的风险** —— 这点比新机型省心得多。

先确认你的设备确实是这两项（设置 → 设备选项 → 设备信息）：

```
序列号前缀 G090 G1...   （第 7 代 Paperwhite 常见前缀）
固件版本 5.16.2.1.1
```

---

## 第 0 步：先别越狱，用自带浏览器看一次

**这一步能省掉大量无用功。** 越狱之前先用 Kindle 自带的「体验版网页浏览器」
把副屏打开看看 —— 网络通不通、字够不够大、版面顺不顺眼，一次性都能验证。

### 该打开哪个地址

在 host 上跑自检：

```powershell
warden doctor
```

它会直接告诉你地址。管理控制台（`http://<host>:8080/`）的预览图下方也会显示。

### ⚠️ Kindle 上要开的是 /kindle，不是 /

| 地址 | 用途 | Kindle 浏览器 |
|---|---|---|
| `/` | **管理控制台** | ❌ **打不开** |
| `/kindle` | 纯画面页（零 JS） | ✅ 用这个 |
| `/view` | 桌面上的全屏预览 | ❌ 用了现代 JS |

原因：Kindle 自带浏览器是老 WebKit ——
**不支持 `let` / `const`**（脚本里一出现，整段脚本都不执行）、
没有 `fetch`、没有箭头函数、CSS 不支持自定义属性和 `display:grid`。
管理控制台是现代 SPA，在上面必然白屏。

`/kindle` 是**零 JavaScript** 的：只有一个 `<img>` 和一条
`<meta http-equiv="refresh">`，自动刷新由服务端按当前活跃度决定间隔
（上限 60 秒）。老浏览器不需要为此执行任何脚本。

### 具体操作

1. Kindle 连上 Wi-Fi，确认和 host 在**同一个局域网**
2. 回主页 → 右上角菜单 **☰** → **体验版网页浏览器**
   （不同固件的入口名可能是「网页浏览器」/ "Experimental Browser"）
3. 地址栏输入自检给出的地址，例如：
   ```
   http://192.168.1.50:8080/kindle
   ```
4. 建议**加为书签**，以后一点就开
5. 页面会自己刷新，不用手动重载

### 画面尺寸不对怎么办

页面上的图片尺寸是**服务端算好后写死在 `<img width= height=>` 上**的，
不依赖 CSS 缩放。原因是：想让浏览器自动适配而写的 `max-height:100%`，
在「替换元素 + 百分比高度父容器」这个组合下，老 WebKit 的解析并不可靠 ——
实测就是**底部那行「更新于 HH:MM」被切掉**，而那一行恰恰是判断画面有没有过期的唯一依据。

服务端按 `[browser] viewport_w / viewport_h` 算出等比缩放后能完整放下的尺寸。
默认值按 PW3 推：屏幕 1072×1448 减去约 60px 的浏览器工具栏。

**在设备上直接试**（不用改配置，改地址栏就行）：

```
http://192.168.1.50:8080/kindle?w=1000&h=1300
```

| 现象 | 调整 |
|---|---|
| 四周留白太多、图偏小 | 把 `w` / `h` 调**大** |
| 出现滚动条、或底部被切 | 把 `w` / `h` 调**小** |
| 图被拉伸变形 | 不该发生 —— 如果发生了，说明浏览器忽略了 width/height，告诉我 |

试出合适的值之后，填进管理控制台 **设置 → browser** 里的两个框，保存即生效。

> 也可以直接改 `panels.toml` 的 `[browser]` 段，但要重启服务才读。

### 可选：关掉浏览器的 JavaScript

我们的页面没有 JS，关掉它反而更快、更省电。
（在浏览器的设置菜单里，不同固件叫法不同。）

### 如果打不开

| 现象 | 原因 |
|---|---|
| 页面无法显示 | 服务没跑 / 防火墙 / 地址不对 → **跑 `warden doctor`** |
| 能开但白屏或排版全乱 | 你开的是 `/`。改开 `/kindle` |
| 连不上、提示超时 | 端口问题。老浏览器对非标端口有时不友好，把服务改绑 80 端口试试（需要管理员权限） |

放行防火墙需要**管理员权限**：

```powershell
New-NetFirewallRule -DisplayName "kindlewarden" -Direction Inbound `
  -Protocol TCP -LocalPort 8080 -Action Allow -Profile Private
```

> ⚠️ **从 host 本机访问自己的局域网 IP 走的是回环，不能证明防火墙放行了。**
> 最终判据只能是 **Kindle 那边打得开**。

**强烈建议在路由器上给 host 做 DHCP 保留**（固定 IP）。
Kindle 自带的浏览器和 wget 都不支持 mDNS，IP 一变就得改配置。

### 浏览器方案的固有限制

- **工具栏占高度**，画面不会 1:1 显示（`/kindle` 已做等比缩放，保证整屏可见）
- **不能长期挂着**：Kindle 会休眠、Wi-Fi 会断，浏览器前台也很耗电
- 合上保护套 / 按电源键之后要重新打开

**结论：浏览器方案适合"看一眼"和"验证网络"，长期常驻还是走越狱路径**（下面几节）。
这也正是为什么 `/kindle` 的自动刷新做得比较保守 —— 它本来就是给人临时看的。

---

## 开始之前：三件必须先办的事

### 0. 先确认 USB 能连上（越狱的硬前提）

**LanguageBreak 要求先恢复出厂设置、进入 demo 模式，而且整个流程要插拔 USB 两次。**
如果 USB 传不了文件，你会卡在 demo 模式里出不来。

所以**在动手之前先把这个确认掉**：

```powershell
warden usb
```

它会查 Windows 注册表里的 USB 枚举记录。判断依据很硬：
Windows 只有在**电气层面枚举成功**时才会写记录 —— 哪怕驱动没装好、盘符没分配，
也会留下痕迹。所以：

| 结果 | 含义 | 下一步 |
|---|---|---|
| 有 Amazon 设备记录 | 电脑看见了，问题在驱动/盘符 | 设备管理器、`diskmgmt.msc` |
| 没有任何记录 | **电脑根本没看见** | 线材 / 接口针脚 / 焊点 —— 见下 |

实时确认（插着的时候动一动插头）：

```powershell
warden usb --watch 60
```

> micro-USB 有 5 个针脚：**电源在两端（1、5），数据在中间（2、3）**。
> 「能充电但电脑毫无反应」正是「两端通了、中间没通」的典型表现。

**"能充电但电脑没反应" 的处理顺序：**

1. **清接口绒毛**（最常见且免费）：关机，强光看 micro-USB 口内部，
   用**木质牙签**挑出绒毛 —— 绝对不要用金属，会短路针脚
2. **换一根确认能传数据的线**：拿它连手机传一次文件，能传才算数。
   "标称是数据线" 和 "实际还能传数据" 是两回事
3. **换一台电脑**：这一步能一刀切开是线的问题还是设备的问题
4. **都无效 → 硬件问题**：PW3 是 2015 年的机器，micro-USB 焊点开裂很常见，
   需要补焊或更换接口（手机维修店即可）

**如果 USB 暂时修不好**：副屏其实**现在就能用** ——
Kindle 自带浏览器打开 `http://<IP>:8080/kindle` 不依赖 USB，
只是拿不到 FBInk 方案的常驻与省电优势。

### 1. 让副屏服务脱离这个会话自己跑

如果你现在看到的服务是跑在某个临时终端 / agent 会话里的，**它随时会没了**。
找个自己的终端窗口常驻：

```powershell
$env:KINDLEWARDEN_DATA = "$env:LOCALAPPDATA\kindlewarden"
python -m kindlewarden.cli serve
```

或者做成**登录时自动启动**（不需要管理员权限）：

```powershell
schtasks /Create /TN "kindlewarden" /SC ONLOGON /RL LIMITED `
  /TR "cmd /c cd /d C:\workspace\kindlewarden && python -m kindlewarden.cli serve"
```

删掉它：

```powershell
schtasks /Delete /TN "kindlewarden" /F
```

> 部署完先手动跑一段时间，确认稳定了再挂自启动。

### 2. 确认网络这条路是通的

```powershell
warden doctor
```

在 Kindle 自带浏览器里打开它给出的 `http://<IP>:8080/kindle`，
**能看到画面再往下走**。越狱不能解决网络问题，只会让你在更麻烦的状态下排查它。

### 3. 把客户端包打好

```powershell
warden package --server http://<host的局域网IP>:8080
```

产物在 `dist/kindlewarden/`。打包脚本会做三件手工最容易翻车的事：
把地址写进 `config.sh`、**统一换行符为 LF**（Windows 上的 CRLF 会让 Kindle 上的
shell 脚本报 `bad interpreter`，且报错完全看不出原因）、以 UTF-8 **无 BOM** 写出。

### 越狱的代价，先说清楚

- 会改动系统分区，**操作失误可能变砖**
- 具体命令与文件**以官方指南为准**，本文只说顺序、依赖和易错点
- 有个有利条件：7~9 代机型**停在 5.16.2.1.1**（末版固件），
  越狱后**不存在被 OTA 冲掉的风险**

---

## 第 1 步（推荐）：WinterBreak

> ## ⚠️ 别用 LanguageBreak
>
> **官方已经把 LanguageBreak 归入 Legacy（不再推荐）**：
>
> > *"These legacy jailbreak methods are no-longer recommended in favour for simpler
> > methods such as WinterBreak which support more models and firmware versions."*
> > —— [KindleModding Legacy 页](https://kindlemodding.org/jailbreaking/Legacy/index.html)
>
> **在 PW3 + 5.16.2.1.1 这个组合上，LanguageBreak 有一个已知失败 mode**：
> 走到 "press power button" 之后，设备会**自己启动**、不等待长按，
> 于是**永远不会出现那个让你选中文的语言选择菜单** ——
> 而选语言包正是整个攻击的触发点。结果就是 `jb/` 和 `patchedUks.sqsh`
> 被放在分区上，却从未被执行。
>
> 已有多人报告同一现象，例如
> [LanguageBreak issue #59 — "PW3 No Language select after step 12"](https://github.com/notmarek/LanguageBreak/issues/59)。
>
> **所以：优先走下面的 WinterBreak。** LanguageBreak 的详细流程保留在文末作为参考。
>
> 开始之前先确认机型：
> **[Jailbreak Wizard](https://kindlemodding.org/jailbreak-wizard.html)** ——
> 输入序列号**前 8 位**（例如 `G090G105`）。**不要在公开论坛贴完整序列号。**

---

原理和 LanguageBreak **完全不同**：它走 Kindle 商店的 Mesquito 漏洞，
**不需要恢复出厂、不需要 demo 模式、不需要选语言包**。
操作次数少、没有时机敏感的窗口。

**已确认在 PW3 + 5.16.2.1.1 上可用**：
[GitHub issue #63](https://github.com/KindleModding/WinterBreak/issues/63)、
[MobileRead 讨论](https://www.mobileread.com/forums/showthread.php?t=374427)。

### 前提

- **设备已注册**（登录 Amazon 账号）
- **Wi-Fi 可用**（要走商店）
- PC + 数据线

### 先清干净根目录

如果之前试过 LanguageBreak，把它的遗留物删掉（保留 `documents` / `fonts` / `system`）：

```
.demo                  ← demo 模式标记
DONT_CHECK_BATTERY     ← demo 模式绕过文件
jb/                    ← LanguageBreak 载荷
patchedUks.sqsh        ← LanguageBreak 补丁镜像
```

> ⚠️ 根目录里**不要留任何 `.bin` 文件**，否则开机时会尝试安装更新（可能报 `update error`）。

### 步骤

1. 下载
   [最新版 WinterBreak](https://github.com/KindleModding/WinterBreak/releases/latest/download/WinterBreak.tar.gz)
2. **开飞行模式 → 重启设备**
3. 解压 `WinterBreak.tar.gz` 到**电脑本地**（不要直接解压到 Kindle 上，官方说可能失败）
4. 把解压出来的文件**拷到 Kindle 根目录**，提示覆盖就覆盖
   > ⚠️ 确认隐藏目录 **`.active_content_sandbox` 也拷进去了** —— 这是最容易漏的一步
5. 安全弹出、拔线
6. 在 Kindle 上**打开 Kindle 商店**（主页的购物车图标）
7. 提示时点 **`Yes`** 关闭飞行模式
8. Mesquito 加载后，**点 WinterBreak 图标**
9. 等约 **30 秒** —— 设备会刷屏文字并重启 GUI

> ✅ **验证点**：**看到那些文字就说明成功了**。没看到就重做一遍。

10. 用官方 FAQ 的办法确认：搜索框输入 **`;log`**，**有弹窗 = 已越狱**

### 排错

- **打开商店报 "Unexpected error"，或只显示商店首页** → 官方给了两套办法
  （LocalStorage 替换 / 恢复出厂后注册前先放文件），
  见 [WinterBreak 页面的 Troubleshooting](https://kindlemodding.org/jailbreaking/WinterBreak/#troubleshooting)
- **`jb.sh` 跑了但 KPM / sh_integration 不正常** → 这是 PW3 上的已知后续问题，
  见 [issue #63](https://github.com/KindleModding/WinterBreak/issues/63)。
  **KUAL 本身通常仍可用**，我们只需要 KUAL + FBInk

---

## 第 1 步（备选）：LanguageBreak 详细流程

> ☝️ **先看上面的警告。** 这一段保留作为参考 ——
> 如果你的机型/固件不在 WinterBreak 支持范围内，才考虑这条。
> 对 **PW3 + 5.16.2.1.1** 我已经实测过：它会卡在"没有语言选择菜单"这一步。

LanguageBreak 的官方支持范围是 **5.14.3 – 5.16.2.1.1**。


> ⚠️ **这一步要恢复出厂设置，会抹掉设备上的用户数据。**
> 已购书籍在云端，重新登录就回来了；但自己拷进去的文档、笔记、收藏会丢。
> 先把想留的拷出来。
>
> ⚠️ **全程要插拔 USB 两次。** 开始前先确认 `warden usb` 报「✓ 一切正常」——
> 中途 USB 不通的话，你会卡在 demo 模式里出不来。

### 1.1 准备

1. 从 [LanguageBreak Releases](https://github.com/notmarek/LanguageBreak/releases/latest)
   下载最新的 `tar.gz`，解压到一个安全位置
2. 先给 Kindle 充电（这个过程要多次重启）
3. 把 Kindle 上想保留的文件拷到电脑

### 1.2 进入 demo 模式

1. **恢复出厂设置**：设置 → 设备选项 → 重置设备
2. 在语言选择界面**任选一种语言**
3. 跳过 Wi-Fi 注册：随便选一个网络，**然后退出来**
4. 在**搜索框**输入 `;enter_demo` 回车
5. 长按电源键，选 `reboot` 重启
6. 设备应该进入 demo 模式（没进的话见 §1.6 排错）
7. 跳过 Wi-Fi 设置，提示时随便填假信息
8. 跳过「搜索 demo payload」
9. demo 类型选 **`standard`**
10. 在 "sideload content" 提示处按 **`Done`**
11. 出现 `misconfiguration` 错误时，用这个手势绕过：
    **两根手指点一下，然后向左滑**
12. 搜索框输入 `;demo` 回车
13. 选 **"sideload content"**

### 1.3 第一次 sideload

1. **插上 USB**（第一次）—— 电脑上应出现 Kindle 盘符
2. 把 LanguageBreak 文件夹里的**内容**拷到 **Kindle 根目录**，
   **合并并覆盖所有同名文件**
   （Windows 会弹「替换或跳过文件」→ 选**替换目标中的文件**）
3. 安全弹出，拔掉 USB，回到 demo 菜单
4. 选 **"Resell Device"**
5. 选 **`Yes/Resell`**
6. 等屏幕出现 **`press power button`** 提示

### 1.4 第二次 sideload（时机关键）

7. **一看到那个提示就立刻插上 USB**，把 LanguageBreak 内容**再拷一次**，
   覆盖，然后安全弹出
8. 按屏幕指示**长按电源键**
9. 几秒后应进入语言选择菜单
10. **选「中文」** —— 右侧列表**倒数第二个**，在 `p s e u d o` 上面
11. Kindle 会重启，屏幕上闪过一些日志
12. **此时设备已处于越狱状态**

### 1.5 装 LanguageBreak 专用 Hotfix（不能跳过）

> LanguageBreak 比较特殊：**它自己的 Hotfix 必须先装，之后才能做其它
> post-jailbreak 步骤。** 跳过这一步的话，一次意外重启就可能丢掉越狱。

1. 搜索框输入 `;uzb` 回车（进入 USB 驱动器模式）
2. 插上 USB
3. 把解压出来的 **`update_hotfix_languagebreak.bin`** 拷到 **Kindle 根目录**
4. 安全弹出，拔掉
5. 搜索框输入 `;dsts` 回车
6. 选 **`Update Your Kindle`**

### 1.6 排错

**进不去 demo 模式？** 换个入口：

1. 插上 USB
2. 在 Kindle **根目录**建一个**空文件**，命名为 `DONT_CHECK_BATTERY`
   （新建文本文档，改名并删掉 `.txt` 后缀）
3. 搜索框输入 `;demo` 回车
4. 从上面 **§1.2 的第 5 步**继续

**卡在 demo 模式里想重置？**

1. 搜索框输入 `;uzb` 回车
2. 插上 USB
3. 在 Kindle 根目录建一个空文件 `DO_FACTORY_RESTORE`
4. 重启 Kindle

**手势比划不出来？** 官方有[演示视频](https://www.youtube.com/watch?v=JzuIGbGPpig)。

> 也有 PW3 + 5.16.2.1.1 使用 WinterBreak 的社区案例，
> 但**以 KindleModding 的兼容性表为准**，不要凭传闻换工具。

---

## 第 2 步：选设备端方案

有两条路，**先看这张表再决定**：

| | **A. KOReader 插件**（推荐） | B. shell + fbink |
|---|---|---|
| 屏幕归属 | ✅ **KOReader 独占** | ❌ 盖在 framework 上 |
| 抢屏问题 | ✅ 不存在 | ❌ 要靠每 60 秒重绘去抢 |
| 触摸 | ✅ **原生**，不会穿透 | ❌ 点屏幕会打开底下的书 |
| 点击看详情 | ✅ 支持 | ❌ 不支持 |
| 随时退出 | ✅ KOReader 的 Home / 返回 | ⚠️ 要读 evdev 或长按电源重启 |
| 依赖 | 装 KOReader | 无 |
| 显示 | 独占整屏 | 会和 framework 互相覆盖 |

> **为什么 A 明显更好**：B 的问题是**架构性的**，不是调参能解决的。
> `fbink` 直接往 framebuffer 画，而 Kindle 的 Java framework 也在画同一个、
> **并且仍然处理触摸** —— 所以 B 只能靠不断重绘去抢屏幕，而且点屏幕必然穿透。
> KOReader 是全屏应用，这两件事天然成立。

下面 §2A 讲插件，§2B 讲 shell 那套（保留作为没有 KOReader 时的退路）。

### 2A. KOReader 插件（推荐）

**前提：Kindle 上已装 KOReader。**

```powershell
warden package --server http://<host的局域网IP>:8080
warden deploy
```

`deploy` 会自动找 Kindle 盘符，按**产物结构 → 设备落点**的固定映射拷过去：

| 产物目录 | 设备落点 |
|---|---|
| `koreader\plugins\kindlewarden.koplugin\` | `<Kindle>\koreader\plugins\kindlewarden.koplugin\` |
| `extensions\kindlewarden\` | `<Kindle>\extensions\kindlewarden\`（备选方案的客户端） |
| `documents\*.sh` | `<Kindle>\documents\`（备选方案的启动器） |

然后：

1. 安全弹出，拔线
2. **重启 KOReader**（新插件要重启才加载；只退出重进不够）
3. KOReader 菜单 → **工具 → kindlewarden 副屏**

**就用这一个菜单项**：点开显示看板，点简报卡片进详情，点 `‹ 返回` 回看板。
退出就是 KOReader 的 Home 或返回手势 —— 没有额外机制。

服务端地址在打包时已经烘进插件，也可以在 KOReader 菜单里改
（「工具 → kindlewarden：服务端地址」，IP 变了不用改代码）。

**排错**

| 现象 | 原因 |
|---|---|
| 菜单里没有这一项 | 插件没拷对位置，或者 **KOReader 没重启** |
| 点开一闪就退 | 看 `crash.log`（KOReader 数据目录下），插件报错会写在那儿 |
| 显示"副屏服务不可达" | IP 不对 / 服务没跑 / 防火墙。用 Kindle 浏览器开 `http://<IP>:8080/kindle` 验一下 |
| 卡片位置和点击对不上 | 插件里的 `IMG_W/IMG_H` 和服务端画布不一致 —— 跑 `pytest tests/test_koplugin_lua.py` 会抓到 |

**为什么插件不自己排版**：服务端在返回 PNG 的同时，把每张卡片的矩形
（`rect`，图像坐标）一并给出。插件只做「减原点、除缩放比」一次换算，
然后按矩形判断命中。所以**改版面只动服务端，插件一行都不用改**。

### 2B. shell + fbink（备选）

只有在不装 KOReader 时才用这条。它靠 Scriptlet 启动 —— 官方已明确
**KUAL 废弃**（*"KUAL ... is now obsolete and does not work"*），
现在的机制是把 `.sh` 放进 `documents/`，它会以「书」的形式出现在书库，点一下就跑。

部署见上面 `deploy` 的映射表。启动后画面会**和 framework 互相覆盖**，
所以客户端每 60 秒无条件重绘一次把屏幕抢回来（`config.sh` 里的 `WARDEN_REDRAW_EVERY`）。

> ⚠️ 这条路**没有点击交互**，而且点屏幕会穿透到底下的书。
> 想让它独占屏幕可以设 `WARDEN_STOP_FRAMEWORK=1`（代价：退出要长按电源重启）。


---

## 第 3 步：确认 FBInk（多半已经有了）

FBInk 是真正往电子墨水屏上画图的工具 —— Kindle 端的「画笔」。

**WinterBreak 的越狱包自带它**，实测位置：

```
libkh/bin/fbink        （约 1.4 MB）
```

**它不在 PATH 里**，所以客户端的 `warden.sh` 会主动在几个常见位置找一遍
（见 `find_fbink()`），也可以在 `config.sh` 里用 `WARDEN_FBINK` 写死。

**验证**（越狱后，需要 kterm 之类的终端，或者直接用我们的客户端试）：

```sh
/mnt/us/libkh/bin/fbink --help
/mnt/us/libkh/bin/fbink "hello"     # 屏幕上应出现 hello
```

**如果确实没有**，两条路：装 KOReader（自带 FBInk），或者从
[NiLuJe 的 MobileRead 帖子](https://www.mobileread.com/forums/showthread.php?t=299620)
取独立的 `fbink` 二进制。

**关于中文**：FBInk 的内置字体**不含 CJK**，所以**不要在设备上画中文**。
这正是我们把渲染放在服务端的原因 —— Kindle 只负责把 PNG 贴到屏幕上。

> 顺带说明一个已核实的限制：**PW3 不支持硬件抖动**。
> FBInk README 明确写着真正的硬件抖动（`-D`）在 Kindle 上完全未测试，
> 且只有 Oasis 2 / PW4 / Oasis 3 *可能*支持。
> 所以我们必须用 `-g` 的 `dither` 子选项走**软件**抖动。

**顺手确认这几个参数**（都要在设备上实测，见文末「已知未验证项」）：

```sh
/mnt/us/libkh/bin/fbink --help | head -40
command -v setsid nohup
wget -T 10 -O - "http://<host IP>:8080/rev?c=kindle"
lipc-get-prop com.lab126.powerd preventScreenSaver
```

---

## 第 4 步：部署

```powershell
warden package --server http://<host的局域网IP>:8080
warden deploy
```

`deploy` 自动找 Kindle 盘符（按 `documents` / `system` 两个特征目录判断，
不猜盘符 —— 用户机器上常有网络驱动器）。产出结构和设备落点**一一对应**，
所以部署没有"哪个文件放哪"的判断题：

```
dist/kindlewarden/
    koreader/plugins/kindlewarden.koplugin/main.lua  →  KOReader 插件（推荐）
    extensions/kindlewarden/                         →  shell 客户端（备选）
    documents/kindlewarden-*.sh                      →  Scriptlet 启动器（备选）
```

打包脚本会把三件手工最容易翻车的事做掉：

1. **把服务端地址烘进插件和 `config.sh`** —— 省掉最容易填错的一步
2. **统一换行符为 LF** —— Windows 上编辑出来的 CRLF 会让 Kindle 上的
   shell 脚本直接报 `bad interpreter`，而且报错信息完全看不出原因
3. 以 UTF-8 **无 BOM** 写出 —— BOM 会被 shell 当成命令的一部分

它会自己报告自检结果，必须是 `9 个文件，全部 LF 换行、无 BOM ✓`。

拷完在系统托盘里**安全弹出**再拔线。

---

## 第 5 步：启动与验证

### 5.1 KOReader 插件（推荐路径）

1. 启动 KOReader（**必须重启过一次**，新插件才加载）
2. 菜单 → **工具 → kindlewarden 副屏**
3. 首次打开会弹一个提示，写明怎么退出

**操作**

| 操作 | 效果 |
|---|---|
| 点简报卡片 | 进详情页 |
| 点 `‹ 返回` | 回看板 |
| 点 `下一页 ›` | 内容长时翻页 |
| 点待办 | 弹一行提示（只读） |
| **点右下角「退出 ✕」** | **退出副屏** |
| **任意方向滑动屏幕** | **退出副屏** |

> ⚠️ **不要指望用 Home 键或返回键退出。** PW3 是纯触摸设备，
> KOReader 的 `InputContainer` 只在 `Device:hasKeys()` 为真时才注册 Home 键 ——
> 在 Kindle 上**那个键根本不存在**。第一版插件就是因为默认它存在、
> 又没留别的出口，导致打开后**只能长按电源键重启设备**。
>
> 现在退出有三层：**插件自己画的「退出」按钮**（服务端连不上时依然在）、
> 任意方向滑动、以及服务端给的区域。滑动不判断方向 ——
> 误退出只是重新点开，退不出去要重启设备，代价差得远。

**排查**

| 现象 | 处理 |
|---|---|
| 菜单里没有这一项 | 插件位置不对，或 **KOReader 没重启** |
| 点开一闪就退 | 看 KOReader 数据目录下的 `crash.log`，插件报错会写在那儿 |
| 一直显示"副屏服务不可达" | IP / 服务 / 防火墙。用 Kindle 浏览器开 `http://<IP>:8080/kindle` 验 |
| 卡片位置和点击对不上 | 插件里的 `IMG_W/IMG_H` 和服务端画布不一致 —— 跑 `pytest tests/test_koplugin_lua.py` 会抓到 |

下载的 PNG 落在 `/mnt/us/koreader/kindlewarden/`（USB 可见），
插上电脑能直接看到有没有文件 —— 画面为空时先看这里。
**这个目录里有 `board.png` 就说明插件成功跑通了整条链路。**

### 5.2 shell 客户端（备选路径）

书库里点 **「kindlewarden 副屏」**。客户端在后台常驻，
画面每 60 秒重绘一次（防止被 framework 盖掉）。

日志在 `<Kindle盘符>\kindlewarden\warden.log`，正常启动应该看到：

```
2026-10-10 23:22:47 [start.sh] 已用 setsid 启动
2026-10-10 23:22:47 启动 pid=15819，重绘周期=60s
2026-10-10 23:22:50 [start.sh] 自检通过：客户端 pid=15819 启动 3 秒后仍在运行
2026-10-10 23:22:53 周期重绘（已 60s 无变化）
```

| 日志内容 | 含义 | 处理 |
|---|---|---|
| `已更新画面 rev=...` | 正常 | — |
| `fbink: /mnt/us/libkh/bin/fbink` | 找到画笔了 | — |
| `警告：找不到 fbink` | 路径不对 | 在 `config.sh` 里用 `WARDEN_FBINK` 写死 |
| `拉取失败（第 N 次）` | 网络不通 | 检查 `config.sh` 里的 IP |
| `解析 rev 失败` | wget 参数或响应格式问题 | 改 `config.sh` 里的 `WARDEN_WGET_OPTS` |
| `周期重绘` 从来不出现 | 进程死了 | 看有没有 `进程结束（exit=…）`：有就是自己退的，没有就是被强杀 |

### 5.3 端到端验证

在 host 上发一条简报：

```powershell
warden publish "部署验证成功" --status ok --summary "Kindle 端已上屏"
```

设备应该在十几秒到一分钟内刷新。

**另一个更直观的判据**：打开管理控制台 `http://<host>:8080/`，
右上角的「Kindle 尚未连接」应该变成具体时间。这证明设备真的在轮询
（只有带 `?c=kindle` 的请求才算设备活动）。

### 5.4 「画面闪一下就消失」是怎么回事

**这是这套方案最容易踩的坑，而且原因不在我们的代码逻辑。**

`fbink` 是**直接往 framebuffer 画**的。但 Kindle 的 **Java framework 也在画同一个
framebuffer**。你点 scriptlet → 客户端画上副屏 → scriptlet 退出 → **framework 重绘书库
界面，把画面盖掉**。现象就是"闪一下就没了"。

我们的客户端原本**只在 `rev` 变化时才画**，所以被盖掉之后要等好几分钟才恢复 ——
看起来就像完全失效。

**两个对策，都已实现在 `warden.sh` 里：**

**① 周期性重绘**（`config.sh` 里的 `WARDEN_REDRAW_EVERY`，默认 60 秒）

内容没变也定期重画一次，把屏幕抢回来。用**不闪刷**的波形，免得每 60 秒黑闪一次；
只有内容真的变了才用闪刷（顺便清残影）。

> 参考实现 [alexishida/kindle-dashboard](https://github.com/alexishida/kindle-dashboard)
> 的做法一样：它的 `dash-loop.sh` 无条件每 45 秒重绘一次，不依赖内容是否变化。

**② 每轮重新设置 `preventScreenSaver`**

`lipc-set-prop com.lab126.powerd preventScreenSaver 1` **会被 powerd 重置**，
设一次不管用 —— 屏保一起来同样会盖掉画面。参考实现的注释也写着这一点
（*"powerd 有时会重置，所以每个周期都要重新设置"*）。

| 配置 | 默认 | 说明 |
|---|---|---|
| `WARDEN_REDRAW_EVERY` | 60 | 无条件重绘周期（秒）。**设 0 会退化成"闪一下就消失"** |

> 想让画面真正独占屏幕（不再和 framework 抢），可以停掉 framework
> （`/etc/init.d/framework stop`）。但那样 Kindle 就不是阅读器了，
> 退出时要 `start` 回来 —— 属于进阶用法，默认不做。

### 5.5 确认能长期跑

- 让它跑一天，看日志有没有异常增长
- 确认屏幕不会休眠（`preventScreenSaver` 生效的话）
- 稳定之后，再考虑开机自启

---

## 排错清单

| 现象 | 先查什么 |
|---|---|
| **画面「闪一下就消失」** | **framework 抢屏** —— 见下面专门一节。检查 `config.sh` 里 `WARDEN_REDRAW_EVERY` 是不是被设成了 0 |
| **插上电脑没反应（能充电）** | **先跑 `warden usb`** —— 分辨是「电脑没看见」还是「看见了没挂载」 |
| **Kindle 浏览器打不开 `http://<IP>:8080/`** | **先跑 `warden doctor`** —— 九成是「服务只绑了 127.0.0.1」或「用了虚拟网卡地址」 |
| 屏幕一直不变 | `warden.log` 有没有「拉取失败」；`config.sh` 里的 IP 对不对 |
| 报 `bad interpreter` | 脚本是 CRLF。重新跑 `warden package` |
| 报 `: not found` 之类 | BOM。同上 |
| `fbink: command not found` | 第 3 步没做，或 FBInk 不在 PATH |
| 画面糊/有噪点 | `-W` 波形不对；或 PNG 不是 8bit 灰度 |
| 屏幕一直亮着很耗电 | `preventScreenSaver` 没生效，或客户端重复启动 |
| 客户端起不来 | `/tmp/warden.pid` 残留，先跑 KUAL → 停止副屏 |

**确认客户端在跑**（需要 kterm 或 usbnet 之类的终端）：

```sh
cat /tmp/warden.pid
ps | grep warden
tail -30 /mnt/us/kindlewarden/warden.log
```

---

## 已知未验证项

这些命令是社区常见写法，但**我没有在真机上验证过**，上机时请逐个确认：

| 位置 | 命令 | 说明 |
|---|---|---|
| `warden.sh` | `lipc-set-prop com.lab126.powerd preventScreenSaver 1` | 阻止屏保；属性名可能随固件变化 |
| `warden.sh` | `fbink -g file=...,dither -W GC16 -f -q` | 参数名以设备上 `fbink --help` 为准 |
| `warden.sh` | `eips -g <png>` | 退路方案，老设备可能可用 |
| `start.sh` | `setsid` / `nohup` | busybox 未必两个都有，脚本里已做降级 |
| `stop.sh` | `ps \| grep ... \| awk` | busybox 的 ps 输出格式可能不同 |

---

## 回退

想停掉副屏：

1. Kindle 上：**KUAL → 停止副屏**（会顺带恢复屏保）
2. 要彻底清掉：删掉 `<Kindle盘符>\extensions\kindlewarden\` 与
   `<Kindle盘符>\kindlewarden\`（日志/状态目录）

越狱本身是**不建议回退**的（回退需要进 diags 分区，风险高于保留）。
如果只是不想用了，把客户端停掉、扩展目录删掉即可，越狱不会影响正常读书。

---

## 为什么不做的事

这些是设计上的**有意取舍**，不是没做完：

| 没做 | 原因 |
|---|---|
| 设备主动注册到服务端 | 拉取模型下服务端不需要触达设备，注册只增加复杂度 |
| 从 Kindle 回传操作 | e-ink 触摸反馈慢，且「点一下就能取消 agent 任务」的安全边界不划算 |
| 端上渲染中文 | 需要往设备塞 CJK 字体，还得自己实现排版；服务端渲染免费且可控 |
| 局部刷新 | 当前没有持续变化的元素，块级脏更新的收益接近零（协议字段已预留） |
