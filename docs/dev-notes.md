# 开发说明

## 环境

| 项 | 值 |
|---|---|
| Python | 3.11+（用到 `tomllib`）；开发环境实测 3.14 |
| 运行依赖 | **只有 Pillow** |
| MCP 依赖 | `mcp>=1.0,<2`（可选，装了才有 `warden-mcp`） |
| 测试 | pytest |

服务端刻意只用标准库（`http.server`）：这个进程要 7×24 常驻，
少一个依赖就少一种半夜挂掉的方式。

> **MCP SDK 装 1.x，不要装 2.x。** mcp 2.x 的 stdio 传输依赖 `pywin32` 的
> Windows Job Object 支持，而 pywin32 需要后置安装步骤（`pywin32_postinstall.py`）
> 才能用。1.30 也用但至少在有 `.pth` 处理的正常安装下没问题 ——
> 见下面「沙箱注意事项」。

---

## 跑起来

```powershell
# 依赖（用 uv，见下面为什么不用 pip）
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
$env:TEMP = "$PWD\.tools\tmp"; $env:TMP = $env:TEMP
uv pip install --target .tools --python (Get-Command python).Source pytest "mcp>=1.0,<2"

# 关键：让沙箱垫片生效
$env:PYTHONPATH = "$PWD;$PWD\.tools"

# 测试
python -m pytest -q

# 本地跑服务（数据放在工作区内，避免污染 %LOCALAPPDATA%）
$env:KINDLEWARDEN_DATA = "$PWD\.runtime"
python -m kindlewarden.cli init
python -m kindlewarden.cli serve
python -m kindlewarden.cli seed-demo
```

---

## Docker 构建里的几个决定

### 为什么是 `pip install -e .` 而不是 `pip install .`

``cli.py`` 用 ``Path(__file__).resolve().parent.parent`` 定位**仓库根目录**，
再去找 ``koplugin/`` 和 ``kindle/`` —— ``warden package`` / ``warden deploy``
要靠它们生成设备侧的安装包。

装成普通 wheel 的话包会进 site-packages，那个相对路径就指向
``site-packages``，这两个命令直接失败。所以镜像里**保留源码布局 + 可编辑安装**：
既拿到入口脚本和依赖，又保住路径。

### 字体发现为什么要**兜底扫描**，而不只是列路径

原来 ``FontBook.CANDIDATES`` 里只有一条 Linux 路径
（``/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc``）。
但各家发行版放 Noto CJK 的位置不一样：

```
Debian/Ubuntu   /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
某些发行版       /usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc
语言子集         NotoSansCJKsc-Regular.otf / -jp / -kr / -tc …
```

**把路径穷举一遍既列不全也维护不动**，而容器里没有交互终端可以现场排查 ——
找不到字体就直接渲染失败，现象还和「网络不通」长得一模一样。

所以加了按**文件名模式**的兜底扫描（``FONT_PATTERNS`` + ``FONT_DIRS``），
并补了"只有常规体时粗体退回常规体"（wqy-zenhei 这类只有一个字重）。

⚠️ 这条兜底路径**在开发机上永远不会被执行**（Windows 上永远命中 msyh
快速路径）。"只有生产环境才跑的分支"是最容易长期带着 bug 的那类代码，
所以 ``tests/test_fonts.py`` 把 ``FONT_DIRS`` 指到临时目录、清空
``CANDIDATES``，人为造出各种 Linux 布局来验它。

### 字体检查放在**构建时**，而且调真正的发现逻辑

```dockerfile
RUN pip install -e . \
 && python -c "from kindlewarden.render import FontBook; fb = FontBook(); …"
```

不是 ``test -f 某个具体路径`` —— 写死路径换个基础镜像就失效。
让代码自己认，并把选中的字体打出来：缺字体在**构建阶段**就炸，
而不是等第一次渲染才发现。

### 容器里的"本机地址"是错的

``lan_addresses()`` 自动探测到的会是**容器自己的**地址（172.17.x.x），
那个地址在局域网里不存在。Kindle 照着连的现象只是「页面无法显示」，
很难联想到是地址猜错了。

所以加了 ``KINDLEWARDEN_PUBLIC_HOST`` 环境变量覆盖，并且**只返回那一个地址**
—— 把自动探测的结果列在旁边只会让人挑错。它接受
``192.168.1.50`` / ``http://192.168.1.50:8080`` 等几种写法。

---

## 沙箱注意事项

`.tools/sitecustomize.py` 里有两个**纯环境问题**的垫片，
与 kindlewarden 的功能无关。真实部署（`pip install` 到 site-packages）不受影响。

### 1. DSH 沙箱的 ACL 不继承

`os.mkdir(path, 0o700)`（**`tempfile.mkdtemp` 内部用的正是它**）创建出来的目录
不继承沙箱授予工作区的那条 ACE，于是往里写任何东西都被拒：

```
PermissionError: [Errno 13] Permission denied
```

复现：

```powershell
python -c "import os; os.mkdir('d', 0o700); open('d/f','w')"   # 挂
python -c "import os; os.mkdir('d', 0o777); open('d/f','w')"   # 正常
```

后果：pip 解包 wheel 失败；pytest 的 `tmp_path` fixture 直接报错。
垫片把 `0o700` 提升为 `0o777`。

### 2. `--target` 安装不处理 `.pth`

`uv pip install --target .tools` 装上的 pywin32 依赖 `pywin32.pth` 来注册
DLL 搜索路径和 `win32`/`win32com` 子目录。但 `site` 只处理 site-packages 里的
`.pth`，普通 PYTHONPATH 目录会被跳过 → `import mcp` 直接失败
（`No module named 'pywintypes'`）。

垫片手工实现了 `.pth` 协议：`import ...` 行执行，相对路径行加进 `sys.path`。

> 注意不要偷懒对整个 `.pth` 文件 `exec` —— pywin32 的 `.pth` 前几行是
> 相对路径（`win32`、`win32\lib`、`pythonwin`），`exec` 会在第一行
> SyntaxError 然后被 except 静默吞掉。

### 3. pytest 的 basetemp

必须落在工作区内（已写进 `pyproject.toml` 的 `addopts`）。
系统临时目录在工作区外，连读都会被拒。

### 4. 不要用 PowerShell 读写含中文的源文件

踩过一次，代价是重写整个 `server.py`：

```powershell
# 危险：Get-Content -Raw 按 ANSI 码页解码，WriteAllText 再按 UTF-8 写回
$t = Get-Content -LiteralPath $p -Raw
[System.IO.File]::WriteAllText($p, $t, (New-Object System.Text.UTF8Encoding($false)))
```

结果是 UTF-8 被当成 GBK 解释，中文字符大面积变成替换字符 —— **有损，不可逆**。
文件看起来还在，但里全是乱码，Python 直接 `SyntaxError`。

**改用 `edit` / `write` 工具做这类修改**（它们按 UTF-8 处理）。
真要在 PowerShell 里处理，必须显式指定编码：

```powershell
Get-Content -LiteralPath $p -Raw -Encoding UTF8
[System.IO.File]::WriteAllText($p, $t, [System.Text.UTF8Encoding]::new($false))
```

（`.tools/sitecustomize.py` 里那两处 PowerShell 相关的东西不涉及这个：
它们只碰路径和权限，不碰文本内容。）

---

## 测试布局

| 文件 | 覆盖 |
|---|---|
| `test_models.py` | 输入规范化（宽容性）、脱敏、文本度量、存储 vs 显示截断 |
| `test_store.py` | 事件溯源投影、老化规则、残行容错 |
| `test_layout.py` | 版面数学、**锚点稳定性**、欠载回流、无空白洞、居中 |
| `test_render.py` | 尺寸、**确定性**、调色板网格、编码 |
| `test_mcp.py` | 发件箱、退避、**host 离线时非阻塞**、stdio 端到端 |
| `test_server.py` | 启动提示（含「只绑回环」警告）、虚拟网卡地址排序 |
| `test_admin.py` | 管理页接口端到端（真起 HTTP 服务）、**DOM 引用一致性** |
| `test_admin_dom.py` | 管理页**运行时注入的 HTML**：属性完整性、点击真的发出请求、端到端删除（需要 node，缺则跳过） |
| `test_koplugin_contract.py` | KOReader 插件依赖的**服务端契约**：点击区域与渲染同源、详情分页自洽 |
| `test_koplugin_lua.py` | 插件的**跨语言契约**：端点存在性、画布尺寸一致、打包/部署映射 |
| `test_koplugin_device_api.py` | 对着**设备上真实的 KOReader 源码**核对插件用到的每个 API |

### KOReader 插件的四层验证

插件是 Lua，这台机器上跑不起来（没有 lua 解释器），只能在设备上实测。
所以拆成四层，把"能提前抓到的"全部提前抓掉：

| 层 | 抓什么 | 在哪 |
|---|---|---|
| **语法** | 少个 `end`、括号不配对 —— KOReader 加载不了，而设备上的现象只是「菜单里没这一项」 | `tools/check_lua.py`（Python 的 luaparser） |
| **服务端契约** | 端点写错（设备上 404）、画布尺寸不一致（**所有点击偏移，且偏移量随缩放比变化**）、分页与返回区域不自洽 | `test_koplugin_contract.py` + `test_koplugin_lua.py` |
| **框架语义** | 类系统的 `_init`/`init` 调用约定、leaf widget 必须自己画、退出必须留出口 | `test_koplugin_framework_contract.py` + `test_koplugin_exit.py` |
| **设备 API** | 对着设备上那一版 KOReader 的源码，核对每个符号真的存在 | `test_koplugin_device_api.py` + `tools/check_menu_hint.py` |

**为什么第三、四层必不可少**：KOReader 的 master 文档和设备上装的版本可以差很远，
而**本地测试全绿、上机必崩**的错误就藏在这些差异里。实测踩到三个：

1. **`DataStorage:getCacheDir()` 在设备上不存在。**
   照着 master 文档写的，而设备是 **v2026.07.1**，没这个方法 ——
   打开副屏直接 `attempt to call method (a nil value)`。
   正确做法是用 `getFullDataDir()`（那一版有，且能把 `"."` 解析成绝对路径）。

2. **`sorting_hint` 指向不存在的菜单 id 会让 KOReader 崩溃。**
   `menusorter.lua` 里这段**没有兜底**：

       if sorting_hint then
           local m = self:findById(menu_table["KOMenu:menu_buttons"], sorting_hint)
           m = m.sub_item_table or m      -- ← findById 返回 nil 时，这里就崩
           table.insert(m, v)
       end

   而且这个值只能对着**设备那一版**的菜单顺序核对（master 与 v2026.07.1 不同）。
   官方最小插件 `hello.koplugin` 用 `more_tools`，我们用 `tools` —— 两个都合法，
   但**必须先验证再确定**。

3. **模块不一定在 `frontend/`，也不一定是 `.lua`。**
   `libs/libkoreader-lfs` 是编译好的 `.so`；
   `json` / `ltn12` / `socket.http` 在 **`common/`** 下（LuaSocket 的标准位置）。
   第一版检查只找 `frontend/*.lua`，把这三个全判成"不存在" ——
   **是检查写错了，不是插件有问题**（设备上已下载的 PNG 就是它们能用的证据）。

### 异常状态下的两条底线

用户明确提过两条要求：

> 不要出现无法退出的情况，不要出现无法解除休眠状态的情况

这两条比"功能正常"更难保证，因为它们的失效**只在异常状态下暴露**
（服务器关了、长时间放置、待机后唤醒），而暴露时的现象是
"界面卡住 / 按钮点不动 / 永远不休眠"，完全没有排查线索。

所以专门做了一次系统性审计（`tools/audit_exit_paths.py`），覆盖 5 类场景。
查出并修掉的问题：

| # | 问题 | 后果 |
|---|---|---|
| 1 | HTTP 超时 20/30 秒，且 `http.request` **阻塞主线程** | 服务器断开时每轮冻住 34 秒，期间**点退出完全没反应** |
| 2 | 错误用 `InfoMessage` **模态弹窗** | **盖住退出按钮** + **阻止自动休眠** |
| 3 | 完全没处理待机/唤醒 | 醒来时定时器集中触发、WiFi 没连上 |
| 4 | 每轮无条件 `setDirty("full")` | 费电，且持续刷屏**妨碍自动休眠** |
| 5 | shell 客户端 `preventScreenSaver 1` 可能永久残留 | **Kindle 从此永不休眠**，只能重启恢复 |

**第 1 条是"退不出去"的直接原因。** KOReader 是单线程事件循环，
`http.request` 阻塞期间整个界面是死的 —— 用户点退出毫无反应，
看起来就是"卡死了"。60 秒后又重来一次，形成"每 90 秒里 34 秒是冻住的"。
现在超时压到 4/4/8 秒，代价只是"服务器慢时更新晚一点"。

**第 2 条是"无法解除休眠"的直接原因。** 更讽刺的是：有一处弹窗是
**首次打开时"教用户怎么退出"的提示** —— 它自己却盖住了退出按钮 6 秒。
现在所有提示/错误都画在自己的画布上（`_paint_status_text`）：

| | 模态弹窗 | 画布提示 |
|---|---|---|
| 盖住退出按钮 | ❌ 会 | ✅ 不会 |
| 阻止自动休眠 | ❌ 会 | ✅ 不会 |
| 拦输入 | ❌ 会 | ✅ 不会 |

**第 5 条是"无法解除休眠"的根源。** shell 客户端设了
`lipc-set-prop com.lab126.powerd preventScreenSaver 1`，而清除只在
`on_exit` trap 里做。**被 SIGKILL 或直接断电，trap 不会执行** ——
那个属性就永久停留在 1，Kindle 再也不自动休眠、掉电飞快，
而且它不属于任何可见设置项，用户找不到原因。
修法是**启动时先无条件清零**，让残留状态自愈。

### 待机处理：设计成「不依赖事件被送达」

官方事件文档写着：

> Events are sent to the first widget in `UIManager._window_stack`.
> If it is not consumed, then UIManager will try to send it to all
> active widgets (`widget.is_always_active` equals `true`).

所以**"定义了 `onSuspend` 就会被调用"是错的** —— 取决于事件是用
`broadcastEvent` 还是 `sendEvent` 发的、以及窗口在不在栈顶。
这一点本地无法验证。

正确的应对不是去猜，而是**设计成不依赖它**：

* `schedule()` 每次先 `unschedule()` → 任何时刻**最多一个**待触发定时器，
  不可能积压成请求风暴
* HTTP 超时只有几秒 → 醒来撞上没连上的 WiFi 也不会冻住主线程
* 失败只画在画布上 → 不弹窗、不挡退出

**即使这两个处理器从未被调用，界面依然可操作、能退出、能休眠。**
它们只是让"醒来瞬间"更干净。

这是个值得记住的模式：**当你无法验证某个外部行为时，把正确性建立在
自己能控制的不变量上，而不是建立在"那个行为一定会发生"上。**

### 让空闲真的空闲

原来 `refresh` 结尾是**无条件** `UIManager:setDirty(self, "full")` ——
每 60 秒全屏刷一次，哪怕画面一个字都没变。两个坏处：

1. e-ink 全屏刷新要几百毫秒，纯属浪费电（副屏是常驻的）
2. 持续刷屏让 KOReader 认为"有活动"，**妨碍自动休眠** ——
   而"长时间放置后能正常待机"正是用户要求的底线之一

改成有条件重画：内容变了 / 错误状态变了 / 有提示要画或要擦。

顺带发现一个相关问题：临时提示（点待办）原来要等到**下次轮询**才消失 ——
一条 4 秒的提示会挂在屏幕上将近一分钟。现在到期主动擦除。

### 全屏模态窗口必须自己留出口

这是代价最大的一次事故：插件第一版**打开之后没有任何办法退出**，
只能长按电源键重启设备。

两条错误叠加：

1. **以为框架会给出口，其实没有。** `InputContainer:_init` 里是：

       if Device:hasKeys() then
           self.key_events.Home = { { "Home" } }
       end

   PW3 是纯触摸设备，`hasKeys()` 为假 ——
   **"按 Home 键退出"在 Kindle 上从来没被设上**。我默认它在了，没验证。

2. **模态窗口没留出口。** `covers_fullscreen = true` + `modal = true` 吃掉整屏，
   而只注册了 `tap`（用来点卡片），等于把唯一的路也堵死。

修法是**分层**，各层依赖不同的东西：

| 层 | 依赖 | 服务端挂了还能用吗 |
|---|---|---|
| 看得见的「退出」按钮 | 插件自己画（TextWidget + 字体） | ✅ 能 |
| 任意方向滑动 | 手势识别 | ✅ 能 |
| 服务端给的动作区域 | 服务端渲染的 PNG | ❌ 不能 |

**第一层是关键**：它是插件自己画的，不是服务端 PNG 的一部分。
服务端连不上时画面全白 —— 如果退出入口只存在于那张图里，
用户面对的是一块**没有任何提示的白屏**。

`test_koplugin_exit.py` 把这九条约束固化了下来，其中几条特别值得注意：

- 退出按钮**不能**被塞进 `image_ready` 分支（白屏时正是最需要它的时候）
- 按钮的命中判断**必须**排在 `image_ready` 检查**之前**
- 按钮**必须**落在底部边距内（这条会真的去读 `layout.py` 算边界）
- 退出**必须**通知插件，否则**退一次就再也打不开**
  （屏幕能自己关闭，那条路径上 `self.screen` 仍指着死对象，
  下次点菜单会去 close 一个死对象 → 新窗口永远打不开）

### 剥注释：一个踩了三次的坑

检查"源码里**不许**出现某种写法"时，**必须先剥掉注释**。

因为注释里往往正好在解释**为什么不那么写** —— 于是注释本身把规则判成了违规。
同一个坑连踩三次：

1. `test_plugin_does_not_use_autoscale` —— 注释里写着「不能用 `scale_factor = 0`」
2. `test_plugin_uses_a_stable_cache_dir` —— 注释里解释「`getCacheDir` 设备上没有」
3. 设备插件核对脚本 —— 同样两处

所以 `strip_comments()` 现在是**唯一实现**（`tools/check_lua.py`），
测试和验证脚本都 import 它，不各自实现一份。

> JS 那份是独立的 `_strip_js_comments()`（`tests/test_admin_dom.py`）——
> 注释语法不同（Lua 是 `--`，JS 是 `//` 与 `/* */`），没法共用；
> 但它同样**保留行号**（块注释替换成等量换行），这样报出来的行号还对得上。
> 同一课又踩了一次：管理页里那段「反面教材」的注释，
> 在被检查时把规则本身判成了违规。

### 注入的 HTML：属性边界会被**没转义的引号**撑破

管理页的列表行、详情按钮都是 `innerHTML` 注入的。曾经这样拼：

```js
'<button onclick="removeTodo(' + JSON.stringify(t.id) + ')">删除</button>'
```

`JSON.stringify` 产出的是**双引号**字符串，而 HTML 属性本身也用双引号定界：

```html
onclick="removeTodo("todo_abc")"
                    ↑ 解析器在这里就认为属性结束了
```

属性值只剩 `removeTodo(` —— 语法错误。**点击毫无反应**，而且：

- 加载期**不报错**，只在事件真正触发那一刻才炸
- `test_admin_page_inline_handlers_are_defined` **抓不到** ——
  函数确实定义了，坏掉的是属性**边界**

实测这条把**五个**地方一起打哑了：待办删除、待办勾选、待办原地编辑、
简报撤回、简报钉住。用户看到的现象是「删除功能无效」。

**修法**：统一走 `act()` + `q()`（`admin_ui.py`）——
参数 JSON 化后再做 HTML 实体转义（`"` → `&quot;`）。
实体解码发生在属性边界确定**之后**，所以属性不再被撑破。

**测法**（`tests/test_admin_dom.py`）—— 三层，缺一不可：

| 层 | 做法 | 抓什么 |
|---|---|---|
| 源码 | 同一行不许同时出现内联属性和 `JSON.stringify` | 复现那个 bug 的**确切形状** |
| 解析 | Node 跑**真实脚本**产出 HTML → `html.parser` 解析属性 | 属性被撑破 |
| 点击 | 把解析出的属性值**当脚本执行**，看发出什么请求 | "点了没反应" |

要点：

- **不要自己写正则切 HTML 属性** —— 那等于把同一个 bug 再犯一遍。
  用 `html.parser`（真解析器），它会按规范处理实体，跟浏览器一致。
- Node 侧的 harness（`tests/_admin_dom_harness.js`）只桩掉 DOM 与 `fetch`，
  **不复制、不重写**模板逻辑 —— 测的必须是线上那份代码。
- harness 里执行处理器前**必须做实体解码**，否则拿到的是
  `removeTodo(&quot;x&quot;)`，会误报 `Unexpected token '&'`。
- 最后一条是**端到端**：用真实 `/board` 数据生成页面，把按钮发出的请求
  原样打回真服务，确认数据真的没了。前面几层只证明"请求对"，
  这条证明"真的删掉了"。

> **验证测试本身有效**：把 `removeTodo` 改回坏写法，确认有 **6 条**测试变红
> （含端到端那条），再改回来。改完就删的临时脚本放在 `.runtime/`，
> 不进仓库。**抓不到 bug 的测试等于没有测试。**

### 别用 PowerShell 传含引号的 Python

`pwsh -Command` 会先做一层 PowerShell 解析。内联 Python 里的
`|`、`"`、`'...' if ... else '...'` 会被 PowerShell 抢先解释，
报出 `无法将"more_tools"项识别为 cmdlet` 之类的错 —— **看起来像代码问题，
其实是 shell 引号问题**。

踩了两次之后的做法：**一律写成 .py 文件再执行**。
（这个坑和之前"用 `Get-Content -Raw` 编辑 CJK 源文件会损坏内容"是同一类：
**不要让 shell 参与文本处理**。）

几条值得单独说明的测试：

- **`test_admin_page_element_ids_all_exist`** —— JS 里 `$("id")` 引用的元素必须都存在。
  这类错误**不会抛异常**，只会在运行时静默失效（按钮点了没反应）。
  语法检查抓不到。因为沙箱里跑不起浏览器（Chrome 的 IPC 被拦），只能静态查。
- **`test_admin_page_inline_handlers_are_defined`** —— 同理，查内联 `onclick` 调的函数。
  注意它把 `if` 这类关键字排除掉了，因为内联处理器里可以写表达式。
  ⚠️ **它只能证明"函数存在"，证明不了"属性没被撑破"** —— 后者见下面「注入的 HTML」。
- **`test_admin_page_has_no_external_resources`** —— 管理页**不能引任何 CDN**。
  这台机器和 Kindle 同一个局域网，很可能没有外网。
- **`test_device_status_only_counts_kindle_requests`** —— 管理页自己也轮询 `/rev`，
  不区分的话「Kindle 最后在线」永远显示「刚刚」，指标就废了。
- **`test_render_is_deterministic`** —— 同样输入必须产出同样像素。
  否则 `rev` 机制会失效，屏幕会莫名刷新。
- **`test_zones_are_contiguous_without_a_hole`** —— 回归测试：分配时
  「先给高度再折算条数」的舍入余量曾经留在两个板块中间，渲染出来像个空白洞。
- **`test_mcp_call_is_non_blocking_when_host_down`** —— 起真子进程，
  host 指向一个连不上的端口，验证工具调用仍然立刻成功返回。

### 存储 vs 显示：一个容易被搞反的边界

**简报存全文，截断只发生在渲染时。** 这条边界踩过一次坑：

早先的实现是在入库时就把标题截到 20 字、摘要截到 28 字。听上去合理（板上一行就那么大），
但后果是**完整内容在写进日志的同时就永久丢了** —— 事件溯源的意义本来是「日志即真相」，
而真相被有损地存了下来。做管理页的详情功能时才发现无从展示。

现在：

| 层 | 约束 | 目的 |
|---|---|---|
| `models.py` | `*_STORE_MAX`（200 / 1000 字） | 防止往日志里灌长文 |
| `models.py` | `*_DISPLAY_W`（20 / 28 字宽） | **仅供参考**，用于给管理页提示 |
| `render.py` | `_fit()` 按**像素宽度**截断 | 板上实际显示多少 |

改这几个常量时注意别把「提示」当成「截断」。

---

### 夜间 / 横屏：为什么全是服务端改动

**约束**：插件里 ``IMG_W, IMG_H = 1072, 1448`` 是**硬编码**的
（它按这个算缩放比和点击坐标）。所以服务端**永远**只能交付这个尺寸 ——
不管开什么模式。

这恰好决定了两个功能的实现方式：

| 模式 | 做法 | 尺寸 |
|---|---|---|
| 夜间 | 渲染完**反色**（`255 - v`） | 不变 |
| 横屏 | 先按 **1448×1072** 排版，再**旋转 90°** | 1072×1448，正好对上 |

好处是设备侧一行都不用改 —— 改配置就生效，不用重新打包部署插件。

代价是**横屏要物理转动设备**（PW3 没有重力感应，KOReader 也不会替我们转）。

#### 夜间模式能做纯后处理的原因

调色板是 17 的倍数（``255/15``），而 ``255 - 17n = 17(15-n)``
**仍是 17 的倍数** —— 所以反色之后灰阶依然落在设备的 16 级网格上，
不需要重新量化。这条性质有测试盯着（``test_night_mode_stays_on_the_16_level_grid``）；
哪天有人往渲染里塞了不在网格上的灰（比如抗锯齿），夜间模式就会出现抖动噪点。

#### 用了"重新绑定模块全局变量"来省掉穿参数

渲染器里有 **50 多处**引用 ``CANVAS_W/CANVAS_H``。横屏要换个宽高比排版，
如果把这些尺寸当参数穿下去，要改遍所有函数签名 —— 而且**漏掉一处会静默错位**
（那一处仍按旧尺寸画）。

所以改成**重新绑定模块级全局变量**（`layout.set_canvas` / `render.set_canvas`）。
Python 在函数执行时才解析全局名，所以改一处即全局生效。

**安全性依赖一个前提**：渲染全程在 ``BoardService._lock`` 里跑，是**串行**的，
不存在两个线程同时按不同尺寸渲染。这一点不成立，这个做法就是错的。

必须用 ``try/finally`` 恢复，否则一次渲染异常会让**后续所有渲染**都用错尺寸
（`test_canvas_restored_even_when_render_raises` 盯住这条）。

#### 横屏最容易错的三处

**① 画转了、坐标没转。** 现象是"按钮看得见但点不中"，
而且**不报任何错**。坐标变换用半开区间推导（矩形 ``[x0,y0,x1,y1]`` 里 x1 不含）：

```
顺时针 (x,y) → (H-y, x)    矩形 [x0,y0,x1,y1] → [H-y1, x0, H-y0, x1]
```

第一版把矩形当闭区间、又加了 `+1`，10×10 的框变换后变成 11×11 ——
**面积变了就不是纯旋转**，测试当场抓到。

**② PIL 的旋转常量名有历史包袱，别按名字猜方向。** 实测（左上角标记块）：

| 常量 | 标记跑到 | 实际方向 |
|---|---|---|
| `ROTATE_90` | 左下 | **逆时针** |
| `ROTATE_270` | 右上 | **顺时针** |

我按名字硬对（`cw ↔ ROTATE_90`）就错了，画和坐标**反着转**。
`test_image_rotation_matches_coordinate_transform` 用**像素级验证**锁住两者一致。

**③ 同一种逻辑写在两个端点，就一定会漏掉一处。**
我只在 `/board` 里加了坐标旋转，`/screen.json` 忘了 —— 而**插件用的正是后者**。
所以测试全绿但设备上不对。现在两个端点都走
`board_taps_for_delivery`，并有测试盯住"它们给出一样的矩形"。

#### `exit_zone` 不旋转（和 taps 相反）

这一条看着该统一，其实**必须不同**：

* 它是**插件画的**（不依赖服务端，见"退不出去"那次事故）。
  插件拿到矩形后画一个固定大小（152×34）的**横**按钮 ——
  把矩形转成 34×152 的竖条，按钮会**溢出 118px**，点击区域也对不上。
* 不旋转并不会盖住内容：MARGIN 四边都是 40px，所以
  「交付图的底部边带」旋转后正好落在「排版图的左/右边距带」——
  两边都是布局引擎留白的区域，本来就是空的。

实测反推（顺时针）：竖屏 `[880,1410,1032,1444]` → 排版图 `[1410,40,1444,192]`，
而排版画布 1448 宽、右边距从 1408 开始 —— 确实在边距带里。

另外 ``render.exit_zone_rect()`` 读的是**当前画布**，横屏时会返回排版坐标系的值
（右边界 1408），而交付坐标系只有 1072 宽 —— 所以交付前要用
``exit_zone_rect_at(DEVICE_W, DEVICE_H)`` 重算。

#### `_fit` 必须返回**单行** —— 一个"看几行"引发的重叠

用户报过「**待办与最近完成最后一条的第二行文字重叠**」。
分区坐标查下来没有重叠（29px 间隙），所以问题在**绘制**。

根因在 ``_fit``：

```python
# 旧实现
if _tw(d, s, f) <= max_w:
    return s          # ← 原样返回
```

而 ``_tw`` 对多行会**先压平再量**（因为 Pillow 的 ``textlength`` 遇到
``\n`` 会抛 ``ValueError``）。于是当原文**含换行、但压平后没超宽**时，
``_fit`` 就把带换行的原串返回了 —— 调用方拿它交给 ``d.text``，
而 ``d.text`` 遇到 ``\n`` 会**画多行**，直接溢出那条固定高度的条目。

触发条件一点不罕见，因为简报的 ``summary`` 本来就**支持多行**：

```
首行短 + 换行   →  压平宽 560（< TEXT_W 922）
               →  旧实现返回含 2 个换行的串
               →  d.text 画 2 行、高 116px
               →  从 y+66 画到 y+182，而条目只有 128px
               →  盖住了下面的待办
```

现在 ``_fit`` 入口处先压平，保证输出永远是单行
（``test_fit_never_returns_multiline_text`` 用 8 种换行形状 × 4 种宽度盯住）。

#### 横屏用一套独立的紧凑度量

横屏画布是 1448×**1072** —— 竖向空间比竖屏少 376px。沿用竖屏度量会挤满：
简报只能放 3 条，「待办」悬在中间离底部很远，两端却挤得没有呼吸。

所以 ``layout.metrics()`` 按方向返回不同的一套：

| 项 | 竖屏 | 横屏 | 为什么 |
|---|---|---|---|
| 简报条目 | 128（两行） | **84（只标题一行）** | 横屏很宽，标题一行放得下 |
| 待办条目 | 64 | **56** | 更紧凑 |
| 环境区 | 132 | **74** | 见下：量出来的 |
| 告警条 | 196 | **168** | 内部节奏按比例收紧 |
| 最近完成上限 | 配置值 | **4** | 用户明确要求 |

**"画几行"由条目高度决定，不另传开关**：``_draw_brief`` 比较
``h >= BRIEF_TWO_LINE_MIN_H``。高度是**布局已经算好的事实**，
渲染照着它画，两边天然一致。如果改成传 ``compact=True``，
调用方漏传就会出现"布局按单行留位、渲染画了两行"—— 又是重叠。

**横屏的「待办贴底」**：竖屏是"简报+待办一起在弹性区垂直居中"，
横屏则把多余空白**全部塞在简报之后**，于是待办落到紧贴环境区之上。
做法是给 todos 预留它要的高度，简报拿剩下的，空白加在两块之间。

#### 分区高度要**量出来**，不能拍脑袋

环境区那条最初给了横屏 112px。用户反馈「待办下方的日期显示往下放，
现在占用了太多的位置」。一量才发现内容根本用不了那么多：

```
时钟 56 号字 → 墨迹 43px（三者里最高）
日期 38 号字 → 37px
天气 32 号字 → 30px
上下各留 ~15px 呼吸 → 73px 就够
```

给 112px = 白扔 41px。改成 **74px** 之后一举两得 ——
环境区是**贴底**锚定的，压矮它等于**顶边下移**：

```
112px → 环境区顶 836，待办最远到 824
 74px → 环境区顶 874，待办最远到 862
```

「往下放」和「少占地方」本来就是同一件事的两面。

#### ⚠️ 偏移量和分区高度，只能有一处是真的

这一节是同一个坑的**第三次**出现，值得单独记：

| 次 | 出问题的地方 | 症状 |
|---|---|---|
| 1 | ``_fit`` 返回多行 | 简报文字溢出条目，盖住待办 |
| 2 | ``_draw_todo`` 写死 ``y+8`` | 待办文字溢出 56px 的条目 9px |
| 3 | ``_draw_ambient`` 写死偏移 | 分区压矮后日期画到区外 |

共同点：**绘制里另写了一套位置，和布局算出来的高度对不上。**
只要"分区高度"改了，写死的那套就静默错位 —— 而不报任何错。

现在的规矩：**凡是纵向位置，一律从分区中线推**。

```python
cy = y + h / 2                                  # 分区中线
d.text((x, cy - _text_v_offset(d, s, f)), s, …)  # 把墨迹对准中线
```

``_text_v_offset`` 返回墨迹中心相对绘制 y 的偏移（按钮文字居中用的
是同一个函数）。这样竖屏 64 / 横屏 56、环境区 132 / 74 都自动正确。

⚠️ 顺带一个教训：``_draw_todo`` 原来的签名里**根本没有条目高度**
（``def _draw_todo(d, t, y, f)``）—— 它想跟着布局走也做不到。
**函数签名里缺了那个决定布局的量，就是一个迟早会踩的坑。**

#### 一个通用的教训：测试判据太弱等于没测

横屏的 `board_payload` bug（矩形按竖屏算）我第一版**没抓到**，
因为判据是"矩形宽度"—— 而竖屏卡片旋转后和横屏卡片旋转后**宽度都是 128**
（那是卡片高度），根本区分不出来。

换成比**长边**（992 竖屏 vs 1368 横屏）才抓到。

**写测试时要问：这个判据能把正确和错误区分开吗？**
区分不开的判据，绿色也只是自欺。

---

## 版面迭代的正确姿势

改版面时**不要**去调设备，用：

```powershell
python -m kindlewarden.cli seed-demo
python -m kindlewarden.cli preview --out mockups/out/preview.png
```

`preview` 是纯本地的（直接从事件日志渲染），几秒一轮。
`tools/mockup.py` 是更早的独立试验场，改结构性想法时更顺手。

---

## 事件日志（调试神器）

所有状态都在一个 append-only JSONL 里：

```
%LOCALAPPDATA%\kindlewarden\board.jsonl      （或用 KINDLEWARDEN_DATA 覆盖）
```

因为是事件溯源，你可以：

- 把它整个拷走做测试固件
- 手工删掉最后一行（模拟崩溃残行）验证容错
- 改配置后重放，看历史画面在不同版面下长什么样

投影是纯函数（`store.project`），没有 IO、没有 `time.time()` ——
所有时间都来自事件本身。所以「看板显示错了」是可复现的 bug。
