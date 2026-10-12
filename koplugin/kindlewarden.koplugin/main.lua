--[[
kindlewarden —— KOReader 插件（副屏看板）

为什么做成 KOReader 插件，而不是继续用「shell + fbink 盖图」：

  fbink 直接往 framebuffer 画，而 Kindle 的 Java framework 也在画同一个、
  **并且仍然处理触摸**。结果是画面要靠不断重绘去抢，而且点屏幕会穿透到底下的书。

  KOReader 是**独占屏幕的全屏应用**：排版、刷新、触摸都归它管。站在它上面，
  「显示一张图 + 处理点击」是常规能力，不用再和谁抢。

分工（和整个项目一致：**服务端决定排版，设备端是哑终端**）：
  服务端  →  渲染 PNG + 给出点击区域（图像坐标）
  本插件  →  下载 PNG、显示、把点击坐标换算回图像坐标、按区域派发动作

插件完全不需要知道版面长什么样，改版面只动服务端。
]]

local Blitbuffer = require("ffi/blitbuffer")
local FrameContainer = require("ui/widget/container/framecontainer")
local Geom = require("ui/geometry")
local ImageWidget = require("ui/widget/imagewidget")
local InputContainer = require("ui/widget/container/inputcontainer")
local NetworkMgr = require("ui/network/manager")
local TextWidget = require("ui/widget/textwidget")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local DataStorage = require("datastorage")
local Font = require("ui/font")
local json = require("json")
local logger = require("logger")
local ffiUtil = require("ffi/util")
local lfs = require("libs/libkoreader-lfs")
local ltn12 = require("ltn12")
local http = require("socket.http")
local _ = require("gettext")
local Device = require("device")

local Screen = Device.screen

-- 看板图像的原始尺寸，必须和服务端 layout.py 的 CANVAS_W/CANVAS_H 一致。
-- 插件靠它把点击坐标换算回服务端那套坐标。
local IMG_W, IMG_H = 1072, 1448

local DEFAULT_SERVER = "http://192.168.1.50:8577"
local AUTOREFRESH_SEC = 60

-- 所有端点集中在这里。
--
-- 两个理由：
--   1. 服务端改了路径，只需要改这一处
--   2. Python 侧的契约测试会**拿这张表和 server.py 的路由逐条对照** ——
--      端点写错在本地就能发现，不用等插上设备看到 404
--
-- ⚠️ 不要用字符串拼接去凑路径（比如 "/detail" .. ".json"）——
--    那样上面的对照就抓不到了，等于把这道防线关掉。
local EP = {
    rev          = "/rev",
    board_json   = "/screen.json",
    board_png    = "/screen.png",
    detail_json  = "/detail.json",
    detail_png   = "/detail.png",
    offline_png  = "/offline.png",
}

-- 「退出」按钮的兜底矩形，用**图像坐标**表示。
--
-- ⚠️ 这只是一个**兜底值**：正常情况下 ``exit_rect`` 会由按钮控件的
--    **实际尺寸**算出来（见 init 里的 ``self.exit_rect``），
--    这样"画在哪里"和"哪里能点"天然同源，不会漂移。
--
--    为什么要这样：绘制位置和命中区域分开算，是这类方案最经典的 bug ——
--    改了 padding 或字体，画出来的按钮和能点的区域就对不上了。
--    这个项目在服务端那边也踩过同一个坑（卡片矩形必须和渲染同源）。
--
-- 兜底值：服务端没给 exit_zone 时用它。
--
-- ⚠️ 必须和 render.py 的 exit_zone_rect() **完全一致**：
--    右下角，152 宽 × 34 高，离底边 4px。
--    即  y0 = IMG_H - 4 - 34 = IMG_H - 38，y1 = IMG_H - 4。
--    （我第一版写成 IMG_H - 54，那是把"高度"和"偏移"搞混了 ——
--      有测试专门比对这两个值，改一边必须同时改另一边。）
local EXIT_ZONE_FALLBACK = { IMG_W - 192, IMG_H - 38, IMG_W - 40, IMG_H - 4 }

-- ---------------------------------------------------------------- HTTP
--
-- ⚠️⚠️ 这里的所有超时都必须**短**，因为 KOReader 是单线程事件循环 ——
--      ``http.request`` 会**阻塞主线程**，期间屏幕不刷新、触摸不响应。
--
-- 原来的值是 20 / 30 秒，后果非常严重：
--   服务器断开时，每轮刷新都会把主线程冻住最多 30 秒。
--   用户在这 30 秒里点「退出」**完全没反应** —— 看起来就是"卡死了退不出去"。
--   而 60 秒后又会重来一次，形成"每 90 秒里 34 秒是冻住的"。
--
-- 所以：超时压到 4 / 8 秒，并且连接失败要尽快返回。
-- 代价只是"服务器慢的时候画面更新晚一点"，而收益是**界面永远保持可操作**。
local HTTP_TIMEOUT_REV = 4        -- 只是取一个 rev 字符串，应该毫秒级返回
local HTTP_TIMEOUT_JSON = 4       -- 点击区域 JSON，几十 KB
local HTTP_TIMEOUT_PNG = 8        -- 图片 65KB，给宽一点

local function get_url(url, timeout)
    local chunks = {}
    local ok, code = http.request{
        url = url,
        sink = ltn12.sink.table(chunks),
        timeout = timeout or HTTP_TIMEOUT_JSON,
    }
    if code ~= 200 then
        return nil, ("HTTP %s"):format(tostring(code))
    end
    return table.concat(chunks)
end

local function download(url, path, timeout)
    local tmp = path .. ".tmp"
    local fh = io.open(tmp, "wb")
    if not fh then return false, "打不开临时文件" end

    -- ltn12.sink.file 会在结束时关掉 fh
    local ok, code = http.request{
        url = url,
        sink = ltn12.sink.file(fh),
        timeout = timeout or HTTP_TIMEOUT_PNG,
    }
    if code ~= 200 then
        os.remove(tmp)
        return false, ("HTTP %s"):format(tostring(code))
    end
    os.remove(path)          -- 有些文件系统 rename 覆盖不了，先删
    if not os.rename(tmp, path) then
        os.remove(tmp)
        return false, "改名失败"
    end
    return true
end

-- ---------------------------------------------------------------- 类定义

-- ⚠️⚠️ 这个类定义**必须排在所有 ``function WardenScreen:xxx`` 之前**。
--
-- 踩过一次：把 ``_reap_rival_clients`` / ``_heal_stuck_input`` 插到了它前面，
-- 于是加载时报
--
--     main.lua:156: attempt to index global 'WardenScreen' (a nil value)
--
-- Lua 是逐行执行的，那条 ``function WardenScreen:xxx`` 执行时
-- ``WardenScreen`` 还是 nil。后果是 **KOReader 直接丢弃整个插件** ——
-- 菜单里那一项凭空消失，而且现象上完全看不出是"定义顺序"的问题。
--
-- **语法检查抓不到这个错**（它不是语法错误，是运行时的作用域错误），
-- 所以 tools/check_lua.py 里专门加了一条"定义顺序"检查。
local WardenScreen = InputContainer:extend{
    covers_fullscreen = true,
    is_borderless = true,
    modal = true,
    -- ⚠️ 这两个必须显式给空表：
    --    InputContainer 的 onGesture 会 pairs(self.ges_events)、
    --    onKeyPress 会 pairs(self.key_events)。它们平时由 InputContainer:_init 建，
    --    但我们自己定义了 init，_init 不会跑 —— 不给兜底就是 pairs(nil) 崩溃。
    ges_events = {},
    key_events = {},
}

-- ---------------------------------------------------------------- 清理竞争者

-- 启动时杀掉可能存在的旧 shell 客户端。
--
-- ⚠️⚠️ 这是踩过的一次严重故障，务必理解为什么必须做这件事。
--
-- 设备上有**两套**客户端：
--   * 旧方案：shell + fbink，用 ``fbink`` **直接写 framebuffer**
--   * 新方案：本插件（KOReader 里的一个全屏窗口）
--
-- 如果旧的还在后台跑，它会每 60 秒把服务端的 PNG 直接刷到屏幕上 ——
-- **覆盖掉 KOReader 画的所有东西**，包括我们自画的「退出」按钮。
--
-- 用户看到的现象（实测报告）：
--   * 看板内容还在更新（那是 shell 客户端在画）
--   * 「退出」按钮消失（服务端 PNG 里没有按钮）
--   * 触摸、滑动、点条目**全部无效**（事件给了 KOReader，画面却是 fbink 画的）
--   * 电源键也没反应（屏幕不刷新，看起来像死了）
--
-- 而且 shell 客户端设了 ``preventScreenSaver 1``，活着就一直在设 ——
-- 于是设备**永远不休眠**。
--
-- 结论：**同一块屏幕只能有一个程序在画。** 插件启动时主动清掉旧的，
-- 这比"让用户记住别点书库里的那个"可靠得多。
function WardenScreen:_reap_rival_clients()
    local pidfile = "/tmp/warden.pid"

    local ok, err = pcall(function()
        -- 1. 按 PID 文件杀（旧客户端自己维护的，最精确）
        local f = io.open(pidfile, "r")
        local pid = nil
        if f then
            pid = f:read("*l")
            f:close()
        end
        pid = pid and pid:match("^(%d+)$") or nil

        if pid then
            ffiUtil.execute("/bin/sh", "-c",
                ("kill %s 2>/dev/null; sleep 0.2; kill -9 %s 2>/dev/null")
                :format(pid, pid))
            logger.info("kindlewarden: 已停掉旧 shell 客户端 pid=" .. pid)
        end

        -- 2. 兜底：按进程名扫一遍（PID 文件可能丢了、过期了、或被杀剩）
        --
        --    用 `ps | grep | awk` 而不是 pkill —— busybox 里不一定有 pkill。
        --    匹配 warden.sh 的完整路径特征，避免误杀。
        ffiUtil.execute("/bin/sh", "-c",
            "for p in $(ps -ef 2>/dev/null | grep -F 'warden.sh' "
            .. "| grep -v grep | awk '{print $2}'); do "
            .. "kill $p 2>/dev/null; done; "
            -- busybox 的 ps 可能不支持 -ef，退回无参形式
            .. "for p in $(ps 2>/dev/null | grep -F 'warden.sh' "
            .. "| grep -v grep | awk '{print $1}'); do "
            .. "kill $p 2>/dev/null; done")

        -- 3. 清掉 PID 文件，免得下次误判"已经在跑"
        os.remove(pidfile)

        -- 4. **恢复休眠**。旧客户端设过 preventScreenSaver=1，
        --    而在被 SIGKILL 的情况下它的 trap 不会执行 ——
        --    那个属性就永久停在 1，设备**永远不会自动休眠**，掉电飞快，
        --    而且它不属于任何可见设置项，用户根本找不到原因。
        --    这里无条件清零：插件自己也不靠它（休眠交给 KOReader 管）。
        ffiUtil.execute("/usr/bin/lipc-set-prop",
            "com.lab126.powerd", "preventScreenSaver", "0")
    end)

    if not ok then
        logger.warn("kindlewarden: 清理旧客户端时出错（不影响使用）：", err)
    end
end

-- ---------------------------------------------------------------- 输入自愈

-- 检测并解除"输入被永久抑制"的状态。
--
-- ⚠️⚠️ 这是"点什么都没反应"的第二个原因（第一个是旧 shell 客户端抢屏）。
--
-- KOReader 在待机前会调 ``Input:inhibitInput(true)``（见 ``Device:_beforeSuspend``）：
--
--     function Input:inhibitInput(toggle)
--         if toggle then
--             self.handleKeyBoardEv = self.handlePowerManagementOnlyEv
--             self.handleTouchEv = self.voidEv          -- 触摸丢进虚空
--
-- 也就是说**期间触摸事件被完全丢弃，电源键也只剩电源管理用途**。
-- 恢复靠 ``_afterResume`` 调 ``inhibitInput(false)``，或者
-- ``inhibitInputUntil(秒)`` 里的定时器。
--
-- **如果"抑制"发生了但"恢复"没走完**（定时器被打断、待机期间进程状态异常……），
-- 输入就**永久失效** —— 现象和用户报的一模一样：
-- 触摸、滑动、点条目、连电源键都没有任何反应。
--
-- 自愈办法：在每次轮询里检查一下，如果发现输入还处于被抑制状态，
-- 就主动放开。判断依据是 KOReader 自己替换掉的那两个 handler ——
-- ``self._key_ev_handler`` 非空就说明抑制还没解除（见上面源码：
-- 恢复时会把它们清回 nil）。
--
-- 只做"解开"不做"上锁"，所以不会干扰正常的待机流程。
function WardenScreen:_heal_stuck_input()
    -- ⚠️ 待机期间"输入被抑制"是**正常的**，绝不能在那时去解开 ——
    --    那会让设备在睡着的时候还在处理触摸，反而制造问题。
    --    只有**醒着却还被抑制**才是真故障。
    if Device.screen_saver_mode then
        return
    end

    local ok, err = pcall(function()
        local input = Device.input
        if not input then
            return
        end

        -- 判据：inhibitInput(true) 时会把原 handler 存进 _key_ev_handler /
        -- _abs_ev_handler，并在恢复时清空。它们还留着 = 没恢复。
        local stuck = (input._key_ev_handler ~= nil)
            or (input._abs_ev_handler ~= nil)

        if stuck then
            logger.warn("kindlewarden: 检测到输入被抑制且未恢复，主动解除")
            input:inhibitInput(false)
        end
    end)

    if not ok then
        logger.warn("kindlewarden: 检查输入状态时出错（忽略）：", err)
    end
end

-- ---------------------------------------------------------------- 看板屏幕
--
-- （``WardenScreen`` 的类定义见上方「类定义」一节 ——
--   它**必须**排在所有 ``function WardenScreen:xxx`` 之前。）

function WardenScreen:init()
    -- ⚠️ 第一件事：清掉可能还在后台画屏的旧 shell 客户端。
    --
    -- 必须排在**任何绘制之前**。否则会看到这样的画面：
    -- 我们先画好，旧客户端紧接着用 fbink 覆盖掉 ——
    -- 结果就是"画面在、但退出按钮不见了、点什么都没反应"。
    self:_reap_rival_clients()

    local sw, sh = Screen:getWidth(), Screen:getHeight()

    -- 自己算缩放比，**不用 scale_factor = 0（自适应）**。
    -- 自适应会把图居中并产生偏移，点击换算要额外减掉那个偏移，容易算错；
    -- 自己算好之后绘制原点已知，换算只有一次乘除。
    self.scale = math.min(sw / IMG_W, sh / IMG_H)
    self.disp_w = math.floor(IMG_W * self.scale)
    self.disp_h = math.floor(IMG_H * self.scale)
    self.origin_x = math.floor((sw - self.disp_w) / 2)
    self.origin_y = math.floor((sh - self.disp_h) / 2)

    self.dimen = Geom:new{ x = 0, y = 0, w = sw, h = sh }

    self.server = G_reader_settings:readSetting("kindlewarden_server")
        or DEFAULT_SERVER

    self.taps = {}
    self.screen_kind = "board"
    self.brief_id = nil
    self.page = 1
    self.shown_rev = nil
    -- 服务端给的退出按钮保留区（见 _compute_exit_rect 的说明）。
    -- 取到之前是 nil，_compute_exit_rect 会自己兜底算一个。
    self.exit_zone = nil
    self.image_ready = false
    self.busy = false
    self.last_error = nil
    self.image_widget = nil
    self._closed = false
    self.notice = nil
    self.notice_until = 0
    -- 内容是否变化 —— refresh 据此决定要不要重画（见那里的注释）
    self._image_changed = true

    -- 缓存目录。
    --
    -- ⚠️ 用 getFullDataDir()，不是 getCacheDir()，也不是 getDataDir()：
    --
    --   - **getCacheDir() 在设备上的 KOReader v2026.07.1 里根本不存在** ——
    --     照着 master 的文档写会直接 attempt to call method (a nil value)。
    --     这个坑是靠读设备上真实的 datastorage.lua 发现的，不是靠猜。
    --   - getDataDir() 在 Kindle 上走 else 分支返回 "."（相对路径），
    --     随启动方式变化，图会不知道写到哪儿去。
    --   - getFullDataDir() 专门负责把 "." 解析成绝对路径，这个版本里有。
    --
    -- 顺带好处：落在 /mnt/us/koreader/kindlewarden/ 是 USB 可见的，
    -- 插上电脑就能直接看到插件下载下来的 PNG —— 排查时非常有用。
    self.cache_dir = DataStorage:getFullDataDir() .. "/kindlewarden"
    if lfs.attributes(self.cache_dir, "mode") ~= "directory" then
        lfs.mkdir(self.cache_dir)
    end

    -- 退出方式。
    --
    -- ⚠️⚠️ 这里踩过一次**很痛的坑**：第一版只注册了 tap，结果打开之后
    --      **没有任何办法退出**，只能长按电源键重启设备。
    --
    -- 根因：PW3 是纯触摸设备，``Device:hasKeys()`` 为假，所以
    -- ``InputContainer:_init`` 里那句 ``key_events.Home`` **根本没被设上** ——
    -- "按 Home 键退出"在 Kindle 上压根不存在。我当时默认它在了。
    -- 教训：全屏模态窗口**必须自己留出口**，不能指望框架给。
    --
    -- 现在做**三层**，从最好发现到最兜底：
    --   1. 右下角看得见的「退出」按钮（插件自己画，见 paintTo）
    --      —— 服务端连不上、画面全白时**依然在**，这是关键
    --   2. 任意"有意图的拖动"—— swipe 和 pan 都算
    --   3. 服务端在画面里给的动作区域（kind = "back"/"close"）
    --
    -- ⚠️ 为什么滑动要注册**两个**手势（swipe + pan）：
    --    手势识别有阈值 —— ``SWIPE_INTERVAL_MS = 900``、``PAN_THRESHOLD = 35``
    --    （按 DPI 缩放）。滑得慢一点、短一点就会被识别成 ``pan`` 而不是 ``swipe``，
    --    那样"滑动退出"就静默失效了。退不出去要重启设备，所以两个都收。
    --
    -- ⚠️ 为什么**不**注册这两个（都试过，取舍不对）：
    --    * ``touch``  —— "一碰就发"，会让**每次点击卡片都变成退出**
    --    * ``hold``   —— ``HOLD_INTERVAL_MS = 500``，手指按住超过半秒就触发。
    --                    而 ``TAP_INTERVAL_MS = 0``（tap 没有最小时长）。
    --                    于是"点卡片时稍微按久一点"会**变成退出而不是打开详情** ——
    --                    这会破坏正常操作，比多点一次退出按钮糟得多。
    --
    --   判断依据：退出入口的**代价**是"多点一次"，而误退出的代价是
    --   "正常操作用不了"。所以宁可少收手势，也不要侵占 tap。
    self:registerTouchZones({
        {
            id = "kindlewarden_tap",
            ges = "tap",
            screen_zone = { ratio_x = 0, ratio_y = 0, ratio_w = 1, ratio_h = 1 },
            handler = function(ges) return self:on_tap(ges) end,
        },
        {
            id = "kindlewarden_exit_swipe",
            ges = "swipe",
            screen_zone = { ratio_x = 0, ratio_y = 0, ratio_w = 1, ratio_h = 1 },
            handler = function(ges) return self:on_swipe(ges) end,
        },
        {
            id = "kindlewarden_exit_pan",
            ges = "pan",
            screen_zone = { ratio_x = 0, ratio_y = 0, ratio_w = 1, ratio_h = 1 },
            handler = function(ges) return self:on_swipe(ges) end,
        },
    })

    -- 「退出」按钮的控件。
    --
    -- 用 ``FrameContainer`` 包一个 ``TextWidget``，画成**带边框的按钮**。
    --
    -- 为什么非要边框：第一版只是一行深灰文字「退出 ✕」，看不出是个按钮 ——
    -- 而这跟"被困住"是**同一类问题：出口存在，但不可发现**。
    -- 全屏窗口里必须有一个一眼就知道能点的地方。
    --
    -- 用 pcall 包住：**新加的东西绝不能让核心功能变脆**。
    -- 万一字体取不到（``Font:getFace`` 在字体缺失时返回 nil）或者
    -- 尺寸计算出问题，按钮建不起来只是少一个入口 ——
    -- 滑动退出这条**不依赖任何控件**的路依然在。
    local ok, widget = pcall(function()
        -- ⚠️ 字号和内边距是**按退出区高度倒推**的，别随便调大。
        --
        -- 退出区只有 34px 高（它是底部边距那条 40px 带子里能腾出的最大空间），
        -- 而 FrameContainer 的高度 =
        --     TextWidget 高（≈ 字号 × 1.5 + 内边距×2）
        --   + (bordersize + margin) × 2
        --   + padding×2
        --
        -- 第一版用 18 号字 + padding 6 + 边框 2，算出来约 49px ——
        -- **比退出区高 15px，底部直接画出画布外**。
        -- 用户看到的就是"按钮下边缘跑到页面外"。
        --
        -- 现在 14 号字 + padding 1 + 边框 1 ≈ 28px，稳稳装在 34px 里。
        local label = TextWidget:new{
            text = _("退出 ✕"),
            face = Font:getFace("cfont", 14),
            fgcolor = Blitbuffer.COLOR_BLACK,
            padding = 1,
        }
        -- 位置参数必须显式写成 [1] —— 直接混在命名参数后面虽然合法，
        -- 但可读性差，而且依赖"位置参数落在 1"这条隐含规则。
        -- FrameContainer 的 getSize/paintTo 都读 self[1]，写清楚更安全。
        local framed = FrameContainer:new{
            bordersize = 1,
            color = Blitbuffer.COLOR_DARK_GRAY,
            background = Blitbuffer.COLOR_WHITE,
            radius = 5,
            padding = 1,
            padding_left = 10,
            padding_right = 10,
        }
        framed[1] = label
        return framed
    end)
    self.exit_widget = (ok and widget) or nil
    if not ok then
        logger.warn("kindlewarden: 退出按钮控件创建失败，仍可用滑动退出：", widget)
    end

    -- 由控件的**实际尺寸**推出可点矩形 —— 让"画在哪里"和"哪里能点"同源。
    --
    -- 放在右下角、底边距内：底边距是 y ∈ [CANVAS_H - 40, CANVAS_H] = [1408, 1448]。
    -- 按钮比边距高时向上溢出（压到状态栏一点），但**绝不能溢出屏幕底边** ——
    -- 屏幕外的部分点不到，按钮就成了半残的。
    self.exit_rect = self:_compute_exit_rect()

    -- 第一次打开时提示怎么退出 —— 否则用户只能靠猜（或者重启设备）。
    --
    -- ⚠️ 用**画布提示**而不是 InfoMessage。这里特别讽刺：
    --    一个"教你怎么退出"的模态弹窗，本身会**盖住退出按钮 6 秒** ——
    --    等于在最需要它的时候把它藏起来了。而且弹窗期间不会自动休眠。
    if not G_reader_settings:isTrue("kindlewarden_hint_shown") then
        G_reader_settings:makeTrue("kindlewarden_hint_shown")
        self.notice = _("退出：点右下角「退出 ✕」，或任意方向滑动屏幕")
        self.notice_until = os.time() + 12
    end

    UIManager:setDirty(self, "full")
    self:refresh()
end

function WardenScreen:getSize()
    return Geom:new{ w = Screen:getWidth(), h = Screen:getHeight() }
end

function WardenScreen:img_path(name)
    return self.cache_dir .. "/" .. name
end

-- 算出退出按钮在**图像坐标系**里的矩形。
--
-- 关键约束：**绝不能让按钮超出屏幕底边**。屏幕外的部分点不到 ——
-- 那会让按钮变成半残的（下半截可见、点不到），比没有按钮更糟。
--
-- 屏幕底边在图像坐标里就是 IMG_H。所以底边固定贴到 IMG_H 减一点余量，
-- 高度不够时**向上长**（宁可压到状态栏一点，也不能溢出屏幕）。
function WardenScreen:_compute_exit_rect()
    -- ⚠️ **优先用服务端给的 exit_zone，不要自己算位置。**
    --
    -- 踩过一次：插件自己算（贴屏幕底边、按控件尺寸向上长），
    -- 而服务端渲染详情页时也在右下角画「‹ 上一页 / 下一页 ›」——
    -- 两边**各算各的**，结果完全重叠（重叠 45px），两个按钮都点不准。
    --
    -- 现在服务端通过 ``exit_zone`` 字段给出**唯一的**矩形，
    -- 并且保证不在那里画任何东西。插件照画即可，不可能再撞。
    local rect
    if self.exit_zone and #self.exit_zone == 4 then
        rect = self.exit_zone
    else
        rect = self:_fallback_exit_rect()
    end

    -- ⚠️⚠️ **夹紧到画布内 —— 这是根本保证。**
    --
    -- 踩过一次：服务端给的区只有 36px 高，而插件那个带边框的按钮
    -- 实际约 42~49px。从区顶开始画，**底部就溢出到画布外**了 ——
    -- 用户看到的是"按钮下边缘跑到页面外"。
    --
    -- 光靠"服务端给够尺寸"不够稳：字体不同、内边距改了、
    -- KOReader 版本差异，都会让控件高度变化。
    -- 所以这里**无论拿到的矩形是什么、控件多高，都保证画在画布内**。
    return self:_clamp_rect(rect)
end

-- 兜底矩形：服务端没给 exit_zone 时（老版本、请求失败）自己算一个。
-- 位置和 render.py 的 exit_zone_rect() 保持一致。
function WardenScreen:_fallback_exit_rect()
    local w = 152
    local h = 48
    local right = IMG_W - 40
    local bottom = IMG_H - 6
    return { right - w, bottom - h, right, bottom }
end

-- 把矩形夹紧到画布内，并保证高度容得下真实按钮。
--
-- 「容得下」的做法：保持**顶边**不动（服务端是按顶边规划的），
-- 向下扩展高度；如果扩展后超出画布底边，就整体上移。
-- 这样按钮永远完整可见，也不会盖住上面的翻页按钮
-- （服务端已经把翻页按钮安排在这条带子之上了）。
function WardenScreen:_clamp_rect(rect)
    local x0, y0, x1, y1 = rect[1], rect[2], rect[3], rect[4]

    -- 控件真实高度（屏幕像素 → 图像坐标）
    local need_h = y1 - y0
    if self.exit_widget then
        local ok, size = pcall(function() return self.exit_widget:getSize() end)
        if ok and size and size.h and self.scale and self.scale > 0 then
            need_h = math.max(need_h, math.ceil(size.h / self.scale))
        end
    end
    -- 再留 4px 余量：边框贴着画布边缘看起来像被切掉
    need_h = need_h + 4

    -- 横向夹紧
    x0 = math.max(0, math.min(x0, IMG_W - 1))
    x1 = math.max(x0 + 1, math.min(x1, IMG_W))

    -- 纵向：先按需要的高度向下扩，超出底边就整体上移
    y1 = math.min(IMG_H, y0 + need_h)
    if y0 + need_h > IMG_H then
        y0 = math.max(0, IMG_H - need_h)
        y1 = IMG_H
    end

    return { x0, y0, x1, y1 }
end

-- ---------------------------------------------------------------- 定时
--
-- ⚠️ 待机（suspend）会**冻结所有定时器**，醒来后积压的 scheduleIn 会集中触发，
--    而且 WiFi 通常要几秒才重连上 —— 所以醒来瞬间必然"没有网络"。
--    如果那时弹模态窗、或者做长超时的阻塞请求，用户就会看到一个
--    卡住且退不出去的界面。所以：
--      * 待机时**撤掉定时器**，醒来后**重新起算**（而不是补跑积压的）
--      * 醒来后先等一下再刷新，给 WiFi 重连留时间
--      * 刷新失败只画在画布上，绝不弹窗

function WardenScreen:schedule()
    self:unschedule()
    self.task = function() self:refresh() end
    UIManager:scheduleIn(AUTOREFRESH_SEC, self.task)

    -- ⚠️ 看门狗：**保证轮询永远不会彻底停摆。**
    --
    -- 为什么必须有它：``refresh`` 开头是 ``if self.busy then return end``，
    -- 而 ``self.busy = false`` 写在 ``UIManager:nextTick`` 的回调**里面**。
    -- 如果那个回调因为任何原因没跑到（Lua 错误、UIManager 状态异常、
    -- 待机把任务队列冻住……），``busy`` 就**永久卡在 true** ——
    -- 此后每次 refresh 都在第一行直接 return，**连自愈都不会再执行**，
    -- 界面彻底变成"内容不更新、输入已死"的一潭死水。
    --
    -- 看门狗是独立的定时器（不经过 busy 判断），它定期强行把 busy 清掉。
    -- 最坏情况也只是"多跑一轮注定失败的请求"，而收益是**系统永远能自己恢复**。
    self.watchdog = function()
        if self._closed then
            return
        end
        if self.busy then
            logger.warn("kindlewarden: 轮询卡住（busy 超过一个周期），强制复位")
            self.busy = false
            -- 顺便把输入自愈也再跑一次 —— 卡住时它可能一直没机会执行
            self:_heal_stuck_input()
        end
        self:schedule()
    end
    UIManager:scheduleIn(AUTOREFRESH_SEC * 3, self.watchdog)
end

function WardenScreen:unschedule()
    if self.task then
        UIManager:unschedule(self.task)
        self.task = nil
    end
    if self.watchdog then
        UIManager:unschedule(self.watchdog)
        self.watchdog = nil
    end
end

-- 即将待机：停掉轮询，别让定时器在冻结期间堆积。
--
-- ⚠️ **这两个处理器是「优化」，不是「正确性依赖」。**
--
-- 官方事件文档写着：
--     Events are sent to the first widget in UIManager._window_stack.
--     If it is not consumed, then UIManager will try to send it to all
--     active widgets (widget.is_always_active equals true).
--
-- 也就是说**它们不一定会被调用** —— 取决于 Suspend/Resume 是用
-- broadcastEvent 还是 sendEvent 发的，以及我们的窗口在不在栈顶。
-- 我无法在本地验证这一点。
--
-- 所以整个设计**不能依赖它们被调用**。好在我们本来就不依赖：
--
--   * 定时器是一次性的（scheduleIn），每次 schedule 前先 unschedule ——
--     即使待机时没撤掉，积压的最多也只有 1 个，不会形成请求风暴
--   * HTTP 超时只有 4~8 秒，醒来后撞上没连上的 WiFi 也不会冻住主线程
--   * 失败只画在画布上，不弹模态窗、不挡退出按钮
--
-- 换句话说：**即使这两个函数从未被调用，界面依然可操作、能退出、能休眠。**
-- 它们只是让"醒来瞬间"更干净（少一次注定失败的请求）。
--
-- **不返回值**（返回 nil）：对齐官方 AutoTurn 插件。返回 true 在事件语义里
-- 表示"我消费了这个事件" —— 待机是广播，不该由我们截断。
function WardenScreen:onSuspend()
    logger.info("kindlewarden: 设备待机，暂停轮询")
    self:unschedule()
end

-- 从待机恢复：延迟一会儿再刷新。
--
-- 为什么不立刻刷新：醒来时 WiFi 往往还没连上，
-- 立刻请求必然失败（然后画一个"没有网络连接"的提示 —— 无害但难看）。
-- 等 5 秒让网络栈恢复，是省掉一次无意义失败的最小代价。
function WardenScreen:onResume()
    logger.info("kindlewarden: 设备唤醒，5 秒后恢复轮询")
    self:unschedule()
    -- 醒来后强制重新取一次（待机期间服务端可能更新过）
    self.shown_rev = nil
    UIManager:scheduleIn(5, function()
        -- 屏幕可能在等待期间被关掉了，别对已关闭的对象操作。
        -- 这个回调也可能**在我们已经退出之后**才跑（待机期间用户点了 Home），
        -- 所以必须检查。
        if self._closed then
            return
        end
        self:refresh()
    end)
end

-- ---------------------------------------------------------------- 取数据

function WardenScreen:refresh()
    if self.busy then return end
    self.busy = true

    -- ⚠️ 自愈动作放在**每轮轮询的开头**，这一点很关键。
    --
    -- 用户报的故障是"点什么都没反应"，但**看板内容还在更新** ——
    -- 说明轮询这条线程是活的，坏掉的只有输入。
    -- 所以把自愈挂在这里，就能在用户察觉之前把输入救回来：
    -- 最多等一个轮询周期（60 秒），界面就又能操作了。
    --
    -- （如果挂在"用户点击"上就永远等不到 —— 因为点击本身就是坏的。）
    self:_heal_stuck_input()

    UIManager:nextTick(function()
        -- 整个拉取过程包 pcall：任何未预期的错误都不该让插件卡死或崩掉。
        -- 失败时我们只是"这一轮没更新"，下一轮照常重试。
        -- pcall 失败时第二个返回值装的是错误信息，这里换到 err 上。
        local ran, ok, err = pcall(self._fetch, self)
        if not ran then
            err = tostring(ok)
            ok = false
        end

        self.busy = false
        self:schedule()

        local had_error = self.last_error
        if not ok and err then
            logger.warn("kindlewarden:", err)
            self.last_error = err
        else
            self.last_error = nil
        end

        -- ⚠️ **只在真的需要重画时才 setDirty**。
        --
        -- 原来这里是无条件 ``UIManager:setDirty(self, "full")`` ——
        -- 每 60 秒全屏刷一次，哪怕画面一个字都没变。两个坏处：
        --
        --   1. e-ink 全屏刷新要几百毫秒，纯属浪费电（副屏是常驻的）
        --   2. 持续刷屏会让 KOReader 认为"有活动"，**妨碍自动休眠**
        --      —— 而"长时间放置后能正常待机"正是用户要求的底线之一
        --
        -- 只在下面三种情况重画：
        --   * 画面内容变了（rev 变化 → _swap_image 已置标记）
        --   * 错误状态变化（开始报错 / 错误消失）
        --   * 临时提示刚好在这一轮过期（需要擦掉它）
        local notice_active = self.notice and os.time() < (self.notice_until or 0)

        -- 提示过期的那一刻也要重画一次，否则它会**一直留在屏幕上**
        -- （没有别的事件会触发重画，因为一切正常）。
        local notice_just_expired = (not notice_active)
            and (self.notice ~= nil)
            and ((self.notice_until or 0) > 0)

        local need_redraw = self._image_changed
            or (self.last_error ~= had_error)
            or notice_active
            or notice_just_expired

        self._image_changed = false
        if notice_just_expired then
            self.notice = nil
            self.notice_until = 0
        end

        if need_redraw then
            UIManager:setDirty(self, "full")
        end
    end)
end

-- ⚠️⚠️ 这里**绝对不能用 InfoMessage 之类的模态弹窗**，这是踩过的坑：
--
--   * 它会**盖住整个屏幕**，包括我们画的「退出」按钮 ——
--     服务器断开时反复弹窗，用户就**退不出去了**
--   * 弹窗存在期间 KOReader **不会自动休眠** ——
--     用户报的"无法解除休眠状态"就是它
--   * 每轮刷新都弹一次的话，界面会被弹窗永久占据
--
-- 所以错误一律**画在自己的画布上**（见 _paint_status_text）：
-- 它是非模态的、不拦输入、不影响休眠，而且退出按钮永远在它上面。
function WardenScreen:_fetch()
    if not NetworkMgr:isConnected() then
        return false, _("没有网络连接")
    end
    if self.screen_kind == "board" then
        return self:_fetch_board()
    end
    return self:_fetch_detail()
end

function WardenScreen:_fetch_board()
    local body, err = get_url(self.server .. EP.rev .. "?c=kindle")
    if not body then
        return false, _("副屏服务不可达（") .. tostring(err) .. "）"
    end

    local rev_obj = json.decode(body)
    local rev = rev_obj and rev_obj.global_rev
    if not rev then
        return false, _("服务端响应看不懂")
    end

    -- rev 没变就不重新下载：和 shell 版是同一个省电思路
    if rev == self.shown_rev and self.image_ready then
        return true
    end

    local board_json = get_url(self.server .. EP.board_json .. "?c=kindle")
    local ok, derr = download(
        self.server .. EP.board_png .. "?c=kindle&rev=" .. rev,
        self:img_path("board.png"))
    if not ok then
        return false, _("画面下载失败（") .. tostring(derr) .. "）"
    end

    self.taps = {}
    if board_json then
        local info = json.decode(board_json)
        for _, item in ipairs((info and info.briefs) or {}) do
            item.kind = "brief"
            table.insert(self.taps, item)
        end
        for _, item in ipairs((info and info.todos) or {}) do
            item.kind = "todo"
            table.insert(self.taps, item)
        end
        -- 服务端给的退出按钮保留区 —— 照它画，别自己算（见 _compute_exit_rect）
        if info and info.exit_zone then
            self.exit_zone = info.exit_zone
            self.exit_rect = self:_compute_exit_rect()
        end
    end

    self.shown_rev = rev
    self:_swap_image("board.png")
    return true
end

function WardenScreen:_fetch_detail()
    local q = ("?id=%s&page=%d&c=kindle"):format(self.brief_id, self.page)

    local body = get_url(self.server .. EP.detail_json .. q)
    local info = body and json.decode(body)
    if not info then
        return false, _("详情信息加载失败")
    end

    local ok, derr = download(self.server .. EP.detail_png .. q,
                              self:img_path("detail.png"))
    if not ok then
        return false, _("详情图下载失败（") .. tostring(derr) .. "）"
    end

    self.taps = info.taps or {}
    self.total_pages = info.pages or 1
    -- 详情页的退出按钮位置也由服务端给（每页都一样，但拿到就更新）
    if info.exit_zone then
        self.exit_zone = info.exit_zone
        self.exit_rect = self:_compute_exit_rect()
    end
    self:_swap_image("detail.png")
    return true
end

function WardenScreen:_swap_image(name)
    -- 换文件必须重建 ImageWidget：它内部缓存了 BlitBuffer，
    -- 只改 file 字段不会重新读盘。
    if self.image_widget then
        self.image_widget:free()
        self.image_widget = nil
    end
    self.image_widget = ImageWidget:new{
        file = self:img_path(name),
        scale_factor = self.scale,
        file_do_cache = false,
    }
    self.image_ready = true
    -- 标记"内容真的变了"，refresh 据此决定要不要重画。
    -- 不置这个标记的话，refresh 会认为无事发生 —— 画面永远不更新。
    self._image_changed = true
end

-- ---------------------------------------------------------------- 绘制

-- 绘制入口。**整体包 pcall，并且兜底重画退出按钮。**
--
-- 这是"永远能退出"的最后一道保险：如果主绘制路径里任何一步出错
-- （图片解码失败、字体问题、尺寸算出 NaN……），
-- 屏幕可能一片空白或者只画了一半 —— 而**退出按钮也就跟着没了**。
--
-- 所以出错时要在白底上至少把「退出」按钮补画出来。
-- 用户看到的可能是"没有内容的副屏 + 一个退出按钮"，但那已经够用了 ——
-- 比"全白且不知道该点哪里"好得多。
function WardenScreen:paintTo(bb, x, y)
    local ok, err = pcall(self._paint_all, self, bb, x, y)
    if ok then
        return
    end

    err = tostring(err)
    logger.warn("kindlewarden: 绘制失败，兜底：", err)

    pcall(function() bb:fill(Blitbuffer.COLOR_WHITE) end)
    -- 把错误显示出来，不要只留一片白（见 _paint_diagnostic）
    self.paint_error = err
    self:_paint_diagnostic(bb, x, y)
    pcall(self._paint_exit_button, self, bb, x, y)
end

function WardenScreen:_paint_all(bb, x, y)
    local size = self:getSize()
    self.dimen = Geom:new{ x = x, y = y, w = size.w, h = size.h }
    self.paint_x, self.paint_y = x, y

    -- 铺白：夜间模式下 KOReader 会反色，这里强制白底保证和 PNG 一致
    bb:fill(Blitbuffer.COLOR_WHITE)

    -- ⚠️ **画画面这一步单独包 pcall，失败要说清原因。**
    --
    -- 踩过一次：这里静默失败 → 用户看到一片白 + 一个退出按钮，
    -- 完全不知道发生了什么，我只能远程反复猜。
    --
    -- 两种情况都会白屏，而且都不抛异常到外层：
    --   1. image_widget 是 nil（但 image_ready 是 true）—— 纯粹静默
    --   2. image_widget:paintTo 抛异常
    -- 现在两种都记进 self.paint_error，由 _paint_diagnostic 写到屏幕上。
    self.paint_error = nil
    if self.image_ready then
        if not self.image_widget then
            self.paint_error = "画面组件不存在（image_widget=nil）"
            logger.warn("kindlewarden: " .. self.paint_error)
        else
            local drew, derr = pcall(self.image_widget.paintTo,
                self.image_widget, bb, x + self.origin_x, y + self.origin_y)
            if not drew then
                self.paint_error = tostring(derr)
                logger.warn("kindlewarden: 画面绘制失败：", self.paint_error)
            end
        end
    end

    self:_paint_status_text(bb, x, y)

    -- 「退出」按钮**永远画**，和有没有画面、有没有错误都无关。
    --
    -- 这是从"退不出去"那次事故里学到的：服务端连不上时画面是全白的，
    -- 如果退出入口只在服务端给的图里，用户面对的是一块没有任何提示的白屏。
    -- 这个按钮是插件自己画的 —— 不依赖服务端、不依赖网络、不依赖手势识别。
    self:_paint_exit_button(bb, x, y)
end

-- 把"为什么没有画面"写到屏幕上。
--
-- 为什么要在屏幕上写，而不是只记日志：
--   日志在设备里，要插 USB 才能读；而用户就坐在屏幕前面。
--   直接显示出来，用户一眼就能告诉我是什么问题 ——
--   这比远程猜快得多（这次就是猜了好几轮）。
function WardenScreen:_paint_diagnostic(bb, x, y)
    local err = self.paint_error
    if not err then
        return
    end

    local ok = pcall(function()
        local cache = self.cache_dir or "?"
        local file = self:img_path(self.screen_kind == "board"
            and "board.png" or "detail.png")
        local exists = "?"
        local ok_f = io.open(file, "r")
        if ok_f then
            exists = "是"
            ok_f:close()
        else
            exists = "否"
        end

        local lines = {
            "副屏画面绘制失败",
            "",
            err:sub(1, 56),
            err:sub(57, 112),
            "",
            "图片存在: " .. exists,
            "图片路径: " .. file,
        }

        local face = Font:getFace("cfont", 18)
        local ty = y + math.floor(Screen:getHeight() * 0.26)
        for i, line in ipairs(lines) do
            if line ~= "" then
                local w = TextWidget:new{
                    text = line,
                    face = face,
                    fgcolor = Blitbuffer.COLOR_BLACK,
                    max_width = Screen:getWidth() - 100,
                }
                w:paintTo(bb, x + 50, ty + (i - 1) * 30)
                w:free()
            end
        end
        logger.info("kindlewarden: 缓存目录 " .. cache)
    end)
    if not ok then
        logger.warn("kindlewarden: 连诊断信息都画不出来")
    end
end

-- 在画布上写一行状态（"正在加载…" 或错误信息）。
--
-- 为什么不用 InfoMessage：
--   模态弹窗会盖住退出按钮、并阻止 KOReader 自动休眠 ——
--   服务器断开时反复弹窗就等于"退不出去 + 不休眠"，这两条都是用户明确要求避免的。
--
-- 这里画在**底部**（退出按钮上方一点），既有提示又不挡任何可点区域。
function WardenScreen:_paint_status_text(bb, x, y)
    -- 优先级：临时提示（点待办）> 错误 > 加载中
    local text = nil
    if self.notice and os.time() < (self.notice_until or 0) then
        text = self.notice
    elseif self.last_error then
        text = self.last_error
    elseif not self.image_ready then
        text = _("正在连接副屏服务…")
    end
    if not text then
        return                         -- 一切正常，不占地方
    end

    local ok, err = pcall(function()
        local widget = TextWidget:new{
            text = text,
            face = Font:getFace("cfont", 15),
            fgcolor = Blitbuffer.COLOR_DARK_GRAY,
            max_width = math.floor(self.disp_w * 0.8),
        }
        -- 放在左下角、退出按钮左侧，避开所有可点区域
        local bx = x + self.origin_x + 40 * self.scale
        local by = y + self.origin_y + (IMG_H - 30) * self.scale
        widget:paintTo(bb, math.floor(bx), math.floor(by))
        widget:free()
    end)
    if not ok then
        logger.warn("kindlewarden: 状态文字绘制失败：", err)
    end
end

function WardenScreen:_paint_exit_button(bb, x, y)
    if not self.exit_widget then
        return
    end
    -- 图像坐标 → 屏幕坐标（和点击换算是同一套比例，反着来）
    local r = self.exit_rect or EXIT_ZONE_FALLBACK
    local bx = x + self.origin_x + r[1] * self.scale
    local by = y + self.origin_y + r[2] * self.scale

    -- ⚠️ **绘制前再夹紧一次**，用控件的真实尺寸。
    --
    -- 为什么矩形已经夹过了还要再来一次：
    --   ``_compute_exit_rect`` 是在 init / 取数据时算的，
    --   而控件的实际高度可能在那之后才确定（ImageWidget/TextWidget
    --   都是**惰性**计算尺寸的，第一次 getSize 才真正渲染）。
    --   如果那时量到的尺寸偏小，矩形就会偏矮，画出来仍然溢出。
    --
    -- 这一层是**最后一道**，直接以屏幕边界为准，不依赖任何估值：
    -- 无论控件多高，底边都不许超过屏幕底边。
    local draw_y = by
    local draw_x = bx
    local ok_size, size = pcall(function() return self.exit_widget:getSize() end)
    if ok_size and size and size.h then
        local screen_h = Screen:getHeight()
        local screen_w = Screen:getWidth()
        if draw_y + size.h > screen_h then
            draw_y = screen_h - size.h
        end
        if draw_y < 0 then
            draw_y = 0
        end
        if draw_x + size.w > screen_w then
            draw_x = screen_w - size.w
        end
        if draw_x < 0 then
            draw_x = 0
        end
    end

    -- 同样 pcall 保护：画不出按钮不该连累整屏画面。
    local ok, err = pcall(function()
        self.exit_widget:paintTo(bb, math.floor(draw_x), math.floor(draw_y))
    end)
    if not ok then
        logger.warn("kindlewarden: 退出按钮绘制失败：", err)
    end
end

-- ---------------------------------------------------------------- 点击

function WardenScreen:on_tap(ges)
    if not ges or not ges.pos then return true end

    -- 屏幕坐标 → 图像坐标。因为缩放比自算、绘制原点已知，只有一次乘除。
    local ix = (ges.pos.x - self.paint_x - self.origin_x) / self.scale
    local iy = (ges.pos.y - self.paint_y - self.origin_y) / self.scale

    -- 退出按钮优先判断，而且**不受 image_ready 影响** ——
    -- 画面没下载下来（白屏）时它也必须能用。
    local er = self.exit_rect or EXIT_ZONE_FALLBACK
    if ix >= er[1] and ix <= er[3]
        and iy >= er[2] and iy <= er[4] then
        return self:do_exit()
    end

    if not self.image_ready then
        self:refresh()
        return true
    end

    logger.dbg("kindlewarden tap", ges.pos.x, ges.pos.y, "->", ix, iy)

    for _, item in ipairs(self.taps) do
        local r = item.rect
        if r and ix >= r[1] and ix <= r[3] and iy >= r[2] and iy <= r[4] then
            return self:handle_hit(item)
        end
    end
    return true
end

-- 任意方向滑动 = 退出。
--
-- 不判断方向：这块屏是纯展示的，滑动没有别的含义。
-- 误退出只是"从菜单再点开一次"，而退不出去要重启设备 —— 两者代价差得远，
-- 所以宁可宽松。
function WardenScreen:on_swipe(_ges)
    return self:do_exit()
end

function WardenScreen:do_exit()
    logger.info("kindlewarden: 退出副屏")
    -- 必须走 Warden:toggle()，不能只 UIManager:close(self)：
    -- 插件那边持有 self.screen 引用，不通知它就会留下一个已经关掉的死对象，
    -- 下次点菜单会去 close 那个死对象，**新窗口永远打不开**。
    if self.on_exit then
        self.on_exit()
    else
        UIManager:close(self)
    end
    return true
end

function WardenScreen:handle_hit(item)
    if item.kind == "brief" then
        self.screen_kind = "detail"
        self.brief_id = item.id
        self.page = 1
        self.image_ready = false
        self:_switch_screen()

    elseif item.kind == "todo" then
        -- 待办在设备上只读。勾选涉及写操作和冲突处理，
        -- 一台只读副屏没必要承担这些 —— 那留给管理页。
        --
        -- ⚠️ 这里**也不能用 InfoMessage**：它是模态弹窗，会盖住退出按钮。
        --    改成画到自己的画布上（和错误提示走同一条路），
        --    下次刷新时会自动消失，而且永远不挡退出。
        self.notice = (item.title or "") .. (item.done and _("（已完成）") or "")
        self.notice_until = os.time() + 4
        UIManager:setDirty(self, "ui")
        -- 4 秒后主动擦掉提示。
        --
        -- 不这么做的话，提示会一直留到**下一次轮询**才消失 ——
        -- 轮询间隔是 60 秒，一条 4 秒的提示会挂在屏幕上将近一分钟。
        UIManager:scheduleIn(4, function()
            if self._closed then
                return
            end
            self.notice = nil
            self.notice_until = 0
            UIManager:setDirty(self, "full")
        end)

    elseif item.kind == "back" then
        self:go_board()

    elseif item.kind == "page" then
        self.page = item.page or 1
        self.image_ready = false
        self:_switch_screen()
    end
    return true
end

-- 切换屏幕内容（看板 ↔ 详情、翻页）。
--
-- 必须**立刻重画一次**（而不是等 refresh 回来）：
-- 用户刚点了卡片，屏幕却几十毫秒到几秒不动，会以为没点上而重复点。
-- 先画一帧"正在加载"（状态文字已经在 _paint_status_text 里处理），
-- 再让 refresh 去取真内容。
function WardenScreen:_switch_screen()
    self._image_changed = true
    UIManager:setDirty(self, "full")
    self:refresh()
end

function WardenScreen:go_board()
    self.screen_kind = "board"
    self.brief_id = nil
    self.page = 1
    self.image_ready = false
    self.shown_rev = nil      -- 强制重新取一次，免得看到旧板
    self:_switch_screen()
end

function WardenScreen:onCloseWidget()
    -- 标记已关闭：onResume 里延迟的刷新、以及任何迟到的回调
    -- 都要靠这个标志避免操作一个已经销毁的对象（那会崩）。
    self._closed = true

    self:unschedule()
    if self.image_widget then
        self.image_widget:free()
        self.image_widget = nil
    end
    if self.exit_widget then
        self.exit_widget:free()
        self.exit_widget = nil
    end
    -- 通知插件"我被关掉了"，让它清掉对已关闭对象的引用。
    -- 用完就清掉回调：这个函数只在关闭时该跑一次。
    if self.on_closed then
        local cb = self.on_closed
        self.on_closed = nil
        self.on_exit = nil
        cb()
    end
end

-- ---------------------------------------------------------------- 插件

local Warden = WidgetContainer:extend{
    name = "kindlewarden",
    is_doc_only = false,       -- 文件管理器和阅读器里都能调用
}

function Warden:init()
    self.ui.menu:registerToMainMenu(self)
    self.screen = nil
end

-- 关闭副屏，**幂等**。
--
-- ⚠️ 这里有个必须绕开的陷阱：屏幕可以**自己**关闭（用户滑动退出，
--    或者 KOReader 因为别的原因关掉它）。如果只在 toggle 里清 self.screen，
--    那么"屏幕自己关掉"这条路径上 self.screen 仍然指着那个已经关闭的对象，
--    下次点菜单会去 close 一个死对象 —— **新窗口永远打不开**。
--
-- 所以：清引用放在**唯一**的关闭函数里，而且先清引用再 close，
-- 这样 close 触发的 onCloseWidget 再回调进来时已经是无操作（幂等）。
function Warden:close_screen()
    local s = self.screen
    self.screen = nil
    if s then
        UIManager:close(s)
    end
end

function Warden:toggle()
    if self.screen then
        self:close_screen()
    else
        local s = WardenScreen:new{}
        -- 两个回调指向同一个幂等函数：
        --   on_exit   —— 屏幕里的「退出」按钮 / 滑动手势
        --   on_closed —— 屏幕被 KOReader 从别处关掉（onCloseWidget）
        s.on_exit = function() self:close_screen() end
        s.on_closed = function() self:close_screen() end
        self.screen = s
        UIManager:show(s)
    end
end

function Warden:addToMainMenu(menu_items)
    menu_items.kindlewarden = {
        text = _("kindlewarden 副屏"),
        sorting_hint = "tools",
        callback = function() self:toggle() end,
    }
    menu_items.kindlewarden_server = {
        text = _("kindlewarden：服务端地址"),
        sorting_hint = "tools",
        help_text = _("例如 http://192.168.1.50:8577"),
        callback = function()
            local InputDialog = require("ui/widget/inputdialog")
            local current = G_reader_settings:readSetting("kindlewarden_server")
                or DEFAULT_SERVER
            local dialog
            dialog = InputDialog:new{
                title = _("副屏服务端地址"),
                input = current,
                buttons = {{
                    {
                        text = _("取消"),
                        id = "close",
                        callback = function() UIManager:close(dialog) end,
                    },
                    {
                        text = _("保存"),
                        is_enter_default = true,
                        callback = function()
                            local url = dialog:getInputText()
                            -- 容错：只填 IP 或 IP:端口 也接受
                            if url ~= "" and not url:match("^https?://") then
                                url = "http://" .. url
                            end
                            url = url:gsub("/+$", "")
                            G_reader_settings:saveSetting("kindlewarden_server", url)
                            UIManager:close(dialog)
                        end,
                    },
                }},
            }
            UIManager:show(dialog)
            dialog:onShowKeyboard()
        end,
    }
end

return Warden
