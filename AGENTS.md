# KindleWarden — Agent 工作规则

> 本文件由 **WorkTime** 生成（2026-10-16）。任何在本仓库工作的 Agent 都必须遵守。
> 规则改动请直接改本文件并提交，不要只改某个工具的私有副本。

## 0. WorkTime 托管的三条外设链路

| 链路 | 用途 | 何时用 |
|---|---|---|
| **项目看板** `project-manager` | 提报项目信息、思考、计划、发布 | 会话开始读计划；执行中记进展；发布留档 |
| **Kindle 副屏** `kindle-brief` | 一屏只读的结论展示 | 一个任务**做完**之后，一条 |
| **小爱音箱** `mi-voice-control` | 语音汇报 | 一个任务**做完**之后，一句话 |

固定顺序：**看板 → 副屏 → 语音**。三者都是「任务级」动作，不是「步骤级」。

三个 MCP 都走 **HTTP**（客户端只写一个 URL，不装任何本地程序），
服务器地址见第 7 节。本项目的配置只写进了**当前编辑器**的落点：
`dsh`。

## 1. 开场：先看计划，再动手

```
pm_get_daily_plan        # 先取今天的计划
```

- 返回 `status: none` 时才调 `pm_generate_daily_plan`
- **不要改写已生成的计划标题** —— 用户可能已手工调整过
- 需要背景时按代价递增：`pm_brief` → `pm_get_feed` → `pm_summary`

## 2. 执行前的约束

- 破坏性操作（删文件、改系统配置、动网络 / 容器、改他人数据）**先给执行计划等确认**
- 只读排查类任务不要顺手改配置
- 每个修复都要在真实环境验证；未验证不得声称完成
- 不把 `.env` 内容、密钥、token 写进日志、提交信息或对外输出

## 3. 提交规范

```
<type>(<scope>): <中文一句话说明>
```

- `type` ∈ `feat` `fix` `refactor` `perf` `docs` `test` `chore`
- 一个逻辑改动一个 commit，不把无关改动混在一起
- 想给看板记进度增量时，commit body 加一行 `[pm:kindlewarden] +N`
  （写了这行就**不要**再调 `pm_report_progress` 累加，会重复计算）
- **禁止** `--no-verify`；hook 失败先修根因
- 已在看板汇报过的改动，commit 时不要重复汇报

### 3.1 提交里只放项目代码

**本机开发环境一律不提交。** 它属于「这台机器」，不属于这个项目：

| 不提交 | 为什么 |
|---|---|
| 编辑器技能副本 `.workbuddy/skills/`、`.claude/skills/`、`.cursor/skills/`、`.codebuddy/skills/`、`.clinerules/skills/`、`.dsh/skills/` | 由 WorkTime 按那台机器的地址与口令生成，换台机器就得重来 |
| MCP 配置 `.mcp.json`、`.workbuddy/mcp.json`、`.codebuddy/mcp.json`、`.cursor/mcp.json`、`.vscode/mcp.json`、`.dsh/mcp.json` | 里面含服务地址，`literal` 模式下还有口令 |
| 工具专属规则副本（`.cursor/rules/`、`.clinerules/`、`.dsh/`、`.workbuddy/worktime.md`、`.codebuddy/worktime.md`、`CLAUDE.md` 视项目而定） | 规则正文只有 `AGENTS.md` 一份，副本是生成物 |
| `.env`、`.worktime/`、任何密钥 | 见下 |

它们都已在 `.gitignore` 里。**不要**用 `git add -f` 绕过去。

- 发现它们已经被跟踪了（老项目）→ `git rm -r --cached <路径>`，文件留在工作区，只是不再跟着仓库走
- 确实要把 MCP 配置分享给别的机器 / 同事 → 用 `--cred-mode env` 重新生成，
  配置里写 `${PM_MCP_AUTH}` 占位，真值放各自机器上的 `.env`
- 提交信息、代码注释、文档里**不要写**本机地址、口令、绝对路径
- 清理自己造的测试目录 / 临时文件**直接 `rm -rf`**，不要改成 `mv` 到回收站
  或 `.Trash-*/` 之类的方式躲「批量删除」确认弹窗 —— 那是绕过，不是解决。
  弹窗只是阈值提示，不是错误；躲了反而留残迹、掩盖真实行为。

## 4. 进展随时记，不要攒到最后

```
pm_report_progress(kindlewarden, content, refs={ commit: "<sha>" })
```

- `content` 的写法：**首行是结论**（一句话，能带数字就带）+ 下面分条
- 每条以标签开头：`完成` / `调整` / `修复` / `决策` / `卡点` / `问题` / `验证` / `下一步`
- **已提交的改动必须带 `refs: { commit: "<sha>" }`**，否则同一件事会在看板上显示成两行
- 思考、方案取舍用 `type: "note"`；被卡住用 `type: "blocker"`
- 一件事一条记录；只写做了什么、不写结果与影响的，重写

## 5. 收尾：三件事，缺一不可

### 5.1 看板

- 有当日任务 → `pm_complete_daily_task(task_id, note)`（会自动回写进展，不必再手动 report）
- 发布了版本 → `pm_report_release(kindlewarden, version, note, url)`，`note` 写「这一版用户能做什么」
- 想清楚下一步 → `pm_upsert_roadmap_item(kindlewarden, title, status)`
  —— **标题必须是动词开头的具体一件事**，`优化性能` / `完善文档` 这类不合格
  —— `status` 只能是 `planned` / `in_progress` / `done` / `dropped`（**没有 `todo`**）
- ⚠️ 本机部署下看板**不能自动采集 git**（容器里没挂仓库目录），
  所以「汇报必须自己带 `refs: { commit: "<sha>" }`」不是可选项 —— 不带就等于没关联

### 5.2 Kindle 副屏

```
warden_list_recent()          # 先去重
warden_publish_brief(title, status, summary, project="kindlewarden")
```

- **一个任务最多一条**。中间步骤、纯查询、单文件编辑**不发**
- `title` ≤ 20 个中文字，写**结论**不写过程
- `summary` 看板视图只显示前 28 字，第一句必须独立成句
- 同一任务的后续更新用 `supersedes: "<brief_id>"`，不要追加第二条
- 不写密钥 / 私人信息 / 大段代码 / 内部主机名

### 5.3 小爱音箱

```
speaker_tts(text="一句话口语总结：做了什么、结果如何、下一步")
```

- 只面向用户本人，口语化，**不堆技术细节**（不要说工具名、文件名、行号）
- 任务失败或需要介入时同样要播，说清卡在哪

### 5.4 需要用户决策时，**主动喊一声**

这一档**独立于任务收尾**：不是"做完了汇报"，而是**"卡住了，等你回答"**。

一旦出现下面任一情况，**先 `speaker_tts` 通知，再停下来等**：

| 情况 | 播什么 |
|---|---|
| 需要授权（sudo / OAuth / 登录 / 令牌 / 点确认弹窗） | 要授权什么、授权后会做什么、怎么拒绝 |
| 需要用户在几个方案里选 | 每个方案一句话好处与代价，然后说「你定」 |
| 缺少信息无法继续（地址、口令、路径、字段缺失） | 缺哪一个、为什么需要、去哪拿 |
| 连续失败需要人工介入（同一个错误 ≥ 2 次、改配置无果） | 现象是什么、已经试过什么、卡在哪一步 |

要点：

- **通知要早**：一发现就播，不要攒到最后。用户在等的过程中可能已经走开，
  音箱是他唯一会收到的通道
- **一步只问一件事**：把多个问题塞进一句话会让人没法回答
- 播完**就停**，不要自己猜着往下做 —— 等回复
- 问句结尾明确说「你定」或「等你回复」，不要用开放式语气让人以为还在执行
- 这条播报**可以先于**看板/副屏的收尾三件套；任务真正完成后才走那套

### 5.5 什么时候**不要**播报

- 中间步骤
- 纯信息查询（读文件、搜索、浏览）
- 与上一条同一任务的连续小改动 → 合并

## 6. 环境约定

- `.env` 进 `.gitignore`；只提交 `.env.example`
- 环境变量优先于 `.env` 文件
- npm / pnpm 走国内镜像（`https://registry.npmmirror.com`）
- 见到明文密钥被贴进对话 / 提交时，主动提醒轮换
- **提交前确认没带出本机开发环境**：`git status` 里不该出现 `.claude/`、
  `.workbuddy/`、`.cursor/`、`.clinerules/`、`.vscode/mcp.json`、`.mcp.json` 这类路径

## 7. 服务地址与体检

服务地址由**生成本项目时那台机器的档案**决定，已写进下面的表格与本项目 `.env`。
换机器 / 服务搬家时，改 `.env` 与 MCP 配置即可，不要改规则正文。

| 服务 | 地址 | MCP 端点 | 凭据 |
|---|---|---|---|
| 项目看板 | `http://192.168.9.57:8930` | `http://192.168.9.57:8930/mcp` | Basic `pm:***`（见 `.env`） |
| Kindle 副屏 | `http://192.168.9.57:8577` | `http://192.168.9.57:8577/mcp` | 无 |
| 小爱音箱 | `http://192.168.9.57:3001` | `http://192.168.9.57:3001/mcp` | 无 |

体检（三个服务连通性 + MCP 握手）：

```bash
# 脚本随 worktime 技能一起装在项目里
python3 .dsh/skills/worktime/scripts/worktime.py check
```

`CHECK_CMD` 认不出服务时，先怀疑**地址**：这三台服务很可能不在这台机器上，
地址要从本机档案 `~/.workbuddy/worktime.env` 里读，缺了就问用户，别猜。

---

本项目由 **WorkTime**（私有仓库 `yszpatt/WorkTime`）初始化。
初始化的那份档案在**执行初始化的那台机器**上（默认 `~/.workbuddy/worktime.env`），不随仓库走。
