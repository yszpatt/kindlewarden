# kindlewarden — 设计评审与架构建议

> 目标：把 AI agent 的任务运行状态同步到 Kindle Paperwhite 7 代（PW3）上显示。
> 本文结论基于对目标机型固件、Kindle 生态现状、以及本机 DSH 运行时的实际探测。

---

## 0. 结论速览

| 你的设想 | 判定 |
|---|---|
| agent → MCP → host 上报状态 | ✅ 可行 |
| host 服务端渲染 → Kindle 显示 | ✅ 可行（已有同构先例） |
| 「agent 主动调 MCP 工具」作为**主通道** | ⚠️ 可行但不推荐，应降级为**辅助通道** |
| 「同步」= host 主动 push 到 Kindle | ⚠️ 应改为 **Kindle 侧 pull + 版本号比对** |
| 在 MCP 工具里直接做设备 I/O | ❌ 会阻塞 agent 主循环，必须异步化 |
| 每个 token / 每个 step 都刷新屏幕 | ❌ e-ink 残影 + 电池灾难 |

**核心建议：把「agent 主动上报」换成「旁路进程自动观测」。**
DSH 已经把每一步运行状态以结构化事件写入本地日志（本文已验证），
所以状态源可以是**零 token 开销、零 agent 配合、永不遗漏**的。

---

## 1. 事实核查（决定了后面的所有选型）

### 1.1 目标设备
| 项 | 值 |
|---|---|
| 机型 | Kindle Paperwhite 3 = 第 7 代（2015） |
| 型号号 | DP75SDI |
| 屏幕 | 6", 300 PPI, **1072 × 1448**, 16 级灰度, 前光（无暖光） |
| 固件 | 5.16.2.1.1 |

> 屏幕参数自洽性校验：`√(1072² + 1448²) / 300 = 6.005"` ✓

### 1.2 5.16.2.1.1 是个「好固件」
7~9 代老机型**停在 5.16.2.1.1**，这是它们的末版固件。
含义：越狱之后**不存在被 OTA 升级冲掉越狱的风险** —— 这是老机型相对新机型的一个实质优势。

### 1.3 越狱路径
- **LanguageBreak** 官方支持范围 **5.14.3 – 5.16.2.1.1**，正好覆盖你的机型。
  - 仓库自述：*"Jailbreak for any kindle running FW 5.16.2.1.1 or LOWER"*
  - 越狱后需要装 **Jailbreak Hotfix**（防越狱失效）
- 也有 PW3 + 5.16.2.1.1 使用 **WinterBreak** 的社区案例，但**以 KindleModding 兼容表为准**，不要凭传闻。
- 越狱后工具链：`MRPI`（包安装器）→ `KUAL`（应用启动器）→ **`FBInk`**（直绘 framebuffer）→ `kterm`/`usbnet`（调试）

> ⚠️ 结论：**越狱可行且低风险**（末版固件无 OTA 威胁）。但建议先用不越狱路径验证价值，再越狱。

### 1.4 同类先例（重要参考）
[`codecube/kindle-mcp-dashboard`](https://github.com/codecube/kindle-mcp-dashboard) —— **已经实现了你这个想法的 80%**：
- MCP (stdio) + HTTP 渲染服务 + 服务端渲染 PNG → Kindle 拉取
- 目标设备是 **PW1 / EY21（758×1024）**，不是 PW3
- 架构原则值得直接借用：*"The Kindle is a display client, not the MCP host."*

它的增量缺口，正是本项目的价值所在：
1. 状态靠 agent 主动推（我们改为**事件驱动自动观测**）
2. 面向 e-ink 的信息设计较弱（我们做**告警优先**布局）
3. 用 `eips`（我们用 **FBInk**，在 5.16.x 上更可靠）

---

## 2. 关键发现：DSH 暴露了结构化运行事件

对 `%USERPROFILE%\.dsh\sessions\` 的实际探测结果：

```
.dsh/sessions/<cwd-编码>/<session-id>/session.v4.jsonl.zstd
```

**格式特征（已验证）：**
- 追加式 **多帧 zstd** 拼接流：每个事件一个独立 zstd frame
- 每帧可独立解码 → **天然支持增量 tail**
- 帧内容为 JSON lines，带 `type` / `seq` / `time` / `data`

**实测解码到的事件类型词表：**

| 事件 | 数量级 | 对状态看板的价值 |
|---|---|---|
| `turn/start` / `turn/end` | 24 / 23 | 任务生命周期骨架 |
| `step/start` / `step/end` | 1131 / 1130 | 当前处于第几步 |
| `tool/call` / `tool/result` | 1195 / 1194 | 「正在做什么」的一手信息 |
| `assistant/message` | 1122 | 摘要/结论 |
| `todo/write` | 9 | **天然的任务进度清单** ⭐ |
| `subagent/descriptor` | 4 | 子 agent 树 |
| `team/task` / `team/member` / `team/message/*` | 13 / 8 / 10 | **多 agent 并行泳道** ⭐ |
| `goal/change` | 5 | 目标变更 |
| `llm/retry` / `llm/retry-started` | 59 | 「卡住了/在重试」 |
| `approval/policy` / `sandbox/mode` | 12 / 12 | 权限与沙箱上下文 |
| `deliverables/presented` | 4 | 产出物就绪 ⭐ |
| `command/run` / `command/done` | 2 / 2 | 长命令执行 |
| `workspace/changes` | 8 | 文件改动量 |

> ⭐ = 直接映射到 e-ink 看板上最有价值的信息。

### 这意味着什么

状态**不需要 agent 主动汇报** —— 它已经被免费、完整、结构化地记录在本地了。
一个旁路 sidecar 进程 tail 这个文件，就能重建出完整的任务状态机。

**这比 MCP 推送好在哪里：**

| 维度 | agent 主动调 MCP | 旁路观测事件流 |
|---|---|---|
| token 开销 | 每次上报都进上下文 | **0** |
| 可靠性 | 模型可能忘记调用（长工具循环中尤其） | **不可能遗漏** |
| 上下文污染 | agent 要分心思考「怎么显示」 | **完全解耦** |
| 阻塞风险 | 工具实现不当会拖慢推理循环 | **无** |
| 覆盖范围 | 只有 agent 想汇报的事 | **全量**（含重试、子 agent、权限） |

---

## 3. 推荐架构

### 3.1 总览

```
┌──────────────────────────────────────────────────────────────┐
│  Host (Windows, 常驻)                                         │
│                                                              │
│  ┌─────────────┐      ┌──────────────────────────────┐       │
│  │ DSH / agent │      │  kindlewarden-collector      │       │
│  │             │      │  (tail session.v4.jsonl.zstd)│       │
│  │  session ───┼─────▶│   ①事件 → ②状态机 → ③去抖    │       │
│  │  log (zstd) │      └──────────────┬───────────────┘       │
│  └──────┬──────┘                     │                       │
│         │ MCP (stdio)                │ 写                     │
│         ▼                            ▼                       │
│  ┌─────────────────┐      ┌──────────────────────┐           │
│  │ kindlewarden-mcp│─────▶│  State Store         │           │
│  │  显式/覆盖通道   │      │  (SQLite, 单一事实源) │           │
│  └─────────────────┘      └──────────┬───────────┘           │
│                                      │ 读                     │
│                           ┌──────────▼───────────┐           │
│                           │   Renderer (Pillow)  │           │
│                           │  1072×1448 1-bit PNG │           │
│                           │  rev = hash(state)   │           │
│                           └──────────┬───────────┘           │
│                                      │                       │
│                           ┌──────────▼───────────┐           │
│                           │  HTTP :80            │           │
│                           │  /kindle  /screen.png│           │
│                           │  /rev (轻量轮询)      │           │
│                           └──────────┬───────────┘           │
└──────────────────────────────────────┼───────────────────────┘
                                       │ LAN (明文 HTTP)
                        ┌──────────────▼───────────────┐
                        │  Kindle PW3                  │
                        │  A) 浏览器 + meta refresh     │
                        │  B) 越狱 poller + FBInk       │
                        └──────────────────────────────┘
```

### 3.2 三条状态来源，按优先级降级

1. **自动事件源**（主）— tail DSH session log。零配合、全量、免费。
2. **MCP 显式工具**（辅）— 用于 agent 想汇报**业务级语义**（"已处理 30/100 篇"），
   或人/agent 想手动覆盖看板内容。**Fire-and-forget，立即返回。**
3. **MCP resource 回读**（自省）— `kindle://screen` 返回当前 rev + 文本摘要，
   让 agent 能确认"屏幕上现在是什么"，形成闭环。

> 设计原则：**MCP 不该是状态的主干道，而是表达能力的扩展口。**

### 3.3 为什么 Kindle 必须 pull，而不是被 push

- 无越狱时 Kindle 没有任何对外服务，host 无法主动触达它
- 越狱后虽然可以跑监听进程，但功耗与稳定性更差
- pull + **版本号** 才是一切省电优化的基础：
  ```
  GET /rev           → "a3f9c1"        (几十字节，极轻)
  若与本地 rev 相同   → 不下载、不刷新   ← 省电 + 省屏
  若不同            → GET /screen.png → FBInk 绘制
  ```

---

## 4. MCP 接口设计

### 4.1 反模式（不要这样设计）

```
❌ update_region(screen_id, region_id, fields)   # 要求 agent 理解布局
❌ set_pixel / set_layout(...)                    # agent 不该管像素
❌ 工具内部 wget 到 Kindle 并等待刷新完成          # 阻塞主循环
```

理由：**agent 的注意力应该花在任务上，而不是排版上。**
让模型去维护"哪个区域放什么"既浪费 token 又极易出错。

### 4.2 推荐接口：语义化 + 生命周期

```
warden_task_begin(key, title, kind, eta?)        → { rev }
warden_task_progress(key, done, total, label?)   → { rev }
warden_task_note(key, text, level?)              → { rev }
warden_task_end(key, status, summary?)           → { rev }   # ok|failed|cancelled
warden_attention(title, detail, urgency)         → { rev }   # 抢占用顶部横幅
warden_clear_attention()                         → { rev }
warden_lane(key, owner, state)                   # 多 agent 泳道
warden_screen(mode)                              # overview|focus|quiet
warden_render_preview(path)                      # 人类调试用，返回 PNG 路径
```

**全部 fire-and-forget 语义：**
- 只写 State Store，**立即返回** `rev`，绝不触碰设备
- 设备 I/O 完全由 renderer / Kindle 侧负责
- 这样即使 Kindle 关机、WiFi 断了，agent 也**完全不受影响**

### 4.3 Resources（回读）

```
kindle://screen/current   → { rev, mode, rendered_at, text_summary, tasks[] }
kindle://device/status    → { last_seen, battery?, rssi? }   # 越狱后可拿
```

---

## 5. 设备侧两条路径

| 维度 | A) 不越狱：自带浏览器 | B) 越狱：FBInk poller |
|---|---|---|
| 部署成本 | 极低（打开 URL） | 中（越狱 + KUAL + 脚本） |
| 风险 | 零 | 低（末版固件无 OTA 风险） |
| 刷新控制 | 差（meta refresh 整页） | **好（局部刷新 / 波形可选）** |
| 残影控制 | 差 | **好** |
| 功耗 | 差（浏览器 + WiFi 常开，几天掉完） | **较好**（可轮询后休眠） |
| 可自动化 | 差（需人工保持前台） | **好（开机自启）** |
| 可用信息密度 | 受老 WebKit 限制 | **全可控** |

**建议：A 用于验证价值（1 天），B 用于长期值守。**

### 5.1 路径 A 的实现约束（老 WebKit，务必遵守）

- **明文 HTTP，端口 80**。老浏览器对非标端口、TLS、HTTP/2 都很脆。
  （先例项目也记录了同样现象：PW1 上 `:8787` 不可靠，必须走 80 端口反代。）
- **零 JavaScript** 最稳：老 WebKit 不支持 `let` / `const`，脚本会整段不执行。
- 自动刷新用 HTML 层：
  ```html
  <meta http-equiv="refresh" content="30">
  ```
- 响应头必须 `Cache-Control: no-store`
- 页面只放**一张全屏 `<img>`**，别无他物 —— 最省事也最不容易翻车

### 5.2 路径 B 的实现要点

部署链：`LanguageBreak → Jailbreak Hotfix → MRPI → KUAL → FBInk`

Kindle 侧轮询脚本（伪代码，参数需在设备上实测确认）：

```sh
#!/bin/sh
SERVER="http://192.168.1.50"
REV_FILE=/tmp/warden.rev
PNG=/tmp/warden.png

# 阻止屏保（社区常用，需验证）
lipc-set-prop com.lab126.powerd preventScreenSaver 1

while true; do
  NEW=$(wget -q -O - "$SERVER/rev")
  OLD=$(cat "$REV_FILE" 2>/dev/null)
  if [ "$NEW" != "$OLD" ]; then
    wget -q -O "$PNG" "$SERVER/screen.png"
    fbink -g file="$PNG" -q          # -q: 不触发额外刷新
    echo "$NEW" > "$REV_FILE"
  fi
  sleep 5
done
```

要点：
- **rev 比对是省电核心** —— 无变化时零下载、零刷新
- 定期（如每 30 次）做一次 full refresh 清除累积残影
- 用 KUAL 扩展挂载 + 开机自启
- 调试用 `kterm` / `usbnet`（越狱后）

---

## 6. e-ink 信息设计（容易被低估，但决定成败）

1072×1448 @ 300ppi，观看距离 1–3 米。

**布局建议（告警优先，而非进度优先）：**

```
┌────────────────────────────────────┐
│  ⚠ 需要你介入                       │  ← 只在需要人时出现，反白大字号
│  「允许执行 rm -rf build」           │
├────────────────────────────────────┤
│  ● 重构 auth 模块          运行中    │  ← 每行: 状态符号 + 标题 + 状态
│    └ 正在编辑 src/auth.ts (第 12 步) │
│  ● 抓取论文元数据         30/100    │  ← 进度: 数字 + 细条
│  ● 单元测试               ✓ 完成    │
│  ● 部署到 staging         ✗ 失败    │
├────────────────────────────────────┤
│  空闲 · 3 任务 · 更新于 12 秒前      │  ← 必须有时间戳+心跳
└────────────────────────────────────┘
```

**硬性规则：**
- 正文 ≥ 34px，标题 ≥ 64px（1-bit 渲染下要更大）
- **不要用 emoji** —— 字体缺失 + 灰度化后不可辨；用几何符号 `● ✓ ✗ ⚠` 或内置位图图标
- 避免大面积反白块（e-ink 上刷新慢、残影重），小面积点缀即可
- **必须显示"多久前更新"** —— e-ink 最大风险是「看起来在跑，其实是 3 小时前的旧画面」
- 设计**空态**（显示"空闲"而非空白），否则无法区分"没事干"和"挂了"
- 颜色：只用黑/白/抖动灰。状态区分靠**符号 + 位置 + 反白**，不靠颜色

---

## 7. 刷新与节流策略

| 事件 | 策略 |
|---|---|
| 状态跃迁（begin/end/失败/需介入） | **立即刷新**，可用 full refresh 引起注意 |
| 进度类更新 | 去抖合并，最小间隔 **10–30s**，且变化需超阈值 |
| 心跳（无事件） | 60s 一次，仅更新时间戳 |
| 无变化 | **不刷**（rev 相同直接跳过） |

- 单次刷新有 200ms–2s 的可见闪动，**绝不能按 token 刷**
- 空闲降频，活跃升频 —— 反直觉但正确：**噪声越大越不该刷**

---

## 8. 风险与坑清单

1. **敏感信息泄漏** ⚠️ 最高优先级
   `tool/call` 事件里可能含 token、密钥、私有路径、客户数据。
   渲染前必须走**白名单/脱敏**，只上屏必要字段。
   看板是常亮放在桌上的，等于半公开显示。

2. **DSH 会话格式是私有的 v4**
   升级 DSH 可能破坏解析。适配层要**容错降级**：解析失败 → 显示"状态源不可用"，
   而不是整体崩掉。同时保留 MCP 通道作为兜底。

3. **多会话 / 多任务选择**
   本机存在多个工作区会话。需要明确"哪个会话上屏"：
   建议显式 pin，或自动选最近活跃（但要在看板上标明是哪个）。

4. **网络可达性**
   - Windows 防火墙需加入站规则
   - Kindle 用静态 IP 或 DHCP 保留；老浏览器通常不支持 mDNS
   - 建议 host 走有线/稳定 Wi-Fi，避免 IP 漂移

5. **电量预期管理**
   常亮 WiFi + 浏览器 ≈ 从"几周"降到"几天"。
   越狱 poller 更省，但仍不建议 24h 高频繁刷新。
   可考虑：无任务时让 Kindle 进飞行模式（会失去实时性，需权衡）。

6. **e-ink 不是实时显示器**
   定位为**环境看板 / 告警牌**，不是 dashboard。
   最有价值的瞬间是「**任务卡住 / 需要你介入**」——那一刻 e-ink 常亮不耗电、余光可见、
   比手机推送更不打扰，这才是这个项目的杀手场景。

7. **明文 HTTP 无认证**
   仅绑定 LAN；不要暴露到公网。

---

## 9. 分阶段实施计划

| 阶段 | 内容 | 风险 | 产出 |
|---|---|---|---|
| **P0** | 渲染服务 + 状态存储 + 手工 curl 测试 | 无 | 能在浏览器打开一张 1072×1448 看板 |
| **P1** | Kindle 浏览器上屏（meta refresh，端口 80） | 无 | **端到端跑通，验证价值** |
| **P2** | 事件 collector（tail session log）+ 状态机 | 低 | 自动同步，agent 零配合 |
| **P3** | MCP server（语义化工具 + resource 回读） | 低 | agent 可显式补充 |
| **P4** | 越狱 + FBInk poller + 开机自启 | 中 | 长期值守，低功耗 |
| **P5**（可选） | KUAL 菜单做交互（确认/取消/切屏） | 中 | 粗糙但可用的回传 |

> 关键：**P1 结束就应该能判断这个项目值不值得继续**。
> 不要先越狱再验证价值 —— 顺序反了。

---

## 10. 待确认的决策点

1. **状态源范围**：只做 DSH？还是同时适配 Claude Code / Codex 等？
2. **上屏内容**：只看"当前任务"，还是多任务/多 agent 泳道总览？
3. **是否越狱**：接受 P4 吗？（决定是否现在就把渲染尺寸/波形按 FBInk 优化）
4. **敏感信息策略**：全量上屏（方便但危险）还是白名单字段（安全但信息少）？
5. **交互需求**：需要从 Kindle 回传操作吗？（会显著增加复杂度）

---

## 附：参考链接

- [LanguageBreak（≤5.16.2.1.1 越狱）](https://github.com/notmarek/LanguageBreak)
- [Kindle Modding Guide — LanguageBreak 5.14.3–5.16.2.1.1](https://kindlemodding.gitbook.io/kindlemodding/jailbreak-software/languagebreak-5.14.3-5.16.2.1.1)
- [Kindle Modding Guide — 安装 KUAL/MRPI](https://kindlemodding.gitbook.io/kindlemodding/post-jailbreak/installing-kual-mrpi)
- [FBInk — framebuffer eInker](https://github.com/NiLuJe/FBInk)
- [codecube/kindle-mcp-dashboard — 同构先例](https://github.com/codecube/kindle-mcp-dashboard)
- [Kindle Paperwhite 3 规格（DP75SDI, 300 PPI）](https://www.ifixit.com/Device/Kindle_Paperwhite_3rd_Generation)
- [7–9 代机型停在 5.16.2.1.1](https://blog.the-ebook-reader.com/2024/08/23/new-kindle-software-update-5-16-21-released/)
